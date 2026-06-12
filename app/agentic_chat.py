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

from . import agent_skills, agents, engine as engine_mod, i18n, llm, mcp_server
from .config import load_config

logger = logging.getLogger("paper_agent.agentic_chat")

# Read tools for chat + the gated wiki proposer. propose_wiki_edit never writes —
# it creates a pending proposal the user Accepts inline (propose-and-gate), so the
# lethal-trifecta surface stays closed (no autonomous write, no external comms).
CHAT_TOOLS = [f"mcp__pa__{t}" for t in
              ("get_unreasoned_seeds", "get_fragment", "search_fragments", "read_wiki_page",
               "list_papers", "get_paper_context", "read_paper_text", "propose_wiki_edit")]

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
    "You are a research assistant for ONE paper collection. "
    "Answer conversational or general questions DIRECTLY and immediately — do NOT call "
    "tools for greetings, meta questions ('who are you'), or anything you already know. "
    "ONLY use the read tools (search_fragments, get_fragment, read_wiki_page, "
    "get_unreasoned_seeds, list_papers, get_paper_context, read_paper_text) when the question "
    "genuinely needs specifics from THIS collection — then ground claims in what you find and "
    "say when it isn't covered. Prefer the fewest tool calls that answer the question (often zero). "
    "CROSS-PAPER questions (contradictions, connections, similarities among papers the user has "
    "read): use search_fragments (it covers notes, thoughts, AND highlights) and list_papers to "
    "find candidate papers, then get_paper_context(paper_id) to read the user's notes/highlights "
    "on each — prefer the user's own take; use read_paper_text(paper_id) only if their notes are "
    "thin. CITE the specific passages (get_fragment, page numbers) behind any contradiction or "
    "link you claim — never manufacture one to be helpful."
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


def _materialize_images_under(images, home):
    """Decode base64 data-URL images to files UNDER ``home`` (the agent's cwd) so
    Claude's Read tool — bounded to the working dir — can open them. Returns
    (abs_paths, cleanup). Mirrors paper_chat's decoder but cwd-scoped."""
    import base64
    import re as _re
    import shutil
    import uuid
    from pathlib import Path

    data_url = _re.compile(r"^data:(image/[\w.+-]+);base64,(.*)$", _re.DOTALL)
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
           "image/gif": "gif", "image/webp": "webp"}
    paths, adir = [], Path(home) / ".pa-attachments"
    for url in (images or [])[:4]:
        m = data_url.match(url or "")
        if not m:
            continue
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:  # noqa: BLE001 - skip a bad attachment, don't fail the turn
            continue
        adir.mkdir(parents=True, exist_ok=True)
        p = adir / f"{uuid.uuid4().hex}.{ext.get(m.group(1), 'png')}"
        p.write_bytes(raw)
        paths.append(str(p))

    def cleanup():
        shutil.rmtree(adir, ignore_errors=True)
    return paths, cleanup


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


def answer_topic(messages: list[dict], mcp_slug: str | None,
                 images: list[str] | None = None) -> str:
    """Answer a research-topic turn agentically. ``messages`` is the topic-grounded
    transcript from topic_view.chat_messages ([system, …history, user]). Read-only
    tools are scoped to the topic's primary collection (``mcp_slug``) when present —
    no wiki proposer (a topic has no section wiki). Pasted images are materialized +
    read like the collection chat. Raises ``llm.LLMError`` if the engine is down."""
    from . import paper_chat
    eng = engine_mod.build_engine(load_config())
    ok, detail = eng.available()
    if not ok:
        raise llm.LLMError(f"{eng.name} is unavailable: {detail}")
    system, msgs = "", []
    for m in messages:
        if m.get("role") == "system" and not system:
            system = m.get("content", "")
        elif m.get("role") in ("user", "assistant"):
            msgs.append({"role": m["role"], "content": m.get("content", "")})
    # Read-only tools over the primary collection; drop the wiki proposer (topic ≠ wiki).
    # With no linked collection, the MCP tools can't resolve — answer tool-lessly.
    tools = ([t for t in CHAT_TOOLS if not t.endswith("propose_wiki_edit")] if mcp_slug else [])
    home = agent_skills.ensure_skills_home("chat")
    paths, cleanup = _materialize_images_under(images, home)
    if paths and msgs:
        msgs[-1]["content"] = paper_chat._with_image_note(msgs[-1]["content"], paths)
        if "Read" not in tools:
            tools = ["Read"] + tools
    try:
        res = eng.run_once(
            msgs, system=system,
            allowed_tools=(agents.effective_tools("chat", tools) if tools else None),
            mcp_config=mcp_server.stdio_mcp_config(mcp_slug) if mcp_slug else None,
            cwd=str(home))
    except engine_mod.EngineError as exc:
        raise llm.LLMError(str(exc)) from exc
    finally:
        cleanup()
    return res.text


def answer(slug: str, history: list[dict], user_text: str, images: list[str] | None = None) -> str:
    """Answer a /{collection} turn via the agent with read-only MCP tools. Pasted
    ``images`` are materialized to temp files the agent reads (Claude: Read tool,
    granted only when images are present; Codex: -i). Raises ``llm.LLMError`` if the
    engine is unavailable."""
    eng = engine_mod.build_engine(load_config())
    ok, detail = eng.available()
    if not ok:
        raise llm.LLMError(f"{eng.name} is unavailable: {detail}")
    messages = [{"role": m["role"], "content": m["content"]} for m in history
                if m.get("role") in ("user", "assistant")]
    # Per-collection proactive toggle: when off, the agent keeps its read tools but
    # loses the proposer (the explicit /updatewiki path always keeps it).
    tools = list(CHAT_TOOLS)
    try:
        from . import library
        col = library.get_collection(slug) or {}
        if not col.get("wiki_proactive", 1):
            tools = [t for t in tools if not t.endswith("propose_wiki_edit")]
    except Exception:  # noqa: BLE001
        pass

    # Images: materialize UNDER the agent's cwd (Claude's Read is bounded to the
    # working dir — a system temp dir is blocked), grant Read ONLY for this turn,
    # and clean up after. A plain text turn keeps the minimal MCP-only toolset.
    from . import paper_chat
    home = agent_skills.ensure_skills_home("chat")
    paths, cleanup = _materialize_images_under(images, home)
    if paths:
        user_text = paper_chat._with_image_note(user_text, paths)
        if "Read" not in tools:
            tools = ["Read"] + tools
    messages.append({"role": "user", "content": user_text})
    try:
        res = eng.run_once(
            messages, system=_SYSTEM + i18n.output_directive(), allowed_tools=agents.effective_tools("chat", tools),
            mcp_config=mcp_server.stdio_mcp_config(slug), cwd=str(home),
        )
    except engine_mod.EngineError as exc:
        raise llm.LLMError(str(exc)) from exc
    finally:
        cleanup()
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
