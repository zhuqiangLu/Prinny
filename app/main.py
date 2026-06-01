"""FastAPI app — Phase 0 skeleton.

Routes:
  GET  /          — list Zotero collections (HTMX-rendered)
  GET  /healthz   — liveness + which Zotero backend is in use
  GET  /settings  — settings form
  POST /settings  — persist settings to config.toml
  GET  /c/{slug}  — collection landing (Phase 1 stub for now)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    agent_skills,
    agentic_chat,
    agents as agents_mod,
    annotations as ann_mod,
    context,
    discover,
    export_dir as export_mod,
    frontmatter,
    library,
    live_session,
    llm,
    notes as notes_mod,
    paper_chat,
    pdf_store,
    sync as sync_mod,
    theme as theme_mod,
    thoughts as thoughts_mod,
    topics as topics_mod,
    triage as triage_mod,
    wiki,
)
from .config import highlight_scheme as config_highlight_scheme, load_config, save_config
from .db import init_db
from .markdown import render as render_md
from .repo import (
    add_message,
    clear_messages,
    delete_thread,
    get_artifact,
    get_messages,
    get_or_create_thread,
    get_session_id,
    list_threads,
    new_thread,
    thread_belongs,
    thread_message_count,
    touch_thread,
)
from .slugs import slugify
from .zotero import get_zotero

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Render-time global so every page (via base.html) can apply the active color theme
# without each route having to pass it in. Evaluated per render.
templates.env.globals["pa_theme"] = theme_mod.load_theme
templates.env.globals["pa_branding"] = theme_mod.branding

app = FastAPI(title="Paper Collection Wiki Agent")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.on_event("shutdown")
def _shutdown() -> None:
    live_session.shutdown_all()   # don't orphan persistent chat processes


@app.get("/healthz")
def healthz() -> dict[str, str]:
    z = get_zotero()
    try:
        source = z.source()
    except Exception as exc:  # pragma: no cover - defensive
        source = f"error: {exc}"
    return {"status": "ok", "zotero": source}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    # Local-first: the landing page reads the app's own collections (works with
    # Zotero closed). We also offer Zotero collections not yet activated, but that
    # read is best-effort and degrades gracefully if Zotero is unreachable.
    collections = library.list_collections(with_activity=True)
    available = []
    source = error = None
    try:
        z = get_zotero()
        source = _safe_source(z)
        # Offer ALL Zotero collections — importing the same one again makes an independent
        # local collection, so we no longer hide ones already imported.
        for c in z.list_collections():
            available.append({"name": c.name, "slug": slugify(c.name)})
        available.sort(key=lambda c: c["name"].lower())
    except Exception as exc:  # noqa: BLE001 - Zotero optional on the landing page
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "collections": collections,
            "available": available,
            "existing_names": [c["name"].lower() for c in collections],
            "source": source,
            "error": error,
            "stats": library.workspace_stats(),
        },
    )


@app.get("/hero-image/{mode}")
def hero_image(mode: str) -> FileResponse:
    """Stream the user's uploaded wallpaper for a mode ('light'|'dark')."""
    p = theme_mod.hero_path(mode)
    if not p:
        raise HTTPException(status_code=404, detail="No hero image")
    return FileResponse(str(p))


# ============================ Research Topics (v1) ============================
# Cross-collection investigation threads. Topic = what I'm investigating
# (references collections + entities; owns no papers/notes/wiki).

def _topic_sources(t: dict) -> list[dict]:
    """Resolve a topic's linked collection slugs to {slug,name,n_papers,missing}."""
    by_slug = {c["slug"]: c for c in library.list_collections(with_activity=True)}
    out = []
    for cs in t["collections"]:
        c = by_slug.get(cs)
        out.append({"slug": cs, "name": c["name"] if c else cs,
                    "n_papers": (c.get("paper_count") if c else None), "missing": c is None})
    return out


@app.get("/topics", response_class=HTMLResponse)
def topics_index(request: Request, error: str = "") -> HTMLResponse:
    return templates.TemplateResponse(request, "topics.html", {
        "topics": topics_mod.list_topics(),
        "collections": library.list_collections(with_activity=True),
        "error": error,
    })


@app.post("/topics")
def topic_create(title: str = Form(""), question: str = Form(""),
                 collections: list[str] = Form([])) -> RedirectResponse:
    try:
        slug = topics_mod.create_topic(title, question, collections)
    except ValueError:
        return RedirectResponse("/topics?error=A+research+topic+needs+a+question.",
                                status_code=303)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.get("/t/{slug}", response_class=HTMLResponse)
def topic_page(request: Request, slug: str) -> HTMLResponse:
    t = topics_mod.get_topic(slug)
    if not t:
        raise HTTPException(status_code=404, detail="Research topic not found")
    return templates.TemplateResponse(request, "topic.html", {
        "t": t, "sources": _topic_sources(t),
        "all_collections": library.list_collections(with_activity=True),
    })


@app.post("/t/{slug}/status")
def topic_status(slug: str, status: str = Form("")) -> RedirectResponse:
    topics_mod.set_status(slug, status)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/collections")
def topic_collections(slug: str, collections: list[str] = Form([])) -> RedirectResponse:
    topics_mod.set_collections(slug, collections)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/delete")
def topic_delete(slug: str) -> RedirectResponse:
    topics_mod.delete_topic(slug)
    return RedirectResponse("/topics", status_code=303)


@app.post("/t/{slug}/hypotheses")
def topic_add_hypothesis(slug: str, text: str = Form("")) -> RedirectResponse:
    topics_mod.add_hypothesis(slug, text)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/hypotheses/{hid}/delete")
def topic_del_hypothesis(slug: str, hid: int) -> RedirectResponse:
    topics_mod.delete_hypothesis(slug, hid)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/questions")
def topic_add_question(slug: str, text: str = Form("")) -> RedirectResponse:
    topics_mod.add_question(slug, text, "user")
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/questions/{qid}/delete")
def topic_del_question(slug: str, qid: int) -> RedirectResponse:
    topics_mod.delete_question(slug, qid)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.get("/search-index")
def search_index() -> list[dict]:
    """The flat search index (papers/notes/thoughts/wiki/chat) the landing page fuzzy-searches
    client-side with Fuse.js."""
    return library.search_index()


async def _read_upload(f: UploadFile | None) -> tuple[bytes, str] | None:
    if f is not None and f.filename:
        return await f.read(), Path(f.filename).suffix
    return None


@app.post("/theme")
async def theme_apply(
    bg: str = Form(""), surface: str = Form(""), accent: str = Form(""),
    accent_hover: str = Form(""), accent_fg: str = Form(""), ink: str = Form(""),
    bg_light_url: str = Form(""), bg_dark_url: str = Form(""),
    image_light: UploadFile | None = File(None),
    image_dark: UploadFile | None = File(None),
) -> RedirectResponse:
    """Persist a client-extracted palette + a paired light/dark wallpaper — each mode either an
    uploaded image or a preset URL — then reload the page."""
    palette = {"bg": bg, "surface": surface, "accent": accent,
               "accent_hover": accent_hover, "accent_fg": accent_fg, "ink": ink}
    theme_mod.save_theme(
        palette,
        light_image=await _read_upload(image_light),
        dark_image=await _read_upload(image_dark),
        bg_light_url=bg_light_url, bg_dark_url=bg_dark_url)
    return RedirectResponse("/", status_code=303)


@app.post("/theme/reset")
def theme_reset() -> RedirectResponse:
    """Clear the custom theme + hero image, reverting to the default look."""
    theme_mod.reset_theme()
    return RedirectResponse("/", status_code=303)


@app.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request) -> HTMLResponse:
    """The Agents page: each sub-agent's job, editable skills, and read-only tools/permissions."""
    return templates.TemplateResponse(
        request, "agents.html",
        {"agents": agents_mod.list_agents(), "all_tools": agents_mod.all_mcp_tools()})


@app.get("/agents/skill/{name}/edit", response_class=HTMLResponse)
def agent_skill_edit(request: Request, name: str) -> HTMLResponse:
    """The skill editor fragment, loaded into the Agents skill modal."""
    skill = agent_skills.read_skill(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"No skill '{name}'")
    return templates.TemplateResponse(request, "_skill_editor.html", {"skill": skill})


@app.post("/agents/skill/{name}", response_class=HTMLResponse)
def agent_skill_save(request: Request, name: str,
                     body: str = Form(""), description: str = Form("")) -> HTMLResponse:
    """Save a user override for a skill; returns the re-rendered editor."""
    try:
        agent_skills.save_skill(name, body, description)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"No skill '{name}'")
    return templates.TemplateResponse(
        request, "_skill_editor.html", {"skill": agent_skills.read_skill(name), "saved": True})


@app.post("/agents/skill/{name}/reset", response_class=HTMLResponse)
def agent_skill_reset(request: Request, name: str) -> HTMLResponse:
    """Revert a skill to its shipped default; returns the re-rendered editor."""
    agent_skills.reset_skill(name)
    skill = agent_skills.read_skill(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"No skill '{name}'")
    return templates.TemplateResponse(request, "_skill_editor.html", {"skill": skill, "saved": False})


@app.post("/agents/{key}/tool")
def agent_tool_toggle(key: str, tool: str = Form(...), enabled: str = Form("")) -> Response:
    """Toggle a READ tool for a sub-agent (write tools are locked and silently ignored)."""
    agents_mod.set_tool_enabled(key, tool, enabled in ("true", "on", "1"))
    return Response(status_code=204)


@app.post("/agents/{key}/tools/reset")
def agent_tools_reset(key: str) -> Response:
    """Reset a sub-agent's MCP tools to the code-defined defaults."""
    agents_mod.reset_tools(key)
    return Response(status_code=204)


@app.post("/agents/{key}/skills/reset")
def agent_skills_reset(key: str) -> Response:
    """Reset all of a sub-agent's skills to their shipped defaults."""
    agents_mod.reset_skills(key)
    return Response(status_code=204)


def _settings_ctx(*, saved: bool = False, refresh: bool = False,
                  initial_cat: str = "engine", scheme=None) -> dict:
    """Shared context for the settings form (also feeds the embedded Agents category)."""
    return {
        "config": load_config(), "saved": saved,
        "models": llm.list_models(force=refresh), "status": llm.engine_status(),
        "highlight_scheme": scheme if scheme is not None else config_highlight_scheme(),
        "agents": agents_mod.list_agents(), "all_tools": agents_mod.all_mcp_tools(),
        "initial_cat": initial_cat,
    }


@app.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request, refresh: bool = False, cat: str = "engine") -> HTMLResponse:
    return templates.TemplateResponse(
        request, "settings.html", _settings_ctx(refresh=refresh, initial_cat=cat))


@app.get("/settings/form", response_class=HTMLResponse)
def settings_form(request: Request, refresh: bool = False, cat: str = "engine") -> HTMLResponse:
    """Just the settings form fragment — loaded into the nav modal (and used by the
    'refresh list' button). Fetches the model list lazily, only when opened."""
    return templates.TemplateResponse(
        request, "_settings_form.html", _settings_ctx(refresh=refresh, initial_cat=cat))


def _parse_highlight_scheme(raw: str) -> list[dict]:
    """Validate a submitted highlight scheme (JSON list of {color,label}); fall back to
    the default if empty/invalid."""
    from .config import DEFAULT_HIGHLIGHT_SCHEME
    try:
        v = json.loads(raw or "")
    except (TypeError, ValueError):
        v = None
    out = []
    if isinstance(v, list):
        for x in v:
            if isinstance(x, dict) and (x.get("color") or "").strip():
                out.append({"color": x["color"].strip(), "label": (x.get("label") or "").strip()})
    return out or [dict(x) for x in DEFAULT_HIGHLIGHT_SCHEME]


@app.post("/settings", response_class=HTMLResponse)
def settings_post(
    request: Request,
    engine: str = Form(""),
    claude_bin: str = Form("claude"),
    codex_bin: str = Form("codex"),
    chat_session_mode: str = Form("resume"),
    model: str = Form(""),
    reading_log_cap: str = Form("100"),
    recommend_count: str = Form("10"),
    zotero_sqlite_path: str = Form(""),
    zotero_api_base: str = Form(""),
    pdf_store_path: str = Form(""),
    pdf_dark: str = Form(""),
    debug: str = Form(""),
    show_highlight_legend: str = Form(""),
    highlight_scheme: str = Form(""),
    app_name: str = Form("Prinny"),
    workspace_title: str = Form("Research Workspace"),
    workspace_subtitle: str = Form("A calm space to read, understand, and connect ideas."),
) -> HTMLResponse:
    # Normalize the highlight scheme; remap existing highlights only if the colors changed.
    old_colors = [c["color"] for c in config_highlight_scheme()]
    scheme = _parse_highlight_scheme(highlight_scheme)
    new_colors = [c["color"] for c in scheme]
    cfg = save_config(
        {
            "engine": engine,
            "claude_bin": claude_bin,
            "codex_bin": codex_bin,
            "chat_session_mode": chat_session_mode if chat_session_mode in ("resume", "live") else "resume",
            "model": model,
            "reading_log_cap": str(int(reading_log_cap)) if reading_log_cap.strip().isdigit() else "100",
            "recommend_count": str(min(50, max(1, int(recommend_count)))) if recommend_count.strip().isdigit() else "10",
            "zotero_sqlite_path": zotero_sqlite_path,
            "zotero_api_base": zotero_api_base,
            "pdf_store_path": pdf_store_path,
            "pdf_dark": "true" if pdf_dark else "false",
            "debug": "true" if debug else "false",
            "show_highlight_legend": "true" if show_highlight_legend else "false",
            "highlight_scheme": json.dumps(scheme),
            "app_name": app_name.strip() or "Prinny",
            "workspace_title": workspace_title.strip() or "Research Workspace",
            "workspace_subtitle": workspace_subtitle.strip()
            or "A calm space to read, understand, and connect ideas.",
        }
    )
    if set(new_colors) != set(old_colors):
        ann_mod.remap_to_scheme(new_colors)
    # HTMX submit (page or modal) swaps just the form wrapper; a plain POST gets the page.
    tmpl = "_settings_form.html" if request.headers.get("HX-Request") else "settings.html"
    return templates.TemplateResponse(request, tmpl, _settings_ctx(saved=True, scheme=scheme))


_MAX_CHAT_IMAGES = 4


def _parse_images(images_json: str) -> list[str]:
    """Validate a client-supplied JSON array of base64 image data URLs. We only let
    through `data:image/...;base64,` URLs and cap the count to keep payloads sane."""
    if not images_json:
        return []
    try:
        arr = json.loads(images_json)
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for u in arr if isinstance(arr, list) else []:
        if isinstance(u, str) and u.startswith("data:image/") and ";base64," in u:
            out.append(u)
        if len(out) >= _MAX_CHAT_IMAGES:
            break
    return out


@app.post("/c/{slug}/render")
def render_markdown(slug: str, text: str = Form("")) -> dict:
    """Render markdown→HTML (note Preview). Same renderer as chat, so math/wikilinks match."""
    return {"html": render_md(text, slug)}


@app.get("/models.json")
def models_json(refresh: bool = False) -> dict:
    """Filtered chat-model list (cached) + the current model. Empty list on failure
    so the UI falls back to a text field."""
    cfg = load_config()
    return {"models": llm.list_models(force=refresh), "current": cfg.get("model", "")}


@app.post("/model")
def set_model(model: str = Form(...)) -> dict:
    """Quick model switch (chat-header selector). Persists to config; affects all chats."""
    model = model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Empty model")
    save_config({"model": model})
    return {"ok": True, "model": model}


def _rendered_history(slug: str, paper_id: int | None = None) -> list[dict]:
    """Thread messages (user+assistant) rendered to HTML for display.

    paper_id None -> collection thread; set -> that paper's own thread.
    """
    thread_id = get_or_create_thread(slug, paper_id)
    out = []
    for m in get_messages(thread_id):
        if m["role"] in ("user", "assistant"):
            out.append({"role": m["role"], "html": render_md(m["content"], slug)})
    return out


def _collection_source(col: dict | None) -> dict:
    """UI descriptor for where a collection imports from / syncs to. Today every
    collection is Zotero-backed (you can only refresh once it's linked to a Zotero
    collection). Structured so future importers (local folders, etc.) can return their
    own label/verbs from one place — the Refresh/Sync buttons read this, not "Zotero".
    """
    return {"name": "Zotero", "refreshable": bool(col and col.get("zotero_collection_id"))}


@app.get("/c/{slug}", response_class=HTMLResponse)
def collection_page(request: Request, slug: str) -> HTMLResponse:
    col = _require_collection(slug)
    papers = library.list_papers(slug)
    others = [c for c in library.list_collections() if c["slug"] != slug]
    artifact = get_artifact(get_or_create_thread(slug, None))
    return templates.TemplateResponse(
        request,
        "collection.html",
        {
            "slug": slug,
            "name": col["name"],
            "col": col,
            "source": _collection_source(col),
            "export_dir_default": str(export_mod.default_dir(slug)),
            "papers": papers,
            "others": others,
            "triage_count": len(triage_mod.list_triage(slug)),
            "dup_count": len(library.find_duplicate_groups(slug)),
            "read_map": {str(p["id"]): p["read"] for p in papers},
            "graveyard_count": library.graveyard_count(slug),
            "messages": _rendered_history(slug),
            "artifact_html": render_md(artifact, slug) if artifact else "",
            "paper_key": "",
            "model": load_config().get("model", ""),
        },
    )


# --- collection management (local-first: activate / refresh / summary / sync) ---
def _require_unique_name(name: str) -> str:
    """Reject a blank or already-used collection name (the client warns first; this is the
    server-side backstop). Returns the trimmed name."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    if library.name_taken(name):
        raise HTTPException(status_code=409,
                            detail=f"A collection named “{name}” already exists. Pick a different name.")
    return name


@app.post("/collections/new")
def collections_new(name: str = Form(...), purpose: str = Form(""), summary: str = Form("")) -> RedirectResponse:
    name = _require_unique_name(name)
    slug = library.create_local_collection(name, purpose=purpose.strip(), summary=summary.strip())
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.post("/c/{slug}/tags")
def collection_tags(slug: str, tags: str = Form("[]")) -> dict:
    """Replace a collection's custom tags. ``tags`` is a JSON list of {label, color}."""
    _require_collection(slug)
    try:
        parsed = json.loads(tags)
    except (json.JSONDecodeError, TypeError):
        parsed = []
    return {"tags": library.set_tags(slug, parsed)}


@app.post("/c/{slug}/summary")
def collection_summary(slug: str, summary: str = Form("")) -> RedirectResponse:
    _require_collection(slug)
    library.set_summary(slug, summary.strip())
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.post("/c/{slug}/delete")
def collection_delete(slug: str) -> RedirectResponse:
    _require_collection(slug)
    library.delete_collection(slug)
    return RedirectResponse("/", status_code=303)


@app.post("/c/{slug}/activate")
def collection_activate(slug: str, copy_mode: str = Form("eager")) -> RedirectResponse:
    z = get_zotero()
    new_slug = library.activate(z, slug, "lazy" if copy_mode == "lazy" else "eager")
    return RedirectResponse(f"/c/{new_slug}", status_code=303)


@app.post("/collections/import")
def collections_import(
    slugs: list[str] = Form(default=[]), copy_mode: str = Form("eager")
) -> RedirectResponse:
    """Bulk-activate one or more Zotero collections (legacy multi-select)."""
    mode = "lazy" if copy_mode == "lazy" else "eager"
    z = get_zotero()
    last = None
    for slug in slugs:
        last = library.activate(z, slug, mode)
    return RedirectResponse(f"/c/{last}" if last and len(slugs) == 1 else "/", status_code=303)


@app.get("/collections/zotero-preview/{slug}")
def zotero_preview(slug: str) -> dict:
    """Papers in a Zotero collection (key/title/authors) for the import wizard preview."""
    try:
        z = get_zotero()
        col = z.resolve_collection_id(slug)
        if col is None:
            return {"error": "Collection not found in Zotero."}
        papers = [{"key": p.key, "title": p.title, "authors": p.authors}
                  for p in z.list_papers(col.id)]
        return {"name": col.name, "count": len(papers), "papers": papers}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Couldn't read Zotero: {exc}"}


@app.post("/collections/import-zotero")
def import_zotero_one(
    slug: str = Form(...), name: str = Form(""), tags: str = Form("[]"),
    copy_mode: str = Form("eager"), paper_keys: list[str] = Form(default=[]),
    draft_wiki: str = Form(""),
) -> RedirectResponse:
    """Import ONE Zotero collection as a new linked collection with a chosen name + tags.
    ``paper_keys`` (default: all) selects which papers to bring in; ``draft_wiki`` seeds a
    starter wiki from the abstracts."""
    if name.strip():
        _require_unique_name(name)
    z = get_zotero()
    new_slug = library.activate_async(
        z, slug, "lazy" if copy_mode == "lazy" else "eager", name=name,
        only_keys=paper_keys or None, draft_wiki=bool(draft_wiki),
    )
    try:
        library.set_tags(new_slug, json.loads(tags or "[]"))
    except (json.JSONDecodeError, TypeError):
        pass
    return RedirectResponse("/", status_code=303)   # card shows a 'parsing' state until done


@app.get("/collections/dir-browse")
def dir_browse(path: str = "") -> dict:
    """One level of a folder (subfolders + PDFs) for the directory-import explorer. Empty
    path starts at the home directory."""
    try:
        return library.browse_directory(path.strip() or None)
    except (ValueError, OSError) as exc:
        return {"error": str(exc)}


@app.post("/collections/dir-preview")
def dir_preview(path: str = Form("")) -> dict:
    """Preview the PDFs in a local folder (filenames only) for the import wizard."""
    try:
        return library.scan_directory_pdfs(path.strip())
    except ValueError as exc:
        return {"error": str(exc)}


@app.post("/collections/dir-import")
def dir_import(
    name: str = Form(""), path: str = Form(""), tags: str = Form("[]"),
    files: list[str] = Form(default=[]), draft_wiki: str = Form(""),
) -> RedirectResponse:
    """Import a local folder of PDFs as a new local collection. ``files`` (default: all)
    selects which PDFs; ``draft_wiki`` seeds a starter wiki from the abstracts."""
    if name.strip():
        _require_unique_name(name)
    try:
        parsed = json.loads(tags or "[]")
    except (json.JSONDecodeError, TypeError):
        parsed = []
    library.import_directory_async(
        name, path.strip(), parsed if isinstance(parsed, list) else [],
        only_files=files or None, draft_wiki=bool(draft_wiki),
    )
    return RedirectResponse("/", status_code=303)   # card shows a 'parsing' state until done


@app.get("/collections/{slug}/import-status")
def import_status(slug: str) -> dict:
    """Poll target for an importing collection's card: {state: running|done|error|idle}."""
    return {"state": library.import_state(slug) or "idle"}


@app.post("/c/{slug}/rename")
def collection_rename(slug: str, name: str = Form("")) -> dict:
    """Inline rename (display name only; slug stays). Rejects empty/duplicate names."""
    _require_collection(slug)
    ok, msg = library.rename_collection(slug, name)
    return {"ok": ok, "name": msg} if ok else {"ok": False, "error": msg}


@app.post("/c/{slug}/duplicate")
def collection_duplicate(slug: str) -> RedirectResponse:
    """Clone a collection into a new independent one and open it."""
    _require_collection(slug)
    new_slug = library.duplicate_collection(slug)
    return RedirectResponse(f"/c/{new_slug or slug}", status_code=303)


@app.post("/c/{slug}/refresh")
def collection_refresh(slug: str) -> RedirectResponse:
    _require_collection(slug)
    library.refresh(get_zotero(), slug)
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.post("/c/{slug}/download-all")
def collection_download_all(slug: str) -> RedirectResponse:
    _require_collection(slug)
    library.download_all(get_zotero(), slug)
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.post("/c/{slug}/papers/parse")
def papers_parse(slug: str, urls: str = Form("")) -> dict:
    """Parse a chunk of arXiv/OpenReview URLs (newline/comma separated) into entries with
    fetched metadata, for the Add-paper wizard. Flags ones already in this collection."""
    _require_collection(slug)
    entries = discover.parse_add_input(urls)
    have_arxiv = {p["arxiv_id"] for p in library.list_papers(slug) if p.get("arxiv_id")}
    have_or = {p.get("openreview_id") for p in library.list_papers(slug) if p.get("openreview_id")}
    for e in entries:
        e["dup"] = (e["kind"] == "arxiv" and e["id"] in have_arxiv) or \
                   (e["kind"] == "openreview" and e["id"] in have_or)
        # If it's currently removed (graveyard/permanently-deleted), adding restores it.
        e["removed"] = None if e["dup"] or not e.get("ok") else library.removal_tier(
            slug, arxiv_id=e["id"] if e["kind"] == "arxiv" else None,
            openreview_id=e["id"] if e["kind"] == "openreview" else None)
    return {"entries": entries}


@app.post("/c/{slug}/papers/add")
def papers_add(slug: str, entries: str = Form("[]")) -> RedirectResponse:
    """Add the selected parsed entries (JSON list) and start background PDF downloads."""
    _require_collection(slug)
    try:
        parsed = json.loads(entries or "[]")
    except (json.JSONDecodeError, TypeError):
        parsed = []
    if isinstance(parsed, list):
        triage_mod.add_entries(slug, parsed)
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.get("/c/{slug}/p/{paper_id}/pdf-status", response_class=HTMLResponse)
def pdf_status(request: Request, slug: str, paper_id: int) -> HTMLResponse:
    """Poll fragment: re-renders the whole paper row so its download ring, Preview button and
    (non-)clickable title all reflect the current state. Stops polling once the PDF lands."""
    _require_collection(slug)
    paper = library.get_collection_paper(slug, paper_id)
    if paper is None:
        return HTMLResponse("")            # dropped while polling -> remove the row
    return templates.TemplateResponse(
        request, "_paper_row.html", {"slug": slug, "p": paper},
    )


@app.post("/c/{slug}/p/{paper_id}/retry-download", response_class=HTMLResponse)
def retry_download(request: Request, slug: str, paper_id: int) -> HTMLResponse:
    """Retry a failed PDF download; returns the refreshed row."""
    _require_collection(slug)
    pdf_store.start_download(paper_id)
    paper = library.get_collection_paper(slug, paper_id)
    return templates.TemplateResponse(request, "_paper_row.html", {"slug": slug, "p": paper})


@app.post("/c/{slug}/p/{paper_id}/drop", response_class=HTMLResponse)
def drop_paper(slug: str, paper_id: int) -> HTMLResponse:
    """Hard-remove a failed import (no graveyard); returns empty to delete the row."""
    _require_collection(slug)
    library.drop_paper(slug, paper_id)
    return HTMLResponse("")


@app.post("/c/{slug}/papers/delete-pdf")
def papers_delete_pdf(slug: str, paper_ids: list[int] = Form(default=[])) -> RedirectResponse:
    """Bulk-delete the cached PDFs for the selected papers. Titles and collection
    membership are kept; only the local PDF file is removed (see library.delete_pdf)."""
    _require_collection(slug)
    for pid in paper_ids:
        library.delete_pdf(pid)
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.post("/c/{slug}/papers/move")
def papers_move(
    slug: str, target: str = Form(...), paper_ids: list[int] = Form(default=[])
) -> RedirectResponse:
    """Bulk-move the selected papers from this collection into ``target``."""
    _require_collection(slug)
    _require_collection(target)
    for pid in paper_ids:
        library.move_paper(slug, target, pid)
    return RedirectResponse(f"/c/{target}", status_code=303)


@app.post("/c/{slug}/papers/remove")
def papers_remove(slug: str, paper_ids: list[int] = Form(default=[])) -> RedirectResponse:
    """Stage removal of the selected papers: hide them from the collection now and drop
    their cached PDFs. The removal is queued (survives Refresh) and applied to the source
    on the next Sync, which deletes the item from Zotero."""
    _require_collection(slug)
    for pid in paper_ids:
        library.stage_removal(slug, pid)
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.get("/c/{slug}/graveyard", response_class=HTMLResponse)
def graveyard_panel(request: Request, slug: str) -> HTMLResponse:
    _require_collection(slug)
    return templates.TemplateResponse(
        request, "_graveyard.html",
        {"slug": slug, "items": library.list_graveyard(slug),
         "deleted": library.list_deleted(slug)},
    )


@app.post("/c/{slug}/graveyard/restore")
def graveyard_restore(slug: str, paper_ids: list[int] = Form(default=[])) -> RedirectResponse:
    """Un-remove the selected papers (either tier) — they return to the collection."""
    _require_collection(slug)
    for pid in paper_ids:
        library.restore_removal(slug, pid)
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.post("/c/{slug}/graveyard/delete")
def graveyard_delete(slug: str, paper_ids: list[int] = Form(default=[])) -> RedirectResponse:
    """Permanently delete the selected Graveyard papers — they become tombstones (work kept,
    recoverable via Restore; a future Pull won't silently re-add them)."""
    _require_collection(slug)
    library.permanently_delete(slug, paper_ids)
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.post("/c/{slug}/graveyard/purge")
def graveyard_purge(slug: str, paper_ids: list[int] = Form(default=[])) -> RedirectResponse:
    """Purge the selected tombstones — forget their metadata and work entirely. A future
    Pull may then re-add them as brand-new papers."""
    _require_collection(slug)
    library.purge_removals(slug, paper_ids)
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.post("/c/{slug}/papers/mark-read")
def papers_mark_read(slug: str, paper_ids: list[int] = Form(default=[]),
                     read: str = Form("")) -> RedirectResponse:
    """Mark the selected papers read (read='true') or unread (read='') in this collection."""
    _require_collection(slug)
    library.mark_read(slug, paper_ids, read=read == "true")
    return RedirectResponse(f"/c/{slug}", status_code=303)




@app.get("/c/{slug}/duplicates", response_class=HTMLResponse)
def duplicates_panel(request: Request, slug: str) -> HTMLResponse:
    """Review panel for same-title duplicates: each group's members with their attention
    pattern, a pre-selected 'keep', and an Apply form (merge / remove the empties)."""
    _require_collection(slug)
    return templates.TemplateResponse(
        request, "_duplicates.html",
        {"slug": slug, "groups": library.find_duplicate_groups(slug)},
    )


@app.post("/c/{slug}/merge")
def papers_merge(
    slug: str, keep_id: int = Form(...), drop_ids: list[int] = Form(default=[])
) -> RedirectResponse:
    """Merge a duplicate group into ``keep_id``: the drops' chat/notes/highlights and
    memberships fold into keep, one PDF survives, empty drops just go (see merge_papers)."""
    _require_collection(slug)
    library.merge_papers(slug, keep_id, drop_ids)
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.post("/c/{slug}/merge-all")
def papers_merge_all(slug: str) -> RedirectResponse:
    """Resolve every duplicate group at once, each into its recommended keep (the
    most-engaged copy). Groups are over disjoint papers, so one pass is safe."""
    _require_collection(slug)
    for g in library.find_duplicate_groups(slug):
        library.merge_papers(slug, g["keep_id"], [m["id"] for m in g["members"]])
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.get("/c/{slug}/sync/preview", response_class=HTMLResponse)
def sync_preview(request: Request, slug: str) -> HTMLResponse:
    col = _require_collection(slug)
    tmpl = "_sync_panel.html" if _hx(request) else "sync.html"   # HTMX → modal fragment
    return templates.TemplateResponse(
        request, tmpl,
        {"slug": slug, "name": col["name"], "preview": sync_mod.pull_preview(slug),
         "running": sync_mod.is_running(slug)},
    )


@app.post("/c/{slug}/sync", response_class=HTMLResponse)
def sync_pull(request: Request, slug: str,
              readd_keys: list[str] = Form(default=[])) -> HTMLResponse:
    """Pull from Zotero (read-only model): add new papers, restore the picked previously-removed
    ones (``readd_keys``). Never writes to Zotero. The panel polls /status."""
    col = _require_collection(slug)
    sync_mod.start(slug, readd_keys=readd_keys)
    tmpl = "_sync_panel.html" if _hx(request) else "sync.html"
    return templates.TemplateResponse(
        request, tmpl,
        {"slug": slug, "name": col["name"], "preview": sync_mod.pull_preview(slug), "running": True},
    )


@app.get("/c/{slug}/sync/status")
def sync_status(slug: str) -> dict:
    return sync_mod.get_progress(slug)


@app.post("/c/{slug}/export/dir", response_class=HTMLResponse)
def export_to_dir(request: Request, slug: str, path: str = Form("")) -> HTMLResponse:
    """Clone the collection's PDFs into the given folder (no BibTeX). Returns an inline
    result fragment for the export popup."""
    _require_collection(slug)
    try:
        r = export_mod.export_pdfs(slug, path.strip() or None)
        msg = f"Copied {r['copied']} PDF(s) to {r['dir']}" + (
            f" ({r['missing']} had no PDF)" if r["missing"] else "")
        ok = True
    except ValueError as exc:
        msg, ok = str(exc), False
    return templates.TemplateResponse(request, "_export_result.html", {"ok": ok, "msg": msg})


@app.get("/c/{slug}/export/bibtex")
def export_bibtex(slug: str) -> dict:
    """The collection as BibTeX text (no file) — the popup shows it for copy."""
    _require_collection(slug)
    return {"bibtex": export_mod.to_bibtex(slug)}


def _reading_log_cap() -> int:
    try:
        return max(1, int(load_config().get("reading_log_cap", "100")))
    except (TypeError, ValueError):
        return 100


@app.get("/c/{slug}/p/{paper_id}", response_class=HTMLResponse)
def paper_page(request: Request, slug: str, paper_id: int, nav: str = "") -> HTMLResponse:
    col = _require_collection(slug)
    paper = library.get_paper(paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail=f"No paper {paper_id}")
    library.mark_read_if_unread(slug, paper_id)   # opening a paper marks it read
    if nav != "back":                             # back-nav preserves the walk-back order
        library.log_open(slug, paper_id, _reading_log_cap())
    note = notes_mod.get_note(slug, paper_id)
    # Free-markdown note = the note's fields folded into one markdown body.
    note_md = "\n\n".join(p for p in (note["summary"], note["thoughts"], note["key_quotes"]) if p)
    return templates.TemplateResponse(
        request,
        "paper.html",
        {
            "slug": slug,
            "name": col["name"],
            "paper": paper,
            "messages": _rendered_history(slug, paper_id),
            "paper_key": paper_id,
            "threads": list_threads(slug, paper_id),
            "active_thread": get_or_create_thread(slug, paper_id),
            "note": note,
            "note_md": note_md,
            "highlight_scheme": config_highlight_scheme(),
            "show_highlight_legend": load_config().get("show_highlight_legend", "true") != "false",
            "statuses": notes_mod.STATUSES,
            "usage": llm.usage(),
            "model": load_config().get("model", ""),
            "pdf_dark": load_config().get("pdf_dark", "true") != "false",
            # stream the chat when the paper sub-agent is eligible (Claude + cached PDF)
            "chat_streaming": paper_chat.get_agent(paper_id) is not None,
            "debug": load_config().get("debug", "false") == "true",
            "session_id": get_session_id(get_or_create_thread(slug, paper_id)) or "",
        },
    )


@app.post("/c/{slug}/chat", response_class=HTMLResponse)
def chat_post(
    request: Request,
    slug: str,
    message: str = Form(""),
    paper_key: str = Form(""),
    images_json: str = Form(""),
) -> HTMLResponse:
    col = _require_collection(slug)

    message = message.strip()
    images = _parse_images(images_json)
    if not message and not images:
        raise HTTPException(status_code=400, detail="Empty message")

    pid = int(paper_key) if paper_key else None
    thread_id = get_or_create_thread(slug, pid)
    history = get_messages(thread_id, limit=10)

    # /{collection-slug} routes the turn through the agent with read-only MCP tools
    # (AGENTIC_PLAN P6). Checked before the legacy /collection /wiki literals.
    prefix, remainder = agentic_chat.parse_prefix(message)
    if prefix is not None:
        return _agentic_chat_turn(request, slug, thread_id, prefix, remainder, message)

    # Interactive paper sub-agent (PAPER_CHAT_AGENT P8): when a paper with a cached PDF
    # is open, the turn goes to a persistent CLI session that reads the PDF itself.
    # Pasted images reach it via files (Claude Read / Codex -i).
    agent = paper_chat.get_agent(pid)
    if agent is not None:
        return _paper_subagent_turn(request, slug, col["name"], thread_id, pid, message, agent, images)

    # /collection (or /wiki) pulls the collection wiki into a per-paper turn.
    include_collection = False
    for token in ("/collection", "/wiki"):
        if token in message:
            include_collection = True
            message = message.replace(token, "").strip()

    messages, refs = context.build_messages(
        slug, col["name"], history, message, pid, include_collection,
        images=images, artifact=get_artifact(thread_id),
    )
    # What we persist/display for the user turn (image bytes aren't stored in history).
    stored = message + (f"\n\n_({len(images)} image{'s' if len(images) != 1 else ''} attached)_"
                        if images else "")

    # Only persist the turn if the LLM call succeeds, so the thread never holds
    # an orphan user message with no reply.
    error = None
    assistant_text = ""
    try:
        assistant_text = llm.complete(messages)
        add_message(thread_id, "user", stored or "(image)", refs)
        # Store refs on the assistant turn too (kept for context/grounding).
        add_message(thread_id, "assistant", assistant_text, refs)
    except llm.LLMError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - surface API errors in the UI
        logging.getLogger("paper_agent.chat").exception("chat completion failed")
        error = f"LLM call failed: {exc}"

    return templates.TemplateResponse(
        request,
        "_chat_turn.html",
        {
            "slug": slug,
            "user_html": render_md(stored or "(image)", slug),
            "user_images": images,
            "assistant_html": render_md(assistant_text, slug) if assistant_text else "",
            "error": error,
            "usage": llm.usage(),
            "agentic": False,
        },
    )


@app.post("/c/{slug}/chat/stream")
def collection_chat_stream(slug: str, message: str = Form(""),
                           paper_key: str = Form(""), images_json: str = Form("")):
    """Streamed PLAIN collection turn (also a non-agent paper turn when a paper is
    open) — NDJSON token/done/error, so the collection chat shows the message +
    a typing indicator immediately instead of blocking like a frozen app. Slash-
    command / agentic-prefixed turns are NOT routed here; the composer sends those
    to the blocking /chat endpoint (they need the agentic flow)."""
    col = _require_collection(slug)
    message = message.strip()
    images = _parse_images(images_json)
    if not message and not images:
        raise HTTPException(status_code=400, detail="Empty message")
    pid = int(paper_key) if paper_key else None
    thread_id = get_or_create_thread(slug, pid)
    # /collection /wiki include tokens (mirror chat_post).
    include_collection = False
    for token in ("/collection", "/wiki"):
        if token in message:
            include_collection = True
            message = message.replace(token, "").strip()
    history = get_messages(thread_id, limit=10)
    messages, refs = context.build_messages(
        slug, col["name"], history, message, pid, include_collection,
        images=images, artifact=get_artifact(thread_id),
    )
    stored = message + (f"\n\n_({len(images)} image{'s' if len(images) != 1 else ''} attached)_"
                        if images else "")

    def _ev(d: dict) -> str:
        return json.dumps(d) + "\n"

    def gen():
        acc = ""
        try:
            for tok in llm.stream(messages):
                acc += tok
                yield _ev({"type": "token", "text": tok})
        except llm.LLMError as exc:
            yield _ev({"type": "error", "text": str(exc)}); return
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("paper_agent.chat").exception("collection chat stream failed")
            yield _ev({"type": "error", "text": f"LLM call failed: {exc}"}); return
        # Persist only on success, so the thread never holds an orphan user turn.
        add_message(thread_id, "user", stored or "(image)", refs)
        add_message(thread_id, "assistant", acc, refs)
        yield _ev({"type": "done"})

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/c/{slug}/p/{paper_id}/chat/stream")
def paper_chat_stream(slug: str, paper_id: int, message: str = Form(""),
                      images_json: str = Form("")):
    """Streamed per-paper turn (PAPER_CHAT_AGENT Phase A): NDJSON events
    (status/token/done/error). Sub-agent when eligible; else a classic one-shot emitted
    as a single token. Persists the turn only on success."""
    col = _require_collection(slug)
    message = message.strip()
    images = _parse_images(images_json)
    if not message and not images:
        raise HTTPException(status_code=400, detail="Empty message")
    paper = library.get_paper(paper_id)
    title = paper["title"] if paper else str(paper_id)
    thread_id = get_or_create_thread(slug, paper_id)
    agent = paper_chat.get_agent(paper_id)
    stored = message + (f"\n\n_({len(images)} image{'s' if len(images) != 1 else ''} attached)_"
                        if images else "")

    def _ev(d: dict) -> str:
        return json.dumps(d) + "\n"

    def gen():
        refs = [{"type": "paper", "id": paper_id}]
        if agent is None:
            # classic fallback: one-shot completion emitted as a single chunk (text-only)
            history = get_messages(thread_id, limit=10)
            messages, refs2 = context.build_messages(slug, col["name"], history, message,
                                                     paper_id, False, images=images)
            try:
                text = llm.complete(messages)
            except llm.LLMError as exc:
                yield _ev({"type": "error", "text": str(exc)}); return
            add_message(thread_id, "user", stored or "(image)", refs2)
            add_message(thread_id, "assistant", text, refs2)
            yield _ev({"type": "token", "text": text}); yield _ev({"type": "done"}); return
        final = ""
        try:
            for ev in agent.stream(slug, col["name"], thread_id, paper_id, title, message, images):
                if ev.get("type") == "done":
                    final = ev.get("text", "")
                yield _ev(ev)
        except llm.LLMError as exc:
            yield _ev({"type": "error", "text": str(exc)}); return
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("paper_agent.chat").exception("paper chat stream failed")
            yield _ev({"type": "error", "text": f"Paper chat failed: {exc}"}); return
        if final:  # persist only a successful turn
            add_message(thread_id, "user", stored or "(image)", refs)
            add_message(thread_id, "assistant", final, refs)

    return StreamingResponse(gen(), media_type="application/x-ndjson")


def _paper_subagent_turn(request, slug, name, thread_id, paper_id, message, agent, images=None):
    """One turn of the interactive paper sub-agent (reads the PDF + the user's notes via
    a persistent CLI session). Read-only; nothing is saved to notes/wiki here."""
    paper = library.get_paper(paper_id)
    title = paper["title"] if paper else str(paper_id)
    error, assistant_text = None, ""
    stored = message + (f"\n\n_({len(images)} image{'s' if len(images) != 1 else ''} attached)_"
                        if images else "")
    try:
        res = agent.answer(slug, name, thread_id, paper_id, title, message, images)
        assistant_text = res.reply
        add_message(thread_id, "user", stored or "(image)", [{"type": "paper", "id": paper_id}])
        add_message(thread_id, "assistant", assistant_text, [{"type": "paper", "id": paper_id}])
    except llm.LLMError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("paper_agent.chat").exception("paper sub-agent failed")
        error = f"Paper chat failed: {exc}"
    return templates.TemplateResponse(
        request, "_chat_turn.html",
        {"slug": slug, "user_html": render_md(message, slug), "user_images": [],
         "assistant_html": render_md(assistant_text, slug) if assistant_text else "",
         "error": error, "suggestion": None, "usage": llm.usage(), "agentic": False},
    )


def _agentic_chat_turn(request, slug, thread_id, prefix, remainder, original):
    """A /{collection} turn: answer via the agent (read-only MCP tools) or, if the
    prefix isn't a known collection, reply listing the available ones (no LLM call)."""
    slugs = {c["slug"] for c in library.list_collections()}
    if prefix not in slugs:
        avail = ", ".join(sorted(slugs)) or "(none yet)"
        reply = f"No collection `/{prefix}`. Available: {avail}"
        return templates.TemplateResponse(
            request, "_chat_turn.html",
            {"slug": slug, "user_html": render_md(original, slug), "user_images": [],
             "assistant_html": render_md(reply, slug), "error": None,
             "suggestion": None, "usage": llm.usage(), "agentic": False},
        )
    history = get_messages(thread_id, limit=10)
    error, assistant_text = None, ""
    try:
        assistant_text = agentic_chat.answer(prefix, history, remainder or "(no question)")
        add_message(thread_id, "user", original, [{"type": "collection", "id": prefix}])
        add_message(thread_id, "assistant", assistant_text, [])
    except llm.LLMError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("paper_agent.chat").exception("agentic chat failed")
        error = f"Agentic chat failed: {exc}"
    return templates.TemplateResponse(
        request, "_chat_turn.html",
        {"slug": slug, "user_html": render_md(original, slug), "user_images": [],
         "assistant_html": render_md(assistant_text, slug) if assistant_text else "",
         "assistant_text": assistant_text, "error": error, "suggestion": None,
         "usage": llm.usage(), "agentic": True, "capture_slug": prefix},
    )


@app.post("/c/{slug}/thoughts/capture", response_class=HTMLResponse)
def thoughts_capture(slug: str, agent_text: str = Form(""), your_take: str = Form("")) -> HTMLResponse:
    """Attribution-safe capture from agentic chat: the agent's reply becomes a
    (seed, agent) thought (can't ground an assertion); an optional 'your take' becomes
    a (reasoning, human) thought linked to it via prompted_by."""
    _require_collection(slug)
    agent_text = agent_text.strip()
    if not agent_text:
        raise HTTPException(status_code=400, detail="nothing to capture")
    seed_id = thoughts_mod.create_thought(slug, agent_text, synth_kind="seed", author_origin="agent")
    take = your_take.strip()
    if take:
        thoughts_mod.create_thought(slug, take, synth_kind="reasoning",
                                    author_origin="human", prompted_by=seed_id)
    msg = "Captured as a seed" + (" + your reasoning" if take else "") + "."
    return HTMLResponse(f'<span class="text-emerald-700">✓ {msg}</span>')


@app.post("/c/{slug}/chat/compact")
def collection_chat_compact(slug: str) -> RedirectResponse:
    """Compact the collection chat into a single 'artifact' summary, then clear the
    back-and-forth history. The artifact persists as the thread's system message and
    carries forward as grounding for future turns. History is only cleared if the
    summarization succeeds, so a failed LLM call never loses the conversation."""
    _require_collection(slug)
    thread_id = get_or_create_thread(slug, None)
    live = [m for m in get_messages(thread_id) if m["role"] in ("user", "assistant")]
    if not live:
        return RedirectResponse(f"/c/{slug}", status_code=303)   # nothing to compact

    parts = []
    prior = get_artifact(thread_id)
    if prior:
        parts.append(f"PREVIOUS SUMMARY (already compacted):\n{prior}")
    for m in live:
        parts.append(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}")
    prompt = [
        {"role": "system", "content":
            "You compact a research-assistant conversation into a concise 'artifact' — "
            "a faithful briefing that preserves the USER's questions, the key findings "
            "and conclusions, any decisions or open threads, and the specific papers or "
            "claims referenced. It replaces the chat history and becomes your memory of "
            "the conversation, so do not invent or editorialize. Write compact markdown "
            "with short headed sections."},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    try:
        summary = llm.complete(prompt)
    except Exception:  # noqa: BLE001 - never destroy history on a failed compaction
        logging.getLogger("paper_agent.chat").exception("chat compaction failed")
        return RedirectResponse(f"/c/{slug}", status_code=303)

    clear_messages(thread_id)
    add_message(thread_id, "system", summary)
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.post("/c/{slug}/chat/delete")
def collection_chat_delete(slug: str) -> RedirectResponse:
    """Remove the current collection chat entirely (history + any artifact). A fresh
    empty thread is created on next load."""
    _require_collection(slug)
    delete_thread(get_or_create_thread(slug, None))
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.get("/c/{slug}/p/{paper_id}/prev")
def paper_prev(slug: str, paper_id: int) -> dict:
    """The previously-read paper in this collection's reading log (browser-style back).
    Returns {paper_id} to navigate to, or {first: true} when it's the oldest in the log."""
    _require_collection(slug)
    prev = library.previous_in_log(slug, paper_id)
    return {"paper_id": prev} if prev else {"first": True}


@app.get("/c/{slug}/p/{paper_id}/next-unread")
def paper_next_unread(slug: str, paper_id: int) -> RedirectResponse:
    """Jump to the next unread paper in this collection (wraps); back to the collection
    page if there are no other unread papers."""
    _require_collection(slug)
    nxt = library.next_unread(slug, paper_id)
    target = f"/c/{slug}/p/{nxt}" if nxt else f"/c/{slug}"
    return RedirectResponse(target, status_code=303)


@app.get("/jump", response_class=HTMLResponse)
def jump_panel(request: Request, current: str = "") -> HTMLResponse:
    """A cross-collection paper picker (loaded into the 'Jump to paper' modal). Lists
    every collection and its papers; the current collection floats to the top."""
    cols = library.list_collections()
    cols.sort(key=lambda c: (c["slug"] != current, c["name"].lower()))
    groups = [
        {"slug": c["slug"], "name": c["name"], "current": c["slug"] == current,
         "papers": [{"id": p["id"], "title": p.get("title") or "(untitled)",
                     "authors": p.get("authors") or "", "year": p.get("year") or ""}
                    for p in library.list_papers(c["slug"])]}
        for c in cols
    ]
    return templates.TemplateResponse(request, "_jump.html", {"groups": groups, "current": current})


@app.post("/c/{slug}/p/{paper_id}/chat/new")
def chat_new(slug: str, paper_id: int) -> RedirectResponse:
    """Start a new conversation for this paper (per-paper mode). Avoids spawning an
    empty thread if the current one is already empty."""
    _require_collection(slug)
    tid = get_or_create_thread(slug, paper_id)
    if thread_message_count(tid) > 0:
        new_thread(slug, paper_id)
    return RedirectResponse(f"/c/{slug}/p/{paper_id}", status_code=303)


@app.post("/c/{slug}/p/{paper_id}/chat/delete")
def chat_delete(slug: str, paper_id: int) -> RedirectResponse:
    """Delete the current conversation for this paper. The next-most-recent thread
    (or a fresh one) becomes active on reload."""
    _require_collection(slug)
    tid = get_or_create_thread(slug, paper_id)
    live_session.drop(tid)   # kill any live process bound to this thread
    delete_thread(tid)
    return RedirectResponse(f"/c/{slug}/p/{paper_id}", status_code=303)


@app.post("/c/{slug}/p/{paper_id}/chat/switch/{thread_id}")
def chat_switch(slug: str, paper_id: int, thread_id: int) -> RedirectResponse:
    """Reopen an older conversation for this paper (makes it the active one)."""
    _require_collection(slug)
    if thread_belongs(thread_id, slug, paper_id):
        touch_thread(thread_id)
    return RedirectResponse(f"/c/{slug}/p/{paper_id}", status_code=303)


@app.get("/pdf/{paper_id}")
def pdf_stream(paper_id: int) -> FileResponse:
    if not pdf_store.store_available():
        raise HTTPException(status_code=503, detail="PDF store is unavailable (drive disconnected?).")
    dest = pdf_store.pdf_dest(paper_id)
    if not dest.exists():
        dest = pdf_store.ensure_cached(paper_id)   # lazy copy-on-demand
    if dest is None or not dest.exists():
        raise HTTPException(status_code=404, detail="No PDF for this paper")
    return FileResponse(
        dest, media_type="application/pdf", headers={"Content-Disposition": "inline"}
    )


def _require_collection(slug: str) -> dict:
    col = library.get_collection(slug)
    if col is None:
        raise HTTPException(status_code=404, detail=f"No collection for slug '{slug}'")
    return col


# ===========================================================================
# Phase 1.5 — PDF annotations (addendum Capability 1). JSON API: the overlay is
# positioned client-side from PDF coordinates, so HTMX swaps don't fit here.
# ===========================================================================
@app.get("/c/{slug}/p/{paper_id}/annotations")
def annotations_list(slug: str, paper_id: int) -> dict:
    return {"annotations": [ann_mod.to_client(a) for a in ann_mod.list_all(paper_id, slug)]}


@app.post("/c/{slug}/p/{paper_id}/annotations")
def annotations_create(slug: str, paper_id: int, payload: dict = Body(...)) -> dict:
    _require_collection(slug)
    position = payload.get("position") or {}
    ann = ann_mod.create(
        slug, paper_id,
        kind=payload.get("kind", "highlight"),
        color=payload.get("color"),
        page=int(position.get("pageIndex", payload.get("page", 0))),
        position_json=json.dumps(position),
        selected_text=payload.get("selected_text", ""),
        note_text=payload.get("note_text", ""),
    )
    return ann_mod.to_client(ann)


@app.patch("/annotations/{ann_id}")
def annotations_update(ann_id: int, payload: dict = Body(...)) -> dict:
    ann = ann_mod.update(
        ann_id, color=payload.get("color"), note_text=payload.get("note_text")
    )
    if ann is None:
        raise HTTPException(status_code=403, detail="Zotero-origin annotations are read-only.")
    return ann_mod.to_client(ann)


@app.delete("/annotations/{ann_id}")
def annotations_delete(ann_id: int) -> dict:
    if not ann_mod.delete(ann_id):
        raise HTTPException(
            status_code=403,
            detail="Can't delete: not found or a read-only Zotero annotation.",
        )
    return {"ok": True}


# ===========================================================================
# Phase 3 — per-paper notes
# ===========================================================================
@app.get("/c/{slug}/p/{paper_id}/notes", response_class=HTMLResponse)
def notes_get(request: Request, slug: str, paper_id: int) -> HTMLResponse:
    col = _require_collection(slug)
    paper = library.get_paper(paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="No paper")
    note = notes_mod.get_note(slug, paper_id)
    return templates.TemplateResponse(
        request,
        "notes.html",
        {"slug": slug, "name": col["name"], "paper": paper, "note": note,
         "statuses": notes_mod.STATUSES, "draft": False},
    )


@app.post("/c/{slug}/p/{paper_id}/notes", response_class=HTMLResponse)
def notes_post(
    request: Request, slug: str, paper_id: int,
    summary: str = Form(""), thoughts: str = Form(""),
    key_quotes: str = Form(""), status: str = Form("unread"),
    synth_kind: str = Form("auto"),
) -> RedirectResponse:
    _require_collection(slug)
    # author_origin is stamped by this human-capture door, never from the form.
    notes_mod.save_note(slug, paper_id, summary, thoughts, key_quotes, status,
                        synth_kind=synth_kind, author_origin="human")
    return RedirectResponse(f"/c/{slug}/p/{paper_id}/notes", status_code=303)


def _draft_note_fields(slug: str, paper_id: int, paper_title: str) -> tuple[dict, str | None]:
    """Draft note fields from this paper's highlights + its chat history.

    Returns ({summary, thoughts, key_quotes}, error). Never saves.
    """
    thread_id = get_or_create_thread(slug, paper_id)  # the paper's own thread
    history = get_messages(thread_id, limit=12)
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history) or "(no chat yet)"
    highlights = context._highlights_block(slug, paper_id) or "(no highlights yet)"
    try:
        resp = llm.complete([
            {"role": "system", "content": "You output only valid JSON with keys "
             "summary, thoughts, key_quotes. Draft the USER's note from their "
             "highlights and chat about this paper; do not invent content beyond them."},
            {"role": "user", "content":
             f"Paper: {paper_title}\n\nHighlights:\n{highlights}\n\nChat:\n{convo}\n\n"
             'Return {"summary": "...", "thoughts": "...", "key_quotes": "- ..."}'},
        ])
        data = json.loads(resp[resp.find("{"): resp.rfind("}") + 1])
        return ({"summary": data.get("summary", ""), "thoughts": data.get("thoughts", ""),
                 "key_quotes": data.get("key_quotes", "")}, None)
    except llm.LLMError as exc:
        return ({}, str(exc))
    except Exception as exc:  # noqa: BLE001
        return ({}, f"Draft failed: {exc}")


@app.post("/c/{slug}/p/{paper_id}/notes/draft", response_class=HTMLResponse)
def notes_draft(request: Request, slug: str, paper_id: int) -> HTMLResponse:
    """Draft notes from highlights + chat (full-page /notes variant). Never saves."""
    col = _require_collection(slug)
    paper = library.get_paper(paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="No paper")
    note = notes_mod.get_note(slug, paper_id)
    fields, error = _draft_note_fields(slug, paper_id, paper["title"])
    if fields:
        note = {**fields, "status": note["status"]}
    return templates.TemplateResponse(
        request, "notes.html",
        {"slug": slug, "name": col["name"], "paper": paper, "note": note,
         "statuses": notes_mod.STATUSES, "draft": True, "error": error},
    )


@app.post("/c/{slug}/p/{paper_id}/note/draft.json")
def note_draft_json(slug: str, paper_id: int) -> dict:
    """JSON draft for the in-reading Note modal: fields from highlights + chat."""
    _require_collection(slug)
    paper = library.get_paper(paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="No paper")
    fields, error = _draft_note_fields(slug, paper_id, paper["title"])
    return {"fields": fields, "error": error}


# ===========================================================================
# Phase 4 — thoughts stream
# ===========================================================================
@app.get("/c/{slug}/thoughts", response_class=HTMLResponse)
def thoughts_get(request: Request, slug: str) -> HTMLResponse:
    col = _require_collection(slug)
    items = thoughts_mod.list_thoughts(slug)
    return templates.TemplateResponse(
        request, "thoughts.html",
        {"slug": slug, "name": col["name"], "thoughts": items, "consolidation": None},
    )


@app.post("/c/{slug}/thoughts")
def thoughts_create(slug: str, text: str = Form(...), synth_kind: str = Form("seed")) -> RedirectResponse:
    _require_collection(slug)
    if text.strip():
        # author_origin stamped by this human door; synth_kind from the seed|reasoning toggle.
        thoughts_mod.create_thought(slug, text.strip(), synth_kind=synth_kind, author_origin="human")
    return RedirectResponse(f"/c/{slug}/thoughts", status_code=303)


@app.get("/c/{slug}/thoughts/panel", response_class=HTMLResponse)
def thoughts_panel(request: Request, slug: str) -> HTMLResponse:
    """Thoughts tab body for the collection chat panel (HTMX-loaded)."""
    _require_collection(slug)
    return templates.TemplateResponse(
        request, "_thoughts_panel.html",
        {"slug": slug, "thoughts": thoughts_mod.list_thoughts(slug)},
    )


@app.post("/c/{slug}/thoughts/add", response_class=HTMLResponse)
def thoughts_add(
    request: Request, slug: str, text: str = Form(""), synth_kind: str = Form("seed")
) -> HTMLResponse:
    """Quick-add from the Thoughts tab; returns the refreshed panel fragment."""
    _require_collection(slug)
    if text.strip():
        thoughts_mod.create_thought(slug, text.strip(), synth_kind=synth_kind, author_origin="human")
    return templates.TemplateResponse(
        request, "_thoughts_panel.html",
        {"slug": slug, "thoughts": thoughts_mod.list_thoughts(slug)},
    )


@app.post("/c/{slug}/thoughts/{tid}/update")
def thoughts_update(slug: str, tid: str, text: str = Form(...)) -> RedirectResponse:
    thoughts_mod.update_thought(slug, tid, text.strip())
    return RedirectResponse(f"/c/{slug}/thoughts", status_code=303)


@app.post("/c/{slug}/thoughts/{tid}/delete")
def thoughts_delete(slug: str, tid: str) -> RedirectResponse:
    thoughts_mod.delete_thought(slug, tid)
    return RedirectResponse(f"/c/{slug}/thoughts", status_code=303)


@app.post("/c/{slug}/thoughts/{tid}/supersede")
def thoughts_supersede(slug: str, tid: str) -> RedirectResponse:
    thoughts_mod.supersede_thought(slug, tid)
    return RedirectResponse(f"/c/{slug}/thoughts", status_code=303)


@app.post("/c/{slug}/thoughts/consolidate", response_class=HTMLResponse)
def thoughts_consolidate(request: Request, slug: str, ids: list[str] = Form(...)) -> HTMLResponse:
    col = _require_collection(slug)
    items = thoughts_mod.list_thoughts(slug)
    error = None
    proposed = ""
    try:
        proposed = thoughts_mod.propose_consolidation(slug, ids)
    except llm.LLMError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        error = f"Consolidation failed: {exc}"
    return templates.TemplateResponse(
        request, "thoughts.html",
        {"slug": slug, "name": col["name"], "thoughts": items,
         "consolidation": {"ids": ids, "text": proposed, "error": error}},
    )


@app.post("/c/{slug}/thoughts/consolidate/accept")
def thoughts_consolidate_accept(
    slug: str, ids: list[str] = Form(...), text: str = Form(...)
) -> RedirectResponse:
    thoughts_mod.accept_consolidation(slug, ids, text)
    return RedirectResponse(f"/c/{slug}/thoughts", status_code=303)


# ===========================================================================
# Phase 5 — wiki + review queue
# ===========================================================================
# --- inline-panel helpers (left column of the collection page) ---------------
def _hx(request: Request) -> bool:
    """True when the request came from HTMX (so we return a fragment, not a page)."""
    return request.headers.get("HX-Request") == "true"


def _wiki_panel(request: Request, slug: str, gaps=None,
                attention_since: str | None = None) -> HTMLResponse:
    """Render the inline wiki panel. ``attention_since`` is the user's PREVIOUS
    last_wiki_viewed_at (the GET handler bumps it via wiki.read_and_bump_viewed and
    passes the old value here). POST handlers pass None to suppress the "new since
    last view" badges so a re-render after ↻ Regenerate doesn't reset badge state.

    Async draft contract: if a draft job exists for this slug, pass it through so
    the template can render the in-panel overlay. A done/failed job is observed
    once here and then cleared, so the next render is back to the idle path and
    /wiki/draft/status starts returning 'idle' for new polls."""
    job = wiki.get_draft_job(slug)
    if job and job.get("status") in ("done", "failed"):
        # Job finished; this render shows the regenerated panel (no overlay).
        # Clear the job so polls return 'idle' and we don't loop a refresh.
        wiki.clear_draft_job(slug)
        job_err = job.get("error") if job["status"] == "failed" else None
        job = None
    else:
        job_err = None

    # Field Model (cognitive-model wiki) — no markdown rendering needed at the
    # panel level; the template renders structured data (thesis callouts,
    # landscape bullet lists, paper cards) directly via Jinja.
    overview = wiki.load_overview(slug, attention_since=attention_since)

    return templates.TemplateResponse(
        request, "_wiki_panel.html",
        {"slug": slug,
         "overview": overview,
         "draft_job": job,                       # active running job, or None
         "draft_initial_action": wiki._stage_message(job)["action"] if job else "",
         "draft_initial_subline": wiki._stage_message(job)["subline"] if job else "",
         "draft_initial_progress": wiki._stage_progress(job) if job else 0,
         "draft_error": job_err},
    )


def _triage_panel(request: Request, slug: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_triage_panel.html",
        {"slug": slug, "items": triage_mod.list_triage(slug), "inbox": triage_mod._inbox_config(slug)},
    )


@app.get("/c/{slug}/wiki/panel", response_class=HTMLResponse)
def wiki_panel(request: Request, slug: str) -> HTMLResponse:
    _require_collection(slug)
    # GET → bump-and-pass-old so the embedded paper cards get "new since last view" badges.
    return _wiki_panel(request, slug, attention_since=wiki.read_and_bump_viewed(slug))


@app.post("/c/{slug}/wiki/draft", response_class=HTMLResponse)
def wiki_draft_seed(request: Request, slug: str) -> HTMLResponse:
    """Kick off the starter-wiki draft on a background daemon thread and re-render
    the panel immediately. The new panel renders the in-progress overlay (because
    wiki.get_draft_job(slug) now returns status='running'); the overlay's polling
    script handles refresh on completion. Last-viewed isn't bumped here, so the
    badge state survives this re-render."""
    _require_collection(slug)
    wiki.start_draft_async(slug, force=True)
    return _wiki_panel(request, slug)


@app.get("/c/{slug}/wiki/draft/status", response_class=JSONResponse)
def wiki_draft_status(slug: str) -> JSONResponse:
    """Live state of the slug's draft job for the in-panel overlay to poll. Returns
    status, stage, the agent's human-voice message + subline, a progress estimate,
    and the started_at timestamp. Status 'idle' means no job is tracked (either
    nothing was started, or the job was cleaned up after the next panel render)."""
    _require_collection(slug)
    job = wiki.get_draft_job(slug)
    if not job:
        return JSONResponse({"status": "idle"})
    msg = wiki._stage_message(job)
    return JSONResponse({
        "status": job.get("status", "running"),
        "stage": job.get("stage", "gathering"),
        "action": msg["action"],
        "subline": msg["subline"],
        "progress": wiki._stage_progress(job),
        "started_at": job.get("started_at"),
        "error": job.get("error"),
    })


# --- Phase C: belief tray routes -------------------------------------------
# Suggest is synchronous (typical 10-20s LLM call). Accept/Dismiss are pure
# file moves. All three re-render the panel so the user sees the new state.

@app.post("/c/{slug}/wiki/beliefs/suggest", response_class=HTMLResponse)
def wiki_beliefs_suggest(request: Request, slug: str) -> HTMLResponse:
    """Run the belief-draft LLM call synchronously and re-render the panel.
    Refusal modes (no concepts, no signal) come back as a flash message via
    request.session if available; otherwise the panel simply re-renders with
    no new candidates."""
    _require_collection(slug)
    wiki.suggest_beliefs(slug)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/themes/{sig}/rename", response_class=HTMLResponse)
def wiki_theme_rename(request: Request, slug: str, sig: str,
                      name: str = Form("")) -> HTMLResponse:
    """User-edit a theme's name (overrides the agent's). Re-renders the panel."""
    _require_collection(slug)
    wiki.rename_theme(slug, sig, name)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/beliefs/{cid}/accept", response_class=HTMLResponse)
def wiki_belief_accept(request: Request, slug: str, cid: str) -> HTMLResponse:
    """Promote a candidate to accepted (file move). Re-render the panel."""
    _require_collection(slug)
    wiki.accept_belief(slug, cid)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/beliefs/{cid}/dismiss", response_class=HTMLResponse)
def wiki_belief_dismiss(request: Request, slug: str, cid: str) -> HTMLResponse:
    """Drop a candidate (delete the file). Re-render the panel."""
    _require_collection(slug)
    wiki.dismiss_belief(slug, cid)
    return _wiki_panel(request, slug)


# --- Recommended-papers-to-add (arXiv discovery → triage → collection) --------

@app.post("/c/{slug}/wiki/recommend-add", response_class=HTMLResponse)
def wiki_recommend_add(request: Request, slug: str) -> HTMLResponse:
    """Run arXiv discovery seeded by the field model and enqueue new candidates
    into triage (synchronous network action). Re-renders the panel."""
    _require_collection(slug)
    try:
        wiki.suggest_papers_to_add(slug)
    except Exception:  # noqa: BLE001
        logging.getLogger("paper_agent.wiki").exception("suggest_papers_to_add failed")
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/add/{tid}/accept", response_class=HTMLResponse)
def wiki_add_accept(request: Request, slug: str, tid: int) -> HTMLResponse:
    """Accept a recommended paper → import it into the collection (via triage)."""
    _require_collection(slug)
    triage_mod.accept(slug, tid)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/add/{tid}/dismiss", response_class=HTMLResponse)
def wiki_add_dismiss(request: Request, slug: str, tid: int) -> HTMLResponse:
    """Dismiss a recommended paper (reject the triage candidate)."""
    _require_collection(slug)
    triage_mod.reject(slug, tid)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/paper/{pid}/toggle-read", response_class=HTMLResponse)
def wiki_paper_toggle_read(request: Request, slug: str, pid: int) -> HTMLResponse:
    """Flip a paper's read state from the wiki's Evidence section (papers live
    in the wiki now). Opening a paper auto-marks it read; this is the manual
    toggle. Re-renders the panel."""
    _require_collection(slug)
    cur = library.get_collection_paper(slug, pid)
    library.mark_read(slug, [pid], read=not bool(cur and cur.get("read")))
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/gaps", response_class=HTMLResponse)
def wiki_gaps(request: Request, slug: str) -> HTMLResponse:
    _require_collection(slug)
    error = None
    gaps = []
    try:
        gaps = discover.find_gaps(slug)
    except llm.LLMError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        error = f"Gap search failed: {exc}"
    gaps_ctx = {"items": gaps, "error": error}
    return _wiki_panel(request, slug, gaps=gaps_ctx)


# ===========================================================================
# Phase 7 — triage
# ===========================================================================
@app.get("/c/{slug}/triage", response_class=HTMLResponse)
def triage_get(request: Request, slug: str) -> HTMLResponse:
    col = _require_collection(slug)
    cfg = triage_mod._inbox_config(slug)
    return templates.TemplateResponse(
        request, "triage.html",
        {"slug": slug, "name": col["name"], "items": triage_mod.list_triage(slug),
         "inbox": cfg, "message": None},
    )


@app.get("/c/{slug}/triage/panel", response_class=HTMLResponse)
def triage_panel(request: Request, slug: str) -> HTMLResponse:
    _require_collection(slug)
    return _triage_panel(request, slug)


@app.post("/c/{slug}/triage/scan", response_class=HTMLResponse)
def triage_scan(request: Request, slug: str):
    z = get_zotero()
    _require_collection(slug)
    triage_mod.scan_inbox(z, slug)
    if _hx(request):
        return _triage_panel(request, slug)
    return RedirectResponse(f"/c/{slug}/triage", status_code=303)


@app.post("/c/{slug}/triage/{tid}/pitch", response_class=HTMLResponse)
def triage_pitch(request: Request, slug: str, tid: int):
    try:
        triage_mod.generate_pitch(slug, tid)
    except llm.LLMError:
        pass
    if _hx(request):
        return _triage_panel(request, slug)
    return RedirectResponse(f"/c/{slug}/triage", status_code=303)


@app.post("/c/{slug}/triage/{tid}/{action}", response_class=HTMLResponse)
def triage_action(request: Request, slug: str, tid: int, action: str):
    fn = {"accept": triage_mod.accept, "reject": triage_mod.reject, "defer": triage_mod.defer}.get(action)
    if fn:
        fn(slug, tid)
    if _hx(request):
        return _triage_panel(request, slug)
    return RedirectResponse(f"/c/{slug}/triage", status_code=303)


@app.post("/c/{slug}/gaps/send", response_class=HTMLResponse)
def gaps_send(
    request: Request,
    slug: str,
    arxiv_id: str = Form(...),
    title: str = Form(""),
    abstract: str = Form(""),
    note: str = Form(""),
):
    # Local-first: a discovered paper goes straight into the collection as an
    # arxiv-suggested member (PDF pulled if eager). It reaches Zotero only via Sync.
    _require_collection(slug)
    triage_mod.accept_arxiv_into_collection(slug, arxiv_id, title=title, abstract=abstract, note=note)
    if _hx(request):
        return _wiki_panel(request, slug)   # back to the wiki panel (item now in the list)
    return RedirectResponse(f"/c/{slug}", status_code=303)


# ===========================================================================
# Phase 8 — stale papers
# ===========================================================================
@app.get("/c/{slug}/stale", response_class=HTMLResponse)
def stale_get(request: Request, slug: str) -> HTMLResponse:
    col = _require_collection(slug)
    stale = discover.find_stale(slug)
    return templates.TemplateResponse(
        request, "stale.html",
        {"slug": slug, "name": col["name"], "stale": stale},
    )


def _safe_source(z) -> str:
    try:
        return z.source()
    except Exception as exc:  # pragma: no cover - defensive
        return f"unavailable ({exc})"
