"""Timestamped thought stream (CLAUDE.md Phase 4).

Each thought is a markdown file under ``collections/<slug>/thoughts/`` named by an
ISO timestamp. Superseded/consolidated originals move to ``thoughts-archive/``
(never deleted there). Consolidation asks the LLM to synthesize a date range into
one new entry — but only the user accepting it writes anything.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import frontmatter, llm
from .config import COLLECTIONS_DIR

# Typed-capture stamps (AGENTIC_PLAN P1). Reads default missing frontmatter to
# (seed, human) so thoughts written before this phase migrate non-destructively.
SYNTH_KINDS = ("seed", "reasoning")
ORIGINS = ("human", "agent", "external")


def _norm_kind(v: str | None) -> str:
    return v if v in SYNTH_KINDS else "seed"


def _norm_origin(v: str | None) -> str:
    return v if v in ORIGINS else "human"


def _dir(slug: str) -> Path:
    return COLLECTIONS_DIR / slug / "thoughts"


def _archive_dir(slug: str) -> Path:
    return COLLECTIONS_DIR / slug / "thoughts-archive"


def _new_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def _path(slug: str, tid: str) -> Path:
    return _dir(slug) / f"{tid}.md"


def list_thoughts(slug: str, paper_key: str | None = None) -> list[dict]:
    """All thoughts (newest first). If ``paper_key`` is given, return only the thoughts
    anchored to that paper — the per-paper view; None returns the whole stream."""
    d = _dir(slug)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.md"), reverse=True):
        meta, body = frontmatter.parse(f.read_text(encoding="utf-8"))
        pk = str(meta.get("paper_key") or "")
        if paper_key is not None and pk != str(paper_key):
            continue
        out.append(
            {
                "id": f.stem,
                "created": meta.get("created", f.stem),
                "tags": meta.get("tags", []) or [],
                "synth_kind": _norm_kind(meta.get("synth_kind")),
                "author_origin": _norm_origin(meta.get("author_origin")),
                "prompted_by": meta.get("prompted_by", ""),
                "paper_key": pk,
                "body": body.strip(),
            }
        )
    return out


def get_thought(slug: str, tid: str) -> dict | None:
    p = _path(slug, tid)
    if not p.exists():
        return None
    meta, body = frontmatter.parse(p.read_text(encoding="utf-8"))
    return {
        "id": tid,
        "created": meta.get("created", tid),
        "tags": meta.get("tags", []) or [],
        "synth_kind": _norm_kind(meta.get("synth_kind")),
        "author_origin": _norm_origin(meta.get("author_origin")),
        "prompted_by": meta.get("prompted_by", ""),
        "body": body.strip(),
    }


def create_thought(
    slug: str,
    text: str,
    tags: list[str] | None = None,
    synth_kind: str = "seed",
    author_origin: str = "human",
    prompted_by: str | None = None,
    paper_key: str | None = None,
) -> str:
    """Create a thought, stamping (synth_kind, author_origin) by the creating door.

    ``author_origin`` is the door's identity — callers pass a constant, never user
    input. The agentic-chat capture door (Phase 6) is the only one that stamps 'agent'.
    ``prompted_by`` links a human 'your take' to the agent seed that prompted it.
    ``paper_key`` anchors the thought to a paper (it still rolls up into the collection
    stream); None/empty = a collection-level thought.
    """
    d = _dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    tid = _new_id()
    # avoid same-second collisions
    while _path(slug, tid).exists():
        tid = tid + "-x"
    meta = {"created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "tags": tags or [],
            "synth_kind": _norm_kind(synth_kind),
            "author_origin": _norm_origin(author_origin)}
    if prompted_by:
        meta["prompted_by"] = prompted_by
    if paper_key:
        meta["paper_key"] = str(paper_key)
    _path(slug, tid).write_text(frontmatter.dump(meta, text), encoding="utf-8")
    return tid


def update_thought(slug: str, tid: str, text: str, synth_kind: str | None = None) -> bool:
    """Edit a thought's body (and optionally its synth_kind). author_origin is
    preserved — editing never relaunders an agent thought into a human one."""
    p = _path(slug, tid)
    if not p.exists():
        return False
    meta, _ = frontmatter.parse(p.read_text(encoding="utf-8"))
    meta["author_origin"] = _norm_origin(meta.get("author_origin"))
    meta["synth_kind"] = _norm_kind(synth_kind if synth_kind is not None else meta.get("synth_kind"))
    p.write_text(frontmatter.dump(meta, text), encoding="utf-8")
    return True


def delete_thought(slug: str, tid: str) -> bool:
    p = _path(slug, tid)
    if not p.exists():
        return False
    p.unlink()
    return True


def supersede_thought(slug: str, tid: str) -> bool:
    """Move a thought to the archive (not deleted)."""
    p = _path(slug, tid)
    if not p.exists():
        return False
    adir = _archive_dir(slug)
    adir.mkdir(parents=True, exist_ok=True)
    p.rename(adir / p.name)
    return True


def propose_consolidation(slug: str, ids: list[str]) -> str:
    """Ask the LLM to synthesize the given thoughts. Returns proposed markdown.

    Does NOT write anything — the caller shows it for the user to accept/edit.
    """
    entries = []
    for tid in ids:
        t = get_thought(slug, tid)
        if t:
            entries.append(f"[{t['created']}]\n{t['body']}")
    if not entries:
        return ""
    joined = "\n\n---\n\n".join(entries)
    messages = [
        {
            "role": "system",
            "content": (
                "You consolidate a researcher's own rough thoughts into one "
                "coherent note, preserving their voice and claims. Do not add new "
                "ideas or external facts; only synthesize what is present. Output "
                "markdown only."
            ),
        },
        {"role": "user", "content": f"Consolidate these thoughts:\n\n{joined}"},
    ]
    return llm.complete(messages)


def accept_consolidation(slug: str, ids: list[str], text: str) -> str:
    """Archive the originals and write the consolidated entry (tagged)."""
    new_id = create_thought(slug, text, tags=["consolidated"])
    for tid in ids:
        supersede_thought(slug, tid)
    return new_id
