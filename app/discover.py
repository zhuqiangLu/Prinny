"""Gap detection and stale-paper flagging (CLAUDE.md Phase 8).

- ``find_gaps``: LLM reads the wiki + recent arXiv results and proposes papers
  that would fill stated gaps. The user can send any to the triage queue.
- ``find_stale``: cheap, no LLM. Flags papers with zero appearances in
  notes/thoughts/wiki after 90 days. NEVER removes anything — the user decides.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from . import library, llm, openreview
from .config import COLLECTIONS_DIR
from .wiki import _read, _wikidir

logger = logging.getLogger("paper_agent.discover")

ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
# arXiv rate-limits/serves 429 to requests without a descriptive User-Agent and
# asks callers to back off between requests — set one and retry transient failures.
_HEADERS = {"User-Agent": "paper-agent/0.1 (local research wiki; +https://arxiv.org)"}


class ArxivError(RuntimeError):
    """arXiv was unreachable or kept failing (e.g. 429) after retries."""


# Shown whenever a source rate-limits (HTTP 429). On a shared/NAT network (e.g. a
# university Wi-Fi where everyone exits through one public IP) the provider throttles
# the WHOLE network, not just you — so it can 429 even on your first request.
_RATE_LIMIT_HINT = (
    "arXiv is rate-limiting this network (HTTP 429). On shared/campus Wi-Fi everyone "
    "shares one public IP, so arXiv throttles the whole network — not just you. Fixes: "
    "try your own network (home Wi-Fi, phone hotspot, or a VPN), or add a free "
    "Semantic Scholar API key in Settings (it gives you a personal quota that works "
    "even behind the shared IP)."
)


# Process-wide min-interval throttle: arXiv asks for ≤ ~1 request / 3s and 429s +
# tarpits the IP on bursts. Serialize all arXiv access so no path (the deep loop,
# the agent's search tool, or rapid clicks) can ever burst again. Tests can set
# _MIN_INTERVAL = 0 to skip the wait.
_MIN_INTERVAL = 3.0
_RATE_LOCK = threading.Lock()
_last_call = [0.0]


def _throttle() -> None:
    with _RATE_LOCK:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _arxiv_get(params: dict, *, timeout: float = 10.0, retries: int = 2):
    """GET the arXiv API with a real User-Agent, a global min-interval throttle, and
    backoff retry on TRANSIENT failures (timeout / 5xx / network). A 429 fails fast
    (retrying within seconds is futile and only adds load). Raises ArxivError."""
    last = None
    backoff = 2.0
    for attempt in range(retries):
        if attempt:
            time.sleep(min(backoff, 10.0))
            backoff *= 2
        _throttle()
        try:
            r = httpx.get(ARXIV_API, params=params, headers=_HEADERS,
                          timeout=timeout, follow_redirects=True)
            if getattr(r, "status_code", 200) == 429:
                # Fail fast — a 429 won't clear in seconds, so don't retry/hammer.
                raise ArxivError(_RATE_LIMIT_HINT)
            r.raise_for_status()
            return r
        except ArxivError:
            raise
        except httpx.HTTPError as exc:
            last = exc
            logger.warning("arxiv request failed (attempt %d/%d): %s", attempt + 1, retries, exc)
    raise ArxivError(f"arXiv unreachable: {last}")


# --- website (HTML) fallback ------------------------------------------------
# The arXiv API (export.arxiv.org) is rate-limited far more aggressively than the
# website, and on shared/institutional IPs the API 429s while arxiv.org itself
# stays 200. When the API fails we read the same data off the website pages:
# arxiv.org/search (results carry id + title + abstract) and arxiv.org/abs/<id>
# (citation meta tags). HTML is more fragile than the Atom API, so it's a FALLBACK
# only — the clean API stays primary wherever it works.
ARXIV_WEB = "https://arxiv.org"
_HTML_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; paper-agent/0.1; local research wiki)"}


def _strip_html(s: str) -> str:
    import html as _h
    return " ".join(_h.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def _web_get(url: str, params: dict | None = None, timeout: float = 30.0, retries: int = 3):
    """GET an arXiv website page (throttled, browser UA). The search page is large
    (~260KB) and can be slow, so use a generous timeout and retry transient
    failures. Raises ArxivError once retries are exhausted."""
    last = None
    backoff = 2.0
    for attempt in range(retries):
        if attempt:
            time.sleep(min(backoff, 8.0))
            backoff *= 2
        _throttle()
        try:
            r = httpx.get(url, params=params, headers=_HTML_HEADERS,
                          timeout=timeout, follow_redirects=True)
            if getattr(r, "status_code", 200) == 429:
                raise ArxivError(_RATE_LIMIT_HINT)        # NAT/shared-IP throttle — explain it
            r.raise_for_status()
            return r
        except ArxivError:
            raise
        except httpx.HTTPError as exc:
            last = exc
            logger.warning("arxiv website request failed (attempt %d/%d): %s", attempt + 1, retries, exc)
    raise ArxivError(f"arXiv website unreachable: {last}")


def _arxiv_search_html(query: str, max_results: int = 10, sort: str = "relevance") -> list[dict]:
    """Search via the arxiv.org/search HTML page (each result carries id + title +
    full abstract). Returns the same shape as the API search."""
    # The website's `query` param is plain text and ANDs every term, so a long
    # query (fine for the API) matches almost nothing here. Strip the API's field
    # prefixes (all:/ti:/abs:…) + punctuation and cap to the first ~6 keywords so
    # the AND still returns a usable pool.
    q = re.sub(r"\b(?:all|ti|abs|au|cat|co|jr|rn|id):", " ", query or "")
    q = " ".join(re.sub(r'["()]', " ", q).split()[:6]) or (query or "")
    order = "-announced_date_first" if sort == "submittedDate" else "relevance"
    r = _web_get(f"{ARXIV_WEB}/search/",
                 params={"searchtype": "all", "query": q, "start": 0, "order": order})
    out = []
    for block in re.findall(r'<li class="arxiv-result">.*?</li>', r.text, re.S):
        idm = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", block)
        if not idm:
            continue
        tm = re.search(r'<p class="title is-5[^"]*">(.*?)</p>', block, re.S)
        am = re.search(r'<span class="abstract-full[^"]*"[^>]*>(.*?)<a\s', block, re.S)
        out.append({"arxiv_id": idm.group(1),
                    "title": _strip_html(tm.group(1)) if tm else "",
                    "summary": _strip_html(am.group(1)) if am else ""})
        if len(out) >= max_results:
            break
    return out


def _arxiv_meta_html(raw_id: str) -> dict | None:
    """Metadata for one id via the arxiv.org/abs HTML page (citation meta tags +
    og:description abstract). None on failure / bad id."""
    aid = normalize_arxiv_id(raw_id)
    if not aid:
        return None
    try:
        txt = _web_get(f"{ARXIV_WEB}/abs/{aid}").text
    except ArxivError:
        return None
    title = re.search(r'<meta name="citation_title" content="(.*?)"', txt, re.S)
    if not title:
        return None
    authors = re.findall(r'<meta name="citation_author" content="(.*?)"', txt)
    date = re.search(r'<meta name="citation_(?:date|online_date|publication_date)" content="(\d{4})', txt)
    abstract = re.search(r'<meta property="og:description" content="(.*?)"\s*/?>', txt, re.S)
    return {"arxiv_id": aid,
            "title": _strip_html(title.group(1)) or "(untitled)",
            "authors": ", ".join(_strip_html(a) for a in authors),
            "year": date.group(1) if date else "",
            "abstract": _strip_html(abstract.group(1)) if abstract else ""}


# --- gaps ------------------------------------------------------------------
def _arxiv_search(query: str, max_results: int = 10, sort: str = "relevance") -> list[dict]:
    """Search arXiv. Tries the API, then falls back to the website HTML page when
    the API fails (429/timeout). ``sort`` is 'relevance' (default — on-topic first)
    or 'submittedDate' (newest first, for explicit 'latest' searches). Raises
    ArxivError only if BOTH are unreachable."""
    sort = sort if sort in ("relevance", "submittedDate") else "relevance"
    try:
        r = _arxiv_get({"search_query": query, "max_results": max_results,
                        "sortBy": sort, "sortOrder": "descending"})
    except ArxivError as exc:
        logger.warning("arxiv API search failed (%s); falling back to website", exc)
        return _arxiv_search_html(query, max_results, sort=sort)
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


def _venue_label(c: dict) -> str:
    """A short source tag for the LLM listing / UI: the peer-reviewed venue (with year)
    when known, else the arXiv id, else 'preprint'. Lets the picker prefer published work."""
    venue = (c.get("venue") or "").strip()
    if venue:
        year = str(c.get("year") or "").strip()
        return f"{venue} {year}".strip()
    if c.get("arxiv_id"):
        return f"arXiv:{c['arxiv_id']}"
    return "preprint"


def _cand_key(c: dict):
    """Dedup key across sources: arXiv id, then DOI, then lowercased title."""
    aid = normalize_arxiv_id(c.get("arxiv_id") or "")
    if aid:
        return ("arxiv", aid)
    doi = (c.get("doi") or "").strip().lower()
    if doi:
        return ("doi", doi)
    title = (c.get("title") or "").strip().lower()
    return ("title", title) if title else None


def _merge_candidates(*lists: list[dict]) -> list[dict]:
    """Union candidate lists from multiple sources, deduped by _cand_key. When the same
    paper appears in more than one source, fields are unioned (the longer abstract wins;
    Semantic Scholar's venue/citation/pdf_url/doi enrich an arXiv hit). Source order is
    preserved (first list's hits lead)."""
    by_key: dict = {}
    order: list = []
    for lst in lists:
        for c in (lst or []):
            key = _cand_key(c)
            if not key or not key[1]:
                continue
            if key in by_key:
                dst = by_key[key]
                for k, v in c.items():
                    if k == "summary":
                        if len(str(v or "")) > len(str(dst.get("summary") or "")):
                            dst["summary"] = v
                    elif v and not dst.get(k):
                        dst[k] = v
            else:
                by_key[key] = dict(c)
                order.append(key)
    return [by_key[k] for k in order]


_PDF_FETCH_MAX_BYTES = 30 * 1024 * 1024     # cap the parse-time download


def is_pdf_url(tok: str) -> bool:
    """True for an http(s) URL whose path ends in '.pdf' (query/fragment ignored).
    arXiv/OpenReview links are matched earlier, so this only catches direct-PDF links."""
    t = (tok or "").strip()
    if not re.match(r"^https?://", t, re.IGNORECASE):
        return False
    path = re.split(r"[?#]", t, 1)[0]
    return path.lower().endswith(".pdf")


def _title_from_pdf_url(url: str) -> str:
    """Provisional title from a PDF URL's filename (fallback when extraction fails)."""
    path = re.split(r"[?#]", url or "", 1)[0]
    name = path.rsplit("/", 1)[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[._\-]+", " ", name).strip()
    return name or "(untitled)"


def fetch_pdf_url_metadata(url: str):
    """Download a direct PDF link and best-effort extract title/author from it.
    Three outcomes:
      * dict  — a real PDF; {pdf_url, title, authors, year, abstract} (title from the
                PDF's /Title metadata, then a first-page heuristic, then the URL filename).
      * "notpdf"   — the link returned content that isn't a PDF (e.g. an HTML page).
      * "notfound" — a dead link (HTTP 404/410).
      * None  — a transient failure (timeout, 403/429/5xx, connection); caller may still
                add it with a filename title since the PDF is (re)downloaded later."""
    import os
    import tempfile
    from pathlib import Path

    from . import pdf_text
    buf = bytearray()
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers=_HEADERS) as client:
            with client.stream("GET", url) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > _PDF_FETCH_MAX_BYTES:
                        break
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (404, 410):
            return "notfound"
        logger.warning("PDF link fetch failed for %s: %s", url, exc)
        return None
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("PDF link fetch failed for %s: %s", url, exc)
        return None
    if bytes(buf[:5]) != b"%PDF-":
        return "notpdf"
    title = authors = None
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(buf)
            tmp = tf.name
        # The PDF's embedded /Title is usually cleaner than a first-page heuristic
        # (which can swallow an author line) — prefer it when it looks like a real
        # title, not a filename/tool artefact ("Microsoft Word - …", "untitled", ".tex").
        doc_title = None
        try:
            from pypdf import PdfReader
            info = PdfReader(tmp).metadata or {}
            dt = str(info.get("/Title") or "").strip()
            if (8 <= len(dt) <= 250 and re.search(r"[A-Za-z]", dt)
                    and not re.search(r"(microsoft word|\.(docx?|tex|dvi)\b|^untitled)", dt, re.IGNORECASE)):
                doc_title = " ".join(dt.split())
            if info.get("/Author"):
                authors = str(info.get("/Author")).strip()
        except Exception as exc:  # noqa: BLE001 - docinfo is best-effort
            logger.debug("pdf docinfo read failed for %s: %s", url, exc)
        title = doc_title or pdf_text.extract_title(Path(tmp))
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return {"pdf_url": url, "title": title or _title_from_pdf_url(url),
            "authors": authors or "", "year": "", "abstract": ""}


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
        if is_pdf_url(tok):
            meta = fetch_pdf_url_metadata(tok)
            if meta in ("notpdf", "notfound"):
                out.append({"input": tok, "kind": None, "id": None, "title": None,
                            "authors": "", "year": "", "abstract": "", "ok": False,
                            "error": "PDF not found (404)" if meta == "notfound"
                                     else "Link didn't return a PDF"})
            else:
                # dict → metadata read; None → couldn't fetch now (still addable; the
                # PDF is downloaded later) with a filename title and a soft note.
                m = meta or {"pdf_url": tok, "title": _title_from_pdf_url(tok),
                             "authors": "", "year": "", "abstract": ""}
                out.append({"input": tok, "kind": "pdf", "id": tok, "pdf_url": tok,
                            "title": m["title"], "authors": m["authors"],
                            "year": m["year"], "abstract": m["abstract"], "ok": True,
                            "note": None if meta else "couldn't read metadata — title from filename",
                            "error": None})
            continue
        out.append({"input": tok, "kind": None, "id": None, "title": None, "authors": "",
                    "year": "", "abstract": "", "ok": False,
                    "error": "Not an arXiv, OpenReview, or .pdf URL"})
    return out


def fetch_arxiv_metadata(raw_id: str) -> dict | None:
    """Look up a single arXiv paper's metadata by id/URL. Returns
    {arxiv_id, title, authors, year, abstract} or None (bad id / network error)."""
    aid = normalize_arxiv_id(raw_id)
    if not aid:
        return None
    try:
        r = _arxiv_get({"id_list": aid, "max_results": 1})
    except ArxivError as exc:
        logger.warning("arxiv API metadata fetch failed for %s (%s); trying website", aid, exc)
        return _arxiv_meta_html(aid)         # website fallback
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
            resp = _arxiv_get({"id_list": ",".join(chunk), "max_results": len(chunk)}, timeout=30.0)
            for entry in ET.fromstring(resp.text).findall(f"{ATOM}entry"):
                if entry.find(f"{ATOM}id") is None:
                    continue
                meta = _parse_entry(entry)
                if meta["arxiv_id"]:
                    out[meta["arxiv_id"]] = meta
        except (ArxivError, ET.ParseError) as exc:
            logger.warning("arxiv API batch fetch failed for %s (%s); trying website per-id", chunk, exc)
            for a in chunk:                  # website fallback, one page per id
                m = _arxiv_meta_html(a)
                if m and m["arxiv_id"]:
                    out[m["arxiv_id"]] = m
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

    try:
        results = _arxiv_search(query)
    except ArxivError as exc:
        logger.warning("gap search failed: %s", exc)
        return []
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


def _paper_yyyymm(c: dict) -> tuple[int, int] | None:
    """(year, month) for a candidate — from its arXiv id (YYMM.nnnnn) or its `year`
    field. None when undatable (then a cutoff won't drop it — we don't hide what we
    can't date)."""
    m = re.match(r"(\d{2})(\d{2})\.", str(c.get("arxiv_id") or ""))
    if m:
        return (2000 + int(m.group(1)), int(m.group(2)))
    y = str(c.get("year") or "").strip()[:4]
    if y.isdigit():
        return (int(y), 12)              # year-only source → lenient within that year
    return None


def passes_since(c: dict, since: str) -> bool:
    """Keep candidate ``c`` iff its publication date is on/after ``since`` (a cutoff
    'YYYY' | 'YYYY-MM' | 'YYYY-MM-DD', compared at month granularity)."""
    since = (since or "").strip()
    if not since:
        return True
    parts = since.split("-")
    try:
        cy = int(parts[0])
        cm = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    except (ValueError, IndexError):
        return True
    pm = _paper_yyyymm(c)
    return pm is None or pm >= (cy, cm)


def find_related_papers(seed: str, exclude_titles: set[str] | None = None,
                        limit: int = 10, intent: str = "",
                        prefer: list[str] | None = None,
                        avoid: list[str] | None = None,
                        since: str = "") -> list[dict]:
    """Recommend arXiv papers, seeded by a free-text FOCUS. Two LLM steps: build
    an arXiv query from the focus, then pick up to ``limit`` of the most relevant
    results, each with a CONCRETE reason. Results already present (by title) are
    dropped. ``intent`` (optional) states the PURPOSE — what to look for and how
    to judge fit. ``prefer``/``avoid`` (optional) are the titles of papers the user
    previously kept / passed on — a SOFT bias for the pick step (learning signal).

    Returns ``[{arxiv_id, title, summary, authors, note}]`` (note = the reason).
    Network action (arXiv) — callers gate it behind an explicit user click."""
    seed = (seed or "").strip()
    if not seed:
        return []
    limit = max(1, int(limit))
    intent = (intent or "").strip()
    q_goal = intent or "a researcher building this collection would want to read or cite next"
    pick_goal = intent or "best extend or fill gaps in this collection"
    bias = ""
    if prefer:
        bias += "\nThe researcher PREVIOUSLY KEPT (prefer this kind): " + "; ".join(prefer[:8])
    if avoid:
        bias += "\nThey PASSED ON (deprioritise this kind): " + "; ".join(avoid[:8])
    exclude = {t.lower() for t in (exclude_titles or set())}
    try:
        query_resp = llm.complete([
            {"role": "system", "content": "Output only a short arXiv search query string (3-6 keywords)."},
            {"role": "user", "content": f"FOCUS:\n{seed}\n\n"
             f"Give a concise arXiv search query for papers that {q_goal}. CRITICAL: anchor the "
             "query on this collection's DEFINING subject/domain (the specific modality, task, or "
             "object it's about) so results stay on-topic — don't reduce it to generic ML terms "
             "(e.g. 'reasoning', 'memory', 'attention', 'efficiency') that match unrelated work. "
             "Lead with the domain-anchor terms."},
        ]).strip().strip('"')
    except Exception:  # noqa: BLE001
        return []
    query = f"all:{query_resp}" if ":" not in query_resp else query_resp
    # Fetch a wider candidate pool than `limit` so the LLM has room to pick the
    # best `limit` after dropping papers already present. Relevance-sorted (not
    # recency) so the pool is on-topic, not just "newest that loosely matches".
    # A `since` cutoff drops older papers, so widen the pool to keep enough.
    pool = max(limit * 3, 30) if since else max(limit * 2, 20)
    # Query BOTH arXiv and Semantic Scholar and merge — S2 reaches peer-reviewed top
    # venues (CVPR/ICCV/NeurIPS/ICLR/ACL…) with open-access PDFs that arXiv alone
    # misses, while arXiv covers the freshest preprints. Each is best-effort: a source
    # that errors (e.g. the shared-IP 429) is skipped; only if BOTH fail do we surface
    # the rate-limit hint.
    from . import semantic_scholar
    arxiv_hits: list[dict] = []
    s2_hits: list[dict] = []
    arxiv_err = s2_err = None
    try:
        arxiv_hits = _arxiv_search(query, max_results=pool)
    except ArxivError as exc:
        arxiv_err = exc
        logger.warning("arxiv search failed (%s) — using Semantic Scholar only", exc)
    try:
        s2_hits = semantic_scholar.search(query_resp or query, max_results=pool)
    except semantic_scholar.S2Error as exc:
        s2_err = exc
        logger.info("semantic scholar search skipped (%s)", exc)
    if arxiv_err and s2_err:                  # both providers refused — actionable hint
        raise ArxivError(_RATE_LIMIT_HINT)
    raw = _merge_candidates(arxiv_hits, s2_hits)
    results = [r for r in raw if r["title"].lower() not in exclude and passes_since(r, since)]
    if not results:
        return []
    listing = "\n".join(f"{i}. {r['title']} [{_venue_label(r)}]: {(r.get('summary') or '')[:300]}"
                        for i, r in enumerate(results))
    try:
        pick = llm.complete([
            {"role": "system", "content": "You output only valid JSON."},
            {"role": "user", "content": (
                f"FOCUS:\n{seed}{bias}\n\nCANDIDATES (the [bracketed] tag is the venue, "
                f"or 'preprint'):\n{listing}\n\n"
                f"Pick up to {limit} candidates that {pick_goal}. For EACH, give a "
                "concrete one-sentence reason it fits this goal (what it adds / which "
                "gap, concept, hypothesis or unknown it speaks to) — not a generic "
                "summary. When two candidates are comparably relevant, prefer the one "
                "published at a peer-reviewed venue over a bare preprint. "
                'Respond JSON: {"picks": [{"index": 0, "note": "why it fits"}]}')},
        ])
        import json
        data = json.loads(pick[pick.find("{"): pick.rfind("}") + 1])
    except (ValueError, Exception):  # noqa: BLE001
        return []
    out, seen = [], set()
    for p in data.get("picks", []):
        idx = p.get("index")
        if isinstance(idx, int) and 0 <= idx < len(results) and idx not in seen:
            seen.add(idx)
            out.append({**results[idx], "note": (p.get("note") or "").strip()})
        if len(out) >= limit:
            break
    return out


def validate_candidates(target: str, candidates: list[dict], intent: str = "") -> list[dict]:
    """Independent, skeptical pre-validation of finder candidates (the 'validator'
    stage of find → verify → land). For each candidate, check the claimed relevance
    against the paper's OWN abstract — the summary we already fetched, so no extra
    network. Drops clear mismatches ('fail'); keeps 'pass'/'weak', annotated with
    verdict + confidence + a one-line justification.

    One LLM call per candidate, in an independent skeptical context (a genuine check,
    not the finder grading itself). Read-only; the user still gates Accept."""
    import json
    goal = (intent or target or "").strip()
    out = []
    for c in candidates:
        abstract = (c.get("summary") or "").strip()
        if not abstract:                       # nothing to verify against → keep, low confidence
            out.append({**c, "verdict": "weak", "confidence": 0.4,
                        "justification": "No abstract available to verify."})
            continue
        system = ("You are a skeptical reviewer checking whether a paper actually serves "
                  "a stated research goal. Judge ONLY from the abstract. Default to 'fail' "
                  "unless the abstract clearly supports the goal. STRICT JSON: "
                  '{"verdict":"pass|weak|fail","confidence":0.0-1.0,'
                  '"why":"one sentence grounded in the abstract"}.')
        user = (f"GOAL: {goal}\n\nPAPER: {c.get('title', '')}\nABSTRACT: {abstract[:1500]}\n\n"
                "Does the abstract clearly support the goal? Be strict — a passing mention "
                "is not support.")
        try:
            raw = llm.complete([{"role": "system", "content": system},
                                {"role": "user", "content": user}])
            j = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except (ValueError, Exception):  # noqa: BLE001
            j = {}
        verdict = j.get("verdict") if j.get("verdict") in ("pass", "weak", "fail") else "weak"
        if verdict == "fail":
            continue                           # drop clear mismatches
        try:
            conf = max(0.0, min(1.0, float(j.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        out.append({**c, "verdict": verdict, "confidence": round(conf, 2),
                    "justification": _clip((j.get("why") or "").strip(), 320)})
    return out


def _clip(text: str, n: int) -> str:
    """Trim to ~n chars on a word boundary with an ellipsis — never mid-word/number
    (the validator's 'why' was hard-cut at a fixed length, e.g. '… 759 hours, 1,253')."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0].rstrip(".,;:—- ") + "…"


# --- learning: deterministic preference profile from accept/reject history ----
_STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "with", "via",
    "from", "by", "is", "are", "be", "as", "at", "using", "use", "based", "toward",
    "towards", "into", "over", "under", "we", "our", "this", "that", "these", "those",
    "can", "learning", "model", "models", "approach", "method", "methods", "paper",
    "study", "novel", "new", "efficient", "scalable", "framework", "via",
}


def _tokens(text: str) -> set[str]:
    toks = {w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", (text or "").lower())
            if w not in _STOPWORDS}
    # CJK text has no spaces, so the ASCII word regex extracts nothing — approximate
    # tokens with overlapping Han-character bigrams so preference learning (accept/reject
    # keyword profile) also works on Chinese titles instead of silently no-op'ing.
    for run in re.findall(r"[一-鿿]{2,}", text or ""):
        toks |= {run[i:i + 2] for i in range(len(run) - 1)}
    return toks


def preference_profile(accepted_titles: list[str], dismissed_titles: list[str]) -> dict:
    """A lightweight keyword profile from accept/reject history: words from kept
    papers BOOST, words from passed-on papers PENALISE (minus any also boosted).
    Deterministic, no LLM — improves as the user acts."""
    boost: set[str] = set()
    for t in accepted_titles or []:
        boost |= _tokens(t)
    penalise: set[str] = set()
    for t in dismissed_titles or []:
        penalise |= _tokens(t)
    penalise -= boost
    return {"boost": boost, "penalise": penalise}


def rerank_by_profile(candidates: list[dict], profile: dict,
                      dismissed_arxiv: set[str] | None = None) -> list[dict]:
    """Re-rank candidates by the preference profile (stable: ties keep finder
    order). Tags each with ``seen_before`` (previously dismissed — a soft penalty,
    NOT a hard block, so it can resurface). Returns the reordered list."""
    boost, penalise = profile.get("boost", set()), profile.get("penalise", set())
    dismissed_arxiv = dismissed_arxiv or set()
    scored = []
    for i, c in enumerate(candidates):
        toks = _tokens(f"{c.get('title','')} {c.get('summary','')}")
        seen = c.get("arxiv_id") in dismissed_arxiv
        # Strong (validator 'pass') matches lead; 'weak' sort beneath them. Then the
        # learned preference score; then finder order. So the on-topic, verified
        # suggestions are at the top and borderline ones don't bury them.
        verdict_rank = 0 if c.get("verdict") == "pass" else 1
        # Lightly prefer peer-reviewed venues over bare preprints (a gentle +1 tiebreaker;
        # relevance/verdict still dominate).
        venue_bonus = 1 if (c.get("venue") or "").strip() else 0
        score = len(toks & boost) - len(toks & penalise) - (2 if seen else 0) + venue_bonus
        scored.append((verdict_rank, -score, i, {**c, "seen_before": seen}))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))   # pass>weak, score desc, finder order
    return [c for _v, _s, _i, c in scored]


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
