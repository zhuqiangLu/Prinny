"""Pull from Zotero (pull-only model).

Zotero's *local* API is read-only (writes return 501/400), so the app never writes back.
Import is one-off; thereafter a manual Pull brings in newly-added Zotero papers. Papers the
user previously removed are held back into a re-add picker rather than silently re-added
(see library.pull_preview / library.refresh). Nothing is ever pushed or deleted in Zotero.
"""

from __future__ import annotations

import logging
import threading

from . import library
from .zotero import get_zotero

log = logging.getLogger("paper_agent.sync")

# In-memory per-collection progress for the UI to poll. Single-process app, so a
# module-level dict is enough; each run replaces the previous entry.
_PROGRESS: dict[str, dict] = {}


def pull_preview(slug: str) -> dict:
    """Read-only preview of a Pull: incoming_new (auto-add), held (previously-removed, shown
    in the picker), incoming_gone (members no longer in Zotero). Zotero failures degrade to
    an empty preview with an error note so the panel still renders."""
    try:
        p = library.pull_preview(get_zotero(), slug)
        return {**p, "error": None}
    except Exception as exc:  # noqa: BLE001 - Zotero down / not linked
        return {"incoming_new": [], "held": [], "incoming_gone": [], "error": str(exc)}


# --- progress-tracked background run -------------------------------------------
def get_progress(slug: str) -> dict:
    return _PROGRESS.get(
        slug, {"phase": "idle", "total": 0, "done": 0, "current": "", "result": None, "error": None}
    )


def is_running(slug: str) -> bool:
    return get_progress(slug)["phase"] == "running"


def start(slug: str, *, readd_keys: list[str] | None = None) -> None:
    """Run a Pull in a background thread; progress is polled via get_progress. ``readd_keys``
    are the Zotero keys the user picked to re-add from the held list."""
    if is_running(slug):
        return
    _PROGRESS[slug] = {"phase": "running", "total": 0, "done": 0, "current": "Pulling from Zotero…",
                       "result": None, "error": None}

    def _run() -> None:
        try:
            res = library.refresh(get_zotero(), slug, readd_keys=readd_keys)
            _PROGRESS[slug].update(phase="done", result=res, current="")
        except Exception as exc:  # noqa: BLE001 - surface to the UI
            log.exception("pull failed for %s", slug)
            _PROGRESS[slug].update(phase="error", error=str(exc))

    threading.Thread(target=_run, daemon=True).start()
