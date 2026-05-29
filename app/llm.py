"""Thin LLM interface — delegates to the selected Engine (AGENTIC_PLAN P3).

Everything LLM-related goes through ``complete``/``stream`` here; the actual backend
(Claude Code | Codex) is chosen in ``engine.py`` from config. This is the
dependency flip: by default the app runs on a CLI agent with no API key. The public
surface (``complete``, ``stream``, ``list_models``, ``usage``, ``LLMError``) is
unchanged so every existing call site keeps working.

Every call logs latency + token counts (CLAUDE.md). Token tallies are best-effort:
CLI engines report usage when they can, else the tally only counts calls.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

from . import engine as engine_mod
from .config import load_config

logger = logging.getLogger("paper_agent.llm")

# Cumulative usage for this process (the "usage" indicator). Resets on restart.
_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}


def usage() -> dict:
    return dict(_USAGE)


def _record(u: dict | None) -> None:
    _USAGE["calls"] += 1
    if u:
        _USAGE["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
        _USAGE["completion_tokens"] += u.get("completion_tokens", 0) or 0


class LLMError(RuntimeError):
    """Raised when the selected backend is unusable (missing key/binary, bad exit)."""


def _engine():
    return engine_mod.build_engine(load_config())


def engine_status() -> dict:
    """For Settings: the selected engine and whether it's usable right now."""
    eng = _engine()
    ok, detail = eng.available()
    return {"name": eng.name, "ok": ok, "detail": detail}


def complete(messages: list[dict], model: str | None = None) -> str:
    """Single-shot completion via the selected engine. Returns the assistant text."""
    eng = _engine()
    t0 = time.monotonic()
    try:
        res = eng.run_once(messages, model=model)
    except engine_mod.EngineError as exc:
        raise LLMError(str(exc)) from exc
    _record(res.usage)
    logger.info(
        "llm.complete engine=%s latency=%.2fs prompt_tokens=%s completion_tokens=%s",
        eng.name, time.monotonic() - t0,
        (res.usage or {}).get("prompt_tokens", "?"),
        (res.usage or {}).get("completion_tokens", "?"),
    )
    return res.text


def stream(messages: list[dict], model: str | None = None) -> Iterator[str]:
    """Streaming variant: yields text deltas from the selected engine."""
    eng = _engine()
    try:
        yield from eng.stream(messages, model=model)
    except engine_mod.EngineError as exc:
        raise LLMError(str(exc)) from exc


def list_models(force: bool = False) -> list[str]:
    """Models the selected engine can offer (for the Settings picker). [] on failure
    — callers fall back to a free-text model field."""
    try:
        return _engine().models(force=force)
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_models failed: %s", exc)
        return []
