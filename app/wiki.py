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
    """LEGACY (pre-2026-05-30): the JSON starter wiki's location. Kept for
    migration detection only — wiki/sections/ is the new home (cognitive-model
    layout, Phase A). load_overview() returns the migration-banner shape when
    this file or the intermediate wiki/starter/ tree still exists and the new
    sections directory doesn't."""
    return _wikidir(slug) / "overview.json"


# --- cognitive-model wiki: Phase A (2026-05-31) -------------------------------
# The wiki is now a single page composed of stage-gated sections. Phase A ships
# Stage 0 only:
#   wiki/sections/thesis.md       — Collection Thesis (1 paragraph + 3 callouts)
#   wiki/sections/landscape.md    — Research Landscape (problems/methods/debates/open)
# Papers (Stage 0) are pulled live from the DB and decorated with attention at
# render time — no agent-written file needed for them.
#
# Later phases add more files in the same directory:
#   focus.md          — Stage 2 (concept-based attention)
#   understanding.md  — Stage 3 (accepted beliefs)
#   recommended.md    — Stage 2/3 (reading order)
#   beliefs/*.md      — belief tray + accepted beliefs
#   concepts.json     — extracted concept space
#   intent.md         — stored grilling answers
#
# Legacy wiki/starter/ (the prior llm_wiki layout) and wiki/<phase-5-section>/*
# (the notes-based wiki) are deprecated. They stay on disk for safety; nothing
# reads them. A regenerate of the Field Model writes only wiki/sections/.

_LANDSCAPE_MAX_ITEMS = 6           # cap per column to force real clustering
_THESIS_CALLOUTS = ("core_tension", "key_intuition", "central_question")
# Phase B (2026-05-31): concept space + editorial recommended reading
_CONCEPTS_MAX = 12               # how many concepts the field-model skill may keep
_CONCEPTS_MIN_NAME_LEN = 3        # drop garbage names ("ab"); same rule as landscape items
_CONCEPT_SYNONYMS_MAX = 6         # cap synonyms per concept (keeps the regex set sane)
_RECOMMENDED_MAX = 5              # cap on recommended_reading; default rendered = 3
# Threshold on a single concept's attention score before the Focus section
# renders. Below this we don't even hint at user-mind inference (the user's
# "premature inference is anchoring" concern from the blueprint).
_FOCUS_CONCEPT_FLOOR = 3
# Position labels for the recommended-reading list (assigned by index at render
# time, not by the LLM — keeps the vocabulary consistent across collections).
_REC_POSITIONS = ("Start here", "Next", "Then", "Then", "Then")

# Slugify helper (page slugs, concept tags). The pattern stays the same; future
# phases will reuse it.
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Wikilink syntax: [[Page Name]]. Used by future phases (belief cross-refs).
_WIKILINK_PAGE_RE = re.compile(r"\[\[([^\]\|]+)(?:\|[^\]]+)?\]\]")


def _sections_dir(slug: str) -> Path:
    return _wikidir(slug) / "sections"


def _thesis_path(slug: str) -> Path:
    return _sections_dir(slug) / "thesis.md"


def _landscape_path(slug: str) -> Path:
    return _sections_dir(slug) / "landscape.md"


def _concepts_path(slug: str) -> Path:
    return _sections_dir(slug) / "concepts.json"


def _recommended_path(slug: str) -> Path:
    return _sections_dir(slug) / "recommended.json"


def _has_field_model(slug: str) -> bool:
    """True iff the new wiki/sections/ tree exists with at least the two Stage 0
    pages. Drives the regen-button vs migration-banner branching."""
    return _thesis_path(slug).is_file() and _landscape_path(slug).is_file()


# Legacy detector kept here for the migration banner. Removed once we've burned
# off all old collections (or earlier — the banner is cheap).
def _has_starter_wiki(slug: str) -> bool:
    return (_wikidir(slug) / "starter" / "index.md").is_file()


# Section-parser regex: splits a markdown body by `## <Heading>` lines, capturing
# the heading and content to next `##`. Used to round-trip the Thesis page's
# callout sections (Core tension / Key intuition / Central question) back into
# fielded data at load time.
_SECTION_RE = re.compile(r"(?m)^##\s+(?P<title>[^\n]+?)\s*\n(?P<body>.*?)(?=^##\s+|\Z)", re.DOTALL)


def _validate_field_model(data: dict, valid_refs: set | None = None) -> dict:
    """Validate the field-model LLM output and clamp it.

    Thesis: one paragraph + three single-sentence callouts. Empty fields stay
    empty (we never invent content to fill them).

    Landscape: four lists of short items capped at _LANDSCAPE_MAX_ITEMS so the
    agent can't dump a 17-method bibliography. Items shorter than 3 chars or
    duplicates are dropped.

    Concepts (Phase B): a list of named research concepts the agent extracted
    as worth tracking. Each carries 1-_CONCEPT_SYNONYMS_MAX synonyms used by
    the deterministic attention scorer. Concept count capped at _CONCEPTS_MAX;
    short/empty names dropped; duplicate slugs deduped.

    Recommended reading (Phase B): 1-_RECOMMENDED_MAX papers the agent
    suggests as a starting reading path. Refs not in ``valid_refs`` are
    dropped; ``why_now`` text trimmed; positional labels assigned at render
    time (not by the LLM) for consistency."""
    def text(s):
        return (s or "").strip() if isinstance(s, str) else ""
    valid_refs = valid_refs or set()

    th = data.get("thesis") if isinstance(data.get("thesis"), dict) else {}
    thesis = {"one_paragraph": text(th.get("one_paragraph"))}
    for k in _THESIS_CALLOUTS:
        thesis[k] = text(th.get(k))

    ls = data.get("landscape") if isinstance(data.get("landscape"), dict) else {}
    def items(key: str) -> list[str]:
        raw = ls.get(key) or []
        if not isinstance(raw, list):
            return []
        out, seen = [], set()
        for x in raw:
            s = text(x)
            if len(s) < 3 or s.lower() in seen:
                continue
            seen.add(s.lower())
            out.append(s)
            if len(out) >= _LANDSCAPE_MAX_ITEMS:
                break
        return out

    landscape = {
        "problems":       items("problems"),
        "methods":        items("methods"),
        "debates":        items("debates"),
        "open_questions": items("open_questions"),
    }

    # --- concepts (Phase B) -------------------------------------------------
    concepts: list[dict] = []
    seen_slugs: set[str] = set()
    for c in (data.get("concepts") or []):
        if not isinstance(c, dict):
            continue
        name = text(c.get("name"))
        if len(name) < _CONCEPTS_MIN_NAME_LEN:
            continue
        slug = _SLUG_RE.sub("-", name.lower()).strip("-")
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        raw_syns = c.get("synonyms") if isinstance(c.get("synonyms"), list) else []
        synonyms = []
        # Always include the canonical name as a synonym candidate so the
        # scorer matches text that uses the name verbatim.
        for syn_raw in [name, *raw_syns]:
            s = text(syn_raw).lower()
            if len(s) >= _CONCEPTS_MIN_NAME_LEN and s not in synonyms:
                synonyms.append(s)
            if len(synonyms) >= _CONCEPT_SYNONYMS_MAX:
                break
        concepts.append({
            "name": name, "slug": slug, "synonyms": synonyms,
            "blurb": text(c.get("blurb")),
        })
        if len(concepts) >= _CONCEPTS_MAX:
            break

    # --- recommended reading (Phase B) --------------------------------------
    recommended: list[dict] = []
    seen_recs: set[str] = set()
    for r in (data.get("recommended_reading") or []):
        if not isinstance(r, dict):
            continue
        ref = r.get("paper")
        if ref not in valid_refs or ref in seen_recs:
            continue
        seen_recs.add(ref)
        recommended.append({"paper": ref, "why_now": text(r.get("why_now"))})
        if len(recommended) >= _RECOMMENDED_MAX:
            break

    return {"thesis": thesis, "landscape": landscape,
            "concepts": concepts, "recommended": recommended}


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


def _wipe_sections_tree(slug: str) -> None:
    """Remove the previous Field Model tree before a regenerate. Only touches
    wiki/sections/ — legacy wiki/starter/ and wiki/<section>/* are not touched
    (they're deprecated; nothing reads them)."""
    import shutil
    sdir = _sections_dir(slug)
    if sdir.exists():
        shutil.rmtree(sdir)


def _write_thesis_page(slug: str, thesis: dict, meta_extra: dict) -> None:
    """Compose and write wiki/sections/thesis.md. Body = opening paragraph
    followed by H2 callouts for each non-empty callout field. Frontmatter
    carries provenance (generated_by/generated_at/paper_count/pdfs_read)."""
    body_parts = []
    if thesis["one_paragraph"]:
        body_parts.append(thesis["one_paragraph"])
    callout_titles = {"core_tension": "Core tension",
                      "key_intuition": "Key intuition",
                      "central_question": "Central question"}
    for key, title in callout_titles.items():
        if thesis.get(key):
            body_parts.append(f"\n## {title}\n{thesis[key]}")
    meta = {"type": "thesis", "title": f"Collection Thesis: {slug}",
            "generated_by": "agent", "generator": "field-model",
            "generated_at": _now(), **meta_extra}
    _thesis_path(slug).write_text(frontmatter.dump(meta, "\n".join(body_parts)),
                                   encoding="utf-8")


def _write_landscape_page(slug: str, landscape: dict, meta_extra: dict) -> None:
    """Compose and write wiki/sections/landscape.md. Body = four H2 sections
    (Problems / Methods / Debates / Open Questions), each a markdown bullet list.
    Sections with no items are omitted entirely (no empty headings rendered)."""
    body_parts = []
    titles = (("problems", "Problems"), ("methods", "Methods"),
              ("debates", "Debates"), ("open_questions", "Open Questions"))
    for key, title in titles:
        items = landscape.get(key) or []
        if not items:
            continue
        body_parts.append(f"## {title}")
        for it in items:
            body_parts.append(f"- {it}")
        body_parts.append("")
    meta = {"type": "landscape", "title": f"Research Landscape: {slug}",
            "generated_by": "agent", "generator": "field-model",
            "generated_at": _now(), **meta_extra}
    _landscape_path(slug).write_text(frontmatter.dump(meta, "\n".join(body_parts)),
                                      encoding="utf-8")


def _write_concepts_file(slug: str, concepts: list[dict]) -> None:
    """Write wiki/sections/concepts.json — the concept space the deterministic
    attention scorer uses. JSON, not markdown: structured data, no prose."""
    payload = {
        "concepts": concepts,
        "_meta": {"generated_by": "agent", "generator": "field-model",
                  "generated_at": _now()},
    }
    _concepts_path(slug).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_recommended_file(slug: str, recommended: list[dict]) -> None:
    """Write wiki/sections/recommended.json — the agent's editorial reading
    path (3-5 papers in order). JSON for the same reason as concepts: it's
    structured data the renderer composes into a Section 4 card.

    Key is `picks` (not `items`) because Jinja resolves `obj.items` as
    `dict.items()`, not the key — a footgun we've hit twice already."""
    payload = {
        "picks": recommended,
        "_meta": {"generated_by": "agent", "generator": "field-model",
                  "generated_at": _now()},
    }
    _recommended_path(slug).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def generate_overview(slug: str, force: bool = False, stage_cb=None) -> bool:
    """Generate the Field Model (Stage 0 of the cognitive-model wiki, 2026-05-31).

    One-shot pipeline:
      1. gathering    — pull collection abstracts.
      2. reading_pdfs — extract a first-pages excerpt per cached PDF (real fraction).
      3. drafting     — one LLM call (`field-model` skill) produces JSON with
                        a Thesis (one paragraph + 3 callouts) and a Landscape
                        (Problems / Methods / Debates / Open Questions, each 3-6
                        items — the validator caps so 'lazy listing' can't happen).
      4. writing      — write wiki/sections/thesis.md and landscape.md.

    Direct-write, agent-tagged. Non-destructive of the notes-based wiki (legacy
    sections under wiki/<phase-5-section>/* are not touched). Public name kept
    for route stability; the implementation is the cognitive-model wiki now.

    ``stage_cb`` is the progress callback used by start_draft_async to publish
    state into the polling endpoint. No-op if None."""
    def stage(name, **extra):
        if stage_cb:
            try:
                stage_cb(name, **extra)
            except Exception:  # noqa: BLE001
                pass

    if _has_field_model(slug) and not force:
        return False

    # --- Gather + read PDFs (real progress) -----------------------------------
    stage("gathering")
    papers = _collection_abstracts(slug)
    with_abs = [p for p in papers if p["abstract"]]
    total = len(with_abs)
    for i, p in enumerate(with_abs):
        stage("reading_pdfs", pdfs_done=i, pdfs_total=total)
        p["pdf_excerpt"] = _pdf_excerpt(p["id"], max_chars=_OVERVIEW_PDF_CHARS)
    stage("reading_pdfs", pdfs_done=total, pdfs_total=total)
    digest, included_refs, pdf_refs = _overview_digest(with_abs)
    if not digest.strip():
        return False

    # --- One LLM call for the whole Field Model ------------------------------
    stage("drafting", paper_count=len(included_refs), pdfs_read=len(pdf_refs))
    from . import agent_skills
    system = (agent_skills.skill_body("field-model")
              or "Output JSON: {thesis:{one_paragraph,core_tension,key_intuition,central_question}, landscape:{problems[],methods[],debates[],open_questions[]}}.")
    try:
        out = llm.complete([{"role": "system", "content": system},
                            {"role": "user", "content": "Papers:\n\n" + digest}])
        data = _extract_json(out)
    except Exception:  # noqa: BLE001
        return False
    field = _validate_field_model(data or {}, valid_refs=included_refs)
    # Refuse drafts where neither the thesis nor any landscape column came back
    # populated — better to fail visibly than write empty section files.
    if (not field["thesis"]["one_paragraph"]
            and not any(field["landscape"].values())):
        return False

    # --- Write the section files atomically ----------------------------------
    # Four files in Phase B: thesis.md, landscape.md, concepts.json,
    # recommended.json. pages_total reflects all four for the progress UI.
    stage("writing", pages_done=0, pages_total=4)
    _wipe_sections_tree(slug)
    _sections_dir(slug).mkdir(parents=True, exist_ok=True)
    meta_extra = {"paper_count": len(included_refs),
                  "pdfs_read": len(pdf_refs),
                  "pdfs_missing": len(included_refs) - len(pdf_refs)}
    _write_thesis_page(slug, field["thesis"], meta_extra)
    stage("writing", pages_done=1, pages_total=4)
    _write_landscape_page(slug, field["landscape"], meta_extra)
    stage("writing", pages_done=2, pages_total=4)
    _write_concepts_file(slug, field["concepts"])
    stage("writing", pages_done=3, pages_total=4)
    _write_recommended_file(slug, field["recommended"])
    stage("writing", pages_done=4, pages_total=4)
    _append_log(slug, f"generated field model "
                       f"({len(pdf_refs)}/{len(included_refs)} PDFs, "
                       f"{len(field['concepts'])} concepts, "
                       f"{len(field['recommended'])} recommended)", digest)
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
    "analyzing":    "I'm picking the top papers to start with.",  # the analyze step
    "writing":      "I'm writing the page.",   # gets "(N of M)" appended below
    "linking":      "I'm wiring up cross-references.",
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
    elif stage == "writing":
        done, total = job.get("pages_done", 0), job.get("pages_total", 0)
        if total:
            action = f"I'm writing the page ({done}/{total})."
    return {"action": action, "subline": ""}


# Stage progress milestones. With the llm_wiki pipeline we have multiple real
# events to peg progress to (reading_pdfs fraction, analyzing milestone, writing
# fraction of pages_done/pages_total), so a time-based asymptote isn't needed —
# progress climbs in real steps with stage events. Caps at <100 until status=done
# lands; never claim 100% before the response actually arrives (the honesty rule).
_PCT_GATHERING   = 3
_PCT_PDFS_FLOOR  = 5
_PCT_PDFS_CEIL   = 25   # reading_pdfs maxes here; analyze begins above
_PCT_ANALYZING   = 30
_PCT_WRITING_LO  = 35
_PCT_WRITING_HI  = 90   # writing maxes here as pages_done -> pages_total
_PCT_LINKING     = 95


def _stage_progress(job: dict) -> int:
    """Coarse 0-100 progress estimate from the job state. Each stage has real
    events (PDF count, page count) so progress moves in discrete observed steps,
    not time-based guesses. Caps at <100 until status='done' lands on 100."""
    stage = job.get("stage", "gathering")
    if job.get("status") in ("done", "failed"):
        return 100
    if stage == "gathering":
        return _PCT_GATHERING
    if stage == "reading_pdfs":
        done, total = job.get("pdfs_done", 0), job.get("pdfs_total", 0) or 1
        return min(_PCT_PDFS_CEIL, _PCT_PDFS_FLOOR + int((_PCT_PDFS_CEIL - _PCT_PDFS_FLOOR) * done / total))
    if stage == "analyzing":
        return _PCT_ANALYZING
    if stage == "writing":
        done, total = job.get("pages_done", 0), job.get("pages_total", 0) or 1
        # Linearly scale within the writing band as pages complete.
        return min(_PCT_WRITING_HI, _PCT_WRITING_LO + int((_PCT_WRITING_HI - _PCT_WRITING_LO) * done / total))
    if stage == "linking":
        return _PCT_LINKING
    return 0


def start_draft_async(slug: str, force: bool = True) -> bool:
    """Kick off the starter-wiki draft on a daemon thread. Returns True if a job was
    started, False if one was already running for this slug. The thread updates
    _DRAFT_JOBS as the pipeline advances; UI polls get_draft_job for the live state."""
    existing = get_draft_job(slug)
    if existing and existing.get("status") == "running":
        return False
    _set_job(slug, status="running", stage="gathering", started_at=_now(),
             pdfs_done=0, pdfs_total=0, pages_done=0, pages_total=0,
             paper_count=None, pdfs_read=None, error=None, finished_at=None)

    def cb(name: str, **extra):
        _set_job(slug, stage=name, **extra)

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


def attention_per_concept(slug: str, concepts: list[dict]) -> dict[str, int]:
    """Per-concept attention score (Phase B). Deterministic regex over highlight
    selected_text and per-paper note bodies — no LLM. A concept gets +1 per
    matching highlight and +_ATTENTION_NOTE_WEIGHT per matching note. Synonym
    matching is case-insensitive substring against the concept's synonyms list
    (which always includes the canonical name; see _validate_field_model).

    Returns ``{concept_slug: int}`` with entries only for concepts that scored
    at least 1 (no fake zeros)."""
    if not concepts:
        return {}
    scores: dict[str, int] = defaultdict(int)
    # Pre-compile a single regex per concept that ORs its synonyms with word
    # boundaries so 'gap' doesn't match 'agape'. Synonyms are lowercase already.
    patterns: dict[str, re.Pattern] = {}
    for c in concepts:
        syns = [re.escape(s) for s in (c.get("synonyms") or []) if s]
        if not syns:
            continue
        patterns[c["slug"]] = re.compile(r"\b(?:" + "|".join(syns) + r")\b",
                                          flags=re.IGNORECASE)
    if not patterns:
        return {}
    con = connect()
    try:
        # Highlights: count one hit per highlight (a long quote that mentions
        # the concept multiple times still counts as one signal — same shape
        # as Phase C's per-paper attention).
        for r in con.execute(
            "SELECT selected_text FROM annotations "
            "WHERE collection_slug=? AND kind='highlight' "
            "AND COALESCE(selected_text,'')<>''", (slug,)
        ):
            text = r["selected_text"] or ""
            for slug_, pat in patterns.items():
                if pat.search(text):
                    scores[slug_] += 1
        # Notes: a non-empty note that mentions the concept counts more (×5,
        # same weighting as per-paper note attention). The match is over the
        # union of summary/thoughts/key_quotes (anywhere in the user's writing).
        for r in con.execute(
            "SELECT COALESCE(summary,'') || ' ' || COALESCE(thoughts,'') || "
            "' ' || COALESCE(key_quotes,'') AS body "
            "FROM paper_notes WHERE collection_slug=? AND ("
            "  COALESCE(summary,'')<>'' OR COALESCE(thoughts,'')<>'' "
            "  OR COALESCE(key_quotes,'')<>''"
            ")", (slug,)
        ):
            body = r["body"] or ""
            for slug_, pat in patterns.items():
                if pat.search(body):
                    scores[slug_] += _ATTENTION_NOTE_WEIGHT
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


def _parse_thesis_body(body: str) -> dict:
    """Reverse of _write_thesis_page. The opening paragraph is everything before
    the first H2 header; each H2 callout becomes a fielded value."""
    out = {"one_paragraph": body.split("##", 1)[0].strip()}
    for k in _THESIS_CALLOUTS:
        out[k] = ""
    title_to_key = {"Core tension": "core_tension",
                    "Key intuition": "key_intuition",
                    "Central question": "central_question"}
    for m in _SECTION_RE.finditer(body):
        key = title_to_key.get(m.group("title").strip())
        if key:
            out[key] = m.group("body").strip()
    return out


def _parse_landscape_body(body: str) -> dict:
    """Reverse of _write_landscape_page. Each H2 section becomes a list of
    bullet items. Empty/unrecognized sections produce empty lists."""
    titles = {"Problems": "problems", "Methods": "methods",
              "Debates": "debates", "Open Questions": "open_questions"}
    out = {v: [] for v in titles.values()}
    for m in _SECTION_RE.finditer(body):
        key = titles.get(m.group("title").strip())
        if not key:
            continue
        for line in m.group("body").splitlines():
            s = line.strip()
            if s.startswith("- "):
                out[key].append(s[2:].strip())
    return out


def load_overview(slug: str, attention_since: str | None = None) -> dict | None:
    """Read the Field Model from wiki/sections/ and return the panel shape:

      {needs_migration, thesis, landscape, papers, meta}

    where thesis is the fielded callouts dict, landscape is the four-column
    bullet lists, and papers is the live DB-derived collection list (decorated
    with attention_score / is_hot / is_new). Returns None when no wiki exists
    yet (the template shows the empty-state Draft button); returns
    {needs_migration: True} when a legacy wiki/starter/ or wiki/overview.json
    is on disk but the new wiki/sections/ tree isn't."""
    if not _has_field_model(slug):
        if _has_starter_wiki(slug) or _overview_path(slug).exists():
            return {"needs_migration": True, "thesis": {}, "landscape": {},
                    "papers": [], "meta": {}}
        return None

    # --- Read the two section files ------------------------------------------
    try:
        thesis_meta, thesis_body = frontmatter.parse(
            _thesis_path(slug).read_text(encoding="utf-8"))
        landscape_meta, landscape_body = frontmatter.parse(
            _landscape_path(slug).read_text(encoding="utf-8"))
    except OSError:
        return None
    thesis = _parse_thesis_body(thesis_body)
    landscape = _parse_landscape_body(landscape_body)

    # --- Papers (live from DB, decorated with attention) ---------------------
    from . import library
    raw_papers = library.list_papers(slug)
    scores = attention_scores(slug)
    nonzero = sorted(v for v in scores.values() if v > 0)
    hot_threshold = max(_ATTENTION_HOT_FLOOR, nonzero[len(nonzero) // 2]) if nonzero else None
    changed = attention_changed_since(slug, attention_since)
    papers: list[dict] = []
    for p in raw_papers:
        pid = p["id"]
        score = scores.get(pid, 0)
        papers.append({
            "id": pid, "title": p.get("title", ""),
            "authors": p.get("authors", ""), "year": p.get("year"),
            "has_pdf": p.get("has_pdf", False),
            "arxiv_id": p.get("arxiv_id"),
            "zotero_key": p.get("zotero_key"),
            "attention_score": score,
            "is_hot": hot_threshold is not None and score >= hot_threshold,
            "is_new": pid in changed,
        })
    # Stable sort: attended papers float to the top of the evidence row; zeros
    # preserve DB order (which is title-sorted from library.list_papers).
    papers.sort(key=lambda p: -p["attention_score"])

    meta_out = {
        "generated_at": thesis_meta.get("generated_at"),
        "paper_count": thesis_meta.get("paper_count"),
        "pdfs_read": thesis_meta.get("pdfs_read"),
        "pdfs_missing": thesis_meta.get("pdfs_missing"),
        "generated_by": thesis_meta.get("generated_by", "agent"),
    }

    # --- Phase B: concepts → Focus + Recommended Reading -------------------
    # Concepts are written by generate_overview; we read them and compute
    # attention live. The Focus section is threshold-gated — it doesn't render
    # at all until at least one concept crosses _FOCUS_CONCEPT_FLOOR.
    focus = None
    try:
        if _concepts_path(slug).is_file():
            concepts_payload = json.loads(_concepts_path(slug).read_text(encoding="utf-8"))
            concepts = concepts_payload.get("concepts") or []
            concept_scores = attention_per_concept(slug, concepts)
            if any(s >= _FOCUS_CONCEPT_FLOOR for s in concept_scores.values()):
                top = sorted(
                    [{"name": c["name"], "slug": c["slug"], "blurb": c.get("blurb", ""),
                      "score": concept_scores.get(c["slug"], 0)} for c in concepts
                     if concept_scores.get(c["slug"], 0) >= _FOCUS_CONCEPT_FLOOR],
                    key=lambda x: -x["score"])[:5]
                # Honest signal counts (for the "Based on X highlights · Y notes"
                # subtext under the focus chips). Pulled live; not stored.
                # Use the module-level `connect` symbol so tests' monkeypatch
                # of wiki.connect takes effect.
                _con = connect()
                try:
                    nh = _con.execute(
                        "SELECT COUNT(*) FROM annotations "
                        "WHERE collection_slug=? AND kind='highlight'", (slug,)
                    ).fetchone()[0]
                    nn = _con.execute(
                        "SELECT COUNT(*) FROM paper_notes WHERE collection_slug=? AND ("
                        "  COALESCE(summary,'')<>'' OR COALESCE(thoughts,'')<>'' "
                        "  OR COALESCE(key_quotes,'')<>''"
                        ")", (slug,)
                    ).fetchone()[0]
                finally:
                    _con.close()
                focus = {"concepts": top, "highlights": nh, "notes": nn}
    except (ValueError, OSError):
        focus = None

    # Recommended reading: always render when the file has items. Resolve each
    # ref to a paper object so the template can link straight to /c/<slug>/p/<id>,
    # and decorate with hot/new chips driven by per-paper attention.
    recommended = None
    try:
        if _recommended_path(slug).is_file():
            rec_payload = json.loads(_recommended_path(slug).read_text(encoding="utf-8"))
            raw_picks = rec_payload.get("picks") or []
            paper_by_id = {p["id"]: p for p in papers}
            # Resolve refs via _ref_map(slug) so any of {zotero_key,arxiv_id,
            # openreview_id, str(id)} works.
            rmap = _ref_map(slug)
            picks_out = []
            for i, r in enumerate(raw_picks):
                ref = r.get("paper")
                pinfo = rmap.get(ref) if ref else None
                if not pinfo:
                    continue
                pid = pinfo["id"]
                paper_full = paper_by_id.get(pid)
                if not paper_full:
                    continue
                picks_out.append({
                    "paper": paper_full,
                    "why_now": (r.get("why_now") or "").strip(),
                    "position_label": _REC_POSITIONS[min(i, len(_REC_POSITIONS) - 1)],
                })
            if picks_out:
                recommended = {"picks": picks_out}
    except (ValueError, OSError):
        recommended = None

    return {"needs_migration": False, "thesis": thesis, "landscape": landscape,
            "papers": papers, "focus": focus, "recommended": recommended,
            "meta": meta_out}
