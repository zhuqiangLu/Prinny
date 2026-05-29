"""App-authored PDF annotations (Addendum Capability 1).

Annotation authority = the app (Option A): we author highlights/notes into our
own store and read Zotero's annotations out one-way for display. We do NOT write
back into Zotero in v1.

position is stored as JSON ``{"pageIndex": n, "rects": [[x1,y1,x2,y2], ...]}`` in
PDF point space (bottom-left origin), matching Zotero's shape so a future
write-back path can be added without changing this data model.

# WRITEBACK-TODO: a Zotero annotation writer would consume these same rows
# (origin='app') and POST them to Zotero. Keep position_json in PDF-point space
# so no coordinate conversion beyond it is needed here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .db import connect
from .zotero import get_zotero


def _nearest(color: str, palette: list[str]) -> str:
    """The palette color closest to ``color`` by RGB distance."""
    def rgb(h):
        h = (h or "").lstrip("#")
        if len(h) != 6:
            return (0, 0, 0)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    cr = rgb(color)
    return min(palette, key=lambda p: sum((a - b) ** 2 for a, b in zip(cr, rgb(p)))) if palette else color


def remap_to_scheme(colors: list[str]) -> int:
    """Recolor every app highlight to the nearest color in ``colors`` (used when the
    highlight scheme changes). Returns how many annotations were recolored."""
    if not colors:
        return 0
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, color FROM annotations WHERE origin='app' AND kind='highlight'"
        ).fetchall()
        changed = 0
        for r in rows:
            new = _nearest(r["color"], colors)
            if new and new != r["color"]:
                con.execute("UPDATE annotations SET color=?, updated_at=? WHERE id=?",
                            (new, _now(), r["id"]))
                changed += 1
        con.commit()
    finally:
        con.close()
    return changed


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def create(
    slug: str,
    paper_id: int,
    *,
    kind: str,
    color: str | None,
    page: int,
    position_json: str,
    selected_text: str = "",
    note_text: str = "",
) -> dict:
    con = connect()
    try:
        cur = con.execute(
            """
            INSERT INTO annotations
              (paper_id, collection_slug, origin, kind, color, page,
               position_json, selected_text, note_text)
            VALUES (?, ?, 'app', ?, ?, ?, ?, ?, ?)
            """,
            (paper_id, slug, kind, color, page, position_json, selected_text, note_text),
        )
        con.commit()
        return _row(con, cur.lastrowid)
    finally:
        con.close()


def _row(con, ann_id: int) -> dict | None:
    r = con.execute("SELECT * FROM annotations WHERE id = ?", (ann_id,)).fetchone()
    return dict(r) if r else None


def get(ann_id: int) -> dict | None:
    con = connect()
    try:
        return _row(con, ann_id)
    finally:
        con.close()


def list_app(paper_id: int, slug: str | None = None) -> list[dict]:
    """App highlights for a paper. Highlights are per-collection (annotations carry a
    collection_slug): pass ``slug`` to scope them to one collection — otherwise the same
    paper shared by two collections would show each other's highlights."""
    con = connect()
    try:
        if slug is None:
            rows = con.execute(
                "SELECT * FROM annotations WHERE paper_id = ? AND origin = 'app' ORDER BY page, id",
                (paper_id,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM annotations WHERE paper_id = ? AND collection_slug = ? "
                "AND origin = 'app' ORDER BY page, id",
                (paper_id, slug),
            ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def list_all(paper_id: int, slug: str | None = None) -> list[dict]:
    """App annotations (scoped to ``slug``) + Zotero read-in (Zotero-origin, read-only).

    The Zotero read-in is keyed by the paper's Zotero item key; papers minted in
    the app (arxiv-suggested / app-created) have none, so we skip it for them."""
    out = list_app(paper_id, slug)
    from . import library

    paper = library.get_paper(paper_id)
    zotero_key = paper["zotero_key"] if paper else None
    if zotero_key:
        try:
            out.extend(get_zotero().read_annotations(zotero_key))
        except Exception:  # noqa: BLE001 - read-in is best-effort
            pass
    return out


def update(ann_id: int, *, color: str | None = None, note_text: str | None = None) -> dict | None:
    con = connect()
    try:
        row = _row(con, ann_id)
        if not row or row["origin"] != "app":
            return None
        con.execute(
            "UPDATE annotations SET color = COALESCE(?, color), "
            "note_text = COALESCE(?, note_text), updated_at = ? WHERE id = ?",
            (color, note_text, _now(), ann_id),
        )
        con.commit()
        return _row(con, ann_id)
    finally:
        con.close()


def delete(ann_id: int) -> bool:
    """Delete an app-origin annotation. Zotero-origin annotations are read-only."""
    con = connect()
    try:
        row = _row(con, ann_id)
        if not row or row["origin"] != "app":
            return False
        con.execute("DELETE FROM annotations WHERE id = ?", (ann_id,))
        con.commit()
        return True
    finally:
        con.close()


def to_client(ann: dict) -> dict:
    """Shape a row for the JSON the viewer consumes."""
    pos = ann.get("position_json")
    try:
        position = json.loads(pos) if isinstance(pos, str) else pos
    except (json.JSONDecodeError, TypeError):
        position = {"pageIndex": ann.get("page", 0), "rects": []}
    return {
        "id": ann.get("id"),
        "origin": ann.get("origin", "app"),
        "kind": ann.get("kind", "highlight"),
        "color": ann.get("color"),
        "page": ann.get("page", position.get("pageIndex", 0)),
        "position": position,
        "selected_text": ann.get("selected_text") or "",
        "note_text": ann.get("note_text") or "",
    }
