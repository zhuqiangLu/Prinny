"""Theme engine + landing-page aggregate stats / search."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    """Point the whole app at a tmp ~/.paper-agent and reset module-level paths."""
    monkeypatch.setenv("PAPER_AGENT_HOME", str(tmp_path))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.theme as theme
    importlib.reload(theme)
    import app.library as library
    importlib.reload(library)
    config.ensure_dirs()
    db.init_db()
    return {"config": config, "db": db, "theme": theme, "library": library}


def test_theme_roundtrip_and_reset(app_home):
    theme = app_home["theme"]
    # No custom theme → the default is the "Reading desk" preset (always active).
    d = theme.load_theme()
    assert d["active"] and d["is_custom"] is False
    assert d["bg_light_url"] == theme.DEFAULT_THEME["bg_light_url"]
    theme.save_theme({
        "bg": "#f4f1ec", "surface": "#ffffff", "accent": "#3b6ea5",
        "accent_hover": "#315c8a", "accent_fg": "#ffffff", "ink": "#1f2937",
    }, light_image=(b"\x89PNG\r\n\x1a\nfake", ".png"))
    t = theme.load_theme()
    assert t["active"] and t["is_custom"] and t["accent"] == "#3b6ea5"
    assert theme.hero_path("light") is not None and theme.hero_path("light").read_bytes().startswith(b"\x89PNG")
    theme.reset_theme()
    assert theme.load_theme()["is_custom"] is False          # back to the default preset
    assert theme.hero_path("light") is None and theme.hero_path("dark") is None


def test_theme_rejects_bad_hex_and_needs_bg_and_accent(app_home):
    theme = app_home["theme"]
    theme.save_theme({"bg": "not-a-color", "accent": "#3b6ea5"})
    assert theme.load_theme()["is_custom"] is False          # bad bg dropped → falls back to default
    theme.save_theme({"bg": "#f4f1ec", "accent": "#3b6ea5"})
    assert theme.load_theme()["is_custom"] is True


def test_preset_paired_wallpaper(app_home):
    theme = app_home["theme"]
    theme.save_theme(
        {"bg": "#f4f4f4", "surface": "#fcfcfc", "accent": "#106bc6",
         "accent_hover": "#0d579e", "accent_fg": "#ffffff", "ink": "#3d1f1f"},
        bg_light_url="/static/themes/light.png", bg_dark_url="/static/themes/dark.png")
    t = theme.load_theme()
    assert t["active"] and t["is_custom"]
    assert theme.hero_path("light") is None                  # preset → no uploaded image
    assert t["bg_light_url"] == "/static/themes/light.png"
    assert t["bg_dark_url"] == "/static/themes/dark.png"


def test_background_url_rejects_remote(app_home):
    theme = app_home["theme"]
    theme.save_theme({"bg": "#f4f4f4", "accent": "#106bc6"},
                     bg_light_url="https://evil.example/x.png", bg_dark_url="")
    t = theme.load_theme()
    # Remote URL rejected → falls back to the default light wallpaper (never the evil URL).
    assert t["bg_light_url"] == theme.DEFAULT_THEME["bg_light_url"]


def test_paired_upload_light_and_dark(app_home):
    theme = app_home["theme"]
    theme.save_theme({"bg": "#f4f4f4", "accent": "#106bc6"},
                     light_image=(b"\x89PNGlight", ".png"),
                     dark_image=(b"\x89PNGdark", ".jpg"))
    t = theme.load_theme()
    assert t["bg_light_url"].startswith("/hero-image/light")
    assert t["bg_dark_url"].startswith("/hero-image/dark")
    assert theme.hero_path("dark").read_bytes() == b"\x89PNGdark"


def test_upload_only_dark_keeps_default_light(app_home):
    theme = app_home["theme"]
    theme.save_theme({}, dark_image=(b"\x89PNGdark", ".png"))
    t = theme.load_theme()
    assert t["bg_dark_url"].startswith("/hero-image/dark")
    assert t["bg_light_url"] == theme.DEFAULT_THEME["bg_light_url"]   # skipped mode → default scene


def test_agent_tool_override_read_toggle_write_locked(app_home):
    from app import agents, agentic_chat, organizer
    base = lambda lst: [t.replace("mcp__pa__", "") for t in lst]
    ct = agentic_chat.CHAT_TOOLS
    # a read tool can be disabled and re-enabled
    agents.set_tool_enabled("chat", "search_fragments", False)
    assert "search_fragments" not in base(agents.effective_tools("chat", ct))
    agents.set_tool_enabled("chat", "search_fragments", True)
    assert "search_fragments" in base(agents.effective_tools("chat", ct))
    # a write tool stays locked on (can't be disabled from the UI)
    assert "submit_proposal" in base(organizer._TOOLS)        # sanity: organizer has a write tool
    agents.set_tool_enabled("organizer", "submit_proposal", False)
    assert "submit_proposal" in base(agents.effective_tools("organizer", organizer._TOOLS))
    # unknown tool / wrong agent is ignored (can't grant new powers)
    agents.set_tool_enabled("chat", "submit_proposal", True)
    assert "submit_proposal" not in base(agents.effective_tools("chat", ct))


def test_agent_can_add_read_tool_beyond_defaults(app_home):
    from app import agents, agentic_chat
    base = lambda lst: [t.replace("mcp__pa__", "") for t in lst]
    ct = agentic_chat.CHAT_TOOLS
    extra = "get_chat_history"                                  # a read tool not in chat's defaults
    assert extra not in base(ct) and extra in agents.read_universe()
    agents.set_tool_enabled("chat", extra, True)               # the "+ Add tool" action
    assert extra in base(agents.effective_tools("chat", ct))
    agents.set_tool_enabled("chat", extra, False)
    assert extra not in base(agents.effective_tools("chat", ct))
    # write tools are never in the addable universe
    assert "submit_proposal" not in agents.read_universe()


def test_branding_defaults_and_override(app_home):
    theme = app_home["theme"]
    config = app_home["config"]
    b = theme.branding()
    assert b["app_name"] == "Prinny"
    assert b["workspace_title"] == "Research Workspace"
    config.save_config({"app_name": "MyLab", "workspace_title": "Lab Wiki"})
    b2 = theme.branding()
    assert b2["app_name"] == "MyLab" and b2["workspace_title"] == "Lab Wiki"


def test_workspace_stats_and_search(app_home):
    library = app_home["library"]
    db = app_home["db"]
    library.upsert_collection("ml", "ML", activated=1)
    pid = library.upsert_paper(title="Attention Is All You Need", authors="Vaswani et al.")
    library.add_membership("ml", pid)
    s = library.workspace_stats()
    assert s["papers"] == 1 and s["unread"] >= 0
    hits = library.search("Attention")
    assert any(h["paper_id"] == pid and h["slug"] == "ml" for h in hits)
    assert library.search("   ") == []
