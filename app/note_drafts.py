"""Staged auto-drafts (Slice 2). When you leave a paper that has chat/highlights but no
note yet, a "Draft from highlights + chat" is computed in the background and held here —
INERT (not a note, doesn't feed attention/beliefs/regen) — until you review and accept it.

Attribution: the draft is the agent's words. It only becomes your note when you accept it
(and you can edit first). Until then it sits in this staging table, surfaced in the paper's
Notes tab and counted in the sidebar "To review" card.
"""

from __future__ import annotations

from .db import connect


def stage(slug: str, paper_id: int, draft_md: str) -> None:
    """Store (or replace) the staged draft for a paper."""
    con = connect()
    try:
        con.execute(
            "INSERT INTO note_drafts (paper_id, collection_slug, draft_md) VALUES (?, ?, ?) "
            "ON CONFLICT(paper_id) DO UPDATE SET draft_md=excluded.draft_md, "
            "created_at=CURRENT_TIMESTAMP",
            (paper_id, slug, draft_md))
        con.commit()
    finally:
        con.close()


def get(slug: str, paper_id: int) -> str | None:
    con = connect()
    try:
        row = con.execute(
            "SELECT draft_md FROM note_drafts WHERE paper_id=? AND collection_slug=?",
            (paper_id, slug)).fetchone()
        return row["draft_md"] if row else None
    finally:
        con.close()


def has(slug: str, paper_id: int) -> bool:
    return get(slug, paper_id) is not None


def delete(slug: str, paper_id: int) -> None:
    con = connect()
    try:
        con.execute("DELETE FROM note_drafts WHERE paper_id=? AND collection_slug=?",
                    (paper_id, slug))
        con.commit()
    finally:
        con.close()


def list_for_collection(slug: str) -> list[dict]:
    con = connect()
    try:
        rows = con.execute(
            "SELECT paper_id, draft_md, created_at FROM note_drafts WHERE collection_slug=? "
            "ORDER BY created_at DESC", (slug,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def count_all() -> int:
    """Total staged drafts across all collections (for the sidebar 'To review' card)."""
    con = connect()
    try:
        return con.execute("SELECT COUNT(*) FROM note_drafts").fetchone()[0]
    finally:
        con.close()
