"""AGENTIC_PLAN Phase 5 — agentic organizing pass.

Drives organizer.organize with a fake "agent" engine that mimics the real one: it
reads the collection slug from the MCP config it's handed and calls submit_proposal,
so the gate runs and proposals land in the queue. Also covers the tool-less fallback
and graceful degradation. No real CLI needed.
"""
from __future__ import annotations

import pytest

import app.annotations as ann_mod
import app.engine as engine_mod
import app.library as library
import app.mcp_server as mcp
import app.notes as notes_mod
import app.organizer as organizer
import app.thoughts as thoughts_mod
import app.wiki as wiki
from app.db import connect, init_db
from app.engine import EngineResult, FakeEngine


@pytest.fixture
def wired(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    cols = tmp_path / "collections"
    for mod in (notes_mod, wiki, ann_mod, mcp, library):
        monkeypatch.setattr(mod, "connect", lambda: connect(db))
    for mod in (notes_mod, thoughts_mod, wiki):
        monkeypatch.setattr(mod, "COLLECTIONS_DIR", cols)
    import app.agent_skills as agent_skills              # organizer's cwd = a skills-home
    monkeypatch.setattr(agent_skills, "APP_DIR", tmp_path)
    con = connect(db)
    con.execute("INSERT INTO collections(slug,name) VALUES('c','C')")
    con.execute("INSERT INTO papers(id,title) VALUES(1,'P1')")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('c',1)")
    con.commit(); con.close()
    return {"db": db, "cols": cols}


class FakeAgent(FakeEngine):
    """Mimics a CLI agent: reads the bound collection from the MCP config and calls
    submit_proposal (so the gate runs), exactly as the real agent would over stdio."""
    name = "claude-code"

    def run_once(self, messages, *, mcp_config=None, **kw) -> EngineResult:
        slug = mcp_config["mcpServers"]["pa"]["env"]["PA_MCP_COLLECTION"]
        mcp.submit_proposal(slug, [{
            "section": "problems", "slug": "eff", "title": "Efficiency",
            "claims": [
                {"text": "P1 reports R.", "claim_type": "attributed", "papers": ["1"]},
                {"text": "unsupported.", "claim_type": "attributed", "papers": ["GHOST"]},
            ]}])
        return EngineResult(text="Proposed 1 page.")


def test_organize_agentic_runs_gate_and_queues(wired, monkeypatch):
    monkeypatch.setattr(organizer, "load_config", lambda: {"engine": "claude-code"})
    monkeypatch.setattr(engine_mod, "build_engine", lambda cfg: FakeAgent())
    res = organizer.organize("c")
    assert res["agentic"] is True and res["engine"] == "claude-code"
    assert res["new_proposals"] == ["problems/eff.md"]   # the gated survivor
    props = wiki.list_proposed("c")
    assert len(props) == 1
    texts = [c["text"] for c in props[0]["claims"]]
    assert "P1 reports R." in texts and "unsupported." not in texts  # gate dropped the ghost
    # nothing applied to the wiki
    assert not (wired["cols"] / "c" / "wiki" / "problems" / "eff.md").exists()


def test_organize_falls_back_to_two_step_for_non_agentic_engine(wired, monkeypatch):
    # codex isn't in organizer._AGENTIC -> tool-less two-step fallback
    monkeypatch.setattr(organizer, "load_config", lambda: {"engine": "codex"})
    monkeypatch.setattr(engine_mod, "build_engine",
                        lambda cfg: type("E", (FakeEngine,), {"name": "codex"})())
    called = {}
    def fake_rg(slug, mode):
        called["args"] = (slug, mode)
        return []
    monkeypatch.setattr(wiki, "run_generation", fake_rg)
    res = organizer.organize("c", "incremental")
    assert res["agentic"] is False and called["args"] == ("c", "incremental")


def test_organize_raises_when_engine_unavailable(wired, monkeypatch):
    class Down(FakeEngine):
        name = "claude-code"
        def available(self):
            return False, "claude not on PATH"
    monkeypatch.setattr(organizer, "load_config", lambda: {"engine": "claude-code"})
    monkeypatch.setattr(engine_mod, "build_engine", lambda cfg: Down())
    with pytest.raises(organizer.llm.LLMError):
        organizer.organize("c")
