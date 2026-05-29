"""Best-effort PDF text extraction for chat context (uses pypdf).

We only need a snippet (~8k chars) to ground the model in the open paper, so we
read pages until we hit the budget and stop. Failures are non-fatal: chat still
works with metadata + notes if text can't be extracted.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("paper_agent.pdf")

# Lines at the top of an academic PDF that are not the title.
_BOILERPLATE = re.compile(
    r"^(published as|under review|to appear|accepted|preprint|proceedings|"
    r"\d{4}\b|copyright|©|arxiv:|doi:|https?://|www\.|openreview|"
    r"workshop|conference|technical report|journal of|vol\.|page \d|\d+$)",
    re.IGNORECASE,
)
# A line that marks the end of the title block (authors / abstract / emails).
_STOP = re.compile(r"(\babstract\b|@|\buniversity\b|\binstitute\b|\b1\s*introduction\b)",
                   re.IGNORECASE)


def _title_from_text(text: str) -> str | None:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if len(ln) >= 2]
    i = 0
    while i < len(lines) and _BOILERPLATE.search(lines[i]):
        i += 1
    title_parts: list[str] = []
    for ln in lines[i:]:
        if _STOP.search(ln):
            break
        # An author line often has several comma-separated capitalised names or
        # trailing affiliation digits; treat a comma-heavy short line as authors.
        if title_parts and ln.count(",") >= 2:
            break
        title_parts.append(ln)
        if len(" ".join(title_parts)) > 180:
            break
    title = " ".join(title_parts).strip(" .")
    # de-hyphenate words split across line breaks ("self- injection" -> "self-injection")
    title = re.sub(r"(\w)-\s(\w)", r"\1-\2", title)
    if 8 <= len(title) <= 250 and re.search(r"[A-Za-z]", title) and len(title.split()) >= 2:
        return title
    return None


def extract_title(path: Path) -> str | None:
    """Best-effort paper title from a PDF's first page. None if not confident."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if not reader.pages:
            return None
        text = reader.pages[0].extract_text() or ""
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        logger.warning("pdf title extraction failed for %s: %s", path, exc)
        return None
    return _title_from_text(text)


def extract_pages(path: Path, start_page: int = 1, count: int = 5,
                  max_chars: int = 16000) -> dict:
    """Extract text for pages [start_page, start_page+count) (1-indexed). Returns
    {total_pages, start_page, end_page, text, truncated} so an agent can page through a
    paper without a shell. Best-effort; empty text on failure."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        total = len(reader.pages)
        start = max(1, int(start_page))
        end = min(total, start + max(1, int(count)) - 1)
        parts = []
        for i in range(start - 1, end):
            parts.append(reader.pages[i].extract_text() or "")
        text = "\n\n".join(parts)
        truncated = len(text) > max_chars
        return {"total_pages": total, "start_page": start, "end_page": end,
                "text": text[:max_chars], "truncated": truncated}
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.warning("pdf page extraction failed for %s: %s", path, exc)
        return {"total_pages": 0, "start_page": start_page, "end_page": start_page,
                "text": "", "truncated": False}


def extract_text(path: Path, max_chars: int = 8000) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
            if total >= max_chars:
                break
        return "".join(parts)[:max_chars]
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        logger.warning("pdf text extraction failed for %s: %s", path, exc)
        return ""
