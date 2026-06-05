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
from .config import load_config

logger = logging.getLogger("paper_agent.paper_finder")

# Read-only allowlist for the finder agent: arXiv (its only network reach) + the
# collection read tools + the accept/reject history. No write tools — trifecta closed.
FINDER_TOOLS = [f"mcp__pa__{t}" for t in
                ("arxiv_search", "recommendation_history", "search_fragments",
                 "read_wiki_page", "read_paper_text")]


def deep_find(slug: str, focus: str, intent: str, *, limit: int = 10) -> list[dict]:
    """Spawn the paper-finder agent (MCP scoped to ``slug``, read-only); parse its
    JSON picks; fetch each arXiv id's metadata so the validator has a real abstract.
    Returns pre-validation candidates ``[{arxiv_id, title, summary, authors, note}]``.
    Raises ``llm.LLMError`` if the engine is unavailable."""
    eng = engine_mod.build_engine(load_config())
    ok, detail = eng.available()
    if not ok:
        raise llm.LLMError(f"{eng.name} is unavailable: {detail}")
    system = (agent_skills.skill_body("paper-finder")
              or 'Find arXiv papers serving the purpose; output STRICT JSON '
                 '{"papers":[{"arxiv_id","title","why"}]}.')
    user = (f"FOCUS:\n{focus}\n\nPURPOSE / WHAT TO FIND:\n{intent}\n\n"
            f"Find up to {limit} papers. Use your tools to search and cross-check, "
            "then output the JSON.")
    try:
        res = eng.run_once([{"role": "user", "content": user}], system=system,
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
    out, seen = [], set()
    for p in (data.get("papers") or [])[: max(1, limit) * 2]:
        aid = discover.normalize_arxiv_id(str(p.get("arxiv_id") or ""))
        if not aid or aid in seen:
            continue
        seen.add(aid)
        meta = discover.fetch_arxiv_metadata(aid)        # real abstract for the validator
        if not meta:
            continue                                     # invented / unresolvable id → drop
        out.append({"arxiv_id": aid, "title": meta.get("title") or (p.get("title") or ""),
                    "summary": meta.get("abstract", ""), "authors": meta.get("authors", ""),
                    "note": (p.get("why") or "").strip()})
        if len(out) >= limit:
            break
    return out
