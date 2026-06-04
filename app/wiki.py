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
from collections import Counter, defaultdict
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
# Phase B (2026-05-31): concept space
_CONCEPTS_MAX = 12               # how many concepts the field-model skill may keep
_CONCEPTS_MIN_NAME_LEN = 3        # drop garbage names ("ab"); same rule as landscape items
_CONCEPT_SYNONYMS_MAX = 6         # cap synonyms per concept (keeps the regex set sane)
# Threshold on a single concept's attention score before the Focus section
# renders. Below this we don't even hint at user-mind inference (the user's
# "premature inference is anchoring" concern from the blueprint).
_FOCUS_CONCEPT_FLOOR = 3

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


def _landscape_json_path(slug: str) -> Path:
    """Structured landscape (problems/methods carry paper membership for the
    graph). Source of truth when present; landscape.md is the human-readable
    render + the fallback for pre-2026-05-31 collections that predate it."""
    return _sections_dir(slug) / "landscape.json"


def _concepts_path(slug: str) -> Path:
    return _sections_dir(slug) / "concepts.json"


def _benchmarks_path(slug: str) -> Path:
    """Extracted benchmark results (method × benchmark performance). Filled on
    demand by extract_benchmarks() (one LLM call); read live by load_benchmarks().
    Each result is grounded in a paper that reported it — no result without a
    citing paper survives the validator."""
    return _sections_dir(slug) / "benchmarks.json"


def _themes_path(slug: str) -> Path:
    """Cached theme names+descriptions, keyed by cluster signature. Filled on
    demand by name_themes() (one LLM call); read live by connection_view().
    Clusters are computed deterministically — only the LABELS live here."""
    return _sections_dir(slug) / "themes.json"


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
    the deterministic attention scorer + a `papers` membership list. Concept
    count capped at _CONCEPTS_MAX; short/empty names dropped; slugs deduped."""
    def text(s):
        return (s or "").strip() if isinstance(s, str) else ""
    valid_refs = valid_refs or set()

    th = data.get("thesis") if isinstance(data.get("thesis"), dict) else {}
    thesis = {"one_paragraph": text(th.get("one_paragraph"))}
    for k in _THESIS_CALLOUTS:
        thesis[k] = text(th.get(k))

    ls = data.get("landscape") if isinstance(data.get("landscape"), dict) else {}

    def str_items(key: str) -> list[str]:
        """debates / open_questions: plain short strings (not paper-anchored)."""
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

    def node_items(key: str) -> list[dict]:
        """problems / methods: paper-anchored nodes. The agent may send a plain
        string (legacy) or an object {name|text, papers}; normalize to
        {text, papers} with papers filtered to real refs. These become graph
        nodes (concept-style membership)."""
        raw = ls.get(key) or []
        if not isinstance(raw, list):
            return []
        out, seen = [], set()
        for x in raw:
            if isinstance(x, dict):
                s = text(x.get("text") or x.get("name"))
                papers = [r for r in (x.get("papers") or []) if r in valid_refs]
            else:
                s, papers = text(x), []
            if len(s) < 3 or s.lower() in seen:
                continue
            seen.add(s.lower())
            # dedupe papers, preserve order
            seen_p, pp = set(), []
            for r in papers:
                if r not in seen_p:
                    seen_p.add(r); pp.append(r)
            out.append({"text": s, "papers": pp})
            if len(out) >= _LANDSCAPE_MAX_ITEMS:
                break
        return out

    landscape = {
        "problems":       node_items("problems"),
        "methods":        node_items("methods"),
        "debates":        str_items("debates"),
        "open_questions": str_items("open_questions"),
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
        # LLM-assigned membership: which papers this concept covers. The agent
        # sees every abstract at draft, so this is the reliable mapping; the
        # deterministic synonym match (papers_to_concepts) is a fallback for
        # what the LLM misses / for pre-membership drafts. Filtered to real refs.
        cpapers, seen_cp = [], set()
        for ref in (c.get("papers") or []):
            if ref in valid_refs and ref not in seen_cp:
                seen_cp.add(ref)
                cpapers.append(ref)
        concepts.append({
            "name": name, "slug": slug, "synonyms": synonyms,
            "blurb": text(c.get("blurb")), "papers": cpapers,
        })
        if len(concepts) >= _CONCEPTS_MAX:
            break

    return {"thesis": thesis, "landscape": landscape, "concepts": concepts}


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
            # problems/methods are {text, papers}; debates/open_questions are str.
            body_parts.append(f"- {it['text'] if isinstance(it, dict) else it}")
        body_parts.append("")
    meta = {"type": "landscape", "title": f"Research Landscape: {slug}",
            "generated_by": "agent", "generator": "field-model",
            "generated_at": _now(), **meta_extra}
    _landscape_path(slug).write_text(frontmatter.dump(meta, "\n".join(body_parts)),
                                      encoding="utf-8")
    # Structured source of truth — carries the problem/method → paper edges the
    # graph engine needs (the .md above loses them).
    _landscape_json_path(slug).write_text(
        json.dumps({"landscape": landscape,
                    "_meta": {"generated_by": "agent", "generator": "field-model",
                              "generated_at": _now()}}, indent=2),
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


# --- Agent section editor (propose → diff → apply; reversible; no silent writes) ---
# The user instructs a change in plain language; the agent re-emits the section's
# structured content (one-shot completion, NO PDF/MCP tools — so a write-capable
# path never roams untrusted content). Our validators clamp it, the UI shows a
# diff, and only the user's Apply writes (with a one-step undo snapshot + log).

def _thesis_history(slug: str) -> Path:
    return _sections_dir(slug) / ".history" / "thesis.bak"


def current_thesis(slug: str) -> dict | None:
    """The thesis as the fielded dict (one_paragraph + 3 callouts), or None."""
    p = _thesis_path(slug)
    if not p.is_file():
        return None
    try:
        _, body = frontmatter.parse(p.read_text(encoding="utf-8"))
    except OSError:
        return None
    return _parse_thesis_body(body)


def _concept_names(slug: str) -> list[str]:
    if not _concepts_path(slug).is_file():
        return []
    try:
        cs = json.loads(_concepts_path(slug).read_text(encoding="utf-8")).get("concepts") or []
        return [c.get("name", "") for c in cs if c.get("name")]
    except (ValueError, OSError):
        return []


def propose_thesis_edit(slug: str, instruction: str) -> dict:
    """One LLM call → a revised thesis from the user's instruction. Returns
    ``{ok, error, current, proposed}``; writes nothing."""
    cur = current_thesis(slug)
    if cur is None:
        return {"ok": False, "error": "No thesis to edit yet — draft the Field Model first."}
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False, "error": "Tell the agent what to change."}

    from . import agent_skills
    system = (agent_skills.skill_body("section-edit")
              or "Revise the section's JSON per the instruction; same keys; change only "
                 "what's asked; invent nothing. Output JSON only.")
    concepts = _concept_names(slug)
    user = (
        "SECTION: Collection Thesis\n"
        "SHAPE (return exactly these keys): "
        "{one_paragraph, core_tension, key_intuition, central_question}\n\n"
        "CURRENT CONTENT (JSON):\n" + json.dumps(cur, ensure_ascii=False, indent=2) + "\n\n"
        + (f"(For context, this collection's concepts: {', '.join(concepts[:20])}.)\n\n" if concepts else "")
        + "USER INSTRUCTION:\n" + instruction + "\n")
    try:
        data = _extract_json(llm.complete([{"role": "system", "content": system},
                                           {"role": "user", "content": user}])) or {}
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "The LLM call failed."}
    th = data.get("thesis") if isinstance(data.get("thesis"), dict) else data
    proposed = _validate_field_model({"thesis": th})["thesis"]
    if not any(proposed.values()):
        return {"ok": False, "error": "The agent returned nothing usable."}
    return {"ok": True, "current": cur, "proposed": proposed}


def apply_thesis_edit(slug: str, proposed: dict) -> dict:
    """Apply a proposed thesis (snapshot current for undo, write, log). The
    proposed dict is re-validated here (never trust the round-trip)."""
    proposed = _validate_field_model({"thesis": proposed})["thesis"]
    if not any(proposed.values()):
        return {"ok": False, "error": "Empty edit."}
    p = _thesis_path(slug)
    if not p.is_file():
        return {"ok": False, "error": "No thesis to edit."}
    cur_text = p.read_text(encoding="utf-8")
    meta, _ = frontmatter.parse(cur_text)
    _thesis_history(slug).parent.mkdir(parents=True, exist_ok=True)
    _thesis_history(slug).write_text(cur_text, encoding="utf-8")   # one-step undo
    meta_extra = {k: meta.get(k) for k in ("paper_count", "pdfs_read", "pdfs_missing")
                  if meta.get(k) is not None}
    _write_thesis_page(slug, proposed, meta_extra)
    _append_log(slug, "edited thesis via agent instruction",
                json.dumps(proposed, ensure_ascii=False)[:500])
    return {"ok": True}


def has_thesis_undo(slug: str) -> bool:
    return _thesis_history(slug).is_file()


def undo_thesis_edit(slug: str) -> dict:
    """Restore the thesis from the last agent-edit snapshot."""
    bak = _thesis_history(slug)
    if not bak.is_file():
        return {"ok": False, "error": "Nothing to undo."}
    _thesis_path(slug).write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
    bak.unlink()
    _append_log(slug, "undid last thesis agent edit", "")
    return {"ok": True}


# --- Benchmarks (method × benchmark performance table) ----------------------
# Net-new extraction layer (2026-06-02). One LLM call reads the collection's
# abstracts + PDF excerpts and pulls out reported benchmark numbers as
# (method, benchmark, metric, value) tuples, each citing the paper that
# reported it. The grounding rule is enforced in code: a result with no valid
# supporting paper is dropped (no fabricated numbers reach the table).
_BENCHMARK_RESULTS_MAX = 240


def _num(value) -> float | None:
    """Parse a leading number out of a value string ('56.3', '56.3%', '0.71') for
    best-in-column comparison. None when there's no parseable number."""
    if value is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(m.group()) if m else None


def _validate_benchmarks(data: dict, valid_refs: set[str]) -> list[dict]:
    """Keep only well-formed, paper-grounded results. Each surviving row has a
    non-empty method, benchmark, value, and a supporting paper ref that exists in
    the collection. Deduped by (method, benchmark, paper); capped."""
    out, seen = [], set()
    for r in (data or {}).get("results") or []:
        if not isinstance(r, dict):
            continue
        method = (r.get("method") or "").strip()
        bench = (r.get("benchmark") or "").strip()
        value = (str(r.get("value")) if r.get("value") is not None else "").strip()
        ref = (r.get("paper") or "").strip()
        if not (method and bench and value) or ref not in valid_refs:
            continue            # un-grounded or incomplete → drop (the honesty gate)
        if _num(value) is None:
            continue            # not a number we can put in a cell / compare
        key = (method.lower(), bench.lower(), ref)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "method": method[:60], "benchmark": bench[:48],
            "metric": (r.get("metric") or "").strip()[:24],
            "value": value[:16], "paper": ref,
            "higher_is_better": r.get("higher_is_better", True) is not False,
        })
        if len(out) >= _BENCHMARK_RESULTS_MAX:
            break
    return out


def extract_benchmarks(slug: str) -> dict:
    """One LLM call → method × benchmark performance numbers, written to
    wiki/sections/benchmarks.json. The agent sees the same abstract + PDF-excerpt
    digest the Field Model does, and is told to report ONLY numbers stated in the
    text, each citing the paper that reported it. Returns ``{results, error}``."""
    papers = _collection_abstracts(slug)
    with_abs = [p for p in papers if p["abstract"]]
    for p in with_abs:
        p["pdf_excerpt"] = _pdf_excerpt(p["id"], max_chars=_OVERVIEW_PDF_CHARS)
    digest, included_refs, _pdf_refs = _overview_digest(with_abs)
    if not digest.strip():
        return {"results": 0, "error": "No paper abstracts to read yet."}

    from . import agent_skills
    system = (agent_skills.skill_body("benchmark-extract")
              or "Output JSON: {results:[{method, benchmark, metric, value, "
                 "higher_is_better, paper}]}. Report ONLY numbers explicitly "
                 "stated in the provided text; cite the paper ref each came from.")
    try:
        out = llm.complete([{"role": "system", "content": system},
                            {"role": "user", "content": "Papers:\n\n" + digest}])
        data = _extract_json(out)
    except Exception:  # noqa: BLE001
        return {"results": 0, "error": "The LLM call failed."}

    results = _validate_benchmarks(data or {}, included_refs)
    _sections_dir(slug).mkdir(parents=True, exist_ok=True)
    _benchmarks_path(slug).write_text(
        json.dumps({"results": results, "generated_by": "agent",
                    "generator": "benchmark-extract", "generated_at": _now()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    _append_log(slug, f"extracted {len(results)} benchmark result(s)", digest)
    return {"results": len(results), "error": None}


def load_benchmarks(slug: str) -> dict | None:
    """Shape benchmarks.json into the table the template renders:

      {benchmarks: [name,...],                 # column order (most-covered first)
       methods: [{name, n, cells: [cell|None per benchmark]}],  # row per method
       n_results, generated_at}

    where a cell is {value, metric, paper:{id,title}, best}. ``best`` flags the
    leading value in its column (max if higher_is_better, else min). Methods are
    ordered by coverage (cells filled) desc — the template shows the top 5 and
    reveals the rest on demand. Returns None when nothing has been extracted."""
    p = _benchmarks_path(slug)
    if not p.is_file():
        return None
    try:
        results = json.loads(p.read_text(encoding="utf-8")).get("results") or []
        generated_at = json.loads(p.read_text(encoding="utf-8")).get("generated_at")
    except (ValueError, OSError):
        return None
    if not results:
        return {"benchmarks": [], "methods": [], "n_results": 0,
                "generated_at": None}

    rmap = _ref_map(slug)
    # Column order: benchmarks by number of methods reporting them (desc).
    bench_methods: dict[str, set[str]] = defaultdict(set)
    bench_dir: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        bench_methods[r["benchmark"]].add(r["method"])
        bench_dir[r["benchmark"]].append(bool(r["higher_is_better"]))
    benchmarks = sorted(bench_methods, key=lambda b: (-len(bench_methods[b]), b.lower()))
    # Per-benchmark direction by majority vote of its rows.
    higher = {b: (sum(d) >= len(d) / 2) for b, d in bench_dir.items()}

    # Best (method,benchmark) cell — if a method reports a benchmark from several
    # papers, keep the leading value for that direction.
    cell_by: dict[tuple[str, str], dict] = {}
    for r in results:
        k = (r["method"], r["benchmark"])
        cur = cell_by.get(k)
        n = _num(r["value"])
        if cur is None or (n is not None and (
                (higher[r["benchmark"]] and n > (_num(cur["value"]) or float("-inf")))
                or (not higher[r["benchmark"]] and n < (_num(cur["value"]) or float("inf"))))):
            pinfo = rmap.get(r["paper"])
            cell_by[k] = {
                "value": r["value"], "metric": r["metric"],
                "paper": {"id": pinfo["id"], "title": pinfo["title"]} if pinfo else None,
                "best": False,
            }

    # Flag the best value per benchmark column.
    for b in benchmarks:
        vals = [(m, cell_by[(m, b)]) for m in bench_methods[b] if (m, b) in cell_by]
        nums = [(m, _num(c["value"])) for m, c in vals if _num(c["value"]) is not None]
        if nums:
            best_m = (max if higher[b] else min)(nums, key=lambda x: x[1])[0]
            cell_by[(best_m, b)]["best"] = True

    method_names = {r["method"] for r in results}
    methods = []
    for m in method_names:
        cells = [cell_by.get((m, b)) for b in benchmarks]
        n = sum(1 for c in cells if c)
        methods.append({"name": m, "n": n, "cells": cells})
    # Most-covered methods first; tie-break by how many columns they lead.
    methods.sort(key=lambda r: (-r["n"], -sum(1 for c in r["cells"] if c and c["best"]),
                                r["name"].lower()))
    return {"benchmarks": benchmarks, "methods": methods,
            "n_results": len(results), "generated_at": generated_at}


def _add_seed(slug: str) -> str:
    """Free-text 'what this collection is about' for the add-paper recommender:
    the thesis paragraph + concept names + open questions + problem statements."""
    parts = []
    try:
        _, tb = frontmatter.parse(_thesis_path(slug).read_text(encoding="utf-8"))
        parts.append(tb.split("##", 1)[0].strip())
    except OSError:
        pass
    if _concepts_path(slug).is_file():
        try:
            cs = json.loads(_concepts_path(slug).read_text(encoding="utf-8")).get("concepts") or []
            if cs:
                parts.append("Concepts: " + ", ".join(c.get("name", "") for c in cs))
        except (ValueError, OSError):
            pass
    ls = _load_landscape(slug)
    if ls.get("open_questions"):
        parts.append("Open questions: " + "; ".join(ls["open_questions"]))
    if ls.get("problems"):
        parts.append("Problems: " + "; ".join(p["text"] for p in ls["problems"]))
    return "\n".join(p for p in parts if p).strip()


# Collection "Suggested reading" purposes → (seed, intent) for the arXiv search.
COLLECTION_PURPOSES = ("related", "gaps", "concept", "thesis", "adjacent", "custom")


def _purpose_seed(slug: str, purpose: str, target: str = "", custom: str = "") -> tuple[str, str]:
    """Build (seed, intent) for a collection suggested-reading purpose. seed = the
    free-text focus; intent = what to look for / how to judge fit."""
    ls = _load_landscape(slug)
    thesis = current_thesis(slug) or {}
    para = thesis.get("one_paragraph", "")
    concepts = _concept_names(slug)
    if purpose == "related":
        return (_add_seed(slug),
                "be the most relevant key or recent work related to this collection")
    if purpose == "gaps":
        bits = []
        if ls.get("open_questions"):
            bits.append("Open questions: " + "; ".join(ls["open_questions"]))
        if ls.get("problems"):
            bits.append("Problems: " + "; ".join(p["text"] for p in ls["problems"]))
        return ("\n".join(bits) or para, "address an open problem or stated gap in this collection")
    if purpose == "concept":
        name = target.strip() or (concepts[0] if concepts else "")
        return (f"{para}\n\nConcept of interest: {name}".strip(),
                f"deepen or extend the concept “{name}”")
    if purpose == "thesis":
        return (para or _add_seed(slug),
                "represent recent or state-of-the-art work directly relevant to this thesis")
    if purpose == "adjacent":
        return (f"{para}\n\nConcepts: {', '.join(concepts)}".strip(),
                "come from an adjacent area this collection doesn't yet cover but that connects to it")
    if purpose == "custom":
        return (_add_seed(slug), custom.strip() or "be worth reading next for this collection")
    return (_add_seed(slug), "")        # default: original 'extend/fill gaps' framing


def suggest_papers_to_add(slug: str, purpose: str = "gaps", target: str = "",
                          custom: str = "") -> dict:
    """On-demand arXiv discovery for a chosen PURPOSE (Fill field gaps / Extend a
    concept / Latest on the thesis / Broaden-adjacent / Custom). New candidates
    (not already in the collection, not already pending) are enqueued into triage
    as 'pending'. Network action — gated behind an explicit button.
    Returns ``{added, error}``."""
    from . import discover, library, triage
    from .config import load_config
    if purpose not in COLLECTION_PURPOSES:
        purpose = "gaps"
    seed, intent = _purpose_seed(slug, purpose, target, custom)
    if not seed.strip():
        return {"added": 0, "error": "Draft the Field Model first — there's no focus to search from."}
    try:
        limit = max(1, min(50, int(load_config().get("recommend_count", "10"))))
    except (TypeError, ValueError):
        limit = 10
    have_titles = {(p.get("title") or "").lower() for p in library.list_papers(slug)}
    have_arxiv = {p.get("arxiv_id") for p in library.list_papers(slug) if p.get("arxiv_id")}
    pending_arxiv = {c.get("arxiv_id") for c in triage.list_triage(slug, "pending") if c.get("arxiv_id")}
    try:
        cands = discover.find_related_papers(seed, exclude_titles=have_titles,
                                             limit=limit, intent=intent)
        cands = discover.validate_candidates(intent or seed, cands, intent)  # find → verify
    except Exception as exc:  # noqa: BLE001
        return {"added": 0, "error": f"arXiv discovery failed: {exc}"}
    added = 0
    for c in cands:
        aid = c.get("arxiv_id")
        if not aid or aid in have_arxiv or aid in pending_arxiv:
            continue
        note = c.get("note", "")
        if c.get("verdict") == "pass" and c.get("justification"):
            note = f"{note}  ·  ✓ verified: {c['justification']}"
        elif c.get("verdict") == "weak":
            note = f"{note}  ·  ~ weak match (verify)"
        if triage.add_from_arxiv(slug, aid, c.get("title", ""), note):
            pending_arxiv.add(aid)
            added += 1
    _append_log(slug, f"suggested {added} paper(s) [{purpose}]", seed)
    return {"added": added, "error": None}


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
    # Three files: thesis.md, landscape.md (+ landscape.json), concepts.json.
    stage("writing", pages_done=0, pages_total=3)
    _wipe_sections_tree(slug)
    _sections_dir(slug).mkdir(parents=True, exist_ok=True)
    meta_extra = {"paper_count": len(included_refs),
                  "pdfs_read": len(pdf_refs),
                  "pdfs_missing": len(included_refs) - len(pdf_refs)}
    _write_thesis_page(slug, field["thesis"], meta_extra)
    stage("writing", pages_done=1, pages_total=3)
    _write_landscape_page(slug, field["landscape"], meta_extra)
    stage("writing", pages_done=2, pages_total=3)
    _write_concepts_file(slug, field["concepts"])
    stage("writing", pages_done=3, pages_total=3)
    _append_log(slug, f"generated field model "
                       f"({len(pdf_refs)}/{len(included_refs)} PDFs, "
                       f"{len(field['concepts'])} concepts)", digest)
    # Name the structural themes now (one extra LLM call, folded into this
    # already-explicit regen) so they're labelled by default — no separate
    # "Name themes" button. Failure here doesn't fail the regen.
    stage("naming")
    try:
        name_themes(slug)
    except Exception:  # noqa: BLE001
        pass
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


def papers_to_concepts(slug: str, concepts: list[dict], max_tags: int = 2) -> dict[int, list[dict]]:
    """Map each paper to the concept(s) it belongs to — turning the Papers (Evidence)
    row from a flat list into a map of the Field Model's concept space.

    Two sources, unioned:
      1. **LLM-assigned membership** (``concept["papers"]`` refs) — the reliable
         mapping the field-model agent produced at draft (it saw every abstract).
         Listed FIRST per paper.
      2. **Deterministic synonym match** over title + abstract — a no-LLM fallback
         that fills what the LLM missed and covers pre-membership drafts.

    Returns ``{paper_id: [{name, slug}, ...]}`` (up to ``max_tags`` per paper).
    Papers tied to no concept are absent (no fake tags)."""
    if not concepts:
        return {}
    name_by_slug = {c["slug"]: c.get("name", c["slug"]) for c in concepts}
    patterns: dict[str, re.Pattern] = {}
    for c in concepts:
        syns = [re.escape(s) for s in (c.get("synonyms") or []) if s]
        if syns:
            patterns[c["slug"]] = re.compile(r"\b(?:" + "|".join(syns) + r")\b", flags=re.IGNORECASE)

    # Invert LLM membership (concept.papers refs) → {ref: [concept_slug, ...]}.
    llm_by_ref: dict[str, list[str]] = defaultdict(list)
    for c in concepts:
        for ref in (c.get("papers") or []):
            llm_by_ref[ref].append(c["slug"])

    out: dict[int, list[dict]] = {}
    for p in _collection_abstracts(slug):
        ordered: list[str] = []      # concept slugs, LLM-assigned first, then synonym
        for s in llm_by_ref.get(p["ref"], []):
            if s in name_by_slug and s not in ordered:
                ordered.append(s)
        text = f"{p.get('title', '')} {p.get('abstract', '')}"
        syn_hits = sorted(
            ((s, len(pat.findall(text))) for s, pat in patterns.items() if pat.search(text)),
            key=lambda x: -x[1])
        for s, _ in syn_hits:
            if s not in ordered:
                ordered.append(s)
        if ordered:
            out[p["id"]] = [{"name": name_by_slug[s], "slug": s} for s in ordered[:max_tags]]
    return out


def attention_counts(slug: str) -> tuple[int, int]:
    """``(highlights, non_empty_notes)`` for the collection — the raw attention
    signal counts shown in the header stat strip. Cheap; no LLM."""
    con = connect()
    try:
        h = con.execute(
            "SELECT COUNT(*) FROM annotations "
            "WHERE collection_slug=? AND kind='highlight'", (slug,)).fetchone()[0]
        n = con.execute(
            "SELECT COUNT(*) FROM paper_notes WHERE collection_slug=? AND ("
            "  COALESCE(summary,'')<>'' OR COALESCE(thoughts,'')<>'' "
            "  OR COALESCE(key_quotes,'')<>'')", (slug,)).fetchone()[0]
        return h, n
    finally:
        con.close()


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
    """Reverse of _write_landscape_page, for collections that predate
    landscape.json (the .md is the only source). Problems/methods come back as
    paper-anchored nodes with EMPTY papers (the .md lost the edges — a regenerate
    repopulates them); debates/open_questions are plain strings."""
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
                txt = s[2:].strip()
                if key in ("problems", "methods"):
                    out[key].append({"text": txt, "papers": []})
                else:
                    out[key].append(txt)
    return out


def _load_landscape(slug: str) -> dict:
    """Read the structured landscape: prefer landscape.json (carries paper edges),
    fall back to parsing landscape.md for pre-2026-05-31 collections."""
    jp = _landscape_json_path(slug)
    if jp.is_file():
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
            ls = data.get("landscape") or {}
            # Defensive: ensure the expected keys + shapes exist.
            return {
                "problems": ls.get("problems") or [],
                "methods": ls.get("methods") or [],
                "debates": ls.get("debates") or [],
                "open_questions": ls.get("open_questions") or [],
            }
        except (ValueError, OSError):
            pass
    try:
        _, body = frontmatter.parse(_landscape_path(slug).read_text(encoding="utf-8"))
    except OSError:
        return {"problems": [], "methods": [], "debates": [], "open_questions": []}
    return _parse_landscape_body(body)


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

    # --- Read the section files ----------------------------------------------
    try:
        thesis_meta, thesis_body = frontmatter.parse(
            _thesis_path(slug).read_text(encoding="utf-8"))
    except OSError:
        return None
    thesis = _parse_thesis_body(thesis_body)
    # Landscape: structured (landscape.json) when present, else parse the .md.
    landscape = _load_landscape(slug)

    # --- Concept space (drives Focus, belief tags, and the Papers evidence map) --
    concept_list: list[dict] = []
    try:
        if _concepts_path(slug).is_file():
            concept_list = (json.loads(_concepts_path(slug).read_text(encoding="utf-8"))
                            .get("concepts") or [])
    except (ValueError, OSError):
        concept_list = []

    # --- Papers (live from DB, decorated with attention + concept tags) ----------
    from . import library
    raw_papers = library.list_papers(slug)
    scores = attention_scores(slug)
    nonzero = sorted(v for v in scores.values() if v > 0)
    hot_threshold = max(_ATTENTION_HOT_FLOOR, nonzero[len(nonzero) // 2]) if nonzero else None
    changed = attention_changed_since(slug, attention_since)
    # Per-paper concept tags — turns the evidence row into a map of the concept space.
    paper_tags = papers_to_concepts(slug, concept_list)
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
            "read": bool(p.get("read")),
            "tags": paper_tags.get(pid, []),
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

    # Recommended papers to ADD (was: reading order). Pending arXiv candidates
    # surfaced by the discovery seed (suggest_papers_to_add → triage). Accept
    # imports into the collection; Dismiss rejects. Sourced live from triage.
    from . import triage as _triage
    try:
        add_candidates = _triage.list_triage(slug, status="pending")
    except Exception:  # noqa: BLE001
        add_candidates = []

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

    # --- Knowledge-graph connections & themes (structural; no LLM) ----------
    connections = connection_view(slug)

    # Tag each paper with the theme(s) it sits in (its anchored entities'
    # clusters) so the Papers section can show theme chips + filter by theme.
    if connections:
        theme_name = {t["index"]: (t["name"] or f"Theme {t['index']}")
                      for t in connections.get("themes", [])}
        ptheme_map = connections.get("paper_themes", {})
        for p in papers:
            idxs = ptheme_map.get(p["id"], [])
            p["themes"] = [{"index": i, "name": theme_name.get(i, f"Theme {i}")}
                           for i in idxs]
        # Link Section 2 ↔ Section 5: tag each landscape problem/method with the
        # graph node it maps to (same slug the graph uses) + its theme, so the
        # template can show a theme badge and jump-to-highlight in the map. Only
        # paper-anchored items become graph nodes; others get node_key=None.
        graph_ids = {n["id"] for n in connections.get("graph", {}).get("nodes", [])}
        entity_themes = connections.get("entity_themes", {})
        _rmap_ls = _ref_map(slug)
        _pbyid_ls = {p["id"]: p for p in papers}
        for _kind in ("problem", "method"):
            for item in landscape.get(_kind + "s") or []:
                if not isinstance(item, dict):
                    continue
                key = f"{_kind}:" + (_SLUG_RE.sub("-", item["text"].lower()).strip("-")[:60] or _kind)
                item["node_key"] = key if key in graph_ids else None
                item["theme"] = entity_themes.get(key)
                # Resolve anchored papers (landscape.json carries refs) to live
                # paper objects for the dedicated Problems/Methods tab cards.
                seen_pids, papers_full = set(), []
                for ref in item.get("papers") or []:
                    pinfo = _rmap_ls.get(ref)
                    if pinfo and pinfo["id"] in _pbyid_ls and pinfo["id"] not in seen_pids:
                        seen_pids.add(pinfo["id"])
                        papers_full.append(_pbyid_ls[pinfo["id"]])
                item["papers_full"] = papers_full
    else:
        for p in papers:
            p["themes"] = []

    # --- Concept space (full list w/ attention score + member papers) for the
    # Concepts tab. Score is the same deterministic per-concept attention the
    # Focus sidebar uses; papers are the live objects mapped via synonym/LLM. ---
    concept_view: list[dict] = []
    if concept_list:
        cscores = attention_per_concept(slug, concept_list)
        cp: dict[str, list] = defaultdict(list)
        for p in papers:
            for t in p.get("tags") or []:
                cp[t["slug"]].append(p)
        for c in concept_list:
            concept_view.append({
                "name": c.get("name", c["slug"]), "slug": c["slug"],
                "blurb": c.get("blurb", ""),
                "score": cscores.get(c["slug"], 0),
                "papers": cp.get(c["slug"], []),
            })
        concept_view.sort(key=lambda c: (-c["score"], -len(c["papers"]), c["name"].lower()))

    return {"needs_migration": False, "thesis": thesis, "landscape": landscape,
            "papers": papers, "focus": focus, "add_candidates": add_candidates,
            "belief_candidates": belief_candidates, "beliefs": beliefs,
            "can_suggest_beliefs": can_suggest_beliefs(slug),
            "connections": connections, "concepts": concept_view,
            "meta": meta_out}


# --- knowledge graph: assembly + render view (2026-05-31) ---------------------

def build_collection_graph(slug: str) -> dict:
    """Assemble the structural knowledge graph for a collection: nodes are
    papers + concepts + problems + methods + accepted beliefs; edges are the
    memberships we already compute (concept/problem/method/belief ↔ papers) plus
    belief→concept links. Returns the ``app.graph`` graph dict. No LLM, no
    embeddings — purely structural over data already on disk."""
    from . import graph as _graph, library

    rmap = _ref_map(slug)
    resolve = lambda refs: sorted({rmap[r]["id"] for r in (refs or []) if r in rmap})

    papers_min = [{"id": p["id"], "title": p.get("title", "")}
                  for p in library.list_papers(slug)]

    entities: list[dict] = []

    # Concepts: union of LLM membership + synonym match, via papers_to_concepts
    # (inverted from paper→concepts to concept→paper_ids).
    concept_list, concept_name = [], {}
    if _concepts_path(slug).is_file():
        try:
            concept_list = (json.loads(_concepts_path(slug).read_text(encoding="utf-8"))
                            .get("concepts") or [])
        except (ValueError, OSError):
            concept_list = []
    concept_papers: dict[str, set[int]] = defaultdict(set)
    for pid, tags in papers_to_concepts(slug, concept_list).items():
        for t in tags:
            concept_papers[t["slug"]].add(pid)
    for c in concept_list:
        concept_name[c["slug"]] = c.get("name", c["slug"])
        pids = set(concept_papers.get(c["slug"], set())) | set(resolve(c.get("papers")))
        if pids:
            entities.append({"key": f"concept:{c['slug']}", "kind": "concept",
                             "label": c.get("name", c["slug"]), "paper_ids": pids})

    # Problems + methods from the structured landscape.
    landscape = _load_landscape(slug)
    for kind in ("problem", "method"):
        for item in landscape.get(kind + "s") or []:
            if not isinstance(item, dict):
                continue
            pids = resolve(item.get("papers"))
            if not pids:
                continue   # an unanchored problem/method is not a graph node
            key = f"{kind}:" + (_SLUG_RE.sub('-', item['text'].lower()).strip('-')[:60] or kind)
            entities.append({"key": key, "kind": kind, "label": item["text"], "paper_ids": pids})

    # Accepted beliefs → papers (supporting) + concept links (related). Belief
    # dicts from list_accepted_beliefs key on "id" (not "slug").
    for b in list_accepted_beliefs(slug):
        bkey = b.get("id") or _SLUG_RE.sub("-", (b.get("title") or "belief").lower()).strip("-")
        pids = resolve(b.get("supporting_papers"))
        links = [f"concept:{s}" for s in (b.get("related_concepts") or [])
                 if s in concept_name]
        if pids or links:
            entities.append({"key": f"belief:{bkey}", "kind": "belief",
                             "label": b.get("title", bkey),
                             "paper_ids": pids, "links": links})

    return _graph.build_graph(papers_min, entities)


# --- Theme naming (cached LLM labels over deterministic clusters) -------------
# Section 5's structure is computed live with no LLM. The only LLM-touched part
# is the human-readable NAME of each cluster, which is cached by the cluster's
# membership signature and refreshed only on demand (the "Name themes" button)
# or when membership changes. The render path never calls the LLM.

def _theme_sig(entity_keys) -> str:
    """Stable signature for a cluster = hash of its sorted entity keys. Same
    members → same name, regardless of run order. Papers are excluded (they
    drift in/out as the collection grows; the idea-set defines the theme)."""
    return hashlib.sha1("|".join(sorted(entity_keys)).encode("utf-8")).hexdigest()[:16]


def _theme_strength(cohesion: dict) -> str:
    """Qualitative binding strength from the COMPUTED cohesion (shared papers +
    intra-cluster concept links). Honest label, not a guess."""
    score = (cohesion.get("shared_papers", 0) or 0) + (cohesion.get("links", 0) or 0)
    if score >= 5:
        return "strong"
    if score >= 3:
        return "medium"
    return "emerging"


def _load_theme_names(slug: str) -> dict:
    """{sig: {name, description}} from themes.json; {} if missing/unreadable."""
    p = _themes_path(slug)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("names", {}) or {}
    except (ValueError, OSError):
        return {}


def _save_theme_names(slug: str, names: dict) -> None:
    _sections_dir(slug).mkdir(parents=True, exist_ok=True)
    _themes_path(slug).write_text(
        json.dumps({"names": names}, ensure_ascii=False, indent=2), encoding="utf-8")


def _cluster_entities(slug: str):
    """The deterministic clusters as ``[(sig, [entity_keys], [paper_titles])]``,
    entity-only (the unit a theme names), ordered like connection_view's themes
    (size desc). Shared by name_themes (write) and connection_view (read)."""
    from . import graph as _graph
    g = build_collection_graph(slug)
    nodes = g["nodes"]
    out = []
    for members in _graph.clusters(g):
        ent_keys = [m for m in members if nodes[m]["kind"] != "paper"]
        if len(ent_keys) < 2:
            continue
        # binding papers: anchoring >=2 of the cluster's entities
        pc: Counter = Counter()
        for m in ent_keys:
            for pid in nodes[m]["papers"]:
                pc[pid] += 1
        titles = [nodes[f"paper:{pid}"]["label"] for pid, c in pc.most_common()
                  if c >= 2 and f"paper:{pid}" in nodes][:5]
        out.append((_theme_sig(ent_keys), ent_keys, titles, nodes))
    return out


def themes_need_naming(slug: str) -> bool:
    """True iff at least one current cluster has no cached name — drives the
    'Name themes' button (only offered when there's work for it)."""
    cached = _load_theme_names(slug)
    return any(sig not in cached for sig, _e, _t, _n in _cluster_entities(slug))


def name_themes(slug: str) -> dict:
    """One LLM call → names + one-sentence descriptions for every currently-
    unnamed cluster, merged into the themes.json cache. Already-named clusters
    are untouched. Returns ``{named, error}``."""
    clusters = _cluster_entities(slug)
    if not clusters:
        return {"named": 0, "error": "No themes to name yet."}
    cached = _load_theme_names(slug)
    unnamed = [(sig, ekeys, titles, nodes) for sig, ekeys, titles, nodes in clusters
               if sig not in cached]
    if not unnamed:
        return {"named": 0}

    # Build the LLM input: one block per unnamed cluster, referenced by index.
    blocks = []
    for i, (_sig, ekeys, titles, nodes) in enumerate(unnamed):
        ideas = "\n".join(f"  - [{nodes[k]['kind']}] {nodes[k]['label']}" for k in ekeys)
        papers = "\n".join(f"  - {t}" for t in titles) or "  (none)"
        blocks.append(f"CLUSTER ref={i}:\n IDEAS:\n{ideas}\n BINDING PAPERS:\n{papers}")
    user = ("Name each cluster below. Echo each ref exactly.\n\n"
            + "\n\n".join(blocks))

    from . import agent_skills
    system = (agent_skills.skill_body("theme-name")
              or 'Output JSON: {themes:[{ref, name, description}]}.')
    try:
        out = llm.complete([{"role": "system", "content": system},
                            {"role": "user", "content": user}])
        data = _extract_json(out)
    except Exception:  # noqa: BLE001
        return {"named": 0, "error": "The LLM call failed."}

    by_ref = {}
    for t in (data or {}).get("themes", []):
        try:
            ref = int(t.get("ref"))
        except (TypeError, ValueError):
            continue
        name = (t.get("name") or "").strip()[:48]
        desc = (t.get("description") or "").strip()[:200]
        expl = (t.get("explanation") or "").strip()[:600]
        if name:
            by_ref[ref] = {"name": name, "description": desc, "explanation": expl}

    named = 0
    seen_names = {v.get("name", "").lower() for v in cached.values()}
    for i, (sig, _e, _t, _n) in enumerate(unnamed):
        lab = by_ref.get(i)
        if not lab:
            continue
        # keep names distinct
        nm = lab["name"]
        if nm.lower() in seen_names:
            nm = f"{nm} ({i + 1})"
        seen_names.add(nm.lower())
        cached[sig] = {"name": nm, "description": lab["description"],
                       "explanation": lab.get("explanation", ""),
                       "generated_by": "agent", "generator": "theme-name"}
        named += 1

    if named:
        _save_theme_names(slug, cached)
        _append_log(slug, f"named {named} theme(s)", "theme-name")
    return {"named": named}


def rename_theme(slug: str, sig: str, name: str) -> bool:
    """User-edit a theme's name (the hybrid contract: agent suggests, user owns).
    Keyed by the cluster's membership signature; stamps user_named so a future
    name_themes() run won't touch it. Empty name is ignored. NOTE: the name is
    tied to the cluster's exact membership — if the cluster later reshuffles
    (new papers/ideas), its signature changes and this name no longer applies."""
    name = (name or "").strip()[:48]
    if not name:
        return False
    cached = _load_theme_names(slug)
    entry = cached.get(sig, {})
    entry.update({"name": name, "user_named": True})
    entry.setdefault("description", "")
    cached[sig] = entry
    _save_theme_names(slug, cached)
    _append_log(slug, f"renamed theme to “{name}”", "user")
    return True


def connection_view(slug: str) -> dict | None:
    """Render-ready knowledge-graph view for Section 5. Returns:

      {themes, overview, insights, orphans, co_occurrences, paper_themes, graph,
       needs_naming}

    Themes are deterministic clusters decorated with a CACHED LLM name +
    description (or None when unnamed), a computed strength, cohesion, and key
    papers. overview/insights are cheap graph stats (the right-rail dashboard +
    Key Insights). paper_themes maps paper id → the theme indices it sits in (so
    the Papers section can show theme chips + filter). Returns None when the
    graph is too sparse to say anything."""
    from . import graph as _graph
    g = build_collection_graph(slug)
    nodes = g["nodes"]
    if not nodes:
        return None
    label = lambda nid: nodes[nid]["label"] if nid in nodes else nid
    kind = lambda nid: nodes[nid]["kind"] if nid in nodes else ""
    cached_names = _load_theme_names(slug)

    themes = []
    paper_themes: dict[int, list[int]] = defaultdict(list)
    node_theme: dict[str, int] = {}   # node id → theme index (for graph grouping)
    clusters_list = _graph.clusters(g)
    for members in clusters_list:
        member_set = set(members)
        ent_keys = [m for m in members if kind(m) != "paper"]
        if len(ent_keys) < 2:   # a theme is a cluster of related ENTITIES
            continue
        idx = len(themes) + 1   # 1-based, matches render order (size desc)
        ents = [{"label": label(m), "kind": kind(m)} for m in ent_keys]
        # Honest cohesion — what actually binds this cluster, computed:
        #   shared_papers : papers anchoring >=2 of the cluster's entities.
        #   links         : intra-cluster entity<->entity edges (belief->concept).
        paper_count: Counter = Counter()
        for m in ent_keys:
            for pid in nodes[m]["papers"]:
                paper_count[pid] += 1
        # Clusters are entity-only now; a theme's papers = those its ideas touch.
        n_papers = len(paper_count)
        shared_pids = [pid for pid, c in paper_count.most_common() if c >= 2]
        key_papers = [{"id": pid, "title": label(f"paper:{pid}")}
                      for pid in shared_pids if f"paper:{pid}" in nodes][:3]
        seen_pairs: set = set()
        n_links = 0
        for m in ent_keys:
            for nb in g["adj"].get(m, {}):
                if nb in member_set and kind(nb) != "paper":
                    pair = tuple(sorted((m, nb)))
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        n_links += 1
        cohesion = {"shared_papers": len(shared_pids), "links": n_links,
                    "shared_paper_labels": [k["title"] for k in key_papers]}
        sig = _theme_sig(ent_keys)
        nm = cached_names.get(sig) or {}
        themes.append({
            "index": idx, "sig": sig,
            "name": nm.get("name"), "description": nm.get("description"),
            "explanation": nm.get("explanation"), "user_named": nm.get("user_named", False),
            "strength": _theme_strength(cohesion),
            "entities": ents, "n_papers": n_papers, "n_ideas": len(ents),
            "cohesion": cohesion, "key_papers": key_papers})
        # node→theme + paper→themes from this cluster's entity papers
        for m in members:
            node_theme[m] = idx
        for m in ent_keys:
            for pid in nodes[m]["papers"]:
                if idx not in paper_themes[pid]:
                    paper_themes[pid].append(idx)

    name_by_idx = {t["index"]: (t["name"] or f"Theme {t['index']}") for t in themes}
    # entity node-key → its theme (so Section 2's problems/methods can show + jump
    # to their theme; same node keys the graph uses).
    entity_themes = {nid: {"index": idx, "name": name_by_idx.get(idx, f"Theme {idx}")}
                     for nid, idx in node_theme.items() if kind(nid) != "paper"}

    def _papers_of(nid):
        return [{"id": pid, "title": label(f"paper:{pid}")}
                for pid in sorted(nodes[nid]["papers"]) if f"paper:{pid}" in nodes]

    ins = _graph.insights(g)
    orphans = [{"id": int(nid.split(":", 1)[1]), "label": label(nid)}
               for nid in ins["orphans"]]
    # co-occurrences keep their entity keys so the popup can list shared papers.
    co = [{"a": label(a), "b": label(b), "shared": n, "a_key": a, "b_key": b}
          for a, b, n in ins["co_occurrences"][:6]]

    # --- Bridges: entities/papers that touch >=2 distinct themes -------------
    bridges = []
    for nid, theme_ids in ins["bridges"]:
        if nid not in nodes:
            continue
        # ins["bridges"] cluster ids index into ALL clusters; re-derive the
        # distinct THEME indices this node's neighbors sit in (themes only).
        nbr_themes = sorted({node_theme[m] for m in g["adj"].get(nid, {})
                             if m in node_theme})
        if len(nbr_themes) >= 2:
            bridges.append({"label": label(nid), "kind": kind(nid), "node_key": nid,
                            "themes": [name_by_idx.get(i, f"Theme {i}") for i in nbr_themes],
                            "n_themes": len(nbr_themes), "papers": _papers_of(nid)[:6]})
    bridges.sort(key=lambda b: (-b["n_themes"], b["label"]))

    # --- Overview dashboard stats (all cheap, all honest) -------------------
    n_nodes = len(nodes)
    n_ideas = sum(1 for n in nodes.values() if n["kind"] != "paper")
    n_papers_total = n_nodes - n_ideas
    edge_set = set()
    for a, nbrs in g["adj"].items():
        for b in nbrs:
            edge_set.add(tuple(sorted((a, b))))
    n_edges = len(edge_set)
    max_edges = n_nodes * (n_nodes - 1) / 2 if n_nodes > 1 else 0
    density = round(n_edges / max_edges, 2) if max_edges else 0.0
    overview = {"papers": n_papers_total, "ideas": n_ideas, "themes": len(themes),
                "connections": n_edges, "orphans": len(orphans), "density": density}

    # --- Key Insights (call-outs carry real data for the click-to-expand) ----
    strongest = None
    if co:
        top = co[0]
        pids = nodes[top["a_key"]]["papers"] & nodes[top["b_key"]]["papers"]
        strongest = {**top, "papers": [{"id": pid, "title": label(f"paper:{pid}")}
                                       for pid in sorted(pids) if f"paper:{pid}" in nodes]}
    insights_out = {
        "strongest": strongest,
        "bridge": (bridges[0] if bridges else None),
        "orphans": len(orphans),
    }

    # --- Cytoscape payload: IDEAS ONLY (no paper nodes), connected by the idea
    # projection (shared-paper edges). Papers are deliberately excluded — they
    # only dilute the idea structure and, when hidden, leave orphan dots. --------
    viz_nodes = [{"id": nid, "label": n["label"], "kind": n["kind"],
                  "theme": node_theme.get(nid)}
                 for nid, n in nodes.items() if n["kind"] != "paper"]
    viz_edges = _graph.projection_edges(g)

    if not viz_nodes and not orphans:
        return None
    return {"themes": themes, "overview": overview, "insights": insights_out,
            "bridges": bridges, "orphans": orphans, "co_occurrences": co,
            "paper_themes": {pid: idxs for pid, idxs in paper_themes.items()},
            "entity_themes": entity_themes,
            "graph": {"nodes": viz_nodes, "edges": viz_edges},
            "needs_naming": any(t["name"] is None for t in themes)}
