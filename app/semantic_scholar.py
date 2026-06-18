"""Semantic Scholar adapter — a second paper-discovery source.

Used as a fallback when arXiv rate-limits (429), and for its quality signals
(venue, citation count) and relevance ranking. A free API key (set in Settings)
gives a personal rate limit that works on shared/institutional IPs where the
public quota 429s. Read-only; the only network use is api.semanticscholar.org.

Candidate shape matches the arXiv one (arxiv_id/title/summary/authors) plus extras
(venue, citation_count, year, doi, pdf_url, s2_id) so the rest of the pipeline —
LLM pick, validator, triage — is unchanged.
"""
from __future__ import annotations

import logging

import httpx

from .config import load_config

logger = logging.getLogger("paper_agent.semantic_scholar")

_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
_FIELDS = "title,abstract,year,venue,citationCount,externalIds,openAccessPdf,authors"
_UA = {"User-Agent": "prinny/0.1 (local research wiki)"}


class S2Error(RuntimeError):
    """Semantic Scholar was unreachable or rate-limited."""


def _headers() -> dict:
    h = dict(_UA)
    key = (load_config().get("semantic_scholar_api_key") or "").strip()
    if key:
        h["x-api-key"] = key
    return h


def _to_candidate(p: dict, *, require_abstract: bool = True) -> dict | None:
    """Map an S2 paper object to our candidate shape.

    Discovery/ranking paths require an abstract because the validator needs one to
    ground relevance. Exact identifier imports (DOI/arXiv link paste) can keep a
    metadata-only result so the PDF source is still usable.
    """
    abstract = (p.get("abstract") or "").strip()
    if require_abstract and not abstract:
        return None
    ext = p.get("externalIds") or {}
    oa = p.get("openAccessPdf") or {}
    return {
        "arxiv_id": ext.get("ArXiv"),                 # present for most ML/CS papers
        "doi": ext.get("DOI"),
        "s2_id": p.get("paperId"),
        "title": (p.get("title") or "").strip() or "(untitled)",
        "summary": abstract,
        "authors": ", ".join(a.get("name", "") for a in (p.get("authors") or []) if a.get("name")),
        "year": str(p.get("year") or ""),
        "venue": (p.get("venue") or "").strip(),
        "citation_count": p.get("citationCount") or 0,
        "pdf_url": (oa.get("url") or "").strip(),      # open-access PDF (bypasses arXiv)
    }


def search(query: str, max_results: int = 20) -> list[dict]:
    """Relevance-ranked search. Returns candidates (abstract-bearing only). Raises
    S2Error on rate-limit / network failure so callers can fall back or surface it."""
    try:
        r = httpx.get(_SEARCH, headers=_headers(), timeout=20.0, follow_redirects=True,
                      params={"query": query, "limit": max(1, min(100, max_results)),
                              "fields": _FIELDS})
        if r.status_code == 429:
            raise S2Error("Semantic Scholar is rate-limiting (HTTP 429). Add a free API key "
                          "in Settings for a personal quota, or try later.")
        r.raise_for_status()
        data = r.json()
    except S2Error:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise S2Error(f"Semantic Scholar unreachable: {exc}")
    out = []
    for p in (data.get("data") or []):
        c = _to_candidate(p)
        if c:
            out.append(c)
    return out


def fetch_batch(ids: list[str], *, require_abstract: bool = True) -> dict[str, dict]:
    """Resolve S2 paper ids (the deep-search agent's scholar picks) to candidates in
    ONE request. ``ids`` are S2 paperIds (also accepts 'DOI:…' / 'ARXIV:…' forms).
    Returns ``{requested_id: candidate}`` for those that resolved with an abstract.
    Best-effort: returns {} on rate-limit / network failure (caller drops those picks)."""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return {}
    try:
        r = httpx.post(_BATCH, headers=_headers(), timeout=30.0, follow_redirects=True,
                       params={"fields": _FIELDS}, json={"ids": ids})
        if r.status_code == 429:
            raise S2Error("Semantic Scholar is rate-limiting (HTTP 429).")
        r.raise_for_status()
        data = r.json()
    except (S2Error, httpx.HTTPError, ValueError) as exc:
        logger.warning("Semantic Scholar batch fetch failed: %s", exc)
        return {}
    out: dict[str, dict] = {}
    # The batch endpoint returns a list aligned to the input ids (null for misses).
    for rid, p in zip(ids, data if isinstance(data, list) else []):
        if not p:
            continue
        c = _to_candidate(p, require_abstract=require_abstract)
        if c:
            out[rid] = c
    return out
