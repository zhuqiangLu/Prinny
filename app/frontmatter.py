"""Minimal YAML-frontmatter read/write.

We deliberately avoid a PyYAML dependency (not in the locked list). Our
frontmatter only ever uses scalars and flat string lists, e.g.:

    ---
    type: problem
    title: Efficiency
    sources: [ABCD1234, WXYZ5678]
    derived_from_thoughts: [2026-05-01T14-00-00]
    last_regen: 2026-05-22T10:00:00
    ---
    body markdown...

so a tiny parser/dumper is enough and keeps round-trips predictable.
"""

from __future__ import annotations


def _parse_scalar(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
    return v.strip('"').strip("'")


def parse(text: str) -> tuple[dict, str]:
    """Return (metadata, body). No frontmatter → ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    i = 1
    pending_key: str | None = None
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            body = "\n".join(lines[i + 1 :])
            return meta, body.lstrip("\n")
        if pending_key and line.lstrip().startswith("- "):
            meta.setdefault(pending_key, [])
            meta[pending_key].append(line.lstrip()[2:].strip())
        elif ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            if val.strip() == "":
                meta[key] = []
                pending_key = key
            else:
                meta[key] = _parse_scalar(val)
                pending_key = None
        i += 1
    return meta, ""  # no closing fence


def _dump_value(v) -> str:
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(str(x) for x in v) + "]"
    return str(v)


def dump(meta: dict, body: str) -> str:
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {_dump_value(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"
