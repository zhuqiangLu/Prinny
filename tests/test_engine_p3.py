"""AGENTIC_PLAN Phase 3 — the Engine seam + CLI swap.

Covers engine selection, message flattening for CLI engines, the Claude Code
stream-json parser, graceful degradation when a binary is missing, and that
llm.complete delegates to the selected engine (with FakeEngine, no real CLI).
"""
from __future__ import annotations

import pytest

import app.engine as engine_mod
import app.llm as llm
from app.engine import (
    ClaudeCodeEngine,
    CodexEngine,
    EngineError,
    FakeEngine,
    _parse_claude_stream_json,
    _split_system,
    build_engine,
    select_engine_name,
)


# --- selection ------------------------------------------------------------------
def test_select_engine_explicit_and_auto():
    assert select_engine_name({"engine": "codex"}) == "codex"
    # CLI-only: default is claude-code; no API backend
    assert select_engine_name({"engine": ""}) == "claude-code"
    assert select_engine_name({"engine": "bogus"}) == "claude-code"
    assert select_engine_name({"engine": "openai"}) == "claude-code"  # removed -> default


def test_build_engine_returns_selected_type():
    assert isinstance(build_engine({"engine": "claude-code"}), ClaudeCodeEngine)
    assert isinstance(build_engine({"engine": "codex"}), CodexEngine)


# --- message flattening ---------------------------------------------------------
def test_split_system_single_turn():
    sys, prompt = _split_system([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hello"},
    ])
    assert sys == "be terse"
    assert prompt == "hello"   # single non-system turn collapses to its content


def test_split_system_multi_turn_is_labeled():
    _, prompt = _split_system([
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ])
    assert prompt == "USER: a\n\nASSISTANT: b\n\nUSER: c"


# --- Claude Code stream-json parser --------------------------------------------
def test_parse_claude_stream_json_prefers_result_event():
    out = "\n".join([
        '{"type":"system","session_id":"abc"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"part "}]}}',
        '{"type":"result","result":"final answer","session_id":"abc",'
        '"usage":{"input_tokens":12,"output_tokens":5}}',
    ])
    res = _parse_claude_stream_json(out)
    assert res.text == "final answer"
    assert res.session_id == "abc"
    assert res.usage == {"prompt_tokens": 12, "completion_tokens": 5}


def test_parse_claude_stream_json_falls_back_to_assistant_text():
    out = "\n".join([
        '{"type":"assistant","message":{"content":[{"type":"text","text":"a"}]}}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"b"}]}}',
        "not json — ignored",
    ])
    assert _parse_claude_stream_json(out).text == "ab"


# --- graceful degradation -------------------------------------------------------
def test_missing_binary_reports_unavailable_and_errors():
    eng = ClaudeCodeEngine(binary="definitely-not-a-real-binary-xyz")
    ok, _ = eng.available()
    assert ok is False
    with pytest.raises(EngineError):
        eng.run_once([{"role": "user", "content": "hi"}])


# --- llm.complete delegates to the selected engine -----------------------------
def test_llm_complete_delegates(monkeypatch):
    fake = FakeEngine(reply="delegated reply")
    monkeypatch.setattr(llm, "_engine", lambda: fake)
    assert llm.complete([{"role": "user", "content": "x"}]) == "delegated reply"
    assert fake.calls and fake.calls[0]["allowed_tools"] is None  # tool-less by default


def test_llm_complete_wraps_engine_error_as_llmerror(monkeypatch):
    class Boom(FakeEngine):
        def run_once(self, *a, **k):
            raise EngineError("kaboom")
    monkeypatch.setattr(llm, "_engine", lambda: Boom())
    with pytest.raises(llm.LLMError):
        llm.complete([{"role": "user", "content": "x"}])


def test_engine_status_shape(monkeypatch):
    monkeypatch.setattr(llm, "_engine", lambda: FakeEngine())
    st = llm.engine_status()
    assert st["name"] == "fake" and st["ok"] is True
