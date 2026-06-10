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
import threading as _threading

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
    # Sanitize lone surrogates (split mathematical-bold PDF glyphs) — they crash .encode.
    h = hashlib.sha256((content or "").encode("utf-8", "ignore")).hexdigest()[:12]
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
        txt = pdf_text.extract_text(pdf_store.pdf_dest(paper_id), max_chars=max_chars)
    except Exception:  # noqa: BLE001
        return ""
    # Strip lone surrogates so the digest (hashed + written) never crashes UTF-8 encode.
    return (txt or "").encode("utf-8", "ignore").decode("utf-8")


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
    # Drop lone UTF-16 surrogates (split mathematical-bold glyphs) — they crash UTF-8
    # encoding downstream (the field-model draft feeding the CLI agent's stdin).
    return _extract_abstract(text).encode("utf-8", "ignore").decode("utf-8")


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
    out, backfill = [], []
    for r in rows:
        ref = r["zotero_key"] or r["arxiv_id"] or r["openreview_id"] or str(r["id"])
        abstract = (r["abstract"] or "").strip()
        if not abstract:
            abstract = _pdf_abstract(r["id"])         # expensive: parses the PDF
            if abstract:
                backfill.append((abstract, r["id"]))  # cache it so we never re-parse
        out.append({"id": r["id"], "ref": ref, "title": r["title"] or "", "abstract": abstract})
    # Persist PDF-extracted abstracts — without this, every wiki render re-parsed every
    # abstract-less PDF (×2: concept map + graph), which made opening a large field-model
    # collection take seconds. One-time per paper.
    if backfill:
        con2 = connect()
        try:
            con2.executemany(
                "UPDATE papers SET abstract=? WHERE id=? AND COALESCE(abstract,'')=''", backfill)
            con2.commit()
        finally:
            con2.close()
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
            nm = ""
            if isinstance(x, dict):
                s = text(x.get("text") or x.get("name"))
                nm = text(x.get("name") or "")
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
            # `name` = short label (≤ ~6 words); `text` = the one-line gist. If the agent
            # gave no distinct name, derive a short one from the gist so the title isn't a
            # full sentence repeated as the quote.
            name = nm if (nm and nm.lower() != s.lower()) else " ".join(s.split()[:6]).rstrip(".,;:")
            out.append({"name": name, "text": s, "papers": pp})
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
_OVERVIEW_MAX_PAPERS = 150       # only bites on very large collections
_OVERVIEW_PDF_CHARS = 2000       # max PDF excerpt per paper (also the fetch cap)
_OVERVIEW_PDF_FLOOR = 700        # min PDF excerpt per paper before falling to abstract-only
_OVERVIEW_ABSTRACT_CHARS = 900
_OVERVIEW_PDF_BUDGET = 80000     # PDF-excerpt chars shared across ALL PDF-equipped papers
_OVERVIEW_TOTAL_BUDGET = 150000  # hard ceiling (huge collections only)


def _overview_digest(papers: list[dict]) -> tuple[str, set[str], set[str]]:
    """Build the LLM input from the collection's papers. EVERY PDF-equipped paper gets a PDF
    excerpt — the PDF budget is split across them, so the per-paper size shrinks as the
    collection grows (capped at _OVERVIEW_PDF_CHARS, floored at _OVERVIEW_PDF_FLOOR); papers
    without a cached PDF are abstract-only. No paper is silently dropped (the count + total
    budget only bite on very large collections). Returns ``(digest, included_refs, pdf_refs)``;
    marked per-paper so the skill knows which cards must leave mechanism/evidence empty."""
    # Researcher-flagged 'core focus' papers sort first (so they survive the budget) and get
    # the FULL excerpt; everyone else shares the remaining PDF budget adaptively.
    ranked = sorted(papers, key=lambda p: (0 if p.get("important") else 1,
                                           0 if p.get("pdf_excerpt") else 1))[:_OVERVIEW_MAX_PAPERS]
    n_imp_pdf = sum(1 for p in ranked if p.get("important") and p.get("pdf_excerpt"))
    n_oth_pdf = sum(1 for p in ranked if not p.get("important") and p.get("pdf_excerpt"))
    rem = max(0, _OVERVIEW_PDF_BUDGET - n_imp_pdf * _OVERVIEW_PDF_CHARS)
    per_other = (max(_OVERVIEW_PDF_FLOOR, min(_OVERVIEW_PDF_CHARS, rem // n_oth_pdf))
                 if n_oth_pdf else _OVERVIEW_PDF_CHARS)
    blocks, used = [], 0
    included, pdf_refs = [], set()
    for p in ranked:
        ab = (p.get("abstract") or "").strip()[:_OVERVIEW_ABSTRACT_CHARS]
        per_pdf = _OVERVIEW_PDF_CHARS if p.get("important") else per_other
        exc = (p.get("pdf_excerpt") or "")[:per_pdf]
        core = "  (CORE — the researcher flagged this as central; orient the model around it)" \
            if p.get("important") else ""
        parts = [f"[{p['ref']}] {p['title']}{core}"]
        if ab:
            parts.append(f"Abstract: {ab}")
        if exc:
            parts.append(f"PDF excerpt:\n{exc}")
            parts.append("(HAS_PDF_EXCERPT — mechanism/evidence/limitation are fair game.)")
        else:
            parts.append("(ABSTRACT_ONLY — no PDF excerpt here; leave mechanism/evidence/limitation empty for this paper.)")
        block = "\n".join(parts)
        if blocks and used + len(block) > _OVERVIEW_TOTAL_BUDGET:
            break                        # truly out of room (very large collections only)
        blocks.append(block)
        used += len(block)
        included.append(p["ref"])
        if exc:
            pdf_refs.add(p["ref"])
    return "\n\n---\n\n".join(blocks), set(included), pdf_refs


# --- Incremental ("Update wiki") helpers -----------------------------------
_FIELD_MODEL_UPDATE_PREAMBLE = (
    "UPDATE MODE. Below is the collection's CURRENT Field Model, followed by the NEW papers "
    "added since it was drafted. Produce the UPDATED Field Model in the SAME JSON shape — do "
    "NOT start over from the new papers alone. Rules:\n"
    "- Keep existing thesis + landscape items unless the new papers genuinely change the "
    "picture; fold new papers into existing problems/methods/debates/open_questions, and add "
    "a new item only when it doesn't fit one. Respect the per-column cap (<=6): if a column is "
    "full, MERGE or REPLACE the weakest item — re-cluster, don't just append.\n"
    "- Preserve concept names already present; add a concept only for a genuinely new idea.\n"
    "- The thesis must still read as one coherent paragraph over the WHOLE collection, not a "
    "changelog of the new papers.")


def _thesis_generated_at(slug: str) -> str:
    """The field model's generated_at as a comparable 'YYYY-MM-DD HH:MM:SS' (or '')."""
    try:
        meta, _ = frontmatter.parse(_thesis_path(slug).read_text(encoding="utf-8"))
    except OSError:
        return ""
    return str(meta.get("generated_at") or "").replace("T", " ")[:19]


def _papers_added_since(slug: str, gen_at: str) -> set:
    """Paper ids added to the collection after the field model was drafted."""
    if not gen_at:
        return set()
    con = connect()
    try:
        rows = con.execute("SELECT paper_id FROM collection_papers WHERE collection_slug=? "
                           "AND added_at > ?", (slug, gen_at)).fetchall()
    finally:
        con.close()
    return {r["paper_id"] for r in rows}


def _field_model_as_text(slug: str) -> str:
    """Compact text of the current Field Model (thesis callouts + landscape columns + concept
    names) — fed back to the agent so an incremental update extends it instead of restarting."""
    parts = ["=== CURRENT FIELD MODEL (update this; don't restart) ==="]
    try:
        _, tb = frontmatter.parse(_thesis_path(slug).read_text(encoding="utf-8"))
        th = _parse_thesis_body(tb)
        if th.get("one_paragraph"):
            parts.append("THESIS: " + th["one_paragraph"])
        for k in ("core_tension", "key_intuition", "central_question"):
            if th.get(k):
                parts.append(f"{k}: {th[k]}")
    except Exception:  # noqa: BLE001
        pass
    ls = _load_landscape(slug)
    for col in ("problems", "methods", "debates", "open_questions"):
        items = ls.get(col) or []
        if not items:
            continue
        labels = [((it.get("name") or it.get("text") or "") if isinstance(it, dict) else str(it))
                  for it in items]
        parts.append(col.upper() + ":\n" + "\n".join(f"- {x}" for x in labels if x))
    try:
        cons = (json.loads(_concepts_path(slug).read_text(encoding="utf-8")).get("concepts") or [])
        names = [c.get("name", "") for c in cons if c.get("name")]
        if names:
            parts.append("CONCEPTS: " + ", ".join(names))
    except Exception:  # noqa: BLE001
        pass
    return "\n\n".join(parts)


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


# --- user-editable concepts (direct edits the user owns; survive regenerate) ---
def _read_concepts(slug: str) -> list[dict]:
    p = _concepts_path(slug)
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("concepts") or []
    except (ValueError, OSError):
        return []


def _cslug(name: str) -> str:
    return _SLUG_RE.sub("-", (name or "").lower()).strip("-")


def user_owned_concepts(slug: str) -> list[dict]:
    """The user's hand-added/edited concepts (captured BEFORE a regenerate wipes
    the sections tree)."""
    return [c for c in _read_concepts(slug) if c.get("user_owned")]


def merge_user_concepts(user_owned: list[dict], agent_concepts: list[dict]) -> list[dict]:
    """Keep the user-owned concepts and add the agent's fresh ones that don't collide
    by name-slug — so manual edits/additions survive a regenerate."""
    user_slugs = {_cslug(c.get("name")) for c in user_owned}
    merged = list(user_owned)
    for c in agent_concepts:
        if _cslug(c.get("name")) not in user_slugs:
            merged.append(c)
    return merged


def _concept_words(c: dict) -> set:
    """Significant (>2-char) words across a concept's name + synonyms, lowercased."""
    toks: set = set()
    for s in [c.get("name", "")] + list(c.get("synonyms") or []):
        toks |= {w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(w) > 2}
    return toks


def _attention_excerpts(slug: str, n_high: int = 20, n_notes: int = 10) -> str:
    """The user's recent highlights + notes as a compact block — so a regenerate can
    make concept synonyms cover the researcher's actual vocabulary (keeps Focus alive)."""
    con = connect()
    try:
        hs = [(r["selected_text"] or "").strip() for r in con.execute(
            "SELECT selected_text FROM annotations WHERE collection_slug=? AND kind='highlight' "
            "AND COALESCE(selected_text,'')<>'' ORDER BY created_at DESC LIMIT ?", (slug, n_high))]
        ns = [" ".join(filter(None, [r["s"], r["t"], r["q"]])).strip() for r in con.execute(
            "SELECT COALESCE(summary,'') s, COALESCE(thoughts,'') t, COALESCE(key_quotes,'') q "
            "FROM paper_notes WHERE collection_slug=? AND (COALESCE(summary,'')<>'' "
            "OR COALESCE(thoughts,'')<>'' OR COALESCE(key_quotes,'')<>'') "
            "ORDER BY updated_at DESC LIMIT ?", (slug, n_notes))]
    finally:
        con.close()
    # Your reasoning thoughts (your externalized understanding) also shape the regen.
    from . import thoughts as _th
    rt = [t["body"].strip() for t in _th.list_thoughts(slug)
          if t.get("synth_kind") == "reasoning" and (t.get("body") or "").strip()][:n_notes]
    lines = ([f"- {h[:240]}" for h in hs if h]
             + [f"- (note) {x[:300]}" for x in ns if x]
             + [f"- (your reasoning) {x[:300]}" for x in rt])
    return "\n".join(lines)


def carry_concept_synonyms(old_concepts: list[dict], new_concepts: list[dict]) -> list[dict]:
    """Carry synonyms from the PREVIOUS concept list into the most word-overlapping new
    concept (mutates + returns new_concepts).

    Regeneration renames concepts ('Token Efficiency' → 'Visual Token Reduction'), and the
    deterministic attention scorer matches highlights/notes against concept *synonyms*. A
    rename that drops the user's vocabulary (e.g. 'patches') silently zeroes their Focus.
    Folding the old synonyms into the best-matching new concept keeps the match alive so
    Focus survives a regen."""
    if not old_concepts or not new_concepts:
        return new_concepts
    new_words = [(c, _concept_words(c)) for c in new_concepts]
    for oc in old_concepts:
        ow = _concept_words(oc)
        if not ow:
            continue
        best, score = None, 0
        for c, nw in new_words:
            ov = len(ow & nw)
            if ov > score:
                best, score = c, ov
        if best is None or score < 1:
            continue
        syns = list(best.get("synonyms") or [])
        have = {s.lower() for s in syns}
        for s in (oc.get("synonyms") or []):
            if s and s.lower() not in have:
                syns.append(s)
                have.add(s.lower())
        best["synonyms"] = syns
    return new_concepts


def add_concept(slug: str, name: str, blurb: str = "") -> dict:
    name = (name or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "Concept needs a name."}
    concepts = _read_concepts(slug)
    if any(_cslug(c.get("name")) == _cslug(name) for c in concepts):
        return {"ok": False, "error": f"“{name}” already exists."}
    concepts.append({"name": name, "synonyms": [], "blurb": (blurb or "").strip(),
                     "papers": [], "user_owned": True})
    _write_concepts_file(slug, concepts)
    _append_log(slug, "added concept (user)", name[:200])
    return {"ok": True}


def edit_concept(slug: str, target: str, name: str, blurb: str = "") -> dict:
    """Rename/re-blurb a concept (by its current name-slug). Marks it user_owned so
    the edit survives the next regenerate. Synonyms/papers are left untouched."""
    name = (name or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "Concept needs a name."}
    concepts = _read_concepts(slug)
    ts = _cslug(target)
    found = next((c for c in concepts if _cslug(c.get("name")) == ts), None)
    if not found:
        return {"ok": False, "error": "Concept not found."}
    if _cslug(name) != ts and any(_cslug(c.get("name")) == _cslug(name) for c in concepts):
        return {"ok": False, "error": f"“{name}” already exists."}
    found["name"], found["blurb"], found["user_owned"] = name, (blurb or "").strip(), True
    _write_concepts_file(slug, concepts)
    _append_log(slug, "edited concept (user)", name[:200])
    return {"ok": True}


def remove_concept(slug: str, target: str) -> dict:
    concepts = _read_concepts(slug)
    ts = _cslug(target)
    kept = [c for c in concepts if _cslug(c.get("name")) != ts]
    if len(kept) == len(concepts):
        return {"ok": False, "error": "Concept not found."}
    _write_concepts_file(slug, kept)
    _append_log(slug, "removed concept (user)", target[:200])
    return {"ok": True}


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
    # A reasoning thought (your argument) is also honest signal to suggest from.
    from . import thoughts as _th
    if any(t.get("synth_kind") == "reasoning" for t in _th.list_thoughts(slug)):
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
    # Your reasoning thoughts (arguments/connections you made) are prime belief material.
    from . import thoughts as _th
    reasoning_thoughts = [t["body"] for t in _th.list_thoughts(slug)
                          if t.get("synth_kind") == "reasoning" and (t.get("body") or "").strip()][:15]

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
        "USER'S REASONING THOUGHTS (their own arguments — strong belief material):\n"
        + ("\n\n---\n\n".join(reasoning_thoughts) or "(none)") + "\n\n"
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


def extract_benchmarks(slug: str, stage_cb=None) -> dict:
    """Per-paper AGENTIC extraction → method × benchmark numbers in
    wiki/sections/benchmarks.json. For each paper with a cached PDF, a tool-using
    agent reads the PDF (paging to its results tables) and reports the numbers —
    far better coverage than a one-shot abstract+intro digest (the numbers live in
    tables deep in the paper). Each row is tagged with its paper, then validated.
    ``stage_cb(done, total)`` reports progress. Returns ``{results, error, papers, failures}``."""
    from . import benchmark_agent, library, pdf_store
    targets = []
    for p in library.list_papers(slug):
        if pdf_store.has_pdf(p["id"]):
            ref = p.get("arxiv_id") or p.get("zotero_key") or str(p["id"])
            targets.append({"id": p["id"], "title": p.get("title", ""), "ref": ref})
    if not targets:
        return {"results": 0, "error": "No cached PDFs to read — add papers with PDFs first."}

    valid_refs = {t["ref"] for t in targets}
    all_rows, failures = [], 0
    for i, t in enumerate(targets):
        if stage_cb:
            stage_cb(i, len(targets))
        try:
            rows = benchmark_agent.extract_paper(slug, t["id"], t["title"])
        except Exception:  # noqa: BLE001 - one paper failing shouldn't sink the run
            logger.warning("benchmark extract failed for paper %s", t["id"])
            failures += 1
            continue
        for r in rows:
            r["paper"] = t["ref"]          # tag with the paper that reported it (for validation)
            all_rows.append(r)
    if stage_cb:
        stage_cb(len(targets), len(targets))

    results = _validate_benchmarks({"results": all_rows}, valid_refs)
    _sections_dir(slug).mkdir(parents=True, exist_ok=True)
    _benchmarks_path(slug).write_text(
        json.dumps({"results": results, "generated_by": "agent",
                    "generator": "benchmark-paper", "generated_at": _now()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    _append_log(slug, f"extracted {len(results)} benchmark result(s) from {len(targets)} paper(s)", "")
    return {"results": len(results), "error": None, "papers": len(targets), "failures": failures}


# --- async benchmark-extract job (per-paper spawns are slow → background + progress) ---
_BENCH_JOBS: dict[str, dict] = {}
_BENCH_LOCK = _threading.Lock()


def get_benchmark_job(slug: str) -> dict | None:
    with _BENCH_LOCK:
        j = _BENCH_JOBS.get(slug)
        return dict(j) if j else None


def clear_benchmark_job(slug: str) -> None:
    with _BENCH_LOCK:
        _BENCH_JOBS.pop(slug, None)


def start_benchmark_async(slug: str) -> bool:
    """Run extract_benchmarks on a daemon thread (per-paper agent spawns); the panel
    overlay polls /wiki/benchmarks/status for done/total progress."""
    existing = get_benchmark_job(slug)
    if existing and existing.get("status") == "running":
        return False
    with _BENCH_LOCK:
        _BENCH_JOBS[slug] = {"status": "running", "done": 0, "total": 0, "error": None}

    def cb(done, total):
        with _BENCH_LOCK:
            j = _BENCH_JOBS.get(slug, {})
            j.update({"done": done, "total": total})
            _BENCH_JOBS[slug] = j

    def runner():
        try:
            res = extract_benchmarks(slug, stage_cb=cb)
            err = res.get("error")
            with _BENCH_LOCK:
                _BENCH_JOBS[slug] = {"status": "failed" if err else "done",
                                     "done": res.get("papers", 0), "total": res.get("papers", 0),
                                     "results": res.get("results", 0), "error": err,
                                     "finished_at": _now()}
            from . import notify
            if err:
                notify.add(f"Benchmark extraction failed ({slug})", f"/c/{slug}?tab=benchmarks", slug, ok=False)
            else:
                notify.add(f"Benchmarks: {res.get('results', 0)} result(s) extracted ({slug})",
                           f"/c/{slug}?tab=benchmarks", slug)
        except Exception as exc:  # noqa: BLE001
            with _BENCH_LOCK:
                _BENCH_JOBS[slug] = {"status": "failed", "error": str(exc), "finished_at": _now()}

    _threading.Thread(target=runner, daemon=True, name=f"bench-{slug}").start()
    return True


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
        # The method's "own" paper = the one reporting most of its cells (link target).
        counts: dict[tuple, int] = {}
        for c in cells:
            if c and c.get("paper"):
                key = (c["paper"]["id"], c["paper"]["title"])
                counts[key] = counts.get(key, 0) + 1
        paper = None
        if counts:
            pid, ptitle = max(counts, key=counts.get)
            paper = {"id": pid, "title": ptitle}
        methods.append({"name": m, "n": n, "cells": cells, "paper": paper})
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
COLLECTION_PURPOSES = ("related", "gaps", "concept", "method", "problem", "thesis", "adjacent", "custom", "similar", "approach")

# Suggested-reading grouping: source → (display order, label template). "" = ungrouped (old).
_READING_SOURCE_ORDER = ["approach", "similar", "custom", "concept", "method", "problem", "related", "thesis", "adjacent", "gaps", ""]


def _reading_group_label(source: str, detail: str) -> str:
    detail = (detail or "").strip()
    if source == "similar":
        return f"Similar to: “{detail}”" if detail else "Similar to a paper"
    if source == "approach":
        return f"Methodology like: “{detail}”" if detail else "Methodology like my idea"
    if source == "custom":
        return f"Custom search: “{detail}”" if detail else "Custom search"
    if source == "concept":
        return f"Concept: {detail}" if detail else "Concept"
    if source == "method":
        return f"Method: {detail}" if detail else "Method"
    if source == "problem":
        return f"Problem: {detail}" if detail else "Problem"
    if source == "related":
        return "Related to this collection"
    if source == "thesis":
        return "Latest on the thesis"
    if source == "adjacent":
        return "Adjacent areas"
    if source == "gaps":
        return "Fills open questions / gaps"
    return "Earlier (ungrouped)"


def _group_reading(cands: list[dict]) -> list[dict]:
    """Group pending suggested-reading candidates by (source, source_detail) for display.
    Returns ordered ``[{source, label, items:[...]}, …]`` (similar/custom first, ungrouped last)."""
    buckets: dict[tuple, dict] = {}
    for c in cands:
        src = (c.get("source") or "")
        detail = (c.get("source_detail") or "")
        key = (src, detail)
        if key not in buckets:
            buckets[key] = {"source": src, "detail": detail,
                            "label": _reading_group_label(src, detail), "cands": []}
        buckets[key]["cands"].append(c)
    return sorted(buckets.values(),
                  key=lambda g: (_READING_SOURCE_ORDER.index(g["source"])
                                 if g["source"] in _READING_SOURCE_ORDER else 99, g["detail"]))


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
    if purpose == "method":
        name = target.strip()
        return (f"{para}\n\nMethod family: {name}".strip(),
                f"use, advance, or compete with the method “{name}”")
    if purpose == "problem":
        name = target.strip()
        return (f"{para}\n\nProblem: {name}".strip(),
                f"address or study the problem “{name}”")
    if purpose == "thesis":
        return (para or _add_seed(slug),
                "represent recent or state-of-the-art work directly relevant to this thesis")
    if purpose == "adjacent":
        return (f"{para}\n\nConcepts: {', '.join(concepts)}".strip(),
                "come from an adjacent area this collection doesn't yet cover but that connects to it")
    if purpose == "similar":
        # Seed from THE paper being read (title + abstract); bias toward collection focus.
        from . import library as _lib
        pid = int(target) if (target and str(target).isdigit()) else None
        p = _lib.get_paper(pid) if pid else None
        title = (p or {}).get("title", "") or ""
        abstract = ((p or {}).get("abstract") or "").strip()
        if not abstract and pid:
            abstract = _pdf_abstract(pid)
        seed = f"Paper: {title}\n\nAbstract: {abstract[:1500]}".strip()
        foc = ", ".join(concepts[:8])
        intent = ("be closely similar in topic and method to the paper above"
                  + (f", and connect to this collection's themes ({foc})" if foc else ""))
        return (seed, intent)
    if purpose == "approach":
        # The user's OWN idea/method is the anchor (FOCUS); intent fixes the match to
        # methodology rather than topic. Don't pull back to the collection — the idea may
        # sit off to the side of it. Empty box falls back to the collection seed.
        idea = custom.strip()
        seed = f"My idea / approach:\n{idea}" if idea else _add_seed(slug)
        intent = ("use a methodology or technical approach closely similar to the idea above — "
                  "match on *how* the work is done (the technique, formulation, or "
                  "training/inference recipe), not merely the application topic")
        return (seed, intent)
    if purpose == "custom":
        return (_add_seed(slug), custom.strip() or "be worth reading next for this collection")
    return (_add_seed(slug), "")        # default: original 'extend/fill gaps' framing


def suggest_papers_to_add(slug: str, purpose: str = "gaps", target: str = "",
                          custom: str = "", deep: bool = False, since: str = "") -> dict:
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
    # how this batch was found → stored on each candidate for grouped display.
    src_detail = ""
    if purpose in ("custom", "approach"):
        src_detail = custom.strip()
    elif purpose in ("concept", "method", "problem"):
        src_detail = target.strip()
    elif purpose == "similar" and target and str(target).isdigit():
        _sp = library.get_paper(int(target))
        src_detail = (_sp or {}).get("title", "") if _sp else ""
    if not seed.strip():
        return {"added": 0, "error": "Draft the Field Model first — there's no focus to search from."}
    try:
        fast_limit = max(1, min(50, int(load_config().get("recommend_count", "15"))))
    except (TypeError, ValueError):
        fast_limit = 15
    # Deep search reads PDFs with a tool-using agent — cast a wider net (~50) since
    # it's the thorough path. Fast (keyword) search returns the configured count (~15).
    limit = 50 if deep else fast_limit
    hist = triage.outcome_history(slug)          # learning: accept/reject memory
    have_titles = {(p.get("title") or "").lower() for p in library.list_papers(slug)}
    have_titles |= {t.lower() for t in hist["accepted_titles"]}   # exclude accepted (hard)
    have_arxiv = {p.get("arxiv_id") for p in library.list_papers(slug) if p.get("arxiv_id")}
    have_arxiv |= hist["accepted_arxiv"]
    pending_arxiv = {c.get("arxiv_id") for c in triage.list_triage(slug, "pending") if c.get("arxiv_id")}
    try:
        if deep:                                  # 🔬 Deep search: tool-using sub-agent
            from . import paper_finder
            cands = paper_finder.deep_find(slug, seed, intent or "the most relevant work for this collection",
                                           limit=limit, since=since)
        else:
            cands = discover.find_related_papers(seed, exclude_titles=have_titles, limit=limit,
                                                 intent=intent, prefer=hist["accepted_titles"],
                                                 avoid=hist["dismissed_titles"], since=since)
        cands = discover.validate_candidates(intent or seed, cands, intent)  # find → verify
        cands = discover.rerank_by_profile(                                   # learn → re-rank
            cands, discover.preference_profile(hist["accepted_titles"], hist["dismissed_titles"]),
            hist["dismissed_arxiv"])
    except Exception as exc:  # noqa: BLE001
        return {"added": 0, "error": f"arXiv discovery failed: {exc}"}
    added = 0
    seen_keys = set(have_arxiv) | set(pending_arxiv)
    for c in cands:
        # Dedupe by arxiv id when present, else DOI (Semantic Scholar non-arXiv papers).
        # A candidate with neither key + no PDF source isn't importable — skip it.
        key = c.get("arxiv_id") or c.get("doi")
        if not key or key in seen_keys:
            continue
        if not (c.get("arxiv_id") or c.get("pdf_url")):
            continue                              # no way to fetch a PDF / import it
        note = c.get("note", "")
        if c.get("verdict") == "pass" and c.get("justification"):
            note = f"{note}  ·  ✓ verified: {c['justification']}"
        elif c.get("verdict") == "weak":
            note = f"{note}  ·  ~ weak match (verify)"
        if c.get("seen_before"):
            note = f"↩ seen before · {note}"
        if c.get("venue"):
            note = f"{note}  ·  {c['venue']}" + (f" · {c['citation_count']} cites" if c.get("citation_count") else "")
        if triage.add_candidate(slug, c, note, source=purpose, source_detail=src_detail):
            seen_keys.add(key)
            added += 1
    _append_log(slug, f"suggested {added} paper(s) [{purpose}]", seed)
    return {"added": added, "error": None}


# --- async suggested-reading job (so Find reading doesn't freeze the panel) ----
import threading as _threading
_READING_JOBS: dict[str, dict] = {}
_READING_LOCK = _threading.Lock()


def get_reading_job(slug: str) -> dict | None:
    with _READING_LOCK:
        j = _READING_JOBS.get(slug)
        return dict(j) if j else None


def clear_reading_job(slug: str) -> None:
    with _READING_LOCK:
        _READING_JOBS.pop(slug, None)


def start_reading_async(slug: str, purpose: str = "related", target: str = "",
                        custom: str = "", deep: bool = False, since: str = "") -> bool:
    """Run suggest_papers_to_add on a daemon thread; the panel overlay polls
    /wiki/reading/status."""
    existing = get_reading_job(slug)
    if existing and existing.get("status") == "running":
        return False
    with _READING_LOCK:
        _READING_JOBS[slug] = {"status": "running", "started_at": _now(), "added": 0, "error": None}

    def runner():
        try:
            res = suggest_papers_to_add(slug, purpose=purpose, target=target,
                                        custom=custom, deep=deep, since=since)
            err = res.get("error")
            with _READING_LOCK:
                _READING_JOBS[slug] = {"status": "failed" if err else "done",
                                       "added": res.get("added", 0),
                                       "error": err, "finished_at": _now()}
            from . import notify
            if err:
                notify.add(f"Suggested reading failed ({slug})", f"/c/{slug}?tab=reading", slug, ok=False)
            else:
                notify.add(f"Suggested reading: {res.get('added', 0)} paper(s) found ({slug})",
                           f"/c/{slug}?tab=reading", slug)
        except Exception as exc:  # noqa: BLE001
            with _READING_LOCK:
                _READING_JOBS[slug] = {"status": "failed", "error": str(exc), "finished_at": _now()}

    _threading.Thread(target=runner, daemon=True, name=f"collread-{slug}").start()
    return True


def generate_overview(slug: str, force: bool = False, stage_cb=None, mode: str = "full") -> bool:
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

    from . import agent_skills
    incremental = mode == "incremental" and _has_field_model(slug)
    if _has_field_model(slug) and not force and not incremental:
        return False
    _skill = (agent_skills.skill_body("field-model")
              or "Output JSON: {thesis:{one_paragraph,core_tension,key_intuition,central_question}, landscape:{problems[],methods[],debates[],open_questions[]}}.")

    # --- Gather + read PDFs (real progress) -----------------------------------
    stage("gathering")
    papers = _collection_abstracts(slug)
    with_abs = [p for p in papers if p["abstract"]]
    from . import library as _lib
    _imp_ids = _lib.important_ids(slug)
    for p in with_abs:
        p["important"] = p["id"] in _imp_ids
    _core_titles = [p["title"] for p in with_abs if p.get("important")]

    if incremental:
        # Fold ONLY papers added since the last draft into the EXISTING model — cheap, and it
        # preserves the structure the agent (and your edits) already shaped. The model still
        # spans the whole collection, so validate against all refs.
        new_ids = _papers_added_since(slug, _thesis_generated_at(slug))
        targets = [p for p in with_abs if p["id"] in new_ids]
        total = len(targets)
        for i, p in enumerate(targets):
            stage("reading_pdfs", pdfs_done=i, pdfs_total=total)
            p["pdf_excerpt"] = _pdf_excerpt(p["id"], max_chars=_OVERVIEW_PDF_CHARS)
        stage("reading_pdfs", pdfs_done=total, pdfs_total=total)
        new_digest, _new_refs, new_pdf_refs = _overview_digest(targets)
        if not new_digest.strip() and not _attention_excerpts(slug):
            return False                          # nothing new to fold in
        valid_refs = set(_ref_map(slug).keys())   # the model may cite ANY paper (all refs)
        con = connect()
        try:                                       # paper_count = live distinct papers
            n_papers = con.execute("SELECT COUNT(*) FROM collection_papers WHERE collection_slug=?",
                                   (slug,)).fetchone()[0]
        finally:
            con.close()
        system = _skill + "\n\n" + _FIELD_MODEL_UPDATE_PREAMBLE
        user_content = (_field_model_as_text(slug)
                        + "\n\n=== NEW PAPERS added since the model above ===\n\n" + new_digest)
        try:                                      # banner: PDFs the model has seen ≈ prior + new
            _pm, _ = frontmatter.parse(_thesis_path(slug).read_text(encoding="utf-8"))
            pdf_read_count = int(_pm.get("pdfs_read") or 0) + len(new_pdf_refs)
        except Exception:  # noqa: BLE001
            pdf_read_count = len(new_pdf_refs)
    else:
        total = len(with_abs)
        for i, p in enumerate(with_abs):
            stage("reading_pdfs", pdfs_done=i, pdfs_total=total)
            p["pdf_excerpt"] = _pdf_excerpt(p["id"], max_chars=_OVERVIEW_PDF_CHARS)
        stage("reading_pdfs", pdfs_done=total, pdfs_total=total)
        digest, included_refs, pdf_refs = _overview_digest(with_abs)
        if not digest.strip():
            return False
        valid_refs = included_refs
        n_papers = len(included_refs)
        system = _skill
        user_content = "Papers:\n\n" + digest
        pdf_read_count = len(pdf_refs)

    # --- One LLM call for the whole Field Model ------------------------------
    stage("drafting", paper_count=n_papers, pdfs_read=pdf_read_count)
    # Fold in the researcher's accepted beliefs so a regenerate ORIENTS the thesis +
    # landscape around the user's stated focus (the wiki is their understanding, papers
    # are evidence). User-owned signal, so it's legitimate to foreground — not invent.
    _accepted = list_accepted_beliefs(slug)
    if _accepted:
        _focus = "\n".join(f"- {b['title']} (confidence: {b.get('confidence', 'emerging')})"
                           for b in _accepted)
        user_content += (
            "\n\n=== RESEARCHER'S CURRENT UNDERSTANDING ===\n"
            "The researcher has ACCEPTED these beliefs about this collection. Treat them as "
            "their stated focus: foreground them in the Thesis (especially core_tension / "
            "key_intuition / central_question) and let them shape which Landscape items lead. "
            "Do not contradict or omit them; do not fabricate beyond the papers:\n" + _focus + "\n")
    # Fold in the researcher's ATTENTION (their highlights/notes) so the concepts cover
    # the vocabulary they actually use — the Focus panel matches highlights against concept
    # synonyms, so a concept must carry the researcher's wording or their Focus goes blank.
    _att = _attention_excerpts(slug)
    if _att:
        user_content += (
            "\n\n=== WHAT THE RESEARCHER HAS BEEN READING (their highlights & notes) ===\n"
            "These are passages they highlighted / wrote. Make sure your concepts COVER these "
            "themes, and include the researcher's own wording (key noun phrases below) in the "
            "relevant concept's `synonyms` so their attention keeps matching:\n" + _att + "\n")
    # Core-focus papers: the researcher flagged these as central, so the whole model should
    # orbit them — the thesis is told to take its center of gravity from them, and the
    # landscape to lead with what they establish.
    if _core_titles:
        user_content += (
            "\n\n=== CORE-FOCUS PAPERS (researcher-flagged — the model must orbit these) ===\n"
            "Treat these as the collection's center of gravity: anchor the Thesis on what they "
            "argue, and let the Landscape lead with the problems/methods THEY establish. Don't "
            "let abstract-only papers outweigh them; don't omit them:\n"
            + "\n".join(f"- {t}" for t in _core_titles) + "\n")
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user_content}]
    # The field-model agent is non-deterministic — a run occasionally returns prose
    # around the JSON, a truncated object, or nothing. Retry once before giving up,
    # and log the raw output so a real failure is debuggable (not just "no output").
    from .config import agent_model
    field = None
    for attempt in (1, 2):
        try:
            out = llm.complete(msgs, model=agent_model())
        except Exception as exc:  # noqa: BLE001
            logger.warning("field-model LLM call failed (attempt %d) for %s: %s", attempt, slug, exc)
            continue
        data = _extract_json(out)
        if not data:
            logger.warning("field-model output didn't parse to JSON (attempt %d) for %s; "
                           "raw len=%d, head=%r", attempt, slug, len(out or ""), (out or "")[:200])
            continue
        cand = _validate_field_model(data, valid_refs=valid_refs)
        # Accept once the thesis paragraph or any landscape column is populated.
        if cand["thesis"]["one_paragraph"] or any(cand["landscape"].values()):
            field = cand
            break
        logger.warning("field-model validated empty (attempt %d) for %s; data keys=%s",
                       attempt, slug, list(data.keys()))
    if field is None:
        return False

    # Incremental: the fed-back model text carries landscape item NAMES but not their paper
    # refs, so the agent can only re-cite the NEW papers — a kept problem/method would lose its
    # old members (concepts survive via synonym match; methods/problems don't). Union the
    # previous landscape's papers back into items kept by name, so a fold-in never drops papers.
    if incremental:
        _old_ls = _load_landscape(slug)

        def _norm_item(it):
            return (((it.get("name") or it.get("text") or "") if isinstance(it, dict)
                     else str(it)).lower().strip())

        for _col in ("problems", "methods"):
            _old_by = {}
            for _it in (_old_ls.get(_col) or []):
                if isinstance(_it, dict) and _it.get("papers"):
                    _old_by.setdefault(_norm_item(_it), set()).update(_it["papers"])
            for _it in (field["landscape"].get(_col) or []):
                if isinstance(_it, dict) and _old_by.get(_norm_item(_it)):
                    _it["papers"] = sorted(set(_it.get("papers") or []) | _old_by[_norm_item(_it)])

    # --- Write the section files atomically ----------------------------------
    # Three files: thesis.md, landscape.md (+ landscape.json), concepts.json.
    stage("writing", pages_done=0, pages_total=3)
    kept_concepts = user_owned_concepts(slug)        # capture BEFORE the wipe
    try:                                             # all old concepts → synonym carry-over
        _old_concepts = (json.loads(_concepts_path(slug).read_text(encoding="utf-8"))
                         .get("concepts") or [])
    except Exception:  # noqa: BLE001
        _old_concepts = []
    _wipe_sections_tree(slug)
    _sections_dir(slug).mkdir(parents=True, exist_ok=True)
    meta_extra = {"paper_count": n_papers,
                  "pdfs_read": pdf_read_count,
                  "pdfs_missing": max(0, n_papers - pdf_read_count)}
    _write_thesis_page(slug, field["thesis"], meta_extra)
    stage("writing", pages_done=1, pages_total=3)
    _write_landscape_page(slug, field["landscape"], meta_extra)
    stage("writing", pages_done=2, pages_total=3)
    # Carry old synonyms forward so a rename doesn't zero the user's Focus, then keep user-owned.
    _carried = carry_concept_synonyms(_old_concepts, field["concepts"])
    _write_concepts_file(slug, merge_user_concepts(kept_concepts, _carried))
    stage("writing", pages_done=3, pages_total=3)
    _append_log(slug, f"{'updated' if incremental else 'generated'} field model "
                       f"({pdf_read_count}/{n_papers} PDFs, "
                       f"{len(field['concepts'])} concepts)", user_content)
    # Name the structural themes now (one extra LLM call, folded into this
    # already-explicit regen) so they're labelled by default — no separate
    # "Name themes" button. Failure here doesn't fail the regen.
    stage("naming")
    try:
        name_themes(slug)
    except Exception:  # noqa: BLE001
        pass
    return True


# ===========================================================================
# Literature review (agent-drafted from YOUR notes/thoughts; you edit + accept).
#
# A full Problems · Methods · Gaps review. The spine is the user's own writing
# (per-paper notes + reasoning thoughts); papers they haven't noted are filled
# from abstracts but explicitly marked "(not yet reviewed by you)" so the doc is
# honest about coverage. It's staged at wiki/review/_draft.md and only becomes
# wiki/review.md when the user accepts (after editing) — never auto-final.
# Lives OUTSIDE wiki/sections/ so a Field-Model regenerate can't touch it.
# ===========================================================================
def _safe_text(s: str) -> str:
    """Drop lone UTF-16 surrogates (split PDF glyphs) so writes never crash."""
    return (s or "").encode("utf-8", "ignore").decode("utf-8")


def _review_path(slug: str) -> Path:
    return _wikidir(slug) / "review.md"


def _review_draft_path(slug: str) -> Path:
    return _wikidir(slug) / "review" / "_draft.md"


def _gather_review_inputs(slug: str) -> dict:
    """Assemble review context: the user's notes + reasoning thoughts (the spine),
    the landscape (problems/methods/open-questions), the concept space, and every
    paper's title+abstract tagged with whether the user has noted it."""
    from . import library
    from . import thoughts as _th
    rmap = _ref_map(slug)
    id_to_ref: dict = {}
    for ref, info in rmap.items():
        pid = info.get("id")
        if pid is not None and pid not in id_to_ref:
            id_to_ref[pid] = ref
    notes_by_paper: dict = {}
    con = connect()
    try:
        for r in con.execute(
            "SELECT paper_id, COALESCE(summary,'') s, COALESCE(thoughts,'') t, "
            "COALESCE(key_quotes,'') q FROM paper_notes WHERE collection_slug=? AND ("
            "COALESCE(summary,'')<>'' OR COALESCE(thoughts,'')<>'' OR COALESCE(key_quotes,'')<>'')",
            (slug,)):
            notes_by_paper[r["paper_id"]] = " ".join(filter(None, [r["s"], r["t"], r["q"]])).strip()
    finally:
        con.close()
    papers = []
    for p in library.list_papers(slug):
        gp = library.get_paper(p["id"]) or {}
        papers.append({
            "ref": id_to_ref.get(p["id"]) or str(p["id"]),
            "title": p.get("title") or "", "year": p.get("year") or "",
            "abstract": _safe_text((gp.get("abstract") or "").strip()),
            "note": notes_by_paper.get(p["id"], ""),
        })
    rthoughts = []
    for t in _th.list_thoughts(slug):
        if t.get("synth_kind") == "reasoning" and (t.get("body") or "").strip():
            pk = (t.get("paper_key") or "")
            ref = id_to_ref.get(int(pk), "") if pk.isdigit() else ""
            rthoughts.append({"body": t["body"].strip(), "ref": ref})
    concepts = []
    if _concepts_path(slug).is_file():
        try:
            concepts = json.loads(_concepts_path(slug).read_text(encoding="utf-8")).get("concepts") or []
        except (ValueError, OSError):
            concepts = []
    return {"papers": papers, "reasoning_thoughts": rthoughts,
            "concepts": concepts, "landscape": _load_landscape(slug)}


def _build_review_prompt(slug: str, ctx: dict) -> str:
    def _bullets(items) -> str:
        out = [(it["text"] if isinstance(it, dict) else str(it)) for it in (items or [])]
        return "\n".join(f"- {x}" for x in out) or "(none)"
    ls = ctx["landscape"] or {}
    noted = [p for p in ctx["papers"] if p["note"]]
    unnoted = [p for p in ctx["papers"] if not p["note"]]
    parts = [
        f"COLLECTION: {slug}",
        f"PAPERS: {len(ctx['papers'])} total — {len(noted)} you have noted, {len(unnoted)} not yet.\n",
        "CONCEPTS:\n" + ("\n".join(f"- {c.get('name','')}: {c.get('blurb','')}"
                                   for c in ctx["concepts"]) or "(none)"),
        "\nLANDSCAPE — PROBLEMS:\n" + _bullets(ls.get("problems")),
        "LANDSCAPE — METHODS:\n" + _bullets(ls.get("methods")),
        "LANDSCAPE — OPEN QUESTIONS / GAPS:\n" + _bullets(ls.get("open_questions")),
        "\nYOUR REASONING THOUGHTS — lead the review with these; they are the spine "
        "and carry the most weight:",
    ]
    for t in ctx["reasoning_thoughts"][:20]:
        parts.append(f"- {t['body']}" + (f" [[{t['ref']}]]" if t["ref"] else ""))
    if not ctx["reasoning_thoughts"]:
        parts.append("- (none yet)")
    parts.append("\nPAPERS YOU HAVE NOTED — use YOUR take as the voice; cite as [[ref]]:")
    for p in noted[:40]:
        parts.append(f"### [[{p['ref']}]] {p['title']} ({p['year']})\n"
                     f"YOUR NOTE: {p['note'][:800]}\nABSTRACT: {p['abstract'][:400]}")
    parts.append("\nPAPERS YOU HAVE NOT NOTED — summarize from the abstract, and you MUST "
                 "mark each as '(not yet reviewed by you)':")
    for p in unnoted[:40]:
        parts.append(f"### [[{p['ref']}]] {p['title']} ({p['year']})\nABSTRACT: {p['abstract'][:400]}")
    return "\n".join(parts)


def suggest_review(slug: str) -> dict:
    """One LLM call → a full literature review (Problems · Methods · Gaps) drafted
    from the user's notes + reasoning thoughts, unnoted papers filled from abstracts
    (marked). Writes wiki/review/_draft.md (status=draft). Returns {ok, error}."""
    ctx = _gather_review_inputs(slug)
    if not ctx["papers"]:
        return {"ok": False, "error": "No papers in this collection yet."}
    from . import agent_skills
    system = (agent_skills.skill_body("literature-review")
              or ("Write a literature review in markdown with `## Problems`, `## Methods`, "
                  "and `## Gaps`. Lead with the user's own takes (their notes/thoughts) as the "
                  "voice; cite papers as [[ref]]. Clearly mark anything summarized only from an "
                  "abstract with '(not yet reviewed by you)'. Do not invent findings."))
    try:
        out = llm.complete([{"role": "system", "content": system},
                            {"role": "user", "content": _build_review_prompt(slug, ctx)}])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"The LLM call failed: {exc}"}
    out = _safe_text(out or "").strip()
    if not out:
        return {"ok": False, "error": "The agent produced no usable output."}
    p = _review_draft_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "review", "status": "draft", "generated_by": "agent",
            "generator": "literature-review", "generated_at": _now()}
    p.write_text(frontmatter.dump(meta, out), encoding="utf-8")
    return {"ok": True}


def accept_review(slug: str, text: str) -> bool:
    """Promote the (possibly edited) draft to wiki/review.md and drop the draft.
    The user owns the result — it's their accepted review now."""
    body = _safe_text(text or "").strip()
    if not body:
        return False
    meta = {"type": "review", "status": "accepted", "generated_by": "agent",
            "generator": "literature-review", "accepted_at": _now()}
    dp = _review_draft_path(slug)
    if dp.is_file():
        try:
            m, _ = frontmatter.parse(dp.read_text(encoding="utf-8"))
            if m.get("generated_at"):
                meta["generated_at"] = m["generated_at"]
        except OSError:
            pass
    ap = _review_path(slug)
    ap.parent.mkdir(parents=True, exist_ok=True)
    ap.write_text(frontmatter.dump(meta, body), encoding="utf-8")
    if dp.is_file():
        dp.unlink()
    return True


def dismiss_review(slug: str) -> bool:
    dp = _review_draft_path(slug)
    if dp.is_file():
        dp.unlink()
        return True
    return False


def load_review(slug: str) -> dict:
    """{accepted_md, draft_md, has_accepted, has_draft, generated_at, accepted_at}."""
    out = {"accepted_md": "", "draft_md": "", "has_accepted": False,
           "has_draft": False, "generated_at": "", "accepted_at": ""}
    ap = _review_path(slug)
    if ap.is_file():
        try:
            m, b = frontmatter.parse(ap.read_text(encoding="utf-8"))
            out["accepted_md"] = b.strip()
            out["has_accepted"] = bool(b.strip())
            out["generated_at"] = m.get("generated_at", "") or ""
            out["accepted_at"] = m.get("accepted_at", "") or ""
        except OSError:
            pass
    dp = _review_draft_path(slug)
    if dp.is_file():
        try:
            m, b = frontmatter.parse(dp.read_text(encoding="utf-8"))
            out["draft_md"] = b.strip()
            out["has_draft"] = bool(b.strip())
            if not out["generated_at"]:
                out["generated_at"] = m.get("generated_at", "") or ""
        except OSError:
            pass
    return out


_REVIEW_JOBS: dict[str, dict] = {}


def get_review_job(slug: str) -> dict | None:
    with _DRAFT_LOCK:
        j = _REVIEW_JOBS.get(slug)
        return dict(j) if j else None


def clear_review_job(slug: str) -> None:
    with _DRAFT_LOCK:
        _REVIEW_JOBS.pop(slug, None)


def start_review_async(slug: str) -> bool:
    """Draft the literature review on a daemon thread (one big LLM call). Returns
    False if a review job is already running for this slug."""
    existing = get_review_job(slug)
    if existing and existing.get("status") == "running":
        return False
    with _DRAFT_LOCK:
        _REVIEW_JOBS[slug] = {"status": "running", "started_at": _now(), "error": None}

    def runner():
        from . import notify
        try:
            res = suggest_review(slug)
            ok = bool(res.get("ok"))
            with _DRAFT_LOCK:
                _REVIEW_JOBS[slug] = {"status": "done" if ok else "failed",
                                      "error": None if ok else res.get("error")}
            notify.add(f"Literature review {'drafted' if ok else 'failed'} ({slug})",
                       f"/c/{slug}?tab=review", slug, ok=ok)
        except Exception as exc:  # noqa: BLE001
            with _DRAFT_LOCK:
                _REVIEW_JOBS[slug] = {"status": "failed", "error": str(exc)}
            notify.add(f"Literature review failed ({slug})", f"/c/{slug}?tab=review", slug, ok=False)

    threading.Thread(target=runner, daemon=True, name=f"review-{slug}").start()
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


def start_draft_async(slug: str, force: bool = True, mode: str = "full") -> bool:
    """Kick off the field-model draft on a daemon thread. ``mode='incremental'`` folds new
    papers/signal into the existing model (cheap, structure-preserving); ``'full'`` rebuilds
    from scratch. Returns True if a job was started, False if one was already running."""
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
            ok = generate_overview(slug, force=force, stage_cb=cb, mode=mode)
            if ok:
                # Refresh per-entity reviews against the regenerated entity set.
                try:
                    generate_entity_reviews(slug)
                except Exception:  # noqa: BLE001 - reviews are best-effort, never block the draft
                    pass
            _set_job(slug, status="done" if ok else "failed",
                     stage="done" if ok else "failed",
                     finished_at=_now(),
                     error=None if ok else "the agent produced no usable output")
            from . import notify
            notify.add(f"Wiki draft {'ready' if ok else 'failed'} ({slug})",
                       f"/c/{slug}?tab=overview", slug, ok=ok)
        except Exception as exc:  # noqa: BLE001 - publish, don't crash the worker
            _set_job(slug, status="failed", stage="failed",
                     finished_at=_now(), error=str(exc))
            from . import notify
            notify.add(f"Wiki draft failed ({slug})", f"/c/{slug}?tab=overview", slug, ok=False)

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
# Thoughts as attention: a 'reasoning' thought is YOUR argument (count like a note);
# a 'seed' is agent text you chose to keep (lighter — count like a highlight).
_THOUGHT_REASONING_WEIGHT = 5
_THOUGHT_SEED_WEIGHT = 1


def _thought_weight(t: dict) -> int:
    return _THOUGHT_REASONING_WEIGHT if t.get("synth_kind") == "reasoning" else _THOUGHT_SEED_WEIGHT


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
    # Anchored thoughts contribute to their paper (reasoning ~ a note, seed ~ a highlight).
    from . import thoughts as _th
    for t in _th.list_thoughts(slug):
        pk = (t.get("paper_key") or "")
        if pk.isdigit():
            scores[int(pk)] += _thought_weight(t)
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
    # Thoughts: a thought mentioning a concept counts toward it (reasoning ×5, seed ×1).
    from . import thoughts as _th
    for t in _th.list_thoughts(slug):
        body = t.get("body") or ""
        if not body:
            continue
        w = _thought_weight(t)
        for slug_, pat in patterns.items():
            if pat.search(body):
                scores[slug_] += w
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
            "important": bool(p.get("important")),
            "tags": paper_tags.get(pid, []),
        })
    # Core-focus papers float to the very top, then attended papers; zeros preserve DB order
    # (title-sorted from library.list_papers).
    papers.sort(key=lambda p: (0 if p["important"] else 1, -p["attention_score"]))

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
    add_groups = _group_reading(add_candidates)

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
        pent_map = connections.get("paper_entities", {})
        for p in papers:
            idxs = ptheme_map.get(p["id"], [])
            p["themes"] = [{"index": i, "name": theme_name.get(i, f"Theme {i}")}
                           for i in idxs]
            # full entity membership (theme:idx / concept|method|problem:slug) for the filter
            p["ent_keys"] = pent_map.get(p["id"], [])
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
            "add_groups": add_groups,
            "belief_candidates": belief_candidates, "beliefs": beliefs,
            "can_suggest_beliefs": can_suggest_beliefs(slug),
            "connections": connections, "concepts": concept_view,
            "meta": meta_out}


# --- knowledge graph: assembly + render view (2026-05-31) ---------------------

def _paper_entity_overrides(slug: str) -> dict:
    """{entity_key: {"add": set(paper_ids), "remove": set(paper_ids)}} of the user's tag edits."""
    con = connect()
    try:
        rows = con.execute("SELECT paper_id, entity_key, action FROM paper_entity_overrides "
                           "WHERE collection_slug=?", (slug,)).fetchall()
    finally:
        con.close()
    out: dict = defaultdict(lambda: {"add": set(), "remove": set()})
    for r in rows:
        out[r["entity_key"]][r["action"]].add(r["paper_id"])
    return out


def toggle_paper_entity(slug: str, paper_id: int, entity_key: str, present: bool) -> None:
    """Toggle a paper's membership of an entity. ``present`` = it currently shows the pill, so
    toggling REMOVES it (and vice-versa). Stores the override; build_collection_graph applies it."""
    action = "remove" if present else "add"
    con = connect()
    try:
        # A new override supersedes any prior one for this (paper, entity).
        con.execute("DELETE FROM paper_entity_overrides WHERE collection_slug=? AND paper_id=? "
                    "AND entity_key=?", (slug, paper_id, entity_key))
        con.execute("INSERT INTO paper_entity_overrides (collection_slug, paper_id, entity_key, "
                    "action) VALUES (?,?,?,?)", (slug, paper_id, entity_key, action))
        con.commit()
    finally:
        con.close()


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
            # label = short name (falls back to text for pre-name collections); gist = the
            # one-line text, shown as the popup quote so title ≠ quote.
            entities.append({"key": key, "kind": kind,
                             "label": item.get("name") or item["text"],
                             "gist": item["text"], "paper_ids": pids})

    # User tag overrides (right-click → Edit tags on a Papers card): add/remove a paper from a
    # concept/method/problem. Applied to the entity → paper_ids sets here so it propagates
    # consistently to the pills, the Papers filter, the entity popups, and the themes.
    _ov = _paper_entity_overrides(slug)
    if _ov:
        for e in entities:
            ov = _ov.get(e["key"])
            if ov:
                e["paper_ids"] = (set(e.get("paper_ids") or set()) | ov["add"]) - ov["remove"]

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
        # key_papers carry `binds` = how many of the theme's entities each anchors
        # ("shared by N" in the popup), sorted by binding strength.
        key_papers = [{"id": pid, "title": label(f"paper:{pid}"), "binds": paper_count[pid]}
                      for pid, c in paper_count.most_common()
                      if c >= 2 and f"paper:{pid}" in nodes][:6]
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

    # --- Entity browser (Map): concept/method/problem cards (paper-anchored only).
    # Concepts carry blurb + attention score; problems/methods are read-only text. --
    _cmeta: dict = {}
    try:
        if _concepts_path(slug).is_file():
            _cl = (json.loads(_concepts_path(slug).read_text(encoding="utf-8")).get("concepts") or [])
            _cs = attention_per_concept(slug, _cl)
            for c in _cl:
                _cmeta[c["slug"]] = {"blurb": c.get("blurb", ""), "score": _cs.get(c["slug"], 0),
                                     "name": c.get("name", c["slug"])}
    except (ValueError, OSError):
        pass
    _pattn = attention_scores(slug)   # per-paper attention, to roll up onto method/problem cards

    def _entity_card(nid):
        k = kind(nid)
        rel = [{"label": label(r), "kind": kind(r)}
               for r, _w in _graph.related(g, nid, k=5) if kind(r) != "paper"]
        card = {"key": nid, "label": label(nid), "kind": k,
                "gist": nodes[nid].get("gist", ""),
                "paper_count": len(nodes[nid]["papers"]),
                "papers": _papers_of(nid)[:6], "related": rel,
                "theme": entity_themes.get(nid)}
        if k == "concept":
            cslug = nid.split(":", 1)[1]
            m = _cmeta.get(cslug, {})
            card.update(blurb=m.get("blurb", ""), score=m.get("score", 0),
                        concept_slug=cslug, name=m.get("name", card["label"]))
        else:
            # method/problem attention = sum of their papers' attention (highlights/notes/thoughts)
            card["score"] = sum(_pattn.get(pid, 0) for pid in nodes[nid]["papers"])
        return card

    entities = {"concept": [], "method": [], "problem": []}
    for _nid, _n in nodes.items():
        if _n["kind"] in entities:
            entities[_n["kind"]].append(_entity_card(_nid))
    entities["concept"].sort(key=lambda e: (-e.get("score", 0), e["label"].lower()))
    for _k in ("method", "problem"):
        entities[_k].sort(key=lambda e: (-e["paper_count"], e["label"].lower()))

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
                "connections": n_edges, "orphans": len(orphans), "density": density,
                "concepts": len(entities["concept"]), "methods": len(entities["method"]),
                "problems": len(entities["problem"])}

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

    # paper_id → every entity key it belongs to (theme:idx / concept|method|problem:slug),
    # for the Papers-tab filter (slice by theme/concept/method/problem).
    paper_entities: dict = defaultdict(list)
    for _nid, _n in nodes.items():
        if _n["kind"] == "paper":
            continue
        for pid in _n["papers"]:
            paper_entities[pid].append(_nid)
    for pid, idxs in paper_themes.items():
        for i in idxs:
            paper_entities[pid].append(f"theme:{i}")

    if not viz_nodes and not orphans:
        return None
    return {"themes": themes, "overview": overview, "insights": insights_out,
            "bridges": bridges, "orphans": orphans, "co_occurrences": co,
            "paper_themes": {pid: idxs for pid, idxs in paper_themes.items()},
            "entity_themes": entity_themes, "entities": entities,
            "paper_entities": {pid: keys for pid, keys in paper_entities.items()},
            "graph": {"nodes": viz_nodes, "edges": viz_edges},
            "needs_naming": any(t["name"] is None for t in themes)}


def theme_detail(slug: str, sig: str) -> dict | None:
    """The full theme record (entities, binding papers, explanation, cohesion) for the
    Map's theme popup — looked up by sig from the live connection view."""
    cx = connection_view(slug)
    if not cx:
        return None
    for t in cx.get("themes", []):
        if t.get("sig") == sig:
            return t
    return None


def entity_detail(slug: str, key: str) -> dict | None:
    """Rich detail for one Map entity (concept/method/problem): description, top papers,
    related entities. Concepts include blurb + attention score (editable in the popup)."""
    from . import graph as _graph
    try:
        g = build_collection_graph(slug)
    except Exception:  # noqa: BLE001
        return None
    nodes = g.get("nodes", {})
    if key not in nodes:
        return None
    n = nodes[key]
    k = n["kind"]
    lbl = lambda nid: nodes[nid]["label"] if nid in nodes else nid
    papers = [{"id": pid, "title": lbl(f"paper:{pid}")}
              for pid in sorted(n["papers"]) if f"paper:{pid}" in nodes]
    related = [{"label": lbl(r), "kind": nodes[r]["kind"], "key": r}
               for r, _w in _graph.related(g, key, k=8) if nodes.get(r, {}).get("kind") != "paper"]
    out = {"key": key, "kind": k, "label": n["label"], "gist": n.get("gist", ""),
           "papers": papers, "related": related, "blurb": "", "score": 0, "concept_slug": "",
           "review": load_entity_reviews(slug).get(key, "")}
    if k == "concept":
        cslug = key.split(":", 1)[1]
        out["concept_slug"] = cslug
        try:
            if _concepts_path(slug).is_file():
                cl = json.loads(_concepts_path(slug).read_text(encoding="utf-8")).get("concepts") or []
                cs = attention_per_concept(slug, cl)
                for c in cl:
                    if c["slug"] == cslug:
                        out["blurb"] = c.get("blurb", "")
                        out["label"] = c.get("name", out["label"])
                        out["score"] = cs.get(cslug, 0)
                        break
        except (ValueError, OSError):
            pass
    return out


# ===========================================================================
# Per-entity literature reviews — a short paper-grounded write-up for each
# concept/method/problem, surfaced inside the Map detail popups. One LLM pass
# over all entities (entities-as-sections, bounded cost); unnoted entities are
# summarized from abstracts and marked. Generated from the ⋯ menu and folded
# into a Field-Model regenerate. Augments (does not replace) the Review tab.
# ===========================================================================
def _entity_reviews_path(slug: str) -> Path:
    return _wikidir(slug) / "entity_reviews.json"


def update_available(slug: str) -> bool:
    """True when the field model is stale: either there's attention signal (a highlight or
    note) newer than it, OR papers were added since it was drafted (live count > the
    paper_count it was generated from). Drives the 'Update wiki' button label."""
    if not _thesis_path(slug).is_file():
        return False
    try:
        meta, _ = frontmatter.parse(_thesis_path(slug).read_text(encoding="utf-8"))
    except OSError:
        return False
    gen = str(meta.get("generated_at") or "").replace("T", " ")[:19]
    if not gen:
        return False
    con = connect()
    try:
        a = con.execute("SELECT MAX(created_at) FROM annotations WHERE collection_slug=?",
                        (slug,)).fetchone()[0]
        n = con.execute("SELECT MAX(updated_at) FROM paper_notes WHERE collection_slug=?",
                        (slug,)).fetchone()[0]
        live_papers = con.execute("SELECT COUNT(*) FROM collection_papers WHERE collection_slug=?",
                                  (slug,)).fetchone()[0]
    finally:
        con.close()
    try:
        drafted_from = int(meta.get("paper_count") or 0)
    except (TypeError, ValueError):
        drafted_from = 0
    if drafted_from and live_papers > drafted_from:   # papers added since the last draft
        return True
    # Normalize separators: notes store 'YYYY-MM-DDTHH:MM:SS' (T) while annotations + `gen`
    # use a space — and 'T' > ' ' in ASCII, so a naive string compare reads any note as newer.
    latest = max([str(x)[:19].replace("T", " ") for x in (a, n) if x] or [""])
    return bool(latest) and latest > gen


def load_entity_reviews(slug: str) -> dict:
    """{entity_key: review_markdown}."""
    p = _entity_reviews_path(slug)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("reviews", {}) or {}
    except (ValueError, OSError):
        return {}


def generate_entity_reviews(slug: str) -> dict:
    """One LLM pass → a 2-4 sentence literature review per concept/method/problem,
    grounded in the user's notes where present and paper abstracts otherwise (marked
    '(not yet reviewed by you)'). Writes wiki/entity_reviews.json. Returns {ok, generated}."""
    from . import library, agent_skills
    try:
        g = build_collection_graph(slug)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    nodes = g.get("nodes", {})
    ents = [(k, n) for k, n in nodes.items() if n["kind"] in ("concept", "method", "problem")]
    if not ents:
        return {"ok": False, "error": "No entities to review yet."}
    # per-paper notes (the user's own writing — the spine)
    notes_by_paper: dict = {}
    con = connect()
    try:
        for r in con.execute(
            "SELECT paper_id, COALESCE(summary,'') s, COALESCE(thoughts,'') t, "
            "COALESCE(key_quotes,'') q FROM paper_notes WHERE collection_slug=? AND ("
            "COALESCE(summary,'')<>'' OR COALESCE(thoughts,'')<>'' OR COALESCE(key_quotes,'')<>'')",
            (slug,)):
            notes_by_paper[r["paper_id"]] = " ".join(filter(None, [r["s"], r["t"], r["q"]])).strip()
    finally:
        con.close()
    # index keys (E0, E1…) keep the LLM from mangling colon-keys; mapped back after.
    idx_to_key: dict = {}
    blocks = []
    for i, (k, n) in enumerate(ents):
        eid = f"E{i}"
        idx_to_key[eid] = k
        plines = []
        for pid in sorted(n["papers"])[:8]:
            gp = library.get_paper(pid) or {}
            note = notes_by_paper.get(pid, "")
            line = f"- {gp.get('title','')}: {_safe_text(gp.get('abstract','') or '')[:300]}"
            if note:
                line += f"  [MY NOTE: {note[:200]}]"
            plines.append(line)
        blocks.append(f"### {eid} | {n['kind']} | {n['label']}\n" + ("\n".join(plines) or "(no papers)"))
    user = ("Write a 2-4 sentence literature review for EACH entity below, keyed by its id "
            "(E0, E1, …). Synthesize what the papers collectively say about that entity; lead "
            "with MY NOTE where present. If an entity has no note, summarize from the abstracts "
            "and append '(not yet reviewed by you)'. Do not invent findings.\n\n"
            + "\n\n".join(blocks))
    system = (agent_skills.skill_body("entity-review")
              or 'Output STRICT JSON only: {"reviews": {"E0": "markdown…", "E1": "…"}}. '
                 "Each value is a 2-4 sentence markdown literature review of that entity.")
    try:
        out = llm.complete([{"role": "system", "content": system},
                            {"role": "user", "content": user}])
        data = _extract_json(out)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"The LLM call failed: {exc}"}
    reviews: dict = {}
    for eid, md in (data.get("reviews") or {}).items():
        key = idx_to_key.get(eid)
        if key and isinstance(md, str) and md.strip():
            reviews[key] = _safe_text(md.strip())
    if not reviews:
        return {"ok": False, "error": "The agent produced no usable reviews."}
    p = _entity_reviews_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"reviews": reviews,
                             "_meta": {"generated_by": "agent", "generator": "entity-review",
                                       "generated_at": _now()}}, indent=2), encoding="utf-8")
    return {"ok": True, "generated": len(reviews)}


_ENTREV_JOBS: dict[str, dict] = {}


def get_entity_reviews_job(slug: str) -> dict | None:
    with _DRAFT_LOCK:
        j = _ENTREV_JOBS.get(slug)
        return dict(j) if j else None


def clear_entity_reviews_job(slug: str) -> None:
    with _DRAFT_LOCK:
        _ENTREV_JOBS.pop(slug, None)


def start_entity_reviews_async(slug: str) -> bool:
    """Generate per-entity reviews on a daemon thread (one LLM pass). False if running."""
    existing = get_entity_reviews_job(slug)
    if existing and existing.get("status") == "running":
        return False
    with _DRAFT_LOCK:
        _ENTREV_JOBS[slug] = {"status": "running", "started_at": _now(), "error": None}

    def runner():
        from . import notify
        try:
            res = generate_entity_reviews(slug)
            ok = bool(res.get("ok"))
            with _DRAFT_LOCK:
                _ENTREV_JOBS[slug] = {"status": "done" if ok else "failed",
                                      "error": None if ok else res.get("error")}
            notify.add(f"Entity reviews {'ready' if ok else 'failed'} ({slug})",
                       f"/c/{slug}?tab=connections", slug, ok=ok)
        except Exception as exc:  # noqa: BLE001
            with _DRAFT_LOCK:
                _ENTREV_JOBS[slug] = {"status": "failed", "error": str(exc)}
            notify.add(f"Entity reviews failed ({slug})", f"/c/{slug}?tab=connections", slug, ok=False)

    threading.Thread(target=runner, daemon=True, name=f"entrev-{slug}").start()
    return True
