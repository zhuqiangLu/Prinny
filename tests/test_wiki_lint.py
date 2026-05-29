"""Deep wiki lint (heuristic, report-only) — engine dispatch.

The agent pass runs only on an MCP-capable CLI engine (claude-code) and is READ-ONLY
(no submit tools). Non-agentic engines return the deterministic checks only.
"""
from __future__ import annotations

import app.agent_skills as agent_skills
import app.engine as engine_mod
import app.wiki_lint as wiki_lint


def test_deep_lint_agentic_runs_readonly(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_skills, "APP_DIR", tmp_path)
    monkeypatch.setattr(wiki_lint, "load_config", lambda: {"engine": "claude-code"})

    captured = {}

    class Eng(engine_mod.FakeEngine):
        name = "claude-code"
        def run_once(self, messages, **kw):
            captured.update(kw)
            return engine_mod.EngineResult(text="- Pages A and B disagree on X.")

    monkeypatch.setattr(engine_mod, "build_engine", lambda cfg: Eng())
    out = wiki_lint.deep_lint("c")
    assert out["agentic"] is True
    assert "disagree" in out["report"]
    # read-only: no submit_* in the allowlist
    assert all("submit_" not in t for t in captured["allowed_tools"])
    assert "mcp__pa__read_wiki_page" in captured["allowed_tools"]


def test_deep_lint_non_agentic_returns_no_report(monkeypatch):
    monkeypatch.setattr(wiki_lint, "load_config", lambda: {"engine": "codex"})
    monkeypatch.setattr(engine_mod, "build_engine",
                        lambda cfg: type("E", (engine_mod.FakeEngine,), {"name": "codex"})())
    out = wiki_lint.deep_lint("c")
    assert out["agentic"] is False and out["report"] == ""
