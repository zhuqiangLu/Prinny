"""Notes storage + DB↔file two-way sync."""

from __future__ import annotations

import os
import time

import app.notes as notes
from app.db import connect, init_db


def _isolate(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    con = connect(db)
    con.execute("INSERT INTO papers (id, title, origin) VALUES (1, 'P1', 'zotero-import')")
    con.commit()
    con.close()
    monkeypatch.setattr(notes, "connect", lambda: connect(db))
    monkeypatch.setattr(notes, "COLLECTIONS_DIR", tmp_path / "collections")


def test_save_writes_db_and_file(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    notes.save_note("vlms", 1, "sum", "think", "- q", "reading")

    got = notes.get_note("vlms", 1)
    assert got["summary"] == "sum"
    assert got["status"] == "reading"

    mirror = tmp_path / "collections" / "vlms" / "notes" / "1.md"
    assert mirror.exists()
    assert "## Summary" in mirror.read_text()


def test_file_edit_wins_on_newer_mtime(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    notes.save_note("vlms", 1, "old", "", "", "noted")

    mirror = tmp_path / "collections" / "vlms" / "notes" / "1.md"
    # simulate an out-of-band Obsidian edit with a clearly newer mtime
    mirror.write_text(
        "---\npaper_id: 1\nstatus: noted\nupdated_at: 2030-01-01T00:00:00\n---\n\n"
        "## Summary\n\nedited in obsidian\n\n## Thoughts\n\n\n\n## Key Quotes\n\n\n",
        encoding="utf-8",
    )
    future = time.time() + 10
    os.utime(mirror, (future, future))

    got = notes.get_note("vlms", 1)
    assert got["summary"] == "edited in obsidian"
    # and it synced back into the DB
    row = connect(tmp_path / "app.sqlite").execute(
        "SELECT summary FROM paper_notes WHERE paper_id=1"
    ).fetchone()
    assert row[0] == "edited in obsidian"
