"""Triage queue for candidate papers (CLAUDE.md Phase 7).

A collection's inbox is configured in ``purpose.md`` frontmatter
(``inbox_collection`` or ``inbox_tag``). Scanning the inbox is cheap (no LLM):
it just finds papers not yet in the main collection or the queue. The relevance
pitch is generated on demand (an LLM cost the user triggers), per the project's
"don't auto-trigger expensive operations" rule.

Local-first (ADR 0001): accepting a candidate imports it into the app's own store
and adds it to the collection (local). It reaches Zotero only via an explicit "Sync
to Zotero" — accept/reject never write to Zotero here.
"""

from __future__ import annotations

import logging

from . import frontmatter, library, llm, pdf_store
from .config import COLLECTIONS_DIR
from .db import connect
from .wiki import _read, _wikidir
from .zotero import ZoteroBackend, get_zotero

logger = logging.getLogger("paper_agent.triage")


def _inbox_config(slug: str) -> dict:
    meta, _ = frontmatter.parse(_read(COLLECTIONS_DIR / slug / "purpose.md"))
    return {
        "inbox_collection": meta.get("inbox_collection"),
        "inbox_tag": meta.get("inbox_tag"),
    }


def _existing_keys(slug: str) -> set[str]:
    con = connect()
    try:
        rows = con.execute(
            "SELECT zotero_key FROM triage_items WHERE collection_slug = ? "
            "AND zotero_key IS NOT NULL",
            (slug,),
        ).fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


def scan_inbox(z: ZoteroBackend, slug: str) -> int:
    """Find new inbox candidates and enqueue them (pending). Returns count added."""
    cfg = _inbox_config(slug)
    candidates = []
    if cfg["inbox_collection"]:
        candidates = z.list_papers_by_collection_name(cfg["inbox_collection"])
    elif cfg["inbox_tag"]:
        candidates = z.list_papers_by_tag(cfg["inbox_tag"])
    else:
        return 0

    main_keys = library.member_zotero_keys(slug)   # already in the local collection
    seen = _existing_keys(slug)

    added = 0
    con = connect()
    try:
        for p in candidates:
            if p.key in main_keys or p.key in seen:
                continue
            full = z.paper_full(p.key) or {}
            con.execute(
                "INSERT INTO triage_items "
                "(collection_slug, zotero_key, title, abstract, authors, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (slug, p.key, p.title, full.get("abstract", ""), p.authors),
            )
            added += 1
        con.commit()
    finally:
        con.close()
    return added


def list_triage(slug: str, status: str = "pending") -> list[dict]:
    con = connect()
    try:
        rows = con.execute(
            "SELECT * FROM triage_items WHERE collection_slug = ? AND status = ? "
            "ORDER BY created_at DESC",
            (slug, status),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def get_item(triage_id: int) -> dict | None:
    con = connect()
    try:
        row = con.execute("SELECT * FROM triage_items WHERE id = ?", (triage_id,)).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def generate_pitch(slug: str, triage_id: int) -> str:
    """LLM relevance pitch grounded in the collection's purpose + wiki index."""
    item = get_item(triage_id)
    if not item:
        return ""
    purpose = _read(COLLECTIONS_DIR / slug / "purpose.md")
    wiki_index = _read(_wikidir(slug) / "index.md")
    messages = [
        {
            "role": "system",
            "content": (
                "You assess whether a candidate paper fits a research collection. "
                "Write a 2-3 sentence pitch grounded in the collection's purpose and "
                "current wiki. Be honest if it's a weak fit."
            ),
        },
        {
            "role": "user",
            "content": (
                f"COLLECTION PURPOSE:\n{purpose or '(none)'}\n\n"
                f"WIKI INDEX:\n{wiki_index or '(empty)'}\n\n"
                f"CANDIDATE:\n{item['title']}\n{item['abstract']}"
            ),
        },
    ]
    pitch = llm.complete(messages)
    con = connect()
    try:
        con.execute(
            "UPDATE triage_items SET llm_relevance_note = ? WHERE id = ?",
            (pitch, triage_id),
        )
        con.commit()
    finally:
        con.close()
    return pitch


def _set_status(triage_id: int, status: str) -> None:
    con = connect()
    try:
        con.execute("UPDATE triage_items SET status = ? WHERE id = ?", (status, triage_id))
        con.commit()
    finally:
        con.close()


def _import_candidate(slug: str, *, zotero_key, arxiv_id, title, authors, abstract) -> int:
    """Mint/import a candidate paper into the local store + add it to the collection.
    Pulls its PDF into the store eagerly when the collection's copy_mode is eager.
    Returns the new paper_id. Reaches Zotero only later, via Sync."""
    origin = "zotero-import" if zotero_key else "arxiv-suggested"
    pid = library.upsert_paper(
        zotero_key=zotero_key or None,
        arxiv_id=arxiv_id or None,
        title=title or "(untitled)",
        authors=authors or "",
        abstract=abstract or "",
        origin=origin,
    )
    flag = "local" if zotero_key else "arxiv-suggested"
    library.add_membership(slug, pid, flag)
    col = library.get_collection(slug)
    if col and col["copy_mode"] == "eager":
        if zotero_key:
            pdf_store.copy_into_store(pid, get_zotero().pdf_path(zotero_key))
        elif arxiv_id:
            pdf_store.fetch_arxiv_pdf(pid, arxiv_id)
    return pid


def accept(slug: str, triage_id: int) -> tuple[bool, str]:
    item = get_item(triage_id)
    if not item:
        return False, "Not found."
    pid = _import_candidate(
        slug,
        zotero_key=item.get("zotero_key"),
        arxiv_id=item.get("arxiv_id"),
        title=item.get("title"),
        authors=item.get("authors"),
        abstract=item.get("abstract"),
    )
    library.link_triage_paper(triage_id, pid)
    _set_status(triage_id, "accepted")
    return True, "Accepted into the collection (local). Use Sync to push it to Zotero."


def reject(slug: str, triage_id: int) -> tuple[bool, str]:
    # Reject is local-only: it never tags/writes Zotero (no write-back here).
    if not get_item(triage_id):
        return False, "Not found."
    _set_status(triage_id, "rejected")
    return True, "Rejected locally."


def defer(slug: str, triage_id: int) -> tuple[bool, str]:
    if not get_item(triage_id):
        return False, "Not found."
    _set_status(triage_id, "deferred")
    return True, "Deferred."


def accept_arxiv_into_collection(
    slug: str, arxiv_id: str, title: str = "", authors: str = "", abstract: str = "", note: str = ""
) -> int:
    """Send a discovered arXiv paper straight into the collection as a local
    (arxiv-suggested) member. Used by the 'send to collection' gap action."""
    return _import_candidate(
        slug, zotero_key=None, arxiv_id=arxiv_id, title=title, authors=authors, abstract=abstract
    )


def add_entries(slug: str, entries: list[dict]) -> list[int]:
    """Add parsed arXiv/OpenReview entries (the Add-paper wizard) as user-curated
    (app-created) members and kick off a background PDF download for each. ``entries`` are
    the parsed dicts {kind, id, title, authors, year, abstract}. Returns the created ids."""
    col = library.get_collection(slug)
    eager = bool(col and col["copy_mode"] == "eager")
    pids = []
    for e in entries:
        kind, rid = e.get("kind"), (e.get("id") or "").strip()
        if not rid or kind not in ("arxiv", "openreview"):
            continue
        kw = {"title": e.get("title") or "(untitled)", "authors": e.get("authors") or "",
              "year": e.get("year") or "", "abstract": e.get("abstract") or "",
              "origin": "app-created"}
        if kind == "arxiv":
            pid = library.upsert_paper(arxiv_id=rid, **kw)
        else:
            pid = library.upsert_paper(openreview_id=rid, **kw)
        library.add_membership(slug, pid, "local")
        # A manual add is explicit intent: if this paper was removed (graveyard or even a
        # permanently-deleted tombstone), un-bury it so it returns to the collection.
        library.restore_removal(slug, pid)
        if eager and not pdf_store.has_pdf(pid):
            pdf_store.start_download(pid)        # streams in the background; row shows a ring
        pids.append(pid)
    return pids


def add_arxiv_manual(slug: str, raw_id: str) -> tuple[bool, str]:
    """Add one paper to a collection by arXiv id/URL (used by gap/triage paths)."""
    from . import discover

    meta = discover.fetch_arxiv_metadata(raw_id)
    if not meta:
        return False, "Couldn't find that arXiv paper — check the id or URL."
    add_entries(slug, [{"kind": "arxiv", "id": meta["arxiv_id"], "title": meta["title"],
                        "authors": meta["authors"], "year": meta["year"],
                        "abstract": meta["abstract"]}])
    return True, f"Added “{meta['title']}”."


def add_from_arxiv(slug: str, arxiv_id: str, title: str, note: str) -> int:
    """Enqueue a discovered arXiv paper into the triage queue (pending)."""
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO triage_items "
            "(collection_slug, arxiv_id, title, llm_relevance_note, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (slug, arxiv_id, title, note),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()
