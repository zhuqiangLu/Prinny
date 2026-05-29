"""Provenance resolver (AGENTIC_PLAN P1).

One function, ``effective_stamp(ref, slug)``, maps any fragment reference to its
effective ``(synth_kind, author_origin)``. This is the single source of truth the
Phase 2 gate consults; it never trusts an agent-supplied stamp.

Reference shape: ``{"type": "highlight"|"paper"|"note"|"thought", "id": ...}``.
``slug`` is required for note/thought (they live per-collection); ignored otherwise.

Stamps:
  highlight -> (seed, human)      a human pointer to the paper's words
  paper     -> (seed, external)   the cited work itself
  note      -> origin human; kind = override else reasoning iff thoughts field present
  thought   -> read from the file's frontmatter (door-stamped on create)
"""
from __future__ import annotations

from . import notes as notes_mod, thoughts as thoughts_mod

SEED, REASONING = "seed", "reasoning"
HUMAN, AGENT, EXTERNAL = "human", "agent", "external"

# Fragment types whose stamp is constant regardless of stored data.
_CONST = {
    "highlight": (SEED, HUMAN),
    "paper": (SEED, EXTERNAL),
}


def effective_stamp(ref: dict, slug: str | None = None) -> tuple[str, str]:
    """Return (synth_kind, author_origin) for a fragment reference.

    Unknown/missing types resolve to (seed, human) — the most-restricted-but-human
    default, so an unrecognized ref can ground nothing it shouldn't.
    """
    rtype = (ref or {}).get("type")
    if rtype in _CONST:
        return _CONST[rtype]
    rid = (ref or {}).get("id")
    if rtype == "note":
        if slug is None or rid is None:
            return (SEED, HUMAN)
        return notes_mod.note_kind(slug, int(rid))
    if rtype == "thought":
        if slug is None or rid is None:
            return (SEED, HUMAN)
        t = thoughts_mod.get_thought(slug, str(rid))
        if not t:
            return (SEED, HUMAN)
        return (t["synth_kind"], t["author_origin"])
    return (SEED, HUMAN)
