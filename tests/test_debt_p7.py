"""AGENTIC_PLAN Phase 7 — reading-debt queue + quarantined brainstorm."""
from __future__ import annotations

import pytest

import app.annotations as ann_mod
import app.debt as debt
import app.library as library
import app.mcp_server as mcp
import app.notes as notes_mod
import app.provenance as provenance
import app.thoughts as thoughts_mod
import app.wiki as wiki
from app.db import connect, init_db


@pytest.fixture
def wired(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    cols = tmp_path / "collections"
    for mod in (notes_mod, wiki, ann_mod, mcp, library, debt):
        monkeypatch.setattr(mod, "connect", lambda: connect(db))
    for mod in (notes_mod, thoughts_mod, wiki):
        monkeypatch.setattr(mod, "COLLECTIONS_DIR", cols)
    import app.agent_skills as agent_skills              # debt/brainstorm cwd = a skills-home
    monkeypatch.setattr(agent_skills, "APP_DIR", tmp_path)
    con = connect(db)
    con.execute("INSERT INTO collections(slug,name) VALUES('c','C')")
    con.execute("INSERT INTO papers(id,title) VALUES(1,'P1')")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('c',1)")
    con.commit(); con.close()
    return {"db": db, "cols": cols}


# --- data layer: idempotent upsert + status ------------------------------------
def test_upsert_dedupes_by_sources_and_ignore_sticks(wired):
    d1 = debt.upsert_debt("c", "what connects A and B?", ["note:1", "thought:t"])
    d2 = debt.upsert_debt("c", "reworded but same sources", ["thought:t", "note:1"])
    assert d1 == d2                                   # order-independent hash
    assert debt.count_open("c") == 1
    debt.set_status("c", d1, "ignored")
    debt.upsert_debt("c", "same sources again", ["note:1", "thought:t"])
    assert debt.count_open("c") == 0                  # ignored item not re-surfaced


def test_fill_creates_reasoning_human_thought_and_marks_filled(wired):
    did = debt.upsert_debt("c", "your take?", ["note:1"])
    tid = debt.fill_debt("c", did, "I think A subsumes B.")
    t = thoughts_mod.get_thought("c", tid)
    assert (t["synth_kind"], t["author_origin"]) == ("reasoning", "human")
    assert t["prompted_by"] == "note:1"
    assert debt.get_debt("c", did)["status"] == "filled"
    # and that thought can ground a synthesis (resolver agrees)
    assert provenance.effective_stamp({"type": "thought", "id": tid}, "c") == ("reasoning", "human")


# --- deterministic find fallback (no agent) ------------------------------------
def test_find_deterministic_groups_seeds_by_paper(wired, monkeypatch):
    notes_mod.save_note("c", 1, "a seed summary", "", "", "noted")   # seed note on paper 1
    # force the non-agentic path (codex isn't in debt._AGENTIC)
    monkeypatch.setattr(debt, "load_config", lambda: {"engine": "codex"})
    import app.engine as engine_mod
    monkeypatch.setattr(engine_mod, "build_engine",
                        lambda cfg: type("E", (engine_mod.FakeEngine,), {"name": "codex"})())
    res = debt.find_debt("c")
    assert res["new"] >= 1
    items = debt.list_debt("c")
    assert any("note:1" in d["sources"] for d in items)


# --- brainstorm is gate-exempt + quarantined -----------------------------------
def test_brainstorm_pages_quarantined_and_labeled(wired):
    out = wiki.brainstorm_pages("c", [{"title": "Maybe a link?", "slug": "link",
                                       "body": "DR and sysid might both target the gap.",
                                       "sources": ["note:1"]}])
    assert out["pages"] == ["brainstorming/link.md"]
    props = wiki.list_proposed("c")
    p = next(p for p in props if p["section"] == "brainstorming")
    assert "Speculative" in p["new_content"] and "machine-generated" in p["new_content"]
    assert "author_origin: agent" in p["new_content"]
    # accept writes it under wiki/brainstorming/, NOT a grounded section
    assert wiki.accept_proposed("c", p["id"])
    assert (wired["cols"] / "c" / "wiki" / "brainstorming" / "link.md").exists()


# --- MCP tools route to the right place ----------------------------------------
def test_mcp_submit_debt_and_brainstorm(wired):
    r = mcp.submit_debt("c", [{"question": "q1?", "sources": ["note:1"]}])
    assert r["written"] == 1 and debt.count_open("c") == 1
    b = mcp.submit_brainstorm("c", [{"title": "B", "slug": "b", "body": "idea", "sources": ["note:1"]}])
    assert b["pages"] == ["brainstorming/b.md"]
    # both tools are advertised
    names = {t["name"] for t in mcp._TOOLS}
    assert {"submit_debt", "submit_brainstorm"} <= names
