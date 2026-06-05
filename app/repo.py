"""app.sqlite read/write helpers that tie Zotero data to local state.

Phase 1 only needs slug↔collection persistence in ``sync_state`` so that slugs
stay stable and we record which Zotero collection a slug points at.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import library
from .db import connect


def _now() -> str:
    # Microsecond precision so rapid new/switch actions never tie (CURRENT_TIMESTAMP
    # is only second-resolution). ISO strings also sort chronologically as text.
    return datetime.now(timezone.utc).isoformat()


def require_collection(slug: str) -> dict | None:
    """The local collection row for ``slug``, or None. Local-first: collections live
    in the app's own ``collections`` table, not resolved from Zotero per request."""
    return library.get_collection(slug)


# --- chat threads ----------------------------------------------------------
# One collection-wide thread (paper_id IS NULL, context = wiki) + one thread
# per paper (paper_id set, context = that paper).
def get_or_create_thread(slug: str, paper_id: int | None = None) -> int:
    con = connect()
    try:
        # The most-recently-active thread is current, so both a "New chat" and a
        # "switch to older chat" become current.
        order = "ORDER BY COALESCE(last_active_at, created_at) DESC, id DESC LIMIT 1"
        if paper_id:
            row = con.execute(
                f"SELECT id FROM chat_threads WHERE collection_slug=? AND paper_id=? {order}",
                (slug, paper_id),
            ).fetchone()
        else:
            row = con.execute(
                f"SELECT id FROM chat_threads WHERE collection_slug=? AND paper_id IS NULL {order}",
                (slug,),
            ).fetchone()
        if row:
            return row["id"]
        cur = con.execute(
            "INSERT INTO chat_threads (collection_slug, paper_id, last_active_at) "
            "VALUES (?, ?, ?)",
            (slug, paper_id, _now()),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def new_thread(slug: str, paper_id: int | None = None) -> int:
    """Always start a fresh thread (used by 'New chat'); becomes the active one."""
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO chat_threads (collection_slug, paper_id, last_active_at) "
            "VALUES (?, ?, ?)",
            (slug, paper_id, _now()),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def touch_thread(thread_id: int) -> None:
    """Mark a thread active (used when switching to an older conversation)."""
    con = connect()
    try:
        con.execute(
            "UPDATE chat_threads SET last_active_at = ? WHERE id = ?",
            (_now(), thread_id),
        )
        con.commit()
    finally:
        con.close()


def get_session_id(thread_id: int) -> str | None:
    """The CLI agent session id bound to this thread, if any (P8 paper sub-agent)."""
    con = connect()
    try:
        row = con.execute(
            "SELECT agent_session_id FROM chat_threads WHERE id = ?", (thread_id,)
        ).fetchone()
    finally:
        con.close()
    return row["agent_session_id"] if row and row["agent_session_id"] else None


def set_session_id(thread_id: int, session_id: str | None) -> None:
    con = connect()
    try:
        con.execute(
            "UPDATE chat_threads SET agent_session_id = ? WHERE id = ?",
            (session_id, thread_id),
        )
        con.commit()
    finally:
        con.close()


def thread_belongs(thread_id: int, slug: str, paper_id: int | None) -> bool:
    con = connect()
    try:
        if paper_id is None:
            row = con.execute(
                "SELECT 1 FROM chat_threads WHERE id=? AND collection_slug=? AND paper_id IS NULL",
                (thread_id, slug),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT 1 FROM chat_threads WHERE id=? AND collection_slug=? AND paper_id=?",
                (thread_id, slug, paper_id),
            ).fetchone()
        return row is not None
    finally:
        con.close()


def list_threads(slug: str, paper_id: int) -> list[dict]:
    """Conversations for a paper, most-recently-active first, with a label + count."""
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT t.id,
                   (SELECT content FROM chat_messages m WHERE m.thread_id=t.id AND m.role='user'
                      ORDER BY m.id LIMIT 1) AS first_msg,
                   (SELECT COUNT(*) FROM chat_messages m WHERE m.thread_id=t.id) AS n
            FROM chat_threads t
            WHERE t.collection_slug=? AND t.paper_id=?
            ORDER BY COALESCE(t.last_active_at, t.created_at) DESC, t.id DESC
            """,
            (slug, paper_id),
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        label = (r["first_msg"] or "").strip().replace("\n", " ")
        label = (label[:40] + "…") if len(label) > 40 else (label or "New conversation")
        out.append({"id": r["id"], "label": label, "count": r["n"]})
    return out


def delete_thread(thread_id: int) -> None:
    con = connect()
    try:
        con.execute("DELETE FROM chat_messages WHERE thread_id = ?", (thread_id,))
        con.execute("DELETE FROM chat_threads WHERE id = ?", (thread_id,))
        con.commit()
    finally:
        con.close()


def clear_messages(thread_id: int) -> None:
    """Delete a thread's messages but keep the thread row (used by compaction, which
    replaces the history with a single artifact)."""
    con = connect()
    try:
        con.execute("DELETE FROM chat_messages WHERE thread_id = ?", (thread_id,))
        con.commit()
    finally:
        con.close()


def get_artifact(thread_id: int) -> str | None:
    """The compacted-summary 'artifact' for a thread, if one exists. The artifact is
    stored as the thread's ``system`` message; user/assistant turns are the live
    history. Returns the most recent artifact's text or None."""
    con = connect()
    try:
        row = con.execute(
            "SELECT content FROM chat_messages WHERE thread_id=? AND role='system' "
            "ORDER BY id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
    finally:
        con.close()
    return row["content"] if row else None


def thread_message_count(thread_id: int) -> int:
    con = connect()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]
    finally:
        con.close()


def add_message(
    thread_id: int, role: str, content: str, context_refs: list[dict] | None = None,
    images: list[str] | None = None,
) -> int:
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO chat_messages (thread_id, role, content, context_refs, images) "
            "VALUES (?, ?, ?, ?, ?)",
            (thread_id, role, content, json.dumps(context_refs) if context_refs else None,
             json.dumps(images or [])),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def get_message(message_id: int) -> dict | None:
    con = connect()
    try:
        row = con.execute(
            "SELECT id, role, content, context_refs FROM chat_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    refs = json.loads(row["context_refs"]) if row["context_refs"] else []
    return {"id": row["id"], "role": row["role"], "content": row["content"], "refs": refs}


def get_messages(thread_id: int, limit: int = 50) -> list[dict]:
    """Return up to ``limit`` most recent live turns (user/assistant), chronological.

    System messages are excluded: the only stored system message is a compaction
    artifact, which is surfaced separately via ``get_artifact`` and injected into
    context as grounding rather than treated as conversation history.
    """
    con = connect()
    try:
        rows = con.execute(
            "SELECT role, content, images FROM chat_messages WHERE thread_id = ? "
            "AND role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
            (thread_id, limit),
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in reversed(rows):
        try:
            imgs = json.loads(r["images"]) if r["images"] else []
        except (ValueError, TypeError):
            imgs = []
        out.append({"role": r["role"], "content": r["content"], "images": imgs})
    return out
