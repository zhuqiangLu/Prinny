"""User-customizable color theme + paired light/dark background wallpaper.

The user picks a shipped **preset** (a paired light/dark wallpaper) or uploads their own —
**a separate image per mode** (light + dark), since the two modes show different backgrounds.
The palette is extracted **client-side** (a `<canvas>` samples the pixels — no server image
library, no LLM) from the *light* image (the accent only shows in light mode); only the
resulting hex values + the chosen images/URLs are persisted here.

Rendering (``base.html``): light mode paints ``bg_light_url`` as a fixed cover background,
dark mode paints ``bg_dark_url``; the palette tints surfaces + the accent app-wide in light
mode. Cards/panels stay opaque on top. Each mode independently falls back to the default
"Reading desk" scene when the user hasn't set one.

The default look (no custom theme) IS the "Reading desk" preset. Fully reversible via
:func:`reset_theme`. Stored as flat keys in ``config.toml``; uploaded images live next to the
config as ``hero-light.<ext>`` / ``hero-dark.<ext>`` (served at ``/hero-image/{mode}``).
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import APP_DIR, load_config, save_config

ROLES = ("bg", "surface", "accent", "accent_hover", "accent_fg", "ink")
_PREFIX = "theme_"
_IMG_KEY = {"light": "theme_img_light", "dark": "theme_img_dark"}    # uploaded filenames
_BG_KEY = {"light": "theme_bg_light_url", "dark": "theme_bg_dark_url"}  # preset URLs
_MODES = ("light", "dark")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_OK_EXT = ("png", "jpg", "jpeg", "webp", "gif")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# The default look is the shipped "Reading desk" preset: a paired light/dark wallpaper.
# The accent is Prinny's brand purple (violet-600/700) — it drives the primary buttons,
# send button, and other accent surfaces app-wide.
DEFAULT_THEME = {
    "bg": "#f4f4f4", "surface": "#fcfcfc",
    "accent": "#7c3aed", "accent_hover": "#6d28d9", "accent_fg": "#ffffff", "ink": "#3d1f1f",
    "bg_light_url": "/static/themes/light.png", "bg_dark_url": "/static/themes/dark.png",
}


def _key(role: str) -> str:
    return _PREFIX + role


def hero_path(mode: str) -> Path | None:
    """Path to the uploaded wallpaper for ``mode`` ('light'|'dark'), or ``None``."""
    if mode not in _MODES:
        return None
    name = (load_config().get(_IMG_KEY[mode]) or "").strip()
    if not name:
        return None
    p = APP_DIR / name
    return p if p.exists() else None


def load_theme() -> dict:
    """The active theme. Always active: with nothing customized, returns the default
    "Reading desk" preset (``is_custom=False``); otherwise the user's theme. Each mode's
    ``bg_{light,dark}_url`` resolves to: an uploaded image → a stored preset URL → the default
    scene. Missing palette roles fall back to the default palette."""
    cfg = load_config()
    palette = {}
    for r in ROLES:
        v = (cfg.get(_key(r)) or "").strip()
        if _HEX.match(v):
            palette[r] = v

    imgs = {m: hero_path(m) for m in _MODES}
    bg_stored = {m: (cfg.get(_BG_KEY[m]) or "").strip() for m in _MODES}
    has_custom = (
        ("bg" in palette and "accent" in palette)
        or any(imgs.values())
        or any(bg_stored.values())
    )
    if not has_custom:
        return dict(DEFAULT_THEME, active=True, is_custom=False)

    t = dict(DEFAULT_THEME)
    t.update(palette)
    t["active"] = True
    t["is_custom"] = True
    for m in _MODES:
        p = imgs[m]
        if p:
            t[f"bg_{m}_url"] = f"/hero-image/{m}?v={int(p.stat().st_mtime)}"
        else:
            t[f"bg_{m}_url"] = bg_stored[m] or DEFAULT_THEME[f"bg_{m}_url"]
    return t


def save_theme(palette: dict, *,
               light_image: tuple[bytes, str] | None = None,
               dark_image: tuple[bytes, str] | None = None,
               bg_light_url: str | None = None, bg_dark_url: str | None = None) -> dict:
    """Persist a full theme spec. Each apply fully defines state: the palette (only valid
    ``#rrggbb`` kept, the rest cleared) and, per mode, either an uploaded image or a preset URL
    (whichever is absent is cleared so it falls back to the default scene)."""
    values: dict[str, str] = {}
    for r in ROLES:
        v = (palette.get(r) or "").strip()
        values[_key(r)] = v if _HEX.match(v) else ""

    sources = {"light": (light_image, bg_light_url), "dark": (dark_image, bg_dark_url)}
    for m, (img, url) in sources.items():
        _remove_hero(m)
        if img and img[0]:
            values[_IMG_KEY[m]] = _write_hero(m, img[0], img[1])
            values[_BG_KEY[m]] = ""
        else:
            values[_IMG_KEY[m]] = ""
            values[_BG_KEY[m]] = _safe_url(url or "")
    return save_config(values)


def reset_theme() -> dict:
    """Clear the custom theme + uploaded images, reverting to the default preset."""
    for m in _MODES:
        _remove_hero(m)
    blanks = {_key(r): "" for r in ROLES}
    for m in _MODES:
        blanks[_IMG_KEY[m]] = ""
        blanks[_BG_KEY[m]] = ""
    return save_config(blanks)


def _write_hero(mode: str, data: bytes, ext: str) -> str:
    ext = (ext or "png").lstrip(".").lower()
    if ext == "jpeg":
        ext = "jpg"
    if ext not in _OK_EXT:
        ext = "png"
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"hero-{mode}.{ext}"
    (APP_DIR / fname).write_bytes(data)
    return fname


def _remove_hero(mode: str) -> None:
    for p in APP_DIR.glob(f"hero-{mode}.*"):
        try:
            p.unlink()
        except OSError:  # pragma: no cover - best effort
            pass


def _safe_url(url: str) -> str:
    """Only accept same-origin app/static paths for background URLs (no remote/external)."""
    url = (url or "").strip()
    return url if url.startswith("/static/") or url.startswith("/hero-image") else ""


# --- branding (app name, editable copy, custom nav images) -----------------------------------
_ASSETS = {"logo": "logo", "moon": "icon-moon", "sun": "icon-sun",
           "agents": "icon-agents", "settings": "icon-settings"}
_ASSET_EXTS = ("svg", "png", "webp", "jpg", "jpeg")


def _branding_assets() -> dict:
    """Map of any user-supplied branding images in ``static/branding/`` → cache-busted URL.
    Missing keys fall back to the built-in emoji/SVG in the templates."""
    out: dict[str, str] = {}
    d = STATIC_DIR / "branding"
    if not d.exists():
        return out
    for key, fname in _ASSETS.items():
        for ext in _ASSET_EXTS:
            f = d / f"{fname}.{ext}"
            if f.exists():
                out[key] = f"/static/branding/{fname}.{ext}?v={int(f.stat().st_mtime)}"
                break
    return out


def branding() -> dict:
    """App name, editable workspace copy, and any custom branding images (the ``pa_branding``
    Jinja global)."""
    cfg = load_config()
    return {
        "app_name": (cfg.get("app_name") or "Prinny").strip() or "Prinny",
        "workspace_title": cfg.get("workspace_title") or "Research Workspace",
        "workspace_subtitle": (cfg.get("workspace_subtitle")
                               or "A calm space to read, understand, and connect ideas."),
        "assets": _branding_assets(),
    }
