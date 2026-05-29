"""Phase 1.5 — app annotation store + Zotero read-in (read-only)."""

from __future__ import annotations

import json
import sqlite3

import app.annotations as ann
from app.db import connect, init_db
from app.zotero import LocalZotero


def _isolate(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    # annotations.paper_id is an FK to papers(id) — seed one.
    con = connect(db)
    con.execute("INSERT INTO papers (id, title, origin) VALUES (1, 'P1', 'app-created')")
    con.commit()
    con.close()
    monkeypatch.setattr(ann, "connect", lambda: connect(db))
    return db


def test_create_list_update_delete(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pos = json.dumps({"pageIndex": 0, "rects": [[10, 20, 100, 32]]})
    a = ann.create("vlms", 1, kind="highlight", color="#ffd400", page=0,
                   position_json=pos, selected_text="hi")
    assert a["origin"] == "app"

    rows = ann.list_app(1)
    assert len(rows) == 1

    ann.update(a["id"], note_text="my note")
    assert ann.get(a["id"])["note_text"] == "my note"

    assert ann.delete(a["id"]) is True
    assert ann.list_app(1) == []


def test_to_client_parses_position(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pos = json.dumps({"pageIndex": 2, "rects": [[1, 2, 3, 4]]})
    a = ann.create("vlms", 1, kind="highlight", color=None, page=2, position_json=pos)
    client = ann.to_client(a)
    assert client["position"]["pageIndex"] == 2
    assert client["position"]["rects"] == [[1, 2, 3, 4]]


def _zotero_fixture(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT);
        CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER,
          contentType TEXT);
        CREATE TABLE itemAnnotations (itemID INTEGER, parentItemID INTEGER, type INTEGER,
          color TEXT, position TEXT, text TEXT, comment TEXT);
        CREATE TABLE deletedItems (itemID INTEGER);
        INSERT INTO items VALUES (1, 'PAPER1');
        INSERT INTO items VALUES (2, 'ATTACH1');
        INSERT INTO itemAttachments VALUES (2, 1, 'application/pdf');
        INSERT INTO itemAnnotations VALUES
          (10, 2, 1, '#ff6666', '{"pageIndex":0,"rects":[[1,2,3,4]]}', 'quoted', 'cmt');
        """
    )
    con.commit()
    con.close()


def test_zotero_read_annotations(tmp_path):
    db = tmp_path / "zotero.sqlite"
    _zotero_fixture(str(db))
    z = LocalZotero(str(db), "http://127.0.0.1:1")
    anns = z.read_annotations("PAPER1")
    assert len(anns) == 1
    a = anns[0]
    assert a["origin"] == "zotero"
    assert a["kind"] == "highlight"
    assert a["color"] == "#ff6666"
    assert a["selected_text"] == "quoted"
    assert a["note_text"] == "cmt"
    assert json.loads(a["position_json"])["pageIndex"] == 0
