"""Local-first store (ADR 0001): clean-reset migration, import/refresh merge,
PDF store, and additive sync — all with a fake Zotero + temp dirs (no network)."""

from __future__ import annotations

import sqlite3

import pytest

import app.library as library
import app.pdf_store as pdf_store
import app.sync as sync
from app.db import connect, init_db
from app.zotero import Collection, Paper, ZoteroBackend, ZoteroWriteError


# --- fakes ----------------------------------------------------------------------
class FakeZotero(ZoteroBackend):
    """A scriptable Zotero: mutable papers + a recorder for write calls."""

    def __init__(self, papers, pdf):
        self.papers = papers          # {key: Paper}
        self._pdf = pdf               # Path | None used as every paper's source PDF
        self.calls = []               # recorded write calls
        self.collections_made = {}    # name -> key
        self._n = 0

    def list_collections(self):
        return [Collection("C1", "My Coll", None)]

    def resolve_collection_id(self, slug):
        return Collection("C1", "My Coll", None)

    def list_papers(self, cid):
        return list(self.papers.values())

    def get_paper(self, key):
        return self.papers.get(key)

    def pdf_path(self, key):
        return self._pdf if key in self.papers else None

    def paper_full(self, key):
        return {"abstract": f"abs {key}"} if key in self.papers else None

    def source(self):
        return "fake"

    # writes
    def _require_write(self):
        return None

    def find_collection_key(self, name):
        return self.collections_made.get(name)

    def create_collection(self, name, parent_key=None):
        self.collections_made[name] = "COLL1"
        self.calls.append(("create_collection", name))
        return "COLL1"

    def create_item(self, item):
        self._n += 1
        key = f"ITEM{self._n}"
        self.calls.append(("create_item", item.get("title"), item.get("collections")))
        return key

    def upload_attachment(self, parent_key, pdf_path):
        self.calls.append(("upload_attachment", parent_key))
        return "ATT1"

    def remove_item_from_collection(self, item_key, collection_key):
        self.calls.append(("remove", item_key, collection_key))

    def delete_item(self, item_key):
        self.calls.append(("delete_item", item_key))

    def add_item_to_collection(self, item_key, collection_key):
        self.calls.append(("add_to_coll", item_key, collection_key))


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A temp DB + PDF store, with every module's connect()/config pointed at them."""
    db = tmp_path / "app.sqlite"
    init_db(db)
    store = tmp_path / "store"
    store.mkdir()
    cfg = {"pdf_store_path": str(store)}
    for mod in (library, pdf_store):
        monkeypatch.setattr(mod, "connect", lambda: connect(db))
    monkeypatch.setattr(pdf_store, "load_config", lambda: cfg)
    return {"db": db, "store": store}


def _src_pdf(tmp_path):
    p = tmp_path / "src.pdf"
    p.write_bytes(b"%PDF-1.4 hello")
    return p


# --- clean-reset migration ------------------------------------------------------
def test_init_db_resets_old_schema(tmp_path):
    db = tmp_path / "app.sqlite"
    # Old (Zotero-keyed) schema signature: paper_notes present, no papers table.
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE paper_notes (zotero_key TEXT PRIMARY KEY)")
    con.execute("INSERT INTO paper_notes VALUES ('OLD')")
    con.commit(); con.close()

    init_db(db)

    bak = tmp_path / "app.sqlite.bak"
    assert bak.exists(), "old DB must be backed up, never silently destroyed"
    tables = {r[0] for r in connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"papers", "collections", "collection_papers"} <= tables
    # backup retains the original row
    assert sqlite3.connect(bak).execute("SELECT zotero_key FROM paper_notes").fetchone()[0] == "OLD"


def test_init_db_fresh_no_backup(tmp_path):
    db = tmp_path / "app.sqlite"
    init_db(db)
    assert not (tmp_path / "app.sqlite.bak").exists()


# --- import + non-destructive refresh merge -------------------------------------
def test_activate_eager_imports_and_caches(wired, tmp_path):
    z = FakeZotero({"K1": Paper("K1", "One", "A", "2024", True),
                    "K2": Paper("K2", "Two", "B", "2023", True)}, _src_pdf(tmp_path))
    slug = library.activate(z, "my-coll", "eager")    # returns the new local slug
    papers = library.list_papers(slug)
    assert len(papers) == 2
    assert all(p["has_pdf"] for p in papers)          # eager copied PDFs
    assert all(p["sync_status"] == "synced" for p in papers)
    assert all(p["source_flag"] == "new-from-zotero" for p in papers)


def test_lazy_import_defers_pdf(wired, monkeypatch, tmp_path):
    z = FakeZotero({"K1": Paper("K1", "One", "A", "2024", True)}, _src_pdf(tmp_path))
    monkeypatch.setattr("app.zotero.get_zotero", lambda: z)  # ensure_cached's source
    library.activate(z, "my-coll", "lazy")
    pid = library.list_papers("my-coll")[0]["id"]
    assert pdf_store.pdf_dest(pid).exists() is False   # not cached yet
    # ensure_cached pulls it on demand
    assert pdf_store.ensure_cached(pid) is not None
    assert pdf_store.has_pdf(pid)


def test_refresh_merge_is_non_destructive(wired, tmp_path):
    z = FakeZotero({"K1": Paper("K1", "One", "A", "2024", True),
                    "K2": Paper("K2", "Two", "B", "2023", True)}, _src_pdf(tmp_path))
    library.activate(z, "my-coll", "eager")
    # a local-only curated paper that must survive refresh
    local_pid = library.upsert_paper(arxiv_id="2401.1", title="Local", origin="arxiv-suggested")
    library.add_membership("my-coll", local_pid, "arxiv-suggested")

    # Zotero changes underneath: drop K2, add K3, edit K1's title.
    del z.papers["K2"]
    z.papers["K3"] = Paper("K3", "Three", "C", "2025", True)
    z.papers["K1"] = Paper("K1", "One REVISED", "A", "2024", True)
    res = library.refresh(z, "my-coll")

    by_title = {p["title"]: p for p in library.list_papers("my-coll")}
    assert "One REVISED" in by_title                       # Zotero wins on metadata
    assert by_title["Three"]["source_flag"] == "new-from-zotero"
    assert by_title["Two"]["source_flag"] == "removed-in-zotero"  # flagged, not deleted
    assert by_title["Local"]["sync_status"] == "local-only"       # preserved
    assert res["removed_in_zotero"] == 1
    # idempotent
    res2 = library.refresh(z, "my-coll")
    assert res2["added"] == 0 and res2["readded"] == 0 and res2["removed_in_zotero"] == 0


# --- PDF store graceful degradation --------------------------------------------
def test_store_unavailable_is_graceful(wired, monkeypatch, tmp_path):
    z = FakeZotero({"K1": Paper("K1", "One", "A", "2024", True)}, _src_pdf(tmp_path))
    library.activate(z, "my-coll", "lazy")
    pid = library.list_papers("my-coll")[0]["id"]
    # point the store at a path whose parent is also absent (a disconnected mount):
    # _ensure_store must NOT fabricate it.
    monkeypatch.setattr(pdf_store, "load_config",
                        lambda: {"pdf_store_path": str(tmp_path / "gone" / "pdfs")})
    assert pdf_store.store_available() is False
    assert pdf_store.ensure_cached(pid) is None
    assert pdf_store.has_pdf(pid) is False
    assert pdf_store.copy_into_store(pid, _src_pdf(tmp_path)) is False


# --- pull-only model ------------------------------------------------------------
def test_pull_holds_previously_removed_until_picked(wired, monkeypatch, tmp_path):
    # Linked collection with A + B; the user removes B. Zotero still has both.
    z = FakeZotero({"A": Paper("A", "Paper A", "", "", True),
                    "B": Paper("B", "Paper B", "", "", True)}, _src_pdf(tmp_path))
    slug = library.activate(z, "col", "eager")
    pb = next(p["id"] for p in library.list_papers(slug) if p["zotero_key"] == "B")
    library.stage_removal(slug, pb)
    assert {p["zotero_key"] for p in library.list_papers(slug)} == {"A"}   # B hidden

    # Preview: B is HELD (previously removed), not offered as incoming_new.
    prev = library.pull_preview(z, slug)
    assert prev["incoming_new"] == []
    assert [h["zotero_key"] for h in prev["held"]] == ["B"]

    # A plain Pull leaves B out (held), then picking B re-adds it.
    res = library.refresh(z, slug)
    assert res["held"] == 1 and res["added"] == 0
    assert {p["zotero_key"] for p in library.list_papers(slug)} == {"A"}
    res2 = library.refresh(z, slug, readd_keys=["B"])
    assert res2["readded"] == 1
    assert {p["zotero_key"] for p in library.list_papers(slug)} == {"A", "B"}
    assert library.list_graveyard(slug) == []                      # cleared from the Graveyard


def test_pull_preview_diffs_zotero_vs_local(wired, monkeypatch, tmp_path):
    # Zotero has A + B; local (linked) has A + a local C not in Zotero.
    z = FakeZotero({"A": Paper("A", "Paper A", "", "", False),
                    "B": Paper("B", "Paper B", "", "", False)}, _src_pdf(tmp_path))
    library.upsert_collection("col", "My Coll", zotero_collection_id="C1")
    pa = library.upsert_paper(zotero_key="A", title="Paper A", origin="zotero-import")
    pc = library.upsert_paper(zotero_key="C", title="Paper C", origin="zotero-import")
    library.add_membership("col", pa, "zotero")
    library.add_membership("col", pc, "zotero")

    out = library.pull_preview(z, "col")
    assert [p["zotero_key"] for p in out["incoming_new"]] == ["B"]    # in Zotero, not local
    assert [p["zotero_key"] for p in out["incoming_gone"]] == ["C"]   # local, gone from Zotero


def test_title_repair_and_no_clobber(wired, monkeypatch):
    # junk-title detection
    assert library._is_junk_title("openreview.net/pdf?id=XYZ")
    assert library._is_junk_title("pdf.pdf")
    assert library._is_junk_title("(untitled)")
    assert not library._is_junk_title("A Real Paper Title")

    monkeypatch.setattr(library.openreview, "title_for",
                        lambda t: "Stacked from One" if "id=XYZ" in (t or "") else None)
    pid = library.upsert_paper(zotero_key="K", title="openreview.net/pdf?id=XYZ", origin="zotero-import")
    assert library.repair_title(pid) is True
    assert library.get_paper(pid)["title"] == "Stacked from One"

    # a later re-import with the junk title must NOT revert the repaired one
    library.upsert_paper(zotero_key="K", title="openreview.net/pdf?id=XYZ", origin="zotero-import")
    assert library.get_paper(pid)["title"] == "Stacked from One"


# --- delete PDF (keep title + membership) & move between collections ------------
def test_delete_pdf_keeps_title_and_membership(wired, tmp_path):
    z = FakeZotero({"K1": Paper("K1", "One", "A", "2024", True)}, _src_pdf(tmp_path))
    library.activate(z, "my-coll", "eager")
    pid = library.list_papers("my-coll")[0]["id"]
    assert pdf_store.pdf_dest(pid).exists()              # eager-cached

    assert library.delete_pdf(pid) is True               # file unlinked
    assert pdf_store.pdf_dest(pid).exists() is False
    assert pdf_store.has_pdf(pid) is False

    paper = library.get_paper(pid)
    assert paper is not None and paper["title"] == "One"  # title kept
    assert paper["pdf_state"] == "absent"
    members = library.list_papers("my-coll")
    assert len(members) == 1 and members[0]["id"] == pid  # still in the collection


def test_delete_pdf_local_only_paper(wired):
    """A purely-local paper (no Zotero/arXiv key) stays as a title-only stub."""
    pid = library.upsert_paper(title="Local note", origin="app-created")
    slug = library.create_local_collection("scratch")
    library.add_membership(slug, pid, "local")
    library.delete_pdf(pid)                               # no file to unlink -> no-op file-wise
    paper = library.get_paper(pid)
    assert paper["title"] == "Local note" and paper["pdf_state"] == "absent"
    assert [p["id"] for p in library.list_papers(slug)] == [pid]


def test_move_paper_between_collections(wired, tmp_path):
    z = FakeZotero({"K1": Paper("K1", "One", "A", "2024", True)}, _src_pdf(tmp_path))
    src = library.activate(z, "src-coll", "eager")
    dst = library.create_local_collection("dst-coll")
    pid = library.list_papers(src)[0]["id"]

    library.move_paper(src, dst, pid)
    assert library.list_papers(src) == []                 # gone from source
    moved = library.list_papers(dst)
    assert [p["id"] for p in moved] == [pid]              # now in target
    assert moved[0]["source_flag"] == "local"
    assert pdf_store.has_pdf(pid)                          # PDF (global) follows


def test_move_paper_same_collection_is_noop(wired, tmp_path):
    z = FakeZotero({"K1": Paper("K1", "One", "A", "2024", True)}, _src_pdf(tmp_path))
    library.activate(z, "my-coll", "eager")
    pid = library.list_papers("my-coll")[0]["id"]
    library.move_paper("my-coll", "my-coll", pid)
    assert [p["id"] for p in library.list_papers("my-coll")] == [pid]


# --- add paper by arXiv (metadata fetch + manual add) ---------------------------
_ATOM = """<?xml version='1.0'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
 <entry>
  <id>http://arxiv.org/abs/2401.12345v2</id>
  <title>A Great   Paper
  Title</title>
  <summary>We do things.</summary>
  <published>2024-01-22T00:00:00Z</published>
  <author><name>Ada Lovelace</name></author>
  <author><name>Alan Turing</name></author>
 </entry>
</feed>"""


def test_fetch_arxiv_metadata_parses(monkeypatch):
    import app.discover as discover

    class _Resp:
        text = _ATOM
        def raise_for_status(self): pass

    monkeypatch.setattr(discover.httpx, "get", lambda *a, **k: _Resp())
    meta = discover.fetch_arxiv_metadata("https://arxiv.org/abs/2401.12345v2")
    assert meta["arxiv_id"] == "2401.12345"          # version stripped
    assert meta["title"] == "A Great Paper Title"     # whitespace collapsed
    assert meta["authors"] == "Ada Lovelace, Alan Turing"
    assert meta["year"] == "2024"


def test_fetch_arxiv_metadata_bad_id(monkeypatch):
    import app.discover as discover
    assert discover.fetch_arxiv_metadata("not-an-arxiv-id") is None


_SEARCH_HTML = """<html><body>
<li class="arxiv-result">
  <p class="list-title is-inline-block"><a href="https://arxiv.org/abs/2501.01234">arXiv:2501.01234</a></p>
  <p class="title is-5 mathjax">A <span class="search-hit">Great</span> Title</p>
  <span class="abstract-full has-text-grey-dark mathjax" id="2501.01234v1-abstract-full" style="display:none;">
    The full abstract about <span class="search-hit">reasoning</span> here. <a href="#">&#9651; Less</a></span>
</li></body></html>"""

_ABS_HTML = """<html><head>
<meta name="citation_title" content="A Great Title" />
<meta name="citation_author" content="Doe, Jane" />
<meta name="citation_author" content="Roe, Rick" />
<meta name="citation_date" content="2025/01/02" />
<meta property="og:description" content="The abstract from the page." />
</head></html>"""


def test_arxiv_search_html_parses(monkeypatch):
    """Website search fallback extracts id + title + full abstract (tags stripped)."""
    import app.discover as discover

    class _Resp:
        text = _SEARCH_HTML
        def raise_for_status(self): pass
    monkeypatch.setattr(discover.httpx, "get", lambda *a, **k: _Resp())
    hits = discover._arxiv_search_html("all:reasoning", max_results=5)
    assert len(hits) == 1
    assert hits[0]["arxiv_id"] == "2501.01234"
    assert hits[0]["title"] == "A Great Title"             # search-hit span stripped
    assert "full abstract about reasoning here." in hits[0]["summary"]   # not truncated at nested span


def test_arxiv_meta_html_parses(monkeypatch):
    import app.discover as discover

    class _Resp:
        text = _ABS_HTML
        def raise_for_status(self): pass
    monkeypatch.setattr(discover.httpx, "get", lambda *a, **k: _Resp())
    m = discover._arxiv_meta_html("2501.01234")
    assert m["title"] == "A Great Title" and m["year"] == "2025"
    assert m["authors"] == "Doe, Jane, Roe, Rick"
    assert m["abstract"] == "The abstract from the page."


def test_arxiv_search_falls_back_to_website_on_api_failure(monkeypatch):
    """When the API raises (429/timeout), _arxiv_search uses the website scraper."""
    import app.discover as discover
    def _boom(*a, **k):
        raise discover.ArxivError("429")
    monkeypatch.setattr(discover, "_arxiv_get", _boom)
    monkeypatch.setattr(discover, "_arxiv_search_html",
                        lambda q, max_results=10: [{"arxiv_id": "2501.0001", "title": "T", "summary": "s"}])
    out = discover._arxiv_search("all:x", max_results=5)
    assert out and out[0]["arxiv_id"] == "2501.0001"


def test_arxiv_get_fails_fast_on_429(monkeypatch):
    """A 429 raises immediately (no retry storm — retrying within seconds is futile
    and only feeds the rate limiter)."""
    import app.discover as discover
    calls = {"n": 0}

    class _R429:
        status_code = 429
        headers = {}
        def raise_for_status(self): pass

    def _get(*a, **k):
        calls["n"] += 1
        return _R429()
    monkeypatch.setattr(discover.httpx, "get", _get)
    with pytest.raises(discover.ArxivError, match="429"):
        discover._arxiv_get({"search_query": "x"})
    assert calls["n"] == 1                               # one call, not retries=2


def test_add_arxiv_manual(wired, monkeypatch):
    import app.triage as triage

    library.upsert_collection("box", "Box", copy_mode="lazy", activated=1)  # lazy → no PDF fetch
    monkeypatch.setattr(
        "app.discover.fetch_arxiv_metadata",
        lambda raw: {"arxiv_id": "2401.99999", "title": "Manual Add",
                     "authors": "Q", "year": "2024", "abstract": "a"},
    )
    ok, msg = triage.add_arxiv_manual("box", "2401.99999")
    assert ok and "Manual Add" in msg

    papers = library.list_papers("box")
    assert [p["title"] for p in papers] == ["Manual Add"]
    p = papers[0]
    assert p["origin"] == "app-created"       # user-curated, not "suggested"
    assert p["sync_status"] == "local-only"
    assert p["source_flag"] == "local"

    # bad id → no paper added, friendly failure
    monkeypatch.setattr("app.discover.fetch_arxiv_metadata", lambda raw: None)
    ok2, _ = triage.add_arxiv_manual("box", "junk")
    assert ok2 is False and len(library.list_papers("box")) == 1


# --- next-unread navigation -----------------------------------------------------
def test_next_unread(wired):
    library.upsert_collection("c", "C", activated=1)
    ids = []
    for t in ["A", "B", "C", "D"]:           # title order == insertion order
        pid = library.upsert_paper(title=t, origin="app-created")
        library.add_membership("c", pid)
        ids.append(pid)
    # mark B as noted (not unread)
    con = library.connect()
    con.execute("INSERT INTO paper_notes (paper_id, collection_slug, status) VALUES (?, 'c', 'noted')", (ids[1],))
    con.commit(); con.close()

    assert library.next_unread("c", ids[0]) == ids[2]   # A -> skip B(noted) -> C
    assert library.next_unread("c", ids[2]) == ids[3]   # C -> D
    assert library.next_unread("c", ids[3]) == ids[0]   # D -> wrap to A

    # when nothing else is unread -> None
    con = library.connect()
    for pid in (ids[2], ids[3]):
        con.execute("INSERT INTO paper_notes (paper_id, collection_slug, status) VALUES (?, 'c', 'noted')", (pid,))
    con.commit(); con.close()
    assert library.next_unread("c", ids[0]) is None     # only A unread (== current)


# --- hotness / engagement heatmap -----------------------------------------------
def test_collection_activity(wired, monkeypatch, tmp_path):
    cdir = tmp_path / "cols"; (cdir / "box" / "thoughts").mkdir(parents=True)
    monkeypatch.setattr(library, "COLLECTIONS_DIR", cdir)
    days = library.activity_days(); today = days[-1]; old = days[1]
    library.upsert_collection("box", "Box", activated=1)
    pid = library.upsert_paper(title="P", origin="app-created")
    library.add_membership("box", pid)
    con = library.connect()
    tid = con.execute("INSERT INTO chat_threads (collection_slug, paper_id) VALUES ('box', NULL)").lastrowid
    con.execute("INSERT INTO chat_messages (thread_id,role,content,created_at) VALUES (?,'user','hi',?)", (tid, today + " 10:00:00"))
    con.execute("INSERT INTO chat_messages (thread_id,role,content,created_at) VALUES (?,'user','yo',?)", (tid, today + " 11:00:00"))
    con.execute("INSERT INTO chat_messages (thread_id,role,content,created_at) VALUES (?,'assistant','r',?)", (tid, today + " 11:01:00"))  # not counted
    con.execute("INSERT INTO annotations (paper_id,collection_slug,origin,kind,page,position_json,created_at) VALUES (?,'box','app','highlight',0,'{}',?)", (pid, old + " 09:00:00"))
    con.execute("INSERT INTO paper_notes (paper_id,collection_slug,status,updated_at) VALUES (?,'box','noted',?)", (pid, today + " 08:00:00"))
    con.commit(); con.close()
    (cdir / "box" / "thoughts" / f"{today}T09-00-00.md").write_text("x", encoding="utf-8")

    a = library.collection_activity()["box"]
    assert a["chat"][-1] == 2          # 2 user msgs today; assistant reply not counted
    assert a["highlights"][1] == 1     # highlight landed on the 2nd day of the window
    assert a["notes"][-1] == 1
    assert a["thoughts"][-1] == 1
    assert a["total"] == 5

    # list_collections(with_activity=True) attaches it; default leaves it off
    cols = {c["slug"]: c for c in library.list_collections(with_activity=True)}
    assert cols["box"]["activity"]["total"] == 5
    assert "activity" not in library.list_collections()[0]


# --- custom tags ----------------------------------------------------------------
def test_collection_tags_roundtrip(wired):
    library.upsert_collection("box", "Box", activated=1)
    library.set_tags("box", [
        {"label": "to-read", "color": "#0ea5e9"},
        {"label": "  spaced  ", "color": "#ABCDEF"},   # trimmed
        {"label": "bad", "color": "notahex"},          # dropped: invalid color
        {"label": "", "color": "#000000"},             # dropped: empty label
    ])
    tags = library.get_collection("box")["tags"]
    assert tags == [{"label": "to-read", "color": "#0ea5e9"},
                    {"label": "spaced", "color": "#ABCDEF"}]
    # also surfaced by list_collections
    by = {c["slug"]: c for c in library.list_collections()}
    assert [t["label"] for t in by["box"]["tags"]] == ["to-read", "spaced"]


# --- directory export (BibTeX) --------------------------------------------------


def test_export_to_bibtex_text(wired, monkeypatch):
    import app.export_dir as export_dir
    monkeypatch.setattr(export_dir, "library", library)
    library.upsert_collection("box", "Box", activated=1)
    pid1 = library.upsert_paper(arxiv_id="2401.01234", title="Deep Vision Models",
                                authors="Chen, Liu", year="2024")
    pid2 = library.upsert_paper(zotero_key="ABCD1234", title="A Study of Things",
                                authors="Smith", year="2020")
    library.add_membership("box", pid1)
    library.add_membership("box", pid2)

    bib = export_dir.to_bibtex("box")               # text, no file
    assert "@misc{Chen2024Deep," in bib             # arxiv -> @misc
    assert "eprint = {2401.01234}" in bib
    assert "@article{Smith2020Study," in bib        # non-arxiv -> @article
    assert "author = {Chen and Liu}" in bib         # comma list -> ' and '


def test_export_pdfs_copies_and_rejects_relative(wired, tmp_path, monkeypatch):
    import app.export_dir as export_dir
    monkeypatch.setattr(export_dir, "library", library)
    library.upsert_collection("box", "Box", activated=1)
    pid = library.upsert_paper(arxiv_id="2401.01234", title="Deep Vision Models",
                               authors="Chen", year="2024")
    library.add_membership("box", pid)
    pdf_store.copy_into_store(pid, _src_pdf(tmp_path))   # give it a cached PDF

    out = export_dir.export_pdfs("box", tmp_path / "pdfs")
    assert out["copied"] == 1 and out["missing"] == 0
    assert list((tmp_path / "pdfs").glob("*.pdf"))       # a PDF landed
    with pytest.raises(ValueError):
        export_dir.export_pdfs("box", "relative/path")   # absolute-path guard


# --- collection identity: unique slug, rename, duplicate, delete-cascade ---------
def test_import_same_source_twice_makes_independent_collections(wired, tmp_path):
    z = FakeZotero({"K1": Paper("K1", "One", "A", "2024", True)}, _src_pdf(tmp_path))
    s1 = library.activate(z, "my-coll", "eager")
    s2 = library.activate(z, "my-coll", "eager")          # same source again
    assert s1 != s2                                        # distinct slugs (unique id)
    assert library.get_collection(s1)["zotero_name"] == "My Coll"
    assert library.get_collection(s2)["zotero_name"] == "My Coll"


def test_rename_collection_enforces_unique_and_keeps_slug(wired):
    library.upsert_collection("a", "Alpha", activated=1)
    library.upsert_collection("b", "Beta", activated=1)
    ok, _ = library.rename_collection("a", "Beta")        # duplicate name
    assert ok is False
    ok, name = library.rename_collection("a", "Gamma")
    assert ok and name == "Gamma"
    assert library.get_collection("a")["name"] == "Gamma" # slug stable, name changed


def test_delete_cascade_orphans_papers_and_keeps_shared(wired):
    library.upsert_collection("a", "A", activated=1)
    library.upsert_collection("b", "B", activated=1)
    p_solo = library.upsert_paper(zotero_key="S", title="Solo", origin="zotero-import")
    p_shared = library.upsert_paper(zotero_key="SH", title="Shared", origin="zotero-import")
    library.add_membership("a", p_solo); library.add_membership("a", p_shared)
    library.add_membership("b", p_shared)
    con = library.connect()
    con.execute("INSERT INTO paper_notes(paper_id,collection_slug,summary) VALUES(?,?,?)",
                (p_solo, "a", "s"))
    con.commit(); con.close()

    library.delete_collection("a")
    con = library.connect()
    assert con.execute("SELECT 1 FROM papers WHERE id=?", (p_solo,)).fetchone() is None   # orphan purged
    assert con.execute("SELECT 1 FROM papers WHERE id=?", (p_shared,)).fetchone()         # kept (in b)
    assert con.execute("SELECT COUNT(*) FROM paper_notes WHERE paper_id=?", (p_solo,)).fetchone()[0] == 0
    con.close()
    assert [p["id"] for p in library.list_papers("b")] == [p_shared]


def test_duplicate_collection_clones_membership_and_work(wired):
    import json as _json
    library.upsert_collection("a", "A", activated=1)
    pid = library.upsert_paper(zotero_key="K", title="Paper", origin="zotero-import")
    library.add_membership("a", pid)
    library.set_tags("a", [{"label": "x", "color": "#ffffff"}])
    con = library.connect()
    con.execute("INSERT INTO annotations (paper_id,collection_slug,origin,kind,color,page,position_json,selected_text) "
                "VALUES (?,'a','app','highlight','#ff0',1,'{}','q')", (pid,))
    con.commit(); con.close()

    new = library.duplicate_collection("a")
    assert new and new != "a"
    assert library.get_collection(new)["name"] == "A (copy)"
    assert [p["id"] for p in library.list_papers(new)] == [pid]   # membership cloned
    new_tags = library.get_collection(new)["tags"]
    if isinstance(new_tags, str):
        new_tags = _json.loads(new_tags)
    assert [t["label"] for t in new_tags] == ["x"]
    con = library.connect()
    assert con.execute("SELECT COUNT(*) FROM annotations WHERE collection_slug=?", (new,)).fetchone()[0] == 1
    con.close()


# --- add-paper wizard: parse / add_entries / drop -------------------------------
def test_parse_add_input_arxiv_openreview_and_bad(wired, monkeypatch):
    import app.discover as discover
    monkeypatch.setattr(discover, "fetch_arxiv_metadata",
        lambda raw: {"arxiv_id": "2401.12345", "title": "A Paper", "authors": "X",
                     "year": "2024", "abstract": "a"} if "2401.12345" in raw else None)
    monkeypatch.setattr(discover.openreview, "fetch_metadata",
        lambda oid: {"openreview_id": oid, "title": "OR Paper", "authors": "Y",
                     "year": "2023", "abstract": "b"})
    out = discover.parse_add_input(
        "https://arxiv.org/abs/2401.12345, https://openreview.net/forum?id=abc123\ngarbage")
    assert [(e["kind"], e["ok"]) for e in out] == [("arxiv", True), ("openreview", True), (None, False)]
    assert out[1]["id"] == "abc123"


def test_add_entries_creates_and_starts_downloads(wired, monkeypatch):
    import app.triage as triage
    started = []
    monkeypatch.setattr(pdf_store, "has_pdf", lambda pid: False)
    monkeypatch.setattr(pdf_store, "start_download", lambda pid: started.append(pid) or True)
    slug = library.create_local_collection("Box")
    pids = triage.add_entries(slug, [
        {"kind": "arxiv", "id": "2401.1", "title": "A", "authors": "x", "year": "2024", "abstract": ""},
        {"kind": "openreview", "id": "OR1", "title": "B", "authors": "y", "year": "2023", "abstract": ""},
        {"kind": None, "id": None},   # skipped
    ])
    assert len(pids) == 2 and len(started) == 2          # both eager downloads kicked off
    assert {p["title"] for p in library.list_papers(slug)} == {"A", "B"}
    con = library.connect()
    assert con.execute("SELECT openreview_id FROM papers WHERE id=?", (pids[1],)).fetchone()[0] == "OR1"
    con.close()


def test_drop_paper_hard_deletes_no_graveyard(wired, monkeypatch):
    monkeypatch.setattr(pdf_store, "remove_pdf", lambda pid: False)
    monkeypatch.setattr(pdf_store, "clear_download", lambda pid: None)
    slug = library.create_local_collection("Box")
    pid = library.upsert_paper(arxiv_id="2401.9", title="X")
    library.add_membership(slug, pid)
    library.drop_paper(slug, pid)
    con = library.connect()
    assert con.execute("SELECT 1 FROM papers WHERE id=?", (pid,)).fetchone() is None  # hard-deleted
    con.close()
    assert library.list_graveyard(slug) == []            # NOT staged to the graveyard


def test_download_percent_and_states(wired):
    pdf_store._DOWNLOADS.clear()
    pdf_store._DOWNLOADS[7] = {"received": 30, "total": 120, "state": "fetching"}
    assert pdf_store.is_fetching(7) and pdf_store.download_percent(7) == 25
    pdf_store._DOWNLOADS[8] = {"received": 0, "total": None, "state": "fetching"}
    assert pdf_store.download_percent(8) is None         # indeterminate
    pdf_store._DOWNLOADS[9] = {"received": 0, "total": None, "state": "failed"}
    assert pdf_store.download_failed(9) and not pdf_store.is_fetching(9)
    pdf_store._DOWNLOADS.clear()


def test_adding_a_removed_paper_restores_it(wired, monkeypatch):
    import app.triage as triage
    monkeypatch.setattr(pdf_store, "has_pdf", lambda pid: True)        # skip download
    monkeypatch.setattr(pdf_store, "remove_pdf", lambda pid: False)
    slug = library.create_local_collection("Box")
    # one graveyard paper, one permanently-deleted tombstone
    g = library.upsert_paper(arxiv_id="2401.10", title="Grave")
    d = library.upsert_paper(arxiv_id="2401.20", title="Tomb")
    library.add_membership(slug, g); library.add_membership(slug, d)
    library.stage_removal(slug, g)
    library.stage_removal(slug, d); library.permanently_delete(slug, [d])
    assert library.list_papers(slug) == []                            # both hidden
    assert library.removal_tier(slug, arxiv_id="2401.10") == "graveyard"
    assert library.removal_tier(slug, arxiv_id="2401.20") == "deleted"

    # Manually adding either restores it (clears the removal, paper returns visible).
    triage.add_entries(slug, [
        {"kind": "arxiv", "id": "2401.10", "title": "Grave"},
        {"kind": "arxiv", "id": "2401.20", "title": "Tomb"},
    ])
    titles = {p["title"] for p in library.list_papers(slug)}
    assert titles == {"Grave", "Tomb"}                                # both back
    assert library.list_graveyard(slug) == [] and library.list_deleted(slug) == []


def test_browse_directory_lists_dirs_and_pdfs(wired, tmp_path):
    root = tmp_path / "lib"; (root / "sub").mkdir(parents=True)
    (root / "A.pdf").write_bytes(b"%PDF"); (root / "B.pdf").write_bytes(b"%PDF")
    (root / ".hidden").mkdir(); (root / "notes.txt").write_text("x")
    out = library.browse_directory(str(root))
    assert out["dirs"] == ["sub"]                      # hidden + files excluded
    assert out["pdfs"] == ["A.pdf", "B.pdf"]
    assert out["count"] == 2 and out["parent"] == str(root.parent)
    import pytest
    with pytest.raises(ValueError):
        library.browse_directory(str(root / "nope"))


def test_import_directory_async_marks_importing_then_done(wired, tmp_path):
    import time
    folder = tmp_path / "p"; folder.mkdir()
    (folder / "A.pdf").write_bytes(b"%PDF-1.4")
    (folder / "B.pdf").write_bytes(b"%PDF-1.4")
    slug = library.import_directory_async("Async Lib", str(folder))   # returns immediately
    assert library.get_collection(slug) is not None                  # card exists right away
    for _ in range(100):                                             # background thread finishes fast
        if library.import_state(slug) != "running":
            break
        time.sleep(0.02)
    assert library.import_state(slug) == "done"
    assert not library.is_importing(slug)
    assert {p["title"] for p in library.list_papers(slug)} == {"A", "B"}
    # the landing card flag reflects the importing state
    assert [c for c in library.list_collections() if c["slug"] == slug][0]["importing"] is False
