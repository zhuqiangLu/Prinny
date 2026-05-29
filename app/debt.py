"""Reading-debt queue + brainstorm (AGENTIC_PLAN P7).

Debt = clusters of `seed` fragments the user flagged but hasn't reasoned over, plus the
gate's demoted synthesis claims. An on-demand agent pass surfaces them as QUESTIONS
(the one agent role the thesis allows — prompt reasoning, never supply it). The user
then, per item: fills it (→ a (reasoning, human) thought that can ground the wiki),
ignores it, or asks the agent to brainstorm it (→ (agent) content quarantined in
wiki/brainstorming/, which can never ground an assertion).

Data layer + the two passes live here; the gate-exempt brainstorm persistence is in
``wiki``; the MCP tools the agent calls are in ``mcp_server``.
"""
from __future__ import annotations

import hashlib
import json
import logging

from . import agent_skills, agents, engine as engine_mod, llm, mcp_server, thoughts as thoughts_mod, wiki
from .config import load_config
from .db import connect

logger = logging.getLogger("paper_agent.debt")

STATUSES = ("open", "filled", "ignored", "brainstormed")
_AGENTIC = {"claude-code"}

_FIND_TOOLS = [f"mcp__pa__{t}" for t in
               ("get_unreasoned_seeds", "get_fragment", "search_fragments",
                "read_wiki_page", "submit_debt")]
_BRAINSTORM_TOOLS = [f"mcp__pa__{t}" for t in
                     ("get_unreasoned_seeds", "get_fragment", "search_fragments",
                      "read_wiki_page", "submit_brainstorm")]


# --- data layer -------------------------------------------------------------
def debt_id(slug: str, sources: list[str]) -> str:
    """Stable id from the cited fragment ids, so re-runs dedupe to the same item."""
    key = slug + "|" + "|".join(sorted(str(s) for s in sources))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def upsert_debt(slug: str, question: str, sources: list[str]) -> str | None:
    """Insert a debt item (or refresh an OPEN one's question). Items the user already
    acted on (filled/ignored/brainstormed) are left alone — never re-surfaced."""
    question = (question or "").strip()
    sources = [str(s) for s in (sources or [])]
    if not question or not sources:
        return None
    did = debt_id(slug, sources)
    con = connect()
    try:
        row = con.execute("SELECT status FROM reading_debt WHERE id=?", (did,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO reading_debt (id, collection_slug, question, sources) "
                "VALUES (?, ?, ?, ?)", (did, slug, question, json.dumps(sources)))
        elif row["status"] == "open":
            con.execute("UPDATE reading_debt SET question=?, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=?", (question, did))
        # else: user already acted on it — leave as is
        con.commit()
    finally:
        con.close()
    return did


def list_debt(slug: str, statuses: tuple = ("open",)) -> list[dict]:
    qmarks = ",".join("?" * len(statuses))
    con = connect()
    try:
        rows = con.execute(
            f"SELECT * FROM reading_debt WHERE collection_slug=? AND status IN ({qmarks}) "
            "ORDER BY created_at", (slug, *statuses)).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = json.loads(d["sources"] or "[]")
        out.append(d)
    return out


def get_debt(slug: str, did: str) -> dict | None:
    con = connect()
    try:
        row = con.execute("SELECT * FROM reading_debt WHERE id=? AND collection_slug=?",
                          (did, slug)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    d = dict(row)
    d["sources"] = json.loads(d["sources"] or "[]")
    return d


def set_status(slug: str, did: str, status: str) -> bool:
    if status not in STATUSES:
        return False
    con = connect()
    try:
        cur = con.execute("UPDATE reading_debt SET status=?, updated_at=CURRENT_TIMESTAMP "
                          "WHERE id=? AND collection_slug=?", (status, did, slug))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def count_open(slug: str) -> int:
    con = connect()
    try:
        return con.execute("SELECT COUNT(*) FROM reading_debt WHERE collection_slug=? "
                           "AND status='open'", (slug,)).fetchone()[0]
    finally:
        con.close()


# --- the fill action (the thesis-blessed path) ------------------------------
def fill_debt(slug: str, did: str, reasoning: str) -> str | None:
    """Save the user's reasoning as a (reasoning, human) thought linked to the debt's
    source fragments, and mark the debt filled. Grounds the wiki on the next organize."""
    item = get_debt(slug, did)
    reasoning = (reasoning or "").strip()
    if not item or not reasoning:
        return None
    tid = thoughts_mod.create_thought(
        slug, reasoning, tags=["from-debt"], synth_kind="reasoning",
        author_origin="human", prompted_by=",".join(item["sources"]))
    set_status(slug, did, "filled")
    return tid


# --- the find pass (questions only) -----------------------------------------
_FIND_SYSTEM = (
    "You help a researcher see the reasoning they still owe on their OWN paper "
    "collection. You read their seed fragments (flagged but not yet reasoned over) and "
    "produce QUESTIONS that prompt THEM to think — you never answer, conclude, or write "
    "prose. Cluster fragments that share a theme (especially across papers) and ask what "
    "the user thinks connects or follows from them."
)
_FIND_USER = (
    "Find this collection's reading debt.\n"
    "1) Call get_unreasoned_seeds to see flagged-but-unreasoned fragments; "
    "read_wiki_page('gaps/open-questions') for already-demoted items.\n"
    "2) Use get_fragment / search_fragments to understand clusters that share a theme.\n"
    "3) For each cluster, call submit_debt with a single pointed QUESTION and the ids of "
    "the fragments it spans. Ask, never answer. Submit several items.\n"
    "Briefly note how many questions you raised."
)


def find_debt(slug: str) -> dict:
    """On-demand: surface reading debt as questions. Agentic when a CLI agent is the
    engine; otherwise a deterministic by-paper fallback."""
    eng = engine_mod.build_engine(load_config())
    ok, detail = eng.available()
    if not ok:
        raise llm.LLMError(f"{eng.name} is unavailable: {detail}")
    before = {d["id"] for d in list_debt(slug)}
    if eng.name in _AGENTIC:
        try:
            eng.run_once([{"role": "user", "content": _FIND_USER}], system=_FIND_SYSTEM,
                         allowed_tools=agents.effective_tools("debt", _FIND_TOOLS),
                         mcp_config=mcp_server.stdio_mcp_config(slug),
                         cwd=str(agent_skills.ensure_skills_home("debt")))
        except engine_mod.EngineError as exc:
            raise llm.LLMError(str(exc)) from exc
    else:
        _find_debt_deterministic(slug)
    new = [d for d in list_debt(slug) if d["id"] not in before]
    logger.info("debt.find slug=%s engine=%s new=%d", slug, eng.name, len(new))
    return {"engine": eng.name, "new": len(new), "open": count_open(slug)}


def _find_debt_deterministic(slug: str) -> None:
    """No-LLM fallback: one debt per paper that has seed fragments but no reasoning."""
    seeds = mcp_server.get_unreasoned_seeds(slug)["seeds"]
    by_paper: dict[str, list[str]] = {}
    for s in seeds:
        by_paper.setdefault(s.get("paper") or "_", []).append(s["id"])
    for paper, ids in by_paper.items():
        if paper == "_":
            continue
        upsert_debt(slug, f"You flagged {len(ids)} fragment(s) on paper {paper} but "
                          "haven't recorded your reasoning. What's your take?", ids)


# --- the brainstorm action (quarantined agent content) ----------------------
_BRAINSTORM_SYSTEM = (
    "You brainstorm SPECULATIVE, clearly-machine notes for a researcher to react to. "
    "Your output is explicitly NOT the user's knowledge and will be quarantined in a "
    "brainstorming area — it can never become a grounded wiki claim. Read their "
    "fragments and float possible connections, tensions, or directions as prompts for "
    "their thinking. Be exploratory; do not assert facts as settled."
)


def brainstorm(slug: str, did: str | None = None) -> dict:
    """Agent brainstorm for one debt (``did``) or all open debts. Output is (agent)
    content submitted to the gate-exempt brainstorm queue → wiki/brainstorming/."""
    eng = engine_mod.build_engine(load_config())
    ok, detail = eng.available()
    if not ok:
        raise llm.LLMError(f"{eng.name} is unavailable: {detail}")
    items = [get_debt(slug, did)] if did else list_debt(slug, ("open",))
    items = [i for i in items if i]
    if not items:
        return {"engine": eng.name, "written": 0}
    qs = "\n".join(f"- {i['question']} (fragments: {', '.join(i['sources'])})" for i in items)
    user = ("Brainstorm speculative notes for the user to react to, addressing these "
            f"open questions about their collection:\n{qs}\n"
            "Read the cited fragments first. Then call submit_brainstorm with one or more "
            "pages, each {title, slug, body, sources:[fragment ids]}. Exploratory only.")

    before = {p["id"] for p in wiki.list_proposed(slug)}
    if eng.name in _AGENTIC:
        try:
            eng.run_once([{"role": "user", "content": user}], system=_BRAINSTORM_SYSTEM,
                         allowed_tools=agents.effective_tools("brainstorm", _BRAINSTORM_TOOLS),
                         mcp_config=mcp_server.stdio_mcp_config(slug),
                         cwd=str(agent_skills.ensure_skills_home("brainstorm")))
        except engine_mod.EngineError as exc:
            raise llm.LLMError(str(exc)) from exc
    else:
        _brainstorm_toolless(slug, items)
    new = [p for p in wiki.list_proposed(slug) if p["id"] not in before]
    for i in items:
        set_status(slug, i["id"], "brainstormed")
    return {"engine": eng.name, "written": len(new),
            "pages": [p["page_path"] for p in new]}


def _brainstorm_toolless(slug: str, items: list[dict]) -> None:
    """No-tools fallback: one LLM call to brainstorm prose, persisted as (agent)."""
    qs = "\n".join(f"- {i['question']}" for i in items)
    try:
        body = llm.complete([
            {"role": "system", "content": _BRAINSTORM_SYSTEM},
            {"role": "user", "content": f"Brainstorm speculative notes on:\n{qs}"},
        ])
    except llm.LLMError:
        return
    sources = sorted({s for i in items for s in i["sources"]})
    wiki.brainstorm_pages(slug, [{"title": "Brainstorm", "slug": "brainstorm",
                                  "body": body, "sources": sources}])
