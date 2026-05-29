"""Wiki generation pipeline (CLAUDE.md Phase 5) — the careful part.

Principles enforced here:
  * The wiki is the USER's externalized understanding; papers/notes/thoughts are
    evidence. The LLM proposes; it never authors silently.
  * Two-step analyze -> generate, modeled on llm_wiki.
  * Generation output is structured *claims*, each carrying provenance. A claim
    survives only if it cites at least one real note or thought — enforced in
    code (``_filter_claims``), not by trusting the LLM.
  * Proposed edits are written to ``proposed-edits/`` and NEVER applied
    automatically. Only ``accept_proposed`` writes into ``wiki/``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import logging

from . import frontmatter, llm, provenance
from .config import COLLECTIONS_DIR
from .db import connect
from .slugs import slugify

logger = logging.getLogger("paper_agent.wiki")

SECTIONS = ("problems", "methods", "gaps", "benchmarks", "synthesis")
# Demoted synthesis claims (no human reasoning behind them) land here as questions.
OPEN_QUESTIONS_PAGE = "gaps/open-questions.md"

# Char budgets for what we feed the generator. Keeps the prompt bounded so
# generation scales to large collections (we select the most relevant notes via
# FTS5 rather than dumping everything — SPEC.md: FTS5, no vector store).
NOTE_CHAR_BUDGET = 14000
THOUGHT_CHAR_BUDGET = 6000
HIGHLIGHT_CHAR_BUDGET = 6000
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with", "is",
    "are", "be", "this", "that", "we", "our", "by", "as", "it", "from", "at",
}


# --- paths -----------------------------------------------------------------
def _coldir(slug: str) -> Path:
    return COLLECTIONS_DIR / slug


def _wikidir(slug: str) -> Path:
    return _coldir(slug) / "wiki"


def _proposed_dir(slug: str) -> Path:
    return _coldir(slug) / "proposed-edits"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _ts_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")


# --- input gathering -------------------------------------------------------
def gather_inputs(slug: str, mode: str = "full") -> dict:
    """Collect purpose, notes, thoughts, current wiki, and the valid provenance.

    For ``incremental`` mode we still read everything (so diffs are against the
    current wiki) but record which notes/thoughts changed since last regen, so
    the prompt can focus there.
    """
    from . import thoughts as thoughts_mod

    purpose = _read(_coldir(slug) / "purpose.md")

    con = connect()
    try:
        rows = con.execute(
            "SELECT paper_id, summary, thoughts, key_quotes, status, updated_at "
            "FROM paper_notes WHERE collection_slug = ?",
            (slug,),
        ).fetchall()
        all_notes = [
            {
                "key": str(r["paper_id"]),
                "summary": r["summary"] or "",
                "thoughts": r["thoughts"] or "",
                "key_quotes": r["key_quotes"] or "",
                "status": r["status"],
                "updated_at": r["updated_at"] or "",
            }
            for r in rows
            if (r["summary"] or r["thoughts"] or r["key_quotes"])
        ]
        # Scale guard: select the most relevant notes within a char budget using
        # FTS5, rather than feeding the whole collection into the prompt.
        query = _fts_query(purpose, SECTIONS)
        notes = _select_notes(con, all_notes, query, NOTE_CHAR_BUDGET)

        # Papers + highlights the wiki may cite (the attributed branch of the gate
        # needs them). valid_papers = collection membership ∪ note papers ∪ highlight
        # papers; note keys are str(paper_id), so they overlap by construction.
        paper_ids = {str(r[0]) for r in con.execute(
            "SELECT paper_id FROM collection_papers WHERE collection_slug = ?", (slug,))}
        paper_ids |= {n["key"] for n in all_notes}
        hl_rows = con.execute(
            "SELECT id, paper_id, page, selected_text FROM annotations "
            "WHERE collection_slug = ? AND kind = 'highlight'", (slug,)
        ).fetchall()
    finally:
        con.close()

    highlights, hl_to_paper = [], {}
    used = 0
    for r in hl_rows:
        hid, pid = r["id"], str(r["paper_id"])
        text = (r["selected_text"] or "").strip()
        hl_to_paper[hid] = pid
        paper_ids.add(pid)
        if used + len(text) <= HIGHLIGHT_CHAR_BUDGET or not highlights:
            highlights.append({"id": hid, "paper": pid, "page": r["page"], "text": text[:240]})
            used += len(text)

    # Most-recent thoughts within budget (recency is the right proxy here —
    # thoughts are the user's latest thinking).
    thoughts = _select_thoughts(thoughts_mod.list_thoughts(slug), THOUGHT_CHAR_BUDGET)

    wiki: dict[str, str] = {}
    wdir = _wikidir(slug)
    if wdir.is_dir():
        for f in wdir.rglob("*.md"):
            if f.name in ("log.md",):
                continue
            wiki[str(f.relative_to(wdir))] = f.read_text(encoding="utf-8")

    return {
        "slug": slug,
        "purpose": purpose,
        "notes": notes,
        "thoughts": thoughts,
        "highlights": highlights,
        "wiki": wiki,
        # Provenance is validated against what the LLM actually saw, so a citation
        # to a note we didn't include counts as a hallucinated cite and is dropped.
        "valid_notes": {n["key"] for n in notes},
        "valid_thoughts": {t["id"] for t in thoughts},
        "valid_papers": paper_ids,
        "valid_highlights": set(hl_to_paper),
        "hl_to_paper": hl_to_paper,
        "mode": mode,
    }


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# --- input selection (scale guard, item 3) ---------------------------------
def _note_text(n: dict) -> str:
    return " ".join([n.get("summary", ""), n.get("thoughts", ""), n.get("key_quotes", "")])


def _fts_query(purpose: str, sections: tuple) -> str:
    """Build a safe FTS5 MATCH query (OR of keywords) from purpose + section names."""
    words = re.findall(r"[A-Za-z0-9]{3,}", (purpose or "").lower())
    keywords = [w for w in words if w not in _STOPWORDS]
    # dedupe preserving order, cap, and always include the section names
    seen, terms = set(), []
    for w in list(sections) + keywords:
        if w not in seen:
            seen.add(w)
            terms.append(w)
        if len(terms) >= 24:
            break
    return " OR ".join(f'"{t}"' for t in terms)


def _select_notes(con, notes: list[dict], query: str, budget: int) -> list[dict]:
    if sum(len(_note_text(n)) for n in notes) <= budget:
        return notes  # small collection: feed everything
    scores: dict[str, float] = {}
    if query:
        try:
            for r in con.execute(
                "SELECT paper_id, bm25(notes_fts) AS s FROM notes_fts "
                "WHERE notes_fts MATCH ?",
                (query,),
            ):
                scores[str(r[0])] = r[1]  # lower bm25 = more relevant
        except sqlite3.OperationalError:
            scores = {}
    # FTS-matched notes first (best score), then the rest by recency.
    def key(n):
        return (0, scores[n["key"]]) if n["key"] in scores else (1, _neg_recency(n))

    out, used = [], 0
    for n in sorted(notes, key=key):
        t = len(_note_text(n))
        if out and used + t > budget:
            break
        out.append(n)
        used += t
    return out


def _neg_recency(n: dict):
    # later updated_at sorts earlier among unmatched notes
    return tuple(-ord(c) for c in (n.get("updated_at") or ""))


def _select_thoughts(thoughts: list[dict], budget: int) -> list[dict]:
    out, used = [], 0
    for t in thoughts:  # already newest-first
        size = len(t.get("body", ""))
        if out and used + size > budget:
            break
        out.append(t)
        used += size
    return out


# --- structural lint (item 2) ----------------------------------------------
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)")


def lint_wiki(slug: str) -> list[dict]:
    """Deterministic, offline checks on the current wiki: broken/orphan links.

    Returns a list of {type, severity, message, pages}. No LLM.
    """
    wdir = _wikidir(slug)
    if not wdir.is_dir():
        return []
    pages: dict[str, str] = {}      # known slug (stem or slugified title) -> rel path
    bodies: dict[str, tuple] = {}   # rel path -> (body, title)
    for f in wdir.rglob("*.md"):
        if f.name in ("index.md", "log.md"):
            continue
        rel = str(f.relative_to(wdir)).replace(".md", "")
        meta, body = frontmatter.parse(f.read_text(encoding="utf-8"))
        title = meta.get("title", f.stem)
        pages[f.stem] = rel
        if title:
            pages[slugify(title)] = rel
        bodies[rel] = (body, title)

    issues: list[dict] = []
    inbound: dict[str, int] = defaultdict(int)
    for rel, (body, title) in bodies.items():
        targets = [slugify(t) for t in _WIKILINK_RE.findall(body)]
        if not targets:
            issues.append({"type": "no-outlink", "severity": "info",
                           "message": f"“{title}” links to no other page.", "pages": [rel]})
        for t in targets:
            if t in pages and pages[t] != rel:
                inbound[pages[t]] += 1
            elif t not in pages:
                issues.append({"type": "broken-link", "severity": "warning",
                               "message": f"“{title}” links to [[{t}]], which doesn't exist.",
                               "pages": [rel]})
    for rel, (body, title) in bodies.items():
        if inbound[rel] == 0:
            issues.append({"type": "orphan", "severity": "info",
                           "message": f"“{title}” has no inbound links.", "pages": [rel]})

    # index drift: the auto-built index (rebuild_index) lists section pages as `section/stem`.
    idx = wdir / "index.md"
    if idx.is_file():
        listed = set(re.findall(r"`([\w-]+/[\w-]+)`", idx.read_text(encoding="utf-8")))
        indexed = {rel for rel in bodies if rel.split("/", 1)[0] in SECTIONS}
        for rel in sorted(indexed - listed):
            issues.append({"type": "index-missing", "severity": "warning",
                           "message": f"“{bodies[rel][1]}” (`{rel}`) exists but isn't in the index.",
                           "pages": [rel]})
        for rel in sorted(listed - indexed):
            issues.append({"type": "index-stale", "severity": "warning",
                           "message": f"The index links `{rel}`, which no longer exists.",
                           "pages": [rel]})
    return issues


# --- page merge on regenerate (item 1) -------------------------------------
def _prov_str(c: dict) -> str:
    """Human-readable provenance for a claim's 'supported by' line."""
    parts = (
        list(c.get("papers", []))
        + [f"hl:{h}" for h in c.get("highlights", [])]
        + list(c.get("notes", []))
        + list(c.get("thoughts", []))
    )
    return ", ".join(str(p) for p in parts)


def _merge_fallback_body(old_body: str, claims: list[dict]) -> str:
    """Deterministic, no-LLM merge: keep the user's body verbatim and append only
    new grounded claim bullets not already present. Never loses hand-edits."""
    new_bullets = []
    for c in claims:
        text = c["text"].strip()
        if text and text not in old_body:
            prov = _prov_str(c)
            new_bullets.append(f"- {text} _(supported by: {prov})_")
    if not new_bullets:
        return old_body.rstrip() + "\n"
    return old_body.rstrip() + "\n\n## New (from latest notes/thoughts)\n" + "\n".join(new_bullets) + "\n"


def _merge_body_llm(old_body: str, claims: list[dict]) -> str | None:
    """LLM merge of the user's current page with new grounded claims. Returns None
    on any failure so the caller falls back to the deterministic merge."""
    additions = "\n".join(f"- {c['text']}" for c in claims)
    try:
        return llm.complete(
            [
                {"role": "system", "content": (
                    "You merge a researcher's existing wiki page with new grounded "
                    "points. PRESERVE the user's existing wording and structure; "
                    "integrate the new points where they fit; do NOT remove the "
                    "user's content unless a new point directly supersedes it; do "
                    "NOT add any fact not present in either input. Output markdown "
                    "body only (no frontmatter)."
                )},
                {"role": "user", "content": (
                    f"EXISTING PAGE BODY:\n{old_body}\n\nNEW GROUNDED POINTS:\n{additions}"
                )},
            ]
        ).strip()
    except Exception:  # noqa: BLE001 - fall back to deterministic merge
        return None


def _merge_into(old_content: str, page: dict, claims: list[dict], use_llm: bool = True) -> str:
    """Merge new claims into an existing page, preserving the user's content.

    Frontmatter provenance lists are unioned deterministically (never via LLM);
    the body is merged by LLM when available, else by the safe fallback.
    """
    old_meta, old_body = frontmatter.parse(old_content)
    # union provenance from old frontmatter + new claims
    def union(field, extra):
        vals = list(old_meta.get(field, []) or [])
        for x in extra:
            if x not in vals:
                vals.append(x)
        return sorted(vals)

    sources, dn, dt, dh = set(), set(), set(), set()
    for c in claims:
        sources.update(c.get("papers", [])); dn.update(c.get("notes", []))
        dt.update(c.get("thoughts", [])); dh.update(str(h) for h in c.get("highlights", []))
    meta = {
        "type": old_meta.get("type", page["section"]),
        "title": old_meta.get("title", page.get("title", "")),
        "sources": union("sources", sources),
        "derived_from_notes": union("derived_from_notes", dn),
        "derived_from_thoughts": union("derived_from_thoughts", dt),
        "derived_from_highlights": union("derived_from_highlights", dh),
        "last_regen": _now(),
    }
    body = (_merge_body_llm(old_body, claims) if use_llm else None)
    if body is None:
        body = _merge_fallback_body(old_body, claims)
    return frontmatter.dump(meta, body)


# --- LLM steps -------------------------------------------------------------
def _extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # last resort: find the outermost object
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1:
            return json.loads(text[s : e + 1])
        raise


def _notes_digest(notes: list[dict]) -> str:
    out = []
    for n in notes:
        parts = [f"[note {n['key']}]"]
        if n["summary"]:
            parts.append(f"summary: {n['summary']}")
        if n["thoughts"]:
            parts.append(f"thoughts: {n['thoughts']}")
        if n["key_quotes"]:
            parts.append(f"quotes: {n['key_quotes']}")
        out.append("\n".join(parts))
    return "\n\n".join(out)


def _thoughts_digest(thoughts: list[dict]) -> str:
    return "\n\n".join(f"[thought {t['id']}]\n{t['body']}" for t in thoughts)


def _highlights_digest(highlights: list[dict]) -> str:
    return "\n".join(
        f"[highlight {h['id']}] (paper {h['paper']}, p.{h['page']}) {h['text']}"
        for h in highlights
    )


def analyze(inputs: dict) -> dict:
    """Step 1: what's new/changed and which sections need work."""
    prompt = (
        "You are helping a researcher maintain a personal wiki about a paper "
        "collection. The wiki sections are: problems, methods, gaps, benchmarks, "
        "synthesis. Based ONLY on the user's purpose, notes, and thoughts below, "
        "decide which sections need updating and summarize what is new.\n\n"
        f"PURPOSE:\n{inputs['purpose'] or '(none)'}\n\n"
        f"NOTES:\n{_notes_digest(inputs['notes']) or '(none)'}\n\n"
        f"THOUGHTS:\n{_thoughts_digest(inputs['thoughts']) or '(none)'}\n\n"
        'Respond with JSON: {"sections": ["problems", ...], "summary": "..."}'
    )
    resp = llm.complete(
        [
            {"role": "system", "content": "You output only valid JSON."},
            {"role": "user", "content": prompt},
        ]
    )
    try:
        data = _extract_json(resp)
    except (json.JSONDecodeError, ValueError):
        data = {"sections": list(SECTIONS), "summary": ""}
    secs = [s for s in data.get("sections", []) if s in SECTIONS] or list(SECTIONS)
    return {"sections": secs, "summary": data.get("summary", "")}


def generate(inputs: dict, analysis: dict) -> list[dict]:
    """Step 2: produce pages as claims with provenance. Returns raw page dicts."""
    valid_keys = sorted(inputs["valid_notes"])
    valid_tids = sorted(inputs["valid_thoughts"])
    valid_papers = sorted(inputs.get("valid_papers", set()))
    valid_hls = sorted(inputs.get("valid_highlights", set()))
    prompt = (
        "Produce wiki page content for the sections listed. Ground every claim; DO "
        "NOT invent facts. Label each claim's claim_type:\n"
        "  - \"attributed\": restates what a paper reports. It MUST cite a paper key "
        "or highlight id (the source's own words), not just your note about it.\n"
        "  - \"synthesis\": your own cross-paper conclusion/connection. It should cite "
        "the thought id or note whose reasoning supports it.\n"
        "Cite only ids from the allowed lists.\n\n"
        f"SECTIONS TO UPDATE: {analysis['sections']}\n"
        f"ALLOWED NOTE KEYS: {valid_keys}\n"
        f"ALLOWED THOUGHT IDS: {valid_tids}\n"
        f"ALLOWED PAPER KEYS: {valid_papers}\n"
        f"ALLOWED HIGHLIGHT IDS: {valid_hls}\n\n"
        f"PURPOSE:\n{inputs['purpose'] or '(none)'}\n\n"
        f"NOTES:\n{_notes_digest(inputs['notes']) or '(none)'}\n\n"
        f"THOUGHTS:\n{_thoughts_digest(inputs['thoughts']) or '(none)'}\n\n"
        f"HIGHLIGHTS:\n{_highlights_digest(inputs.get('highlights', [])) or '(none)'}\n\n"
        "Respond with JSON: {\"pages\": [{\"section\": \"problems\", "
        "\"slug\": \"short-kebab\", \"title\": \"...\", \"claims\": "
        "[{\"text\": \"one sentence\", \"claim_type\": \"attributed\", "
        "\"notes\": [\"KEY\"], \"thoughts\": [\"TID\"], \"papers\": [\"KEY\"], "
        "\"highlights\": [ID]}]}]}"
    )
    resp = llm.complete(
        [
            {"role": "system", "content": "You output only valid JSON."},
            {"role": "user", "content": prompt},
        ]
    )
    try:
        data = _extract_json(resp)
    except (json.JSONDecodeError, ValueError):
        return []
    return data.get("pages", [])


# --- the gate (pure, unit-testable) ----------------------------------------
# Outcomes. ACCEPT/ASSERT become real claims; DEMOTE becomes an open question;
# REJECT is dropped. The attribution boundary lives here, not in any prompt.
ACCEPT, ASSERT, DEMOTE, REJECT = "accept", "assert", "demote", "reject"


def _ctx(inputs: dict) -> dict:
    """The minimal context the gate needs to resolve and validate refs."""
    return {
        "slug": inputs.get("slug"),
        "valid_notes": inputs.get("valid_notes", set()),
        "valid_thoughts": inputs.get("valid_thoughts", set()),
        "valid_papers": inputs.get("valid_papers", set()),
        "valid_highlights": inputs.get("valid_highlights", set()),
        "hl_to_paper": inputs.get("hl_to_paper", {}),
    }


def _clean_refs(claim: dict, ctx: dict) -> dict:
    """Drop hallucinated/invalid citations; normalize highlight ids to int."""
    hls = []
    for h in claim.get("highlights") or []:
        try:
            hid = int(h)
        except (TypeError, ValueError):
            continue
        if hid in ctx["valid_highlights"]:
            hls.append(hid)
    return {
        "text": (claim.get("text") or "").strip(),
        "notes": [n for n in (claim.get("notes") or []) if n in ctx["valid_notes"]],
        "thoughts": [t for t in (claim.get("thoughts") or []) if t in ctx["valid_thoughts"]],
        "papers": [p for p in (claim.get("papers") or []) if p in ctx["valid_papers"]],
        "highlights": hls,
    }


def _distinct_papers(clean: dict, hl_to_paper: dict) -> set:
    """Papers a claim implicates: explicit papers, notes (key == paper_id), and the
    papers behind cited highlights. Used only for the structural claim_type floor."""
    papers = set(clean["papers"]) | set(clean["notes"])
    papers |= {hl_to_paper.get(h) for h in clean["highlights"] if hl_to_paper.get(h)}
    return papers


def _structural_type(clean: dict, hl_to_paper: dict) -> str:
    """Code's floor for claim_type from provenance shape: a claim spanning ≥2 papers
    or leaning on a thought is synthesis; otherwise attributed."""
    if len(_distinct_papers(clean, hl_to_paper)) >= 2 or clean["thoughts"]:
        return "synthesis"
    return "attributed"


def _stricter(a: str, b: str) -> str:
    # synthesis is the higher bar (needs human reasoning), so it wins the max.
    return "synthesis" if "synthesis" in (a, b) else "attributed"


def _has_human_reasoning(clean: dict, ctx: dict) -> bool:
    """True iff some cited ref resolves to (reasoning, human) — the only grounding
    that lets a synthesis claim assert rather than demote to an open question."""
    for rtype, ids in (("note", clean["notes"]), ("thought", clean["thoughts"])):
        for rid in ids:
            kind, origin = provenance.effective_stamp({"type": rtype, "id": rid}, ctx["slug"])
            if kind == "reasoning" and origin == "human":
                return True
    return False


def gate(claim: dict, ctx: dict) -> tuple[str, dict]:
    """Classify one proposed claim and decide its fate, in code.

    Returns (outcome, clean_claim). claim_type = stricter(structural floor, the
    agent's label) so a mislabel can only over-demote (safe), never under-gate.
    """
    clean = _clean_refs(claim, ctx)
    if not clean["text"] or not any(
        clean[k] for k in ("notes", "thoughts", "papers", "highlights")
    ):
        return REJECT, clean  # nothing valid to stand on

    structural = _structural_type(clean, ctx["hl_to_paper"])
    label = claim.get("claim_type") if claim.get("claim_type") in ("attributed", "synthesis") else structural
    clean["claim_type"] = _stricter(structural, label)

    if clean["claim_type"] == "attributed":
        # Must cite the source itself (a paper or a highlight), not just your note.
        return (ACCEPT if (clean["papers"] or clean["highlights"]) else REJECT), clean
    # synthesis: assert only if the human's reasoning backs it; else open question.
    return (ASSERT if _has_human_reasoning(clean, ctx) else DEMOTE), clean


def _build_page(page: dict, claims: list[dict]) -> tuple[str, str]:
    """Return (page_path, full_markdown_with_frontmatter) from surviving claims."""
    section = page["section"]
    slug = re.sub(r"[^a-z0-9]+", "-", (page.get("slug") or "page").lower()).strip("-")
    page_path = f"{section}/{slug}.md"
    sources, dn, dt, dh = set(), set(), set(), set()
    for c in claims:
        sources.update(c.get("papers", []))
        dn.update(c.get("notes", []))
        dt.update(c.get("thoughts", []))
        dh.update(str(h) for h in c.get("highlights", []))
    meta = {
        "type": section,
        "title": page.get("title", slug),
        "sources": sorted(sources),
        "derived_from_notes": sorted(dn),
        "derived_from_thoughts": sorted(dt),
        "derived_from_highlights": sorted(dh),
        "last_regen": _now(),
    }
    body_lines = [f"# {page.get('title', slug)}", ""]
    for c in claims:
        body_lines.append(f"- {c['text']} _(supported by: {_prov_str(c)})_")
    body = "\n".join(body_lines)
    return page_path, frontmatter.dump(meta, body)


# --- proposal IO -----------------------------------------------------------
def _persist_proposal(slug, pdir, page_path, title, section, mode, old, new, claims) -> dict:
    """Write one proposal JSON (diff against the *current* wiki). Applies nothing."""
    diff = "".join(
        difflib.unified_diff(
            (old + "\n").splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{page_path}", tofile=f"b/{page_path}",
        )
    )
    edit_id = f"{_ts_id()}--{page_path.replace('/', '__')}"
    proposal = {
        "id": edit_id, "slug": slug, "page_path": page_path, "title": title,
        "section": section, "mode": mode, "created_at": _now(),
        "old_content": old, "new_content": new, "claims": claims, "diff": diff,
    }
    (pdir / f"{edit_id}.json").write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    return proposal


def _build_questions_content(slug: str, questions: list[dict]) -> str:
    """The gaps/open-questions page: demoted synthesis claims framed as questions the
    human still owes reasoning for. Preserves any existing body; appends new ones."""
    old = _read(_wikidir(slug) / OPEN_QUESTIONS_PAGE)
    old_meta, old_body = frontmatter.parse(old) if old.strip() else ({}, "")
    dn, dt = set(old_meta.get("derived_from_notes", []) or []), set(old_meta.get("derived_from_thoughts", []) or [])
    bullets = []
    for q in questions:
        text = q["text"].strip()
        if text and text not in old_body:
            bullets.append(f"- {text} _(open question — needs your reasoning; from: {_prov_str(q)})_")
        dn.update(q.get("notes", [])); dt.update(q.get("thoughts", []))
    meta = {
        "type": "gaps", "title": old_meta.get("title", "Open Questions"),
        "sources": sorted(set(old_meta.get("sources", []) or [])),
        "derived_from_notes": sorted(dn), "derived_from_thoughts": sorted(dt),
        "last_regen": _now(),
    }
    if old_body.strip():
        body = old_body.rstrip() + ("\n" + "\n".join(bullets) if bullets else "") + "\n"
    else:
        body = "# Open Questions\n\n" + "\n".join(bullets) + "\n"
    return frontmatter.dump(meta, body)


def process_pages(slug: str, raw_pages: list[dict], inputs: dict, mode: str) -> dict:
    """Gate a list of proposed pages and write the survivors to ``proposed-edits/``.

    Shared by ``run_generation`` (LLM-produced pages) and the MCP ``submit_proposal``
    tool (agent-produced pages) so neither can bypass the gate. Returns a summary:
    ``{proposals, written, demoted, rejected}``.
    """
    ctx = _ctx(inputs)
    proposals, open_questions = [], []
    demoted = rejected = 0
    pdir = _proposed_dir(slug)
    pdir.mkdir(parents=True, exist_ok=True)
    for page in raw_pages:
        if page.get("section") not in SECTIONS:
            continue
        assertions = []
        for raw in page.get("claims", []):
            outcome, clean = gate(raw, ctx)
            if outcome in (ACCEPT, ASSERT):
                assertions.append(clean)
            elif outcome == DEMOTE:
                open_questions.append(clean); demoted += 1
                logger.info("wiki.gate DEMOTE slug=%s section=%s text=%r",
                            slug, page["section"], clean["text"][:80])
            else:  # REJECT
                rejected += 1
                logger.info("wiki.gate REJECT slug=%s section=%s text=%r",
                            slug, page["section"], clean["text"][:80])
        if not assertions:
            continue
        page_path, fresh = _build_page(page, assertions)
        old = _read(_wikidir(slug) / page_path)
        new = _merge_into(old, page, assertions) if old.strip() else fresh  # never clobber hand-edits
        proposals.append(_persist_proposal(
            slug, pdir, page_path, page.get("title", page_path), page["section"],
            mode, old, new, assertions))

    if open_questions:
        new = _build_questions_content(slug, open_questions)
        old = _read(_wikidir(slug) / OPEN_QUESTIONS_PAGE)
        proposals.append(_persist_proposal(
            slug, pdir, OPEN_QUESTIONS_PAGE, "Open Questions", "gaps",
            mode, old, new, open_questions))
    return {"proposals": proposals, "written": len(proposals),
            "demoted": demoted, "rejected": rejected}


def brainstorm_pages(slug: str, pages: list[dict], mode: str = "brainstorm") -> dict:
    """Persist agent brainstorm pages to the review queue (AGENTIC_PLAN P7). GATE-EXEMPT
    on purpose: brainstorm is explicitly speculative (agent) content, quarantined under
    wiki/brainstorming/ and clearly labeled — it can never ground a grounded-wiki claim.
    Still goes through accept, so 'only accept writes wiki/' holds."""
    pdir = _proposed_dir(slug)
    pdir.mkdir(parents=True, exist_ok=True)
    written = []
    for pg in pages if isinstance(pages, list) else []:
        title = (pg.get("title") or "Brainstorm").strip()
        pslug = re.sub(r"[^a-z0-9]+", "-", (pg.get("slug") or "brainstorm").lower()).strip("-") or "brainstorm"
        page_path = f"brainstorming/{pslug}.md"
        body = (pg.get("body") or "").strip()
        sources = sorted(str(s) for s in (pg.get("sources") or []))
        meta = {"type": "brainstorming", "title": title, "author_origin": "agent",
                "sources": sources, "last_regen": _now()}
        content = frontmatter.dump(meta, (
            f"# {title}\n\n{body}\n\n"
            "_Speculative — machine-generated brainstorm. Not your reasoning; it cannot "
            "ground a wiki claim. Write your own take to make any of it count._"))
        old = _read(_wikidir(slug) / page_path)
        written.append(_persist_proposal(slug, pdir, page_path, title, "brainstorming",
                                         mode, old, content, []))
    return {"written": len(written), "pages": [p["page_path"] for p in written]}


def run_generation(slug: str, mode: str = "full") -> list[dict]:
    """Full pipeline: analyze -> generate -> gate -> write proposed edits. Applies
    nothing. Returns the proposals written."""
    inputs = gather_inputs(slug, mode)
    analysis = analyze(inputs)
    raw_pages = generate(inputs, analysis)
    return process_pages(slug, raw_pages, inputs, mode)["proposals"]


def proposal_from_chat(
    slug: str, assistant_text: str, refs: list[dict], section: str
) -> dict | None:
    """Create a proposed edit from a chat turn (Phase 6).

    The turn's refs (the context it used) are the only citable provenance — the claim
    goes through ``gate`` exactly like a generated one, so chat can't bypass the
    attribution boundary. Returns the proposal, or None if the gate rejects it.
    """
    if section not in SECTIONS:
        section = "synthesis"
    notes = [str(r["id"]) for r in refs if r.get("type") == "note" and r.get("id")]
    thoughts = [str(r["id"]) for r in refs if r.get("type") == "thought" and r.get("id")]
    papers = [str(r["id"]) for r in refs if r.get("type") == "paper" and r.get("id")]
    claim = {"text": assistant_text.strip(), "notes": notes, "thoughts": thoughts,
             "papers": papers, "highlights": []}
    ctx = {"slug": slug, "valid_notes": set(notes), "valid_thoughts": set(thoughts),
           "valid_papers": set(papers), "valid_highlights": set(), "hl_to_paper": {}}
    outcome, clean = gate(claim, ctx)
    if outcome == REJECT:
        return None

    pdir = _proposed_dir(slug)
    pdir.mkdir(parents=True, exist_ok=True)
    if outcome == DEMOTE:
        # ungrounded synthesis from chat -> open question, not an assertion
        new = _build_questions_content(slug, [clean])
        old = _read(_wikidir(slug) / OPEN_QUESTIONS_PAGE)
        return _persist_proposal(slug, pdir, OPEN_QUESTIONS_PAGE, "Open Questions",
                                 "gaps", "from-chat", old, new, [clean])
    page = {"section": section, "slug": "from-chat", "title": f"{section.capitalize()} (from chat)"}
    page_path, new = _build_page(page, [clean])
    old = _read(_wikidir(slug) / page_path)
    return _persist_proposal(slug, pdir, page_path, page["title"], section,
                             "from-chat", old, new, [clean])


def list_proposed(slug: str) -> list[dict]:
    pdir = _proposed_dir(slug)
    if not pdir.is_dir():
        return []
    out = []
    for f in sorted(pdir.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return out


def get_proposed(slug: str, edit_id: str) -> dict | None:
    f = _proposed_dir(slug) / f"{edit_id}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def reject_proposed(slug: str, edit_id: str) -> bool:
    f = _proposed_dir(slug) / f"{edit_id}.json"
    if not f.exists():
        return False
    f.unlink()
    return True


def accept_proposed(slug: str, edit_id: str, edited_content: str | None = None) -> bool:
    """The ONLY path that writes into wiki/. Writes page, index, log."""
    prop = get_proposed(slug, edit_id)
    if not prop:
        return False
    content = edited_content if edited_content is not None else prop["new_content"]
    target = _wikidir(slug) / prop["page_path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rebuild_index(slug)
    _append_log(slug, f"accepted {prop['page_path']} ({prop['mode']})", content)
    reject_proposed(slug, edit_id)  # remove from queue
    return True


def rebuild_index(slug: str) -> None:
    wdir = _wikidir(slug)
    wdir.mkdir(parents=True, exist_ok=True)
    lines = ["# Wiki Index", ""]
    for section in SECTIONS:
        sdir = wdir / section
        pages = sorted(sdir.glob("*.md")) if sdir.is_dir() else []
        if not pages:
            continue
        lines.append(f"## {section.capitalize()}")
        for p in pages:
            meta, body = frontmatter.parse(p.read_text(encoding="utf-8"))
            title = meta.get("title", p.stem)
            first = next((ln for ln in body.splitlines()
                           if ln.strip() and not ln.startswith(("#", ">"))), "")
            rel = f"{section}/{p.stem}"
            summary = f" — {first[:100]}" if first else ""
            lines.append(f"- [[{title}]] (`{rel}`){summary}")
        lines.append("")
    (wdir / "index.md").write_text("\n".join(lines), encoding="utf-8")


def _append_log(slug: str, reason: str, content: str) -> None:
    log = _wikidir(slug) / "log.md"
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    line = f"- {_now()} · {reason} · inputs_hash={h}\n"
    if not log.exists():
        log.write_text("# Generation Log\n\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as f:
        f.write(line)


# --- starter wiki on import (CLAUDE.md amendment 2026-05-27) --------------------
# Normally the wiki is the USER's externalised thinking and only an accepted proposed edit
# writes it. By explicit, user-opted (default-on, uncheckable) choice, an import may also
# SEED a starter draft from the papers' ABSTRACTS. It is clearly tagged as a machine seed
# (author_origin=agent + an in-body banner) and is NON-DESTRUCTIVE: it only runs when the
# wiki is empty, so it can never overwrite the user's own writing.
_SEED_BANNER = ("> _Starter draft auto-generated from the papers' abstracts — a starting "
                "point, **not** your own synthesis yet. Edit, rewrite, or delete freely._\n\n")


def _has_wiki_pages(slug: str) -> bool:
    wdir = _wikidir(slug)
    return any((wdir / s).is_dir() and any((wdir / s).glob("*.md")) for s in SECTIONS)


def _scaffold(slug: str) -> None:
    """Create the empty wiki structure (index + log + section dirs). No LLM, no content."""
    wdir = _wikidir(slug)
    for section in SECTIONS:
        (wdir / section).mkdir(parents=True, exist_ok=True)
    rebuild_index(slug)
    if not (wdir / "log.md").exists():
        _append_log(slug, "wiki initialized (scaffold)", slug)


def _extract_abstract(text: str) -> str:
    """Pull a paper's abstract out of first-page text: the block after an 'Abstract' heading up
    to the introduction/keywords. Best-effort; returns '' if not found."""
    if not text:
        return ""
    m = re.search(r"\babstract\b[\s:.\-—]*", text, re.IGNORECASE)
    if not m:
        return ""
    after = text[m.end():]
    end = re.search(r"\b(1[\s.)]*\s*introduction|introduction|keywords|index terms|ccs concepts)\b",
                    after, re.IGNORECASE)
    chunk = after[:end.start()] if end else after[:1800]
    return " ".join(chunk.split())[:1800]


def _pdf_excerpt(paper_id: int, max_chars: int = 2500) -> str:
    """First ~max_chars of the cached PDF's text — used by the 'Deepen with PDFs' overview pass.
    Returns '' if no PDF cached or extraction fails."""
    from . import pdf_store, pdf_text
    if not pdf_store.has_pdf(paper_id):
        return ""
    try:
        return pdf_text.extract_text(pdf_store.pdf_dest(paper_id), max_chars=max_chars)
    except Exception:  # noqa: BLE001
        return ""


def _pdf_abstract(paper_id: int) -> str:
    """The abstract read from a paper's cached PDF (first two pages). '' if no PDF / not found."""
    from . import pdf_store

    if not pdf_store.has_pdf(paper_id):
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_store.pdf_dest(paper_id)))
        text = "\n".join((reader.pages[i].extract_text() or "") for i in range(min(2, len(reader.pages))))
    except Exception:  # noqa: BLE001
        return ""
    return _extract_abstract(text)


def _collection_abstracts(slug: str) -> list[dict]:
    """Imported papers with a citable ref + abstract, for seeding the wiki. Falls back to the
    PDF's own abstract when Zotero/DB has none (common for bare-PDF Zotero imports)."""
    con = connect()
    try:
        rows = con.execute(
            """SELECT p.id, p.title, p.abstract, p.zotero_key, p.arxiv_id, p.openreview_id
               FROM collection_papers cp JOIN papers p ON p.id = cp.paper_id
               WHERE cp.collection_slug = ?
                 AND NOT EXISTS (SELECT 1 FROM pending_removals pr
                                 WHERE pr.collection_slug=cp.collection_slug AND pr.paper_id=cp.paper_id)""",
            (slug,),
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        ref = r["zotero_key"] or r["arxiv_id"] or r["openreview_id"] or str(r["id"])
        abstract = (r["abstract"] or "").strip() or _pdf_abstract(r["id"])
        out.append({"id": r["id"], "ref": ref, "title": r["title"] or "", "abstract": abstract})
    return out


def _overview_path(slug: str) -> Path:
    return _wikidir(slug) / "overview.json"


# Starter-wiki content shape (the curiosity-driven blueprint, 2026-05-29).
# Field overview free-text fields the agent is allowed to fill.
_FIELD_OVERVIEW_FIELDS = ("one_sentence", "one_paragraph", "core_tension",
                          "why_matters", "what_changed_recently",
                          "what_newcomer_should_notice")
# Per-paper card status / difficulty enums. Anything else from the agent is dropped.
_PAPER_CARD_STATUS = {"foundation", "method", "benchmark", "empirical", "critique",
                      "survey", "application", "bridge", "outdated"}
_PAPER_CARD_DIFFICULTY = {"easy", "medium", "hard"}
# A reading path needs at least this many concrete papers or it gets dropped (the
# blueprint's "fabrication guard" — don't surface a path the collection can't honestly fill).
_READING_PATH_MIN_PAPERS = 3


def _validate_overview(data: dict, valid: set, pdf_refs: set) -> dict:
    """Validate the curiosity-driven starter-wiki output. Drops hallucinated refs and
    any field-overview text that's empty. PDF-only fields (mechanism/evidence/limitation)
    are blanked on cards whose paper wasn't in the PDF-excerpt set — the agent shouldn't
    have visible evidence for them, so we don't display its guesses.

    Reading paths with fewer than _READING_PATH_MIN_PAPERS papers are dropped; we'd
    rather show zero paths than fake one (Beginner / Critical paths from a homogeneous
    collection were the main pre-rule failure mode)."""
    def keep(refs):
        return [r for r in (refs or []) if r in valid]
    def text(s):
        return (s or "").strip()

    # Field overview ----------------------------------------------------------------
    fo = data.get("field_overview") if isinstance(data.get("field_overview"), dict) else {}
    # Migration: old shape had a top-level "intro" — fold it into one_paragraph.
    if not fo.get("one_paragraph") and data.get("intro"):
        fo = dict(fo, one_paragraph=data["intro"])
    field_overview = {k: text(fo.get(k)) for k in _FIELD_OVERVIEW_FIELDS}

    # Problems ----------------------------------------------------------------------
    problems = []
    for p in (data.get("problems") or []):
        if not isinstance(p, dict) or not text(p.get("title")):
            continue
        approaches = [{"label": text(a.get("label")), "papers": keep(a.get("papers"))}
                      for a in (p.get("approaches") or []) if isinstance(a, dict) and a.get("label")]
        rf = p.get("read_first") if isinstance(p.get("read_first"), dict) else {}
        rf_paper = rf.get("paper") if rf.get("paper") in valid else None
        problems.append({
            "title": text(p["title"]),
            "why": text(p.get("why")),
            "tension": text(p.get("tension")),
            "approaches": [a for a in approaches if a["papers"]],
            "read_first": rf_paper,
            "read_first_why": text(rf.get("why")) if rf_paper else "",
            "papers": keep(p.get("papers")),
        })

    def grouped(key: str) -> list[dict]:
        """methods / benchmarks share a shape: title / key_idea / body / papers[]."""
        out = []
        for x in (data.get(key) or []):
            if not isinstance(x, dict) or not text(x.get("title")):
                continue
            out.append({
                "title": text(x["title"]),
                "key_idea": text(x.get("key_idea")),
                "body": text(x.get("body")),
                "papers": keep(x.get("papers")),
            })
        return out

    # Open problems — renamed from `gaps`; accept either key for migration ----------
    raw_op = data.get("open_problems") if data.get("open_problems") is not None else data.get("gaps")
    open_problems = []
    for g in (raw_op or []):
        if not isinstance(g, dict) or not text(g.get("title")):
            continue
        open_problems.append({
            "title": text(g["title"]),
            "body": text(g.get("body")),
            "papers": keep(g.get("sources") or g.get("papers")),
        })

    # Paper cards — NEW (the heart of the blueprint) -------------------------------
    paper_cards = []
    for pc in (data.get("paper_cards") or []):
        if not isinstance(pc, dict):
            continue
        ref = pc.get("paper")
        if ref not in valid:
            continue
        connected = []
        for c in (pc.get("connected_papers") or []):
            if not isinstance(c, dict):
                continue
            cref = c.get("paper")
            if cref not in valid or cref == ref:
                continue
            connected.append({"paper": cref, "relation": text(c.get("relation")),
                              "why": text(c.get("why"))})
        prereqs = [r for r in keep(pc.get("prerequisites")) if r != ref]
        status = text(pc.get("status")).lower()
        diff = text(pc.get("difficulty")).lower()
        is_abstract_only = ref not in pdf_refs
        paper_cards.append({
            "paper": ref,
            "status": status if status in _PAPER_CARD_STATUS else "",
            "problem": text(pc.get("problem")),
            "idea": text(pc.get("idea")),
            "method_family": text(pc.get("method_family")),
            "contribution": text(pc.get("contribution")),
            "why_read": text(pc.get("why_read")),
            "difficulty": diff if diff in _PAPER_CARD_DIFFICULTY else "",
            "prerequisites": prereqs,
            "connected_papers": connected,
            # PDF-only fields — blanked when no excerpt was supplied (honest about what was read).
            "mechanism": "" if is_abstract_only else text(pc.get("mechanism")),
            "evidence": "" if is_abstract_only else text(pc.get("evidence")),
            "limitation": "" if is_abstract_only else text(pc.get("limitation")),
            "abstract_only": is_abstract_only,
        })

    # Reading paths (plural). Old top-level singular `reading_path` migrates to one
    # "Orientation" entry so old overviews still render after a reload. -------------
    reading_paths = []
    for rp in (data.get("reading_paths") or []):
        if not isinstance(rp, dict):
            continue
        name = text(rp.get("name"))
        if not name:
            continue
        ordered, seen = [], set()
        for step in (rp.get("ordered_papers") or []):
            if not isinstance(step, dict):
                continue
            sref = step.get("paper")
            if sref not in valid or sref in seen:
                continue
            seen.add(sref)
            ordered.append({"paper": sref, "why_now": text(step.get("why_now")),
                            "focus_on": text(step.get("focus_on")), "skip": text(step.get("skip"))})
        if len(ordered) >= _READING_PATH_MIN_PAPERS:
            reading_paths.append({"name": name, "for_who": text(rp.get("for_who")),
                                  "goal": text(rp.get("goal")), "ordered_papers": ordered})
    if not reading_paths and data.get("reading_path"):
        legacy = [r for r in (data.get("reading_path") or []) if r in valid][:6]
        if len(legacy) >= _READING_PATH_MIN_PAPERS:
            reading_paths.append({
                "name": "Orientation",
                "for_who": "anyone new to this collection",
                "goal": "Get the lay of the land",
                "ordered_papers": [{"paper": r, "why_now": "", "focus_on": "", "skip": ""}
                                   for r in legacy],
            })

    return {
        "field_overview": field_overview,
        "problems": problems,
        "methods": grouped("methods"),
        "open_problems": open_problems,
        "benchmarks": grouped("benchmarks"),
        "paper_cards": paper_cards,
        "reading_paths": reading_paths,
    }


def _flag_underread(slug: str, limit: int = 10) -> list[dict]:
    """Heuristic: papers in this collection with NO highlights AND no notes content.
    Surfaced by the curator as 'go back to these' candidates. Sorted most-recent-added first."""
    from .db import connect
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT p.id, p.title FROM papers p
            JOIN collection_papers cp ON cp.paper_id = p.id
            WHERE cp.collection_slug = ?
              AND NOT EXISTS (SELECT 1 FROM pending_removals pr
                              WHERE pr.collection_slug = cp.collection_slug AND pr.paper_id = cp.paper_id)
              AND NOT EXISTS (SELECT 1 FROM annotations a
                              WHERE a.paper_id = p.id AND a.collection_slug = cp.collection_slug
                                AND a.kind = 'highlight')
              AND NOT EXISTS (SELECT 1 FROM paper_notes n
                              WHERE n.paper_id = p.id AND n.collection_slug = cp.collection_slug
                                AND (COALESCE(n.summary,'')<>'' OR COALESCE(n.thoughts,'')<>''
                                     OR COALESCE(n.key_quotes,'')<>''))
            ORDER BY p.added_at DESC LIMIT ?
            """,
            (slug, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def run_curator(slug: str) -> dict:
    """Manual wiki refresh — orchestrates the existing pipelines in one pass: organizer drafts
    proposed wiki edits, debt finder surfaces open questions, arxiv gap-fill files new triage
    candidates, and under-read papers are flagged. Per-pipeline errors are isolated."""
    from . import debt, discover, organizer, triage
    out: dict = {"edits": {"new": [], "total_pending": 0},
                 "questions": {"new": [], "open_total": 0},
                 "triage": {"pending_total": 0, "items": []},
                 "arxiv": {"added": 0, "items": []},
                 "underread": {"papers": []},
                 "health": {"findings": []},
                 "errors": []}
    # 1) proposed wiki edits (organizer → propose→review queue, never direct)
    try:
        before = {p["id"] for p in list_proposed(slug)}
        organizer.organize(slug, mode="incremental")
        after = list_proposed(slug)
        out["edits"] = {"new": [p for p in after if p["id"] not in before],
                        "total_pending": len(after)}
    except Exception as exc:  # noqa: BLE001 - per-pipeline isolation
        out["errors"].append({"step": "proposed edits", "msg": str(exc)})
        try: out["edits"]["total_pending"] = len(list_proposed(slug))
        except Exception: pass
    # 2) open questions (debt finder)
    try:
        before = {d["id"] for d in debt.list_debt(slug)}
        debt.find_debt(slug)
        out["questions"] = {"new": [d for d in debt.list_debt(slug) if d["id"] not in before],
                            "open_total": debt.count_open(slug)}
    except Exception as exc:  # noqa: BLE001
        out["errors"].append({"step": "open questions", "msg": str(exc)})
        try: out["questions"]["open_total"] = debt.count_open(slug)
        except Exception: pass
    # 3) arxiv gap-fill → file new triage candidates (capped, dedupe is the table's job)
    try:
        gaps = discover.find_gaps(slug) or []
        added = []
        for g in gaps[:5]:
            tid = triage.add_from_arxiv(slug, g["arxiv_id"], g["title"], g.get("note", ""))
            if tid:
                added.append({"arxiv_id": g["arxiv_id"], "title": g["title"], "note": g.get("note", "")})
        out["arxiv"] = {"added": len(added), "items": added}
    except Exception as exc:  # noqa: BLE001
        out["errors"].append({"step": "arxiv gap-fill", "msg": str(exc)})
    # 4) triage inbox snapshot (now reflects any arxiv recs we just filed)
    try:
        pending = triage.list_triage(slug, status="pending")
        out["triage"] = {"pending_total": len(pending), "items": pending[:5]}
    except Exception as exc:  # noqa: BLE001
        out["errors"].append({"step": "triage", "msg": str(exc)})
    # 5) under-read in-library papers ('go back to these')
    try:
        out["underread"] = {"papers": _flag_underread(slug)}
    except Exception as exc:  # noqa: BLE001
        out["errors"].append({"step": "under-read", "msg": str(exc)})
    # 6) health check (deterministic wiki lint — flags drift / stale / unsupported pages)
    try:
        out["health"] = {"findings": lint_wiki(slug)}
    except Exception as exc:  # noqa: BLE001
        out["errors"].append({"step": "health check", "msg": str(exc)})
        out["health"] = {"findings": []}
    _touch_last_regen(slug)   # clears refresh_signal until the next change
    return out


# Per-paper char caps for the digest. Papers with PDFs get the bigger excerpt slot;
# abstract-only papers get the smaller abstract slot. _OVERVIEW_TOTAL_BUDGET keeps
# the whole prompt within a single Opus/Sonnet call.
_OVERVIEW_MAX_PAPERS = 40
_OVERVIEW_PDF_CHARS = 2000
_OVERVIEW_ABSTRACT_CHARS = 900
_OVERVIEW_TOTAL_BUDGET = 80000


def _overview_digest(papers: list[dict]) -> tuple[str, set[str], set[str]]:
    """Build the LLM input from the collection's papers. Always includes PDF excerpts
    where cached (the user's accepted 'Always-Deepen-on-import' default, 2026-05-29).
    Returns ``(digest, included_refs, pdf_refs)``. Papers with PDFs are preferred when
    capping; abstract-only papers fill the remaining budget. Marked clearly per-paper
    so the skill knows which cards must leave mechanism/evidence/limitation empty."""
    # Score: PDF-equipped first (so the agent sees richer evidence), then abstract-only.
    ranked = sorted(papers, key=lambda p: (0 if p.get("pdf_excerpt") else 1))[:_OVERVIEW_MAX_PAPERS]
    blocks, used = [], 0
    included, pdf_refs = [], set()
    for p in ranked:
        ab = (p.get("abstract") or "").strip()[:_OVERVIEW_ABSTRACT_CHARS]
        exc = p.get("pdf_excerpt") or ""
        parts = [f"[{p['ref']}] {p['title']}"]
        if ab:
            parts.append(f"Abstract: {ab}")
        if exc:
            parts.append(f"PDF excerpt:\n{exc}")
            parts.append("(HAS_PDF_EXCERPT — mechanism/evidence/limitation are fair game.)")
        else:
            parts.append("(ABSTRACT_ONLY — no PDF was supplied; leave mechanism/evidence/limitation empty for this paper.)")
        block = "\n".join(parts)
        if blocks and used + len(block) > _OVERVIEW_TOTAL_BUDGET:
            break
        blocks.append(block)
        used += len(block)
        included.append(p["ref"])
        if exc:
            pdf_refs.add(p["ref"])
    return "\n\n---\n\n".join(blocks), set(included), pdf_refs


def generate_overview(slug: str, force: bool = False, stage_cb=None) -> bool:
    """Generate the curiosity-driven starter wiki from the papers' abstracts + (where
    cached) PDF excerpts, via the ``starter-wiki`` skill. Always-Deepen at init (user
    decision 2026-05-29) — papers with PDFs contribute the first ~2000 chars of body text
    so the agent can populate mechanism/evidence/limitation fields honestly.

    Direct-write agent seed (CLAUDE.md amendment broadened 2026-05-29), stored as
    ``wiki/overview.json``. Non-destructive unless ``force``. Returns True on success.

    ``stage_cb`` is an optional callable invoked at each pipeline stage with the
    stage name and any kwargs (paper counts, PDF progress). Used by ``start_draft_async``
    to publish progress for the UI polling endpoint. No-op if None."""
    def stage(name, **extra):
        if stage_cb:
            try:
                stage_cb(name, **extra)
            except Exception:  # noqa: BLE001 - never let UI callback errors abort generation
                pass

    if _overview_path(slug).exists() and not force:
        return False
    stage("gathering")
    papers = _collection_abstracts(slug)
    with_abs = [p for p in papers if p["abstract"]]
    # Pull cached PDF excerpts in one pass (best-effort; missing PDFs just mean
    # abstract-only cards). This is the always-deepen step.
    total = len(with_abs)
    for i, p in enumerate(with_abs):
        stage("reading_pdfs", pdfs_done=i, pdfs_total=total)
        p["pdf_excerpt"] = _pdf_excerpt(p["id"], max_chars=_OVERVIEW_PDF_CHARS)
    stage("reading_pdfs", pdfs_done=total, pdfs_total=total)
    digest, included_refs, pdf_refs = _overview_digest(with_abs)
    if not digest.strip():
        return False
    stage("drafting", paper_count=len(included_refs), pdfs_read=len(pdf_refs))
    from . import agent_skills
    system = agent_skills.skill_body("starter-wiki") or "Output a JSON starter wiki from the papers."
    try:
        out = llm.complete([{"role": "system", "content": system},
                            {"role": "user", "content": "Papers:\n\n" + digest}])
        data = _extract_json(out)
    except Exception:  # noqa: BLE001 - no agent / bad output
        return False
    stage("validating", paper_count=len(included_refs), pdfs_read=len(pdf_refs))
    overview = _validate_overview(data or {}, included_refs, pdf_refs)
    # Accept if ANY major section came back populated. field_overview alone isn't enough
    # (the agent may dump only the hook); paper_cards alone IS — that's the spine.
    if not any(overview[k] for k in ("problems", "methods", "open_problems",
                                     "benchmarks", "paper_cards", "reading_paths")):
        return False
    overview["_meta"] = {
        "generated_by": "agent", "generator": "starter-wiki",
        "generated_at": _now(),
        "paper_count": len(included_refs),
        "pdfs_read": len(pdf_refs),
        "pdfs_missing": len(included_refs) - len(pdf_refs),
    }
    _wikidir(slug).mkdir(parents=True, exist_ok=True)
    _overview_path(slug).write_text(json.dumps(overview, indent=2), encoding="utf-8")
    _append_log(slug, f"generated starter wiki ({len(pdf_refs)}/{len(included_refs)} with PDFs)", digest)
    return True


# --- async draft job ('the agent works in the background') ---------------------------
# In-memory only on purpose: a uvicorn restart wipes any in-flight job, which is the
# honest behavior (the background thread is gone too). The UI's polling endpoint will
# return "idle" after a restart and the user can re-Regenerate. Survives the user
# navigating away / closing the wiki tab — when they reopen, the panel checks
# get_draft_job(slug) and re-renders the overlay with the live stage.
import threading

_DRAFT_JOBS: dict[str, dict] = {}
_DRAFT_LOCK = threading.Lock()

# Per-stage human-voice messages. Single-line "current action" only — no subline
# (user 2026-05-30: "just show the current action"). Where a count is useful, it's
# folded into the action itself ("I'm reading the PDFs (3/12).") so the user gets
# one consistent line to read.
_STAGE_MESSAGES = {
    "gathering":    "I'm collecting your papers' abstracts.",
    "reading_pdfs": "I'm reading the PDFs.",   # gets "(done/total)" appended below
    "drafting":     "I'm drafting the wiki.",
    "validating":   "I'm checking the refs and saving.",
    "done":         "Done.",
    "failed":       "Something went wrong. Try Regenerate again.",
}


def _set_job(slug: str, **kwargs) -> None:
    """Merge updates into a slug's job dict. Thread-safe."""
    with _DRAFT_LOCK:
        job = _DRAFT_JOBS.get(slug, {})
        job.update(kwargs)
        _DRAFT_JOBS[slug] = job


def get_draft_job(slug: str) -> dict | None:
    """Snapshot of the running job for this slug, or None if no job is tracked.
    A copy, so callers can read/mutate without holding the lock."""
    with _DRAFT_LOCK:
        job = _DRAFT_JOBS.get(slug)
        return dict(job) if job else None


def clear_draft_job(slug: str) -> None:
    """Forget a slug's job. Used by the panel renderer to clean up after a done/
    failed job has been observed, so the next render is back to the idle path."""
    with _DRAFT_LOCK:
        _DRAFT_JOBS.pop(slug, None)


def _stage_message(job: dict) -> dict:
    """Pure function: compose the single-line action text for the UI from the job
    state. ``subline`` is kept in the return shape (always empty) so the JSON
    contract doesn't change for clients that still read it. Counts are folded into
    the action itself so the user only has one line to read."""
    stage = job.get("stage", "gathering")
    action = _STAGE_MESSAGES.get(stage, _STAGE_MESSAGES["gathering"])
    if stage == "reading_pdfs":
        done, total = job.get("pdfs_done", 0), job.get("pdfs_total", 0)
        if total:
            action = f"I'm reading the PDFs ({done}/{total})."
    return {"action": action, "subline": ""}


# Time-based asymptote inside the drafting stage. The LLM call gives no inner
# progress events, so without this the bar sits at a fixed value (was 60) for the
# 60-90s the call takes, then leaps. With this, progress climbs smoothly inside
# the 30→95 band:
#   p(t) = LOW + (HIGH - LOW) · (1 − e^(−t / TAU))
# Caps at HIGH (=95) until status flips to done — the same honesty rule used
# before: never claim 100% before the response actually arrives.
_DRAFTING_LOW = 30      # progress where reading_pdfs left off
_DRAFTING_HIGH = 95     # asymptote ceiling
_DRAFTING_TAU = 35.0    # seconds; at t≈35s the curve has reached ~63% of the band


def _stage_progress(job: dict) -> int:
    """Coarse 0-100 progress estimate from the job state. Reading-pdfs has a real
    fraction (done/total). Drafting uses a time-based asymptote so the bar keeps
    moving while the LLM runs. Caps at 95 until status='done' lands on 100."""
    import math
    import time as _time
    stage = job.get("stage", "gathering")
    if job.get("status") in ("done", "failed"):
        return 100
    if stage == "gathering":
        return 3
    if stage == "reading_pdfs":
        done, total = job.get("pdfs_done", 0), job.get("pdfs_total", 0) or 1
        return min(_DRAFTING_LOW, 5 + int(25 * done / total))
    if stage == "drafting":
        dts = job.get("drafting_started_at")
        if dts:
            elapsed = max(0.0, _time.time() - dts)
            p = _DRAFTING_LOW + (_DRAFTING_HIGH - _DRAFTING_LOW) * (1 - math.exp(-elapsed / _DRAFTING_TAU))
            return min(_DRAFTING_HIGH, int(p))
        return _DRAFTING_LOW  # drafting just started; pre-asymptote starting point
    if stage == "validating":
        return _DRAFTING_HIGH
    return 0


def start_draft_async(slug: str, force: bool = True) -> bool:
    """Kick off the starter-wiki draft on a daemon thread. Returns True if a job was
    started, False if one was already running for this slug. The thread updates
    _DRAFT_JOBS as the pipeline advances; UI polls get_draft_job for the live state."""
    existing = get_draft_job(slug)
    if existing and existing.get("status") == "running":
        return False
    _set_job(slug, status="running", stage="gathering", started_at=_now(),
             pdfs_done=0, pdfs_total=0, paper_count=None, pdfs_read=None,
             drafting_started_at=None, error=None, finished_at=None)

    def cb(name: str, **extra):
        # Stamp drafting_started_at on first entry to drafting so the time-based
        # progress curve has a reference point (see _stage_progress).
        update = {"stage": name, **extra}
        if name == "drafting":
            import time as _time
            existing = _DRAFT_JOBS.get(slug, {})
            if not existing.get("drafting_started_at"):
                update["drafting_started_at"] = _time.time()
        _set_job(slug, **update)

    def runner():
        try:
            ok = generate_overview(slug, force=force, stage_cb=cb)
            _set_job(slug, status="done" if ok else "failed",
                     stage="done" if ok else "failed",
                     finished_at=_now(),
                     error=None if ok else "the agent produced no usable output")
        except Exception as exc:  # noqa: BLE001 - publish, don't crash the worker
            _set_job(slug, status="failed", stage="failed",
                     finished_at=_now(), error=str(exc))

    threading.Thread(target=runner, daemon=True, name=f"draft-{slug}").start()
    return True


def _touch_last_regen(slug: str) -> None:
    """Bump collections.last_wiki_regen so refresh_signal clears."""
    from .db import connect
    con = connect()
    try:
        con.execute("UPDATE collections SET last_wiki_regen = CURRENT_TIMESTAMP WHERE slug = ?", (slug,))
        con.commit()
    finally:
        con.close()


def refresh_signal(slug: str) -> dict:
    """Cheap deterministic 'should the user refresh the wiki?' signal — no LLM, no scheduler.
    Computed on demand at page load. Returns ``{suggest, reasons, since}``."""
    from datetime import datetime, timedelta, timezone
    from .db import connect
    con = connect()
    try:
        row = con.execute("SELECT last_wiki_regen FROM collections WHERE slug=?", (slug,)).fetchone()
        last = row["last_wiki_regen"] if row else None
        reasons: list[str] = []
        if not last:
            reasons.append("Never refreshed.")
        else:
            try:
                last_dt = datetime.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - last_dt
                if age > timedelta(days=7):
                    reasons.append(f"{age.days} days since last refresh.")
            except ValueError:
                pass
        since = last or "1970-01-01"
        notes_n = con.execute(
            "SELECT COUNT(*) n FROM paper_notes WHERE collection_slug=? AND updated_at > ? "
            "AND (COALESCE(summary,'')<>'' OR COALESCE(thoughts,'')<>'' OR COALESCE(key_quotes,'')<>'')",
            (slug, since)).fetchone()["n"]
        if notes_n:
            reasons.append(f"{notes_n} note(s) changed since last refresh.")
        papers_n = con.execute(
            "SELECT COUNT(*) n FROM collection_papers WHERE collection_slug=? AND added_at > ?",
            (slug, since)).fetchone()["n"]
        if papers_n:
            reasons.append(f"{papers_n} paper(s) added since last refresh.")
        return {"suggest": bool(reasons), "reasons": reasons, "since": str(last) if last else None}
    finally:
        con.close()


def _ref_map(slug: str) -> dict:
    """ref -> {id, title, has_pdf} for every paper in the collection (each paper is reachable
    by its zotero_key / arxiv_id / openreview_id / numeric id)."""
    from . import library
    out: dict = {}
    for p in library.list_papers(slug):
        info = {"id": p["id"], "title": p["title"], "has_pdf": p.get("has_pdf")}
        for ref in (p.get("zotero_key"), p.get("arxiv_id"), p.get("openreview_id"), str(p["id"])):
            if ref:
                out[ref] = info
    return out


# --- attention reweighting (Phase C, 2026-05-29) ---------------------------
# Cheap deterministic re-rank of the starter wiki's paper cards from the user's
# real attention signals: highlights and notes. No LLM. Updates on every render.
# The blueprint's "evolve" half — the wiki responds to where the user actually
# looks, without ever rewriting agent-drafted content.
_ATTENTION_NOTE_WEIGHT = 5    # a non-empty note is worth ~5 highlights of attention
_ATTENTION_HOT_FLOOR = 2      # ignore tiny scores when picking the 🔥 threshold


def attention_scores(slug: str) -> dict[int, int]:
    """Per-paper attention score for the cards. Highlights weighted 1, non-empty notes
    (any of summary / thoughts / key_quotes) weighted ``_ATTENTION_NOTE_WEIGHT``. Returns
    ``{paper_id: score}`` for papers with any signal — papers with no signal are absent."""
    scores: dict[int, int] = defaultdict(int)
    con = connect()
    try:
        for r in con.execute(
            "SELECT paper_id, COUNT(*) n FROM annotations "
            "WHERE collection_slug=? AND kind='highlight' GROUP BY paper_id",
            (slug,),
        ):
            scores[r["paper_id"]] += r["n"]
        for r in con.execute(
            "SELECT paper_id FROM paper_notes WHERE collection_slug=? AND ("
            "  COALESCE(summary,'')<>'' OR COALESCE(thoughts,'')<>'' OR COALESCE(key_quotes,'')<>'')",
            (slug,),
        ):
            scores[r["paper_id"]] += _ATTENTION_NOTE_WEIGHT
    finally:
        con.close()
    return dict(scores)


def attention_changed_since(slug: str, since: str | None) -> set[int]:
    """Paper ids whose highlights/notes were created or updated after ``since``. Used to
    flag cards "new since last view" on the wiki page. ``since`` None → empty (no recency
    baseline → never claim anything is new, never lie about freshness)."""
    if not since:
        return set()
    out: set[int] = set()
    con = connect()
    try:
        for r in con.execute(
            "SELECT DISTINCT paper_id FROM annotations "
            "WHERE collection_slug=? AND kind='highlight' AND (created_at>? OR updated_at>?)",
            (slug, since, since),
        ):
            out.add(r["paper_id"])
        for r in con.execute(
            "SELECT paper_id FROM paper_notes WHERE collection_slug=? AND updated_at>? AND ("
            "  COALESCE(summary,'')<>'' OR COALESCE(thoughts,'')<>'' OR COALESCE(key_quotes,'')<>'')",
            (slug, since),
        ):
            out.add(r["paper_id"])
    finally:
        con.close()
    return out


def read_and_bump_viewed(slug: str) -> str | None:
    """Read the OLD ``collections.last_wiki_viewed_at`` and bump it to now atomically.
    Returns the old value so the caller can pass it to ``load_overview(attention_since=...)``
    for "new since last view" badges. The bump happens here (not in ``load_overview``) so a
    cheap re-render (e.g. after ↻ Regenerate POST) doesn't silently reset the badge state —
    only an actual page-view GET should bump."""
    con = connect()
    try:
        row = con.execute(
            "SELECT last_wiki_viewed_at FROM collections WHERE slug=?", (slug,)
        ).fetchone()
        old = row["last_wiki_viewed_at"] if row and "last_wiki_viewed_at" in row.keys() else None
        con.execute(
            "UPDATE collections SET last_wiki_viewed_at = CURRENT_TIMESTAMP WHERE slug = ?",
            (slug,),
        )
        con.commit()
        return old
    finally:
        con.close()


def load_overview(slug: str, attention_since: str | None = None) -> dict | None:
    """Read overview.json and resolve every ref to a paper object for the interactive render.
    Unresolvable refs (paper since removed) are dropped. None if no overview exists.

    Tolerates old-shape overviews (top-level ``intro``, ``gaps``, singular ``reading_path``)
    so a regenerate isn't required after the 2026-05-29 schema bump — they migrate on read.

    Phase C reweighting: paper cards are annotated with attention_score, is_hot (top-tier
    score floor), and is_new (highlights/notes after ``attention_since``). Cards are
    stable-sorted by ``-attention_score`` so attended papers float to the top; with all
    zeros the editorial order is preserved."""
    path = _overview_path(slug)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    rmap = _ref_map(slug)
    resolve = lambda refs: [rmap[r] for r in (refs or []) if r in rmap]

    # field_overview ----------------------------------------------------------------
    fo_raw = data.get("field_overview") if isinstance(data.get("field_overview"), dict) else {}
    if not fo_raw.get("one_paragraph") and data.get("intro"):
        fo_raw = dict(fo_raw, one_paragraph=data["intro"])
    field_overview = {k: fo_raw.get(k, "") for k in _FIELD_OVERVIEW_FIELDS}

    # problems / methods / benchmarks ----------------------------------------------
    problems = []
    for p in data.get("problems", []):
        rf = rmap.get(p.get("read_first")) if p.get("read_first") else None
        problems.append({
            "title": p.get("title", ""), "why": p.get("why", ""), "tension": p.get("tension", ""),
            "approaches": [{"label": a["label"], "papers": resolve(a["papers"])}
                           for a in p.get("approaches", []) if resolve(a["papers"])],
            "read_first": rf, "read_first_why": p.get("read_first_why", ""),
            "papers": resolve(p.get("papers")),
        })
    def _items(key: str) -> list[dict]:
        return [{"title": x.get("title", ""), "key_idea": x.get("key_idea", ""),
                 "body": x.get("body", ""), "papers": resolve(x.get("papers"))}
                for x in data.get(key, []) if x.get("title")]

    # Open problems (migrate from `gaps` on old overviews) -------------------------
    raw_op = data.get("open_problems") if data.get("open_problems") is not None else data.get("gaps", [])
    open_problems = [{"title": x.get("title", ""), "body": x.get("body", ""),
                      "papers": resolve(x.get("papers"))}
                     for x in (raw_op or []) if x.get("title")]

    # Paper cards -------------------------------------------------------------------
    # Phase C: pull attention signals once (cheap SQL aggregates, no LLM) and decorate
    # each card. is_hot picks the median nonzero score (floored at _ATTENTION_HOT_FLOOR)
    # so the 🔥 badge surfaces "actively attended" cards without spamming low-noise ones.
    scores = attention_scores(slug)
    nonzero = sorted(v for v in scores.values() if v > 0)
    hot_threshold = max(_ATTENTION_HOT_FLOOR, nonzero[len(nonzero) // 2]) if nonzero else None
    changed = attention_changed_since(slug, attention_since)
    paper_cards = []
    for pc in data.get("paper_cards", []):
        paper = rmap.get(pc.get("paper"))
        if not paper:
            continue
        connected = []
        for c in pc.get("connected_papers", []):
            cp = rmap.get(c.get("paper"))
            if cp:
                connected.append({"paper": cp, "relation": c.get("relation", ""),
                                  "why": c.get("why", "")})
        pid = paper["id"]
        score = scores.get(pid, 0)
        paper_cards.append({
            "paper": paper,
            "status": pc.get("status", ""),
            "problem": pc.get("problem", ""),
            "idea": pc.get("idea", ""),
            "method_family": pc.get("method_family", ""),
            "contribution": pc.get("contribution", ""),
            "why_read": pc.get("why_read", ""),
            "difficulty": pc.get("difficulty", ""),
            "prerequisites": resolve(pc.get("prerequisites")),
            "connected_papers": connected,
            "mechanism": pc.get("mechanism", ""),
            "evidence": pc.get("evidence", ""),
            "limitation": pc.get("limitation", ""),
            "abstract_only": bool(pc.get("abstract_only")),
            "attention_score": score,
            "is_hot": hot_threshold is not None and score >= hot_threshold,
            "is_new": pid in changed,
        })
    # Stable re-rank: high-score cards float; with zeros everywhere, editorial order holds.
    paper_cards.sort(key=lambda c: -c["attention_score"])

    # Reading paths (migrate singular reading_path → one "Orientation" path) -------
    reading_paths = []
    for rp in (data.get("reading_paths") or []):
        ordered = []
        for step in rp.get("ordered_papers", []):
            sp = rmap.get(step.get("paper"))
            if sp:
                ordered.append({"paper": sp, "why_now": step.get("why_now", ""),
                                "focus_on": step.get("focus_on", ""), "skip": step.get("skip", "")})
        if ordered:
            reading_paths.append({"name": rp.get("name", ""), "for_who": rp.get("for_who", ""),
                                  "goal": rp.get("goal", ""), "ordered_papers": ordered})
    if not reading_paths and data.get("reading_path"):
        legacy = resolve(data["reading_path"])[:6]
        if legacy:
            reading_paths.append({
                "name": "Orientation",
                "for_who": "anyone new to this collection",
                "goal": "Get the lay of the land",
                "ordered_papers": [{"paper": p, "why_now": "", "focus_on": "", "skip": ""}
                                   for p in legacy],
            })

    return {
        "field_overview": field_overview,
        "problems": problems,
        "methods": _items("methods"),
        "open_problems": open_problems,
        "benchmarks": _items("benchmarks"),
        "paper_cards": paper_cards,
        "reading_paths": reading_paths,
        "meta": data.get("_meta", {}),
    }
