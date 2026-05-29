"""Agentic organizing pass (AGENTIC_PLAN P5).

Turns the wiki "generate" action into an agent-driven pass when a CLI agent is the
engine: the agent reads the user's fragments through the bounded MCP read tools and
returns proposals by calling ``submit_proposal`` — which runs the gate in code and
writes only the review queue. The agent never writes the wiki.

Engines that can't drive MCP tools (Codex until verified) fall back to the
tool-less two-step ``wiki.run_generation`` — same gate, same queue, no agent retrieval.
"""
from __future__ import annotations

import logging

from . import agent_skills, agents, engine as engine_mod, llm, mcp_server, wiki
from .config import load_config

logger = logging.getLogger("paper_agent.organizer")

# Engines that can drive the MCP read tools + submit_proposal.
_AGENTIC = {"claude-code"}

# The read + submit tools the organizing agent is allowed (and ONLY these).
_TOOLS = [f"mcp__pa__{t}" for t in
          ("get_unreasoned_seeds", "get_fragment", "search_fragments",
           "read_wiki_page", "submit_proposal")]

_SYSTEM = (
    "You are a clerical research assistant maintaining a personal wiki for ONE paper "
    "collection. The wiki is the USER's externalized thinking; you ORGANIZE what they "
    "wrote — you never add knowledge of your own. You have read-only tools to inspect "
    "the user's fragments and one tool, submit_proposal, to propose pages for the user "
    "to review (proposals are NOT applied automatically). Rules:\n"
    "1. State only what the user's fragments support; never add facts from your own "
    "knowledge or the wider literature.\n"
    "2. claim_type matters: an 'attributed' claim (what a paper reports) MUST cite a "
    "paper key or highlight id (the source itself), not just the user's note about it. "
    "A 'synthesis' claim (a cross-paper conclusion) MUST cite the thought or note where "
    "the USER reasoned it.\n"
    "3. Cite only fragment ids you actually retrieved.\n"
    "4. Be conservative: when in doubt, propose less."
)

_USER = (
    "Organize this collection's unreasoned fragments into proposed wiki pages.\n"
    "1) Call get_unreasoned_seeds to see what is unorganized.\n"
    "2) For fragments that share a theme, use get_fragment / search_fragments to read "
    "the details, and read_wiki_page('index') to see what already exists.\n"
    "3) Build pages for the sections problems / methods / gaps / benchmarks / synthesis "
    "and call submit_proposal(pages=[...]). Each claim needs text, claim_type, and the "
    "fragment ids it cites (notes, thoughts, papers, highlights).\n"
    "When finished, briefly summarize what you proposed (one or two sentences)."
)


def organize(slug: str, mode: str = "full") -> dict:
    """Run an organizing pass for ``slug``. Agentic when the engine can drive tools;
    otherwise the tool-less two-step. Returns a summary dict. Raises ``llm.LLMError``
    if the selected engine is unavailable (the route surfaces this)."""
    eng = engine_mod.build_engine(load_config())
    ok, detail = eng.available()
    if not ok:
        raise llm.LLMError(f"{eng.name} is unavailable: {detail}")

    if eng.name not in _AGENTIC:
        props = wiki.run_generation(slug, mode)  # tool-less fallback (same gate)
        return {"engine": eng.name, "agentic": False,
                "new_proposals": [p["page_path"] for p in props]}

    before = {p["id"] for p in wiki.list_proposed(slug)}
    try:
        res = eng.run_once(
            [{"role": "user", "content": _USER}],
            system=_SYSTEM,
            allowed_tools=agents.effective_tools("organizer", _TOOLS),
            mcp_config=mcp_server.stdio_mcp_config(slug),
            cwd=str(agent_skills.ensure_skills_home("organizer")),
        )
    except engine_mod.EngineError as exc:
        raise llm.LLMError(str(exc)) from exc
    new = [p for p in wiki.list_proposed(slug) if p["id"] not in before]
    logger.info("organizer.agentic slug=%s engine=%s new_proposals=%d",
                slug, eng.name, len(new))
    return {"engine": eng.name, "agentic": True, "final_text": res.text,
            "new_proposals": [p["page_path"] for p in new]}
