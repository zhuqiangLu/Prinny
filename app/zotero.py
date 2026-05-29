"""Zotero adapter.

Zotero is the source of truth for papers, collections, and PDF paths. Per
CLAUDE.md this must be an abstract interface with two implementations so a
networked backend can be added later as a new file, not a refactor:

  - ``LocalZotero``  — reads the local install. Prefers the local HTTP API
    (port 23119) when reachable, falls back to the read-only SQLite DB.
  - ``WebZotero``    — stub for a future Zotero Web API backend.

SQLite is opened read-only and ``immutable=1`` so we can read safely even while
Zotero is running (it keeps the DB locked under WAL otherwise).
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import load_config


@dataclass(frozen=True)
class Collection:
    id: int
    name: str
    parent_id: int | None


@dataclass(frozen=True)
class Paper:
    key: str            # Zotero item key (stable, used in URLs)
    title: str
    authors: str        # display string, e.g. "Chen, Liu, Xu"
    year: str           # 4-digit year or ""
    has_pdf: bool


class ZoteroBackend(ABC):
    """Abstract interface. The rest of the app only touches this."""

    @abstractmethod
    def list_collections(self) -> list[Collection]:
        """Return all collections as (id, name, parent_id)."""

    @abstractmethod
    def resolve_collection_id(self, slug: str) -> Collection | None:
        """Find the collection whose name slugifies to ``slug``."""

    @abstractmethod
    def list_papers(self, collection_id: int) -> list[Paper]:
        """Papers (top-level items) in a collection."""

    @abstractmethod
    def get_paper(self, key: str) -> Paper | None:
        """A single paper by Zotero item key."""

    @abstractmethod
    def pdf_path(self, key: str) -> Path | None:
        """Absolute path to the paper's attached PDF, or None."""

    @abstractmethod
    def source(self) -> str:
        """Human-readable description of where data is coming from."""

    # --- optional reads/writes (concrete default = unsupported) -----------
    # Added as non-abstract so existing backends/tests need not implement them.
    def paper_full(self, key: str) -> dict | None:
        """Full metadata incl. abstract + dateAdded, or None."""
        raise NotImplementedError

    def list_papers_by_tag(self, tag: str) -> list[Paper]:
        raise NotImplementedError

    def list_papers_by_collection_name(self, name: str) -> list[Paper]:
        raise NotImplementedError

    def read_annotations(self, paper_key: str) -> list[dict]:
        """Read existing Zotero annotations for a paper's PDF (read-only)."""
        return []

    def move_item_to_collection(self, item_key: str, collection_name: str) -> None:
        raise ZoteroWriteError("Zotero write-back requires the Local API (writes).")

    def tag_item(self, item_key: str, tag: str) -> None:
        raise ZoteroWriteError("Zotero write-back requires the Local API (writes).")


class ZoteroWriteError(RuntimeError):
    """Raised when a Zotero write can't be performed (e.g. Local API disabled)."""


class WebZotero(ZoteroBackend):
    """Stub for the future Zotero Web API backend (CLAUDE.md: design for it)."""

    def list_collections(self) -> list[Collection]:  # pragma: no cover - stub
        raise NotImplementedError("WebZotero is not implemented yet.")

    def resolve_collection_id(self, slug: str) -> Collection | None:  # pragma: no cover
        raise NotImplementedError("WebZotero is not implemented yet.")

    def list_papers(self, collection_id: int) -> list[Paper]:  # pragma: no cover
        raise NotImplementedError("WebZotero is not implemented yet.")

    def get_paper(self, key: str) -> Paper | None:  # pragma: no cover
        raise NotImplementedError("WebZotero is not implemented yet.")

    def pdf_path(self, key: str) -> Path | None:  # pragma: no cover
        raise NotImplementedError("WebZotero is not implemented yet.")

    def source(self) -> str:  # pragma: no cover - stub
        return "web (not implemented)"


class LocalZotero(ZoteroBackend):
    def __init__(
        self,
        sqlite_path: str,
        api_base: str,
        write_api_base: str | None = None,
        write_api_key: str | None = None,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.api_base = api_base.rstrip("/")
        self.write_api_base = (write_api_base or api_base).rstrip("/")
        self.write_api_key = write_api_key or ""
        self._http_usable: bool | None = None

    # --- backend detection -------------------------------------------------
    def http_available(self) -> bool:
        """True if the local HTTP API answers the collections endpoint with 2xx.

        Note: a bare connector ping may succeed (502) while the data API is
        disabled, so we probe the actual endpoint we'd use.
        """
        if self._http_usable is not None:
            return self._http_usable
        try:
            r = self._http_get("/api/users/0/collections", timeout=2.0)
            self._http_usable = r.status_code == 200
        except httpx.HTTPError:
            self._http_usable = False
        return self._http_usable

    def _http_get(self, path: str, timeout: float) -> httpx.Response:
        # Zotero is local; never route these calls through an HTTP proxy
        # (the user may have http_proxy/https_proxy set in their env).
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            return client.get(f"{self.api_base}{path}")

    def source(self) -> str:
        return "http" if self.http_available() else f"sqlite ({self.sqlite_path})"

    # --- reads -------------------------------------------------------------
    def list_collections(self) -> list[Collection]:
        if self.http_available():
            return self._list_collections_http()
        return self._list_collections_sqlite()

    def _list_collections_http(self) -> list[Collection]:
        r = self._http_get("/api/users/0/collections", timeout=5.0)
        r.raise_for_status()
        out: list[Collection] = []
        for item in r.json():
            data = item.get("data", item)
            key = data.get("key")
            parent = data.get("parentCollection") or None
            out.append(Collection(id=key, name=data.get("name", ""), parent_id=parent))
        return out

    def _connect_ro(self) -> sqlite3.Connection:
        uri = f"file:{self.sqlite_path}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        return con

    def _list_collections_sqlite(self) -> list[Collection]:
        con = self._connect_ro()
        try:
            rows = con.execute(
                "SELECT collectionID, collectionName, parentCollectionID "
                "FROM collections "
                "WHERE collectionID NOT IN (SELECT collectionID FROM deletedCollections) "
                "ORDER BY collectionName COLLATE NOCASE"
                if _has_table(con, "deletedCollections")
                else "SELECT collectionID, collectionName, parentCollectionID "
                "FROM collections ORDER BY collectionName COLLATE NOCASE"
            ).fetchall()
        finally:
            con.close()
        return [Collection(id=r[0], name=r[1], parent_id=r[2]) for r in rows]

    # --- collection resolution + papers (SQLite-canonical) -----------------
    # Paper reads use SQLite directly because they need Zotero's integer
    # collectionID and item rows. The HTTP API is currently disabled here; when
    # it is enabled (Phase 7 needs it for writes) we revisit the read path.
    def resolve_collection_id(self, slug: str) -> Collection | None:
        from .slugs import slugify

        for c in self._list_collections_sqlite():
            if slugify(c.name) == slug:
                return c
        return None

    def _field_ids(self, con: sqlite3.Connection) -> dict[str, int]:
        return {
            r[0]: r[1]
            for r in con.execute("SELECT fieldName, fieldID FROM fields")
        }

    def _excluded_type_ids(self, con: sqlite3.Connection) -> set[int]:
        # Notes/annotations are never papers. Attachments ARE kept: a top-level
        # collection member that is itself a PDF (e.g. an openreview.net import) is
        # a paper in its own right. Child attachments aren't collection members, so
        # they don't show up here anyway.
        rows = con.execute(
            "SELECT itemTypeID FROM itemTypes WHERE typeName IN ('note','annotation')"
        ).fetchall()
        return {r[0] for r in rows}

    def _descendant_collection_ids(self, con: sqlite3.Connection, root_id: int) -> list[int]:
        """``root_id`` plus all of its (transitive) subcollection IDs, so importing a
        collection brings in papers nested in its subfolders too."""
        ids = [root_id]
        try:
            children_by_parent: dict[int, list[int]] = {}
            for cid, parent in con.execute(
                "SELECT collectionID, parentCollectionID FROM collections"
            ):
                children_by_parent.setdefault(parent, []).append(cid)
        except sqlite3.OperationalError:
            return ids
        stack = [root_id]
        seen = {root_id}
        while stack:
            for child in children_by_parent.get(stack.pop(), []):
                if child not in seen:
                    seen.add(child)
                    ids.append(child)
                    stack.append(child)
        return ids

    def list_papers(self, collection_id: int) -> list[Paper]:
        con = self._connect_ro()
        try:
            fields = self._field_ids(con)
            excluded = self._excluded_type_ids(con)
            title_fid = fields.get("title")
            date_fid = fields.get("date")

            coll_ids = self._descendant_collection_ids(con, collection_id)
            rows = con.execute(
                """
                SELECT i.itemID, i.key, MIN(ci.orderIndex) AS oi
                FROM collectionItems ci
                JOIN items i ON i.itemID = ci.itemID
                WHERE ci.collectionID IN ({colls})
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                  AND i.itemTypeID NOT IN ({types})
                GROUP BY i.itemID, i.key
                ORDER BY oi
                """.format(
                    colls=",".join("?" * len(coll_ids)),
                    types=",".join("?" * len(excluded)) or "NULL",
                ),
                [*coll_ids, *excluded],
            ).fetchall()

            papers: list[Paper] = []
            for r in rows:
                item_id, key = r[0], r[1]
                pdf = self._item_pdf(con, item_id)
                title = self._field_value(con, item_id, title_fid)
                if not title and pdf and pdf[1]:
                    # standalone attachment with no title field → use the filename
                    title = pdf[1].split("/")[-1]
                title = title or "(untitled)"
                date = self._field_value(con, item_id, date_fid) or ""
                year_m = re.search(r"\b(\d{4})\b", date)
                authors = self._authors(con, item_id)
                has_pdf = pdf is not None
                papers.append(
                    Paper(
                        key=key,
                        title=title,
                        authors=authors,
                        year=year_m.group(1) if year_m else "",
                        has_pdf=has_pdf,
                    )
                )
            return papers
        finally:
            con.close()

    def get_paper(self, key: str) -> Paper | None:
        con = self._connect_ro()
        try:
            row = con.execute(
                "SELECT itemID FROM items WHERE key = ?", (key,)
            ).fetchone()
            if not row:
                return None
            item_id = row[0]
            fields = self._field_ids(con)
            pdf = self._item_pdf(con, item_id)
            title = self._field_value(con, item_id, fields.get("title"))
            if not title and pdf and pdf[1]:
                title = pdf[1].split("/")[-1]
            date = self._field_value(con, item_id, fields.get("date")) or ""
            year_m = re.search(r"\b(\d{4})\b", date)
            return Paper(
                key=key,
                title=title or "(untitled)",
                authors=self._authors(con, item_id),
                year=year_m.group(1) if year_m else "",
                has_pdf=pdf is not None,
            )
        finally:
            con.close()

    def pdf_path(self, key: str) -> Path | None:
        con = self._connect_ro()
        try:
            row = con.execute(
                "SELECT itemID FROM items WHERE key = ?", (key,)
            ).fetchone()
            if not row:
                return None
            att = self._item_pdf(con, row[0])
            if not att:
                return None
            att_key, path = att
            if path and path.startswith("storage:"):
                filename = path[len("storage:"):]
                return self.storage_dir() / att_key / filename
            if path:  # linked file: absolute path on disk
                p = Path(path)
                return p if p.is_absolute() else None
            return None
        finally:
            con.close()

    # --- small SQLite helpers ---------------------------------------------
    def storage_dir(self) -> Path:
        return Path(self.sqlite_path).resolve().parent / "storage"

    def _field_value(
        self, con: sqlite3.Connection, item_id: int, field_id: int | None
    ) -> str | None:
        if field_id is None:
            return None
        row = con.execute(
            """
            SELECT idv.value FROM itemData idd
            JOIN itemDataValues idv ON idv.valueID = idd.valueID
            WHERE idd.itemID = ? AND idd.fieldID = ?
            """,
            (item_id, field_id),
        ).fetchone()
        return row[0] if row else None

    def _authors(self, con: sqlite3.Connection, item_id: int) -> str:
        rows = con.execute(
            """
            SELECT c.lastName, c.firstName
            FROM itemCreators ic
            JOIN creators c ON c.creatorID = ic.creatorID
            WHERE ic.itemID = ?
            ORDER BY ic.orderIndex
            """,
            (item_id,),
        ).fetchall()
        names = [(r[0] or r[1] or "").strip() for r in rows]
        names = [n for n in names if n]
        if not names:
            return ""
        if len(names) > 3:
            return ", ".join(names[:3]) + " et al."
        return ", ".join(names)

    def _item_pdf(self, con: sqlite3.Connection, item_id: int) -> tuple[str, str] | None:
        """(attachment_key, path) for the item's PDF — the item itself if it's a
        standalone PDF attachment, otherwise its first child PDF."""
        row = con.execute(
            """
            SELECT i.key, ia.path FROM itemAttachments ia
            JOIN items i ON i.itemID = ia.itemID
            WHERE ia.itemID = ?
              AND ia.contentType = 'application/pdf'
              AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
            """,
            (item_id,),
        ).fetchone()
        if row:
            return (row[0], row[1])
        return self._pdf_attachment(con, item_id)

    def _pdf_attachment(
        self, con: sqlite3.Connection, parent_item_id: int
    ) -> tuple[str, str] | None:
        """Return (attachment_key, path) for the item's first PDF, or None."""
        row = con.execute(
            """
            SELECT ai.key, ia.path
            FROM itemAttachments ia
            JOIN items ai ON ai.itemID = ia.itemID
            WHERE ia.parentItemID = ?
              AND ia.contentType = 'application/pdf'
              AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
            ORDER BY ia.itemID
            LIMIT 1
            """,
            (parent_item_id,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    # --- richer reads ------------------------------------------------------
    def paper_full(self, key: str) -> dict | None:
        con = self._connect_ro()
        try:
            row = con.execute(
                "SELECT itemID, dateAdded FROM items WHERE key = ?", (key,)
            ).fetchone()
            if not row:
                return None
            item_id, date_added = row[0], row[1]
            fields = self._field_ids(con)
            pdf = self._item_pdf(con, item_id)
            title = self._field_value(con, item_id, fields.get("title"))
            if not title and pdf and pdf[1]:
                title = pdf[1].split("/")[-1]
            date = self._field_value(con, item_id, fields.get("date")) or ""
            abstract = self._field_value(con, item_id, fields.get("abstractNote")) or ""
            year_m = re.search(r"\b(\d{4})\b", date)
            return {
                "key": key,
                "title": title or "(untitled)",
                "authors": self._authors(con, item_id),
                "year": year_m.group(1) if year_m else "",
                "abstract": abstract,
                "date_added": date_added,
                "has_pdf": pdf is not None,
            }
        finally:
            con.close()

    def list_papers_by_tag(self, tag: str) -> list[Paper]:
        con = self._connect_ro()
        try:
            rows = con.execute(
                """
                SELECT DISTINCT i.itemID FROM items i
                JOIN itemTags it ON it.itemID = i.itemID
                JOIN tags t ON t.tagID = it.tagID
                WHERE t.name = ?
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                  AND i.itemTypeID NOT IN ({})
                """.format(",".join("?" * len(self._excluded_type_ids(con))) or "NULL"),
                [tag, *self._excluded_type_ids(con)],
            ).fetchall()
            item_ids = [r[0] for r in rows]
        finally:
            con.close()
        return self._papers_from_item_ids(item_ids)

    def list_papers_by_collection_name(self, name: str) -> list[Paper]:
        from .slugs import slugify

        for c in self._list_collections_sqlite():
            if slugify(c.name) == slugify(name):
                return self.list_papers(c.id)
        return []

    def _papers_from_item_ids(self, item_ids: list[int]) -> list[Paper]:
        out = []
        con = self._connect_ro()
        try:
            fields = self._field_ids(con)
            for item_id in item_ids:
                key_row = con.execute(
                    "SELECT key FROM items WHERE itemID = ?", (item_id,)
                ).fetchone()
                if not key_row:
                    continue
                title = self._field_value(con, item_id, fields.get("title")) or "(untitled)"
                date = self._field_value(con, item_id, fields.get("date")) or ""
                ym = re.search(r"\b(\d{4})\b", date)
                out.append(
                    Paper(
                        key=key_row[0],
                        title=title,
                        authors=self._authors(con, item_id),
                        year=ym.group(1) if ym else "",
                        has_pdf=self._pdf_attachment(con, item_id) is not None,
                    )
                )
        finally:
            con.close()
        return out

    # --- annotation read-in (one-way, read-only) --------------------------
    def read_annotations(self, paper_key: str) -> list[dict]:
        """Read Zotero's own annotations for the paper's PDF attachment.

        Zotero stores annotations on the *attachment* item. position is JSON of
        the form {"pageIndex": n, "rects": [[x1,y1,x2,y2], ...]} in PDF points —
        the same shape we use for app annotations.
        """
        con = self._connect_ro()
        try:
            parent = con.execute(
                "SELECT itemID FROM items WHERE key = ?", (paper_key,)
            ).fetchone()
            if not parent:
                return []
            att = con.execute(
                """
                SELECT ia.itemID FROM itemAttachments ia
                WHERE ia.parentItemID = ? AND ia.contentType = 'application/pdf'
                  AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
                ORDER BY ia.itemID LIMIT 1
                """,
                (parent[0],),
            ).fetchone()
            if not att:
                return []
            rows = con.execute(
                "SELECT type, color, position, text, comment FROM itemAnnotations "
                "WHERE parentItemID = ? "
                "AND itemID NOT IN (SELECT itemID FROM deletedItems)",
                (att[0],),
            ).fetchall()
        finally:
            con.close()

        out = []
        for r in rows:
            try:
                pos = json.loads(r["position"])
            except (json.JSONDecodeError, TypeError):
                continue
            out.append(
                {
                    "origin": "zotero",
                    "kind": "highlight" if r["type"] == 1 else "note",
                    "color": r["color"],
                    "page": pos.get("pageIndex", 0),
                    "position_json": r["position"],
                    "selected_text": r["text"] or "",
                    "note_text": r["comment"] or "",
                }
            )
        return out

    # --- writes (HTTP Local API only) -------------------------------------
    # NOTE: the Zotero *local* write API mirrors the Web API v3 shapes used below.
    # These are exercised against a running Zotero with the Local API enabled and a
    # write-enabled key; they degrade to a clear ZoteroWriteError when unavailable.
    def _require_http(self) -> None:
        if not self.http_available():
            raise ZoteroWriteError(
                "Zotero's Local API is not enabled, so changes can't be written "
                "back to Zotero. Enable it in Zotero → Settings → Advanced."
            )

    def _is_local_write(self) -> bool:
        """The local Zotero API (localhost:23119) is unauthenticated — the
        ``Zotero-API-Key`` header is a *web* API (api.zotero.org) concept. So a key is
        only required when the write base points at a remote host."""
        base = (self.write_api_base or self.api_base).rstrip("/")
        host = urllib.parse.urlsplit(base).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1", "")

    def _require_write(self) -> None:
        self._require_http()
        if not self.write_api_key and not self._is_local_write():
            raise ZoteroWriteError(
                "No Zotero write API key configured. Add one on the Settings page "
                "(the remote Web API needs a write-enabled key)."
            )

    def _http_write(self, method: str, path: str, *, json_body=None, content=None,
                    headers: dict | None = None) -> httpx.Response:
        base = (self.write_api_base or self.api_base).rstrip("/")
        h = {"Zotero-API-Version": "3"}
        if self.write_api_key:
            h["Zotero-API-Key"] = self.write_api_key
        if headers:
            h.update(headers)
        with httpx.Client(trust_env=False, timeout=30.0) as client:
            return client.request(method, f"{base}{path}", json=json_body, content=content, headers=h)

    @staticmethod
    def _first_key(data: dict) -> str:
        """Extract the new object key from a Zotero write response envelope."""
        if data.get("failed"):
            raise ZoteroWriteError(f"Zotero rejected the write: {data['failed']}")
        succ = data.get("success") or {}
        if succ:
            return next(iter(succ.values()))
        successful = data.get("successful") or {}
        if successful:
            return next(iter(successful.values())).get("key", "")
        raise ZoteroWriteError(f"Unexpected Zotero write response: {data}")

    def find_collection_key(self, name: str) -> str | None:
        """The Zotero collection KEY (HTTP-API string id) for a collection name."""
        for c in self._list_collections_http():
            if c.name == name:
                return c.id
        return None

    def create_collection(self, name: str, parent_key: str | None = None) -> str:
        self._require_write()
        body = [{"name": name}]
        if parent_key:
            body[0]["parentCollection"] = parent_key
        r = self._http_write("POST", "/api/users/0/collections", json_body=body)
        r.raise_for_status()
        return self._first_key(r.json())

    def create_item(self, item: dict) -> str:
        """Create one regular item. ``item`` is a Zotero item data dict (itemType,
        title, creators, date, abstractNote, collections, ...). Returns its key."""
        self._require_write()
        r = self._http_write("POST", "/api/users/0/items", json_body=[item])
        r.raise_for_status()
        return self._first_key(r.json())

    def add_item_to_collection(self, item_key: str, collection_key: str) -> None:
        self._require_write()
        # Need the item's current version for an optimistic PATCH.
        get = self._http_get(f"/api/users/0/items/{item_key}", timeout=10.0)
        get.raise_for_status()
        version = get.json().get("version") or get.headers.get("Last-Modified-Version", "0")
        cur = (get.json().get("data") or {}).get("collections", [])
        if collection_key in cur:
            return
        r = self._http_write(
            "PATCH", f"/api/users/0/items/{item_key}",
            json_body={"collections": [*cur, collection_key]},
            headers={"If-Unmodified-Since-Version": str(version)},
        )
        if r.status_code not in (200, 204):
            raise ZoteroWriteError(f"Failed to add item to collection ({r.status_code}).")

    def remove_item_from_collection(self, item_key: str, collection_key: str) -> None:
        self._require_write()
        get = self._http_get(f"/api/users/0/items/{item_key}", timeout=10.0)
        get.raise_for_status()
        j = get.json()
        version = j.get("version") or get.headers.get("Last-Modified-Version", "0")
        cur = (j.get("data") or {}).get("collections", [])
        new = [c for c in cur if c != collection_key]
        if new == cur:
            return
        r = self._http_write(
            "PATCH", f"/api/users/0/items/{item_key}",
            json_body={"collections": new},
            headers={"If-Unmodified-Since-Version": str(version)},
        )
        if r.status_code not in (200, 204):
            raise ZoteroWriteError(f"Failed to remove item from collection ({r.status_code}).")

    def delete_item(self, item_key: str) -> None:
        """Delete a Zotero item outright (moves it to Zotero's trash). Affects every
        collection it's in. Needs the item's current version for the optimistic delete."""
        self._require_write()
        get = self._http_get(f"/api/users/0/items/{item_key}", timeout=10.0)
        if get.status_code == 404:
            return  # already gone
        get.raise_for_status()
        version = get.json().get("version") or get.headers.get("Last-Modified-Version", "0")
        r = self._http_write(
            "DELETE", f"/api/users/0/items/{item_key}",
            headers={"If-Unmodified-Since-Version": str(version)},
        )
        if r.status_code not in (200, 204, 404):
            raise ZoteroWriteError(f"Failed to delete item ({r.status_code}).")

    def upload_attachment(self, parent_key: str, pdf_path: Path) -> str:
        """Attach ``pdf_path`` to ``parent_key`` via Zotero's 3-step file upload.

        This multi-step dance (register attachment → request upload authorization →
        upload bytes → register) is the riskiest write path and may need adjusting
        against a live Local API. Returns the attachment item key."""
        import hashlib

        self._require_write()
        data = pdf_path.read_bytes()
        md5 = hashlib.md5(data).hexdigest()
        filename = pdf_path.name
        # 1. register the attachment item (imported_file linkMode).
        att = [{
            "itemType": "attachment", "linkMode": "imported_file",
            "parentItem": parent_key, "title": filename, "filename": filename,
            "contentType": "application/pdf",
        }]
        r = self._http_write("POST", "/api/users/0/items", json_body=att)
        r.raise_for_status()
        att_key = self._first_key(r.json())
        # 2. request upload authorization.
        form = {"md5": md5, "filename": filename, "filesize": str(len(data)),
                "mtime": str(int(pdf_path.stat().st_mtime * 1000))}
        auth = self._http_write(
            "POST", f"/api/users/0/items/{att_key}/file",
            content="&".join(f"{k}={v}" for k, v in form.items()),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "If-None-Match": "*"},
        )
        auth.raise_for_status()
        info = auth.json()
        if info.get("exists"):
            return att_key  # Zotero already has these bytes.
        # 3. upload bytes per the authorization, then register the upload.
        up = info.get("url")
        if up:  # full (Web-API style) upload
            with httpx.Client(trust_env=False, timeout=60.0) as client:
                client.post(up, data=info.get("params", {}),
                            files={"file": (filename, data, "application/pdf")})
            self._http_write(
                "POST", f"/api/users/0/items/{att_key}/file",
                content=f"upload={info.get('uploadKey', '')}",
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "If-None-Match": "*"},
            )
        return att_key


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def get_zotero() -> ZoteroBackend:
    """Factory: build the configured backend (Local for now)."""
    cfg = load_config()
    return LocalZotero(
        cfg["zotero_sqlite_path"],
        cfg["zotero_api_base"],
        write_api_base=cfg.get("zotero_write_api_base"),
        write_api_key=cfg.get("zotero_write_api_key"),
    )
