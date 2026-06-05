"""Chat context assembly (CLAUDE.md Phase 2).

Builds the message list sent to the LLM for one turn:

  1. System prompt from purpose.md + latest 3 thoughts + current wiki.
  2. If a paper is open: its metadata, the user's notes, and ~8k chars of PDF text.
  3. The recent thread history is appended by the caller.

purpose.md, thoughts/, wiki/ and notes don't exist until later phases, so every
read here degrades gracefully to "nothing yet".

This module is strictly read-only with respect to user artifacts.
"""

from __future__ import annotations

from pathlib import Path

from .config import COLLECTIONS_DIR
from .db import connect
from .pdf_text import extract_text

PDF_CHAR_BUDGET = 8000


def _collection_dir(slug: str) -> Path:
    return COLLECTIONS_DIR / slug


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _latest_thoughts(slug: str, n: int = 3) -> list[str]:
    tdir = _collection_dir(slug) / "thoughts"
    if not tdir.is_dir():
        return []
    files = sorted(tdir.glob("*.md"), reverse=True)[:n]
    return [_read(f) for f in files if _read(f)]


def _wiki_overview(slug: str) -> str:
    """A compact summary of the live cognitive-model wiki (wiki/sections/*) — thesis,
    landscape, concepts. Falls back to the legacy wiki/index.md for old collections."""
    import json as _json

    from . import wiki as wiki_mod
    bits: list[str] = []
    th = wiki_mod.current_thesis(slug)
    if th and th.get("one_paragraph"):
        bits.append("Thesis: " + th["one_paragraph"])
        for k, label in (("core_tension", "Core tension"), ("key_intuition", "Key intuition"),
                         ("central_question", "Central question")):
            if th.get(k):
                bits.append(f"  {label}: {th[k]}")
    lp = wiki_mod._landscape_json_path(slug)
    if lp.is_file():
        try:
            ls = _json.loads(lp.read_text(encoding="utf-8")).get("landscape") or {}
            for col, label in (("problems", "Problems"), ("methods", "Methods"),
                               ("debates", "Debates"), ("open_questions", "Open questions")):
                items = [it.get("text") if isinstance(it, dict) else it for it in (ls.get(col) or [])]
                items = [i for i in items if i]
                if items:
                    bits.append(f"{label}: " + "; ".join(items))
        except (ValueError, OSError):
            pass
    names = wiki_mod._concept_names(slug)
    if names:
        bits.append("Concepts: " + ", ".join(names))
    if bits:
        return "\n".join(bits)
    return _read(_collection_dir(slug) / "wiki" / "index.md")     # legacy fallback


def _paper_titles(slug: str, limit: int = 80) -> list[str]:
    from . import library
    return [p.get("title", "") for p in library.list_papers(slug) if p.get("title")][:limit]


def _paper_notes(slug: str, paper_id: int) -> str:
    con = connect()
    try:
        row = con.execute(
            "SELECT summary, thoughts, key_quotes, status FROM paper_notes "
            "WHERE paper_id = ? AND collection_slug = ?",
            (paper_id, slug),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return ""
    bits = []
    if row["summary"]:
        bits.append(f"Summary: {row['summary']}")
    if row["thoughts"]:
        bits.append(f"My thoughts: {row['thoughts']}")
    if row["key_quotes"]:
        bits.append(f"Key quotes:\n{row['key_quotes']}")
    if row["status"]:
        bits.append(f"(status: {row['status']})")
    return "\n".join(bits)


def _highlights_block(slug: str, paper_id: int) -> str:
    """The user's app highlights for this paper, with color meaning as a soft signal."""
    from . import annotations

    meanings = {
        "#ffd400": "important", "#5fd35f": "agree/build-on",
        "#ff6666": "disagree/weak", "#6fb3ff": "don't follow",
    }
    rows = annotations.list_app(paper_id, slug)
    if not rows:
        return ""
    lines = []
    for a in rows:
        tag = meanings.get((a.get("color") or "").lower(), "")
        sel = (a.get("selected_text") or "").strip().replace("\n", " ")
        note = (a.get("note_text") or "").strip()
        if not sel and not note:
            continue
        prefix = f"[{tag}] " if tag else ""
        line = f'- {prefix}"{sel}"'
        if note:
            line += f" — {note}"
        lines.append(line)
    return "\n".join(lines)


def collection_system_prompt(slug: str, collection_name: str) -> str:
    purpose = _read(_collection_dir(slug) / "purpose.md")
    thoughts = _latest_thoughts(slug)
    wiki = _wiki_overview(slug)

    parts = [
        "You are a research assistant for a personal paper collection. "
        "The wiki and notes capture the USER's own thinking; papers are evidence. "
        "You help the user read, organize, and find gaps — you do not replace their "
        "reading, and you never claim to have updated their notes or wiki.",
        f"Collection: {collection_name}.",
    ]
    parts.append(
        f"Collection purpose:\n{purpose}" if purpose
        else "Collection purpose: (not written yet)."
    )
    if thoughts:
        joined = "\n\n---\n\n".join(thoughts)
        parts.append(f"The user's most recent thoughts:\n{joined}")
    else:
        parts.append("The user has not recorded thoughts yet.")
    titles = _paper_titles(slug)
    if titles:
        listed = "\n".join(f"- {t}" for t in titles)
        more = "" if len(titles) < 80 else "\n(…more)"
        parts.append(f"The collection has {len(titles)} paper(s):\n{listed}{more}")
    else:
        parts.append("The collection has no papers yet.")
    parts.append(
        f"The wiki currently says:\n{wiki}" if wiki
        else "The wiki is empty so far."
    )
    return "\n\n".join(parts)


def paper_system_prompt(collection_name: str, paper_title: str) -> str:
    return (
        "You are a research assistant helping the user read ONE paper. The notes, "
        "highlights, and conversation capture the USER's own thinking; the paper is "
        "evidence. Help them understand and react to this paper; do not replace their "
        "reading, and never claim to have updated their notes or wiki. "
        f"Collection: {collection_name}. Paper: {paper_title}."
    )


def paper_block(slug: str, paper_id: int) -> tuple[str, list[dict]]:
    """Return (context text, refs) for the open paper, or ("", []) if absent.

    Reads metadata from the local store and PDF text from the local PDF store.
    Includes the user's notes, highlights, and a PDF excerpt — the per-paper
    'exhaust' the LLM can reason over.
    """
    from . import library, pdf_store

    paper = library.get_paper(paper_id)
    if paper is None:
        return "", []
    refs: list[dict] = [{"type": "paper", "id": paper_id}]
    lines = [
        f"Title: {paper['title']}",
        f"Authors: {paper['authors']}",
        f"Year: {paper['year']}",
    ]
    notes = _paper_notes(slug, paper_id)
    if notes:
        refs.append({"type": "note", "id": paper_id})
        lines.append(f"\nThe user's notes on this paper:\n{notes}")

    highlights = _highlights_block(slug, paper_id)
    if highlights:
        lines.append(f"\nThe user's highlights (color = their reaction):\n{highlights}")

    pdf = pdf_store.ensure_cached(paper_id)
    if pdf and pdf.exists():
        text = extract_text(pdf, PDF_CHAR_BUDGET)
        if text:
            lines.append(
                f"\nExcerpt from the paper's PDF (first ~{PDF_CHAR_BUDGET} chars):\n{text}"
            )
    return "\n".join(lines), refs


def build_messages(
    slug: str,
    collection_name: str,
    history: list[dict],
    user_text: str,
    paper_id: int | None,
    include_collection: bool = False,
    images: list[str] | None = None,
    artifact: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Assemble the message list + the context refs used this turn.

    Two contexts, one mechanism:
      - paper_id set  -> paper-focused chat (this paper's notes/highlights/PDF).
        ``include_collection`` (the ``/collection`` command) also injects the wiki.
      - paper_id None -> collection chat, grounded in the wiki.

    ``artifact`` (a compacted summary of earlier turns) is injected as grounding so it
    survives regardless of the history window.
    """
    from . import library

    refs: list[dict] = []
    messages: list[dict] = []

    if paper_id:
        paper = library.get_paper(paper_id)
        title = paper["title"] if paper else str(paper_id)
        messages.append({"role": "system", "content": paper_system_prompt(collection_name, title)})
        block, paper_refs = paper_block(slug, paper_id)
        if block:
            refs.extend(paper_refs)
            messages.append({"role": "system", "content": block})
        if include_collection:
            messages.append({
                "role": "system",
                "content": "Collection context (requested via /collection):\n"
                + collection_system_prompt(slug, collection_name),
            })
    else:
        messages.append({"role": "system", "content": collection_system_prompt(slug, collection_name)})

    if artifact:
        messages.append({
            "role": "system",
            "content": "Compacted summary of the earlier conversation (your memory of "
            f"what was discussed before):\n{artifact}",
        })

    messages.extend(history)
    # Images are NOT inlined here: CLI agents (our only backend) can't take inline image
    # content. Pasted images reach the sub-agent via files (Claude Read / Codex -i); this
    # tool-less classic path is text-only, so ``images`` is accepted but not embedded.
    if images:
        user_text = (user_text or "") + (
            f"\n\n[{len(images)} image(s) attached — not visible on this text-only path; "
            "open the paper so the reading sub-agent can view them.]")
    messages.append({"role": "user", "content": user_text})
    return messages, refs
