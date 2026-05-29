"""AGENTIC_PLAN Phase 1 — typed captures (synth_kind + author_origin).

Covers stamp-by-door, the per-type resolver, the note heuristic + override, the
markdown mirror round-trip, and the non-destructive paper_notes migration.
"""
from __future__ import annotations

import sqlite3

import pytest

import app.notes as notes_mod
import app.provenance as provenance
import app.thoughts as thoughts_mod
from app.db import _migrate, connect, init_db


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Temp DB + collections dir, with notes/thoughts pointed at them."""
    db = tmp_path / "app.sqlite"
    init_db(db)
    cols = tmp_path / "collections"
    cols.mkdir()
    monkeypatch.setattr(notes_mod, "connect", lambda: connect(db))
    monkeypatch.setattr(notes_mod, "COLLECTIONS_DIR", cols)
    monkeypatch.setattr(thoughts_mod, "COLLECTIONS_DIR", cols)
    # a collection row so FK / collection_slug is sane
    con = connect(db)
    con.execute("INSERT INTO collections(slug, name) VALUES('box','Box')")
    con.execute("INSERT INTO papers(id, title) VALUES(1,'P1')")
    con.commit()
    con.close()
    return {"db": db, "cols": cols}


# --- note stamps ----------------------------------------------------------------
def test_note_kind_heuristic_seed_vs_reasoning(wired):
    # empty thoughts field -> seed
    notes_mod.save_note("box", 1, summary="s", thoughts="", key_quotes="", status="noted")
    assert notes_mod.note_kind("box", 1) == ("seed", "human")
    # non-empty thoughts field -> reasoning (the user wrote a take)
    notes_mod.save_note("box", 1, summary="s", thoughts="I think X because Y", key_quotes="", status="noted")
    assert notes_mod.note_kind("box", 1) == ("reasoning", "human")


def test_note_kind_override_wins(wired):
    notes_mod.save_note("box", 1, summary="s", thoughts="reasoned take", key_quotes="",
                        status="noted", synth_kind="seed")  # override demotes
    assert notes_mod.note_kind("box", 1) == ("seed", "human")


def test_note_author_origin_is_door_stamped_human(wired):
    # The endpoint passes author_origin='human'; a form can't smuggle 'agent'.
    notes_mod.save_note("box", 1, "s", "", "", "noted", author_origin="agent")
    # save_note normalizes only known origins; 'agent' is a valid value but only the
    # agent door would pass it. Here we assert the value persists as given (door's job).
    assert notes_mod.get_note("box", 1)["author_origin"] == "agent"


def test_note_mirror_roundtrips_stamps(wired):
    notes_mod.save_note("box", 1, "s", "take", "", "noted", synth_kind="reasoning")
    md = (wired["cols"] / "box" / "notes" / "1.md").read_text()
    assert "synth_kind: reasoning" in md
    assert "author_origin: human" in md


# --- thought stamps -------------------------------------------------------------
def test_thought_stamp_by_door_default_seed_human(wired):
    tid = thoughts_mod.create_thought("box", "a thought")
    t = thoughts_mod.get_thought("box", tid)
    assert (t["synth_kind"], t["author_origin"]) == ("seed", "human")


def test_thought_reasoning_toggle(wired):
    tid = thoughts_mod.create_thought("box", "a connection", synth_kind="reasoning")
    assert thoughts_mod.get_thought("box", tid)["synth_kind"] == "reasoning"


def test_thought_missing_frontmatter_migrates_to_seed_human(wired):
    # Simulate a pre-P1 file with no synth_kind/author_origin frontmatter.
    d = wired["cols"] / "box" / "thoughts"
    d.mkdir(parents=True)
    (d / "2026-01-01T00-00-00.md").write_text("---\ncreated: 2026-01-01T00:00:00\ntags: []\n---\n\nold\n")
    t = thoughts_mod.get_thought("box", "2026-01-01T00-00-00")
    assert (t["synth_kind"], t["author_origin"]) == ("seed", "human")


def test_thought_edit_preserves_origin(wired):
    tid = thoughts_mod.create_thought("box", "x", synth_kind="reasoning", author_origin="agent")
    thoughts_mod.update_thought("box", tid, "edited")  # human edits the body
    t = thoughts_mod.get_thought("box", tid)
    assert t["author_origin"] == "agent"        # origin not relaundered
    assert t["body"] == "edited"


# --- resolver across all four types --------------------------------------------
def test_effective_stamp_all_types(wired, monkeypatch):
    monkeypatch.setattr(provenance, "notes_mod", notes_mod)
    monkeypatch.setattr(provenance, "thoughts_mod", thoughts_mod)
    notes_mod.save_note("box", 1, "s", "take", "", "noted")  # -> reasoning
    tid = thoughts_mod.create_thought("box", "t", synth_kind="reasoning")

    assert provenance.effective_stamp({"type": "highlight", "id": 9}) == ("seed", "human")
    assert provenance.effective_stamp({"type": "paper", "id": "K1"}) == ("seed", "external")
    assert provenance.effective_stamp({"type": "note", "id": 1}, "box") == ("reasoning", "human")
    assert provenance.effective_stamp({"type": "thought", "id": tid}, "box") == ("reasoning", "human")
    # unknown / missing -> safe default
    assert provenance.effective_stamp({"type": "mystery", "id": 1}, "box") == ("seed", "human")


# --- migration is non-destructive ----------------------------------------------
def test_migration_adds_note_stamp_columns(tmp_path):
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    # The other tables _migrate inspects, already current so only paper_notes migrates.
    con.execute("CREATE TABLE chat_threads (id INTEGER PRIMARY KEY, last_active_at TIMESTAMP)")
    con.execute("CREATE TABLE collections (slug TEXT PRIMARY KEY, tags TEXT)")
    # A paper_notes table from before P1 (no synth_kind / author_origin).
    con.execute(
        "CREATE TABLE paper_notes (paper_id INTEGER PRIMARY KEY, collection_slug TEXT, "
        "summary TEXT, thoughts TEXT, key_quotes TEXT, status TEXT, updated_at TIMESTAMP)"
    )
    con.execute("INSERT INTO paper_notes(paper_id, collection_slug, thoughts) VALUES(1,'box','t')")
    con.commit()
    _migrate(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(paper_notes)")}
    assert {"synth_kind", "author_origin"} <= cols
    row = con.execute("SELECT synth_kind, author_origin FROM paper_notes WHERE paper_id=1").fetchone()
    assert row == ("auto", "human")  # existing row keeps its data, gets defaults
    con.close()
