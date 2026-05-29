"""Markdown rendering with server-side wikilink resolution.

``[[Page Name]]`` becomes a link to that collection's wiki page. Wiki routes
arrive in Phase 5; until then the links resolve to a (currently 404) wiki URL,
which is the intended behavior per CLAUDE.md ("Wikilinks become real links").
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

from .slugs import slugify

_md = MarkdownIt("commonmark", {"linkify": True, "breaks": True}).enable("table")
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def _resolve_wikilinks(text: str, slug: str) -> str:
    def repl(m: re.Match) -> str:
        page = m.group(1).strip()
        label = (m.group(2) or page).strip()
        return f"[{label}](/c/{slug}/wiki/{slugify(page)})"

    return _WIKILINK.sub(repl, text)


def render(text: str, slug: str) -> str:
    """Render markdown to HTML, resolving wikilinks first."""
    return _md.render(_resolve_wikilinks(text or "", slug))
