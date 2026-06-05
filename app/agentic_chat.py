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

# Read tools for chat + the gated wiki proposer. propose_wiki_edit never writes —
# it creates a pending proposal the user Accepts inline (propose-and-gate), so the
# lethal-trifecta surface stays closed (no autonomous write, no external comms).
CHAT_TOOLS = [f"mcp__pa__{t}" for t in
              ("get_unreasoned_seeds", "get_fragment", "search_fragments", "read_wiki_page",
               "list_papers", "propose_wiki_edit")]

# Legacy literal tokens handled by chat_post itself (include-wiki), not collection slugs.
_RESERVED = {"collection", "wiki"}

# Shared guidance on the gated proposer (used in both the normal and /updatewiki flows).
_PROPOSE_RULES = (
    " The collection's wiki lives in wiki/sections/ — read it with read_wiki_page "
    "('sections/thesis', 'sections/landscape', 'sections/concepts'). You may PROPOSE typed "
    "edits with propose_wiki_edit; this NEVER writes — it queues a proposal the user accepts "
    "inline, so you stay an editor, not the author. Evidence edits (concepts, beliefs, "
    "landscape problems/methods) MUST cite supporting_papers (refs from list_papers); thesis "
    "edits are grounded in the conversation. Invent nothing; propose only what the "
    "conversation or the collection actually supports."
)

_SYSTEM = (
    "You are a research assistant for ONE paper collection. Answer the user's question "
    "grounded in THEIR collection — use the read tools (search_fragments, get_fragment, "
    "read_wiki_page, get_unreasoned_seeds, list_papers) to look things up before answering. "
    "Ground claims in what you find and say when the collection doesn't cover something."
    + _PROPOSE_RULES +
    " Do this SPARINGLY — at most one or two proposals per turn, and only when the "
    "conversation clearly surfaces something worth capturing."
)

# /updatewiki: the explicit, user-invoked path — the whole job this turn is to update the wiki.
_UPDATE_SYSTEM = (
    "You are the wiki editor for ONE paper collection. The user has explicitly asked you to "
    "UPDATE THE WIKI now." + _PROPOSE_RULES +
    " First read the relevant current sections and the papers, then propose concrete typed "
    "edits that reflect the user's instruction (or, if none, the substance of the recent "
    "conversation). Make several focused proposals where warranted. End with a one-line "
    "summary of what you proposed; the user will Accept or Dismiss each."
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


def update_wiki(slug: str, history: list[dict], instruction: str) -> str:
    """The /updatewiki turn: spawn the agent with the same read + gated-propose
    tools but an explicit 'update the wiki now' directive. Raises llm.LLMError if
    the engine is unavailable."""
    eng = engine_mod.build_engine(load_config())
    ok, detail = eng.available()
    if not ok:
        raise llm.LLMError(f"{eng.name} is unavailable: {detail}")
    messages = [{"role": m["role"], "content": m["content"]} for m in history
                if m.get("role") in ("user", "assistant")]
    ask = (instruction or "").strip() or "Update the wiki to reflect our recent conversation."
    # NB: must NOT start with '/', or the CLI treats it as its own slash command.
    messages.append({"role": "user", "content": f"Please update the collection's wiki. {ask}"})
    try:
        res = eng.run_once(
            messages, system=_UPDATE_SYSTEM, allowed_tools=agents.effective_tools("chat", CHAT_TOOLS),
            mcp_config=mcp_server.stdio_mcp_config(slug),
            cwd=str(agent_skills.ensure_skills_home("chat")),
        )
    except engine_mod.EngineError as exc:
        raise llm.LLMError(str(exc)) from exc
    return res.text
