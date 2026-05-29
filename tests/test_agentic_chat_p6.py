"""AGENTIC_PLAN Phase 6 — /{collection} agentic chat + attribution-safe capture."""
from __future__ import annotations

import pytest

import app.agentic_chat as agentic_chat
import app.engine as engine_mod
import app.thoughts as thoughts_mod
from app.engine import EngineResult, FakeEngine


# --- prefix parsing -------------------------------------------------------------
def test_parse_prefix_detects_slug():
    assert agentic_chat.parse_prefix("/robotics what are the gaps?") == ("robotics", "what are the gaps?")
    assert agentic_chat.parse_prefix("  /vlms hi") == ("vlms", "hi")


def test_parse_prefix_ignores_plain_and_reserved():
    assert agentic_chat.parse_prefix("no prefix here") == (None, "no prefix here")
    assert agentic_chat.parse_prefix("/collection summarize") == (None, "/collection summarize")
    assert agentic_chat.parse_prefix("/wiki") == (None, "/wiki")


# --- agentic answer uses read-only tools (no submit_proposal) -------------------
def test_answer_passes_readonly_tools(monkeypatch):
    captured = {}

    class Spy(FakeEngine):
        name = "claude-code"
        def run_once(self, messages, *, allowed_tools=None, mcp_config=None, **kw):
            captured["tools"] = allowed_tools
            captured["slug"] = mcp_config["mcpServers"]["pa"]["env"]["PA_MCP_COLLECTION"]
            captured["messages"] = messages
            return EngineResult(text="grounded answer")

    monkeypatch.setattr(agentic_chat, "load_config", lambda: {"engine": "claude-code"})
    monkeypatch.setattr(engine_mod, "build_engine", lambda cfg: Spy())
    out = agentic_chat.answer("robotics", [{"role": "user", "content": "earlier"}], "now?")
    assert out == "grounded answer"
    assert "mcp__pa__submit_proposal" not in captured["tools"]      # chat never proposes
    assert "mcp__pa__search_fragments" in captured["tools"]
    assert captured["slug"] == "robotics"
    assert captured["messages"][-1] == {"role": "user", "content": "now?"}


def test_answer_raises_when_engine_unavailable(monkeypatch):
    class Down(FakeEngine):
        name = "claude-code"
        def available(self):
            return False, "no claude"
    monkeypatch.setattr(agentic_chat, "load_config", lambda: {"engine": "claude-code"})
    monkeypatch.setattr(engine_mod, "build_engine", lambda cfg: Down())
    with pytest.raises(agentic_chat.llm.LLMError):
        agentic_chat.answer("robotics", [], "q")


# --- attribution-safe capture ---------------------------------------------------
def test_capture_stamps_agent_seed_and_human_reasoning(tmp_path, monkeypatch):
    monkeypatch.setattr(thoughts_mod, "COLLECTIONS_DIR", tmp_path)
    # one tap: agent reply only -> (seed, agent)
    seed = thoughts_mod.create_thought("c", "the agent's synthesis", synth_kind="seed", author_origin="agent")
    st = thoughts_mod.get_thought("c", seed)
    assert (st["synth_kind"], st["author_origin"]) == ("seed", "agent")
    # 'your take' -> (reasoning, human) linked to the seed via prompted_by
    take = thoughts_mod.create_thought("c", "my reasoning", synth_kind="reasoning",
                                       author_origin="human", prompted_by=seed)
    tt = thoughts_mod.get_thought("c", take)
    assert (tt["synth_kind"], tt["author_origin"]) == ("reasoning", "human")
    assert tt["prompted_by"] == seed


def test_agent_seed_cannot_ground_synthesis_but_its_take_can(tmp_path, monkeypatch):
    """The whole point: an agent-captured seed can't assert; the human take can."""
    import app.provenance as provenance
    monkeypatch.setattr(thoughts_mod, "COLLECTIONS_DIR", tmp_path)
    monkeypatch.setattr(provenance, "thoughts_mod", thoughts_mod)
    seed = thoughts_mod.create_thought("c", "agent idea", synth_kind="seed", author_origin="agent")
    take = thoughts_mod.create_thought("c", "human reasoning", synth_kind="reasoning",
                                       author_origin="human", prompted_by=seed)
    assert provenance.effective_stamp({"type": "thought", "id": seed}, "c") == ("seed", "agent")
    assert provenance.effective_stamp({"type": "thought", "id": take}, "c") == ("reasoning", "human")
