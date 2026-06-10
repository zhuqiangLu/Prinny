"""Local paper store (ADR 0001) — the data layer the app reads from.

Owns ``papers``, ``collections``, and ``collection_papers``. Routes read papers and
membership from here (not live Zotero). The Zotero adapter is used only by
``activate``/``refresh`` (import), ``pdf_store.ensure_cached``, annotation read-in,
and sync.

``refresh`` implements the non-destructive merge from ADR §4: Zotero wins on metadata,
new-in-Zotero items are flagged, removed/deleted-in-Zotero items are flagged (never
deleted), and unsynced local curation is preserved by construction.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import openreview, pdf_store
from .config import COLLECTIONS_DIR
from .db import connect
from .slugs import slugify
from .zotero import ZoteroBackend

log = logging.getLogger("paper_agent.library")

# Async import progress (single-process app): slug -> {"state": running|done|error}. A
# collection is created immediately and its papers parsed/copied in a background thread, so
# the landing card can show a "parsing…" state and block entry until it finishes.
_IMPORTS: dict[str, dict] = {}


def is_importing(slug: str) -> bool:
    d = _IMPORTS.get(slug)
    return bool(d and d["state"] == "running")


def import_state(slug: str) -> str | None:
    return (_IMPORTS.get(slug) or {}).get("state")

# A "junk" title is one that's really a URL/filename/placeholder, not a real title
# (e.g. bare openreview.net PDF imports store the URL as the title).
_JUNK_TITLE_RE = re.compile(r"^\s*(https?://|www\.|openreview\.net|.*\?id=)", re.IGNORECASE)


def _is_junk_title(t: str | None) -> bool:
    t = (t or "").strip()
    if not t or t == "(untitled)":
        return True
    return bool(_JUNK_TITLE_RE.search(t)) or t.lower().endswith(".pdf") or "openreview.net" in t.lower()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- collections ----------------------------------------------------------------
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _parse_tags(raw) -> list[dict]:
    """Parse a collection's stored tags JSON into a clean [{label, color}] list."""
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for t in items if isinstance(items, list) else []:
        label = str(t.get("label", "")).strip()[:40] if isinstance(t, dict) else ""
        color = str(t.get("color", "")).strip() if isinstance(t, dict) else ""
        if label and _HEX_RE.match(color):
            out.append({"label": label, "color": color})
    return out


def get_collection(slug: str) -> dict | None:
    con = connect()
    try:
        row = con.execute("SELECT * FROM collections WHERE slug=?", (slug,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["tags"] = _parse_tags(d.get("tags"))
        return d
    finally:
        con.close()


def set_tags(slug: str, tags: list[dict]) -> list[dict]:
    """Replace a collection's custom tags (validated [{label, color}]). Returns clean list."""
    clean = _parse_tags(json.dumps(tags))
    con = connect()
    try:
        con.execute("UPDATE collections SET tags=? WHERE slug=?", (json.dumps(clean), slug))
        con.commit()
    finally:
        con.close()
    return clean


ACTIVITY_TYPES = ("chat", "highlights", "notes", "thoughts")
ACTIVITY_WINDOW = 28          # days shown in the card heatmap
HOT_WINDOW = 7                # days the "Hottest" sort scores on (the rightmost week)


def activity_days(window_days: int = ACTIVITY_WINDOW) -> list[str]:
    """The ISO dates (oldest→today, UTC) covered by the hotness window."""
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=window_days - 1 - i)).isoformat() for i in range(window_days)]


def collection_activity(window_days: int = ACTIVITY_WINDOW) -> dict[str, dict]:
    """Per-collection engagement for the last ``window_days``, for the card heatmap.

    Returns {slug: {chat:[…N], highlights:[…N], notes:[…N], thoughts:[…N],
    total:int, hot7:int}} where each list is per-day counts oldest→today, ``total`` is
    the full-window sum and ``hot7`` is the last-7-days sum (what the Hottest sort uses).
    Signals (each event = 1 point): my chat messages, app highlights created, paper-note
    edits, thoughts added. Computed on the fly: 3 grouped queries + a thoughts-dir scan.
    Only slugs with activity appear; callers fill in zeros for the rest.
    """
    days = activity_days(window_days)
    idx = {d: i for i, d in enumerate(days)}
    cutoff = days[0]
    result: dict[str, dict] = {}

    def bucket(slug: str, kind: str, day: str, n: int) -> None:
        if slug is None or day not in idx:
            return
        row = result.setdefault(slug, {k: [0] * window_days for k in ACTIVITY_TYPES})
        row[kind][idx[day]] += n

    con = connect()
    try:
        for slug, day, n in con.execute(
            "SELECT t.collection_slug, substr(m.created_at,1,10), COUNT(*) "
            "FROM chat_messages m JOIN chat_threads t ON t.id = m.thread_id "
            "WHERE m.role='user' AND m.created_at >= ? GROUP BY 1, 2",
            (cutoff,),
        ):
            bucket(slug, "chat", day, n)
        for slug, day, n in con.execute(
            "SELECT collection_slug, substr(created_at,1,10), COUNT(*) FROM annotations "
            "WHERE origin='app' AND created_at >= ? GROUP BY 1, 2",
            (cutoff,),
        ):
            bucket(slug, "highlights", day, n)
        for slug, day, n in con.execute(
            "SELECT collection_slug, substr(updated_at,1,10), COUNT(*) FROM paper_notes "
            "WHERE updated_at >= ? GROUP BY 1, 2",
            (cutoff,),
        ):
            bucket(slug, "notes", day, n)
    finally:
        con.close()

    # Thoughts live on disk as ISO-timestamp-named files: collections/<slug>/thoughts/*.md
    for tdir in COLLECTIONS_DIR.glob("*/thoughts"):
        slug = tdir.parent.name
        for f in tdir.glob("*.md"):
            bucket(slug, "thoughts", f.stem[:10], 1)

    for row in result.values():
        row["total"] = sum(sum(row[k]) for k in ACTIVITY_TYPES)
        row["hot7"] = sum(sum(row[k][-HOT_WINDOW:]) for k in ACTIVITY_TYPES)
    return result


def _empty_activity(window_days: int = ACTIVITY_WINDOW) -> dict:
    return {k: [0] * window_days for k in ACTIVITY_TYPES} | {"total": 0, "hot7": 0}


def list_collections(with_activity: bool = False) -> list[dict]:
    """All collections with per-collection counts for the landing cards.

    ``with_activity`` also attaches the 7-day engagement heatmap data (only the landing
    page needs it; other callers skip the extra work)."""
    con = connect()
    try:
        cols = [dict(r) for r in con.execute("SELECT * FROM collections ORDER BY name")]
        # Aggregate membership + sync counts in one pass.
        counts = {
            r["collection_slug"]: dict(r)
            for r in con.execute(
                """
                SELECT cp.collection_slug,
                       COUNT(*) AS paper_count,
                       MAX(p.added_at) AS last_added,
                       SUM(CASE WHEN p.sync_status='local-only' THEN 1 ELSE 0 END) AS local_only,
                       SUM(CASE WHEN p.sync_status='dirty' THEN 1 ELSE 0 END) AS dirty,
                       SUM(CASE WHEN cp.source_flag='removed-in-zotero' THEN 1 ELSE 0 END) AS removed,
                       SUM(CASE WHEN cp.source_flag='new-from-zotero' THEN 1 ELSE 0 END) AS new_from_zotero,
                       SUM(CASE WHEN cp.read_at IS NULL THEN 1 ELSE 0 END) AS unread
                FROM collection_papers cp JOIN papers p ON p.id = cp.paper_id
                WHERE NOT EXISTS (SELECT 1 FROM pending_removals pr
                                 WHERE pr.collection_slug = cp.collection_slug AND pr.paper_id = cp.paper_id)
                GROUP BY cp.collection_slug
                """
            )
        }
        # Most-recently-opened paper per collection (for the "Recently opened" line).
        # SQLite returns the row matching MAX(opened_at) for the bare columns.
        recents = {
            r["collection_slug"]: dict(r)
            for r in con.execute(
                """
                SELECT rl.collection_slug, p.title AS title, MAX(rl.opened_at) AS opened_at
                FROM reading_log rl JOIN papers p ON p.id = rl.paper_id
                GROUP BY rl.collection_slug
                """
            )
        }
        for c in cols:
            agg = counts.get(c["slug"], {})
            rec = recents.get(c["slug"])
            if rec and rec.get("title"):
                c["recent"] = {"title": rec["title"], "ago": _ago(rec.get("opened_at"))}
            c["paper_count"] = agg.get("paper_count", 0) or 0
            c["local_only"] = agg.get("local_only", 0) or 0
            c["dirty"] = agg.get("dirty", 0) or 0
            c["removed"] = agg.get("removed", 0) or 0
            c["new_from_zotero"] = agg.get("new_from_zotero", 0) or 0
            c["unread"] = agg.get("unread", 0) or 0
            # "Last updated" for card sorting: most recent paper added, else when created.
            stamps = [s for s in (agg.get("last_added"), c.get("last_refresh"), c["created_at"]) if s]
            c["last_activity"] = max(stamps) if stamps else ""
            c["tags"] = _parse_tags(c.get("tags"))
            c["importing"] = is_importing(c["slug"])
        if with_activity:
            act = collection_activity()
            for c in cols:
                c["activity"] = act.get(c["slug"]) or _empty_activity()
        return cols
    finally:
        con.close()


def _ago(ts: str | None) -> str:
    """Human 'time ago' for a stored UTC timestamp ('YYYY-MM-DD HH:MM:SS'). Empty on parse fail."""
    if not ts:
        return ""
    try:
        dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return ""
    secs = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 90:
        return "just now"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{int(secs // n)}{unit} ago"
    return "just now"


def workspace_stats() -> dict:
    """Aggregate counts across all collections for the landing-page stat bar.

    Papers/unread exclude rows hidden by a pending removal (graveyard/deleted)."""
    con = connect()
    try:
        not_removed = (
            "NOT EXISTS (SELECT 1 FROM pending_removals pr "
            "WHERE pr.collection_slug = cp.collection_slug AND pr.paper_id = cp.paper_id)"
        )
        papers = con.execute(
            f"SELECT COUNT(DISTINCT cp.paper_id) n FROM collection_papers cp WHERE {not_removed}"
        ).fetchone()["n"]
        unread = con.execute(
            f"SELECT COUNT(*) n FROM collection_papers cp WHERE cp.read_at IS NULL AND {not_removed}"
        ).fetchone()["n"]
        highlights = con.execute(
            "SELECT COUNT(*) n FROM annotations WHERE kind='highlight'"
        ).fetchone()["n"]
        notes = con.execute(
            "SELECT COUNT(*) n FROM paper_notes WHERE COALESCE(summary,'')<>'' "
            "OR COALESCE(thoughts,'')<>'' OR COALESCE(key_quotes,'')<>''"
        ).fetchone()["n"]
        return {"papers": papers or 0, "highlights": highlights or 0,
                "notes": notes or 0, "unread": unread or 0,
                "storage": _local_storage_human()}
    finally:
        con.close()


def _local_storage_human() -> str:
    """Human-readable estimate of on-disk usage in ~/.prinny: the cached PDF store +
    app.sqlite + the collections/ tree (wiki, notes, thoughts). Best-effort."""
    from .config import APP_DIR, DB_PATH, COLLECTIONS_DIR
    from . import pdf_store

    def _dir_size(p) -> int:
        total = 0
        try:
            for f in p.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        except (OSError, AttributeError):
            pass
        return total

    total = 0
    try:
        total += DB_PATH.stat().st_size if DB_PATH.exists() else 0
    except OSError:
        pass
    total += _dir_size(COLLECTIONS_DIR)
    try:
        store = pdf_store.store_dir()            # configured PDF store path
        if store and store.exists():
            total += _dir_size(store)
    except Exception:  # noqa: BLE001
        total += _dir_size(APP_DIR / "pdfs")     # fall back to the default location
    # Human units.
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.0f} {unit}" if unit in ("B", "KB") else f"{total:.1f} {unit}"
        total /= 1024
    return f"{total:.1f} GB"


def search(q: str, limit: int = 12) -> list[dict]:
    """Full-text-ish search over papers (title/authors) and notes (FTS). Returns
    ``[{paper_id, title, authors, slug, where}]`` for papers that belong to at least one
    collection (so the result is clickable). ``where`` ∈ {'paper','note'}."""
    q = (q or "").strip()
    if not q:
        return []
    con = connect()
    try:
        like = f"%{q}%"
        first_slug = (
            "(SELECT cp.collection_slug FROM collection_papers cp WHERE cp.paper_id = p.id LIMIT 1)"
        )
        out: dict[int, dict] = {}
        for r in con.execute(
            f"""SELECT p.id AS paper_id, p.title, p.authors, {first_slug} AS slug
                FROM papers p WHERE p.title LIKE ? OR p.authors LIKE ? LIMIT ?""",
            (like, like, limit),
        ):
            d = dict(r)
            if d["slug"]:
                d["where"] = "paper"
                out[d["paper_id"]] = d
        # Notes via FTS — quote each token so punctuation can't break MATCH syntax.
        match = " ".join('"' + tok.replace('"', "") + '"' for tok in q.split() if tok)
        if match:
            try:
                for r in con.execute(
                    f"""SELECT p.id AS paper_id, p.title, p.authors, {first_slug} AS slug
                        FROM notes_fts nf JOIN papers p ON p.id = nf.paper_id
                        WHERE notes_fts MATCH ? LIMIT ?""",
                    (match, limit),
                ):
                    d = dict(r)
                    if d["slug"] and d["paper_id"] not in out:
                        d["where"] = "note"
                        out[d["paper_id"]] = d
            except Exception:  # noqa: BLE001 - malformed FTS query → just skip note hits
                pass
        return list(out.values())[:limit]
    finally:
        con.close()


def _file_title_text(path: Path) -> tuple[str, str]:
    """(title, body) for a markdown file: skip YAML frontmatter, title = first heading/line."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return (path.stem, "")
    body = raw
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4:]
    title = path.stem
    for line in body.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            title = s
            break
    return (title[:120], body)


def search_index(text_cap: int = 1200) -> list[dict]:
    """A flat, client-fuzzy-searchable index across papers, notes, thoughts, wiki and chat.
    Each item is ``{id, type, title, sub, url, text}``; files (thoughts/wiki) are read from
    disk, the rest from the DB. Lexical only (no embeddings — see CLAUDE.md)."""
    items: list[dict] = []
    con = connect()
    try:
        first_slug = "(SELECT cp.collection_slug FROM collection_papers cp WHERE cp.paper_id=p.id LIMIT 1)"
        for r in con.execute(
            f"SELECT p.id, p.title, p.authors, p.abstract, {first_slug} AS slug FROM papers p"
        ):
            if not r["slug"]:
                continue
            items.append({
                "id": f"paper-{r['id']}", "type": "paper",
                "title": r["title"] or "(untitled)", "sub": r["authors"] or "",
                "url": f"/c/{r['slug']}/p/{r['id']}",
                "text": f"{r['title'] or ''} {r['authors'] or ''} {r['abstract'] or ''}"[:text_cap]})
        for r in con.execute(
            """SELECT n.paper_id, n.collection_slug AS slug, n.summary, n.thoughts, n.key_quotes,
                      p.title FROM paper_notes n JOIN papers p ON p.id = n.paper_id
               WHERE COALESCE(n.summary,'')<>'' OR COALESCE(n.thoughts,'')<>'' OR COALESCE(n.key_quotes,'')<>''"""
        ):
            body = " ".join(x for x in (r["summary"], r["thoughts"], r["key_quotes"]) if x)
            items.append({
                "id": f"note-{r['paper_id']}", "type": "note", "title": r["title"] or "Note",
                "sub": "note · " + r["slug"], "url": f"/c/{r['slug']}/p/{r['paper_id']}",
                "text": body[:text_cap]})
        for r in con.execute(
            """SELECT m.id, m.content, t.collection_slug AS slug
               FROM chat_messages m JOIN chat_threads t ON t.id = m.thread_id
               WHERE m.role IN ('user','assistant') AND COALESCE(m.content,'')<>''
               ORDER BY m.id DESC LIMIT 500"""
        ):
            c = (r["content"] or "").strip()
            items.append({
                "id": f"chat-{r['id']}", "type": "chat", "title": c[:80],
                "sub": "chat · " + r["slug"], "url": f"/c/{r['slug']}", "text": c[:text_cap]})
        cols = [(row["slug"], row["name"]) for row in con.execute("SELECT slug, name FROM collections")]
    finally:
        con.close()

    for slug, name in cols:
        tdir = COLLECTIONS_DIR / slug / "thoughts"
        if tdir.exists():
            for f in sorted(tdir.glob("*.md")):
                title, text = _file_title_text(f)
                items.append({"id": f"thought-{slug}-{f.stem}", "type": "thought", "title": title,
                              "sub": "thought · " + name, "url": f"/c/{slug}/thoughts", "text": text[:text_cap]})
        wdir = COLLECTIONS_DIR / slug / "wiki"
        if wdir.exists():
            for f in sorted(wdir.rglob("*.md")):
                title, text = _file_title_text(f)
                items.append({"id": f"wiki-{slug}-{f.stem}", "type": "wiki", "title": title,
                              "sub": "wiki · " + name, "url": f"/c/{slug}/wiki", "text": text[:text_cap]})
    return items


def upsert_collection(
    slug: str,
    name: str,
    *,
    zotero_collection_id: str | None = None,
    zotero_name: str | None = None,
    purpose: str | None = None,
    summary: str | None = None,
    copy_mode: str | None = None,
    activated: int | None = None,
) -> None:
    con = connect()
    try:
        existing = con.execute("SELECT slug FROM collections WHERE slug=?", (slug,)).fetchone()
        if existing is None:
            con.execute(
                """INSERT INTO collections (slug, name, zotero_collection_id, zotero_name,
                       purpose, summary, copy_mode, activated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    slug,
                    name,
                    zotero_collection_id,
                    zotero_name,
                    purpose or "",
                    summary or "",
                    copy_mode or "eager",
                    1 if activated else 0,
                ),
            )
        else:
            sets, vals = ["name=?"], [name]
            if zotero_collection_id is not None:
                sets.append("zotero_collection_id=?"); vals.append(zotero_collection_id)
            if zotero_name is not None:
                sets.append("zotero_name=?"); vals.append(zotero_name)
            if purpose is not None:
                sets.append("purpose=?"); vals.append(purpose)
            if summary is not None:
                sets.append("summary=?"); vals.append(summary)
            if copy_mode is not None:
                sets.append("copy_mode=?"); vals.append(copy_mode)
            if activated is not None:
                sets.append("activated=?"); vals.append(1 if activated else 0)
            vals.append(slug)
            con.execute(f"UPDATE collections SET {', '.join(sets)} WHERE slug=?", vals)
        con.commit()
    finally:
        con.close()


def _unique_slug(name: str) -> str:
    """A stable, unique local slug for a new collection. Derived from the name once, then
    de-duplicated; never re-derived on rename (rename changes only the display name)."""
    base = slugify(name) or "collection"
    con = connect()
    try:
        slug, i = base, 2
        while con.execute("SELECT 1 FROM collections WHERE slug=?", (slug,)).fetchone():
            slug, i = f"{base}-{i}", i + 1
        return slug
    finally:
        con.close()


def name_taken(name: str, exclude_slug: str | None = None) -> bool:
    """True if another collection already uses ``name`` (case-insensitive)."""
    con = connect()
    try:
        row = con.execute(
            "SELECT slug FROM collections WHERE lower(name)=lower(?) AND slug<>?",
            (name.strip(), exclude_slug or ""),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def rename_collection(slug: str, new_name: str) -> tuple[bool, str]:
    """Rename a collection (display name only; the slug/id stays stable). Rejects an empty
    or duplicate name. Returns (ok, message)."""
    new_name = (new_name or "").strip()
    if not new_name:
        return False, "Name can't be empty."
    if name_taken(new_name, exclude_slug=slug):
        return False, "Another collection already has that name."
    con = connect()
    try:
        con.execute("UPDATE collections SET name=? WHERE slug=?", (new_name, slug))
        con.commit()
    finally:
        con.close()
    return True, new_name


def set_summary(slug: str, summary: str) -> None:
    con = connect()
    try:
        con.execute("UPDATE collections SET summary=? WHERE slug=?", (summary, slug))
        con.commit()
    finally:
        con.close()


def set_wiki_proactive(slug: str, on: bool) -> None:
    """Per-collection toggle: may the chat proactively propose wiki edits."""
    con = connect()
    try:
        con.execute("UPDATE collections SET wiki_proactive=? WHERE slug=?", (1 if on else 0, slug))
        con.commit()
    finally:
        con.close()


def create_local_collection(name: str, purpose: str = "", summary: str = "") -> str:
    """Create a local-only collection (no Zotero collection yet) with a fresh unique slug."""
    slug = _unique_slug(name)
    upsert_collection(slug, name, purpose=purpose, summary=summary, activated=1)
    return slug


def delete_collection(slug: str) -> None:
    """Delete a collection and everything in it. This collection's work (notes, highlights,
    chat, triage, reading log, removals, memberships) is removed; then any paper that no
    longer belongs to ANY collection is purged with its work (so a later re-import starts
    fresh). Papers still in another collection survive there. Never touches Zotero."""
    paper_ids = list(_members(slug).keys())
    con = connect()
    try:
        con.execute(
            "DELETE FROM chat_messages WHERE thread_id IN "
            "(SELECT id FROM chat_threads WHERE collection_slug=?)",
            (slug,),
        )
        for table in ("chat_threads", "collection_papers", "annotations", "triage_items",
                      "reading_log", "pending_removals"):
            con.execute(f"DELETE FROM {table} WHERE collection_slug=?", (slug,))
        # Notes are global per paper; only drop the ones authored under this collection.
        con.execute("DELETE FROM paper_notes WHERE collection_slug=?", (slug,))
        con.execute("DELETE FROM collections WHERE slug=?", (slug,))
        con.commit()
    finally:
        con.close()
    # Purge papers (and any straggler work) that now belong to no collection.
    for pid in paper_ids:
        _purge_orphan_paper(pid)
    # Remove the on-disk workspace (purpose/wiki/notes/thoughts), if any.
    shutil.rmtree(COLLECTIONS_DIR / slug, ignore_errors=True)


def duplicate_collection(slug: str) -> str | None:
    """Clone a collection into a new independent one (fresh unique slug, name "X (copy)").
    Copies memberships, tags, read-state, reading log, highlights and chat (re-scoped to the
    new slug). Papers + their notes stay shared (global per paper). Returns the new slug."""
    col = get_collection(slug)
    if col is None:
        return None
    base = f"{col['name']} (copy)"
    new_name = base
    n = 2
    while name_taken(new_name):
        new_name = f"{base} {n}"; n += 1
    new_slug = _unique_slug(new_name)
    upsert_collection(
        new_slug, new_name,
        zotero_collection_id=col.get("zotero_collection_id"),
        zotero_name=col.get("zotero_name"),
        purpose=col.get("purpose", ""), summary=col.get("summary", ""),
        copy_mode=col.get("copy_mode", "eager"), activated=1,
    )
    set_tags(new_slug, col.get("tags") or [])   # get_collection already returns a list
    con = connect()
    try:
        con.execute(
            "INSERT INTO collection_papers (collection_slug, paper_id, added_at, source_flag, read_at, tags) "
            "SELECT ?, paper_id, added_at, source_flag, read_at, tags FROM collection_papers WHERE collection_slug=?",
            (new_slug, slug),
        )
        con.execute(
            "INSERT INTO reading_log (collection_slug, paper_id, opened_at) "
            "SELECT ?, paper_id, opened_at FROM reading_log WHERE collection_slug=?",
            (new_slug, slug),
        )
        con.execute(
            "INSERT INTO annotations (paper_id, collection_slug, origin, kind, color, page, "
            "position_json, selected_text, note_text, created_at, updated_at) "
            "SELECT paper_id, ?, origin, kind, color, page, position_json, selected_text, "
            "note_text, created_at, updated_at FROM annotations WHERE collection_slug=?",
            (new_slug, slug),
        )
        # Chat threads + their messages (remap thread ids).
        threads = con.execute(
            "SELECT id, paper_id, agent_session_id, created_at, last_active_at "
            "FROM chat_threads WHERE collection_slug=?", (slug,)).fetchall()
        for t in threads:
            cur = con.execute(
                "INSERT INTO chat_threads (collection_slug, paper_id, agent_session_id, created_at, last_active_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (new_slug, t["paper_id"], t["agent_session_id"], t["created_at"], t["last_active_at"]))
            new_tid = cur.lastrowid
            con.execute(
                "INSERT INTO chat_messages (thread_id, role, content, context_refs, images, created_at) "
                "SELECT ?, role, content, context_refs, images, created_at FROM chat_messages WHERE thread_id=?",
                (new_tid, t["id"]))
        con.commit()
    finally:
        con.close()
    return new_slug


# --- papers ---------------------------------------------------------------------
def _has_pdf(d: dict) -> bool:
    """A PDF is showable if it's already cached OR there's a source to lazily fetch
    (a Zotero key, an arXiv id, or an OpenReview id)."""
    return (d.get("pdf_state") == "cached" or bool(d.get("zotero_key"))
            or bool(d.get("arxiv_id")) or bool(d.get("openreview_id")))


def get_paper(paper_id: int) -> dict | None:
    con = connect()
    try:
        row = con.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["has_pdf"] = _has_pdf(d)
        return d
    finally:
        con.close()


def paper_id_by_pdf_url(pdf_url: str) -> int | None:
    """The id of the paper with this exact pdf_url, or None. Dedups direct-PDF adds
    (which have no arXiv/OpenReview/DOI natural key to dedup on)."""
    url = (pdf_url or "").strip()
    if not url:
        return None
    con = connect()
    try:
        row = con.execute("SELECT id FROM papers WHERE pdf_url=?", (url,)).fetchone()
        return int(row["id"]) if row else None
    finally:
        con.close()


def collection_pdf_urls(slug: str) -> set[str]:
    """The pdf_url of every (non-removed) member of this collection — for the
    Add-paper wizard's 'already in collection' flag on direct-PDF links."""
    con = connect()
    try:
        rows = con.execute(
            """SELECT p.pdf_url FROM collection_papers cp JOIN papers p ON p.id = cp.paper_id
               WHERE cp.collection_slug = ? AND COALESCE(p.pdf_url,'') <> ''
                 AND NOT EXISTS (SELECT 1 FROM pending_removals pr
                     WHERE pr.collection_slug = cp.collection_slug AND pr.paper_id = cp.paper_id)""",
            (slug,)).fetchall()
        return {r["pdf_url"] for r in rows}
    finally:
        con.close()


def _decorate_paper(d: dict) -> dict:
    """Add the derived UI fields a paper row needs (PDF availability + live download state)."""
    d["has_pdf"] = _has_pdf(d)
    d["read"] = bool(d.get("read_at"))
    d["important"] = bool(d.get("important"))
    d["fetching"] = pdf_store.is_fetching(d["id"])
    d["failed"] = pdf_store.download_failed(d["id"])
    d["pct"] = pdf_store.download_percent(d["id"])
    return d


def get_collection_paper(slug: str, paper_id: int) -> dict | None:
    """One paper's row dict (same shape as list_papers) for re-rendering a single row."""
    con = connect()
    try:
        r = con.execute(
            """SELECT p.id, p.title, p.authors, p.year, p.origin, p.sync_status,
                      p.pdf_state, p.zotero_key, p.arxiv_id, p.openreview_id, p.added_at,
                      cp.source_flag, cp.read_at, cp.important
               FROM collection_papers cp JOIN papers p ON p.id = cp.paper_id
               WHERE cp.collection_slug=? AND cp.paper_id=?""",
            (slug, paper_id),
        ).fetchone()
    finally:
        con.close()
    return _decorate_paper(dict(r)) if r else None


def drop_paper(slug: str, paper_id: int) -> None:
    """Hard-remove a paper from a collection WITHOUT the graveyard (used for a failed import).
    Clears download state, drops membership + cached PDF, and purges the paper if it's now an
    orphan. Never stages a removal (no tombstone)."""
    pdf_store.clear_download(paper_id)
    pdf_store.remove_pdf(paper_id)
    remove_membership(slug, paper_id)
    _purge_orphan_paper(paper_id)


def list_papers(slug: str) -> list[dict]:
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT p.id, p.title, p.authors, p.year, p.origin, p.sync_status,
                   p.pdf_state, p.zotero_key, p.arxiv_id, p.openreview_id, p.added_at,
                   cp.source_flag, cp.read_at, cp.important
            FROM collection_papers cp JOIN papers p ON p.id = cp.paper_id
            WHERE cp.collection_slug = ?
              AND NOT EXISTS (SELECT 1 FROM pending_removals pr
                             WHERE pr.collection_slug = cp.collection_slug AND pr.paper_id = cp.paper_id)
            ORDER BY p.title COLLATE NOCASE
            """,
            (slug,),
        ).fetchall()
        out = [_decorate_paper(dict(r)) for r in rows]
        return out
    finally:
        con.close()


def upsert_paper(
    *,
    arxiv_id: str | None = None,
    zotero_key: str | None = None,
    openreview_id: str | None = None,
    doi: str | None = None,
    pdf_url: str | None = None,
    title: str = "(untitled)",
    authors: str = "",
    year: str = "",
    abstract: str = "",
    origin: str = "app-created",
) -> int:
    """Insert or update a paper, deduped by zotero_key then arxiv_id then openreview_id
    then doi. ``pdf_url`` is an open-access PDF source (e.g. Semantic Scholar) preferred
    over arXiv. On update, metadata is overwritten and missing natural keys are
    backfilled; sync_status/origin are left untouched (don't clobber dirty/local state)."""
    con = connect()
    try:
        found = None
        if zotero_key:
            found = con.execute("SELECT * FROM papers WHERE zotero_key=?", (zotero_key,)).fetchone()
        if found is None and arxiv_id:
            found = con.execute("SELECT * FROM papers WHERE arxiv_id=?", (arxiv_id,)).fetchone()
        if found is None and openreview_id:
            found = con.execute("SELECT * FROM papers WHERE openreview_id=?", (openreview_id,)).fetchone()
        if found is None and doi:
            found = con.execute("SELECT * FROM papers WHERE doi=?", (doi,)).fetchone()
        if found is None:
            sync_status = "synced" if origin == "zotero-import" else "local-only"
            cur = con.execute(
                """INSERT INTO papers (arxiv_id, zotero_key, openreview_id, doi, pdf_url,
                       title, authors, year, abstract, origin, sync_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (arxiv_id, zotero_key, openreview_id, doi, pdf_url, title, authors, year,
                 abstract, origin, sync_status),
            )
            con.commit()
            return int(cur.lastrowid)
        # Update metadata; backfill any missing natural key. Don't clobber a good
        # local title with a junk incoming one (e.g. a re-imported openreview URL):
        # this is what keeps a repaired title from reverting on the next Refresh.
        pid = found["id"]
        final_title = title
        if _is_junk_title(title) and not _is_junk_title(found["title"]):
            final_title = found["title"]
        con.execute(
            """UPDATE papers SET title=?, authors=?, year=?, abstract=?,
                   arxiv_id=COALESCE(arxiv_id, ?), zotero_key=COALESCE(zotero_key, ?),
                   openreview_id=COALESCE(openreview_id, ?), doi=COALESCE(doi, ?),
                   pdf_url=COALESCE(pdf_url, ?), updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (final_title, authors, year, abstract, arxiv_id, zotero_key, openreview_id,
             doi, pdf_url, pid),
        )
        con.commit()
        return int(pid)
    finally:
        con.close()


def set_paper_sync_status(paper_id: int, status: str) -> None:
    con = connect()
    try:
        con.execute(
            "UPDATE papers SET sync_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, paper_id),
        )
        con.commit()
    finally:
        con.close()


def set_paper_title(paper_id: int, title: str) -> None:
    con = connect()
    try:
        con.execute(
            "UPDATE papers SET title=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (title, paper_id),
        )
        con.commit()
    finally:
        con.close()


def repair_title(paper_id: int) -> bool:
    """If a paper's title is junk (a URL/filename), try to replace it with the real
    OpenReview title. Returns True if a better title was set. Best-effort/network."""
    paper = get_paper(paper_id)
    if not paper or not _is_junk_title(paper["title"]):
        return False
    real = openreview.title_for(paper["title"])
    if real and not _is_junk_title(real):
        set_paper_title(paper_id, real)
        return True
    return False


def delete_pdf(paper_id: int) -> bool:
    """Remove a paper's cached PDF from the store, reclaiming disk. The paper row
    (title/metadata) and every collection membership are kept untouched. For a
    Zotero- or arXiv-backed paper this just reverts it to the lazy state (re-fetched on
    next open); for a purely-local paper with no source key, this removes the only copy.
    Never deletes the paper itself. Returns whether a file was actually unlinked."""
    return pdf_store.remove_pdf(paper_id)


def set_paper_zotero_key(paper_id: int, zotero_key: str) -> None:
    con = connect()
    try:
        con.execute(
            "UPDATE papers SET zotero_key=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (zotero_key, paper_id),
        )
        con.commit()
    finally:
        con.close()


# --- membership -----------------------------------------------------------------
def add_membership(slug: str, paper_id: int, source_flag: str = "local") -> None:
    con = connect()
    try:
        con.execute(
            """INSERT INTO collection_papers (collection_slug, paper_id, source_flag)
               VALUES (?, ?, ?)
               ON CONFLICT(collection_slug, paper_id) DO NOTHING""",
            (slug, paper_id, source_flag),
        )
        con.commit()
    finally:
        con.close()


def set_membership_flag(slug: str, paper_id: int, source_flag: str) -> None:
    con = connect()
    try:
        con.execute(
            "UPDATE collection_papers SET source_flag=? WHERE collection_slug=? AND paper_id=?",
            (source_flag, slug, paper_id),
        )
        con.commit()
    finally:
        con.close()


def member_zotero_keys(slug: str) -> set[str]:
    """Zotero item keys of papers already in a collection (for inbox dedupe)."""
    con = connect()
    try:
        return {
            r["zotero_key"]
            for r in con.execute(
                """SELECT p.zotero_key FROM collection_papers cp
                   JOIN papers p ON p.id = cp.paper_id
                   WHERE cp.collection_slug=? AND p.zotero_key IS NOT NULL""",
                (slug,),
            )
        }
    finally:
        con.close()


def link_triage_paper(triage_id: int, paper_id: int) -> None:
    con = connect()
    try:
        con.execute("UPDATE triage_items SET paper_id=? WHERE id=?", (paper_id, triage_id))
        con.commit()
    finally:
        con.close()


def remove_membership(slug: str, paper_id: int) -> None:
    con = connect()
    try:
        con.execute(
            "DELETE FROM collection_papers WHERE collection_slug=? AND paper_id=?",
            (slug, paper_id),
        )
        con.execute(
            "DELETE FROM pending_removals WHERE collection_slug=? AND paper_id=?",
            (slug, paper_id),
        )
        con.commit()
    finally:
        con.close()


def mark_read(slug: str, paper_ids: list[int], read: bool = True) -> None:
    """Mark papers read (read_at = now) or unread (read_at = NULL) in this collection."""
    if not paper_ids:
        return
    con = connect()
    try:
        val = _now() if read else None
        con.executemany(
            "UPDATE collection_papers SET read_at=? WHERE collection_slug=? AND paper_id=?",
            [(val, slug, pid) for pid in paper_ids],
        )
        con.commit()
    finally:
        con.close()


def set_important(slug: str, paper_id: int, important: bool) -> None:
    """Flag/unflag a paper as a 'core focus' in this collection (the field model orbits
    around flagged papers; they get full PDF excerpts + are named as core to the agent)."""
    con = connect()
    try:
        con.execute("UPDATE collection_papers SET important=? WHERE collection_slug=? AND paper_id=?",
                    (1 if important else 0, slug, paper_id))
        con.commit()
    finally:
        con.close()


def important_ids(slug: str) -> set:
    """Paper ids flagged important in this collection."""
    con = connect()
    try:
        rows = con.execute("SELECT paper_id FROM collection_papers WHERE collection_slug=? "
                           "AND important=1", (slug,)).fetchall()
    finally:
        con.close()
    return {r["paper_id"] for r in rows}


def log_open(slug: str, paper_id: int, cap: int = 100) -> None:
    """Record a paper open in the per-collection reading log: bump it to the front
    (opened_at = now) and prune to the most-recent ``cap`` papers. Call on a normal open
    (NOT on back-navigation, so the walk-back order is preserved)."""
    con = connect()
    try:
        con.execute(
            "INSERT INTO reading_log (collection_slug, paper_id, opened_at) VALUES (?, ?, ?) "
            "ON CONFLICT(collection_slug, paper_id) DO UPDATE SET opened_at=excluded.opened_at",
            (slug, paper_id, _now()),
        )
        con.execute(
            """DELETE FROM reading_log WHERE collection_slug=? AND paper_id NOT IN (
                   SELECT paper_id FROM reading_log WHERE collection_slug=?
                   ORDER BY opened_at DESC, paper_id DESC LIMIT ?)""",
            (slug, slug, max(1, int(cap))),
        )
        con.commit()
    finally:
        con.close()


def previous_in_log(slug: str, paper_id: int) -> int | None:
    """The paper one step OLDER than ``paper_id`` in the reading log (browser-style back),
    or None if it's the oldest / not logged."""
    con = connect()
    try:
        cur = con.execute(
            "SELECT opened_at FROM reading_log WHERE collection_slug=? AND paper_id=?",
            (slug, paper_id),
        ).fetchone()
        if not cur:
            return None
        row = con.execute(
            "SELECT paper_id FROM reading_log WHERE collection_slug=? AND opened_at < ? "
            "ORDER BY opened_at DESC, paper_id DESC LIMIT 1",
            (slug, cur["opened_at"]),
        ).fetchone()
        return row["paper_id"] if row else None
    finally:
        con.close()


def mark_read_if_unread(slug: str, paper_id: int) -> None:
    """Mark a paper read on first open — only sets read_at if it's currently NULL."""
    con = connect()
    try:
        con.execute(
            "UPDATE collection_papers SET read_at=? "
            "WHERE collection_slug=? AND paper_id=? AND read_at IS NULL",
            (_now(), slug, paper_id),
        )
        con.commit()
    finally:
        con.close()


def stage_removal(slug: str, paper_id: int, silent: bool = False) -> None:
    """Stage a removal: hide the paper from the collection now and drop its cached PDF;
    Sync deletes it from the source. ``silent`` hides it from the Graveyard (used for
    merged-away duplicates). Survives Refresh. Idempotent."""
    con = connect()
    try:
        con.execute(
            "INSERT INTO pending_removals (collection_slug, paper_id, silent) VALUES (?, ?, ?) "
            "ON CONFLICT(collection_slug, paper_id) DO UPDATE SET silent=excluded.silent",
            (slug, paper_id, 1 if silent else 0),
        )
        con.commit()
    finally:
        con.close()
    pdf_store.remove_pdf(paper_id)


def _list_removals(slug: str, status: str) -> list[dict]:
    """Removed papers of one tier (silent=0), newest first, with metadata for the UI."""
    con = connect()
    try:
        return [
            dict(r)
            for r in con.execute(
                """SELECT p.id, p.title, p.authors, p.year,
                          substr(COALESCE(p.abstract,''), 1, 280) AS abstract,
                          p.zotero_key, pr.created_at
                   FROM pending_removals pr JOIN papers p ON p.id = pr.paper_id
                   WHERE pr.collection_slug = ? AND pr.silent = 0 AND pr.status = ?
                   ORDER BY pr.created_at DESC, pr.paper_id DESC""",
                (slug, status),
            )
        ]
    finally:
        con.close()


def list_graveyard(slug: str) -> list[dict]:
    """Removed-but-restorable papers (the Graveyard tier), newest first. Merged-away
    duplicates (silent) and permanently-deleted tombstones are excluded."""
    return _list_removals(slug, "graveyard")


def list_deleted(slug: str) -> list[dict]:
    """Permanently-deleted papers (tombstones; their work is kept), newest first."""
    return _list_removals(slug, "deleted")


def graveyard_count(slug: str) -> int:
    """Count for the header badge: removed papers across both tiers (excludes silent)."""
    con = connect()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM pending_removals WHERE collection_slug=? AND silent=0", (slug,)
        ).fetchone()[0]
    finally:
        con.close()


def restore_removal(slug: str, paper_id: int) -> None:
    """Un-remove a paper (either tier): it returns to the collection with its work intact
    (its dropped PDF re-fetches on next open). The membership row was kept, so clearing the
    removal row is enough."""
    con = connect()
    try:
        con.execute(
            "DELETE FROM pending_removals WHERE collection_slug=? AND paper_id=?",
            (slug, paper_id),
        )
        con.commit()
    finally:
        con.close()


def removal_tier(slug: str, *, arxiv_id: str | None = None, openreview_id: str | None = None,
                 zotero_key: str | None = None) -> str | None:
    """If a paper (matched by any natural key) is currently removed from ``slug``, return its
    tier ('graveyard' or 'deleted'); else None. Silent (merged-away) removals return None —
    they're not user-facing. Used so the Add wizard can say a paste would *restore* a paper."""
    con = connect()
    try:
        pid = None
        for col, val in (("zotero_key", zotero_key), ("arxiv_id", arxiv_id),
                         ("openreview_id", openreview_id)):
            if pid is None and val:
                r = con.execute(f"SELECT id FROM papers WHERE {col}=?", (val,)).fetchone()
                pid = r["id"] if r else None
        if pid is None:
            return None
        row = con.execute(
            "SELECT status, silent FROM pending_removals WHERE collection_slug=? AND paper_id=?",
            (slug, pid),
        ).fetchone()
        return None if (not row or row["silent"]) else row["status"]
    finally:
        con.close()


def permanently_delete(slug: str, paper_ids: list[int]) -> int:
    """Move Graveyard removals to the permanently-deleted tier (tombstone). The paper's work
    is kept (recoverable via Restore); only the tier/intent changes. Returns the count moved."""
    if not paper_ids:
        return 0
    con = connect()
    try:
        cur = con.executemany(
            "UPDATE pending_removals SET status='deleted' "
            "WHERE collection_slug=? AND paper_id=? AND status='graveyard'",
            [(slug, pid) for pid in paper_ids],
        )
        con.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(paper_ids)
    finally:
        con.close()


def purge_removals(slug: str, paper_ids: list[int]) -> int:
    """Forget tombstones entirely: drop the removal row + the collection membership, and if
    the paper then belongs to no collection, delete the paper row and all its work (notes,
    chat, highlights, triage links, reading log). After purge a future Pull may re-add it as
    a brand-new paper. Returns the count purged."""
    if not paper_ids:
        return 0
    purged = 0
    for pid in paper_ids:
        con = connect()
        try:
            con.execute("DELETE FROM pending_removals WHERE collection_slug=? AND paper_id=?", (slug, pid))
            con.execute("DELETE FROM collection_papers WHERE collection_slug=? AND paper_id=?", (slug, pid))
            con.commit()
        finally:
            con.close()
        pdf_store.remove_pdf(pid)
        _purge_orphan_paper(pid)
        purged += 1
    return purged


def _removed_paper_ids(slug: str) -> set[int]:
    """paper_ids with a removal row in ``slug`` (any tier, including silent merges)."""
    con = connect()
    try:
        return {r[0] for r in con.execute(
            "SELECT paper_id FROM pending_removals WHERE collection_slug=?", (slug,))}
    finally:
        con.close()


def removed_index(slug: str) -> dict:
    """Lookup tables for suppressing re-add on Pull: every removed paper in ``slug`` (any
    tier, including silent merges) by zotero_key, arXiv id, and normalized title."""
    con = connect()
    try:
        rows = con.execute(
            """SELECT p.zotero_key, p.arxiv_id, p.title
               FROM pending_removals pr JOIN papers p ON p.id = pr.paper_id
               WHERE pr.collection_slug = ?""",
            (slug,),
        ).fetchall()
    finally:
        con.close()
    return {
        "keys": {r["zotero_key"] for r in rows if r["zotero_key"]},
        "arxiv": {r["arxiv_id"] for r in rows if r["arxiv_id"]},
        "titles": {_norm_title(r["title"]) for r in rows if r["title"]},
    }


def next_unread(slug: str, current_id: int) -> int | None:
    """The next 'unread' paper in a collection (title order), starting after the current
    one and wrapping around. Unread = no note row, or note status 'unread'. Returns a
    paper_id or None when there's no other unread paper."""
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT p.id AS id, COALESCE(n.status, 'unread') AS status
            FROM collection_papers cp JOIN papers p ON p.id = cp.paper_id
            LEFT JOIN paper_notes n
                   ON n.paper_id = p.id AND n.collection_slug = cp.collection_slug
            WHERE cp.collection_slug = ?
            ORDER BY p.title COLLATE NOCASE, p.id
            """,
            (slug,),
        ).fetchall()
    finally:
        con.close()
    ids = [r["id"] for r in rows]
    unread = [r["id"] for r in rows if r["status"] == "unread" and r["id"] != current_id]
    if not unread:
        return None
    if current_id in ids:                       # first unread after the current one…
        for r in rows[ids.index(current_id) + 1:]:
            if r["status"] == "unread" and r["id"] != current_id:
                return r["id"]
    return unread[0]                            # …else wrap to the first unread


# --- duplicate detection + merge ------------------------------------------------
_WS_RE = re.compile(r"\s+")


def _norm_title(title: str) -> str:
    """Normalize a title for duplicate grouping: trim, lowercase, collapse whitespace."""
    return _WS_RE.sub(" ", (title or "").strip().lower())


def paper_engagement(paper_id: int) -> dict:
    """The user's 'attention pattern' for a paper: chat-message count, whether notes were
    written, and highlight count. ``has_attention`` is true if any is non-empty — that's
    what tells an empty duplicate (safe to drop) apart from one worth merging."""
    con = connect()
    try:
        chat = con.execute(
            "SELECT COUNT(*) FROM chat_messages m JOIN chat_threads t ON t.id = m.thread_id "
            "WHERE t.paper_id = ? AND m.role IN ('user','assistant')",
            (paper_id,),
        ).fetchone()[0]
        notes = con.execute(
            "SELECT COUNT(*) FROM paper_notes WHERE paper_id = ? AND "
            "TRIM(COALESCE(summary,'') || COALESCE(thoughts,'') || COALESCE(key_quotes,'')) <> ''",
            (paper_id,),
        ).fetchone()[0]
        highlights = con.execute(
            "SELECT COUNT(*) FROM annotations WHERE paper_id = ?", (paper_id,)
        ).fetchone()[0]
    finally:
        con.close()
    return {"chat": chat, "notes": bool(notes), "highlights": highlights,
            "has_attention": bool(chat or notes or highlights)}


def find_duplicate_groups(slug: str) -> list[dict]:
    """Papers in a collection that share a normalized title, grouped (≥2 members each).
    Each group enriches its members with engagement, recommends a ``keep_id`` (the
    most-engaged member), and labels the action: ``remove`` when at most one member has
    any attention (the empties just go), else ``merge`` (consolidate into keep)."""
    by_title: dict[str, list] = {}
    for p in list_papers(slug):
        by_title.setdefault(_norm_title(p["title"]), []).append(p)

    groups: list[dict] = []
    for members in by_title.values():
        if len(members) < 2:
            continue
        enriched = [{**m, "engagement": paper_engagement(m["id"])} for m in members]

        def _score(x: dict) -> tuple:
            e = x["engagement"]
            return (e["chat"] + (1 if e["notes"] else 0) + e["highlights"],
                    1 if x.get("has_pdf") else 0, -x["id"])

        enriched.sort(key=_score, reverse=True)
        n_attn = sum(1 for x in enriched if x["engagement"]["has_attention"])
        groups.append({
            "title": members[0]["title"],
            "members": enriched,
            "keep_id": enriched[0]["id"],
            "engaged": n_attn,
            "action": "remove" if n_attn <= 1 else "merge",
        })
    groups.sort(key=lambda g: g["title"].lower())
    return groups


def _delete_orphan_paper(paper_id: int) -> None:
    """Delete a paper row once it belongs to no collection (post-merge cleanup)."""
    con = connect()
    try:
        if not con.execute(
            "SELECT 1 FROM collection_papers WHERE paper_id = ? LIMIT 1", (paper_id,)
        ).fetchone():
            con.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
            con.commit()
    finally:
        con.close()


def _purge_orphan_paper(paper_id: int) -> None:
    """Hard-delete a paper AND all its work (notes, chat, highlights, triage links, reading
    log, removal rows) — but only if it belongs to no collection. Used by purge; FK
    constraints require clearing dependents before the papers row."""
    con = connect()
    try:
        if con.execute(
            "SELECT 1 FROM collection_papers WHERE paper_id = ? LIMIT 1", (paper_id,)
        ).fetchone():
            return  # still a member somewhere — keep it
        con.execute(
            "DELETE FROM chat_messages WHERE thread_id IN "
            "(SELECT id FROM chat_threads WHERE paper_id = ?)", (paper_id,))
        con.execute("DELETE FROM chat_threads WHERE paper_id = ?", (paper_id,))
        con.execute("DELETE FROM paper_notes WHERE paper_id = ?", (paper_id,))
        con.execute("DELETE FROM annotations WHERE paper_id = ?", (paper_id,))
        con.execute("UPDATE triage_items SET paper_id = NULL WHERE paper_id = ?", (paper_id,))
        con.execute("DELETE FROM reading_log WHERE paper_id = ?", (paper_id,))
        con.execute("DELETE FROM pending_removals WHERE paper_id = ?", (paper_id,))
        con.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        con.commit()
    finally:
        con.close()


def merge_papers(slug: str, keep_id: int, drop_ids: list[int]) -> dict:
    """Merge duplicate papers into ``keep_id``. For each drop, re-point its chat threads,
    highlights, triage links, and memberships to keep (notes never lose text). A drop that
    has a Zotero item is then remembered as a silent removal in ``slug`` (hidden from the
    Graveyard, but suppresses re-add on the next Pull); a local-only drop is hard-deleted.
    ``keep`` replaces ``drop`` everywhere — correct because they're the same paper."""
    drop_ids = [d for d in dict.fromkeys(drop_ids) if d != keep_id]
    if not drop_ids:
        return {"merged": 0, "keep_id": keep_id}
    con = connect()
    try:
        keys = {d: (con.execute("SELECT zotero_key FROM papers WHERE id=?", (d,)).fetchone() or [None])[0]
                for d in drop_ids}
        for d in drop_ids:
            keep_note = con.execute("SELECT * FROM paper_notes WHERE paper_id=?", (keep_id,)).fetchone()
            drop_note = con.execute("SELECT * FROM paper_notes WHERE paper_id=?", (d,)).fetchone()
            if drop_note and not keep_note:
                con.execute("UPDATE paper_notes SET paper_id=? WHERE paper_id=?", (keep_id, d))
            elif drop_note and keep_note:
                fields = {}
                for f in ("summary", "thoughts", "key_quotes"):
                    a, b = (keep_note[f] or "").strip(), (drop_note[f] or "").strip()
                    fields[f] = a if not b else b if not a else \
                        f"{a}\n\n— merged from duplicate —\n\n{b}"
                con.execute(
                    "UPDATE paper_notes SET summary=?, thoughts=?, key_quotes=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE paper_id=?",
                    (fields["summary"], fields["thoughts"], fields["key_quotes"], keep_id))
                con.execute("DELETE FROM paper_notes WHERE paper_id=?", (d,))
            con.execute("UPDATE chat_threads SET paper_id=? WHERE paper_id=?", (keep_id, d))
            con.execute("UPDATE annotations SET paper_id=? WHERE paper_id=?", (keep_id, d))
            con.execute("UPDATE triage_items SET paper_id=? WHERE paper_id=?", (keep_id, d))
            # membership: keep replaces drop everywhere (skip collections keep is already in)
            con.execute("UPDATE OR IGNORE collection_papers SET paper_id=? WHERE paper_id=?", (keep_id, d))
            con.execute("DELETE FROM collection_papers WHERE paper_id=?", (d,))
        con.commit()
    finally:
        con.close()
    remembered = 0
    for d in drop_ids:                       # PDFs/rows: outside the txn (own connections)
        pdf_store.remove_pdf(d)
        if keys.get(d):                      # has a Zotero item -> remember (suppress re-add on Pull)
            stage_removal(slug, d, silent=True)
            remembered += 1
        else:                                # local-only -> nothing in Zotero, hard-delete
            _delete_orphan_paper(d)
    return {"merged": len(drop_ids), "keep_id": keep_id, "remembered": remembered}


def move_paper(from_slug: str, to_slug: str, paper_id: int) -> None:
    """Move a paper's membership from one collection to another. The paper and its PDF
    are stored globally per paper-id, so the cached PDF (and notes/highlights, which are
    scoped per collection) follow according to their own rules — only the collection
    listing changes here. No-op if source and target are the same."""
    if from_slug == to_slug:
        return
    add_membership(to_slug, paper_id, "local")
    remove_membership(from_slug, paper_id)


def _active_member_keys(slug: str) -> dict[str, str]:
    """zotero_key -> title for papers that are active (visible) members: a membership row and
    NO removal row. Removed papers keep their membership row, so they're excluded here."""
    con = connect()
    try:
        return {
            r["zotero_key"]: r["title"]
            for r in con.execute(
                "SELECT p.zotero_key, p.title FROM collection_papers cp "
                "JOIN papers p ON p.id = cp.paper_id "
                "WHERE cp.collection_slug=? AND p.zotero_key IS NOT NULL "
                "  AND NOT EXISTS (SELECT 1 FROM pending_removals pr "
                "                  WHERE pr.collection_slug=cp.collection_slug AND pr.paper_id=cp.paper_id)",
                (slug,),
            )
        }
    finally:
        con.close()


def pull_preview(z, slug: str) -> dict:
    """Read-only: what a Pull from the linked Zotero collection would do, WITHOUT writing.
    Partitions Zotero's papers into:
      incoming_new  — truly new, will be added automatically;
      held          — match a paper you previously removed (graveyard/deleted/merged), so
                      they're held back for you to pick (re-add) instead of auto-added;
      incoming_gone — active members no longer in Zotero (kept locally, flagged).
    Empty if the collection isn't linked. May raise if Zotero is unreachable."""
    empty = {"incoming_new": [], "held": [], "incoming_gone": []}
    col = get_collection(slug)
    if not col or not col.get("zotero_collection_id"):
        return empty
    zc = _resolve_linked(z, col)
    if zc is None:
        return empty
    zpapers = list(z.list_papers(zc.id))
    zkeys = {zp.key for zp in zpapers}
    active = _active_member_keys(slug)
    removed = removed_index(slug)
    incoming_new, held = [], []
    for zp in zpapers:
        if zp.key in active:
            continue
        if zp.key in removed["keys"] or _norm_title(zp.title) in removed["titles"]:
            held.append({"title": zp.title, "zotero_key": zp.key})
        else:
            incoming_new.append({"title": zp.title, "zotero_key": zp.key})
    incoming_gone = [{"title": t, "zotero_key": k}
                     for k, t in active.items() if k not in zkeys]
    return {"incoming_new": incoming_new, "held": held, "incoming_gone": incoming_gone}


def _members(slug: str) -> dict[int, str]:
    """paper_id -> source_flag for a collection."""
    con = connect()
    try:
        return {
            r["paper_id"]: r["source_flag"]
            for r in con.execute(
                "SELECT paper_id, source_flag FROM collection_papers WHERE collection_slug=?",
                (slug,),
            )
        }
    finally:
        con.close()


# --- import / refresh -----------------------------------------------------------
def _resolve_linked(z, col: dict):
    """The Zotero Collection a local collection is linked to. Resolved by the stored Zotero
    name (so it survives a local rename); falls back to the slug for legacy rows."""
    target = col.get("zotero_name") or col.get("slug")
    return z.resolve_collection_id(slugify(target))


def activate(z: ZoteroBackend, zslug: str, copy_mode: str = "eager", name: str | None = None,
             only_keys: list[str] | None = None) -> str:
    """Import a Zotero collection as a NEW independent local collection (fresh unique slug, so
    importing the same source again gives a distinct collection). ``zslug`` resolves the Zotero
    collection; ``name`` overrides the display name; ``only_keys`` limits the initial import to
    those Zotero item keys (default: all). Returns the new local slug."""
    col = z.resolve_collection_id(zslug)
    if col is None:
        raise ValueError(f"No Zotero collection resolves to '{zslug}'")
    disp = (name or "").strip() or col.name
    new_slug = _unique_slug(disp)
    upsert_collection(
        new_slug, disp, zotero_collection_id=str(col.id), zotero_name=col.name,
        copy_mode=copy_mode, activated=1,
    )
    refresh(z, new_slug, only_keys=only_keys)
    return new_slug


_ARXIV_WATERMARK = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)


def _pdf_title(path: Path) -> str | None:
    """Best-effort title for an imported PDF: its embedded metadata title, else None."""
    return _local_pdf_meta(path).get("title")


def _heuristic_pdf_meta(text: str) -> tuple[str | None, str | None]:
    """Best-effort title/authors from a paper's first-page text (no LLM). The title is the
    leading line(s) before the abstract; a line ending in ':' is treated as a wrapped title and
    joined with the next. The first non-email line after the title is taken as the authors."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return None, None
    cut = next((i for i, l in enumerate(lines[:30]) if l.lower().lstrip().startswith("abstract")), None)
    head = lines[:cut] if cut is not None else lines[:6]
    if not head or "@" in head[0] or head[0][:1].isdigit():
        return None, None
    title = head[0]
    rest = head[1:]
    if title.endswith(":") and rest:        # wrapped title (e.g. "DeepSeek-V4:" + next line)
        title = f"{title} {rest[0]}".strip()
        rest = rest[1:]
    authors = next((l for l in rest if "@" not in l and not l.lower().startswith(("http", "www"))), "")
    return title or None, authors or None


def _local_pdf_meta(path: Path) -> dict:
    """Deterministic (no-LLM) metadata from a PDF: an arXiv id (from the filename or the
    page-1 ``arXiv:NNNN.NNNNN`` watermark) plus a title/authors — from the embedded PDF
    metadata, else heuristically from the first-page text. The arXiv id (if found) lets the
    caller fetch authoritative metadata from the arXiv API."""
    from .discover import normalize_arxiv_id

    out = {"arxiv_id": normalize_arxiv_id(path.stem) or None, "title": None, "authors": None}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        meta = reader.metadata
        first_text = (reader.pages[0].extract_text() or "") if reader.pages else ""
        if not out["arxiv_id"]:
            m = _ARXIV_WATERMARK.search(first_text)
            if m:
                out["arxiv_id"] = m.group(1)
        embedded_title = (meta.title or "").strip() if meta else ""
        embedded_author = (meta.author or "").strip() if meta else ""
        if embedded_title:
            out["title"], out["authors"] = embedded_title, embedded_author or None
        else:                                # nothing embedded -> read it off page 1
            out["title"], out["authors"] = _heuristic_pdf_meta(first_text)
    except Exception:  # noqa: BLE001 - any parse error -> fall back to filename
        pass
    return out


def _do_directory_import(slug: str, folder: Path, only_files: list[str] | None) -> None:
    """Parse + import a folder's PDFs into an existing collection (the slow part). Metadata is
    recovered without an LLM: arXiv id (filename or page-1 watermark) → arXiv API, else the
    embedded PDF title, else the filename."""
    pdfs = sorted(p for p in folder.glob("*.pdf") if p.is_file())
    if only_files is not None:
        pick = set(only_files)
        pdfs = [p for p in pdfs if p.name in pick]
    local = {f: _local_pdf_meta(f) for f in pdfs}
    from . import discover
    arxiv_meta = discover.fetch_arxiv_batch([m["arxiv_id"] for m in local.values() if m["arxiv_id"]])
    for f in pdfs:
        lm = local[f]
        am = arxiv_meta.get(lm["arxiv_id"]) if lm["arxiv_id"] else None
        if am:                                    # arXiv gave us the real metadata
            pid = upsert_paper(arxiv_id=am["arxiv_id"], title=am["title"], authors=am["authors"],
                               year=am["year"], abstract=am["abstract"], origin="app-created")
        else:                                     # fall back to embedded title, else filename
            pid = upsert_paper(arxiv_id=lm["arxiv_id"], title=lm["title"] or f.stem,
                               authors=lm["authors"] or "", origin="app-created")
        add_membership(slug, pid, "local")
        pdf_store.copy_into_store(pid, f)        # copy now — the source folder may move


def import_directory(name: str, path: str, tags: list | None = None,
                     only_files: list[str] | None = None) -> str:
    """Synchronous folder import (used by tests). Creates the collection and imports its PDFs.
    Returns the new slug. Raises ValueError on a bad path."""
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise ValueError("That folder doesn't exist.")
    slug = create_local_collection((name or "").strip() or folder.name)
    if tags:
        set_tags(slug, tags)
    _do_directory_import(slug, folder, only_files)
    return slug


def _run_async_import(slug: str, fn) -> None:
    """Mark ``slug`` importing, run ``fn`` in a daemon thread, flip the state when done."""
    _IMPORTS[slug] = {"state": "running"}

    def _run() -> None:
        try:
            fn()
            _IMPORTS[slug] = {"state": "done"}
        except Exception as exc:  # noqa: BLE001 - surface in the card
            log.exception("async import failed for %s", slug)
            _IMPORTS[slug] = {"state": "error", "error": str(exc)}

    threading.Thread(target=_run, daemon=True).start()


def _maybe_seed_wiki(slug: str, draft_wiki: bool) -> None:
    """After an import, optionally seed a starter wiki from the papers' abstracts (default-on
    import option). Best-effort: a failure here must not fail the import."""
    if not draft_wiki:
        return
    try:
        from . import wiki
        wiki.generate_overview(slug)
    except Exception:  # noqa: BLE001 - never let wiki seeding break the import
        log.exception("wiki seeding failed for %s", slug)


def import_directory_async(name: str, path: str, tags: list | None = None,
                           only_files: list[str] | None = None, draft_wiki: bool = True) -> str:
    """Create the collection immediately (so its card appears) and parse/import the PDFs in a
    background thread, optionally seeding a starter wiki. Returns the new slug; the card shows
    a 'parsing' state until done."""
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise ValueError("That folder doesn't exist.")
    slug = create_local_collection((name or "").strip() or folder.name)
    if tags:
        set_tags(slug, tags)

    def _job() -> None:
        _do_directory_import(slug, folder, only_files)
        _maybe_seed_wiki(slug, draft_wiki)

    _run_async_import(slug, _job)
    return slug


def activate_async(z: ZoteroBackend, zslug: str, copy_mode: str = "eager",
                   name: str | None = None, only_keys: list[str] | None = None,
                   draft_wiki: bool = True) -> str:
    """Create the linked collection immediately, then pull its papers (and optionally seed a
    starter wiki) in a background thread. Returns the new slug; the card shows a 'parsing'
    state until the pull finishes."""
    col = z.resolve_collection_id(zslug)
    if col is None:
        raise ValueError(f"No Zotero collection resolves to '{zslug}'")
    disp = (name or "").strip() or col.name
    new_slug = _unique_slug(disp)
    upsert_collection(new_slug, disp, zotero_collection_id=str(col.id), zotero_name=col.name,
                      copy_mode=copy_mode, activated=1)

    def _job() -> None:
        refresh(z, new_slug, only_keys=only_keys)
        _maybe_seed_wiki(new_slug, draft_wiki)

    _run_async_import(new_slug, _job)
    return new_slug


def scan_directory_pdfs(path: str) -> dict:
    """Read-only preview of a folder's PDFs (filenames only). Raises ValueError on bad path."""
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise ValueError("That folder doesn't exist.")
    names = sorted(p.name for p in folder.glob("*.pdf") if p.is_file())
    return {"dir": str(folder), "count": len(names), "pdfs": names}


def browse_directory(path: str | None) -> dict:
    """One level of a folder for the import explorer: its subfolders + PDFs (filenames). Empty
    ``path`` starts at the home directory. Returns {path, parent, dirs, pdfs, count}."""
    folder = (Path(path).expanduser() if path else Path.home())
    if not folder.is_dir():
        raise ValueError("That folder doesn't exist.")
    folder = folder.resolve()
    dirs, pdfs = [], []
    try:
        for entry in folder.iterdir():
            name = entry.name
            if name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append(name)
                elif entry.is_file() and name.lower().endswith(".pdf"):
                    pdfs.append(name)
            except OSError:
                continue
    except PermissionError:
        raise ValueError("Permission denied for that folder.")
    parent = str(folder.parent) if folder.parent != folder else None
    return {"path": str(folder), "parent": parent,
            "dirs": sorted(dirs, key=str.lower), "pdfs": sorted(pdfs, key=str.lower),
            "count": len(pdfs)}


def refresh(z: ZoteroBackend, slug: str, readd_keys: list[str] | None = None,
            only_keys: list[str] | None = None) -> dict:
    """Pull from the linked Zotero collection (pull-only model — Zotero is never written).
    Adds truly-new papers automatically. Papers matching one you previously removed are HELD
    (not auto-added) unless their zotero_key is in ``readd_keys`` (the re-add picker), in
    which case they're restored. ``only_keys`` (initial-import selection) restricts which
    Zotero items are considered. Members gone from Zotero are flagged (never deleted)."""
    col = get_collection(slug)
    if col is None or not col.get("zotero_collection_id"):
        raise ValueError(f"Collection '{slug}' is not linked to a Zotero collection")
    zc = _resolve_linked(z, col)
    if zc is None:
        raise ValueError(f"Zotero collection for '{slug}' not found")
    eager = col["copy_mode"] == "eager"
    readd = set(readd_keys or [])
    only = set(only_keys) if only_keys is not None else None

    existing = _members(slug)                 # paper_id -> source_flag (includes removed members)
    removed_pids = _removed_paper_ids(slug)   # paper_ids with a removal row (any tier)
    removed = removed_index(slug)
    zkeys_seen: set[str] = set()
    added = readded = held = removed_in_zotero = 0

    for zp in z.list_papers(zc.id):
        if only is not None and zp.key not in only:
            continue                          # initial import: skip unselected papers
        zkeys_seen.add(zp.key)
        is_removed = zp.key in removed["keys"] or _norm_title(zp.title) in removed["titles"]
        if is_removed and zp.key not in readd:
            held += 1
            continue                          # leave it removed; the picker can bring it back

        full = {}
        try:
            full = z.paper_full(zp.key) or {}
        except NotImplementedError:
            pass
        pid = upsert_paper(
            zotero_key=zp.key, title=zp.title, authors=zp.authors, year=zp.year,
            abstract=full.get("abstract", ""), origin="zotero-import",
        )
        set_paper_sync_status(pid, "synced")  # Zotero-origin papers are, by definition, in Zotero
        repair_title(pid)                     # bare openreview.net imports store the URL as title

        if zp.key in readd and pid in removed_pids:
            restore_removal(slug, pid)        # un-remove (clears the removal row)
            if pid not in existing:
                add_membership(slug, pid, "new-from-zotero")
            readded += 1
            if eager:
                pdf_store.copy_into_store(pid, z.pdf_path(zp.key))
        elif pid not in existing:
            add_membership(slug, pid, "new-from-zotero")
            added += 1
            if eager:
                pdf_store.copy_into_store(pid, z.pdf_path(zp.key))
        elif existing[pid] == "removed-in-zotero":
            set_membership_flag(slug, pid, "zotero")   # it came back in Zotero

    # zotero-origin members gone from Zotero -> flag removed-in-zotero (never delete). A paper
    # the user removed locally is skipped (it has a removal row; it's not "gone from Zotero").
    for pid, flag in existing.items():
        if pid in removed_pids:
            continue
        paper = get_paper(pid)
        if (paper and paper["origin"] == "zotero-import"
                and paper["zotero_key"] not in zkeys_seen and flag != "removed-in-zotero"):
            set_membership_flag(slug, pid, "removed-in-zotero")
            removed_in_zotero += 1

    con = connect()
    try:
        con.execute("UPDATE collections SET last_refresh=? WHERE slug=?", (_now(), slug))
        con.commit()
    finally:
        con.close()
    return {"added": added, "readded": readded, "held": held,
            "new_from_zotero": added, "removed_in_zotero": removed_in_zotero}


def download_all(z: ZoteroBackend, slug: str) -> int:
    """Cache every member paper's PDF that isn't cached yet (upgrades a lazy import)."""
    cached = 0
    con = connect()
    try:
        rows = con.execute(
            """SELECT p.id, p.zotero_key, p.arxiv_id FROM collection_papers cp
               JOIN papers p ON p.id = cp.paper_id
               WHERE cp.collection_slug=? AND p.pdf_state='absent'""",
            (slug,),
        ).fetchall()
    finally:
        con.close()
    for r in rows:
        if r["zotero_key"] and pdf_store.copy_into_store(r["id"], z.pdf_path(r["zotero_key"])):
            cached += 1
        elif r["arxiv_id"] and pdf_store.fetch_arxiv_pdf(r["id"], r["arxiv_id"]):
            cached += 1
    return cached
