"""In-memory, cross-request notification feed for completed background jobs.

Global (spans all collections/topics) so the sidebar bell surfaces a job finishing
even after the user has navigated away. Thread-safe (jobs complete on daemon
threads). Transient by design — cleared on restart.
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_ITEMS: list[dict] = []          # oldest first
_SEQ = 0
_MAX = 50


def add(message: str, link: str = "", collection: str = "", ok: bool = True) -> None:
    """Record a completed-job notification. ``link`` jumps the user to the result."""
    global _SEQ
    with _LOCK:
        _SEQ += 1
        _ITEMS.append({"id": _SEQ, "message": message, "link": link,
                       "collection": collection, "ok": ok, "seen": False})
        if len(_ITEMS) > _MAX:
            del _ITEMS[: len(_ITEMS) - _MAX]


def feed(limit: int = 20) -> dict:
    """Recent notifications (newest first) + the unseen count for the dot."""
    with _LOCK:
        unseen = sum(1 for i in _ITEMS if not i["seen"])
        items = [dict(i) for i in reversed(_ITEMS)][:limit]
    return {"unseen": unseen, "items": items}


def mark_seen() -> None:
    with _LOCK:
        for i in _ITEMS:
            i["seen"] = True
