"""Auto-summary grounded in agent-created highlights.

The agent reads a paper and, for each MEANING in the user's highlight scheme (e.g.
methodology / insight / limitation / motivation), picks the key passages. The app then
CREATES color-coded highlights (tagged ``by_agent``) on those exact quotes — verified to
actually occur in the paper, so nothing is fabricated — and stages a structured summary
note-draft whose points cite each highlight (quote + page). The user reviews/accepts the
summary and can keep or bulk-clear the agent highlights.

Hybrid anchoring: highlights are stored anchorless (page + quote, empty rects); the PDF
viewer best-effort overlays them by searching the text layer, and always lists + cites them.
"""
from __future__ import annotations

import json
import logging
import re
import threading

from . import agent_skills, annotations as ann_mod, i18n, llm, note_drafts, pdf_store
from .config import agent_model, highlight_scheme

log = logging.getLogger("paper_agent.paper_summary")

_MAX_PAGES = 30          # cap the PDF excerpt sent to the model
_PAGE_CHARS = 3500
_POINTS_MAX = 16


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _page_texts(paper_id: int) -> list[str]:
    """Per-page plain text (1-indexed by list position) for the paper's cached PDF."""
    path = pdf_store.ensure_cached(paper_id)
    if not path:
        return []
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        log.warning("paper_summary: can't read PDF for %s: %s", paper_id, exc)
        return []
    pages = []
    for pg in reader.pages[:_MAX_PAGES]:
        try:
            pages.append(pg.extract_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")
    return pages


def _render_md(overall: str, points: list[dict], meanings: list[str]) -> str:
    """Structured summary markdown: an overall TL;DR, then one section per scheme meaning,
    each bullet citing the highlighted quote + page."""
    parts = []
    if overall.strip():
        parts.append(overall.strip())
    for m in meanings:
        pts = [p for p in points if _norm(p["meaning"]) == _norm(m)]
        if not pts:
            continue
        parts.append(f"**{m.capitalize()}**")
        for p in pts:
            parts.append(f"- {p['claim']} — “{p['quote']}” (p.{p['page']})")
    return "\n\n".join(parts).strip()


def summarize_from_highlights(slug: str, paper_id: int) -> dict:
    """One LLM call: summarize the paper + pick a grounded quote per scheme meaning. Creates
    the agent highlights (replacing any prior agent ones) and stages the summary draft.
    Returns ``{ok, error, n_highlights}``."""
    from . import library
    paper = library.get_paper(paper_id)
    if not paper:
        return {"ok": False, "error": "Paper not found.", "n_highlights": 0}
    pages = _page_texts(paper_id)
    if not any(p.strip() for p in pages):
        return {"ok": False, "error": "No PDF text available to summarize.", "n_highlights": 0}

    scheme = highlight_scheme()
    meanings = [s["label"] for s in scheme if (s.get("label") or "").strip()]
    color_by = {_norm(s["label"]): s["color"] for s in scheme if s.get("label")}
    if not meanings:
        return {"ok": False, "error": "No highlight scheme configured.", "n_highlights": 0}

    excerpt = "\n\n".join(f"[p.{i}]\n{t[:_PAGE_CHARS]}"
                          for i, t in enumerate(pages, start=1) if t.strip())
    skill = (agent_skills.skill_body("paper-summary")
             or 'Summarize the paper. For EACH meaning, quote the exact supporting passage from '
                'the paper text. STRICT JSON: {"overall":"…","points":[{"meaning","claim","quote","page"}]}.')
    skill += i18n.output_directive()
    user = (f"PAPER: {paper.get('title','')}\n\n"
            f"HIGHLIGHT MEANINGS (give a grounded point for each where the paper supports it): "
            + ", ".join(meanings) + "\n\n"
            "PAPER TEXT (page-tagged — quote VERBATIM from it, and report the page):\n"
            + excerpt + "\n\nReturn the STRICT JSON now.")
    try:
        out = llm.complete([{"role": "system", "content": skill},
                            {"role": "user", "content": user}], model=agent_model())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "n_highlights": 0}
    from .wiki import _extract_json
    data = _extract_json(out)
    if not data:
        return {"ok": False, "error": "The summarizer produced no usable output.", "n_highlights": 0}

    # Validate + ANTI-HALLUCINATION: keep a point only if its quote actually occurs in the
    # paper text (so an agent highlight is never placed on an invented passage), and resolve
    # its real page.
    page_norm = [_norm(t) for t in pages]
    points: list[dict] = []
    for pt in (data.get("points") or [])[:_POINTS_MAX]:
        meaning = (pt.get("meaning") or "").strip()
        quote = " ".join((pt.get("quote") or "").split()).strip()
        claim = (pt.get("claim") or "").strip()
        if not (meaning and quote and claim) or _norm(meaning) not in color_by:
            continue
        probe = _norm(quote)[:80]
        if not probe:
            continue
        hinted = pt.get("page") if isinstance(pt.get("page"), int) else 0
        found = hinted if (1 <= hinted <= len(pages) and probe in page_norm[hinted - 1]) else 0
        if not found:
            found = next((i for i, pn in enumerate(page_norm, start=1) if probe in pn), 0)
        if not found:
            continue                         # quote not in the paper → drop (don't highlight a fabrication)
        points.append({"meaning": meaning, "claim": claim, "quote": quote, "page": found,
                       "color": color_by[_norm(meaning)]})

    ann_mod.delete_agent(paper_id, slug)     # re-run replaces the prior agent highlights
    for p in points:
        ann_mod.create(slug, paper_id, kind="highlight", color=p["color"], page=p["page"] - 1,
                       position_json=json.dumps({"pageIndex": p["page"] - 1, "rects": []}),
                       selected_text=p["quote"], by_agent=1)
    md = _render_md(data.get("overall", ""), points, meanings)
    if not md.strip():
        return {"ok": False, "error": "Nothing groundable in the paper for the scheme meanings.",
                "n_highlights": 0}
    note_drafts.stage(slug, paper_id, md)
    return {"ok": True, "error": None, "n_highlights": len(points)}


# --- background job ---------------------------------------------------------
_JOBS: dict[int, dict] = {}
_LOCK = threading.Lock()


def get_job(paper_id: int) -> dict | None:
    with _LOCK:
        j = _JOBS.get(paper_id)
        return dict(j) if j else None


def start_async(slug: str, paper_id: int, title: str = "") -> bool:
    with _LOCK:
        if (_JOBS.get(paper_id) or {}).get("status") == "running":
            return False
        _JOBS[paper_id] = {"status": "running", "slug": slug,
                           "label": "Summarizing + highlighting", "started_at": _now()}

    def runner():
        from . import notify
        try:
            res = summarize_from_highlights(slug, paper_id)
            err = res.get("error")
            if err:
                notify.add(f"Summarize failed ({' '.join(str(err).split())[:70]}): {title[:40]}",
                           link=f"/c/{slug}/p/{paper_id}", collection=slug, ok=False)
            else:
                notify.add(f"Summary + {res['n_highlights']} highlights ready: {title[:50]}",
                           link=f"/c/{slug}/p/{paper_id}", collection=slug)
        except Exception as exc:  # noqa: BLE001
            log.exception("paper_summary job failed")
            notify.add(f"Summarize failed: {title[:50]}", link=f"/c/{slug}/p/{paper_id}",
                       collection=slug, ok=False)
        finally:
            with _LOCK:
                _JOBS.pop(paper_id, None)

    threading.Thread(target=runner, daemon=True, name=f"summary-{paper_id}").start()
    return True
