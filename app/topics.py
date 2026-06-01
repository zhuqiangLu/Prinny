"""Research Topics (RESEARCH_TOPICS v1) — data layer.

A Research Topic is an *investigation thread*: a required question, optional
hypotheses/open-questions, and references to one or more collections (its
evidence sources). A topic NEVER owns papers, notes, or wiki pages — it only
points at existing knowledge. Collection = what the field says; Topic = what
I'm investigating.

This module is pure CRUD over app.sqlite. The intelligence layers (relevant-
entity ranking, suggested reading, the topic graph) live in topic_view.py and
build on top of these records + the per-collection graph.
"""
from __future__ import annotations

import json
import re

from .db import connect

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_STATUSES = ("exploring", "active", "answered", "parked")


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").lower()).strip("-")[:60] or "topic"


def _unique_slug(con, base: str) -> str:
    slug, n = base, 1
    while con.execute("SELECT 1 FROM research_topics WHERE slug=?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    return slug


def create_topic(title: str, question: str, collections: list[str] | None = None,
                 description: str = "") -> str:
    """Create a topic. ``question`` is REQUIRED — a topic without a question is a
    collection in disguise. Returns the new slug. Raises ValueError on no question."""
    title = (title or "").strip()
    question = (question or "").strip()
    if not question:
        raise ValueError("A research topic requires a question.")
    if not title:
        title = question[:60]
    con = connect()
    try:
        slug = _unique_slug(con, _slugify(title))
        cur = con.execute(
            "INSERT INTO research_topics(slug, title, question, description) "
            "VALUES(?,?,?,?)", (slug, title, question, (description or "").strip()))
        tid = cur.lastrowid
        for cs in dict.fromkeys(collections or []):   # de-dupe, preserve order
            con.execute("INSERT OR IGNORE INTO topic_collections(topic_id, collection_slug) "
                        "VALUES(?,?)", (tid, cs))
        con.commit()
        return slug
    finally:
        con.close()


def list_topics() -> list[dict]:
    """All topics, newest-updated first, with their linked-collection count."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM topic_collections tc WHERE tc.topic_id=t.id) "
            "AS n_collections FROM research_topics t ORDER BY t.updated_at DESC").fetchall()
        return [_row_to_topic(r) for r in rows]
    finally:
        con.close()


def get_topic(slug: str) -> dict | None:
    """Full topic: record + collections + hypotheses + questions. None if absent."""
    con = connect()
    try:
        r = con.execute("SELECT * FROM research_topics WHERE slug=?", (slug,)).fetchone()
        if not r:
            return None
        t = _row_to_topic(r)
        tid = r["id"]
        t["collections"] = [row["collection_slug"] for row in con.execute(
            "SELECT collection_slug FROM topic_collections WHERE topic_id=? "
            "ORDER BY collection_slug", (tid,)).fetchall()]
        t["hypotheses"] = [{"id": row["id"], "text": row["text"]} for row in con.execute(
            "SELECT id, text FROM topic_hypotheses WHERE topic_id=? "
            "ORDER BY position, id", (tid,)).fetchall()]
        t["questions"] = [{"id": row["id"], "text": row["text"], "source": row["source"]}
                          for row in con.execute(
            "SELECT id, text, source FROM topic_questions WHERE topic_id=? "
            "ORDER BY id", (tid,)).fetchall()]
        return t
    finally:
        con.close()


def _row_to_topic(r) -> dict:
    try:
        seed = json.loads(r["seed"]) if r["seed"] else {}
    except (ValueError, TypeError):
        seed = {}
    return {"id": r["id"], "slug": r["slug"], "title": r["title"], "question": r["question"],
            "description": r["description"], "status": r["status"], "seed": seed,
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "n_collections": r["n_collections"] if "n_collections" in r.keys() else None}


def _touch(con, slug: str) -> None:
    con.execute("UPDATE research_topics SET updated_at=CURRENT_TIMESTAMP WHERE slug=?", (slug,))


def _topic_id(con, slug: str) -> int | None:
    r = con.execute("SELECT id FROM research_topics WHERE slug=?", (slug,)).fetchone()
    return r["id"] if r else None


def delete_topic(slug: str) -> bool:
    con = connect()
    try:
        cur = con.execute("DELETE FROM research_topics WHERE slug=?", (slug,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def set_status(slug: str, status: str) -> bool:
    if status not in _STATUSES:
        return False
    con = connect()
    try:
        cur = con.execute("UPDATE research_topics SET status=?, updated_at=CURRENT_TIMESTAMP "
                          "WHERE slug=?", (status, slug))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def set_collections(slug: str, collections: list[str]) -> bool:
    """Replace the topic's linked collections."""
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None:
            return False
        con.execute("DELETE FROM topic_collections WHERE topic_id=?", (tid,))
        for cs in dict.fromkeys(collections or []):
            con.execute("INSERT OR IGNORE INTO topic_collections(topic_id, collection_slug) "
                        "VALUES(?,?)", (tid, cs))
        _touch(con, slug)
        con.commit()
        return True
    finally:
        con.close()


def add_hypothesis(slug: str, text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None:
            return False
        pos = (con.execute("SELECT COALESCE(MAX(position),0)+1 FROM topic_hypotheses "
                           "WHERE topic_id=?", (tid,)).fetchone()[0])
        con.execute("INSERT INTO topic_hypotheses(topic_id, text, position) VALUES(?,?,?)",
                    (tid, text, pos))
        _touch(con, slug)
        con.commit()
        return True
    finally:
        con.close()


def edit_hypothesis(slug: str, hid: int, text: str) -> bool:
    text = (text or "").strip()
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None or not text:
            return False
        cur = con.execute("UPDATE topic_hypotheses SET text=? WHERE id=? AND topic_id=?",
                          (text, hid, tid))
        _touch(con, slug)
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def delete_hypothesis(slug: str, hid: int) -> bool:
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None:
            return False
        cur = con.execute("DELETE FROM topic_hypotheses WHERE id=? AND topic_id=?", (hid, tid))
        _touch(con, slug)
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def add_question(slug: str, text: str, source: str = "user") -> bool:
    text = (text or "").strip()
    if not text or source not in ("user", "agent"):
        return False
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None:
            return False
        con.execute("INSERT INTO topic_questions(topic_id, text, source) VALUES(?,?,?)",
                    (tid, text, source))
        _touch(con, slug)
        con.commit()
        return True
    finally:
        con.close()


def delete_question(slug: str, qid: int) -> bool:
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None:
            return False
        cur = con.execute("DELETE FROM topic_questions WHERE id=? AND topic_id=?", (qid, tid))
        _touch(con, slug)
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def save_seed(slug: str, seed: dict) -> None:
    con = connect()
    try:
        con.execute("UPDATE research_topics SET seed=? WHERE slug=?",
                    (json.dumps(seed), slug))
        con.commit()
    finally:
        con.close()
