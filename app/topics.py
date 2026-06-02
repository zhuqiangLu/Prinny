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

_STATUSES = ("exploring", "active", "answered", "parked")   # legacy `status` column
# v2 lifecycle (the `lifecycle` column; no DB CHECK so it stays flexible).
_LIFECYCLE = ("exploration", "investigation", "active", "archived")
_LIFECYCLE_LABEL = {"exploration": "Exploration", "investigation": "Investigation",
                    "active": "Active Project", "archived": "Archived"}


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


def collection_usage() -> dict:
    """``{collection_slug: n_topics}`` — how many research topics reference each
    collection. Drives the sidebar delete-collection warning."""
    con = connect()
    try:
        return {r["collection_slug"]: r["n"] for r in con.execute(
            "SELECT collection_slug, COUNT(*) AS n FROM topic_collections GROUP BY collection_slug")}
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


def _stat_counts(con, tid: int) -> dict:
    """Header/stat counts for a topic (evidence papers, hypotheses, unknowns, …)."""
    one = lambda sql: con.execute(sql, (tid,)).fetchone()[0]
    return {
        "n_collections": one("SELECT COUNT(*) FROM topic_collections WHERE topic_id=?"),
        "n_evidence": one("SELECT COUNT(*) FROM topic_evidence WHERE topic_id=? AND kind!='missing'"),
        "n_hypotheses": one("SELECT COUNT(*) FROM topic_hypotheses WHERE topic_id=?"),
        "n_unknowns": one("SELECT COUNT(*) FROM topic_unknowns WHERE topic_id=?"),
        "n_experiments": one("SELECT COUNT(*) FROM topic_experiments WHERE topic_id=?"),
    }


def get_topic(slug: str) -> dict | None:
    """Full topic: record + collections + the v2 inquiry lists (assumptions,
    hypotheses, evidence, unknowns, experiments, notes, timeline). None if absent."""
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
        t["assumptions"] = [{"id": row["id"], "text": row["text"]} for row in con.execute(
            "SELECT id, text FROM topic_assumptions WHERE topic_id=? ORDER BY position, id",
            (tid,)).fetchall()]
        t["hypotheses"] = [
            {"id": row["id"], "text": row["text"], "status": row["status"],
             "support_count": row["support_count"], "counter_count": row["counter_count"]}
            for row in con.execute(
            "SELECT id, text, status, support_count, counter_count FROM topic_hypotheses "
            "WHERE topic_id=? ORDER BY position, id", (tid,)).fetchall()]
        # Evidence joined to its (collection) paper title where grounded.
        t["evidence"] = [
            {"id": row["id"], "kind": row["kind"], "claim": row["claim"],
             "paper_id": row["paper_id"], "paper_ref": row["paper_ref"],
             "collection": row["collection_slug"], "hypothesis_id": row["hypothesis_id"],
             "paper_title": row["paper_title"]}
            for row in con.execute(
            "SELECT e.*, p.title AS paper_title FROM topic_evidence e "
            "LEFT JOIN papers p ON p.id = e.paper_id "
            "WHERE e.topic_id=? ORDER BY e.kind, e.position, e.id", (tid,)).fetchall()]
        t["unknowns"] = [
            {"id": row["id"], "text": row["text"], "priority": row["priority"],
             "status": row["status"], "hypothesis_id": row["hypothesis_id"]}
            for row in con.execute(
            "SELECT id, text, priority, status, hypothesis_id FROM topic_unknowns "
            "WHERE topic_id=? ORDER BY position, id", (tid,)).fetchall()]
        t["experiments"] = [
            {"id": row["id"], "title": row["title"], "method": row["method"],
             "metric": row["metric"], "status": row["status"], "hypothesis_id": row["hypothesis_id"]}
            for row in con.execute(
            "SELECT id, title, method, metric, status, hypothesis_id FROM topic_experiments "
            "WHERE topic_id=? ORDER BY position, id", (tid,)).fetchall()]
        t["notes"] = [{"id": row["id"], "body": row["body"], "created_at": row["created_at"]}
                      for row in con.execute(
            "SELECT id, body, created_at FROM topic_notes WHERE topic_id=? "
            "ORDER BY created_at DESC, id DESC", (tid,)).fetchall()]
        t["timeline"] = [{"event": row["event"], "detail": row["detail"], "created_at": row["created_at"]}
                         for row in con.execute(
            "SELECT event, detail, created_at FROM topic_timeline WHERE topic_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT 50", (tid,)).fetchall()]
        # Legacy open-questions kept for the chat-context (not surfaced in v2 UI).
        t["questions"] = [{"id": row["id"], "text": row["text"], "source": row["source"]}
                          for row in con.execute(
            "SELECT id, text, source FROM topic_questions WHERE topic_id=? ORDER BY id",
            (tid,)).fetchall()]
        return t
    finally:
        con.close()


def _row_to_topic(r) -> dict:
    keys = r.keys()
    try:
        seed = json.loads(r["seed"]) if r["seed"] else {}
    except (ValueError, TypeError):
        seed = {}
    try:
        generated = json.loads(r["generated"]) if ("generated" in keys and r["generated"]) else {}
    except (ValueError, TypeError):
        generated = {}
    lifecycle = r["lifecycle"] if "lifecycle" in keys else "investigation"
    return {"id": r["id"], "slug": r["slug"], "title": r["title"], "question": r["question"],
            "description": r["description"], "status": r["status"], "seed": seed,
            "lifecycle": lifecycle, "lifecycle_label": _LIFECYCLE_LABEL.get(lifecycle, "Investigation"),
            "generated": generated,
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "n_collections": r["n_collections"] if "n_collections" in keys else None}


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


def update_basics(slug: str, *, title: str | None = None, question: str | None = None,
                  description: str | None = None) -> bool:
    """Edit the topic's question/title/description (Section 1 'Edit'). A blank
    question is rejected (the question is the spine)."""
    sets, args = [], []
    if title is not None and title.strip():
        sets.append("title=?"); args.append(title.strip())
    if question is not None:
        if not question.strip():
            return False
        sets.append("question=?"); args.append(question.strip())
    if description is not None:
        sets.append("description=?"); args.append(description.strip())
    if not sets:
        return False
    con = connect()
    try:
        args.append(slug)
        cur = con.execute(f"UPDATE research_topics SET {', '.join(sets)}, "
                          f"updated_at=CURRENT_TIMESTAMP WHERE slug=?", args)
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


# --- v2 lifecycle + timeline -------------------------------------------------

def set_lifecycle(slug: str, lifecycle: str) -> bool:
    if lifecycle not in _LIFECYCLE:
        return False
    con = connect()
    try:
        cur = con.execute("UPDATE research_topics SET lifecycle=?, updated_at=CURRENT_TIMESTAMP "
                          "WHERE slug=?", (lifecycle, slug))
        con.commit()
        if cur.rowcount:
            log_event(slug, "status_changed", _LIFECYCLE_LABEL.get(lifecycle, lifecycle))
        return cur.rowcount > 0
    finally:
        con.close()


def log_event(slug: str, event: str, detail: str = "") -> None:
    """Append a timeline event (created / generated / hypothesis added / …)."""
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None:
            return
        con.execute("INSERT INTO topic_timeline(topic_id, event, detail) VALUES(?,?,?)",
                    (tid, event, (detail or "")[:300]))
        con.commit()
    finally:
        con.close()


# --- v2 inquiry-list CRUD (assumptions / unknowns / experiments / notes) -----

def add_assumption(slug: str, text: str) -> bool:
    return _add_positioned(slug, "topic_assumptions", {"text": (text or "").strip()})


def delete_assumption(slug: str, aid: int) -> bool:
    return _delete_row(slug, "topic_assumptions", aid)


def add_unknown(slug: str, text: str, priority: str = "medium") -> bool:
    if priority not in ("high", "medium", "low"):
        priority = "medium"
    return _add_positioned(slug, "topic_unknowns",
                           {"text": (text or "").strip(), "priority": priority})


def delete_unknown(slug: str, uid: int) -> bool:
    return _delete_row(slug, "topic_unknowns", uid)


def set_unknown(slug: str, uid: int, *, status: str | None = None,
                priority: str | None = None) -> bool:
    sets, args = [], []
    if status in ("open", "investigating", "resolved"):
        sets.append("status=?"); args.append(status)
    if priority in ("high", "medium", "low"):
        sets.append("priority=?"); args.append(priority)
    if not sets:
        return False
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None:
            return False
        args += [uid, tid]
        cur = con.execute(f"UPDATE topic_unknowns SET {', '.join(sets)} WHERE id=? AND topic_id=?", args)
        _touch(con, slug)
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def add_experiment(slug: str, title: str, method: str = "", metric: str = "",
                   status: str = "planned") -> bool:
    title = (title or "").strip()
    if not title:
        return False
    if status not in ("planned", "running", "done"):
        status = "planned"
    return _add_positioned(slug, "topic_experiments",
                           {"title": title, "method": (method or "").strip(),
                            "metric": (metric or "").strip(), "status": status})


def delete_experiment(slug: str, eid: int) -> bool:
    return _delete_row(slug, "topic_experiments", eid)


def add_note(slug: str, body: str) -> bool:
    body = (body or "").strip()
    if not body:
        return False
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None:
            return False
        con.execute("INSERT INTO topic_notes(topic_id, body) VALUES(?,?)", (tid, body))
        _touch(con, slug)
        con.commit()
        return True
    finally:
        con.close()


def delete_note(slug: str, nid: int) -> bool:
    return _delete_row(slug, "topic_notes", nid)


def delete_evidence(slug: str, eid: int) -> bool:
    return _delete_row(slug, "topic_evidence", eid)


def _add_positioned(slug: str, table: str, fields: dict) -> bool:
    """Insert a row with an auto-incremented position. ``fields`` must include a
    non-empty 'text' or 'title'. Generic over the simple list tables."""
    key = "text" if "text" in fields else "title"
    if not (fields.get(key) or "").strip():
        return False
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None:
            return False
        pos = con.execute(f"SELECT COALESCE(MAX(position),0)+1 FROM {table} WHERE topic_id=?",
                          (tid,)).fetchone()[0]
        cols = ["topic_id"] + list(fields) + ["position"]
        vals = [tid] + list(fields.values()) + [pos]
        con.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        _touch(con, slug)
        con.commit()
        return True
    finally:
        con.close()


def _delete_row(slug: str, table: str, rid: int) -> bool:
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None:
            return False
        cur = con.execute(f"DELETE FROM {table} WHERE id=? AND topic_id=?", (rid, tid))
        _touch(con, slug)
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def replace_investigation(slug: str, *, assumptions: list, hypotheses: list,
                          evidence: list, unknowns: list, experiments: list,
                          generated: dict) -> bool:
    """Atomically replace ALL agent-generated investigation content for a topic
    (called by topic_view.generate_investigation after validation). Manual user
    additions made before a regenerate are replaced — generation is an explicit,
    user-triggered action.

    ``hypotheses`` rows: {text, status, support_count, counter_count}.
    ``evidence``/``unknowns``/``experiments`` reference a hypothesis by ``hyp_index``
    (0-based into ``hypotheses``); resolved to the new row id here.
    ``generated`` (next_steps, key_terms, confidence) is stored as JSON on the topic."""
    con = connect()
    try:
        tid = _topic_id(con, slug)
        if tid is None:
            return False
        for table in ("topic_assumptions", "topic_evidence", "topic_unknowns",
                      "topic_experiments", "topic_hypotheses"):
            con.execute(f"DELETE FROM {table} WHERE topic_id=?", (tid,))

        for i, a in enumerate(assumptions):
            con.execute("INSERT INTO topic_assumptions(topic_id, text, position) VALUES(?,?,?)",
                        (tid, a, i))

        hyp_ids: list[int] = []
        for i, h in enumerate(hypotheses):
            cur = con.execute(
                "INSERT INTO topic_hypotheses(topic_id, text, status, support_count, "
                "counter_count, position) VALUES(?,?,?,?,?,?)",
                (tid, h["text"], h.get("status", "unknown"),
                 int(h.get("support_count", 0)), int(h.get("counter_count", 0)), i))
            hyp_ids.append(cur.lastrowid)

        def hyp_id(idx):
            return hyp_ids[idx] if isinstance(idx, int) and 0 <= idx < len(hyp_ids) else None

        for i, e in enumerate(evidence):
            con.execute(
                "INSERT INTO topic_evidence(topic_id, kind, claim, paper_ref, paper_id, "
                "collection_slug, hypothesis_id, position) VALUES(?,?,?,?,?,?,?,?)",
                (tid, e.get("kind", "supporting"), e["claim"], e.get("paper_ref"),
                 e.get("paper_id"), e.get("collection"), hyp_id(e.get("hyp_index")), i))

        for i, u in enumerate(unknowns):
            con.execute(
                "INSERT INTO topic_unknowns(topic_id, text, priority, hypothesis_id, position) "
                "VALUES(?,?,?,?,?)",
                (tid, u["text"], u.get("priority", "medium"), hyp_id(u.get("hyp_index")), i))

        for i, x in enumerate(experiments):
            con.execute(
                "INSERT INTO topic_experiments(topic_id, title, method, metric, status, "
                "hypothesis_id, position) VALUES(?,?,?,?,?,?,?)",
                (tid, x["title"], x.get("method", ""), x.get("metric", ""),
                 x.get("status", "planned"), hyp_id(x.get("hyp_index")), i))

        con.execute("UPDATE research_topics SET generated=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(generated), tid))
        con.commit()
        return True
    finally:
        con.close()
