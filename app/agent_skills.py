"""Per-sub-agent skills (PAPER_CHAT_AGENT Phase B + per-agent skills).

We ship SKILL.md files in ``app/skills/`` and materialize a *scoped* subset into a stable
per-purpose "home" dir whose ``.claude/skills/`` Claude Code discovers when we run that
sub-agent with the dir as cwd (verified: a project-cwd SKILL.md appears in the session's
skill list). Each home has no CLAUDE.md and is fixed, so resumed sessions key to the same
project. Skills are ADVISORY — they guide the agent; the real boundary (provenance gate +
tool allowlist) stays in code.

Codex does not load ``.claude/skills``; for Codex the same guidance lives in the system
prompt. So these homes only add skills on the Claude path (which is all the writing agents
run on anyway — they're claude-code-only).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .config import APP_DIR

_SRC = Path(__file__).resolve().parent / "skills"

# home name -> the skills it carries. Each sub-agent uses its own scoped home so it only
# sees skills relevant to its job. "paper" keeps the legacy dir name (agent-home) so
# existing resumed paper-chat sessions stay keyed to the same project.
_HOMES: dict[str, list[str]] = {
    "paper": ["summarize-section", "extract-contributions", "compare-to-my-notes",
              "list-assumptions", "locate-figure", "find-evidence-for", "resume-paper-chat"],
    "chat": ["answer-from-collection"],
    # The "wiki" agent is the one-shot cognitive-model drafter: Field Model
    # (field-model) + belief candidates (belief-draft). The old notes-pipeline
    # homes (organizer/debt/brainstorm/lint) were removed with the pipeline.
    "wiki": ["field-model", "belief-draft", "theme-name"],
}


# User-editable overrides live here; when present they shadow the shipped skill. This keeps the
# shipped defaults intact (so "Reset to default" always works) and survives app updates.
_USER = APP_DIR / "skills"


def _shipped_file(name: str) -> Path:
    return _SRC / name / "SKILL.md"


def _override_file(name: str) -> Path:
    return _USER / name / "SKILL.md"


def is_customized(name: str) -> bool:
    return _override_file(name).is_file()


def effective_skill_file(name: str) -> Path:
    """The SKILL.md actually in force: the user override if present, else the shipped one."""
    ov = _override_file(name)
    return ov if ov.is_file() else _shipped_file(name)


def skill_body(name: str) -> str:
    """The in-force instruction body (SKILL.md minus frontmatter). Empty if the skill is missing."""
    f = effective_skill_file(name)
    if not f.is_file():
        return ""
    from . import frontmatter
    _, body = frontmatter.parse(f.read_text(encoding="utf-8"))
    return body.strip()


def read_skill(name: str) -> dict | None:
    """A skill's editable view: {name, description, body, customized}. None if not shipped."""
    if not _shipped_file(name).is_file():
        return None
    from . import frontmatter
    meta, body = frontmatter.parse(effective_skill_file(name).read_text(encoding="utf-8"))
    return {"name": name, "description": (meta.get("description") or "").strip(),
            "body": body.strip(), "customized": is_customized(name)}


def save_skill(name: str, body: str, description: str | None = None) -> None:
    """Save a user override for ``name`` (must be a shipped skill). Preserves the skill's name;
    description defaults to the current one. Takes effect on the next agent run."""
    if not _shipped_file(name).is_file():
        raise ValueError(f"unknown skill '{name}'")
    from . import frontmatter
    cur = read_skill(name) or {}
    desc = cur.get("description", "") if description is None else description.strip()
    ov = _override_file(name)
    ov.parent.mkdir(parents=True, exist_ok=True)
    ov.write_text(frontmatter.dump({"name": name, "description": desc}, (body or "").strip()),
                  encoding="utf-8")


def reset_skill(name: str) -> None:
    """Drop the user override, reverting to the shipped skill."""
    ov = _override_file(name)
    if ov.exists():
        ov.unlink()
    try:
        ov.parent.rmdir()           # tidy the now-empty dir (ignore if not empty)
    except OSError:
        pass


def _home_dir(home: str) -> Path:
    return APP_DIR / ("agent-home" if home == "paper" else f"{home}-home")


def ensure_skills_home(home: str = "paper") -> Path:
    """Materialize ``home``'s scoped skills (override in force) into its dir and return it (use
    as the sub-agent's cwd). Idempotent; cheap (a handful of files)."""
    home_dir = _home_dir(home)
    skills = home_dir / ".claude" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    for name in _HOMES.get(home, []):
        src = _SRC / name
        if src.is_dir():
            shutil.copytree(src, skills / name, dirs_exist_ok=True)
            if is_customized(name):     # overlay the user's edited SKILL.md
                shutil.copy2(_override_file(name), skills / name / "SKILL.md")
    return home_dir


def skill_names(home: str = "paper") -> list[str]:
    """The skills carried by a home that actually ship in app/skills/."""
    return sorted(n for n in _HOMES.get(home, []) if (_SRC / n / "SKILL.md").is_file())
