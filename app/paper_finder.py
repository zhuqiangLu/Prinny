"""Deep-search paper finder (Part 2) — a real tool-using sub-agent.

Unlike ``discover.find_related_papers`` (a fast 2-step pipeline we drive step by
step), this spawns a Claude Code / Codex run with read-only MCP tools
(``arxiv_search`` + the collection read tools + ``recommendation_history``) and
lets the MODEL loop: search arXiv, refine, cross-check the collection, learn from
history, then emit JSON picks. We fetch each pick's real metadata so the existing
validator can re-check it, then hand the candidates back to the same
find → verify → land pipeline. Read-only; the user still gates Accept.
"""
from __future__ import annotations

import json
import logging

from . import agent_skills, agents, discover, engine as engine_mod, llm, mcp_server
from .config import agent_model, load_config

logger = logging.getLogger("paper_agent.paper_finder")

# Read-only allowlist for the finder agent: arXiv (its only network reach) + the
# collection read tools + the accept/reject history. No write tools — trifecta closed.
FINDER_TOOLS = [f"mcp__pa__{t}" for t in
                ("arxiv_search", "scholar_search", "recommendation_history",
                 "search_fragments", "read_wiki_page", "read_paper_text")]


def deep_find(slug: str, focus: str, intent: str, *, limit: int = 10, since: str = "") -> list[dict]:
    """Spawn the paper-finder agent (MCP scoped to ``slug``, read-only); parse its
    JSON picks; resolve each pick's metadata (arXiv ids via the arXiv batch API, Semantic
    Scholar ids via the S2 batch API) so the validator has a real abstract. Returns
    pre-validation candidates ``[{arxiv_id|doi|s2_id, title, summary, authors, year,
    venue, citation_count, pdf_url, note}]``. Raises ``llm.LLMError`` if the engine is
    unavailable."""
    eng = engine_mod.build_engine(load_config())
    ok, detail = eng.available()
    if not ok:
        raise llm.LLMError(f"{eng.name} is unavailable: {detail}")
    system = (agent_skills.skill_body("paper-finder")
              or 'Find papers serving the purpose via arxiv_search / scholar_search; '
                 'output STRICT JSON {"papers":[{"arxiv_id"|"s2_id","title","why"}]}.')
    user = (f"FOCUS:\n{focus}\n\nPURPOSE / WHAT TO FIND:\n{intent}\n\n"
            f"Find up to {limit} papers. Use your tools to search and cross-check, "
            "then output the JSON.")
    try:
        res = eng.run_once([{"role": "user", "content": user}], system=system,
                           model=agent_model(),     # heavy reasoning → opus by default
                           allowed_tools=agents.effective_tools("finder", FINDER_TOOLS),
                           mcp_config=mcp_server.stdio_mcp_config(slug, read_only=True),
                           cwd=str(agent_skills.ensure_skills_home("finder")))
    except engine_mod.EngineError as exc:
        raise llm.LLMError(str(exc)) from exc

    text = res.text or ""
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except (ValueError, Exception):  # noqa: BLE001 - no parseable JSON → no picks
        return []
    # Dedupe the agent's picks and split by source. Each pick cites either an arXiv id
    # (arxiv_search) or a Semantic Scholar id (scholar_search). We resolve each source's
    # ids in ONE batched request (not one-per-pick — that bursts requests and trips rate
    # limits). A non-arXiv-shaped id is treated as an S2 id (covers the agent putting a
    # paperId in the arxiv_id field by mistake).
    arxiv_picks, s2_picks, seen = [], [], set()
    for p in (data.get("papers") or [])[: max(1, limit) * 2]:
        raw_id = str(p.get("arxiv_id") or p.get("id") or p.get("s2_id") or "").strip()
        aid = discover.normalize_arxiv_id(raw_id)
        note, title = (p.get("why") or "").strip(), p.get("title") or ""
        if aid:
            if aid in seen:
                continue
            seen.add(aid)
            arxiv_picks.append({"arxiv_id": aid, "title": title, "note": note})
        elif len(raw_id) >= 8:                            # looks like an S2 paperId
            if raw_id in seen:
                continue
            seen.add(raw_id)
            s2_picks.append({"s2_id": raw_id, "title": title, "note": note})
        if len(arxiv_picks) + len(s2_picks) >= limit:
            break
    if not arxiv_picks and not s2_picks:
        return []
    out = []
    if arxiv_picks:
        metas = discover.fetch_arxiv_batch([p["arxiv_id"] for p in arxiv_picks])  # one request
        for p in arxiv_picks:
            meta = metas.get(p["arxiv_id"])
            if not meta:
                continue                                  # invented / unresolvable id → drop
            cand = {"arxiv_id": p["arxiv_id"], "title": meta.get("title") or p["title"],
                    "summary": meta.get("abstract", ""), "authors": meta.get("authors", ""),
                    "year": meta.get("year", ""), "note": p["note"]}
            if discover.passes_since(cand, since):        # respect the date cap
                out.append(cand)
    if s2_picks:
        from . import semantic_scholar
        metas2 = semantic_scholar.fetch_batch([p["s2_id"] for p in s2_picks])  # one request
        for p in s2_picks:
            meta = metas2.get(p["s2_id"])
            if not meta:
                continue                                  # invented / unresolvable id → drop
            cand = {**meta, "note": p["note"]}            # meta carries summary/venue/pdf_url/…
            if not cand.get("title"):
                cand["title"] = p["title"]
            if discover.passes_since(cand, since):
                out.append(cand)
    return out
