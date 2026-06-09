"""Resolve an in-text citation (clicked in the PDF) to a real paper — within the arxiv-only
network rule. One LLM call maps the marker to the cited title using THIS paper's own reference
list (no external metadata service), then arXiv search + metadata fill in the details.
Read-only; never adds — the user does that from the popup."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import discover, llm, pdf_store, pdf_text

logger = logging.getLogger("paper_agent.citations")


def _references_text(paper_id: int, max_chars: int = 18000) -> str:
    """The paper's bibliography tail (from the last 'References'/'Bibliography' heading), where
    the full entry behind an in-text marker lives. Falls back to the document tail."""
    path = pdf_store.ensure_cached(paper_id)
    if not path:
        return ""
    full = pdf_text.extract_text(Path(path), max_chars=160000)
    low = full.lower()
    idx = max(low.rfind("\nreferences"), low.rfind("\nbibliography"), low.rfind("references\n"))
    tail = full[idx:] if idx > 500 else full[-max_chars:]
    return tail[:max_chars]


def resolve_citation(slug: str, paper_id: int, cite: str) -> dict:
    """Map an in-text marker (e.g. 'Lyu et al., 2023' or '[42]') to a paper. Returns
    {found, cite, title, authors, year, arxiv_id, arxiv_title, abstract}."""
    cite = (cite or "").strip()[:200]
    if not cite:
        return {"found": False, "cite": cite, "error": "empty"}
    refs = _references_text(paper_id)
    system = ('You identify the work behind an in-text citation marker using the paper\'s OWN '
              'reference list. Output ONLY JSON: {"title": "...", "authors": "...", "year": "..."}. '
              'Use the full reference entry that matches the marker; if no entry matches, return '
              '{"title": ""}. Never invent a title.')
    user = (f"In-text citation marker (clicked by the user): {cite}\n\n"
            f"The paper's reference list (may be truncated):\n{refs or '(no reference list extracted)'}")
    try:
        out = llm.complete([{"role": "system", "content": system},
                            {"role": "user", "content": user}])
        data = json.loads(out[out.find("{"): out.rfind("}") + 1])
    except Exception as exc:  # noqa: BLE001
        logger.warning("citation resolve failed for %r (%s): %s", cite, slug, exc)
        return {"found": False, "cite": cite, "error": "resolve failed"}
    title = (data.get("title") or "").strip()
    if not title:
        return {"found": False, "cite": cite}
    res = {"found": True, "cite": cite, "title": title,
           "authors": (data.get("authors") or "").strip(), "year": (data.get("year") or "").strip(),
           "arxiv_id": None, "arxiv_title": None, "abstract": ""}
    try:
        hits = discover._arxiv_search(title, max_results=3)
    except Exception:  # noqa: BLE001
        hits = []
    if hits and hits[0].get("arxiv_id"):
        meta = None
        try:
            meta = discover._arxiv_meta_html(hits[0]["arxiv_id"])
        except Exception:  # noqa: BLE001
            meta = None
        if meta:
            res.update(arxiv_id=meta["arxiv_id"], arxiv_title=meta["title"],
                       authors=res["authors"] or meta["authors"],
                       year=res["year"] or meta["year"], abstract=meta["abstract"])
    return res
