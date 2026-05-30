"""Cognitive-model wiki (CLAUDE.md amendment, 2026-05-31).

The wiki is a single page composed of stage-gated sections under
``wiki/sections/``, following Field Model → Belief Model → Research Model:

  * Stage 0 — Field Model: ``thesis.md`` (one paragraph + 3 callouts) and
    ``landscape.md`` (Problems / Methods / Debates / Open Questions). One LLM
    call via the ``field-model`` skill (``generate_overview``).
  * Stage 2 — concepts (``concepts.json``) drive a deterministic, no-LLM
    attention scorer; the "Your Current Focus" section is threshold-gated.
    ``recommended.json`` carries the editorial reading path.
  * Stage 3 — beliefs: agent-drafted candidates land in
    ``beliefs/_candidates/`` (``suggest_beliefs``); the user promotes them with
    ``accept_belief`` into ``beliefs/`` (Section 3, "Your Current Understanding").

All artifacts are agent-tagged (``generated_by: agent``) and regenerable. The
older notes-based wiki pipeline (gate / proposed-edits review queue / organizer)
and reading-debt were removed on 2026-05-31; triage + discover (paper inbox,
gap-fill, stale, add-by-URL) remain as separate paper-management features.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import logging

from . import frontmatter, llm
from .config import COLLECTIONS_DIR
from .db import connect

logger = logging.getLogger("paper_agent.wiki")


# --- paths -----------------------------------------------------------------
def _coldir(slug: str) -> Path:
    return COLLECTIONS_DIR / slug


def _wikidir(slug: str) -> Path:
    return _coldir(slug) / "wiki"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _ts_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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


def _append_log(slug: str, reason: str, content: str) -> None:
    log = _wikidir(slug) / "log.md"
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    line = f"- {_now()} · {reason} · inputs_hash={h}\n"
    if not log.exists():
        log.write_text("# Generation Log\n\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as f:
        f.write(line)


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

# Phase C (2026-05-31): beliefs — agent-drafted candidates the user accepts
# to promote. Strict scope: a belief MUST cite ≥1 supporting paper in the
# collection; below the signal floor, the Suggest button isn't even shown.
_BELIEF_CANDIDATES_MAX = 5         # max candidates per Suggest run
_BELIEF_SUGGEST_FLOOR = 5          # min concept score OR ≥1 note to enable the button
_BELIEF_TITLE_MIN_LEN = 10         # shorter than this looks like a stub
_BELIEF_CONFIDENCE_VALUES = ("emerging", "medium", "uncertain")

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


# Phase C: beliefs tree.
#   wiki/sections/beliefs/                — accepted beliefs (the wiki content)
#   wiki/sections/beliefs/_candidates/    — agent-drafted candidates (the tray)
def _beliefs_dir(slug: str) -> Path:
    return _sections_dir(slug) / "beliefs"


def _belief_candidates_dir(slug: str) -> Path:
    return _beliefs_dir(slug) / "_candidates"


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


# --- belief tray (Phase C) ---------------------------------------------------

def _validate_belief_candidates(data: dict, valid_refs: set, concept_slugs: set) -> list[dict]:
    """Validate the belief-draft LLM output. Hard rules, enforced in code:
      - title is a non-empty sentence (≥_BELIEF_TITLE_MIN_LEN chars).
      - supporting_papers is a non-empty subset of valid_refs. A belief that
        can't cite a paper in the collection gets dropped — same anti-
        fabrication rule we use for the field model.
      - related_concepts is filtered against concept_slugs; unknown slugs drop.
      - confidence is clamped to _BELIEF_CONFIDENCE_VALUES (default 'emerging').
      - Output capped at _BELIEF_CANDIDATES_MAX. Duplicates (by title slug)
        deduped."""
    def text(s):
        return (s or "").strip() if isinstance(s, str) else ""
    raw = data.get("candidates") if isinstance(data.get("candidates"), list) else []
    out: list[dict] = []
    seen_slugs: set[str] = set()
    for c in raw:
        if not isinstance(c, dict):
            continue
        title = text(c.get("title"))
        if len(title) < _BELIEF_TITLE_MIN_LEN:
            continue
        slug = _SLUG_RE.sub("-", title.lower()).strip("-")[:60]
        if not slug or slug in seen_slugs:
            continue
        papers = [p for p in (c.get("supporting_papers") or []) if p in valid_refs]
        if not papers:
            continue  # un-cited beliefs are dropped (hard rule)
        concepts = [s for s in (c.get("related_concepts") or []) if s in concept_slugs]
        confidence = (text(c.get("confidence")).lower()
                      if text(c.get("confidence")).lower() in _BELIEF_CONFIDENCE_VALUES
                      else "emerging")
        out.append({
            "slug": slug, "title": title, "confidence": confidence,
            "supporting_papers": papers, "related_concepts": concepts,
        })
        seen_slugs.add(slug)
        if len(out) >= _BELIEF_CANDIDATES_MAX:
            break
    return out


def _read_belief_file(path: Path) -> dict | None:
    """Parse one belief .md file → dict {id, title, status, confidence,
    supporting_papers, related_concepts, generated_at, accepted_at?}. Returns
    None on read/parse failure (we skip rather than crash the panel)."""
    try:
        meta, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    title = (meta.get("title") or "").strip()
    if not title:
        return None
    return {
        "id": (meta.get("id") or path.stem).strip(),
        "title": title,
        "status": meta.get("status") or "candidate",
        "confidence": meta.get("confidence") or "emerging",
        "supporting_papers": list(meta.get("supporting_papers") or []),
        "related_concepts": list(meta.get("related_concepts") or []),
        "generated_at": meta.get("generated_at"),
        "accepted_at": meta.get("accepted_at"),
        "body": body.strip(),
    }


def list_belief_candidates(slug: str) -> list[dict]:
    """All pending candidates (the tray). Sorted oldest-first so the user
    sees the longest-pending suggestion at the top."""
    cdir = _belief_candidates_dir(slug)
    if not cdir.is_dir():
        return []
    out = [_read_belief_file(p) for p in sorted(cdir.glob("*.md"))]
    return [b for b in out if b]


def list_accepted_beliefs(slug: str) -> list[dict]:
    """All beliefs the user has accepted (Section 3 content). Excludes the
    _candidates/ subdirectory."""
    bdir = _beliefs_dir(slug)
    if not bdir.is_dir():
        return []
    out = []
    for p in sorted(bdir.glob("*.md")):
        b = _read_belief_file(p)
        if b and b.get("status") == "accepted":
            out.append(b)
    return out


def can_suggest_beliefs(slug: str) -> bool:
    """The 'Suggest beliefs' button only renders when there's honest signal:
    at least one concept score ≥ _BELIEF_SUGGEST_FLOOR OR at least one non-
    empty note in the collection. Below that, premature inference would
    anchor (the user's blueprint concern)."""
    if not _concepts_path(slug).is_file():
        return False
    try:
        cdata = json.loads(_concepts_path(slug).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    concepts = cdata.get("concepts") or []
    scores = attention_per_concept(slug, concepts)
    if any(s >= _BELIEF_SUGGEST_FLOOR for s in scores.values()):
        return True
    con = connect()
    try:
        row = con.execute(
            "SELECT 1 FROM paper_notes WHERE collection_slug=? AND ("
            "COALESCE(thoughts,'')<>'' OR COALESCE(summary,'')<>''"
            ") LIMIT 1", (slug,)
        ).fetchone()
        return bool(row)
    finally:
        con.close()


def suggest_beliefs(slug: str) -> dict:
    """One LLM call → 1-5 candidate beliefs written to the tray. The agent
    sees the concept space, the top highlights and notes, and the current
    accepted+candidate beliefs (so it doesn't propose duplicates).

    Returns ``{generated, dropped_dupes, dropped_invalid, error}``. Each
    candidate file is written individually so a partial parse failure still
    surfaces what survived."""
    if not _concepts_path(slug).is_file():
        return {"error": "No concepts file yet — draft the Field Model first.",
                "generated": 0}
    try:
        cdata = json.loads(_concepts_path(slug).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"error": "Concepts file is unreadable.", "generated": 0}
    concepts = cdata.get("concepts") or []
    if not concepts:
        return {"error": "No concepts to anchor beliefs against.", "generated": 0}
    if not can_suggest_beliefs(slug):
        return {"error": "Not enough attention signal to honestly suggest beliefs yet.",
                "generated": 0}

    # --- Gather signal: top highlights + notes (the user's writing) ---------
    con = connect()
    try:
        rows = con.execute(
            "SELECT selected_text FROM annotations WHERE collection_slug=? "
            "AND kind='highlight' AND COALESCE(selected_text,'')<>'' "
            "ORDER BY created_at DESC LIMIT 25", (slug,)
        ).fetchall()
        highlights = [(r["selected_text"] or "").strip() for r in rows if r["selected_text"]]
        rows = con.execute(
            "SELECT COALESCE(summary,'') s, COALESCE(thoughts,'') t, "
            "COALESCE(key_quotes,'') q FROM paper_notes WHERE collection_slug=? "
            "AND (COALESCE(summary,'')<>'' OR COALESCE(thoughts,'')<>'' "
            "     OR COALESCE(key_quotes,'')<>'') ORDER BY updated_at DESC LIMIT 15",
            (slug,)
        ).fetchall()
        notes = [" ".join(filter(None, [r["s"], r["t"], r["q"]])) for r in rows]
    finally:
        con.close()

    # --- Existing beliefs (so the agent doesn't propose dupes) --------------
    existing = list_accepted_beliefs(slug) + list_belief_candidates(slug)
    existing_titles = "\n".join(f"- {b['title']}" for b in existing) or "(none yet)"
    existing_slugs = {b["slug"] for b in existing if b.get("slug")} | {
        _SLUG_RE.sub("-", b["title"].lower()).strip("-")[:60] for b in existing}

    # --- Build the LLM input ------------------------------------------------
    concepts_blurb = "\n".join(
        f"- {c['name']} (slug: {c['slug']}; {c.get('blurb', '')})" for c in concepts)
    # included_refs: refs of papers actually in the collection (the validator gate)
    valid_refs = set()
    rmap = _ref_map(slug)
    for ref in rmap.keys():
        valid_refs.add(ref)
    valid_refs_blurb = "\n".join(f"- {r}" for r in sorted(valid_refs)[:60])

    user = (
        "CONCEPTS in this collection (slug — name — blurb):\n"
        f"{concepts_blurb}\n\n"
        "USER'S HIGHLIGHTS (most recent, one per line):\n"
        + ("\n".join(f"- {h}" for h in highlights[:25]) or "(none)") + "\n\n"
        "USER'S NOTES (most recent, joined):\n"
        + ("\n\n---\n\n".join(notes[:15]) or "(none)") + "\n\n"
        f"EXISTING BELIEFS (don't repeat these):\n{existing_titles}\n\n"
        "VALID PAPER REFS (only cite these exactly):\n"
        f"{valid_refs_blurb}\n"
    )

    from . import agent_skills
    system = (agent_skills.skill_body("belief-draft")
              or "Output JSON: {candidates:[{title, confidence, supporting_papers, related_concepts}]}.")
    try:
        out = llm.complete([{"role": "system", "content": system},
                            {"role": "user", "content": user}])
        data = _extract_json(out)
    except Exception:  # noqa: BLE001
        return {"error": "The LLM call failed.", "generated": 0}

    concept_slugs = {c["slug"] for c in concepts}
    candidates = _validate_belief_candidates(data or {}, valid_refs, concept_slugs)
    # Drop duplicates against existing beliefs
    new_candidates = [c for c in candidates if c["slug"] not in existing_slugs]
    dropped_dupes = len(candidates) - len(new_candidates)

    # --- Write each candidate as its own .md file ---------------------------
    cdir = _belief_candidates_dir(slug)
    cdir.mkdir(parents=True, exist_ok=True)
    import uuid
    for c in new_candidates:
        cid = uuid.uuid4().hex[:10]
        meta = {
            "id": cid, "type": "belief", "status": "candidate",
            "title": c["title"], "confidence": c["confidence"],
            "supporting_papers": c["supporting_papers"],
            "related_concepts": c["related_concepts"],
            "generated_by": "agent", "generator": "belief-draft",
            "generated_at": _now(),
        }
        (cdir / f"{cid}.md").write_text(frontmatter.dump(meta, ""), encoding="utf-8")

    _append_log(slug, f"suggested {len(new_candidates)} belief candidate(s) "
                       f"(dropped {dropped_dupes} dupes)", "belief-draft")
    return {"generated": len(new_candidates), "dropped_dupes": dropped_dupes,
            "dropped_invalid": (data or {}).get("candidates", []) and
                                len((data or {}).get("candidates", [])) - len(candidates)}


def accept_belief(slug: str, candidate_id: str) -> bool:
    """Promote a candidate to accepted. Moves the file from
    wiki/sections/beliefs/_candidates/<id>.md to wiki/sections/beliefs/<slug>.md
    and bumps status + accepted_at. Returns False if the candidate doesn't
    exist."""
    src = _belief_candidates_dir(slug) / f"{candidate_id}.md"
    if not src.is_file():
        return False
    text = src.read_text(encoding="utf-8")
    meta, body = frontmatter.parse(text)
    meta["status"] = "accepted"
    meta["accepted_at"] = _now()
    title_slug = _SLUG_RE.sub("-", (meta.get("title") or "belief").lower()).strip("-")[:60]
    if not title_slug:
        title_slug = candidate_id
    _beliefs_dir(slug).mkdir(parents=True, exist_ok=True)
    (_beliefs_dir(slug) / f"{title_slug}.md").write_text(
        frontmatter.dump(meta, body), encoding="utf-8")
    src.unlink()
    return True


def dismiss_belief(slug: str, candidate_id: str) -> bool:
    """Delete a candidate (the user said no). Returns False if not found."""
    src = _belief_candidates_dir(slug) / f"{candidate_id}.md"
    if not src.is_file():
        return False
    src.unlink()
    return True


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

    # --- Phase C: beliefs (tray + accepted) --------------------------------
    # Resolve supporting_papers refs to live paper objects (with attention
    # chips) and related_concepts slugs to concept names (with blurbs) so the
    # template can render rich cards without re-parsing the JSON files.
    concepts_for_belief = []
    try:
        if _concepts_path(slug).is_file():
            cdata = json.loads(_concepts_path(slug).read_text(encoding="utf-8"))
            concepts_for_belief = cdata.get("concepts") or []
    except (ValueError, OSError):
        concepts_for_belief = []
    concept_by_slug = {c["slug"]: c for c in concepts_for_belief}
    rmap_b = _ref_map(slug)
    paper_by_id_b = {p["id"]: p for p in papers}

    def _decorate_belief(b: dict) -> dict:
        papers_full = []
        for ref in b.get("supporting_papers") or []:
            pinfo = rmap_b.get(ref)
            if pinfo and pinfo["id"] in paper_by_id_b:
                papers_full.append(paper_by_id_b[pinfo["id"]])
        related = [{"slug": s,
                    "name": concept_by_slug.get(s, {}).get("name", s),
                    "blurb": concept_by_slug.get(s, {}).get("blurb", "")}
                   for s in b.get("related_concepts") or []
                   if s in concept_by_slug]
        return {**b, "papers": papers_full, "related": related}

    belief_candidates = [_decorate_belief(b) for b in list_belief_candidates(slug)]
    beliefs = [_decorate_belief(b) for b in list_accepted_beliefs(slug)]

    return {"needs_migration": False, "thesis": thesis, "landscape": landscape,
            "papers": papers, "focus": focus, "recommended": recommended,
            "belief_candidates": belief_candidates, "beliefs": beliefs,
            "can_suggest_beliefs": can_suggest_beliefs(slug),
            "meta": meta_out}
