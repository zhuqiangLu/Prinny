"""MCP read surface + submit_proposal (AGENTIC_PLAN P4).

A bounded, validating tool surface the agent reaches over loopback HTTP. The whole
point of the gate is undermined if the agent can read arbitrary files, so:

  - tools are READ-only except ``submit_proposal``, whose body is app code that runs
    the gate and writes only the review QUEUE (``proposed-edits/``), never the wiki;
  - every tool body calls an existing module function (no second DB connection, no raw
    filesystem); outputs are bounded/previewed, never full dumps;
  - each run is bound to one collection via a per-run bearer token, so the agent can't
    read across collections.

Transport is MCP stdio (``app/mcp_stdio.py``): Claude Code spawns the server as a
subprocess and speaks newline-delimited JSON-RPC. (Claude headless does not connect to
HTTP MCP servers passed via --mcp-config, so stdio is the working transport.) The run is
scoped to one collection via the ``PA_MCP_COLLECTION`` env var on the spawned process.
This module holds the transport-agnostic tools + ``dispatch``; the I/O loop is in
``mcp_stdio``.
"""
from __future__ import annotations

import json
import logging
import os
import sys

from . import annotations as ann_mod, library, notes as notes_mod, provenance, thoughts as thoughts_mod, wiki
from .config import APP_DIR
from .db import connect
from pathlib import Path

logger = logging.getLogger("paper_agent.mcp")

PREVIEW = 140          # max chars of any fragment preview
SEED_CAP = 60          # max seeds returned per call
SEARCH_CAP = 20


# This MCP surface is now read-only — the cognitive-model wiki has no agent
# write tools. Kept as an (empty) set so callers that test membership keep working.
WRITE_TOOLS: set[str] = set()


def _readonly() -> bool:
    """Read-only mode is requested via PA_MCP_READONLY in the spawned server's env."""
    return bool(os.environ.get("PA_MCP_READONLY"))


def stdio_mcp_config(slug: str, server_name: str = "pa", read_only: bool = False) -> dict:
    """The --mcp-config the app passes when launching an agent for ``slug``: spawn this
    package's stdio MCP server, scoped to that collection. ``read_only`` (used by the
    paper chat) sets PA_MCP_READONLY so the server refuses the write tools entirely."""
    repo_root = str(Path(__file__).resolve().parent.parent)  # parent of app/
    env = {"PAPER_AGENT_HOME": str(APP_DIR), "PA_MCP_COLLECTION": slug, "PYTHONPATH": repo_root}
    if read_only:
        env["PA_MCP_READONLY"] = "1"
    return {"mcpServers": {server_name: {
        "command": sys.executable, "args": ["-m", "app.mcp_stdio"], "env": env,
    }}}


# --- tool implementations (read; collection scoped by the spawned run) -------
def _preview(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:PREVIEW] + ("…" if len(text) > PREVIEW else "")


def get_unreasoned_seeds(slug: str) -> dict:
    """Seed-kind fragments (attention signals the human hasn't reasoned over yet),
    grouped by paper. Notes/highlights/thoughts whose effective synth_kind is 'seed'.
    Previews only; capped."""
    con = connect()
    try:
        note_rows = con.execute(
            "SELECT paper_id, summary, key_quotes FROM paper_notes WHERE collection_slug = ?", (slug,)
        ).fetchall()
        hl_rows = con.execute(
            "SELECT id, paper_id, selected_text FROM annotations "
            "WHERE collection_slug = ? AND kind = 'highlight'", (slug,)
        ).fetchall()
    finally:
        con.close()
    seeds = []
    for r in note_rows:
        kind, _ = notes_mod.note_kind(slug, r["paper_id"])
        if kind == "seed":
            seeds.append({"id": f"note:{r['paper_id']}", "type": "note", "paper": str(r["paper_id"]),
                          "preview": _preview((r["summary"] or "") + " " + (r["key_quotes"] or ""))})
    for r in hl_rows:
        seeds.append({"id": f"highlight:{r['id']}", "type": "highlight", "paper": str(r["paper_id"]),
                      "preview": _preview(r["selected_text"] or "")})
    for t in thoughts_mod.list_thoughts(slug):
        if t["synth_kind"] == "seed":
            seeds.append({"id": f"thought:{t['id']}", "type": "thought", "paper": None,
                          "preview": _preview(t["body"])})
    return {"collection": slug, "count": len(seeds[:SEED_CAP]), "seeds": seeds[:SEED_CAP],
            "truncated": len(seeds) > SEED_CAP}


def get_fragment(slug: str, frag_id: str) -> dict:
    """A single fragment in full, by composite id (note:/thought:/highlight:/paper:)."""
    rtype, _, rid = (frag_id or "").partition(":")
    if rtype == "note":
        n = notes_mod.get_note(slug, int(rid))
        kind, origin = provenance.effective_stamp({"type": "note", "id": rid}, slug)
        return {"id": frag_id, "type": "note", "synth_kind": kind, "author_origin": origin,
                "summary": n["summary"], "thoughts": n["thoughts"], "key_quotes": n["key_quotes"]}
    if rtype == "thought":
        t = thoughts_mod.get_thought(slug, rid)
        if not t:
            return {"error": "not found"}
        return {"id": frag_id, "type": "thought", "synth_kind": t["synth_kind"],
                "author_origin": t["author_origin"], "body": t["body"]}
    if rtype == "highlight":
        a = ann_mod.get(int(rid))
        if not a:
            return {"error": "not found"}
        return {"id": frag_id, "type": "highlight", "synth_kind": "seed", "author_origin": "human",
                "paper": str(a["paper_id"]), "page": a["page"],
                "text": a["selected_text"], "note": a["note_text"]}
    if rtype == "paper":
        p = library.get_paper(int(rid))
        if not p:
            return {"error": "not found"}
        return {"id": frag_id, "type": "paper", "synth_kind": "seed", "author_origin": "external",
                "title": p["title"], "authors": p["authors"], "year": p["year"],
                "abstract": _preview(p.get("abstract") or "")}
    return {"error": f"unknown fragment id '{frag_id}'"}


def get_paper_context(slug: str, paper_id) -> dict:
    """The user's current notes + highlights for one paper (P8 paper sub-agent). Always
    live — reflects annotations made mid-chat. Read-only."""
    try:
        pid = int(paper_id)
    except (TypeError, ValueError):
        return {"error": "paper_id must be an integer"}
    note = notes_mod.get_note(slug, pid)
    kind, origin = provenance.effective_stamp({"type": "note", "id": pid}, slug)
    hls = [{"id": a["id"], "page": a["page"], "text": a["selected_text"], "note": a["note_text"]}
           for a in ann_mod.list_app(pid, slug) if (a.get("selected_text") or a.get("note_text"))]
    return {"paper": str(pid), "note": {"summary": note["summary"], "thoughts": note["thoughts"],
            "key_quotes": note["key_quotes"], "synth_kind": kind, "status": note["status"]},
            "highlights": hls}


def get_chat_history(slug: str, paper_id, limit=200) -> dict:
    """The user's prior chat transcript for this paper, as markdown, so a fresh or resumed
    session can recover the conversation instead of starting cold. Reads the paper's most
    recent thread (the one the chat is continuing). Read-only; the in-progress turn isn't
    stored yet, so this returns only the earlier turns."""
    try:
        pid = int(paper_id)
    except (TypeError, ValueError):
        return {"error": "paper_id must be an integer"}
    try:
        lim = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        lim = 200
    con = connect()
    try:
        thread = con.execute(
            "SELECT id FROM chat_threads WHERE collection_slug=? AND paper_id=? "
            "ORDER BY COALESCE(last_active_at, created_at) DESC, id DESC LIMIT 1",
            (slug, pid),
        ).fetchone()
        if not thread:
            return {"paper_id": pid, "total": 0, "returned": 0, "markdown": "",
                    "note": "no prior chat for this paper"}
        rows = con.execute(
            "SELECT role, content FROM chat_messages WHERE thread_id=? "
            "AND role IN ('user','assistant') ORDER BY id",
            (thread["id"],),
        ).fetchall()
    finally:
        con.close()
    total = len(rows)
    shown = rows[-lim:]                                  # keep the most recent if truncated
    blocks = [f"**{'User' if r['role'] == 'user' else 'Assistant'}:** {r['content']}" for r in shown]
    out = {"paper_id": pid, "total": total, "returned": len(shown),
           "markdown": "\n\n".join(blocks)}
    if len(shown) < total:
        out["note"] = f"showing the most recent {len(shown)} of {total} messages (raise limit to see more)"
    return out


def read_paper_text(slug: str, paper_id, start_page=1, pages=5) -> dict:
    """Extract a paper's text by page range (the agent's reliable, no-shell way to read
    a PDF). Returns total_pages so it can page through. Use Read for figures/layout."""
    from . import library, pdf_store
    from .pdf_text import extract_pages
    try:
        pid = int(paper_id)
    except (TypeError, ValueError):
        return {"error": "paper_id must be an integer"}
    if not library.get_paper(pid):
        return {"error": "no such paper"}
    pdf = pdf_store.ensure_cached(pid)
    if not pdf or not pdf.exists():
        return {"error": "no cached PDF for this paper"}
    try:
        sp = int(start_page); n = int(pages)
    except (TypeError, ValueError):
        sp, n = 1, 5
    return extract_pages(pdf, sp, n)


def search_fragments(slug: str, query: str) -> dict:
    """FTS5 over notes + substring over thoughts AND highlights. Previews only.

    Highlights are included so cross-paper questions (does this contradict / connect to a
    passage I highlighted elsewhere?) surface the user's highlighted claims, not just notes.
    Each hit carries a composite id usable with get_fragment, plus paper_id for context."""
    hits = []
    con = connect()
    try:
        try:
            rows = con.execute(
                "SELECT paper_id, summary FROM notes_fts WHERE notes_fts MATCH ? "
                "AND collection_slug = ? LIMIT ?", (query, slug, SEARCH_CAP)
            ).fetchall()
            for r in rows:
                hits.append({"id": f"note:{r['paper_id']}", "type": "note",
                             "paper_id": r["paper_id"], "preview": _preview(r["summary"] or "")})
        except Exception:  # noqa: BLE001 - bad FTS query syntax -> just skip notes
            pass
    finally:
        con.close()
    ql = (query or "").lower()
    for t in thoughts_mod.list_thoughts(slug):
        if ql and ql in t["body"].lower():
            hits.append({"id": f"thought:{t['id']}", "type": "thought", "preview": _preview(t["body"])})
        if len(hits) >= SEARCH_CAP:
            break
    if ql and len(hits) < SEARCH_CAP:                     # highlighted passages + inline notes
        con = connect()
        try:
            like = f"%{ql}%"
            rows = con.execute(
                "SELECT id, paper_id, selected_text, note_text FROM annotations "
                "WHERE collection_slug = ? AND (LOWER(COALESCE(selected_text,'')) LIKE ? "
                "OR LOWER(COALESCE(note_text,'')) LIKE ?) LIMIT ?",
                (slug, like, like, SEARCH_CAP)).fetchall()
            for r in rows:
                txt = r["selected_text"] or r["note_text"] or ""
                hits.append({"id": f"highlight:{r['id']}", "type": "highlight",
                             "paper_id": r["paper_id"], "preview": _preview(txt)})
                if len(hits) >= SEARCH_CAP:
                    break
        finally:
            con.close()
    return {"collection": slug, "count": len(hits), "hits": hits[:SEARCH_CAP]}


def arxiv_search(slug: str, query: str, max_results=10) -> dict:
    """Search arXiv (the finder agent's only network reach — the 'arXiv-only'
    policy is enforced by this being the sole search tool). Returns lightweight
    hits the agent can judge + cite. Read-only."""
    from . import discover
    try:
        n = max(1, min(25, int(max_results)))
    except (TypeError, ValueError):
        n = 10
    try:
        hits = discover._arxiv_search(query, max_results=n)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"arxiv search failed: {exc}"}
    return {"query": query, "count": len(hits),
            "results": [{"arxiv_id": h.get("arxiv_id"), "title": h.get("title"),
                         "summary": _preview(h.get("summary") or "")} for h in hits]}


def recommendation_history(slug: str) -> dict:
    """What the user previously KEPT vs PASSED ON for this collection's suggested
    reading — the finder uses it to bias toward accepted-like and away from
    rejected-like, and to avoid re-pitching. Titles only. Read-only."""
    from . import triage
    h = triage.outcome_history(slug)
    return {"kept": h["accepted_titles"][:40], "passed_on": h["dismissed_titles"][:40]}


def list_papers(slug: str) -> dict:
    """The collection's papers as {ref, title}. Use a `ref` when citing
    supporting_papers in propose_wiki_edit. Read-only."""
    seen, out = set(), []
    for ref, info in wiki._ref_map(slug).items():
        if info["id"] in seen:
            continue
        seen.add(info["id"])
        out.append({"ref": ref, "title": info.get("title", "")})
    return {"count": len(out), "papers": out[:200]}


def propose_wiki_edit(slug: str, section: str, op: str, content: dict,
                      supporting_papers=None, grounding: str = "") -> dict:
    """Propose a TYPED wiki edit (propose-and-gate). This does NOT write — it
    creates a pending proposal the user Accepts/Dismisses inline."""
    from . import wiki_propose
    return wiki_propose.create_proposal(
        slug, section, op, content or {},
        supporting_papers=supporting_papers or [], grounding=grounding, origin="chat")


def read_wiki_page(slug: str, page: str) -> dict:
    """One synthesized wiki page (bounded by the page itself)."""
    page = (page or "").strip().lstrip("/")
    if not page.endswith(".md"):
        page += ".md"
    target = (wiki.COLLECTIONS_DIR / slug / "wiki" / page).resolve()
    base = (wiki.COLLECTIONS_DIR / slug / "wiki").resolve()
    if base not in target.parents or not target.exists():  # no path escape, must exist
        return {"error": "page not found"}
    return {"page": page, "content": target.read_text(encoding="utf-8")}


# --- JSON-RPC dispatch (minimal MCP) ----------------------------------------
PROTOCOL_VERSION = "2025-06-18"
_TOOLS = [
    {"name": "get_unreasoned_seeds",
     "description": "List seed-kind fragments (notes/highlights/thoughts the human hasn't reasoned over), grouped by paper. Previews only.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_fragment",
     "description": "Fetch one fragment in full by id (note:<paperId> | thought:<id> | highlight:<id> | paper:<paperId>).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "search_fragments",
     "description": "Search the collection's notes, thoughts, and highlights; returns previews with paper_id. Use to find which other papers the user has written about a topic.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_paper_context",
     "description": "The user's current notes + highlights for one paper (by paper id). Use to compare a paper to the user's own thinking.",
     "inputSchema": {"type": "object", "properties": {"paper_id": {"type": "string"}}, "required": ["paper_id"]}},
    {"name": "read_paper_text",
     "description": "Extract a paper's text by page range (start_page 1-indexed, pages count). Returns total_pages so you can page through. The reliable way to read the PDF as text — do NOT use a shell. Use the Read tool only when you need figures/tables/layout.",
     "inputSchema": {"type": "object", "properties": {"paper_id": {"type": "string"}, "start_page": {"type": "integer"}, "pages": {"type": "integer"}}, "required": ["paper_id"]}},
    {"name": "get_chat_history",
     "description": "The user's earlier chat transcript for this paper (markdown). Call this when continuing a conversation and you lack the prior context (e.g. the session was resumed cold) — it returns the earlier turns so you don't start over. limit caps the most-recent messages returned.",
     "inputSchema": {"type": "object", "properties": {"paper_id": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["paper_id"]}},
    {"name": "read_wiki_page",
     "description": "Read one current wiki page, e.g. 'problems/efficiency' or 'index'.",
     "inputSchema": {"type": "object", "properties": {"page": {"type": "string"}}, "required": ["page"]}},
    {"name": "arxiv_search",
     "description": "Search arXiv for papers (keywords query). Your ONLY way to reach external papers — issue several focused queries and read the summaries before picking. max_results caps the hits (default 10).",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}},
    {"name": "recommendation_history",
     "description": "What the user previously KEPT vs PASSED ON for suggested reading in this collection. Use it to prefer accepted-like papers, deprioritise rejected-like ones, and avoid re-pitching.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_papers",
     "description": "List the collection's papers as {ref, title}. Use a ref value when you cite supporting_papers in propose_wiki_edit.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "propose_wiki_edit",
     "description": ("Propose a TYPED edit to this collection's wiki. This does NOT write — it creates a "
                     "pending proposal the user Accepts/Dismisses inline, so propose sparingly and only "
                     "when the conversation clearly warrants it. section/op + content shapes: "
                     "thesis/replace content={one_paragraph,core_tension,key_intuition,central_question} "
                     "(grounding=one sentence on why, from the conversation); "
                     "landscape/add_item|remove_item content={column:problems|methods|debates|open_questions, text, papers?}; "
                     "concepts/add_concept content={name,synonyms,blurb,papers}; "
                     "belief/add content={title,confidence:emerging|medium|uncertain,related_concepts}. "
                     "EVIDENCE edits (concepts, beliefs, landscape problems/methods) MUST pass supporting_papers "
                     "(refs from list_papers); the thesis is grounded in the conversation instead. "
                     "Returns {ok,id,summary} or {ok:false,error}."),
     "inputSchema": {"type": "object", "properties": {
         "section": {"type": "string"}, "op": {"type": "string"},
         "content": {"type": "object"},
         "supporting_papers": {"type": "array", "items": {"type": "string"}},
         "grounding": {"type": "string"}},
         "required": ["section", "op", "content"]}},
]


def _call_tool(slug: str, name: str, args: dict):
    if _readonly() and name in WRITE_TOOLS:
        return {"error": "read-only session: write tools are disabled"}
    if name == "get_unreasoned_seeds":
        return get_unreasoned_seeds(slug)
    if name == "get_fragment":
        return get_fragment(slug, args.get("id", ""))
    if name == "search_fragments":
        return search_fragments(slug, args.get("query", ""))
    if name == "get_paper_context":
        return get_paper_context(slug, args.get("paper_id"))
    if name == "read_paper_text":
        return read_paper_text(slug, args.get("paper_id"), args.get("start_page", 1), args.get("pages", 5))
    if name == "get_chat_history":
        return get_chat_history(slug, args.get("paper_id"), args.get("limit", 200))
    if name == "read_wiki_page":
        return read_wiki_page(slug, args.get("page", ""))
    if name == "arxiv_search":
        return arxiv_search(slug, args.get("query", ""), args.get("max_results", 10))
    if name == "recommendation_history":
        return recommendation_history(slug)
    if name == "list_papers":
        return list_papers(slug)
    if name == "propose_wiki_edit":
        return propose_wiki_edit(slug, args.get("section", ""), args.get("op", ""),
                                 args.get("content") or {}, args.get("supporting_papers") or [],
                                 args.get("grounding", ""))
    raise KeyError(name)


def dispatch(slug: str, request: dict) -> dict | None:
    """Handle one JSON-RPC request. Returns a response dict, or None for notifications."""
    method = request.get("method")
    rid = request.get("id")
    if method == "initialize":
        # Echo the client's requested protocol version when present (version mismatch
        # is a common reason a client aborts the handshake), else our default.
        client_ver = (request.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        result = {"protocolVersion": client_ver,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "paper-agent", "version": "0.1"}}
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None  # notification: no response
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        tools = [t for t in _TOOLS if not (_readonly() and t["name"] in WRITE_TOOLS)]
        result = {"tools": tools}
    elif method == "tools/call":
        params = request.get("params") or {}
        name, args = params.get("name"), (params.get("arguments") or {})
        try:
            payload = _call_tool(slug, name, args)
            result = {"content": [{"type": "text", "text": json.dumps(payload)}],
                      "isError": bool(isinstance(payload, dict) and payload.get("error"))}
        except KeyError:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"unknown tool '{name}'"}}
        except Exception as exc:  # noqa: BLE001 - surface as a tool error, don't 500
            logger.exception("mcp tool %s failed", name)
            result = {"content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                      "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"method '{method}' not found"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}
