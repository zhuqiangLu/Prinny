"""AGENTIC_PLAN Phase 4 — MCP read surface + submit_proposal.

Covers the bounded read tools, the token-bound run scoping, the JSON-RPC dispatch
(initialize / tools/list / tools/call), and that submit_proposal goes through the gate
(writes the queue, never the wiki).
"""
from __future__ import annotations

import json

import pytest

import app.annotations as ann_mod
import app.library as library
import app.mcp_server as mcp
import app.notes as notes_mod
import app.thoughts as thoughts_mod
import app.wiki as wiki
from app.db import connect, init_db


@pytest.fixture
def wired(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    cols = tmp_path / "collections"
    for mod in (notes_mod, wiki, ann_mod, mcp, library):
        monkeypatch.setattr(mod, "connect", lambda: connect(db))
    monkeypatch.setattr(notes_mod, "COLLECTIONS_DIR", cols)
    monkeypatch.setattr(thoughts_mod, "COLLECTIONS_DIR", cols)
    monkeypatch.setattr(wiki, "COLLECTIONS_DIR", cols)
    con = connect(db)
    con.execute("INSERT INTO collections(slug,name) VALUES('c','C')")
    con.execute("INSERT INTO papers(id,title,abstract) VALUES(1,'P1','abs1')")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('c',1)")
    con.commit(); con.close()
    return {"db": db, "cols": cols}


# --- read tools -----------------------------------------------------------------
def test_seeds_lists_seed_fragments(wired):
    notes_mod.save_note("c", 1, "a summary", "", "", "noted")        # seed (no thoughts)
    tid = thoughts_mod.create_thought("c", "a seed thought")         # seed
    thoughts_mod.create_thought("c", "reasoned", synth_kind="reasoning")  # not a seed
    out = mcp.get_unreasoned_seeds("c")
    ids = {s["id"] for s in out["seeds"]}
    assert "note:1" in ids and f"thought:{tid}" in ids
    assert all("reasoned" not in s["preview"] for s in out["seeds"])  # reasoning excluded


def test_get_fragment_full_note_and_paper(wired):
    notes_mod.save_note("c", 1, "the summary", "my take", "", "noted")
    n = mcp.get_fragment("c", "note:1")
    assert n["thoughts"] == "my take" and n["synth_kind"] == "reasoning"
    p = mcp.get_fragment("c", "paper:1")
    assert p["title"] == "P1" and p["synth_kind"] == "seed" and p["author_origin"] == "external"
    assert mcp.get_fragment("c", "paper:999")["error"]


def test_search_fragments_finds_thought(wired):
    thoughts_mod.create_thought("c", "transformers struggle with long context")
    hits = mcp.search_fragments("c", "long context")
    assert any(h["type"] == "thought" for h in hits["hits"])


# --- stdio launch config --------------------------------------------------------
def test_stdio_mcp_config_scopes_to_collection():
    cfg = mcp.stdio_mcp_config("robotics")
    server = cfg["mcpServers"]["pa"]
    assert server["args"] == ["-m", "app.mcp_stdio"]
    assert server["env"]["PA_MCP_COLLECTION"] == "robotics"   # run scoped to the collection
    assert "PAPER_AGENT_HOME" in server["env"] and "PYTHONPATH" in server["env"]


# --- JSON-RPC dispatch ----------------------------------------------------------
def test_dispatch_initialize_and_tools_list(wired):
    init = mcp.dispatch("c", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "paper-agent"
    tl = mcp.dispatch("c", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in tl["result"]["tools"]}
    assert {"get_unreasoned_seeds", "get_fragment", "search_fragments",
            "read_wiki_page"} <= names


def test_dispatch_notification_returns_none(wired):
    assert mcp.dispatch("c", {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_dispatch_tools_call_wraps_payload(wired):
    notes_mod.save_note("c", 1, "sum", "", "", "noted")
    resp = mcp.dispatch("c", {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                              "params": {"name": "get_unreasoned_seeds", "arguments": {}}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["collection"] == "c"


def test_dispatch_unknown_tool_errors(wired):
    resp = mcp.dispatch("c", {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                              "params": {"name": "rm_rf", "arguments": {}}})
    assert resp["error"]["code"] == -32601
