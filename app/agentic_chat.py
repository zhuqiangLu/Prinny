"""/{collection} agentic chat (AGENTIC_PLAN P6).

A chat turn prefixed with ``/<collection-slug>`` is answered by the agent with the
bounded MCP READ tools (search_fragments, read_wiki_page, get_fragment,
get_unreasoned_seeds) — never submit_proposal, no raw Read/Write. Chat writes nothing
to the wiki; the only way collection thinking leaves a chat is the attribution-safe
capture (a (seed, agent) thought, plus an optional (reasoning, human) 'your take').

Un-prefixed chat is untouched (the existing collection/paper context path).
"""
from __future__ import annotations

import logging

from . import agent_skills, agents, engine as engine_mod, llm, mcp_server
from .config import load_config

logger = logging.getLogger("paper_agent.agentic_chat")

# Read-only tools for chat — note: NO submit_proposal (chat never proposes/writes).
CHAT_TOOLS = [f"mcp__pa__{t}" for t in
              ("get_unreasoned_seeds", "get_fragment", "search_fragments", "read_wiki_page")]

# Legacy literal tokens handled by chat_post itself (include-wiki), not collection slugs.
_RESERVED = {"collection", "wiki"}

_SYSTEM = (
    "You are a research assistant for ONE paper collection. Answer the user's question "
    "grounded in THEIR collection — use the read tools (search_fragments, get_fragment, "
    "read_wiki_page, get_unreasoned_seeds) to look things up before answering. Ground "
    "claims in what you find and say when the collection doesn't cover something. You "
    "cannot modify anything; you only read."
)


def parse_prefix(text: str) -> tuple[str | None, str]:
    """If ``text`` begins with ``/<token>`` (token not a reserved literal), return
    (token, remainder); else (None, text). Slug validity is checked by the caller."""
    s = (text or "").lstrip()
    if not s.startswith("/"):
        return None, text
    parts = s[1:].split(None, 1)
    token = parts[0] if parts else ""
    remainder = parts[1] if len(parts) > 1 else ""
    if not token or token in _RESERVED:
        return None, text
    return token, remainder


def answer(slug: str, history: list[dict], user_text: str) -> str:
    """Answer a /{collection} turn via the agent with read-only MCP tools. Raises
    ``llm.LLMError`` if the engine is unavailable."""
    eng = engine_mod.build_engine(load_config())
    ok, detail = eng.available()
    if not ok:
        raise llm.LLMError(f"{eng.name} is unavailable: {detail}")
    messages = [{"role": m["role"], "content": m["content"]} for m in history
                if m.get("role") in ("user", "assistant")]
    messages.append({"role": "user", "content": user_text})
    try:
        res = eng.run_once(
            messages, system=_SYSTEM, allowed_tools=agents.effective_tools("chat", CHAT_TOOLS),
            mcp_config=mcp_server.stdio_mcp_config(slug),
            cwd=str(agent_skills.ensure_skills_home("chat")),
        )
    except engine_mod.EngineError as exc:
        raise llm.LLMError(str(exc)) from exc
    return res.text
