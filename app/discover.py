"""Gap detection and stale-paper flagging (CLAUDE.md Phase 8).

- ``find_gaps``: LLM reads the wiki + recent arXiv results and proposes papers
  that would fill stated gaps. The user can send any to the triage queue.
- ``find_stale``: cheap, no LLM. Flags papers with zero appearances in
  notes/thoughts/wiki after 90 days. NEVER removes anything — the user decides.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from . import library, llm, openreview
from .config import COLLECTIONS_DIR
from .wiki import _read, _wikidir

logger = logging.getLogger("paper_agent.discover")

ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"


# --- gaps ------------------------------------------------------------------
def _arxiv_search(query: str, max_results: int = 10) -> list[dict]:
    try:
        r = httpx.get(
            ARXIV_API,
            params={"search_query": query, "max_results": max_results,
                    "sortBy": "submittedDate", "sortOrder": "descending"},
            timeout=20.0,
            follow_redirects=True,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("arxiv search failed: %s", exc)
        return []
    root = ET.fromstring(r.text)
    out = []
    for entry in root.findall(f"{ATOM}entry"):
        idtext = (entry.findtext(f"{ATOM}id") or "").strip()
        m = re.search(r"abs/([^v]+)(?:v\d+)?$", idtext)
        out.append(
            {
                "arxiv_id": m.group(1) if m else idtext,
                "title": (entry.findtext(f"{ATOM}title") or "").strip(),
                "summary": (entry.findtext(f"{ATOM}summary") or "").strip(),
            }
        )
    return out


# id forms we accept: bare "2401.12345", versioned "2401.12345v2", an abs/pdf URL,
# an "arXiv:" prefix, or an old-style "cs.LG/0501001" identifier.
_ARXIV_ID_RE = re.compile(
    r"(\d{4}\.\d{4,5})(?:v\d+)?|([a-z\-]+(?:\.[A-Za-z]{2})?/\d{7})(?:v\d+)?", re.IGNORECASE
)


def normalize_arxiv_id(raw: str) -> str:
    """Extract a canonical (version-stripped) arXiv id from an id, URL, or prefix.
    Returns "" if nothing arXiv-shaped is found."""
    m = _ARXIV_ID_RE.search((raw or "").strip())
    return (m.group(1) or m.group(2)) if m else ""


def parse_add_input(raw: str) -> list[dict]:
    """Parse a chunk of arXiv / OpenReview URLs (newline- or comma-separated) into entries
    with fetched metadata. Each entry: {input, kind, id, title, authors, year, ok, error}.
    Unparseable or unfetchable tokens come back with ok=False so the wizard can flag them."""
    tokens, seen, out = [t.strip() for t in re.split(r"[\n,]+", raw or "")], set(), []
    for tok in tokens:
        if not tok or tok in seen:
            continue
        seen.add(tok)
        aid = normalize_arxiv_id(tok)
        if aid:
            meta = fetch_arxiv_metadata(tok)
            out.append({"input": tok, "kind": "arxiv", "id": aid,
                        "title": meta["title"] if meta else None,
                        "authors": meta["authors"] if meta else "",
                        "year": meta["year"] if meta else "",
                        "abstract": meta["abstract"] if meta else "",
                        "ok": bool(meta), "error": None if meta else "arXiv lookup failed"})
            continue
        oid = openreview.extract_id(tok)
        if oid:
            meta = openreview.fetch_metadata(oid)
            out.append({"input": tok, "kind": "openreview", "id": oid,
                        "title": meta["title"] if meta else None,
                        "authors": meta["authors"] if meta else "",
                        "year": meta["year"] if meta else "",
                        "abstract": meta["abstract"] if meta else "",
                        "ok": bool(meta), "error": None if meta else "OpenReview lookup failed"})
            continue
        out.append({"input": tok, "kind": None, "id": None, "title": None, "authors": "",
                    "year": "", "abstract": "", "ok": False, "error": "Not an arXiv or OpenReview URL"})
    return out


def fetch_arxiv_metadata(raw_id: str) -> dict | None:
    """Look up a single arXiv paper's metadata by id/URL. Returns
    {arxiv_id, title, authors, year, abstract} or None (bad id / network error)."""
    aid = normalize_arxiv_id(raw_id)
    if not aid:
        return None
    try:
        r = httpx.get(ARXIV_API, params={"id_list": aid, "max_results": 1},
                      timeout=20.0, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("arxiv metadata fetch failed for %s: %s", aid, exc)
        return None
    entry = ET.fromstring(r.text).find(f"{ATOM}entry")
    if entry is None or entry.find(f"{ATOM}id") is None:
        return None
    return _parse_entry(entry, aid)


def _parse_entry(entry, fallback_id: str = "") -> dict:
    """Turn one Atom <entry> into our metadata dict."""
    title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
    abstract = " ".join((entry.findtext(f"{ATOM}summary") or "").split())
    authors = ", ".join((a.findtext(f"{ATOM}name") or "").strip()
                        for a in entry.findall(f"{ATOM}author"))
    published = (entry.findtext(f"{ATOM}published") or "").strip()
    idtext = (entry.findtext(f"{ATOM}id") or "").strip()
    m = re.search(r"abs/(.+?)(?:v\d+)?$", idtext)
    return {
        "arxiv_id": (m.group(1) if m else fallback_id),
        "title": title or "(untitled)",
        "authors": authors,
        "year": published[:4],
        "abstract": abstract,
    }


def fetch_arxiv_batch(raw_ids: list[str]) -> dict[str, dict]:
    """Fetch metadata for many arXiv ids in one request (chunked). Returns a dict keyed by the
    version-stripped id. Best-effort: unresolved ids are simply absent."""
    ids = []
    for r in raw_ids:
        a = normalize_arxiv_id(r)
        if a and a not in ids:
            ids.append(a)
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            resp = httpx.get(ARXIV_API, params={"id_list": ",".join(chunk), "max_results": len(chunk)},
                             timeout=30.0, follow_redirects=True)
            resp.raise_for_status()
            for entry in ET.fromstring(resp.text).findall(f"{ATOM}entry"):
                if entry.find(f"{ATOM}id") is None:
                    continue
                meta = _parse_entry(entry)
                if meta["arxiv_id"]:
                    out[meta["arxiv_id"]] = meta
        except (httpx.HTTPError, ET.ParseError) as exc:
            logger.warning("arxiv batch fetch failed for %s: %s", chunk, exc)
    return out


def find_gaps(slug: str) -> list[dict]:
    """Propose arXiv papers that could fill the wiki's stated gaps."""
    col = library.get_collection(slug) or {}
    purpose = _read(COLLECTIONS_DIR / slug / "purpose.md") or col.get("purpose", "")
    if col.get("summary"):
        purpose = f"{purpose}\n\nSummary: {col['summary']}".strip()
    gaps_dir = _wikidir(slug) / "gaps"
    gaps_text = ""
    if gaps_dir.is_dir():
        gaps_text = "\n\n".join(p.read_text(encoding="utf-8") for p in gaps_dir.glob("*.md"))

    # Step 1: an arXiv query string grounded in purpose + gaps.
    query_resp = llm.complete(
        [
            {"role": "system", "content": "Output only a short arXiv search query string."},
            {
                "role": "user",
                "content": f"PURPOSE:\n{purpose}\n\nGAPS:\n{gaps_text}\n\n"
                "Give a concise arXiv search query (keywords) for recent papers "
                "that might fill these gaps.",
            },
        ]
    ).strip().strip('"')
    query = f"all:{query_resp}" if ":" not in query_resp else query_resp

    results = _arxiv_search(query)
    if not results:
        return []

    # Step 2: LLM picks which results address the gaps, with a one-line note.
    listing = "\n".join(
        f"{i}. [{r['arxiv_id']}] {r['title']}: {r['summary'][:300]}"
        for i, r in enumerate(results)
    )
    pick = llm.complete(
        [
            {"role": "system", "content": "You output only valid JSON."},
            {
                "role": "user",
                "content": (
                    f"GAPS:\n{gaps_text or purpose}\n\nCANDIDATES:\n{listing}\n\n"
                    'Which candidates would help fill the gaps? Respond JSON: '
                    '{"picks": [{"index": 0, "note": "why it helps"}]}'
                ),
            },
        ]
    )
    import json

    try:
        data = json.loads(pick[pick.find("{") : pick.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    out = []
    for p in data.get("picks", []):
        idx = p.get("index")
        if isinstance(idx, int) and 0 <= idx < len(results):
            out.append({**results[idx], "note": p.get("note", "")})
    return out


def find_related_papers(seed: str, exclude_titles: set[str] | None = None) -> list[dict]:
    """Recommend arXiv papers to ADD to a collection, seeded by a free-text
    description of what the collection is about (the cognitive-model field
    model — thesis + concepts + open questions). Two LLM steps: build an arXiv
    query from the seed, then pick the most relevant results with a one-line
    'why add it' note. Results already in the collection (by title) are dropped.

    Returns ``[{arxiv_id, title, summary, authors, note}]``. Network action
    (arXiv) — callers gate it behind an explicit user click."""
    seed = (seed or "").strip()
    if not seed:
        return []
    exclude = {t.lower() for t in (exclude_titles or set())}
    try:
        query_resp = llm.complete([
            {"role": "system", "content": "Output only a short arXiv search query string."},
            {"role": "user", "content": f"COLLECTION FOCUS:\n{seed}\n\n"
             "Give a concise arXiv search query (keywords) for recent papers a "
             "researcher building this collection would want to read or cite next."},
        ]).strip().strip('"')
    except Exception:  # noqa: BLE001
        return []
    query = f"all:{query_resp}" if ":" not in query_resp else query_resp
    results = [r for r in _arxiv_search(query) if r["title"].lower() not in exclude]
    if not results:
        return []
    listing = "\n".join(f"{i}. [{r['arxiv_id']}] {r['title']}: {r['summary'][:300]}"
                        for i, r in enumerate(results))
    try:
        pick = llm.complete([
            {"role": "system", "content": "You output only valid JSON."},
            {"role": "user", "content": (
                f"COLLECTION FOCUS:\n{seed}\n\nCANDIDATES:\n{listing}\n\n"
                'Which candidates best extend or fill gaps in this collection? '
                'Respond JSON: {"picks": [{"index": 0, "note": "why add it"}]}')},
        ])
        import json
        data = json.loads(pick[pick.find("{"): pick.rfind("}") + 1])
    except (ValueError, Exception):  # noqa: BLE001
        return []
    out = []
    for p in data.get("picks", []):
        idx = p.get("index")
        if isinstance(idx, int) and 0 <= idx < len(results):
            out.append({**results[idx], "note": p.get("note", "")})
    return out


# --- stale papers ----------------------------------------------------------
def _appearance_count(slug: str, key: str) -> int:
    base = COLLECTIONS_DIR / slug
    count = 0
    for sub in ("notes", "thoughts", "thoughts-archive", "wiki"):
        d = base / sub
        if not d.is_dir():
            continue
        for f in d.rglob("*.md"):
            try:
                if key in f.read_text(encoding="utf-8") or f.stem == key:
                    count += 1
            except OSError:
                continue
    return count


def find_stale(slug: str, days: int = 90) -> list[dict]:
    """Papers with zero appearances in the user's writing after `days`. Reads the
    local store (no live Zotero); `added_at` is the local import/creation time."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stale = []
    for p in library.list_papers(slug):
        pid = p["id"]
        if _appearance_count(slug, str(pid)) > 0:
            continue
        full = library.get_paper(pid) or {}
        added = (full.get("added_at") or "").replace("T", " ")[:19]
        try:
            added_dt = datetime.strptime(added, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, TypeError):
            added_dt = None
        if added_dt is None or added_dt < cutoff:
            stale.append({"id": pid, "key": pid, "title": p["title"], "date_added": added})
    return stale
