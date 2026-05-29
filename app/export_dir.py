"""Directory export: write a collection's papers as a BibTeX file to a local folder.

The user picks (or accepts a default) destination path in the export popup; we create
it if missing and write ``<slug>.bib``. This is read-only with respect to the app's own
data — it only emits a file the user can hand to LaTeX / a reference manager.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import library, pdf_store
from .config import APP_DIR


def default_dir(slug: str) -> Path:
    """Where a collection's export lands unless the user picks elsewhere."""
    return APP_DIR / "exports" / slug


# --- BibTeX rendering ------------------------------------------------------------
_KEY_STRIP = re.compile(r"[^a-zA-Z0-9]+")
# Characters BibTeX treats specially; brace-escape the rest is overkill, just guard braces.
_BRACE = re.compile(r"[{}]")


def _clean(value: str) -> str:
    return _BRACE.sub("", (value or "").strip())


def _authors_bibtex(authors: str) -> str:
    names = [a.strip() for a in (authors or "").split(",") if a.strip()]
    return " and ".join(_clean(n) for n in names)


def _citekey(paper: dict, used: set[str]) -> str:
    authors = (paper.get("authors") or "").split(",")
    first = _KEY_STRIP.sub("", authors[0]) if authors and authors[0].strip() else ""
    year = _KEY_STRIP.sub("", paper.get("year") or "")
    title_word = ""
    for w in re.split(r"\s+", paper.get("title") or ""):
        w = _KEY_STRIP.sub("", w)
        if len(w) > 2:
            title_word = w
            break
    base = (first + year + title_word) or f"paper{paper.get('id', '')}"
    base = base[:48] or "paper"
    key, n = base, 2
    while key in used:
        key = f"{base}_{n}"
        n += 1
    used.add(key)
    return key


def _entry(paper: dict, key: str) -> str:
    arxiv = (paper.get("arxiv_id") or "").strip()
    etype = "misc" if arxiv else "article"
    fields: list[tuple[str, str]] = [("title", _clean(paper.get("title") or "(untitled)"))]
    authors = _authors_bibtex(paper.get("authors") or "")
    if authors:
        fields.append(("author", authors))
    if (paper.get("year") or "").strip():
        fields.append(("year", _clean(paper["year"])))
    if arxiv:
        fields.append(("eprint", _clean(arxiv)))
        fields.append(("archivePrefix", "arXiv"))
        fields.append(("url", f"https://arxiv.org/abs/{_clean(arxiv)}"))
    lines = [f"@{etype}{{{key},"]
    lines += [f"  {name} = {{{val}}}," for name, val in fields]
    lines.append("}")
    return "\n".join(lines)


def to_bibtex(slug: str) -> str:
    papers = library.list_papers(slug)
    used: set[str] = set()
    entries = [_entry(p, _citekey(p, used)) for p in papers]
    header = f"% {len(papers)} papers exported from collection '{slug}' by Paper Agent\n"
    return header + "\n\n".join(entries) + ("\n" if entries else "")


def _safe_name(s: str) -> str:
    return _KEY_STRIP.sub("-", (s or "").strip()).strip("-")[:80] or "paper"


# --- public action ---------------------------------------------------------------
def export_pdfs(slug: str, dest: str | Path | None = None) -> dict:
    """Clone the collection's cached PDFs into ``dest`` (created if missing). One file per
    paper, named ``<citekey>.pdf``. No BibTeX. Papers without a reachable PDF are skipped.

    Raises ValueError on an unusable path so the route can show a friendly error."""
    target = Path(dest).expanduser() if dest else default_dir(slug)
    if not target.is_absolute():
        raise ValueError("Path must be absolute.")
    if target.exists() and not target.is_dir():
        raise ValueError("That path exists and is not a folder.")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Could not create folder: {exc}") from exc

    used: set[str] = set()
    copied = missing = 0
    for p in library.list_papers(slug):
        src = pdf_store.ensure_cached(p["id"])          # fetch from source if not cached
        if not src or not Path(src).exists():
            missing += 1
            continue
        name = _safe_name(_citekey(p, used)) + ".pdf"
        shutil.copy2(src, target / name)
        copied += 1
    return {"dir": str(target), "copied": copied, "missing": missing}
