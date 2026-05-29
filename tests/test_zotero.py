"""Integration test for the Zotero SQLite read path, using a fixture DB.

We build a minimal sqlite that mimics Zotero's ``collections`` table and assert
that ``LocalZotero`` reads it via the SQLite fallback (no HTTP API in tests).
"""

from __future__ import annotations

import sqlite3

from app.zotero import Collection, LocalZotero


def _make_fixture(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE collections (
          collectionID INTEGER PRIMARY KEY,
          collectionName TEXT,
          parentCollectionID INTEGER
        );
        INSERT INTO collections VALUES (1, 'Vision-Language Models', NULL);
        INSERT INTO collections VALUES (2, 'Benchmarks', 1);
        INSERT INTO collections VALUES (3, 'Robotics', NULL);
        """
    )
    con.commit()
    con.close()


def test_list_collections_via_sqlite(tmp_path):
    db = tmp_path / "zotero.sqlite"
    _make_fixture(str(db))

    # api_base points nowhere reachable, so http_available() is False and we
    # exercise the SQLite path.
    z = LocalZotero(str(db), "http://127.0.0.1:1")
    assert z.http_available() is False

    cols = z.list_collections()
    assert Collection(1, "Vision-Language Models", None) in cols
    assert Collection(2, "Benchmarks", 1) in cols
    assert len(cols) == 3
    # ordered case-insensitively by name
    names = [c.name for c in cols]
    assert names == sorted(names, key=str.lower)


def test_source_reports_sqlite(tmp_path):
    db = tmp_path / "zotero.sqlite"
    _make_fixture(str(db))
    z = LocalZotero(str(db), "http://127.0.0.1:1")
    assert "sqlite" in z.source()


def _make_library(path: str) -> None:
    """A fuller fixture: one collection with one paper that has a PDF attachment."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE collections (collectionID INTEGER PRIMARY KEY,
          collectionName TEXT, parentCollectionID INTEGER);
        CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER, orderIndex INTEGER);
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER, key TEXT);
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INTEGER);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER);
        CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER, linkMode INTEGER,
          contentType TEXT, path TEXT);
        CREATE TABLE deletedItems (itemID INTEGER);

        INSERT INTO itemTypes VALUES (1,'journalArticle'),(14,'attachment'),(2,'note');
        INSERT INTO fields VALUES (1,'title'),(6,'date'),(90,'abstractNote');
        INSERT INTO collections VALUES (1,'Long Video',NULL);

        -- paper item (key PAPER1)
        INSERT INTO items VALUES (10,1,'PAPER1');
        INSERT INTO collectionItems VALUES (1,10,0);
        INSERT INTO itemDataValues VALUES (100,'A Great Paper'),(101,'2025-03-01');
        INSERT INTO itemData VALUES (10,1,100),(10,6,101);
        INSERT INTO creators VALUES (1,'Jane','Chen',0),(2,'Bo','Liu',0);
        INSERT INTO itemCreators VALUES (10,1,1,0),(10,2,1,1);

        -- its PDF attachment (key ATTACH1, child of item 10)
        INSERT INTO items VALUES (11,14,'ATTACH1');
        INSERT INTO itemAttachments VALUES (11,10,1,'application/pdf','storage:paper.pdf');

        -- a child note that must NOT appear as a paper
        INSERT INTO items VALUES (12,2,'NOTE1');
        INSERT INTO collectionItems VALUES (1,12,1);
        """
    )
    con.commit()
    con.close()


def test_list_papers_and_pdf(tmp_path):
    db = tmp_path / "zotero.sqlite"
    _make_library(str(db))
    # storage dir lives next to the sqlite file
    pdf = tmp_path / "storage" / "ATTACH1" / "paper.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 test")

    z = LocalZotero(str(db), "http://127.0.0.1:1")

    papers = z.list_papers(1)
    assert len(papers) == 1  # note excluded
    p = papers[0]
    assert p.key == "PAPER1"
    assert p.title == "A Great Paper"
    assert p.year == "2025"
    assert p.authors == "Chen, Liu"
    assert p.has_pdf is True

    assert z.get_paper("PAPER1").title == "A Great Paper"
    assert z.get_paper("NOPE") is None

    resolved = z.pdf_path("PAPER1")
    assert resolved == pdf
    assert resolved.exists()


def test_list_papers_includes_subcollections(tmp_path):
    """Importing a collection must pull in papers nested in its subcollections."""
    db = tmp_path / "zotero.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE collections (collectionID INTEGER PRIMARY KEY,
          collectionName TEXT, parentCollectionID INTEGER);
        CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER, orderIndex INTEGER);
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER, key TEXT);
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INTEGER);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER);
        CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER, linkMode INTEGER, contentType TEXT, path TEXT);
        CREATE TABLE deletedItems (itemID INTEGER);

        INSERT INTO itemTypes VALUES (1,'journalArticle'),(14,'attachment'),(2,'note'),(3,'annotation');
        INSERT INTO fields VALUES (1,'title');
        -- Distill (root, no direct papers) -> Subfolder (has a paper) -> Deep (deeper paper)
        INSERT INTO collections VALUES (1,'Distill',NULL),(2,'Subfolder',1),(3,'Deep',2);
        INSERT INTO items VALUES (10,1,'P_SUB'),(11,1,'P_DEEP');
        INSERT INTO collectionItems VALUES (2,10,0),(3,11,0);  -- nothing directly in coll 1
        INSERT INTO itemDataValues VALUES (100,'Sub Paper'),(101,'Deep Paper');
        INSERT INTO itemData VALUES (10,1,100),(11,1,101);
        """
    )
    con.commit(); con.close()
    z = LocalZotero(str(db), "http://127.0.0.1:1")
    keys = {p.key for p in z.list_papers(1)}
    assert keys == {"P_SUB", "P_DEEP"}, keys   # both nested papers pulled in


def test_list_papers_standalone_attachment(tmp_path):
    """A top-level PDF attachment added straight to a collection (e.g. an
    openreview.net import) is a paper in its own right — it must be listed and its
    PDF resolved from the item itself, not a child."""
    db = tmp_path / "zotero.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT, parentCollectionID INTEGER);
        CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER, orderIndex INTEGER);
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER, key TEXT);
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INTEGER);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER);
        CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER, linkMode INTEGER, contentType TEXT, path TEXT);
        CREATE TABLE deletedItems (itemID INTEGER);

        INSERT INTO itemTypes VALUES (1,'journalArticle'),(14,'attachment'),(2,'note'),(3,'annotation');
        INSERT INTO fields VALUES (1,'title');
        INSERT INTO collections VALUES (1,'distill',NULL);

        -- standalone PDF attachment item, member of the collection, no parent
        INSERT INTO items VALUES (50,14,'STANDALONE1');
        INSERT INTO collectionItems VALUES (1,50,0);
        INSERT INTO itemAttachments VALUES (50,NULL,1,'application/pdf','storage:pdf.pdf');
        INSERT INTO itemDataValues VALUES (500,'openreview.net/pdf?id=lh3Aa1u7kU');
        INSERT INTO itemData VALUES (50,1,500);
        """
    )
    con.commit(); con.close()
    pdf = tmp_path / "storage" / "STANDALONE1" / "pdf.pdf"
    pdf.parent.mkdir(parents=True); pdf.write_bytes(b"%PDF-1.4")

    z = LocalZotero(str(db), "http://127.0.0.1:1")
    papers = z.list_papers(1)
    assert len(papers) == 1
    assert papers[0].key == "STANDALONE1"
    assert papers[0].title == "openreview.net/pdf?id=lh3Aa1u7kU"
    assert papers[0].has_pdf is True
    assert z.pdf_path("STANDALONE1") == pdf and pdf.exists()


def test_resolve_collection_id(tmp_path):
    db = tmp_path / "zotero.sqlite"
    _make_library(str(db))
    z = LocalZotero(str(db), "http://127.0.0.1:1")
    col = z.resolve_collection_id("long-video")
    assert col is not None and col.id == 1
    assert z.resolve_collection_id("nonexistent") is None
