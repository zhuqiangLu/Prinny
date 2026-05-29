"""Uniform local PDF store (ADR 0001).

Every paper — regardless of origin — resolves to ``<store>/<paper-id>.pdf``. There is
no origin branching in resolution. The store directory is configurable
(``pdf_store_path``) and may be a network drive, so every function degrades
gracefully (returns False/None + logs) when the store is unavailable; we never raise
and never ``mkdir`` a NAS root at startup.

PDFs land here two ways:
  - imported papers: copied from Zotero's storage (the PDF of record);
  - arxiv-suggested papers: downloaded from arxiv.org.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import httpx

from .config import load_config
from .db import connect

log = logging.getLogger("paper_agent.pdf_store")


def store_dir() -> Path:
    return Path(load_config()["pdf_store_path"])


def store_available() -> bool:
    """True if the store directory exists (and so is a writable target). A missing
    directory is treated as "unavailable" — e.g. a disconnected network drive."""
    return store_dir().is_dir()


# In-flight background downloads (single-process app, so an in-memory dict is enough). Powers
# the row's progress ring + poll. pid -> {received, total|None, state: fetching|failed}.
_DOWNLOADS: dict[int, dict] = {}


def is_fetching(paper_id: int) -> bool:
    d = _DOWNLOADS.get(paper_id)
    return bool(d and d["state"] == "fetching")


def download_failed(paper_id: int) -> bool:
    d = _DOWNLOADS.get(paper_id)
    return bool(d and d["state"] == "failed")


def download_percent(paper_id: int) -> int | None:
    """0-100 if the size is known, else None (indeterminate)."""
    d = _DOWNLOADS.get(paper_id)
    if not d or not d.get("total"):
        return None
    return min(100, round(d["received"] / d["total"] * 100))


def _paper_pdf_url(paper_id: int) -> str | None:
    """The external PDF URL for a paper, from its arXiv or OpenReview id."""
    con = connect()
    try:
        row = con.execute(
            "SELECT arxiv_id, openreview_id FROM papers WHERE id=?", (paper_id,)
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    if row["arxiv_id"]:
        aid = row["arxiv_id"].strip().removeprefix("arXiv:").removeprefix("arxiv:")
        return f"https://arxiv.org/pdf/{aid}.pdf"
    if row["openreview_id"]:
        return f"https://openreview.net/pdf?id={row['openreview_id']}"
    return None


def start_download(paper_id: int) -> bool:
    """Stream the paper's PDF into the store in a background thread, tracking byte progress so
    the UI can show a ring. Returns False if there's no source URL or one is already running."""
    import threading

    if is_fetching(paper_id):
        return False
    url = _paper_pdf_url(paper_id)
    if not url or not _ensure_store():
        return False
    _DOWNLOADS[paper_id] = {"received": 0, "total": None, "state": "fetching"}

    def _run() -> None:
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                with client.stream("GET", url) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length") or 0) or None
                    _DOWNLOADS[paper_id]["total"] = total
                    tmp = pdf_dest(paper_id).with_suffix(".part")
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_bytes(64 * 1024):
                            fh.write(chunk)
                            _DOWNLOADS[paper_id]["received"] += len(chunk)
                    tmp.replace(pdf_dest(paper_id))
            _mark_cached(paper_id)
            _DOWNLOADS.pop(paper_id, None)          # done -> has_pdf takes over
        except (httpx.HTTPError, OSError) as exc:    # noqa: BLE001
            log.warning("PDF download failed for paper %s: %s", paper_id, exc)
            _DOWNLOADS[paper_id] = {"received": 0, "total": None, "state": "failed"}

    threading.Thread(target=_run, daemon=True).start()
    return True


def clear_download(paper_id: int) -> None:
    """Drop any tracked download state (e.g. when a failed import is removed)."""
    _DOWNLOADS.pop(paper_id, None)


def pdf_dest(paper_id: int) -> Path:
    return store_dir() / f"{paper_id}.pdf"


def has_pdf(paper_id: int) -> bool:
    return store_available() and pdf_dest(paper_id).exists()


def _mark_cached(paper_id: int) -> None:
    con = connect()
    try:
        con.execute(
            "UPDATE papers SET pdf_state='cached', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (paper_id,),
        )
        con.commit()
    finally:
        con.close()


def _mark_absent(paper_id: int) -> None:
    con = connect()
    try:
        con.execute(
            "UPDATE papers SET pdf_state='absent', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (paper_id,),
        )
        con.commit()
    finally:
        con.close()


def remove_pdf(paper_id: int) -> bool:
    """Delete a paper's cached PDF file and mark it absent. The DB row is always marked
    absent (even if the store is offline or the file was already gone) so the paper's
    state reflects "no local copy"; returns whether a file was actually unlinked."""
    removed = False
    if store_available():
        dest = pdf_dest(paper_id)
        try:
            if dest.exists():
                dest.unlink()
                removed = True
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("Failed to remove PDF for paper %s: %s", paper_id, exc)
    _mark_absent(paper_id)
    return removed


def _ensure_store() -> bool:
    """Create the store dir if its parent exists (so we don't fabricate a NAS root).
    Returns whether the store is usable afterwards."""
    d = store_dir()
    if d.is_dir():
        return True
    try:
        # Only create when the parent already exists — avoids materialising a whole
        # absent network-mount path.
        if d.parent.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            return True
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("Could not create PDF store %s: %s", d, exc)
    return d.is_dir()


def copy_into_store(paper_id: int, src: Path | None) -> bool:
    """Copy a source PDF into the store as ``<paper-id>.pdf``. No-op (False) when the
    store is unavailable or the source is missing."""
    if src is None or not Path(src).exists():
        return False
    if not _ensure_store():
        log.warning("PDF store unavailable; skipped caching paper %s", paper_id)
        return False
    try:
        shutil.copy2(src, pdf_dest(paper_id))
    except OSError as exc:
        log.warning("Failed to copy PDF for paper %s: %s", paper_id, exc)
        return False
    _mark_cached(paper_id)
    return True


def fetch_arxiv_pdf(paper_id: int, arxiv_id: str) -> bool:
    """Download an arXiv PDF into the store. External fetch — uses env proxy."""
    if not arxiv_id:
        return False
    if not _ensure_store():
        log.warning("PDF store unavailable; skipped arXiv fetch for paper %s", paper_id)
        return False
    # Normalise: strip any version-less/abs prefixes; arxiv.org/pdf/<id> works for both.
    aid = arxiv_id.strip().removeprefix("arXiv:").removeprefix("arxiv:")
    url = f"https://arxiv.org/pdf/{aid}.pdf"
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            pdf_dest(paper_id).write_bytes(r.content)
    except (httpx.HTTPError, OSError) as exc:
        log.warning("arXiv PDF fetch failed for %s (paper %s): %s", aid, paper_id, exc)
        return False
    _mark_cached(paper_id)
    return True


def ensure_cached(paper_id: int) -> Path | None:
    """Lazy copy-on-demand. If the PDF isn't in the store yet, pull it from the
    paper's source of record (Zotero storage via zotero_key, else arXiv). Returns the
    destination path or None."""
    if not store_available():
        return None
    dest = pdf_dest(paper_id)
    if dest.exists():
        return dest
    # Look up the paper's natural keys.
    con = connect()
    try:
        row = con.execute(
            "SELECT zotero_key, arxiv_id FROM papers WHERE id=?", (paper_id,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    zotero_key, arxiv_id = row["zotero_key"], row["arxiv_id"]
    if zotero_key:
        from .zotero import get_zotero

        src = get_zotero().pdf_path(zotero_key)
        if copy_into_store(paper_id, src):
            return dest
    if arxiv_id and fetch_arxiv_pdf(paper_id, arxiv_id):
        return dest
    return None
