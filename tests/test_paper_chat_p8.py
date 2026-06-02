"""PAPER_CHAT_AGENT Phase 8 (core) — interactive per-paper sub-agent.

Covers get_paper_context, agent eligibility (Claude + cached PDF only), and the session
new-vs-resume behavior: the first turn STARTS a session (sends only the new turn), later
turns RESUME it (history lives in the CLI session, not re-sent).
"""
from __future__ import annotations

import pytest

import app.annotations as ann_mod
import app.engine as engine_mod
import app.library as library
import app.mcp_server as mcp
import app.notes as notes_mod
import app.paper_chat as paper_chat
import app.pdf_store as pdf_store
import app.repo as repo
from app.db import connect, init_db
from app.engine import EngineResult


@pytest.fixture
def wired(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    cols = tmp_path / "collections"
    for mod in (notes_mod, ann_mod, mcp, library, repo):
        monkeypatch.setattr(mod, "connect", lambda: connect(db))
    monkeypatch.setattr(notes_mod, "COLLECTIONS_DIR", cols)
    con = connect(db)
    con.execute("INSERT INTO collections(slug,name) VALUES('c','C')")
    con.execute("INSERT INTO papers(id,title) VALUES(1,'P1')")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('c',1)")
    con.commit(); con.close()
    return {"db": db, "cols": cols}


# --- get_paper_context tool -----------------------------------------------------
def test_get_paper_context_returns_notes_and_highlights(wired):
    notes_mod.save_note("c", 1, "summary text", "my take", "", "noted")
    ann_mod.create("c", 1, kind="highlight", color="#ff0", page=2, position_json="{}",
                   selected_text="key sentence")
    ctx = mcp.get_paper_context("c", 1)
    assert ctx["note"]["thoughts"] == "my take" and ctx["note"]["synth_kind"] == "reasoning"
    assert any(h["text"] == "key sentence" for h in ctx["highlights"])


# --- eligibility ----------------------------------------------------------------
def test_get_agent_dispatches_resume_vs_live(wired, monkeypatch):
    monkeypatch.setattr(pdf_store, "has_pdf", lambda pid: True)
    monkeypatch.setattr(engine_mod, "build_engine", lambda cfg: engine_mod.ClaudeCodeEngine())
    monkeypatch.setattr(paper_chat, "load_config",
                        lambda: {"engine": "claude-code", "chat_session_mode": "resume"})
    assert isinstance(paper_chat.get_agent(1), paper_chat.ClaudeCodePaperAgent)
    monkeypatch.setattr(paper_chat, "load_config",
                        lambda: {"engine": "claude-code", "chat_session_mode": "live"})
    assert isinstance(paper_chat.get_agent(1), paper_chat.LivePaperAgent)


def test_get_agent_dispatches_codex(wired, monkeypatch):
    monkeypatch.setattr(pdf_store, "has_pdf", lambda pid: True)
    monkeypatch.setattr(engine_mod, "build_engine", lambda cfg: engine_mod.CodexEngine())
    monkeypatch.setattr(paper_chat, "load_config", lambda: {"engine": "codex"})
    assert isinstance(paper_chat.get_agent(1), paper_chat.CodexPaperAgent)


def test_codex_turn_events_parses_status_and_done():
    from app.engine import codex_turn_events
    lines = [
        '{"type":"thread.started","thread_id":"abc-123"}',
        '{"type":"item.started","item":{"type":"mcp_tool_call","tool":"read_paper_text"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"the answer"}}',
        '{"type":"turn.completed"}',
    ]
    evs = list(codex_turn_events(lines))
    assert any(e["type"] == "status" for e in evs)
    done = next(e for e in evs if e["type"] == "done")
    assert done["session_id"] == "abc-123" and done["text"] == "the answer"


def test_get_agent_requires_pdf_and_claude(wired, monkeypatch):
    monkeypatch.setattr(paper_chat, "load_config", lambda: {"engine": "claude-code"})
    monkeypatch.setattr(pdf_store, "has_pdf", lambda pid: False)
    assert paper_chat.get_agent(1) is None            # no PDF -> classic fallback
    monkeypatch.setattr(pdf_store, "has_pdf", lambda pid: True)
    monkeypatch.setattr(engine_mod, "build_engine", lambda cfg: engine_mod.FakeEngine())
    assert paper_chat.get_agent(1) is None            # non-CLI engine -> classic fallback
    monkeypatch.setattr(engine_mod, "build_engine", lambda cfg: engine_mod.ClaudeCodeEngine())
    assert isinstance(paper_chat.get_agent(1), paper_chat.ClaudeCodePaperAgent)


# --- session: start vs resume; only the new turn is sent ------------------------
class SpyEngine(engine_mod.ClaudeCodeEngine):
    def __init__(self):
        super().__init__()
        self.calls = []
    def run_once(self, messages, *, session_id=None, resume=False, allowed_tools=None, **kw):
        self.calls.append({"messages": messages, "session_id": session_id,
                           "resume": resume, "tools": allowed_tools})
        return EngineResult(text="answer", session_id="sid-echoed")


def test_skills_ship_and_materialize(tmp_path, monkeypatch):
    import app.agent_skills as ag
    monkeypatch.setattr(ag, "APP_DIR", tmp_path)
    names = ag.skill_names()                                  # default home = "paper"
    assert {"summarize-section", "compare-to-my-notes", "find-evidence-for"} <= set(names)
    home = ag.ensure_skills_home()
    for n in names:  # each becomes <home>/.claude/skills/<name>/SKILL.md
        assert (home / ".claude" / "skills" / n / "SKILL.md").is_file()


def test_per_agent_skills_homes_are_scoped(tmp_path, monkeypatch):
    """Each sub-agent gets its OWN home carrying only its skills — the chat home
    has answer-from-collection and not the paper-reading skills, and vice versa.
    (Post-cleanup: only paper / chat / wiki homes remain.)"""
    import app.agent_skills as ag
    monkeypatch.setattr(ag, "APP_DIR", tmp_path)
    # Single-skill home stays scoped to exactly its skill.
    h = ag.ensure_skills_home("chat")
    assert {p.name for p in (h / ".claude" / "skills").iterdir()} == {"answer-from-collection"}
    # Multi-skill home carries exactly its set (the wiki drafter's skills).
    w = ag.ensure_skills_home("wiki")
    assert {p.name for p in (w / ".claude" / "skills").iterdir()} == {
        "field-model", "belief-draft", "theme-name", "benchmark-extract", "section-edit"}
    # Paper home has its reading skills and none of the others'.
    paper = ag.ensure_skills_home("paper")
    pnames = {p.name for p in (paper / ".claude" / "skills").iterdir()}
    assert "answer-from-collection" not in pnames and "summarize-section" in pnames


def test_stream_self_heals_stale_session(wired, tmp_path, monkeypatch):
    """A dead resumed session id must self-heal: start fresh, swap in a new id."""
    import app.agent_skills as ag
    monkeypatch.setattr(ag, "APP_DIR", tmp_path)
    monkeypatch.setattr(pdf_store, "ensure_cached", lambda pid: tmp_path / "p.pdf")
    (tmp_path / "p.pdf").write_bytes(b"%PDF-1.4")

    class HealEngine(engine_mod.ClaudeCodeEngine):
        def stream_events(self, messages, *, session_id=None, resume=False, **kw):
            if resume:  # the stale resume fails like the real CLI
                yield {"type": "error", "text": "No conversation found with session ID: " + session_id}
            else:       # a fresh session succeeds
                yield {"type": "token", "text": "ok"}
                yield {"type": "done", "session_id": "fresh-sid", "text": "ok"}

    agent = paper_chat.ClaudeCodePaperAgent(HealEngine())
    tid = repo.get_or_create_thread("c", 1)
    repo.set_session_id(tid, "dead-session-id")           # plant a stale id
    evs = list(agent.stream("c", "C", tid, 1, "P1", "hi"))
    types = [e["type"] for e in evs]
    assert "error" not in types and "done" in types       # user never sees the stale error
    assert any(e["type"] == "token" and e["text"] == "ok" for e in evs)
    assert repo.get_session_id(tid) == "fresh-sid"         # stale id replaced


def test_chat_history_tool_returns_markdown_transcript(wired):
    tid = repo.get_or_create_thread("c", 1)
    repo.add_message(tid, "user", "what is BANANA42?")
    repo.add_message(tid, "assistant", "a code word")
    out = mcp.get_chat_history("c", 1)
    assert out["total"] == 2
    assert "**User:** what is BANANA42?" in out["markdown"]
    assert "**Assistant:** a code word" in out["markdown"]
    assert mcp.get_chat_history("c", 999)["total"] == 0       # no thread -> empty, not error


def test_fresh_session_with_history_hints_the_tool_not_stuffing(wired, tmp_path, monkeypatch):
    """A cold/stale resume must NOT stuff history into the prompt; it sends only the new
    turn and points the agent at get_chat_history via a system-prompt hint."""
    import app.agent_skills as ag
    monkeypatch.setattr(ag, "APP_DIR", tmp_path)
    monkeypatch.setattr(pdf_store, "ensure_cached", lambda pid: tmp_path / "p.pdf")
    (tmp_path / "p.pdf").write_bytes(b"%PDF-1.4")

    captured = {}

    class HealEngine(engine_mod.ClaudeCodeEngine):
        def stream_events(self, messages, *, session_id=None, resume=False, system=None, **kw):
            if resume:
                yield {"type": "error", "text": "No conversation found with session ID: x"}
            else:
                captured["messages"] = messages; captured["system"] = system
                yield {"type": "token", "text": "ok"}
                yield {"type": "done", "session_id": "fresh-sid", "text": "ok"}

    tid = repo.get_or_create_thread("c", 1)
    repo.add_message(tid, "user", "earlier question")
    repo.add_message(tid, "assistant", "earlier answer")
    repo.set_session_id(tid, "dead-session-id")

    agent = paper_chat.ClaudeCodePaperAgent(HealEngine())
    list(agent.stream("c", "C", tid, 1, "P1", "follow-up"))

    assert captured["messages"] == [{"role": "user", "content": "follow-up"}]  # only the new turn
    assert "get_chat_history" in captured["system"] and "RESUMING" in captured["system"]
    assert repo.get_session_id(tid) == "fresh-sid"


def test_brand_new_session_has_no_resume_hint(wired, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_store, "ensure_cached", lambda pid: tmp_path / "p.pdf")
    (tmp_path / "p.pdf").write_bytes(b"%PDF-1.4")
    captured = {}

    class Eng(engine_mod.ClaudeCodeEngine):
        def run_once(self, messages, *, session_id=None, resume=False, system=None, **kw):
            captured["system"] = system
            return EngineResult(text="hi", session_id="sid")

    tid = repo.get_or_create_thread("c", 1)                   # no prior messages
    paper_chat.ClaudeCodePaperAgent(Eng()).answer("c", "C", tid, 1, "P1", "first")
    assert "RESUMING" not in captured["system"]               # nothing to resume


def test_tool_status_labels_and_suppresses_internal():
    from app.engine import _tool_status
    assert _tool_status("Read") == "reading the PDF…"
    assert _tool_status("mcp__pa__read_paper_text") == "reading the paper…"
    assert _tool_status("ToolSearch") == ""        # internal noise suppressed
    assert _tool_status("Task") == ""


def test_first_turn_starts_session_then_resumes(wired, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_store, "ensure_cached", lambda pid: tmp_path / "paper.pdf")
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4")
    spy = SpyEngine()
    agent = paper_chat.ClaudeCodePaperAgent(spy)
    tid = repo.get_or_create_thread("c", 1)

    r1 = agent.answer("c", "C", tid, 1, "P1", "first question")
    assert r1.reply == "answer"
    assert spy.calls[0]["resume"] is False                       # first turn STARTS a session
    assert spy.calls[0]["messages"] == [{"role": "user", "content": "first question"}]  # only the new turn
    assert "mcp__pa__submit_proposal" not in spy.calls[0]["tools"]   # read-only: no write tools
    assert "Bash" not in spy.calls[0]["tools"]                       # never a shell
    assert "Read" in spy.calls[0]["tools"]
    assert "mcp__pa__read_paper_text" in spy.calls[0]["tools"]       # no-shell PDF text path
    assert repo.get_session_id(tid) == "sid-echoed"              # session id persisted

    agent.answer("c", "C", tid, 1, "P1", "second question")
    assert spy.calls[1]["resume"] is True                        # later turn RESUMES
    assert spy.calls[1]["session_id"] == "sid-echoed"            # the stored session
