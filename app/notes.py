"""Per-paper structured notes (CLAUDE.md Phase 3).

Notes are stored in two synced places:
  - the ``paper_notes`` table (and, via triggers, the FTS index), and
  - a mirror markdown file ``collections/<slug>/notes/<key>.md`` the user can
    edit in Obsidian or any editor.

Last-write-wins with the file's mtime as the tiebreaker: a form save writes both
(so they agree); if the user later edits the markdown file, its mtime exceeds the
DB ``updated_at`` and the file wins on the next read, syncing back into the DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import frontmatter
from .config import COLLECTIONS_DIR
from .db import connect

STATUSES = ("unread", "reading", "noted", "superseded")
# synth_kind 'auto' => resolve by heuristic (reasoning iff the thoughts field is
# non-empty). author_origin is door-stamped; notes are always 'human' in P1.
SYNTH_KINDS = ("auto", "seed", "reasoning")
EMPTY = {
    "summary": "", "thoughts": "", "key_quotes": "", "status": "unread",
    "synth_kind": "auto", "author_origin": "human",
}


def _norm_synth(v: str | None) -> str:
    return v if v in SYNTH_KINDS else "auto"


def _note_path(slug: str, paper_id: int) -> Path:
    return COLLECTIONS_DIR / slug / "notes" / f"{paper_id}.md"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _iso_to_epoch(s: str | None) -> float:
    if not s:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


# --- serialization to/from the markdown body -------------------------------
def _serialize_body(summary: str, thoughts: str, key_quotes: str) -> str:
    return (
        f"## Summary\n\n{summary.strip()}\n\n"
        f"## Thoughts\n\n{thoughts.strip()}\n\n"
        f"## Key Quotes\n\n{key_quotes.strip()}\n"
    )


def _parse_body(body: str) -> dict:
    sections = {"summary": "", "thoughts": "", "key_quotes": ""}
    current = None
    buf: list[str] = []
    header_map = {"summary": "summary", "thoughts": "thoughts", "key quotes": "key_quotes"}
    for line in body.splitlines():
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(buf).strip()
            current = header_map.get(line[3:].strip().lower())
            buf = []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return sections


# --- DB + file IO ----------------------------------------------------------
def _read_db(slug: str, paper_id: int) -> dict | None:
    con = connect()
    try:
        row = con.execute(
            "SELECT summary, thoughts, key_quotes, status, synth_kind, author_origin, updated_at "
            "FROM paper_notes WHERE paper_id = ? AND collection_slug = ?",
            (paper_id, slug),
        ).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def _write_db(slug: str, paper_id: int, data: dict, updated_at: str) -> None:
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO paper_notes
              (paper_id, collection_slug, summary, thoughts, key_quotes, status,
               synth_kind, author_origin, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id) DO UPDATE SET
              collection_slug=excluded.collection_slug,
              summary=excluded.summary, thoughts=excluded.thoughts,
              key_quotes=excluded.key_quotes, status=excluded.status,
              synth_kind=excluded.synth_kind, author_origin=excluded.author_origin,
              updated_at=excluded.updated_at
            """,
            (
                paper_id, slug, data["summary"], data["thoughts"],
                data["key_quotes"], data["status"],
                data["synth_kind"], data["author_origin"], updated_at,
            ),
        )
        con.commit()
    finally:
        con.close()


def _write_file(slug: str, paper_id: int, data: dict, updated_at: str) -> None:
    path = _note_path(slug, paper_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "paper_id": paper_id, "status": data["status"],
        "synth_kind": data["synth_kind"], "author_origin": data["author_origin"],
        "updated_at": updated_at,
    }
    body = _serialize_body(data["summary"], data["thoughts"], data["key_quotes"])
    path.write_text(frontmatter.dump(meta, body), encoding="utf-8")


def _read_file(slug: str, paper_id: int) -> dict | None:
    path = _note_path(slug, paper_id)
    if not path.exists():
        return None
    meta, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    parsed = _parse_body(body)
    status = meta.get("status", "unread")
    if status not in STATUSES:
        status = "unread"
    origin = meta.get("author_origin")
    return {
        **parsed,
        "status": status,
        "synth_kind": _norm_synth(meta.get("synth_kind")),
        "author_origin": origin if origin in ("human", "agent", "external") else "human",
    }


# --- public API ------------------------------------------------------------
def get_note(slug: str, paper_id: int) -> dict:
    """Return the reconciled note (file wins if edited more recently)."""
    db = _read_db(slug, paper_id)
    path = _note_path(slug, paper_id)
    file_mtime = path.stat().st_mtime if path.exists() else 0.0
    db_epoch = _iso_to_epoch(db["updated_at"]) if db else 0.0

    if path.exists() and file_mtime > db_epoch + 1:
        # File edited out-of-band → file wins; sync back into the DB.
        data = _read_file(slug, paper_id)
        if data:
            _write_db(slug, paper_id, data, _now_iso())
            return data
    if db:
        text_keys = ("summary", "thoughts", "key_quotes")
        out = {k: (db[k] or "") for k in text_keys}
        out["status"] = db["status"]
        out["synth_kind"] = _norm_synth(db["synth_kind"])
        out["author_origin"] = db["author_origin"] or "human"
        return out
    return dict(EMPTY)


def save_note(
    slug: str, paper_id: int, summary: str, thoughts: str, key_quotes: str, status: str,
    synth_kind: str = "auto", author_origin: str = "human",
) -> dict:
    if status not in STATUSES:
        status = "unread"
    data = {
        "summary": summary or "",
        "thoughts": thoughts or "",
        "key_quotes": key_quotes or "",
        "status": status,
        "synth_kind": _norm_synth(synth_kind),
        "author_origin": author_origin if author_origin in ("human", "agent", "external") else "human",
    }
    updated_at = _now_iso()
    _write_db(slug, paper_id, data, updated_at)
    _write_file(slug, paper_id, data, updated_at)
    return data


def note_kind(slug: str, paper_id: int) -> tuple[str, str]:
    """Resolve a note's effective (synth_kind, author_origin) for the gate.

    synth_kind 'auto' resolves by heuristic: 'reasoning' iff the note's thoughts field
    is non-empty (that field is the user's take/criticisms/connections), else 'seed'.
    An explicit override wins. # SPAN-TODO: span-level reasoning marks within a note.
    """
    note = get_note(slug, paper_id)
    override = _norm_synth(note.get("synth_kind"))
    if override in ("seed", "reasoning"):
        kind = override
    else:
        kind = "reasoning" if (note.get("thoughts") or "").strip() else "seed"
    origin = note.get("author_origin") or "human"
    return kind, origin
