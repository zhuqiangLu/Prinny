"""Resolve real paper titles from OpenReview.

Papers imported as bare openreview.net PDF attachments carry a junk "title" that is
just the PDF URL (e.g. ``openreview.net/pdf?id=lh3Aa1u7kU``). The ``id`` there is the
OpenReview note id; one API call returns the official title. Best-effort and external
(uses the env proxy); failures leave the title untouched.
"""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger("paper_agent.openreview")

_ID_RE = re.compile(r"openreview\.net/(?:pdf|forum)\?id=([A-Za-z0-9_\-]+)", re.IGNORECASE)
# api2 serves newer venues (ICLR 2024+); api (v1) the older ones — try both.
_BASES = ("https://api2.openreview.net", "https://api.openreview.net")


def extract_id(text: str | None) -> str | None:
    m = _ID_RE.search(text or "")
    return m.group(1) if m else None


def fetch_title(openreview_id: str) -> str | None:
    for base in _BASES:
        try:
            r = httpx.get(f"{base}/notes", params={"id": openreview_id}, timeout=10.0)
            if r.status_code != 200:
                continue
            notes = r.json().get("notes") or []
            if not notes:
                continue
            title = (notes[0].get("content") or {}).get("title")
            if isinstance(title, dict):          # v2 content: {"value": "..."}
                title = title.get("value")
            if isinstance(title, str) and title.strip():
                return title.strip()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("OpenReview lookup failed for %s via %s: %s", openreview_id, base, exc)
    return None


def title_for(text: str | None) -> str | None:
    """Convenience: given a junk title/URL, return the real OpenReview title or None."""
    oid = extract_id(text)
    return fetch_title(oid) if oid else None


def _v(field):
    """Unwrap an OpenReview content field (v2 wraps values as {'value': ...})."""
    return field.get("value") if isinstance(field, dict) else field


def fetch_metadata(openreview_id: str) -> dict | None:
    """Full metadata for an OpenReview note: {openreview_id, title, authors, year, abstract}.
    Best-effort; returns None if the note can't be found."""
    for base in _BASES:
        try:
            r = httpx.get(f"{base}/notes", params={"id": openreview_id}, timeout=15.0)
            if r.status_code != 200:
                continue
            notes = r.json().get("notes") or []
            if not notes:
                continue
            note = notes[0]
            content = note.get("content") or {}
            title = _v(content.get("title")) or ""
            authors = _v(content.get("authors")) or []
            if isinstance(authors, str):
                authors = [authors]
            abstract = _v(content.get("abstract")) or ""
            year = ""
            cdate = note.get("cdate") or note.get("pdate")
            if isinstance(cdate, (int, float)):
                import datetime
                year = str(datetime.datetime.utcfromtimestamp(cdate / 1000).year)
            t = title.strip() if isinstance(title, str) else ""
            if not t:
                continue
            return {
                "openreview_id": openreview_id,
                "title": t,
                "authors": ", ".join(a for a in authors if isinstance(a, str)),
                "year": year,
                "abstract": abstract.strip() if isinstance(abstract, str) else "",
            }
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("OpenReview metadata failed for %s via %s: %s", openreview_id, base, exc)
    return None
