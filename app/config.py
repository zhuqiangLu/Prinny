"""Application config + storage layout.

Config lives at ``~/.prinny/config.toml`` (or the legacy ``~/.paper-agent`` if a
previous install created it — see APP_DIR below). We read it with the stdlib
``tomllib`` (3.11+) and write it with a tiny hand-rolled serializer so we don't
need a TOML-writing dependency for the handful of flat keys we store.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path


# ---------------------------------------------------------------------------
# Storage layout (see CLAUDE.md "Storage layout")
# ---------------------------------------------------------------------------
def _migrate_legacy_home() -> None:
    """One-time rename of a pre-rename ``~/.paper-agent`` data dir → ``~/.prinny``,
    rewriting absolute ``.paper-agent`` paths in the moved config.toml (e.g.
    ``pdf_store_path``). Runs only for a default install (no env override), only when
    the new home doesn't exist yet, and never under pytest (so a dev/test run can't move
    real data). Must run BEFORE APP_DIR is resolved so paths point at the new home."""
    import sys
    if ("pytest" in sys.modules or os.environ.get("PRINNY_HOME")
            or os.environ.get("PAPER_AGENT_HOME")):
        return
    new, legacy = Path.home() / ".prinny", Path.home() / ".paper-agent"
    if new.exists() or not legacy.exists():
        return
    import shutil
    try:
        shutil.move(str(legacy), str(new))
    except OSError:
        return
    cfg = new / "config.toml"
    try:
        if cfg.is_file():
            cfg.write_text(
                cfg.read_text(encoding="utf-8").replace("/.paper-agent/", "/.prinny/"),
                encoding="utf-8")
    except OSError:
        pass


def _resolve_home() -> Path:
    """The data home. Env override wins (PRINNY_HOME, then legacy PAPER_AGENT_HOME).
    Otherwise ~/.prinny — after _migrate_legacy_home() has moved any pre-rename
    ~/.paper-agent into place. A leftover ~/.paper-agent (e.g. env-pinned or test) is
    still honored as a fallback so data is never orphaned."""
    env = os.environ.get("PRINNY_HOME") or os.environ.get("PAPER_AGENT_HOME")
    if env:
        return Path(env)
    new, legacy = Path.home() / ".prinny", Path.home() / ".paper-agent"
    return legacy if (legacy.exists() and not new.exists()) else new


_migrate_legacy_home()
APP_DIR = _resolve_home()
CONFIG_PATH = APP_DIR / "config.toml"
DB_PATH = APP_DIR / "app.sqlite"
COLLECTIONS_DIR = APP_DIR / "collections"

DEFAULTS: dict[str, str] = {
    # LLM backend — CLI agents only. engine ∈ {claude-code, codex}; empty => claude-code.
    # No API backend: every LLM feature requires the selected CLI agent installed.
    # model: claude aliases sonnet/opus/haiku; codex uses its own model id (blank = default).
    "engine": "",
    "claude_bin": "claude",
    "codex_bin": "codex",
    # Per-paper chat persistence (PAPER_CHAT_AGENT). resume = spawn --resume per turn
    # (durable, survives restart); live = one persistent process (faster, ephemeral).
    "chat_session_mode": "resume",
    "model": "",
    # Model for spawned BACKGROUND sub-agents (Field-Model draft, 🔬 deep finder,
    # benchmark extraction, topic investigation) — heavy reasoning, so default to opus.
    # Interactive chat uses `model` (the picker) instead. Claude aliases only; on Codex
    # this is ignored (it uses `model`).
    "agent_model": "opus",
    # Per-collection reading log size (powers "Previous paper" walk-back).
    "reading_log_cap": "100",
    "show_highlight_legend": "true",
    # Suggested reading: how many papers the FAST (keyword) search returns per run
    # (the agentic 🔬 Deep search casts a wider net, ~50, regardless of this).
    "recommend_count": "15",
    # Semantic Scholar: a second discovery source + a fallback when arXiv rate-limits.
    # A free API key (https://www.semanticscholar.org/product/api) gives a personal
    # rate limit that works on shared/institutional IPs where the public quota 429s.
    "semantic_scholar_api_key": "",
    "zotero_sqlite_path": str(Path.home() / "Zotero" / "zotero.sqlite"),
    "zotero_api_base": "http://localhost:23119",
    # Local-first store (ADR 0001): the app's own PDF store + Zotero write-back creds.
    "pdf_store_path": str(APP_DIR / "pdfs"),
    "zotero_write_api_base": "http://localhost:23119",
    "zotero_write_api_key": "",
    # Display preferences (stored as "true"/"false" strings).
    "pdf_dark": "true",            # invert the PDF in dark mode (per-paper toggle still wins)
    "debug": "false",              # developer: surface internals (e.g. chat session id)
    # Branding / appearance (editable in Settings → Appearance).
    "app_name": "Prinny",
    "workspace_title": "Research Workspace",
    "workspace_subtitle": "A calm space to read, understand, and connect ideas.",
}


def ensure_dirs() -> None:
    """Create the on-disk layout if it doesn't exist yet."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    COLLECTIONS_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, str]:
    """Return config merged over defaults. Missing file is fine."""
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as f:
            cfg.update({k: str(v) for k, v in tomllib.load(f).items()})
    return cfg


def agent_model() -> str:
    """Model for spawned background sub-agents (Field-Model draft, deep finder,
    benchmark extraction, topic investigation). Defaults to opus. Codex doesn't take
    Claude aliases, so there it falls back to the configured `model`."""
    cfg = load_config()
    if (cfg.get("engine") or "claude-code") == "codex":
        return cfg.get("model", "")
    return (cfg.get("agent_model") or "opus").strip() or "opus"


# Default PDF-highlight scheme (user-editable in Settings). color + short legend label.
DEFAULT_HIGHLIGHT_SCHEME = [
    {"color": "#ffd400", "label": "methodology"},
    {"color": "#6fb3ff", "label": "insight"},
    {"color": "#ff0000", "label": "limitation"},
    {"color": "#00e1ff", "label": "interesting"},
]


def highlight_scheme(cfg: dict | None = None) -> list[dict]:
    """The configured highlight scheme (list of {color,label}), or the default. Sanitized."""
    import json
    cfg = cfg if cfg is not None else load_config()
    try:
        v = json.loads(cfg.get("highlight_scheme") or "")
    except (TypeError, ValueError):
        v = None
    if not isinstance(v, list) or not v:
        return [dict(x) for x in DEFAULT_HIGHLIGHT_SCHEME]
    out = []
    for x in v:
        if isinstance(x, dict) and (x.get("color") or "").strip():
            out.append({"color": x["color"].strip(), "label": (x.get("label") or "").strip()})
    return out or [dict(x) for x in DEFAULT_HIGHLIGHT_SCHEME]


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def save_config(values: dict[str, str]) -> dict[str, str]:
    """Merge ``values`` into existing config and persist. Returns the merged config."""
    ensure_dirs()
    cfg = load_config()
    cfg.update({k: str(v) for k, v in values.items()})
    lines = ["# prinny config — edit via the Settings page or by hand.\n"]
    for key, val in cfg.items():
        lines.append(f'{key} = "{_toml_escape(val)}"\n')
    CONFIG_PATH.write_text("".join(lines), encoding="utf-8")
    return cfg
