"""Per-paper chat as an interactive sub-agent (PAPER_CHAT_AGENT.md, Phase 8).

Instead of stuffing system + paper text + the last 10 turns into a one-shot completion,
the per-paper chat becomes a persistent, read-only Claude Code session that reads the
actual PDF (built-in Read) and the user's live notes/highlights (MCP get_paper_context).
The conversation lives in the CLI session (resumed by id per thread), so we send only
the new turn — not the history.

Read-only: the sub-agent has no submit_* / write tools. Insights reach the user's
artifacts only via "draft notes" (edit/accept) or Phase-6 capture — never auto-saved.

Engine seam: ``PaperChatAgent`` with one impl today (Claude Code). Codex slots in later
(PAPER_CHAT_AGENT.md Phase D). No-PDF callers fall back to the classic chat.
"""
from __future__ import annotations

import base64
import logging
import re
import shutil
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from . import agent_skills, agents, engine as engine_mod, live_session, llm, mcp_server, pdf_store
from .config import load_config
from .repo import thread_message_count, get_session_id, set_session_id

_MAX_IMAGES = 4
_DATA_URL = re.compile(r"^data:(image/[\w.+-]+);base64,(.*)$", re.DOTALL)
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
        "image/gif": "gif", "image/webp": "webp"}


def _materialize_images(images):
    """Decode base64 data-URL images to temp files (the only way to show an image to a
    CLI agent: Claude reads them, Codex attaches them with -i). Returns (paths, cleanup)."""
    paths, tmpdir = [], None
    for data_url in (images or [])[:_MAX_IMAGES]:
        m = _DATA_URL.match(data_url or "")
        if not m:
            continue
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:  # noqa: BLE001 - skip a malformed attachment, don't fail the turn
            continue
        if tmpdir is None:
            tmpdir = tempfile.mkdtemp(prefix="pa-img-")
        p = f"{tmpdir}/{uuid.uuid4().hex}.{_EXT.get(m.group(1), 'png')}"
        with open(p, "wb") as f:
            f.write(raw)
        paths.append(p)

    def cleanup():
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return paths, cleanup


def _with_image_note(user_text, paths):
    """Append a Read-the-attached-images instruction (Claude/Live path)."""
    if not paths:
        return user_text
    listing = "\n".join(f"- {p}" for p in paths)
    return (f"{user_text}\n\n[The user attached {len(paths)} image(s) with this message. "
            f"Use the Read tool to view each before answering:\n{listing}]").strip()

logger = logging.getLogger("paper_agent.paper_chat")


class _StaleSession(Exception):
    """A resumed session id no longer exists (cwd change, expiry, cleanup, reinstall)."""


def _session_missing(text: str) -> bool:
    """True if a CLI error means the session id we tried to --resume is gone."""
    t = (text or "").lower()
    return "no conversation found" in t or "session not found" in t

# Read-only sub-agent toolset: paper text via our MCP tool (no shell) + Read for
# figures/layout + the bounded MCP read tools. Note: NO submit_*/Bash — chat never
# writes and never needs a shell.
_TOOLS = ["Read"] + [f"mcp__pa__{t}" for t in
                     ("read_paper_text", "get_paper_context", "search_fragments",
                      "get_fragment", "read_wiki_page", "list_papers", "get_chat_history")]


def _resume_hint(paper_id: int, n_prior: int) -> str:
    """A one-line nudge, added ONLY when a fresh session starts on a thread that already
    has stored history (cold/expired resume) — points the agent at get_chat_history so it
    recovers the conversation instead of starting over. Omitted otherwise."""
    if n_prior <= 0:
        return ""
    return (
        f"\nRESUMING: this conversation has {n_prior} earlier message(s) that are NOT in "
        f"your context (the session was resumed cold). BEFORE answering, call "
        f"get_chat_history(paper_id={paper_id}) to read the prior turns so you continue "
        f"the thread instead of starting over."
    )


def _system(collection: str, title: str, paper_id: int, pdf_path: str, n_prior: int = 0) -> str:
    return (
        "You are an interactive research assistant helping the user read ONE paper. The "
        "paper is evidence; the user's notes/highlights capture THEIR thinking. Help them "
        "understand and react to it — never replace their reading, never claim to have "
        "saved notes or edited the wiki (you cannot).\n"
        f"Collection: {collection}. Paper: {title} (id {paper_id}). PDF at: {pdf_path}\n"
        "READING THE PAPER:\n"
        f"- To read the text, call read_paper_text(paper_id={paper_id}, start_page, pages). "
        "It returns total_pages so you can page through the whole paper.\n"
        "- Use the Read tool ONLY when you need to see figures, tables, or layout.\n"
        "- NEVER use a shell/Bash, pdftotext, or pypdf — you don't have those and don't "
        "need them; read_paper_text is the way.\n"
        "- When the user quotes a passage prefixed with a page marker like '(p. 7)', that "
        "page is exactly where it appears: call read_paper_text(start_page=7, pages=1) to "
        "read it in context. Do NOT guess the location or rely on a similar passage "
        "elsewhere.\n"
        "Use get_paper_context to see the user's current notes/highlights for this paper, "
        "and search_fragments / read_wiki_page for collection context. Ground what you "
        "say in what you actually read; don't assume the paper's contents.\n"
        "COLLECTION CONTEXT: when the question is about the collection rather than only this "
        "paper (e.g. 'most similar paper I have', 'how does this compare to my others', "
        "'have I seen this elsewhere'), call list_papers to enumerate the collection and "
        "search_fragments / read_wiki_page to compare — do this yourself, don't ask the user "
        "for the paper list. Stay paper-centric otherwise.\n"
        "The earlier messages in this session ARE our chat history — treat them as such. If "
        "you ever lack prior context, call get_chat_history(paper_id) to read the stored "
        "transcript; never claim there is no prior history.\n"
        "You have paper-reading SKILLS available (summarize-section, extract-contributions, "
        "compare-to-my-notes, list-assumptions, locate-figure, find-evidence-for, "
        "resume-paper-chat) — invoke the right one when it fits the user's request."
        + _resume_hint(paper_id, n_prior)
    )


@dataclass
class PaperChatResult:
    reply: str
    session_id: str | None


class PaperChatAgent(ABC):
    name = "paper-chat"

    @abstractmethod
    def answer(self, slug: str, collection: str, thread_id: int, paper_id: int,
               title: str, user_text: str, images: list[str] | None = None) -> PaperChatResult: ...


class ClaudeCodePaperAgent(PaperChatAgent):
    name = "claude-code"

    def __init__(self, eng: engine_mod.ClaudeCodeEngine):
        self.eng = eng

    def _kwargs(self, slug, collection, title, paper_id, n_prior=0):
        """Per-call kwargs. ``n_prior`` > 0 adds the resume hint (a fresh session that has
        stored history) so the agent reads it via get_chat_history rather than re-feeding it."""
        pdf = pdf_store.ensure_cached(paper_id)
        return {
            "system": _system(collection, title, paper_id, str(pdf) if pdf else "", n_prior),
            "allowed_tools": _TOOLS,
            # read_only=True: the server itself refuses write tools (belt + suspenders
            # with the read-only --allowedTools above).
            "mcp_config": mcp_server.stdio_mcp_config(slug, read_only=True),
            "cwd": str(agent_skills.ensure_skills_home()),  # cwd carries the paper-reading skills
        }

    def answer(self, slug, collection, thread_id, paper_id, title, user_text,
               images=None) -> PaperChatResult:
        paths, cleanup = _materialize_images(images)
        msgs = [{"role": "user", "content": _with_image_note(user_text, paths)}]  # only the new turn
        try:
            sid = get_session_id(thread_id)
            if sid:                                       # resume the live CLI session (no hint)
                try:
                    res = self.eng.run_once(msgs, session_id=sid, resume=True,
                                            **self._kwargs(slug, collection, title, paper_id))
                    used = sid
                    if not (res.text or "").strip():      # silent stale resume (exit 0, empty)
                        sid = None
                except engine_mod.EngineError as exc:
                    if not _session_missing(str(exc)):
                        raise llm.LLMError(str(exc)) from exc
                    sid = None                            # stale -> fall through to fresh
            if not sid:                                   # fresh session: hint if history exists
                used = str(uuid.uuid4())
                kw = self._kwargs(slug, collection, title, paper_id, thread_message_count(thread_id))
                try:
                    res = self.eng.run_once(msgs, session_id=used, resume=False, **kw)
                except engine_mod.EngineError as exc:
                    raise llm.LLMError(str(exc)) from exc
        finally:
            cleanup()
        new_sid = res.session_id or used
        set_session_id(thread_id, new_sid)
        return PaperChatResult(reply=res.text, session_id=new_sid)

    def stream(self, slug, collection, thread_id, paper_id, title, user_text, images=None):
        """Yield live UI events ({status|token|done|error}); persist the session id on
        'done'. Self-heals a stale/expired session by starting fresh; the fresh session
        gets the resume hint (read get_chat_history) so it isn't cold. Falls back to fresh
        whenever the resume pass produces nothing real — a recognized 'no conversation
        found' error OR a silent failure (claude exits without a done/token)."""
        paths, cleanup = _materialize_images(images)
        msgs = [{"role": "user", "content": _with_image_note(user_text, paths)}]  # only the new turn

        def _drive(sid, resume, kw, st, terminal):
            """Yield events; record in ``st`` whether the turn produced real output. On a
            NON-terminal (resume) pass, a 'done' with no tokens and empty text is a silently
            failed resume — swallow it (don't show an empty bubble) so we fall back to fresh.
            The terminal (fresh) pass always yields its 'done' so the UI finalizes."""
            for ev in self.eng.stream_events(msgs, session_id=sid, resume=resume, **kw):
                tp = ev.get("type")
                # A recognized stale-resume error arrives before any token; restart fresh.
                if (tp == "error" and resume and not st["emitted"]
                        and _session_missing(ev.get("text", ""))):
                    raise _StaleSession()
                if tp == "token":
                    st["emitted"] = True
                if tp == "done":
                    set_session_id(thread_id, ev.get("session_id") or sid)
                    if not terminal and not st["emitted"] and not (ev.get("text") or "").strip():
                        return                            # failed resume -> swallow, go fresh
                    st["done"] = True
                yield ev

        try:
            sid = get_session_id(thread_id)
            if sid:
                st = {"emitted": False, "done": False}
                try:
                    yield from _drive(sid, True, self._kwargs(slug, collection, title, paper_id),
                                      st, terminal=False)
                except _StaleSession:
                    logger.warning("paper_chat: stale session %s for thread %s; starting fresh",
                                   sid, thread_id)
                if st["done"] or st["emitted"]:
                    return                                # resume worked (or streamed real content)
                logger.warning("paper_chat: resume of %s yielded nothing for thread %s; starting fresh",
                               sid, thread_id)
            kw = self._kwargs(slug, collection, title, paper_id, thread_message_count(thread_id))
            yield from _drive(str(uuid.uuid4()), False, kw, {"emitted": False, "done": False},
                              terminal=True)
        finally:
            cleanup()


class LivePaperAgent(PaperChatAgent):
    """Live mode: one persistent claude process per thread (no per-turn spawn). The
    process IS the session, so there's no stored session id to resume — conversation
    lives only while the process does (see live_session)."""
    name = "claude-code-live"

    def __init__(self, eng: engine_mod.ClaudeCodeEngine):
        self.eng = eng

    def stream(self, slug, collection, thread_id, paper_id, title, user_text, images=None):
        pdf = pdf_store.ensure_cached(paper_id)
        argv = self.eng.live_argv(
            system=_system(collection, title, paper_id, str(pdf) if pdf else ""),
            allowed_tools=agents.effective_tools("paper", _TOOLS),
            mcp_config=mcp_server.stdio_mcp_config(slug, read_only=True))
        sess = live_session.get_or_spawn(thread_id, argv, str(agent_skills.ensure_skills_home()))
        paths, cleanup = _materialize_images(images)
        try:
            yield from sess.turn(_with_image_note(user_text, paths))
        finally:
            cleanup()

    def answer(self, slug, collection, thread_id, paper_id, title, user_text,
               images=None) -> PaperChatResult:
        text = ""
        for ev in self.stream(slug, collection, thread_id, paper_id, title, user_text, images):
            if ev.get("type") == "done":
                text = ev.get("text", "")
            elif ev.get("type") == "error":
                raise llm.LLMError(ev.get("text", "live session error"))
        return PaperChatResult(reply=text, session_id=None)


def _codex_system(collection: str, title: str, paper_id: int, n_prior: int = 0) -> str:
    return (
        "You are an interactive research assistant helping the user read ONE paper. The "
        "paper is evidence; the user's notes/highlights capture THEIR thinking. Help them "
        "understand and react to it — never replace their reading, never claim to have "
        "saved notes or edited anything (you can't).\n"
        f"Collection: {collection}. Paper: {title} (id {paper_id}).\n"
        f"Read the paper with the pa MCP tool read_paper_text(paper_id={paper_id}, "
        "start_page, pages) — it returns total_pages so you can page through it. Use "
        "get_paper_context for the user's current notes/highlights, and search_fragments "
        "/ read_wiki_page for collection context. When the question is about the collection "
        "rather than only this paper (e.g. 'most similar paper I have', 'how does this "
        "compare'), call list_papers and search_fragments yourself to compare — don't ask "
        "the user for the paper list. You have READ-only access; do not "
        "attempt to write files or run shell commands. Ground what you say in what you "
        "actually read. When the user quotes a passage prefixed with a page marker like "
        f"'(p. 7)', call read_paper_text(paper_id={paper_id}, start_page=7, pages=1) to "
        "read that exact page for context — don't guess its location. The earlier messages "
        f"are our continuing chat history; if you lack prior context, call "
        f"get_chat_history(paper_id={paper_id}) to read the transcript."
        + _resume_hint(paper_id, n_prior)
    )


class CodexPaperAgent(PaperChatAgent):
    """Codex sub-agent (experimental). Read-only by construction: a read-only sandbox,
    the MCP server in PA_MCP_READONLY mode, and per-tool approve on ONLY these read
    tools (writes require approval → denied headless). PDF is read as text via
    read_paper_text (Codex has no visual PDF read)."""
    name = "codex"
    READ_TOOLS = ["get_unreasoned_seeds", "get_fragment", "search_fragments",
                  "read_wiki_page", "get_paper_context", "read_paper_text",
                  "list_papers", "get_chat_history"]

    def __init__(self, eng: engine_mod.CodexEngine):
        self.eng = eng

    def stream(self, slug, collection, thread_id, paper_id, title, user_text, images=None):
        sid = get_session_id(thread_id)
        # No stored session => a cold start; hint to read history if the thread has any.
        n_prior = 0 if sid else thread_message_count(thread_id)
        paths, cleanup = _materialize_images(images)     # Codex attaches images natively (-i)
        try:
            for ev in self.eng.paper_stream(
                slug=slug, system=_codex_system(collection, title, paper_id, n_prior),
                read_tools=self.READ_TOOLS, cwd=str(agent_skills.ensure_skills_home()),
                session_id=sid, user_text=user_text, image_paths=paths,
            ):
                if ev.get("type") == "done" and ev.get("session_id"):
                    set_session_id(thread_id, ev["session_id"])
                yield ev
        finally:
            cleanup()

    def answer(self, slug, collection, thread_id, paper_id, title, user_text,
               images=None) -> PaperChatResult:
        text, sid = "", get_session_id(thread_id)
        for ev in self.stream(slug, collection, thread_id, paper_id, title, user_text, images):
            if ev.get("type") == "done":
                text, sid = ev.get("text", ""), ev.get("session_id") or sid
            elif ev.get("type") == "error":
                raise llm.LLMError(ev.get("text", "codex error"))
        return PaperChatResult(reply=text, session_id=sid)


def get_agent(paper_id: int | None) -> PaperChatAgent | None:
    """The sub-agent for the current engine, or None to fall back to the classic chat.

    Eligible when the engine is a CLI agent (Claude Code, or Codex — experimental) AND
    the paper has a cached PDF (the sub-agent's whole point is reading it). For Claude the
    persistence mode (resume | live) is a config choice. Everything else (no PDF)
    uses the classic path.
    """
    if not paper_id or not pdf_store.has_pdf(paper_id):
        return None
    cfg = load_config()
    eng = engine_mod.build_engine(cfg)
    if isinstance(eng, engine_mod.CodexEngine):
        return CodexPaperAgent(eng)            # experimental; opted in by selecting Codex
    if isinstance(eng, engine_mod.ClaudeCodeEngine):
        if cfg.get("chat_session_mode", "resume") == "live":
            return LivePaperAgent(eng)
        return ClaudeCodePaperAgent(eng)
    return None
