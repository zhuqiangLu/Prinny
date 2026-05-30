"""Agents page: registry shape + editable-skill override (save/reset, loader prefers override)."""
from __future__ import annotations

import app.agent_skills as agent_skills
import app.agents as agents


def test_registry_lists_all_agents_with_real_tools():
    items = agents.list_agents()
    keys = {a["key"] for a in items}
    # Post-cleanup registry: the notes-pipeline agents (organizer/debt/brainstorm/lint)
    # were removed with the pipeline; only the read-only paper/chat agents and the
    # one-shot wiki drafter remain.
    assert keys == {"paper", "chat", "wiki"}
    paper = next(a for a in items if a["key"] == "paper")
    assert "Read" in [t["name"] for t in paper["tools"]]          # real allowlist
    assert any(s["name"] == "summarize-section" for s in paper["skills"])
    assert next(a for a in items if a["key"] == "wiki")["tools"] == []  # one-shot, no tools
    # No agent carries a write tool anymore (the lethal-trifecta surface is gone).
    assert all(not t["write"] for a in items for t in a["tools"])


def test_skill_override_save_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_skills, "_USER", tmp_path / "skills")
    name = "field-model"
    assert agent_skills.is_customized(name) is False
    shipped = agent_skills.read_skill(name)["body"]

    agent_skills.save_skill(name, "MY CUSTOM INSTRUCTIONS", description="custom desc")
    assert agent_skills.is_customized(name) is True
    sk = agent_skills.read_skill(name)
    assert sk["body"] == "MY CUSTOM INSTRUCTIONS" and sk["description"] == "custom desc"
    assert agent_skills.skill_body(name) == "MY CUSTOM INSTRUCTIONS"   # loader prefers override

    agent_skills.reset_skill(name)
    assert agent_skills.is_customized(name) is False
    assert agent_skills.read_skill(name)["body"] == shipped            # back to shipped


def test_save_unknown_skill_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_skills, "_USER", tmp_path / "skills")
    import pytest
    with pytest.raises(ValueError):
        agent_skills.save_skill("not-a-real-skill", "x")
