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
import threading
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
    citations as citations_mod,
    note_drafts,
    notes as notes_mod,
    paper_chat,
    pdf_store,
    sync as sync_mod,
    theme as theme_mod,
    thoughts as thoughts_mod,
    topic_view,
    topics as topics_mod,
    notify,
    triage as triage_mod,
    wiki,
    wiki_propose,
)
from .config import highlight_scheme as config_highlight_scheme, load_config, save_config
from .db import connect, init_db
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


def _asset_v(name: str) -> int:
    """Cache-busting token for a local static asset = its file mtime. Appended as ?v= so a
    rebuilt annotate.js / app.css is fetched fresh without a manual hard-reload."""
    try:
        return int((BASE_DIR / "static" / name).stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["asset_v"] = _asset_v


def _pa_nav() -> dict:
    """Sidebar data for the app shell (base.html) — collections + research topics.
    Evaluated per render; degrades to empty lists if the DB isn't ready."""
    try:
        usage = topics_mod.collection_usage()
        cols = [{"slug": c["slug"], "name": c["name"], "papers": c.get("paper_count"),
                 "last_added": c.get("last_added") or "",
                 "topics_using": usage.get(c["slug"], 0)}
                for c in library.list_collections(with_activity=True)]
        # "Latest" first for the sidebar's show-more cap (most-recently-added paper);
        # ties + empties fall back to alphabetical (stable two-pass sort).
        cols.sort(key=lambda c: (c["name"] or "").lower())
        cols.sort(key=lambda c: c["last_added"] or "", reverse=True)
    except Exception:  # noqa: BLE001
        cols = []
    try:
        tops = [{"slug": t["slug"], "title": t["title"], "status": t["status"]}
                for t in topics_mod.list_topics()]
    except Exception:  # noqa: BLE001
        tops = []
    return {"collections": cols, "topics": tops}


templates.env.globals["pa_nav"] = _pa_nav

app = FastAPI(title="Prinny")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.on_event("shutdown")
def _shutdown() -> None:
    live_session.shutdown_all()   # don't orphan persistent chat processes


# Autodraft note jobs (keyed by paper_id) — registered only while the speculative LLM
# draft is actually running, so the Background Jobs dropdown lists it.
_AUTODRAFT_JOBS: dict[int, dict] = {}


def _active_jobs() -> list[dict]:
    """Currently-running background jobs across all registries, as labeled items for the
    Background Jobs dropdown (so in-progress work is listed, not just counted)."""
    regs = [(wiki._DRAFT_JOBS, "Drafting Field Model"),
            (wiki._ENTREV_JOBS, "Writing entity reviews"),
            (wiki._BENCH_JOBS, "Extracting benchmarks"),
            (wiki._READING_JOBS, "Searching papers"),
            (topic_view._GEN_JOBS, "Investigating topic"),
            (topic_view._READING_JOBS, "Searching papers")]
    out = []
    for reg, label in regs:
        for slug, job in list(reg.items()):
            if (job or {}).get("status") == "running":
                out.append({"label": label, "slug": slug})
    for job in list(_AUTODRAFT_JOBS.values()):
        if (job or {}).get("status") == "running":
            out.append({"label": job.get("label", "Drafting note"), "slug": job.get("slug", "")})
    return out


def _active_job_count() -> int:
    """Count of running background jobs (see _active_jobs)."""
    return len(_active_jobs())


def _to_review_items() -> list[dict]:
    """Everything awaiting the user's accept/dismiss across collections — staged note
    drafts + belief candidates + chat-proposed wiki edits — for the 'To review' card."""
    from . import wiki_propose
    items: list[dict] = []
    for d in note_drafts.list_all():
        p = library.get_paper(d["paper_id"])
        title = ((p or {}).get("title") or f"paper {d['paper_id']}").strip()
        items.append({"kind": "Note draft", "label": title[:60],
                      "link": f"/c/{d['collection_slug']}/p/{d['paper_id']}"})
    try:
        cols = library.list_collections()
    except Exception:  # noqa: BLE001
        cols = []
    for c in cols:
        slug = c["slug"]
        try:
            for b in wiki.list_belief_candidates(slug):
                items.append({"kind": "Belief", "label": (b.get("title") or "")[:60],
                              "link": f"/c/{slug}?tab=understanding"})
        except Exception:  # noqa: BLE001
            pass
        try:
            for pr in wiki_propose.list_pending(slug):
                items.append({"kind": "Wiki edit", "label": (pr.get("summary") or pr.get("section") or "")[:60],
                              "link": f"/c/{slug}"})
        except Exception:  # noqa: BLE001
            pass
    return items


@app.get("/notifications", response_class=JSONResponse)
def notifications_feed() -> JSONResponse:
    """Global background-job feed + running jobs + items awaiting review."""
    jobs = _active_jobs()
    review = _to_review_items()
    return JSONResponse({**notify.feed(), "running": len(jobs), "running_jobs": jobs,
                         "to_review": len(review), "to_review_items": review})


@app.post("/notifications/seen", response_class=JSONResponse)
def notifications_seen() -> JSONResponse:
    notify.mark_seen()
    return JSONResponse({"ok": True})


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
    _usage = topics_mod.collection_usage()           # {slug: n_topics} — drives the delete guard
    for c in collections:
        c["topics_using"] = _usage.get(c["slug"], 0)
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
            "topics": topics_mod.list_topics(),
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
                 description: str = Form(""),
                 collections: list[str] = Form([])) -> RedirectResponse:
    try:
        slug = topics_mod.create_topic(title, question, collections, description=description)
    except ValueError:
        return RedirectResponse("/topics?error=A+research+topic+needs+a+question.",
                                status_code=303)
    topics_mod.log_event(slug, "created", question.strip()[:200])
    return RedirectResponse(f"/t/{slug}", status_code=303)


def _annotate_recommended(slug: str, suggestions: list[dict]) -> list[dict]:
    """Tag each pending suggestion with `recommended` = the best-fit linked
    collection (so the Add picker defaults to it)."""
    for s in suggestions:
        s["recommended"] = topic_view.recommend_collection(
            slug, s.get("title", ""), s.get("abstract", ""))
    return suggestions


@app.get("/t/{slug}", response_class=HTMLResponse)
def topic_page(request: Request, slug: str) -> HTMLResponse:
    t = topics_mod.get_topic(slug)
    if not t:
        raise HTTPException(status_code=404, detail="Research topic not found")
    # Observe the generation job: render the overlay while running; on a finished
    # job clear it once and surface a one-time error banner on failure.
    gen_job = topic_view.get_generate_job(slug)
    gen_running = bool(gen_job and gen_job.get("status") == "running")
    gen_error = None
    if gen_job and gen_job.get("status") in ("done", "failed"):
        if gen_job["status"] == "failed":
            gen_error = gen_job.get("error")
        topic_view.clear_generate_job(slug)
    # Suggested-reading job (separate overlay on the reading tab).
    rjob = topic_view.get_reading_job(slug)
    reading_running = bool(rjob and rjob.get("status") == "running")
    reading_error = None
    if rjob and rjob.get("status") in ("done", "failed"):
        if rjob["status"] == "failed":
            reading_error = rjob.get("error")
        topic_view.clear_reading_job(slug)
    thread_id = get_or_create_thread(f"topic:{t['slug']}", None)
    chat = [{"role": m["role"], "html": render_md(m["content"], ""), "images": m.get("images") or []}
            for m in get_messages(thread_id, limit=50) if m["role"] in ("user", "assistant")]
    topic_artifact = get_artifact(thread_id)

    ev = t["evidence"]
    stats = {"collections": len(t["collections"]),
             "evidence": sum(1 for e in ev if e["kind"] != "missing"),
             "hypotheses": len(t["hypotheses"]),
             "unknowns": len(t["unknowns"]),
             "experiments": len(t["experiments"])}
    # Connected collections (Section 6): name + paper count + a relevance bar
    # (share of this topic's grounded evidence drawn from that collection).
    from collections import Counter
    evc = Counter(e["collection"] for e in ev if e["collection"])
    allc = {c["slug"]: c for c in library.list_collections(with_activity=True)}
    max_ev = max([1] + list(evc.values()))
    linked = []
    for cs in t["collections"]:
        c = allc.get(cs, {})
        linked.append({"slug": cs, "name": c.get("name", cs),
                       "papers": c.get("paper_count") or 0,
                       "evidence": evc.get(cs, 0),
                       "relevance_pct": round(100 * evc.get(cs, 0) / max_ev)})
    return templates.TemplateResponse(request, "topic.html", {
        "t": t, "sources": _topic_sources(t),
        "all_collections": library.list_collections(with_activity=True),
        "stats": stats, "linked": linked,
        "suggestions": _annotate_recommended(slug, topics_mod.list_suggestions(slug, "pending")),
        "basics_undo": topic_view.has_basics_undo(slug),
        "gen_running": gen_running,
        "gen_label": topic_view.gen_stage_label(gen_job) if gen_running else "",
        "gen_collections": (gen_job or {}).get("n_collections", 0) if gen_running else 0,
        "gen_error": gen_error,
        "reading_running": reading_running,
        "reading_error": reading_error,
        "chat": chat,
        "topic_artifact_html": render_md(topic_artifact, "") if topic_artifact else "",
        "model": load_config().get("model", ""),
    })


@app.post("/t/{slug}/generate")
def topic_generate(slug: str) -> RedirectResponse:
    """Kick off investigation generation on a background thread and redirect back
    immediately. The page renders the in-column overlay (it polls /generate/status)
    while the one LLM call runs. Backs Generate / Find evidence / Regenerate."""
    if not topics_mod.get_topic(slug):
        raise HTTPException(status_code=404, detail="Topic not found")
    topic_view.start_generate_async(slug)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.get("/t/{slug}/generate/status", response_class=JSONResponse)
def topic_generate_status(slug: str) -> JSONResponse:
    """Live state of the slug's generation job for the overlay to poll."""
    job = topic_view.get_generate_job(slug)
    if not job:
        return JSONResponse({"status": "idle"})
    return JSONResponse({"status": job.get("status", "running"),
                         "stage": job.get("stage", "gathering"),
                         "label": topic_view.gen_stage_label(job),
                         "n_collections": job.get("n_collections", 0),
                         "error": job.get("error")})


@app.post("/t/{slug}/analyze")
def topic_analyze(slug: str) -> RedirectResponse:
    """Anchor the question to existing ideas + name missing ones (one LLM call,
    cached). PRG back to the page (re-render uses the cache, no LLM)."""
    try:
        topic_view.analyze(slug)
    except Exception:  # noqa: BLE001
        logging.getLogger("paper_agent.topics").exception("topic analyze failed")
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/questions/suggest")
def topic_suggest_questions(slug: str) -> RedirectResponse:
    try:
        topic_view.suggest_questions(slug)
    except Exception:  # noqa: BLE001
        logging.getLogger("paper_agent.topics").exception("topic suggest-questions failed")
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/chat/stream")
def topic_chat_stream(slug: str, message: str = Form("")):
    """Streamed topic-assistant turn (NDJSON token/done/error). Grounded in the
    topic; read-only. Thread keyed 'topic:<slug>'; persists only on success."""
    t = topics_mod.get_topic(slug)
    if not t:
        raise HTTPException(status_code=404, detail="Topic not found")
    message = message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Empty message")
    thread_id = get_or_create_thread(f"topic:{slug}", None)
    history = get_messages(thread_id, limit=10)
    messages = topic_view.chat_messages(slug, history, message)
    refs = [{"type": "topic", "id": slug}]

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
            logging.getLogger("paper_agent.topics").exception("topic chat stream failed")
            yield _ev({"type": "error", "text": f"LLM call failed: {exc}"}); return
        add_message(thread_id, "user", message, refs)
        add_message(thread_id, "assistant", acc, refs)
        yield _ev({"type": "done"})

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/t/{slug}/chat", response_class=HTMLResponse)
def topic_chat(request: Request, slug: str, message: str = Form(""),
               images_json: str = Form(""), ref_json: str = Form("")) -> HTMLResponse:
    """Agentic topic-assistant turn (mirrors the collection chat): grounded in the
    topic, read-only tools scoped to its primary collection, with pasted images + card
    reference chips. Renders the same _chat_turn.html. Persists only on success."""
    t = topics_mod.get_topic(slug)
    if not t:
        raise HTTPException(status_code=404, detail="Topic not found")
    images = _parse_images(images_json)
    ref = None
    if ref_json:
        try:
            ref = json.loads(ref_json)
        except (ValueError, TypeError):
            ref = None
    # A card reference chip → name the section in the agent's question; the chat shows
    # only the clean chip. The topic agent already has the full investigation in context.
    if ref and ref.get("label"):
        lbl = ref.get("label", "")
        user_text = (f"Regarding the “{lbl}” of this topic: {message.strip()}" if message.strip()
                     else f"Tell me about the “{lbl}” of this topic.")
        original = _ref_display(ref, message)
    else:
        user_text = original = message

    thread_id = get_or_create_thread(f"topic:{slug}", None)
    history = get_messages(thread_id, limit=10)
    mcp_slug = (t.get("collections") or [None])[0]
    error, assistant_text = None, ""
    try:
        msgs = topic_view.chat_messages(slug, history, user_text)
        assistant_text = agentic_chat.answer_topic(msgs, mcp_slug, images=images)
        add_message(thread_id, "user", original, [{"type": "topic", "id": slug}], images=images)
        add_message(thread_id, "assistant", assistant_text, [])
    except llm.LLMError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("paper_agent.topics").exception("topic chat failed")
        error = f"Chat failed: {exc}"
    return templates.TemplateResponse(
        request, "_chat_turn.html",
        {"slug": slug, "user_html": render_md(original, slug), "user_images": images,
         "assistant_html": render_md(assistant_text, slug) if assistant_text else "",
         "assistant_text": assistant_text, "error": error, "suggestion": None,
         "usage": llm.usage(), "agentic": True},
    )


@app.post("/t/{slug}/chat/compact")
def topic_chat_compact(slug: str) -> RedirectResponse:
    """Compact the topic chat into a summary artifact, then clear history (mirrors the
    collection chat). History is only cleared if summarization succeeds."""
    thread_id = get_or_create_thread(f"topic:{slug}", None)
    live = [m for m in get_messages(thread_id) if m["role"] in ("user", "assistant")]
    if not live:
        return RedirectResponse(f"/t/{slug}", status_code=303)
    parts = []
    prior = get_artifact(thread_id)
    if prior:
        parts.append(f"PREVIOUS SUMMARY (already compacted):\n{prior}")
    for m in live:
        parts.append(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}")
    try:
        summary = llm.complete([
            {"role": "system", "content":
                "You compact a research-assistant conversation into a concise 'artifact' — a "
                "faithful briefing that preserves the USER's questions, key findings, decisions, "
                "open threads, and referenced papers/claims. It replaces the chat history and "
                "becomes your memory. Don't invent. Compact markdown, short headed sections."},
            {"role": "user", "content": "\n\n".join(parts)}])
    except Exception:  # noqa: BLE001 - never destroy history on a failed compaction
        logging.getLogger("paper_agent.topics").exception("topic chat compaction failed")
        return RedirectResponse(f"/t/{slug}", status_code=303)
    clear_messages(thread_id)
    add_message(thread_id, "system", summary)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/chat/delete")
def topic_chat_delete(slug: str) -> RedirectResponse:
    """Remove the topic chat (history + any artifact). A fresh thread is created next load."""
    delete_thread(get_or_create_thread(f"topic:{slug}", None))
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/status")
def topic_status(slug: str, status: str = Form("")) -> RedirectResponse:
    topics_mod.set_status(slug, status)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/lifecycle")
def topic_lifecycle(slug: str, lifecycle: str = Form("")) -> RedirectResponse:
    """v2 lifecycle: exploration / investigation / active / archived."""
    topics_mod.set_lifecycle(slug, lifecycle)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/edit")
def topic_edit(slug: str, question: str = Form(""), description: str = Form("")) -> RedirectResponse:
    topics_mod.update_basics(slug, question=question, description=description)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/question/propose", response_class=HTMLResponse)
def topic_question_propose(request: Request, slug: str, instruction: str = Form("")) -> HTMLResponse:
    """Agent proposes a question/description revision; returns the diff fragment."""
    res = topic_view.propose_basics_edit(slug, instruction)
    rows = []
    if res.get("ok"):
        cur, prop = res["current"], res["proposed"]
        rows = [{"label": "Research question", "field": "question",
                 "before": cur.get("question", ""), "after": prop.get("question", "")},
                {"label": "Description", "field": "description",
                 "before": cur.get("description", ""), "after": prop.get("description", "")}]
    return templates.TemplateResponse(request, "_section_edit_diff.html", {
        "error": None if res.get("ok") else res.get("error"), "rows": rows,
        "apply_hx": False, "apply_action": f"/t/{slug}/question/apply"})


@app.post("/t/{slug}/question/apply")
def topic_question_apply(slug: str, question: str = Form(""),
                         description: str = Form("")) -> RedirectResponse:
    topic_view.apply_basics_edit(slug, question, description)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/question/undo")
def topic_question_undo(slug: str) -> RedirectResponse:
    topic_view.undo_basics_edit(slug)
    return RedirectResponse(f"/t/{slug}", status_code=303)


# --- topic suggested reading (purpose-driven external discovery) ------------
@app.post("/t/{slug}/reading/suggest")
def topic_reading_suggest(slug: str, purpose: str = Form("broaden"), target: str = Form(""),
                          custom: str = Form(""), deep: str = Form(""),
                          months: str = Form("")) -> RedirectResponse:
    """Kick off suggested-reading discovery on a background thread; the reading
    pane renders an overlay that polls /reading/status. Redirect is immediate.
    ``deep`` ('1') routes to the tool-using paper-finder sub-agent; ``months`` caps
    out papers older than that recency window."""
    if topics_mod.get_topic(slug):
        tgt = int(target) if (target or "").strip().isdigit() else None
        topic_view.start_reading_async(slug, purpose=purpose, target_id=tgt, custom=custom,
                                       deep=(deep == "1"), since=_since_from_months(months))
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.get("/t/{slug}/reading/status", response_class=JSONResponse)
def topic_reading_status(slug: str) -> JSONResponse:
    job = topic_view.get_reading_job(slug)
    if not job:
        return JSONResponse({"status": "idle"})
    return JSONResponse({"status": job.get("status", "running"),
                         "added": job.get("added", 0), "error": job.get("error")})


@app.post("/t/{slug}/reading/{sid}/accept")
def topic_reading_accept(slug: str, sid: int, collection: str = Form(""),
                         new_name: str = Form("")) -> RedirectResponse:
    topics_mod.accept_suggestion(slug, sid, collection, new_name)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/reading/{sid}/dismiss")
def topic_reading_dismiss(slug: str, sid: int) -> RedirectResponse:
    topics_mod.dismiss_suggestion(slug, sid)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/evidence/{eid}/verify")
def topic_evidence_verify(slug: str, eid: int) -> RedirectResponse:
    topics_mod.verify_evidence(slug, eid)
    return RedirectResponse(f"/t/{slug}", status_code=303)


# --- v2 inquiry-list CRUD (assumptions / unknowns / experiments / notes / evidence) ---
@app.post("/t/{slug}/assumptions")
def topic_add_assumption(slug: str, text: str = Form("")) -> RedirectResponse:
    topics_mod.add_assumption(slug, text)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/assumptions/{aid}/delete")
def topic_del_assumption(slug: str, aid: int) -> RedirectResponse:
    topics_mod.delete_assumption(slug, aid)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/unknowns")
def topic_add_unknown(slug: str, text: str = Form(""), priority: str = Form("medium")) -> RedirectResponse:
    topics_mod.add_unknown(slug, text, priority)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/unknowns/{uid}/delete")
def topic_del_unknown(slug: str, uid: int) -> RedirectResponse:
    topics_mod.delete_unknown(slug, uid)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/unknowns/{uid}/status")
def topic_set_unknown(slug: str, uid: int, status: str = Form("")) -> RedirectResponse:
    topics_mod.set_unknown(slug, uid, status=status)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/experiments")
def topic_add_experiment(slug: str, title: str = Form(""), method: str = Form(""),
                         metric: str = Form("")) -> RedirectResponse:
    topics_mod.add_experiment(slug, title, method, metric)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/experiments/{eid}/delete")
def topic_del_experiment(slug: str, eid: int) -> RedirectResponse:
    topics_mod.delete_experiment(slug, eid)
    return RedirectResponse(f"/t/{slug}", status_code=303)


def _experiment_popup(request: Request, slug: str, eid: int) -> HTMLResponse:
    """Render the experiment detail popup body (log result + Analyze + analysis)."""
    t = topics_mod.get_topic(slug)
    x = topics_mod.get_experiment(slug, eid)
    hyp, hyp_idx = None, None
    if x and x.get("hypothesis_id") and t:
        for i, h in enumerate(t["hypotheses"], 1):
            if h.get("id") == x["hypothesis_id"]:
                hyp, hyp_idx = h, i
                break
    analysis_html = render_md(x["analysis"], "") if (x and x.get("analysis")) else ""
    return templates.TemplateResponse(request, "_experiment_detail.html",
                                      {"slug": slug, "x": x, "hyp": hyp, "hyp_idx": hyp_idx,
                                       "analysis_html": analysis_html})


@app.get("/t/{slug}/experiment/{eid}", response_class=HTMLResponse)
def topic_experiment_detail(request: Request, slug: str, eid: int) -> HTMLResponse:
    return _experiment_popup(request, slug, eid)


@app.post("/t/{slug}/experiments/{eid}/result", response_class=HTMLResponse)
def topic_experiment_result(request: Request, slug: str, eid: int,
                            result: str = Form(""), status: str = Form("")) -> HTMLResponse:
    """Log the experiment's result (and optionally bump status); re-render the popup."""
    topics_mod.set_experiment(slug, eid, result=result, status=(status or None))
    return _experiment_popup(request, slug, eid)


@app.post("/t/{slug}/experiments/{eid}/analyze", response_class=HTMLResponse)
def topic_experiment_analyze(request: Request, slug: str, eid: int) -> HTMLResponse:
    """Agent reasons whether the logged result supports the hypothesis + suggests the next
    step; stores the analysis and re-renders the popup."""
    topic_view.analyze_experiment(slug, eid)
    return _experiment_popup(request, slug, eid)


@app.post("/t/{slug}/notes")
def topic_add_note(slug: str, body: str = Form("")) -> RedirectResponse:
    topics_mod.add_note(slug, body)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/notes/{nid}/delete")
def topic_del_note(slug: str, nid: int) -> RedirectResponse:
    topics_mod.delete_note(slug, nid)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.get("/t/{slug}/hypothesis/{hid}", response_class=HTMLResponse)
def topic_hypothesis_detail(request: Request, slug: str, hid: str) -> HTMLResponse:
    """Hypothesis detail popup: the claim + its supporting/counter evidence + its
    unknowns (each with Find-papers). hid='unlinked' gathers evidence/unknowns with no
    hypothesis (the catch-all card)."""
    t = topics_mod.get_topic(slug)
    if not t:
        return templates.TemplateResponse(request, "_hypothesis_detail.html",
                                          {"slug": slug, "hyp": None})
    if hid == "unlinked":
        hyp, hyp_idx = None, None
        evs = [e for e in t["evidence"] if not e.get("hypothesis_id")]
        unks = [u for u in t["unknowns"] if not u.get("hypothesis_id")]
        unlinked = True
    else:
        hid_i = int(hid) if hid.isdigit() else -1
        hyp, hyp_idx = None, None
        for i, h in enumerate(t["hypotheses"], 1):
            if h.get("id") == hid_i:
                hyp, hyp_idx = h, i
                break
        evs = [e for e in t["evidence"] if e.get("hypothesis_id") == hid_i]
        unks = [u for u in t["unknowns"] if u.get("hypothesis_id") == hid_i]
        unlinked = False
    return templates.TemplateResponse(request, "_hypothesis_detail.html",
                                      {"slug": slug, "hyp": hyp, "hyp_idx": hyp_idx,
                                       "evs": evs, "unks": unks, "unlinked": unlinked})


@app.post("/t/{slug}/evidence/{eid}/delete")
def topic_del_evidence(slug: str, eid: int) -> RedirectResponse:
    topics_mod.delete_evidence(slug, eid)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/collections")
def topic_collections(slug: str, collections: list[str] = Form([])) -> RedirectResponse:
    topics_mod.set_collections(slug, collections)
    return RedirectResponse(f"/t/{slug}", status_code=303)


@app.post("/t/{slug}/delete")
def topic_delete(slug: str, redirect: str = Form("/topics")) -> RedirectResponse:
    topics_mod.delete_topic(slug)
    dest = redirect if redirect in ("/", "/topics") else "/topics"
    return RedirectResponse(dest, status_code=303)


@app.post("/t/{slug}/duplicate")
def topic_duplicate(slug: str) -> RedirectResponse:
    """Clone a topic into a new independent one and open it."""
    new_slug = topics_mod.duplicate_topic(slug)
    return RedirectResponse(f"/t/{new_slug or slug}", status_code=303)


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
    semantic_scholar_api_key: str = Form(""),
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
            "semantic_scholar_api_key": semantic_scholar_api_key.strip(),
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
            out.append({"role": m["role"], "html": render_md(m["content"], slug),
                        "images": m.get("images") or []})
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


@app.post("/c/{slug}/summary")
def collection_summary(slug: str, summary: str = Form("")) -> RedirectResponse:
    _require_collection(slug)
    library.set_summary(slug, summary.strip())
    return RedirectResponse(f"/c/{slug}", status_code=303)


@app.post("/c/{slug}/delete")
def collection_delete(slug: str) -> RedirectResponse:
    col = _require_collection(slug)
    # Guard: a research topic uses this collection as evidence — deleting it would
    # orphan that investigation. Refuse; the user must unlink it from the topic(s) first.
    n = topics_mod.collection_usage().get(slug, 0)
    if n:
        raise HTTPException(
            status_code=409,
            detail=(f"Can't delete “{col['name']}”: it's linked to {n} research topic"
                    f"{'s' if n != 1 else ''} as evidence. Unlink it from "
                    f"{'those topics' if n != 1 else 'that topic'} (topic → Manage collections) first."))
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
    # Land back on the Papers tab (where Add paper was invoked), not Overview.
    return RedirectResponse(f"/c/{slug}?tab=papers", status_code=303)


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


@app.post("/c/{slug}/wiki/papers/remove", response_class=HTMLResponse)
def wiki_papers_remove(request: Request, slug: str, paper_ids: list[int] = Form(default=[])) -> HTMLResponse:
    """Remove the selected papers from the wiki Papers tab (multi-select). Stages removal
    (→ Graveyard, restorable; applied to Zotero on next Sync) and re-renders the panel."""
    _require_collection(slug)
    for pid in paper_ids:
        library.stage_removal(slug, pid)
    return _wiki_panel(request, slug)


def _tags_editor(request: Request, slug: str, paper_id: int) -> HTMLResponse:
    """The per-paper tag editor body: concept/method/problem entities with the paper's current
    membership checked. Toggling persists an override (build_collection_graph applies it)."""
    cv = wiki.connection_view(slug) or {}
    ents = cv.get("entities", {}) or {}
    current = set((cv.get("paper_entities", {}) or {}).get(paper_id, []))
    paper = library.get_collection_paper(slug, paper_id) or {}
    groups = [("concept", "Concepts"), ("method", "Methods"), ("problem", "Problems")]
    tags = {k: [{"key": e["key"], "label": e["label"], "on": e["key"] in current}
                for e in ents.get(k, [])] for k, _ in groups}
    return templates.TemplateResponse(request, "_tags_editor.html",
                                      {"slug": slug, "paper_id": paper_id,
                                       "title": paper.get("title", ""), "groups": groups, "tags": tags})


@app.get("/c/{slug}/wiki/paper/{paper_id}/tags", response_class=HTMLResponse)
def wiki_paper_tags(request: Request, slug: str, paper_id: int) -> HTMLResponse:
    _require_collection(slug)
    return _tags_editor(request, slug, paper_id)


@app.post("/c/{slug}/wiki/paper/{paper_id}/tags/toggle", response_class=HTMLResponse)
def wiki_paper_tag_toggle(request: Request, slug: str, paper_id: int,
                          entity_key: str = Form("")) -> HTMLResponse:
    """Toggle the paper's membership of an entity (concept/method/problem) and re-render the
    editor. `present` is read live from the graph so the override flips the real state."""
    _require_collection(slug)
    cv = wiki.connection_view(slug) or {}
    present = entity_key in set((cv.get("paper_entities", {}) or {}).get(paper_id, []))
    if entity_key:
        wiki.toggle_paper_entity(slug, paper_id, entity_key, present)
    return _tags_editor(request, slug, paper_id)


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
            # A staged auto-draft awaiting review. Additive (appends) when a note already
            # exists; a full draft (replaces an empty note) otherwise.
            "staged_draft": note_drafts.get(slug, paper_id),
            "staged_draft_additive": bool(note_md.strip()),
            "staged_draft_at": note_drafts.staged_at(slug, paper_id),
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
    ref_json: str = Form(""),
) -> HTMLResponse:
    col = _require_collection(slug)

    message = message.strip()
    images = _parse_images(images_json)
    if not message and not images and not ref_json:    # a card reference alone is a valid turn
        raise HTTPException(status_code=400, detail="Empty message")

    pid = int(paper_key) if paper_key else None
    thread_id = get_or_create_thread(slug, pid)
    history = get_messages(thread_id, limit=10)

    # /{collection-slug} routes the turn through the agent with read-only MCP tools
    # (AGENTIC_PLAN P6). Checked before the legacy /collection /wiki literals.
    prefix, remainder = agentic_chat.parse_prefix(message)
    if prefix == "help":            # static command reference, no LLM call
        return _chat_help_turn(request, slug, message)
    if prefix in ("thought", "find", "gaps"):    # quick commands (capture / discover)
        return _chat_command_turn(request, slug, prefix, remainder, message,
                                  paper_key=(str(pid) if pid else ""))
    if prefix == "belief":          # propose ONE belief, grounded by the agent
        instr = ("Propose exactly ONE belief capturing this claim, and cite supporting papers "
                 f"from the collection to ground it: {remainder}" if (remainder or "").strip()
                 else "Propose one belief that the recent conversation supports, with citations.")
        return _agentic_chat_turn(request, slug, thread_id, slug, instr, message, mode="update")
    if prefix == "updatewiki":      # explicit: ask the agent to propose wiki edits now
        return _agentic_chat_turn(request, slug, thread_id, slug, remainder, message, mode="update")

    # "Ask in chat" from a wiki card: a reference chip + (optional) user text. The agent
    # gets a verbose card-scoped question; the user only ever sees the clean chip.
    if ref_json and pid is None:
        try:
            ref = json.loads(ref_json)
        except (ValueError, TypeError):
            ref = None
        if ref and ref.get("label"):
            return _agentic_chat_turn(request, slug, thread_id, slug,
                                      _ref_instruction(ref, message, slug), _ref_display(ref, message),
                                      mode="answer", images=images)
    if prefix is not None:
        return _agentic_chat_turn(request, slug, thread_id, prefix, remainder, message)

    # /collection (or /wiki) folds the collection wiki into THIS paper turn. Handled here —
    # before the sub-agent dispatch — so the literal token never reaches a CLI session
    # (which would treat "/collection" as its own slash command → "Unknown command").
    include_collection = False
    if pid is not None:
        for token in ("/collection", "/wiki"):
            if token in message:
                include_collection = True
                message = message.replace(token, "").strip()

    # Interactive paper sub-agent (PAPER_CHAT_AGENT P8): when a paper with a cached PDF
    # is open, the turn goes to a persistent CLI session that reads the PDF itself.
    # Pasted images reach it via files (Claude Read / Codex -i).
    agent = paper_chat.get_agent(pid)
    if agent is not None:
        agent_message = message
        if include_collection:                       # prepend the wiki for the agent, keep the bubble clean
            wctx = context._wiki_overview(slug)
            if wctx:
                agent_message = (f"Here is this paper's collection wiki, for context:\n\n{wctx}\n\n"
                                 f"Now, about THIS paper: {message}")
        return _paper_subagent_turn(request, slug, col["name"], thread_id, pid, message, agent,
                                    images, agent_message=agent_message)

    # The collection side-chat is AGENTIC by default: a tool-using agent that reads
    # the live wiki/notes, may propose wiki edits (gated by the per-collection toggle),
    # and can view pasted images (materialized to temp files + the Read tool).
    if pid is None:
        return _agentic_chat_turn(request, slug, thread_id, slug, message, message,
                                  mode="answer", images=images)

    # include_collection (the /collection or /wiki token) was resolved above.
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
        add_message(thread_id, "user", stored or "(image)", refs, images=images)
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


def _paper_subagent_turn(request, slug, name, thread_id, paper_id, message, agent, images=None,
                         agent_message=None):
    """One turn of the interactive paper sub-agent (reads the PDF + the user's notes via
    a persistent CLI session). Read-only; nothing is saved to notes/wiki here.

    ``message`` is what the user sees/stores; ``agent_message`` (defaults to it) is what
    the agent receives — they differ when /collection prepends the wiki for context."""
    paper = library.get_paper(paper_id)
    title = paper["title"] if paper else str(paper_id)
    error, assistant_text = None, ""
    stored = message + (f"\n\n_({len(images)} image{'s' if len(images) != 1 else ''} attached)_"
                        if images else "")
    try:
        res = agent.answer(slug, name, thread_id, paper_id, title,
                           agent_message if agent_message is not None else message, images)
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


_CHAT_HELP_MD = (
    "**Chat commands**\n\n"
    "- `/help` — show this list.\n"
    "- `/thought <text>` — save a note to your **thought stream** (your words).\n"
    "- `/find [focus]` — find external papers (**Suggested reading**); bare = related work, "
    "with text = targeted. Results land in the Suggested reading tab.\n"
    "- `/gaps` — find papers that fill your wiki's **open questions / gaps**.\n"
    "- `/belief <claim>` — **propose a belief** from a claim; lands as an inline card you Accept/Dismiss.\n"
    "- `/updatewiki [instruction]` — propose wiki edits now (inline Accept/Dismiss).\n"
    "- `/<collection-slug> <question>` — ask the agent about a *different* collection (read-only).\n\n"
    "A plain message is answered for **this** collection — it reads your wiki, notes, and papers, "
    "and may propose wiki edits (toggle that in the ⋯ menu)."
)


def _chat_static_turn(request, slug, original, reply_md):
    """Render a command result as a chat turn (no LLM call)."""
    return templates.TemplateResponse(
        request, "_chat_turn.html",
        {"slug": slug, "user_html": render_md(original, slug), "user_images": [],
         "assistant_html": render_md(reply_md, slug), "error": None,
         "suggestion": None, "usage": llm.usage(), "agentic": False},
    )


def _chat_help_turn(request, slug, original):
    return _chat_static_turn(request, slug, original, _CHAT_HELP_MD)


def _ref_phrase(ref: dict) -> str:
    kind, label = ref.get("kind", ""), ref.get("label", "")
    return {"concept": f"the concept “{label}”", "belief": f"the belief “{label}”",
            "theme": f"the theme “{label}”", "thesis": "the collection's thesis",
            "landscape": f"the “{label}” column of the research landscape",
            }.get(kind, f"“{label}”")


def _ref_context(slug: str, ref: dict) -> str:
    """The actual on-page content for a card reference, so the agent answers from what
    the user is looking at instead of only the section's name (it can still use its
    tools to go deeper). Best-effort; '' if nothing resolves."""
    kind, label = ref.get("kind", ""), ref.get("label", "")
    try:
        ov = wiki.load_overview(slug) or {}
    except Exception:  # noqa: BLE001
        return ""
    if kind == "thesis":
        t = ov.get("thesis") or {}
        bits = [t.get("one_paragraph", "")]
        for k in ("core_tension", "key_intuition", "central_question"):
            if t.get(k):
                bits.append(f"{k.replace('_', ' ').title()}: {t[k]}")
        return "\n".join(b for b in bits if b)
    if kind == "landscape":
        col = {"problems": "problems", "methods": "methods", "debates": "debates",
               "open questions": "open_questions"}.get(label.lower())
        items = (ov.get("landscape") or {}).get(col or "", []) if col else []
        return "\n".join(f"- {it['text'] if isinstance(it, dict) else it}" for it in items)
    if kind == "concepts" or kind == "concept":
        for c in ov.get("concepts") or []:
            if c.get("name") == label:
                return f"{c['name']}: {c.get('blurb', '')}"
    if kind == "belief":
        for b in ov.get("beliefs") or []:
            if b.get("title") == label:
                return b["title"]
    if kind == "theme":
        for t in (ov.get("connections") or {}).get("themes", []):
            if (t.get("name") or f"Theme {t['index']}") == label:
                return f"{label}: {t.get('description', '')}"
    return ""


def _ref_instruction(ref: dict, user_msg: str, slug: str = "") -> str:
    """The (verbose, hidden) question the agent receives for a card reference —
    includes the section's actual current content so the agent answers from what the
    user is looking at."""
    phrase = _ref_phrase(ref)
    content = _ref_context(slug, ref) if slug else ""
    ctx = f"\n\nThis is its current content:\n{content}\n" if content else ""
    if (user_msg or "").strip():
        return f"Regarding {phrase} in this collection:{ctx}\n{user_msg.strip()}"
    base = {
        "concept": f"Explain {phrase} in this collection — what it means and which papers ground it.",
        "concepts": f"Explain {phrase} in this collection — what it means and which papers ground it.",
        "belief": f"Lay out the evidence for and against {phrase}, citing papers in the collection.",
        "theme": f"Summarize {phrase} — the ideas it groups and how its papers connect.",
        "thesis": "Walk me through the collection's thesis — its core tension, key intuition, "
                  "and central question — and how the papers support it.",
        "landscape": f"Walk me through {phrase} and the papers behind them.",
    }.get(ref.get("kind", ""), f"Tell me about {phrase} in this collection.")
    return f"{base}{ctx}" if ctx else base


def _ref_display(ref: dict, user_msg: str) -> str:
    """The clean message shown to the user (the chip + their words), not the verbose instruction."""
    chip = f"{ref.get('icon', '◆')} {ref.get('label', '')}".strip()
    return f"**{chip}** — {user_msg.strip()}" if (user_msg or "").strip() else f"**{chip}**"


def _chat_command_turn(request, slug, prefix, remainder, original, paper_key: str = ""):
    """Handle the quick (non-agentic) slash commands: /thought, /find, /gaps."""
    text = (remainder or "").strip()
    if prefix == "thought":
        if not text:
            return _chat_static_turn(request, slug, original,
                                     "Usage: `/thought <text>` — saves a note to your thought stream.")
        thoughts_mod.create_thought(slug, text, synth_kind="reasoning", author_origin="human",
                                    paper_key=paper_key or None)
        where = "this paper's notes" if paper_key else "your **thought stream**"
        return _chat_static_turn(request, slug, original, f"✓ Saved to {where}.")
    if prefix == "find":
        if paper_key:                       # in a paper chat → find papers SIMILAR to this paper
            wiki.start_reading_async(slug, purpose="similar", target=paper_key, custom=text)
            what = "similar to this paper" + (f", focused on “{text}”" if text else "")
        elif text:
            wiki.start_reading_async(slug, purpose="custom", custom=text)
            what = f"matching “{text}”"
        else:
            wiki.start_reading_async(slug, purpose="related")
            what = "related to this collection"
        return _chat_static_turn(request, slug, original,
                                 f"🔎 Searching arXiv for papers {what}… results will appear in the "
                                 "**Suggested reading** tab when ready.")
    if prefix == "gaps":
        wiki.start_reading_async(slug, purpose="gaps")
        return _chat_static_turn(request, slug, original,
                                 "🔎 Looking for papers that fill your wiki's open questions / gaps… "
                                 "they'll appear in the **Suggested reading** tab.")
    return _chat_static_turn(request, slug, original, "Unknown command. Try `/help`.")


def _agentic_chat_turn(request, slug, thread_id, prefix, remainder, original, mode="answer",
                       images=None):
    """A /{collection} turn (or /updatewiki when mode='update'): answer via the agent
    with read-only MCP tools + the gated wiki proposer. Pasted ``images`` are shown to
    the agent (temp files + Read tool). Any wiki proposals the agent creates this turn
    are surfaced as inline Accept/Dismiss cards."""
    images = images or []
    slugs = {c["slug"] for c in library.list_collections()}
    if mode != "update" and prefix not in slugs:
        avail = ", ".join(sorted(slugs)) or "(none yet)"
        reply = f"No collection `/{prefix}`. Available: {avail}"
        return templates.TemplateResponse(
            request, "_chat_turn.html",
            {"slug": slug, "user_html": render_md(original, slug), "user_images": [],
             "assistant_html": render_md(reply, slug), "error": None,
             "suggestion": None, "usage": llm.usage(), "agentic": False},
        )
    history = get_messages(thread_id, limit=10)
    before = {p["id"] for p in wiki_propose.list_pending(prefix)}    # so we show only THIS turn's
    error, assistant_text = None, ""
    try:
        if mode == "update":
            assistant_text = agentic_chat.update_wiki(prefix, history, remainder)
        else:
            assistant_text = agentic_chat.answer(prefix, history, remainder or "(no question)",
                                                 images=images)
        add_message(thread_id, "user", original, [{"type": "collection", "id": prefix}], images=images)
        add_message(thread_id, "assistant", assistant_text, [])
    except llm.LLMError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("paper_agent.chat").exception("agentic chat failed")
        error = f"Agentic chat failed: {exc}"
    proposals = [p for p in wiki_propose.list_pending(prefix) if p["id"] not in before]
    return templates.TemplateResponse(
        request, "_chat_turn.html",
        {"slug": slug, "user_html": render_md(original, slug), "user_images": images,
         "assistant_html": render_md(assistant_text, slug) if assistant_text else "",
         "assistant_text": assistant_text, "error": error, "suggestion": None,
         "proposals": proposals,
         "usage": llm.usage(), "agentic": True, "capture_slug": prefix},
    )


@app.post("/c/{slug}/wiki/proposal/{pid}/accept", response_class=HTMLResponse)
def wiki_proposal_accept(slug: str, pid: int) -> HTMLResponse:
    """Apply a chat-proposed wiki edit (snapshot + re-validate). Returns an inline
    status; the card's hx-on refreshes the wiki panel."""
    _require_collection(slug)
    res = wiki_propose.accept_proposal(pid)
    if res.get("ok"):
        return HTMLResponse('<span class="text-emerald-700 text-xs font-medium">✓ Applied to the wiki</span>')
    return HTMLResponse(f'<span class="text-rose-700 text-xs">Couldn\'t apply: {res.get("error", "")}</span>')


@app.post("/c/{slug}/wiki/proposal/{pid}/dismiss", response_class=HTMLResponse)
def wiki_proposal_dismiss(slug: str, pid: int) -> HTMLResponse:
    _require_collection(slug)
    wiki_propose.dismiss_proposal(pid)
    return HTMLResponse('<span class="text-slate-400 text-xs">Dismissed</span>')


@app.post("/c/{slug}/wiki/proactive", response_class=HTMLResponse)
def wiki_set_proactive(slug: str, on: str = Form("")) -> HTMLResponse:
    """Per-collection toggle for proactive chat→wiki proposals."""
    _require_collection(slug)
    library.set_wiki_proactive(slug, on == "1")
    return HTMLResponse('on' if on == "1" else 'off')


@app.post("/c/{slug}/p/{paper_id}/find-similar", response_class=HTMLResponse)
def paper_find_similar(slug: str, paper_id: int) -> HTMLResponse:
    """Fire the discovery agent seeded by THIS paper (similar work, biased to the
    collection's focus). Results land in the collection's Suggested reading tab."""
    _require_collection(slug)
    if library.get_paper(paper_id) is None:
        raise HTTPException(status_code=404, detail="No paper")
    started = wiki.start_reading_async(slug, purpose="similar", target=str(paper_id))
    msg = ("🔎 Searching for similar papers… they'll appear in this collection's "
           "Suggested reading." if started else "A search is already running — check Suggested reading.")
    return HTMLResponse(f'<span class="text-emerald-700">{msg}</span>')


@app.post("/c/{slug}/p/{paper_id}/autodraft")
def paper_autodraft(slug: str, paper_id: int) -> Response:
    """Fire-and-forget (sendBeacon on leaving a paper): if the paper has chat or
    highlights, NO note yet, and no staged draft, background-draft a note from
    highlights+chat and stage it (inert) for review. Returns 204 immediately.

    Staged — NOT saved as a note (attribution: agent words become yours only on accept)."""
    try:
        col = library.get_collection(slug)
    except Exception:  # noqa: BLE001
        col = None
    paper = library.get_paper(paper_id)
    if not col or paper is None:
        return Response(status_code=204)

    def runner():
        try:
            note = notes_mod.get_note(slug, paper_id)
            has_note = any((note.get(k) or "").strip()
                           for k in ("summary", "thoughts", "key_quotes"))
            thread_id = get_or_create_thread(slug, paper_id)
            has_chat = thread_message_count(thread_id) > 0
            has_high = bool(context._highlights_block(slug, paper_id))
            if not (has_chat or has_high):
                return                       # no signal → no speculative LLM call
            # Watermark: (re)draft only when there's chat/highlight signal NEWER than whatever
            # is already there — the user's note OR a draft already queued for this paper. A
            # fresh draft then REPLACES the queued one (one draft per paper, always the latest);
            # if nothing changed since, keep what's queued and skip the LLM call. (Additive when
            # a note exists — net-new material only, never overwriting the user's own note.)
            draft_epoch = note_drafts.staged_epoch(slug, paper_id)
            draft_exists = draft_epoch > 0
            note_epoch = notes_mod.note_updated_epoch(slug, paper_id) if has_note else 0.0
            if (has_note or draft_exists) and \
                    _latest_signal_epoch(slug, paper_id, thread_id) <= max(note_epoch, draft_epoch):
                return
            # Past the cheap gates → the speculative LLM draft is about to run; surface it
            # in the Background Jobs dropdown until it finishes.
            _AUTODRAFT_JOBS[paper_id] = {
                "status": "running", "slug": slug,
                "label": "Drafting note additions" if has_note else "Drafting note"}
            try:
                fields, error = _draft_note_fields(
                    slug, paper_id, paper["title"], existing=note if has_note else None)
                if error or not fields:
                    return
                md = notes_mod._serialize_body(fields.get("summary", ""),
                                               fields.get("thoughts", ""),
                                               fields.get("key_quotes", ""))
                if md.strip():
                    note_drafts.stage(slug, paper_id, md)   # ON CONFLICT → replaces any queued draft
                    label = ("Draft refreshed" if draft_exists else
                             ("Draft additions ready" if has_note else "Draft ready to review"))
                    notify.add(f"{label}: {(paper['title'] or '')[:60]}",
                               link=f"/c/{slug}/p/{paper_id}", collection=slug)
            finally:
                _AUTODRAFT_JOBS.pop(paper_id, None)
        except Exception:  # noqa: BLE001
            _AUTODRAFT_JOBS.pop(paper_id, None)
            logging.getLogger("paper_agent.autodraft").exception("autodraft failed")

    threading.Thread(target=runner, daemon=True).start()
    return Response(status_code=204)


@app.post("/c/{slug}/p/{paper_id}/cite-lookup", response_class=JSONResponse)
def paper_cite_lookup(slug: str, paper_id: int, cite: str = Form("")) -> JSONResponse:
    """Resolve an in-text citation clicked in the PDF to a paper (via this paper's own
    reference list + arXiv). Returns JSON for the in-PDF popup; never adds."""
    _require_collection(slug)
    return JSONResponse(citations_mod.resolve_citation(slug, paper_id, cite))


@app.post("/c/{slug}/p/{paper_id}/cite-add", response_class=JSONResponse)
def paper_cite_add(slug: str, paper_id: int, arxiv_id: str = Form(""), title: str = Form(""),
                   authors: str = Form(""), year: str = Form(""),
                   abstract: str = Form("")) -> JSONResponse:
    """Add a citation's resolved arXiv paper to the collection (from the citation popup)."""
    _require_collection(slug)
    aid = (arxiv_id or "").strip()
    if not aid:
        return JSONResponse({"ok": False, "error": "no arxiv id"}, status_code=400)
    ids = triage_mod.add_entries(slug, [{"kind": "arxiv", "id": aid, "title": title,
                                         "authors": authors, "year": year, "abstract": abstract}])
    return JSONResponse({"ok": bool(ids), "added": len(ids)})


@app.post("/c/{slug}/p/{paper_id}/autodraft/discard", response_class=HTMLResponse)
def paper_autodraft_discard(slug: str, paper_id: int) -> HTMLResponse:
    """Discard a staged auto-draft (the user rejected it)."""
    note_drafts.delete(slug, paper_id)
    return HTMLResponse("")          # hx-swap removes the card


@app.post("/c/{slug}/p/{paper_id}/autodraft/accept", response_class=HTMLResponse)
def paper_autodraft_accept(slug: str, paper_id: int, text: str = Form("")) -> HTMLResponse:
    """Accept a staged auto-draft (possibly edited) as the paper's note, then drop the
    draft. The user reviewed/edited it here, so it's saved as their note. If a note already
    exists, the draft is APPENDED field-wise (never overwrites the user's own words)."""
    _require_collection(slug)
    fields = notes_mod._parse_body(text or "")
    existing = notes_mod.get_note(slug, paper_id)
    has_note = any((existing.get(k) or "").strip()
                   for k in ("summary", "thoughts", "key_quotes"))
    if has_note:
        summary = _append_field(existing.get("summary", ""), fields.get("summary", ""))
        thoughts = _append_field(existing.get("thoughts", ""), fields.get("thoughts", ""))
        key_quotes = _append_field(existing.get("key_quotes", ""), fields.get("key_quotes", ""))
    else:
        summary, thoughts, key_quotes = (fields.get("summary", ""), fields.get("thoughts", ""),
                                         fields.get("key_quotes", ""))
    notes_mod.save_note(slug, paper_id, summary, thoughts, key_quotes, "noted",
                        author_origin="human")
    note_drafts.delete(slug, paper_id)
    return HTMLResponse("")          # hx-swap removes the card


@app.get("/drafts/review", response_class=HTMLResponse)
def drafts_review(request: Request) -> HTMLResponse:
    """Modal body: all staged auto-drafts (across collections), each editable with
    Accept / Discard, plus Approve all."""
    items = []
    for d in note_drafts.list_all():
        p = library.get_paper(d["paper_id"])
        n = notes_mod.get_note(d["collection_slug"], d["paper_id"])
        additive = any((n.get(k) or "").strip() for k in ("summary", "thoughts", "key_quotes"))
        items.append({"slug": d["collection_slug"], "paper_id": d["paper_id"],
                      "title": ((p or {}).get("title") or f"paper {d['paper_id']}"),
                      "draft_md": d["draft_md"],
                      "draft_html": render_md(d["draft_md"], d["collection_slug"]),
                      "additive": additive, "created_at": d.get("created_at")})
    return templates.TemplateResponse(request, "_drafts_review.html", {"drafts": items})


@app.post("/c/{slug}/thoughts/capture", response_class=HTMLResponse)
def thoughts_capture(slug: str, agent_text: str = Form(""), your_take: str = Form(""),
                     paper_key: str = Form("")) -> HTMLResponse:
    """Attribution-safe capture from agentic chat: the agent's reply becomes a
    (seed, agent) thought (can't ground an assertion); an optional 'your take' becomes
    a (reasoning, human) thought linked to it via prompted_by. Anchors to a paper when
    captured from a paper chat (paper_key set)."""
    _require_collection(slug)
    agent_text = agent_text.strip()
    if not agent_text:
        raise HTTPException(status_code=400, detail="nothing to capture")
    pk = paper_key or None
    seed_id = thoughts_mod.create_thought(slug, agent_text, synth_kind="seed",
                                          author_origin="agent", paper_key=pk)
    take = your_take.strip()
    if take:
        thoughts_mod.create_thought(slug, take, synth_kind="reasoning",
                                    author_origin="human", prompted_by=seed_id, paper_key=pk)
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
    # Enrich the CURRENT collection so the modal can offer lightweight in-flow filters
    # (read status + theme/method membership). Other collections stay plain — the filters
    # are cognitive-model artifacts that only make sense within one collection.
    cur_read: dict[int, bool] = {}
    cur_ents: dict[int, list] = {}
    themes_opt: list[dict] = []
    concepts_opt: list[dict] = []
    methods_opt: list[dict] = []
    problems_opt: list[dict] = []
    if current:
        try:
            cv = wiki.connection_view(current)
            cur_ents = {int(k): v for k, v in (cv.get("paper_entities") or {}).items()}
            themes_opt = [{"key": f"theme:{t['index']}", "label": t.get("name") or f"Theme {t['index']}"}
                          for t in (cv.get("themes") or [])]
            ent = cv.get("entities") or {}
            concepts_opt = [{"key": e["key"], "label": e["label"]} for e in ent.get("concept", [])]
            methods_opt = [{"key": e["key"], "label": e["label"]} for e in ent.get("method", [])]
            problems_opt = [{"key": e["key"], "label": e["label"]} for e in ent.get("problem", [])]
        except Exception:  # noqa: BLE001 - filters are best-effort; the list still works
            pass
        con = connect()
        try:
            for r in con.execute("SELECT paper_id, read_at FROM collection_papers "
                                 "WHERE collection_slug=?", (current,)):
                cur_read[r["paper_id"]] = bool(r["read_at"])
        finally:
            con.close()
    groups = []
    for c in cols:
        is_cur = c["slug"] == current
        papers = []
        for p in library.list_papers(c["slug"]):
            row = {"id": p["id"], "title": p.get("title") or "(untitled)",
                   "authors": p.get("authors") or "", "year": p.get("year") or ""}
            if is_cur:
                row["read"] = cur_read.get(p["id"], False)
                row["ents"] = cur_ents.get(p["id"], [])
            papers.append(row)
        g = {"slug": c["slug"], "name": c["name"], "current": is_cur, "papers": papers}
        if is_cur:
            g["themes"], g["concepts"] = themes_opt, concepts_opt
            g["methods"], g["problems"] = methods_opt, problems_opt
        groups.append(g)
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
    note_drafts.delete(slug, paper_id)     # saving a note consumes any staged auto-draft
    return RedirectResponse(f"/c/{slug}/p/{paper_id}/notes", status_code=303)


def _draft_note_fields(slug: str, paper_id: int, paper_title: str,
                       existing: dict | None = None) -> tuple[dict, str | None]:
    """Draft note fields from this paper's highlights + its chat history.

    Returns ({summary, thoughts, key_quotes}, error). Never saves. When ``existing`` is a
    non-empty note, drafts ONLY net-new material the note doesn't already capture (additive
    mode) — empty fields mean nothing new to add, and ({}, None) is returned if the whole
    draft would be empty.
    """
    thread_id = get_or_create_thread(slug, paper_id)  # the paper's own thread
    history = get_messages(thread_id, limit=12)
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history) or "(no chat yet)"
    highlights = context._highlights_block(slug, paper_id) or "(no highlights yet)"
    has_existing = bool(existing and any((existing.get(k) or "").strip()
                                         for k in ("summary", "thoughts", "key_quotes")))
    if has_existing:
        note_block = notes_mod._serialize_body(existing.get("summary", ""),
                                               existing.get("thoughts", ""),
                                               existing.get("key_quotes", ""))
        system = ("You output only valid JSON with keys summary, thoughts, key_quotes. The "
                  "user ALREADY has a note on this paper (below). From their highlights and "
                  "chat, draft ONLY net-new material the existing note does NOT already "
                  "capture — points to ADD, not a rewrite. Do not repeat or rephrase what's "
                  "already there; do not invent beyond the highlights/chat. Leave a field as "
                  '"" if there is nothing new for it.')
        user = (f"Paper: {paper_title}\n\nEXISTING NOTE:\n{note_block}\n\n"
                f"Highlights:\n{highlights}\n\nChat:\n{convo}\n\n"
                'Return only the additions: {"summary": "...", "thoughts": "...", "key_quotes": "- ..."}')
    else:
        system = ("You output only valid JSON with keys summary, thoughts, key_quotes. Draft "
                  "the USER's note from their highlights and chat about this paper; do not "
                  "invent content beyond them.\n"
                  "`summary` must be ORGANIZED, not a long paragraph: one short TL;DR line "
                  "(<=25 words) naming the paper, a blank line, then EXACTLY these three "
                  "markdown lines, each one sentence:\n"
                  "**Problem:** the gap/problem this paper sets out to solve.\n"
                  "**Method:** the core approach it proposes.\n"
                  "**Insight:** the key takeaway / why it works.\n"
                  "Put the user's own reactions or criticisms in `thoughts`, and notable "
                  "quotes in `key_quotes` (markdown list). If the highlights/chat don't "
                  "support a field, write 'unclear' rather than inventing.")
        user = (f"Paper: {paper_title}\n\nHighlights:\n{highlights}\n\nChat:\n{convo}\n\n"
                'Return JSON. Example summary value: "Short TL;DR line.\\n\\n'
                '**Problem:** ...\\n**Method:** ...\\n**Insight:** ..."')
    try:
        resp = llm.complete([{"role": "system", "content": system},
                             {"role": "user", "content": user}])
        data = json.loads(resp[resp.find("{"): resp.rfind("}") + 1])

        def _coerce(v, bullet=False):
            # The model sometimes returns a field as a JSON list (esp. key_quotes); normalize
            # to a string so _serialize_body / save_note never see a non-str.
            if isinstance(v, list):
                items = [str(x).strip() for x in v if str(x).strip()]
                return "\n".join((f"- {i.lstrip('- ').strip()}" if bullet else i) for i in items)
            return str(v) if v is not None else ""

        fields = {"summary": _coerce(data.get("summary")),
                  "thoughts": _coerce(data.get("thoughts")),
                  "key_quotes": _coerce(data.get("key_quotes"), bullet=True)}
        if has_existing and not any(v.strip() for v in fields.values()):
            return ({}, None)            # nothing new to add
        return (fields, None)
    except llm.LLMError as exc:
        return ({}, str(exc))
    except Exception as exc:  # noqa: BLE001
        return ({}, f"Draft failed: {exc}")


def _latest_signal_epoch(slug: str, paper_id: int, thread_id: int) -> float:
    """Newest chat-message / highlight timestamp for a paper (UTC epoch). The watermark
    for deciding whether there's new signal since the note was last updated."""
    con = connect()
    try:
        msg = con.execute("SELECT MAX(created_at) AS t FROM chat_messages WHERE thread_id=?",
                          (thread_id,)).fetchone()["t"]
        hl = con.execute("SELECT MAX(created_at) AS t FROM annotations WHERE paper_id=? AND "
                         "collection_slug=?", (paper_id, slug)).fetchone()["t"]
    finally:
        con.close()
    return max(notes_mod._iso_to_epoch(msg), notes_mod._iso_to_epoch(hl))


def _append_field(existing: str, addition: str) -> str:
    """Non-destructively append an addition to an existing note field (blank-line join)."""
    existing, addition = (existing or "").rstrip(), (addition or "").strip()
    if not addition:
        return existing
    return f"{existing}\n\n{addition}".strip() if existing else addition


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


def _thoughts_panel_response(request: Request, slug: str, paper_key: str = "",
                             panel_id: str = "thoughts-panel") -> HTMLResponse:
    """Render the Thoughts tab fragment (bodies pre-rendered to markdown). ``paper_key``
    scopes it to one paper; ``panel_id`` lets the 'manage all' modal copy coexist."""
    items = thoughts_mod.list_thoughts(slug, paper_key=paper_key or None)
    _titles: dict[str, str] = {}
    for t in items:
        t["body_html"] = render_md(t["body"], slug)
        pk = (t.get("paper_key") or "").strip()
        if pk and pk.isdigit():                       # resolve the anchored paper's title (cached)
            if pk not in _titles:
                p = library.get_paper(int(pk))
                _titles[pk] = (p or {}).get("title", "") if p else ""
            if _titles[pk]:
                t["paper_title"] = _titles[pk]
                t["paper_link"] = f"/c/{slug}/p/{pk}"
    papers = [{"id": p["id"], "title": p.get("title") or ""} for p in library.list_papers(slug)]
    return templates.TemplateResponse(
        request, "_thoughts_panel.html",
        {"slug": slug, "thoughts": items, "paper_key": paper_key, "panel_id": panel_id,
         "papers": papers})


@app.get("/c/{slug}/thoughts/panel", response_class=HTMLResponse)
def thoughts_panel(request: Request, slug: str, paper_key: str = "",
                   panel_id: str = "thoughts-panel") -> HTMLResponse:
    """Thoughts tab body (HTMX-loaded). ?paper_key=<id> scopes it; ?panel_id for the modal."""
    _require_collection(slug)
    return _thoughts_panel_response(request, slug, paper_key, panel_id)


@app.post("/c/{slug}/thoughts/add", response_class=HTMLResponse)
def thoughts_add(
    request: Request, slug: str, text: str = Form(""), synth_kind: str = Form("seed"),
    paper_key: str = Form(""), panel_id: str = Form("thoughts-panel"),
) -> HTMLResponse:
    """Quick-add from the Thoughts tab; anchors to a paper when paper_key is set."""
    _require_collection(slug)
    if text.strip():
        thoughts_mod.create_thought(slug, text.strip(), synth_kind=synth_kind,
                                    author_origin="human", paper_key=paper_key or None)
    return _thoughts_panel_response(request, slug, paper_key, panel_id)


@app.post("/c/{slug}/thoughts/{tid}/update")
def thoughts_update(request: Request, slug: str, tid: str, text: str = Form(...),
                    paper_key: str = Form(""), panel_id: str = Form("thoughts-panel")):
    """Edit a thought. HTMX → refreshed (paper-scoped) panel; plain form → redirect."""
    thoughts_mod.update_thought(slug, tid, text.strip())
    if request.headers.get("HX-Request"):
        return _thoughts_panel_response(request, slug, paper_key, panel_id)
    return RedirectResponse(f"/c/{slug}/thoughts", status_code=303)


@app.post("/c/{slug}/thoughts/{tid}/anchor", response_class=HTMLResponse)
def thoughts_anchor(request: Request, slug: str, tid: str, link_paper: str = Form(""),
                    paper_key: str = Form(""), panel_id: str = Form("thoughts-panel")):
    """Link (or unlink) a thought to a paper. `link_paper` is the chosen paper id (or '');
    paper_key/panel_id keep the panel scope on re-render."""
    _require_collection(slug)
    thoughts_mod.set_paper(slug, tid, link_paper or None)
    return _thoughts_panel_response(request, slug, paper_key, panel_id)


@app.post("/c/{slug}/thoughts/{tid}/delete")
def thoughts_delete(request: Request, slug: str, tid: str, paper_key: str = Form(""),
                    panel_id: str = Form("thoughts-panel")):
    thoughts_mod.delete_thought(slug, tid)
    if request.headers.get("HX-Request"):
        return _thoughts_panel_response(request, slug, paper_key, panel_id)
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
    benchmarks = wiki.load_benchmarks(slug)
    # Suggested-reading async job (overlay on the reading tab).
    rjob = wiki.get_reading_job(slug)
    reading_running = bool(rjob and rjob.get("status") == "running")
    reading_error = None
    if rjob and rjob.get("status") in ("done", "failed"):
        if rjob["status"] == "failed":
            reading_error = rjob.get("error")
        wiki.clear_reading_job(slug)
    # Benchmark-extract async job (overlay on the benchmarks tab).
    bjob = wiki.get_benchmark_job(slug)
    benchmark_running = bool(bjob and bjob.get("status") == "running")
    benchmark_error = None
    if bjob and bjob.get("status") in ("done", "failed"):
        if bjob["status"] == "failed":
            benchmark_error = bjob.get("error")
        wiki.clear_benchmark_job(slug)
    # Literature-review async job (spinner on the Review tab).
    review = wiki.load_review(slug)
    revjob = wiki.get_review_job(slug)
    review_running = bool(revjob and revjob.get("status") == "running")
    review_error = None
    if revjob and revjob.get("status") in ("done", "failed"):
        if revjob["status"] == "failed":
            review_error = revjob.get("error")
        wiki.clear_review_job(slug)
    review_html = render_md(review["accepted_md"], slug) if review["has_accepted"] else ""

    # Header stat strip (Papers · Highlights · Notes · Connections). Cheap counts;
    # 'Connections' reuses the structural graph's edge count from connection_view.
    stats = None
    if overview and not overview.get("needs_migration"):
        cx = (overview.get("connections") or {}).get("overview") or {}
        stats = {
            "papers": len(overview.get("papers") or []),
            "highlights": (overview.get("focus") or {}).get("highlights"),
            "notes": (overview.get("focus") or {}).get("notes"),
            "connections": cx.get("connections"),
        }
        # focus is threshold-gated (often None); fall back to direct counts so the
        # strip always shows real numbers, not blanks.
        if stats["highlights"] is None or stats["notes"] is None:
            h, n = wiki.attention_counts(slug)
            stats["highlights"], stats["notes"] = h, n

    return templates.TemplateResponse(
        request, "_wiki_panel.html",
        {"slug": slug,
         "overview": overview,
         "benchmarks": benchmarks,
         "stats": stats,
         "reading_running": reading_running,
         "reading_error": reading_error,
         "benchmark_running": benchmark_running,
         "benchmark_error": benchmark_error,
         "review": review,
         "review_html": review_html,
         "review_running": review_running,
         "review_error": review_error,
         "col": library.get_collection(slug),
         "collection_name": (library.get_collection(slug) or {}).get("name", slug),
         "dup_count": len(library.find_duplicate_groups(slug)),
         "graveyard_count": library.graveyard_count(slug),
         "thesis_undo": wiki.has_thesis_undo(slug),
         "update_available": wiki.update_available(slug),
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
def wiki_draft_seed(request: Request, slug: str, mode: str = Form("full")) -> HTMLResponse:
    """Kick off the field-model draft on a background daemon thread and re-render the panel
    immediately. mode='incremental' folds new papers/signal into the existing model;
    'full' rebuilds from scratch. The new panel renders the in-progress overlay; the
    overlay's polling refreshes on completion."""
    _require_collection(slug)
    wiki.start_draft_async(slug, force=True, mode=(mode if mode == "incremental" else "full"))
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


def _regen_gate(request: Request, slug: str) -> HTMLResponse:
    """Render the Regenerate gate: belief candidates (to review) + accepted beliefs
    (which will feed the regen) + the Regenerate-now CTA."""
    return templates.TemplateResponse(request, "_regen_gate.html", {
        "slug": slug,
        "candidates": wiki.list_belief_candidates(slug),
        "accepted": wiki.list_accepted_beliefs(slug),
        "can_suggest": wiki.can_suggest_beliefs(slug),
        "has_model": wiki._has_field_model(slug),
    })


@app.get("/c/{slug}/wiki/theme", response_class=HTMLResponse)
def wiki_theme_detail(request: Request, slug: str, sig: str = "") -> HTMLResponse:
    """Theme detail popup (entities by kind, binding papers, explanation, rename)."""
    _require_collection(slug)
    return templates.TemplateResponse(request, "_theme_detail.html",
                                      {"slug": slug, "t": wiki.theme_detail(slug, sig)})


@app.get("/c/{slug}/wiki/entity", response_class=HTMLResponse)
def wiki_entity_detail(request: Request, slug: str, key: str = "") -> HTMLResponse:
    """Detail popup for a Map entity (concept/method/problem): description, top papers,
    related entities, per-entity literature review. Concepts editable; methods/problems read-only."""
    _require_collection(slug)
    e = wiki.entity_detail(slug, key)
    review_html = render_md(e["review"], slug) if (e and e.get("review")) else ""
    return templates.TemplateResponse(request, "_entity_detail.html",
                                      {"slug": slug, "e": e, "review_html": review_html})


@app.post("/c/{slug}/wiki/entity-reviews/generate", response_class=HTMLResponse)
def wiki_entity_reviews_generate(request: Request, slug: str) -> HTMLResponse:
    """Kick off per-entity literature reviews on a background thread; re-render the panel."""
    _require_collection(slug)
    wiki.start_entity_reviews_async(slug)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/regen/prepare", response_class=HTMLResponse)
def wiki_regen_prepare(request: Request, slug: str) -> HTMLResponse:
    """Step 1 of the gated regenerate: draft fresh belief candidates (only if the
    signal floor passes) into the tray, then show the review gate. Regen itself
    does not run until the user clicks 'Regenerate now'."""
    _require_collection(slug)
    if wiki.can_suggest_beliefs(slug):
        wiki.suggest_beliefs(slug)
    return _regen_gate(request, slug)


@app.post("/c/{slug}/wiki/regen/belief/{cid}/accept", response_class=HTMLResponse)
def wiki_regen_belief_accept(request: Request, slug: str, cid: str) -> HTMLResponse:
    """Accept a candidate from within the gate; re-render the gate (not the panel)."""
    _require_collection(slug)
    wiki.accept_belief(slug, cid)
    return _regen_gate(request, slug)


@app.post("/c/{slug}/wiki/regen/belief/{cid}/dismiss", response_class=HTMLResponse)
def wiki_regen_belief_dismiss(request: Request, slug: str, cid: str) -> HTMLResponse:
    """Dismiss a candidate from within the gate; re-render the gate."""
    _require_collection(slug)
    wiki.dismiss_belief(slug, cid)
    return _regen_gate(request, slug)


@app.post("/c/{slug}/wiki/review/suggest", response_class=HTMLResponse)
def wiki_review_suggest(request: Request, slug: str) -> HTMLResponse:
    """Kick off the literature-review draft on a background thread; re-render the
    panel (which shows the Review tab spinner + polls to completion)."""
    _require_collection(slug)
    wiki.start_review_async(slug)
    return _wiki_panel(request, slug)


@app.get("/c/{slug}/wiki/review/status", response_class=JSONResponse)
def wiki_review_status(slug: str) -> JSONResponse:
    job = wiki.get_review_job(slug)
    if not job:
        return JSONResponse({"status": "idle"})
    return JSONResponse({"status": job.get("status", "running"), "error": job.get("error")})


@app.post("/c/{slug}/wiki/review/accept", response_class=HTMLResponse)
def wiki_review_accept(request: Request, slug: str, text: str = Form("")) -> HTMLResponse:
    """Accept the (edited) review draft → wiki/review.md. The user owns it now."""
    _require_collection(slug)
    wiki.accept_review(slug, text)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/review/dismiss", response_class=HTMLResponse)
def wiki_review_dismiss(request: Request, slug: str) -> HTMLResponse:
    """Drop the pending review draft without saving."""
    _require_collection(slug)
    wiki.dismiss_review(slug)
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


_THESIS_LABELS = [("one_paragraph", "Thesis paragraph"), ("core_tension", "Core tension"),
                  ("key_intuition", "Key intuition"), ("central_question", "Central question")]


@app.post("/c/{slug}/wiki/thesis/propose", response_class=HTMLResponse)
def wiki_thesis_propose(request: Request, slug: str, instruction: str = Form("")) -> HTMLResponse:
    """Agent proposes a thesis revision from the instruction; returns the diff
    fragment for the edit modal. Writes nothing."""
    _require_collection(slug)
    res = wiki.propose_thesis_edit(slug, instruction)
    rows = []
    if res.get("ok"):
        cur, prop = res["current"], res["proposed"]
        rows = [{"label": lbl, "field": k, "before": cur.get(k, ""), "after": prop.get(k, "")}
                for k, lbl in _THESIS_LABELS]
    return templates.TemplateResponse(request, "_section_edit_diff.html", {
        "error": None if res.get("ok") else res.get("error"), "rows": rows,
        "apply_hx": True, "apply_action": f"/c/{slug}/wiki/thesis/apply"})


@app.post("/c/{slug}/wiki/thesis/apply", response_class=HTMLResponse)
def wiki_thesis_apply(request: Request, slug: str, one_paragraph: str = Form(""),
                      core_tension: str = Form(""), key_intuition: str = Form(""),
                      central_question: str = Form("")) -> HTMLResponse:
    _require_collection(slug)
    wiki.apply_thesis_edit(slug, {"one_paragraph": one_paragraph, "core_tension": core_tension,
                                  "key_intuition": key_intuition, "central_question": central_question})
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/thesis/undo", response_class=HTMLResponse)
def wiki_thesis_undo(request: Request, slug: str) -> HTMLResponse:
    _require_collection(slug)
    wiki.undo_thesis_edit(slug)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/concepts/add", response_class=HTMLResponse)
def wiki_concept_add(request: Request, slug: str, name: str = Form(""),
                     blurb: str = Form("")) -> HTMLResponse:
    _require_collection(slug)
    wiki.add_concept(slug, name, blurb)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/concepts/edit", response_class=HTMLResponse)
def wiki_concept_edit(request: Request, slug: str, target: str = Form(""),
                      name: str = Form(""), blurb: str = Form("")) -> HTMLResponse:
    _require_collection(slug)
    wiki.edit_concept(slug, target, name, blurb)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/concepts/remove", response_class=HTMLResponse)
def wiki_concept_remove(request: Request, slug: str, target: str = Form("")) -> HTMLResponse:
    _require_collection(slug)
    wiki.remove_concept(slug, target)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/benchmarks/extract", response_class=HTMLResponse)
def wiki_benchmarks_extract(request: Request, slug: str) -> HTMLResponse:
    """Kick off per-paper agentic benchmark extraction on a background thread; the
    panel overlay polls /benchmarks/status (done/total). Returns immediately."""
    _require_collection(slug)
    wiki.start_benchmark_async(slug)
    return _wiki_panel(request, slug)


@app.get("/c/{slug}/wiki/benchmarks/status", response_class=JSONResponse)
def wiki_benchmarks_status(slug: str) -> JSONResponse:
    job = wiki.get_benchmark_job(slug)
    if not job:
        return JSONResponse({"status": "idle"})
    return JSONResponse({"status": job.get("status", "running"), "done": job.get("done", 0),
                         "total": job.get("total", 0), "results": job.get("results", 0),
                         "error": job.get("error")})


# --- Recommended-papers-to-add (arXiv discovery → triage → collection) --------

def _since_from_months(months: str) -> str:
    """Turn a recency window (form 'months', e.g. '6' / '12' / '' for all-time) into
    an absolute 'YYYY-MM' cutoff measured back from today. '' → no cutoff."""
    m = (months or "").strip()
    if not m.isdigit() or int(m) <= 0:
        return ""
    from datetime import date
    today = date.today()
    total = today.year * 12 + (today.month - 1) - int(m)
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


@app.post("/c/{slug}/wiki/recommend-add", response_class=HTMLResponse)
def wiki_recommend_add(request: Request, slug: str, purpose: str = Form("gaps"),
                       target: str = Form(""), custom: str = Form(""),
                       deep: str = Form(""), months: str = Form("")) -> HTMLResponse:
    """Kick off arXiv discovery for the chosen purpose on a background thread and
    re-render the panel (which shows the overlay). ``deep`` ('1') routes to the
    tool-using paper-finder sub-agent; ``months`` caps out papers older than that
    recency window."""
    _require_collection(slug)
    wiki.start_reading_async(slug, purpose=purpose, target=target, custom=custom,
                             deep=(deep == "1"), since=_since_from_months(months))
    return _wiki_panel(request, slug)


@app.get("/c/{slug}/wiki/reading/status", response_class=JSONResponse)
def wiki_reading_status(slug: str) -> JSONResponse:
    job = wiki.get_reading_job(slug)
    if not job:
        return JSONResponse({"status": "idle"})
    return JSONResponse({"status": job.get("status", "running"),
                         "added": job.get("added", 0), "error": job.get("error")})


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


@app.post("/c/{slug}/wiki/reading/clear", response_class=HTMLResponse)
def wiki_reading_clear(request: Request, slug: str) -> HTMLResponse:
    """Clear ALL pending suggestions — removed from the list without judging (not added,
    not rejected; may resurface later)."""
    _require_collection(slug)
    triage_mod.clear_pending(slug)
    return _wiki_panel(request, slug)


@app.post("/c/{slug}/wiki/reading/clear-source", response_class=HTMLResponse)
def wiki_reading_clear_source(request: Request, slug: str, source: str = Form(""),
                              source_detail: str = Form("")) -> HTMLResponse:
    """Clear one source group's pending suggestions (no judgment)."""
    _require_collection(slug)
    triage_mod.clear_source(slug, source, source_detail)
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
