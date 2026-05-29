"""app.sqlite — schema + initialization.

Local-first store (ADR 0001): the app owns a `papers` table (app-owned `id` used in
URLs and as the FK everywhere), `collections`, and `collection_papers` membership.
User-work tables (notes, annotations, chat, triage) key on `papers.id`. No ORM; raw
SQL via stdlib sqlite3.

Migration policy: **clean reset** (ADR 0001). DBs created by the previous
Zotero-keyed schema are backed up to `app.sqlite.bak` and recreated; we never
silently destroy data.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

from .config import DB_PATH, ensure_dirs

log = logging.getLogger("paper_agent.db")

SCHEMA = """
-- App-owned paper store. id is the app identity used in URLs and all FKs.
CREATE TABLE IF NOT EXISTS papers (
  id INTEGER PRIMARY KEY,
  arxiv_id TEXT,                                 -- nullable natural key
  openreview_id TEXT,                            -- nullable; OpenReview note id (PDF source)
  zotero_key TEXT,                               -- nullable; filled after import/sync
  title TEXT NOT NULL DEFAULT '(untitled)',
  authors TEXT DEFAULT '',
  year TEXT DEFAULT '',
  abstract TEXT DEFAULT '',
  origin TEXT CHECK(origin IN ('zotero-import','arxiv-suggested','app-created'))
    NOT NULL DEFAULT 'app-created',
  sync_status TEXT CHECK(sync_status IN ('local-only','synced','dirty'))
    NOT NULL DEFAULT 'local-only',
  pdf_state TEXT CHECK(pdf_state IN ('absent','cached')) NOT NULL DEFAULT 'absent',
  added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Partial uniqueness so multiple NULLs are allowed but a present key is unique.
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_arxiv  ON papers(arxiv_id)  WHERE arxiv_id  IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_zotero ON papers(zotero_key) WHERE zotero_key IS NOT NULL;

-- App-owned collections. slug is the URL token + the FK everywhere.
CREATE TABLE IF NOT EXISTS collections (
  slug TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  zotero_collection_id TEXT,                     -- Zotero collection KEY (string) or NULL
  zotero_name TEXT,                              -- linked Zotero collection's name (for Pull resolution after rename)
  purpose TEXT DEFAULT '',                       -- mirrors collections/<slug>/purpose.md body
  summary TEXT DEFAULT '',                       -- editable; surfaced on landing card
  activated INTEGER NOT NULL DEFAULT 0,          -- 0/1; only activated cols are tracked/imported
  copy_mode TEXT CHECK(copy_mode IN ('eager','lazy')) NOT NULL DEFAULT 'eager',
  tags TEXT NOT NULL DEFAULT '[]',               -- JSON list of {label, color} custom tags
  last_refresh TIMESTAMP,
  last_wiki_regen TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Membership (M:N) with per-membership provenance flag.
CREATE TABLE IF NOT EXISTS collection_papers (
  collection_slug TEXT NOT NULL REFERENCES collections(slug),
  paper_id INTEGER NOT NULL REFERENCES papers(id),
  added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  source_flag TEXT CHECK(source_flag IN
    ('zotero','new-from-zotero','removed-in-zotero','local','arxiv-suggested'))
    NOT NULL DEFAULT 'local',
  PRIMARY KEY (collection_slug, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_colpapers_paper ON collection_papers(paper_id);

-- Removed-paper register (pull-only model; Zotero is never written). A paper the user
-- removed in-app is hidden from the collection but its membership + work are kept, so it
-- can be restored and so a later Pull won't silently re-add it. Survives Refresh.
--   status='graveyard': removed, shown in the Graveyard, one-click Restore.
--   status='deleted':   permanently deleted — a tombstone (work kept), shown in the
--                       Permanently-deleted list; Restore brings it back, Purge forgets it.
--   silent=1:           a merged-away duplicate — hidden from BOTH lists, but still
--                       suppresses re-add on Pull.
-- Every row (any status/silent) suppresses auto re-add on Pull (matched by key/arXiv/title).
CREATE TABLE IF NOT EXISTS pending_removals (
  collection_slug TEXT NOT NULL,
  paper_id INTEGER NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  silent INTEGER NOT NULL DEFAULT 0,
  status TEXT CHECK(status IN ('graveyard','deleted')) NOT NULL DEFAULT 'graveyard',
  PRIMARY KEY (collection_slug, paper_id)
);

-- Per-collection reading log: recency-ordered distinct papers (one row each, opened_at
-- bumped on open). Powers the browser-style "Previous paper" walk-back; pruned to a
-- configurable cap. Back-navigation deliberately does NOT bump opened_at (preserves order).
CREATE TABLE IF NOT EXISTS reading_log (
  collection_slug TEXT NOT NULL,
  paper_id INTEGER NOT NULL,
  opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (collection_slug, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_reading_log ON reading_log(collection_slug, opened_at);

-- User-work tables: keyed by paper_id.
CREATE TABLE IF NOT EXISTS chat_threads (
  id INTEGER PRIMARY KEY,
  collection_slug TEXT NOT NULL,
  paper_id INTEGER,                              -- NULL = collection-wide thread
  agent_session_id TEXT,                         -- CLI agent session id (P8 paper sub-agent)
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP   -- bumped when opened; newest = active
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY,
  thread_id INTEGER REFERENCES chat_threads(id),
  role TEXT CHECK(role IN ('user','assistant','system')),
  content TEXT NOT NULL,
  context_refs TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS paper_notes (
  paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
  collection_slug TEXT NOT NULL,
  summary TEXT,
  thoughts TEXT,
  key_quotes TEXT,
  status TEXT CHECK(status IN ('unread','reading','noted','superseded')) DEFAULT 'unread',
  -- Typed-capture stamps (AGENTIC_PLAN P1). synth_kind 'auto' => resolve by heuristic
  -- (reasoning iff the thoughts field is non-empty); author_origin is door-stamped.
  synth_kind TEXT CHECK(synth_kind IN ('auto','seed','reasoning')) NOT NULL DEFAULT 'auto',
  author_origin TEXT CHECK(author_origin IN ('human','agent','external')) NOT NULL DEFAULT 'human',
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS triage_items (
  id INTEGER PRIMARY KEY,
  collection_slug TEXT NOT NULL,
  paper_id INTEGER,                              -- set once accepted into the store
  zotero_key TEXT,                               -- inbox candidate's Zotero item key (if any)
  arxiv_id TEXT,
  title TEXT,
  abstract TEXT,
  authors TEXT,
  llm_relevance_note TEXT,
  status TEXT CHECK(status IN ('pending','accepted','rejected','deferred')) DEFAULT 'pending',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Addendum Capability 1: app-authored PDF annotations (highlights + notes).
-- Annotation authority = the app (Option A). Zotero-origin annotations are read in
-- one-way for display. position_json mirrors Zotero's {pageIndex, rects} shape so
-- write-back to Zotero could be added later WITHOUT a data-model change.
CREATE TABLE IF NOT EXISTS annotations (
  id INTEGER PRIMARY KEY,
  paper_id INTEGER NOT NULL REFERENCES papers(id),
  collection_slug TEXT NOT NULL,
  origin TEXT CHECK(origin IN ('app','zotero')) DEFAULT 'app',
  kind TEXT CHECK(kind IN ('highlight','note')) DEFAULT 'highlight',
  color TEXT,
  page INTEGER NOT NULL,
  position_json TEXT NOT NULL,
  selected_text TEXT,
  note_text TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_annotations_paper ON annotations(paper_id);

-- Reading-debt queue (AGENTIC_PLAN P7): clusters of seed fragments the user hasn't
-- reasoned over yet, surfaced as questions. id = stable hash of the source fragment
-- ids so re-runs dedupe; status tracks the user's choice (fill/ignore/brainstorm).
CREATE TABLE IF NOT EXISTS reading_debt (
  id TEXT PRIMARY KEY,
  collection_slug TEXT NOT NULL,
  question TEXT NOT NULL,
  sources TEXT NOT NULL DEFAULT '[]',            -- JSON list of fragment ids
  status TEXT CHECK(status IN ('open','filled','ignored','brainstormed'))
    NOT NULL DEFAULT 'open',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_debt_slug ON reading_debt(collection_slug, status);

-- External-content FTS over paper_notes. First column is paper_id; paper_notes.paper_id
-- is an INTEGER PRIMARY KEY so it *is* the rowid (content_rowid='rowid' stays correct).
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  paper_id, collection_slug, summary, thoughts, key_quotes,
  content='paper_notes', content_rowid='rowid'
);

-- Keep the external-content FTS index in sync with paper_notes.
CREATE TRIGGER IF NOT EXISTS paper_notes_ai AFTER INSERT ON paper_notes BEGIN
  INSERT INTO notes_fts(rowid, paper_id, collection_slug, summary, thoughts, key_quotes)
  VALUES (new.rowid, new.paper_id, new.collection_slug, new.summary, new.thoughts, new.key_quotes);
END;
CREATE TRIGGER IF NOT EXISTS paper_notes_ad AFTER DELETE ON paper_notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, paper_id, collection_slug, summary, thoughts, key_quotes)
  VALUES ('delete', old.rowid, old.paper_id, old.collection_slug, old.summary, old.thoughts, old.key_quotes);
END;
CREATE TRIGGER IF NOT EXISTS paper_notes_au AFTER UPDATE ON paper_notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, paper_id, collection_slug, summary, thoughts, key_quotes)
  VALUES ('delete', old.rowid, old.paper_id, old.collection_slug, old.summary, old.thoughts, old.key_quotes);
  INSERT INTO notes_fts(rowid, paper_id, collection_slug, summary, thoughts, key_quotes)
  VALUES (new.rowid, new.paper_id, new.collection_slug, new.summary, new.thoughts, new.key_quotes);
END;
"""


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _needs_reset(con: sqlite3.Connection) -> bool:
    """True for a DB created by the old Zotero-keyed schema: it has app tables
    (e.g. paper_notes) but no `papers` table."""
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return "papers" not in have and "paper_notes" in have


def _backup_path(db_path: Path) -> Path:
    """A `.bak` next to the DB; timestamp-suffixed if one already exists so a second
    reset never clobbers the first backup."""
    bak = Path(str(db_path) + ".bak")
    if bak.exists():
        import time

        bak = Path(f"{db_path}.{int(time.time())}.bak")
    return bak


def init_db(db_path: Path | str = DB_PATH) -> None:
    """Create the schema if absent. On the old (Zotero-keyed) schema, back up and
    recreate (clean reset). Safe to call on every startup."""
    ensure_dirs()
    db_path = Path(db_path)
    # Detect the old schema BEFORE writing so we can back the file up as-is.
    if db_path.exists():
        probe = connect(db_path)
        try:
            reset = _needs_reset(probe)
        finally:
            probe.close()
        if reset:
            backup = _backup_path(db_path)
            shutil.copy2(db_path, backup)
            db_path.unlink()
            log.warning(
                "Old (Zotero-keyed) schema detected; backed up to %s and recreated "
                "with the local-first schema.",
                backup,
            )
    con = connect(db_path)
    try:
        con.executescript(SCHEMA)
        _migrate(con)
        con.commit()
    finally:
        con.close()


def _migrate(con: sqlite3.Connection) -> None:
    """Idempotent column adds for local-first DBs created by an earlier build."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(chat_threads)")}
    if "last_active_at" not in cols:
        # ALTER can't take a non-constant default; backfill from created_at.
        con.execute("ALTER TABLE chat_threads ADD COLUMN last_active_at TIMESTAMP")
        con.execute("UPDATE chat_threads SET last_active_at = created_at WHERE last_active_at IS NULL")
    if "agent_session_id" not in cols:
        con.execute("ALTER TABLE chat_threads ADD COLUMN agent_session_id TEXT")
    ccols = {r[1] for r in con.execute("PRAGMA table_info(collections)")}
    if "tags" not in ccols:
        # Per-collection custom tags: JSON list of {label, color}.
        con.execute("ALTER TABLE collections ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
    if "zotero_name" not in ccols:
        # The linked Zotero collection's own name, captured at import so Pull resolves it by
        # name even after the local collection is renamed (the slug is a stable local id).
        con.execute("ALTER TABLE collections ADD COLUMN zotero_name TEXT")
    if "last_wiki_viewed_at" not in ccols:
        # When the user last opened the wiki page for this collection. Powers the
        # "new since last view" badge in the cheap-reweighting layer (Phase C, 2026-05-29).
        # Read-then-bump on GET only, so a re-render (e.g. after ↻ Regenerate POST) doesn't
        # reset the badge state. NULL = never viewed -> no badges, no fake recency.
        con.execute("ALTER TABLE collections ADD COLUMN last_wiki_viewed_at TIMESTAMP")
    # Typed-capture stamps on notes (AGENTIC_PLAN P1). Defaults make existing notes
    # resolve to (human, heuristic) with no backfill: 'auto' => kind by heuristic.
    ncols = {r[1] for r in con.execute("PRAGMA table_info(paper_notes)")}
    if "synth_kind" not in ncols:
        con.execute(
            "ALTER TABLE paper_notes ADD COLUMN synth_kind TEXT NOT NULL DEFAULT 'auto'"
        )
    if "author_origin" not in ncols:
        con.execute(
            "ALTER TABLE paper_notes ADD COLUMN author_origin TEXT NOT NULL DEFAULT 'human'"
        )
    # Per-collection-paper read state + tags (UI: mark read/unread, per-paper tags that
    # also count as an "attention" signal in duplicate merge). Guarded so _migrate is safe
    # on a partial DB (the table exists after SCHEMA in init_db).
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "collection_papers" in tables:
        cpcols = {r[1] for r in con.execute("PRAGMA table_info(collection_papers)")}
        if "read_at" not in cpcols:
            con.execute("ALTER TABLE collection_papers ADD COLUMN read_at TIMESTAMP")
        if "tags" not in cpcols:
            con.execute("ALTER TABLE collection_papers ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
    if "papers" in tables:
        pcols = {r[1] for r in con.execute("PRAGMA table_info(papers)")}
        if "openreview_id" not in pcols:
            con.execute("ALTER TABLE papers ADD COLUMN openreview_id TEXT")
    if "pending_removals" in tables:
        prcols = {r[1] for r in con.execute("PRAGMA table_info(pending_removals)")}
        if "silent" not in prcols:
            con.execute("ALTER TABLE pending_removals ADD COLUMN silent INTEGER NOT NULL DEFAULT 0")
        if "status" not in prcols:
            con.execute(
                "ALTER TABLE pending_removals ADD COLUMN status TEXT NOT NULL DEFAULT 'graveyard'"
            )
