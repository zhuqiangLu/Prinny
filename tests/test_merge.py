"""Duplicate detection + merge (library.find_duplicate_groups / merge_papers).

Two same-title papers: the empty one is removed; when both carry the user's work they're
merged into one — chat, notes, highlights and membership fold into the kept paper, one PDF
survives, and the dropped stub row is gone. No user writing is lost.
"""
from __future__ import annotations

import pytest

import app.annotations as ann_mod
import app.library as library
import app.notes as notes_mod
import app.pdf_store as pdf_store
from app.db import connect, init_db


@pytest.fixture
def wired(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    store = tmp_path / "store"; store.mkdir()
    cols = tmp_path / "collections"
    for mod in (library, pdf_store, notes_mod, ann_mod):
        monkeypatch.setattr(mod, "connect", lambda: connect(db))
    monkeypatch.setattr(pdf_store, "load_config", lambda: {"pdf_store_path": str(store)})
    monkeypatch.setattr(notes_mod, "COLLECTIONS_DIR", cols)
    con = connect(db)
    con.execute("INSERT INTO collections(slug,name) VALUES('c','C')")
    con.commit(); con.close()
    return {"db": db, "store": store}


def _add(pid, title, key=None):
    con = library.connect()
    con.execute("INSERT INTO papers(id,title,zotero_key) VALUES(?,?,?)", (pid, title, key))
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id,source_flag) "
                "VALUES('c',?, 'new-from-zotero')", (pid,))
    con.commit(); con.close()


def _chat(pid, n=1):
    con = library.connect()
    cur = con.execute("INSERT INTO chat_threads(collection_slug,paper_id) VALUES('c',?)", (pid,))
    tid = cur.lastrowid
    for i in range(n):
        con.execute("INSERT INTO chat_messages(thread_id,role,content) VALUES(?, 'user', ?)",
                    (tid, f"q{i}"))
    con.commit(); con.close()
    return tid


def test_detects_and_recommends_keeping_the_engaged_copy(wired):
    _add(1, "In-Place Test-Time Training", "AAA")        # empty
    _add(2, "in-place   test-time training", "BBB")      # engaged (case/space differ)
    _chat(2, 3)
    groups = library.find_duplicate_groups("c")
    assert len(groups) == 1
    g = groups[0]
    assert g["action"] == "remove"          # only one copy has attention
    assert g["keep_id"] == 2                 # recommend the engaged one
    assert {m["id"] for m in g["members"]} == {1, 2}


def test_merge_remembers_dropped_zotero_duplicate_silently(wired):
    _add(1, "Same Title", "AAA")            # both have Zotero items
    _add(2, "Same Title", "BBB")
    pdf_store.copy_into_store(1, _mkpdf(wired))
    pdf_store.copy_into_store(2, _mkpdf(wired))
    _chat(2, 1)
    res = library.merge_papers("c", keep_id=2, drop_ids=[1, 2])   # 2 is filtered out
    assert res["merged"] == 1 and res["remembered"] == 1
    assert [p["id"] for p in library.list_papers("c")] == [2]     # only the kept paper shown
    con = library.connect()
    assert con.execute("SELECT 1 FROM papers WHERE id=1").fetchone()  # row KEPT (remembered, not orphaned)
    con.close()
    # The dropped Zotero key is remembered so a Pull won't silently re-add the duplicate...
    assert "AAA" in library.removed_index("c")["keys"]
    assert library.list_graveyard("c") == []                     # ...but it's hidden from the Graveyard
    assert library.list_deleted("c") == []                       # and not a permanent-delete tombstone
    assert not pdf_store.has_pdf(1) and pdf_store.has_pdf(2)      # one PDF survives


def test_merge_hard_deletes_local_only_drop(wired):
    _add(1, "Dup")                          # no zotero_key → nothing in Zotero to remember
    _add(2, "Dup", "BBB")
    _chat(2, 1)
    library.merge_papers("c", keep_id=2, drop_ids=[1])
    con = library.connect()
    assert con.execute("SELECT 1 FROM papers WHERE id=1").fetchone() is None  # hard-deleted
    con.close()
    assert library.removed_index("c")["keys"] == set()           # nothing remembered


def test_merge_combines_notes_and_repoints_work(wired):
    _add(1, "Dup", "AAA")
    _add(2, "Dup", "BBB")
    notes_mod.save_note("c", 1, "sum-one", "take-one", "", "noted")
    notes_mod.save_note("c", 2, "sum-two", "take-two", "", "noted")
    ann_mod.create("c", 1, kind="highlight", color="#ff0", page=1, position_json="{}",
                   selected_text="hl from one")
    _chat(1, 2)
    library.merge_papers("c", keep_id=2, drop_ids=[1])
    con = library.connect()
    note = con.execute("SELECT thoughts FROM paper_notes WHERE paper_id=2").fetchone()["thoughts"]
    assert "take-two" in note and "take-one" in note          # neither side's writing lost
    assert con.execute("SELECT COUNT(*) FROM paper_notes WHERE paper_id=1").fetchone()[0] == 0
    assert con.execute("SELECT paper_id FROM annotations").fetchone()["paper_id"] == 2  # re-pointed
    assert con.execute("SELECT COUNT(*) FROM chat_threads WHERE paper_id=2").fetchone()[0] == 1
    con.close()


def test_merge_all_resolves_every_group_into_its_keep(wired):
    _add(1, "Alpha", "A1"); _add(2, "Alpha", "A2"); _chat(2, 1)   # keep 2
    _add(3, "Beta", "B1"); _add(4, "Beta", "B2")                   # both empty -> keep 3
    for g in library.find_duplicate_groups("c"):
        library.merge_papers("c", g["keep_id"], [m["id"] for m in g["members"]])
    assert library.find_duplicate_groups("c") == []               # nothing left to resolve
    ids = sorted(p["id"] for p in library.list_papers("c"))
    assert ids == [2, 3]                                           # one survivor per group


def test_remove_membership_drops_from_collection_only(wired):
    # paper 1 in collections c and d; removing from c keeps it in d and keeps the paper row
    con = library.connect()
    con.execute("INSERT INTO collections(slug,name) VALUES('d','D')")
    con.execute("INSERT INTO papers(id,title,zotero_key) VALUES(1,'P','K1')")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('c',1)")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('d',1)")
    con.commit(); con.close()

    library.remove_membership("c", 1)
    assert [p["id"] for p in library.list_papers("c")] == []     # gone from c
    assert [p["id"] for p in library.list_papers("d")] == [1]     # still in d
    con = library.connect()
    assert con.execute("SELECT 1 FROM papers WHERE id=1").fetchone()  # paper row kept
    con.close()


def test_stage_removal_hides_keeps_row_and_queues(wired, monkeypatch):
    monkeypatch.setattr(library.pdf_store, "remove_pdf", lambda pid: False)  # no PDF store in test
    con = library.connect()
    con.execute("INSERT INTO papers(id,title,zotero_key) VALUES(1,'P','K1')")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('c',1)")
    con.commit(); con.close()

    library.stage_removal("c", 1)
    assert [p["id"] for p in library.list_papers("c")] == []        # hidden from the collection
    con = library.connect()
    assert con.execute("SELECT 1 FROM collection_papers WHERE collection_slug='c' AND paper_id=1").fetchone()
    con.close()                                                      # membership row kept (restorable)
    assert [g["id"] for g in library.list_graveyard("c")] == [1]    # shows in the Graveyard
    assert "K1" in library.removed_index("c")["keys"]              # suppresses re-add on Pull


def test_mark_read_open_and_toggle(wired):
    con = library.connect()
    con.execute("INSERT INTO papers(id,title) VALUES(1,'P')")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('c',1)")
    con.commit(); con.close()
    assert library.list_papers("c")[0]["read"] is False
    library.mark_read_if_unread("c", 1)                  # opening marks read
    assert library.list_papers("c")[0]["read"] is True
    library.mark_read("c", [1], read=False)              # mark-as unread
    assert library.list_papers("c")[0]["read"] is False


def test_graveyard_lists_hides_and_restores(wired, monkeypatch):
    monkeypatch.setattr(library.pdf_store, "remove_pdf", lambda pid: False)
    con = library.connect()
    con.execute("INSERT INTO papers(id,title) VALUES(1,'A'),(2,'B')")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('c',1),('c',2)")
    con.commit(); con.close()
    library.stage_removal("c", 1); library.stage_removal("c", 2)
    assert library.graveyard_count("c") == 2
    assert library.list_papers("c") == []                 # both hidden from the collection
    assert len(library.list_graveyard("c")) == 2          # both in the graveyard
    library.restore_removal("c", 1)
    assert library.graveyard_count("c") == 1
    assert [p["id"] for p in library.list_papers("c")] == [1]   # restored


def test_two_tier_removal_permanently_delete_and_restore(wired, monkeypatch):
    monkeypatch.setattr(library.pdf_store, "remove_pdf", lambda pid: False)
    _add(1, "Paper One", "K1")
    library.stage_removal("c", 1)
    assert [g["id"] for g in library.list_graveyard("c")] == [1]
    assert library.list_deleted("c") == []

    library.permanently_delete("c", [1])                 # graveyard -> tombstone
    assert library.list_graveyard("c") == []
    assert [d["id"] for d in library.list_deleted("c")] == [1]
    assert "K1" in library.removed_index("c")["keys"]    # still suppresses re-add on Pull

    library.restore_removal("c", 1)                      # tombstone is recoverable (work kept)
    assert [p["id"] for p in library.list_papers("c")] == [1]
    assert library.list_deleted("c") == []


def test_purge_forgets_tombstone_and_work(wired, monkeypatch):
    monkeypatch.setattr(library.pdf_store, "remove_pdf", lambda pid: False)
    _add(1, "Paper One", "K1")
    notes_mod.save_note("c", 1, "sum", "take", "", "noted")
    _chat(1, 2)
    library.stage_removal("c", 1)
    library.permanently_delete("c", [1])

    library.purge_removals("c", [1])                     # forget metadata + work entirely
    con = library.connect()
    assert con.execute("SELECT 1 FROM papers WHERE id=1").fetchone() is None       # paper gone
    assert con.execute("SELECT COUNT(*) FROM paper_notes WHERE paper_id=1").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM chat_threads WHERE paper_id=1").fetchone()[0] == 0
    con.close()
    assert library.removed_index("c")["keys"] == set()   # no tombstone -> a Pull may re-add it


def test_reading_log_walk_back_and_cap(wired):
    con = library.connect()
    con.execute("INSERT INTO papers(id,title) VALUES(1,'A'),(2,'B'),(3,'C')")
    con.commit(); con.close()
    library.log_open("c", 1); library.log_open("c", 2); library.log_open("c", 3)
    assert library.previous_in_log("c", 3) == 2          # one step older
    assert library.previous_in_log("c", 2) == 1
    assert library.previous_in_log("c", 1) is None       # oldest
    library.log_open("c", 3, cap=2)                       # prune to 2 most recent (3, 2)
    assert library.previous_in_log("c", 3) == 2
    assert library.previous_in_log("c", 2) is None        # 1 pruned out


def test_highlights_are_scoped_per_collection(wired):
    # Same paper shared by two collections; a highlight made in 'c' must NOT show in 'd'.
    con = library.connect()
    con.execute("INSERT INTO collections(slug,name) VALUES('d','D')")
    con.execute("INSERT INTO papers(id,title,zotero_key) VALUES(1,'Shared','K1')")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('c',1),('d',1)")
    con.commit(); con.close()
    ann_mod.create("c", 1, kind="highlight", color="#ffd400", page=1, position_json="{}", selected_text="hl in c")

    assert [a["selected_text"] for a in ann_mod.list_app(1, "c")] == ["hl in c"]
    assert ann_mod.list_app(1, "d") == []                  # no leak into the other collection
    assert len(ann_mod.list_app(1)) == 1                   # unscoped still sees all (legacy)


def test_remap_highlights_to_scheme(wired):
    con = library.connect()
    con.execute("INSERT INTO papers(id,title) VALUES(1,'P')")
    con.commit(); con.close()
    ann_mod.create("c", 1, kind="highlight", color="#ff6666", page=1, position_json="{}", selected_text="x")
    ann_mod.create("c", 1, kind="highlight", color="#00ff00", page=1, position_json="{}", selected_text="y")
    n = ann_mod.remap_to_scheme(["#ffd400", "#5fd35f"])   # yellow, green
    assert n == 2
    colors = {a["color"] for a in ann_mod.list_app(1)}
    assert colors == {"#ffd400", "#5fd35f"}               # red→yellow, bright-green→green


def test_import_directory_creates_local_collection(wired, tmp_path):
    folder = tmp_path / "papers"; folder.mkdir()
    (folder / "Alpha Paper.pdf").write_bytes(b"%PDF-1.4")
    (folder / "Beta.pdf").write_bytes(b"%PDF-1.4")
    (folder / "notes.txt").write_text("ignore me")           # non-PDF ignored

    prev = library.scan_directory_pdfs(str(folder))
    assert prev["count"] == 2 and "Alpha Paper.pdf" in prev["pdfs"]

    slug = library.import_directory("My Folder", str(folder),
                                    tags=[{"label": "local", "color": "#0ea5e9"}])
    titles = {p["title"] for p in library.list_papers(slug)}
    assert titles == {"Alpha Paper", "Beta"}                  # filename stems (no PDF metadata)
    assert library.get_collection(slug)["zotero_collection_id"] is None  # local-only

    import pytest
    with pytest.raises(ValueError):
        library.import_directory("x", str(tmp_path / "nope"))  # bad path


def test_import_directory_resolves_arxiv_metadata_from_filename(wired, tmp_path, monkeypatch):
    import app.discover as discover
    # A PDF named after its arXiv id -> we resolve the real metadata (no LLM).
    folder = tmp_path / "papers"; folder.mkdir()
    (folder / "2409.14485v4.pdf").write_bytes(b"%PDF-1.4")
    (folder / "random-notes.pdf").write_bytes(b"%PDF-1.4")           # no arXiv id -> filename
    captured = {}
    def fake_batch(ids):
        captured["ids"] = list(ids)
        return {"2409.14485": {"arxiv_id": "2409.14485", "title": "Video-XL",
                               "authors": "Yan Shu", "year": "2024", "abstract": "long video"}}
    monkeypatch.setattr(discover, "fetch_arxiv_batch", fake_batch)

    slug = library.import_directory("Lib", str(folder))
    papers = {p["title"]: p for p in library.list_papers(slug)}
    assert "Video-XL" in papers                                      # arXiv title, not "2409.14485v4"
    assert papers["Video-XL"]["arxiv_id"] == "2409.14485"            # id stored (PDF re-fetch works)
    assert "random-notes" in papers                                  # fell back to filename
    assert captured["ids"] == ["2409.14485"]                         # only the arXiv-id one looked up


def _mkpdf(wired):
    p = wired["store"].parent / f"src-{id(wired)}-{_mkpdf.n}.pdf"
    _mkpdf.n += 1
    p.write_bytes(b"%PDF-1.4 x")
    return p
_mkpdf.n = 0


def test_heuristic_pdf_title_authors_from_first_page():
    # A wrapped title ending in ':' joins the next line; the line after is the authors.
    text = ("DeepSeek-V4:\nTowards Highly Efficient Million-Token Context Intelligence\n"
            "DeepSeek-AI\nresearch@deepseek.com\nAbstract\nWe present a preview...")
    title, authors = library._heuristic_pdf_meta(text)
    assert title == "DeepSeek-V4: Towards Highly Efficient Million-Token Context Intelligence"
    assert authors == "DeepSeek-AI"
    # A single-line title: authors are the next non-email line.
    t2, a2 = library._heuristic_pdf_meta("Attention Is All You Need\nAshish Vaswani, Noam Shazeer\nAbstract\nx")
    assert t2 == "Attention Is All You Need" and a2 == "Ashish Vaswani, Noam Shazeer"
    assert library._heuristic_pdf_meta("") == (None, None)
