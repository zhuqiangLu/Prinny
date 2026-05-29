"""Collection name → URL slug.

  "Vision-Language Models" → "vision-language-models"

Phase 1 will persist the name↔slug mapping in ``sync_state``; for Phase 0 we
just need a deterministic, reversible-enough rendering for links.
"""

from __future__ import annotations

import re


def slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "collection"
