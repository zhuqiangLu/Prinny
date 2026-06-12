"""Lightweight UI internationalization + agent output-language control.

Global setting only (config key ``language``). English source strings are the
keys; a per-language JSON catalog at ``app/i18n/<lang>.json`` maps them to
translations. A missing key falls back to the English source, so a partial
catalog renders cleanly while it's being filled in — no broken UI, no crashes.

Two concerns live here:
  * ``t(text)`` — translate a UI string (used as the Jinja global ``t``).
  * ``output_directive()`` — a sentence appended to content-generating LLM system
    prompts so the AGENT writes its prose (thesis, beliefs, claims, chat) in the
    chosen language. We do NOT translate the prompt instructions themselves — the
    model follows English instructions and still outputs the target language.

No third-party i18n dependency (no gettext/babel): the catalog is plain JSON and
lookups are a dict.get with English fallback.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

from .config import load_config

_DIR = Path(__file__).parent / "i18n"

# Languages the UI offers. Keep the value as the *native* name (shown in the picker).
SUPPORTED: dict[str, str] = {"en": "English", "zh": "中文"}

_lang_cache: str | None = None


@functools.lru_cache(maxsize=None)
def _catalog(lang: str) -> dict:
    """Load (and cache) the JSON catalog for ``lang``. English has no catalog
    (identity). Missing/invalid file → empty dict (everything falls back to English)."""
    if lang == "en":
        return {}
    try:
        return json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def current_lang() -> str:
    """The active UI/content language (cached; call ``refresh()`` after a settings
    save). Falls back to 'en' for an unknown/blank value."""
    global _lang_cache
    if _lang_cache is None:
        lang = (load_config().get("language") or "en").strip()
        _lang_cache = lang if lang in SUPPORTED else "en"
    return _lang_cache


def refresh() -> None:
    """Drop the cached language (call after the Settings form saves a new value)."""
    global _lang_cache
    _lang_cache = None


def t(text: str, lang: str | None = None) -> str:
    """Translate a UI source string. Unknown keys fall back to ``text`` itself, so
    an untranslated string just renders in English."""
    lang = lang or current_lang()
    if lang == "en":
        return text
    return _catalog(lang).get(text, text)


# Appended to the system prompt of CONTENT-generating LLM calls (not query/JSON-only
# calls). Tells the model to emit human-readable prose in the target language while
# leaving machine-y bits (refs, JSON keys, code, untranslatable proper nouns) intact.
_OUTPUT_DIRECTIVE: dict[str, str] = {
    "zh": ("\n\nOUTPUT LANGUAGE — IMPORTANT: write ALL human-readable prose "
           "(titles, paragraphs, summaries, claims, explanations, chat replies) in "
           "Simplified Chinese (简体中文). Keep these UNCHANGED in their original form: "
           "JSON keys/structure, paper references and ids (e.g. [[ref]], arXiv ids), "
           "code, URLs, and established technical terms or proper nouns that have no "
           "standard Chinese equivalent (you may gloss them in parentheses)."),
}


def output_directive(lang: str | None = None) -> str:
    """The output-language instruction to append to a content-generating system
    prompt. Empty string for English (the default), so call sites can append
    unconditionally."""
    lang = lang or current_lang()
    return _OUTPUT_DIRECTIVE.get(lang, "")
