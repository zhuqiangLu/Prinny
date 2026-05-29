"""Report-only deep lint of the wiki (the heuristic pass).

The DETERMINISTIC checks (broken/orphan links, index drift) live in ``wiki.lint_wiki`` and
run in code. This adds the JUDGMENT checks — contradictions, stale claims, missing conflict
annotations, coverage gaps — via a READ-ONLY agent pass that REPORTS findings and never
edits the wiki (no submit tools; read-only MCP). Non-agentic engines get the deterministic
checks only. This honors the accept-only invariant: lint surfaces issues; the user acts.
"""
from __future__ import annotations

import logging

from . import agent_skills, agents, engine as engine_mod, llm, mcp_server
from .config import load_config

logger = logging.getLogger("paper_agent.wiki_lint")

# Only CLI agents that can drive MCP read tools do the heuristic pass.
_AGENTIC = {"claude-code"}

# Read-only tools — NO submit_*; lint never writes.
_TOOLS = [f"mcp__pa__{t}" for t in
          ("read_wiki_page", "search_fragments", "get_fragment", "get_unreasoned_seeds")]

_SYSTEM = (
    "You audit a personal research wiki and REPORT quality issues. You are read-only: you "
    "cannot and must not edit the wiki — you only surface findings for the USER to act on. "
    "Look for, grounded in what you actually read:\n"
    "- Contradictions: two pages (or a page and a cited fragment) that disagree.\n"
    "- Stale claims: a statement a newer note/paper supersedes.\n"
    "- Missing conflict annotations: sources that disagree where the page doesn't say so.\n"
    "- Coverage gaps: a concept referenced across pages with no page of its own.\n"
    "Report ONLY issues you can point to specific pages/fragments for, and cite them. If the "
    "wiki is consistent, say so plainly. Do NOT propose wiki text or claim to have fixed "
    "anything — you are a reviewer, not an editor."
)

_USER = (
    "Audit this collection's wiki. Read the index (read_wiki_page('index')) then the pages "
    "it lists; cross-check against the user's fragments with search_fragments / get_fragment "
    "where useful. Output a concise findings list in markdown — one bullet per issue, each "
    "naming the page(s) involved and why it's a problem. If nothing is wrong, say the wiki "
    "looks consistent."
)


def deep_lint(slug: str) -> dict:
    """Heuristic, report-only lint via a read-only agent. Returns
    {engine, agentic, report}. ``report`` is markdown (empty when not agentic).
    Raises ``llm.LLMError`` if the engine is unavailable."""
    eng = engine_mod.build_engine(load_config())
    ok, detail = eng.available()
    if not ok:
        raise llm.LLMError(f"{eng.name} is unavailable: {detail}")
    if eng.name not in _AGENTIC:
        return {"engine": eng.name, "agentic": False, "report": ""}
    try:
        res = eng.run_once(
            [{"role": "user", "content": _USER}],
            system=_SYSTEM,
            allowed_tools=agents.effective_tools("lint", _TOOLS),
            mcp_config=mcp_server.stdio_mcp_config(slug, read_only=True),
            cwd=str(agent_skills.ensure_skills_home("lint")),
        )
    except engine_mod.EngineError as exc:
        raise llm.LLMError(str(exc)) from exc
    logger.info("wiki_lint.deep slug=%s engine=%s", slug, eng.name)
    return {"engine": eng.name, "agentic": True, "report": res.text}
