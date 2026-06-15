"""Personal meta-journal + daily planner — a standalone, top-level subsystem (v2: cards).

Deliberately ISOLATED: it READS your collections / research topics / papers (read-only),
but nothing in those subsystems references it. A day is a board of CARDS you create (each a
small note or plan) plus a single agent-drafted META SUMMARY that rolls the day up: how many
papers you read, a per-collection summary, an experiment plan, and what's leftover / next —
synthesized from your cards + the day's workspace activity. The summary is drafted on demand
and once a day by a due-check scheduler (catches up when the app was closed).

Contract: the agent SUMMARIZES your cards + real activity — it never fabricates progress —
and the meta summary is a regeneratable draft, not a silent mutation of anything.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime

from . import agent_skills, i18n, llm
from .config import load_config
from .db import connect

log = logging.getLogger("paper_agent.planner")

_RECENT_DAYS = 7
_LIST_MAX = 12
CARD_KINDS = ("note", "plan")


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --- cards -----------------------------------------------------------------
def list_cards(day: str) -> list[dict]:
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, kind, title, body, created_at FROM planner_cards WHERE day=? "
            "ORDER BY position, id", (day,)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def add_card(day: str, kind: str = "note", title: str = "", body: str = "") -> int:
    kind = kind if kind in CARD_KINDS else "note"
    con = connect()
    try:
        pos = (con.execute("SELECT COALESCE(MAX(position), -1)+1 FROM planner_cards WHERE day=?",
                           (day,)).fetchone()[0]) or 0
        cur = con.execute(
            "INSERT INTO planner_cards(day, kind, title, body, position) VALUES(?,?,?,?,?)",
            (day, kind, title, body, pos))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def update_card(card_id: int, title: str, body: str, kind: str | None = None) -> None:
    con = connect()
    try:
        if kind in CARD_KINDS:
            con.execute("UPDATE planner_cards SET title=?, body=?, kind=?, updated_at=? WHERE id=?",
                        (title, body, kind, _now(), card_id))
        else:
            con.execute("UPDATE planner_cards SET title=?, body=?, updated_at=? WHERE id=?",
                        (title, body, _now(), card_id))
        con.commit()
    finally:
        con.close()


def delete_card(card_id: int) -> None:
    con = connect()
    try:
        con.execute("DELETE FROM planner_cards WHERE id=?", (card_id,))
        con.commit()
    finally:
        con.close()


def _recent_cards_text(before: str, n_days: int = _RECENT_DAYS) -> str:
    """Earlier days' cards (oldest→newest) as text, so the agent can roll unfinished
    plans forward with an age."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT day, kind, title, body FROM planner_cards WHERE day<? "
            "ORDER BY day DESC, position LIMIT 200", (before,)).fetchall()
    finally:
        con.close()
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    days = sorted(by_day)[-n_days:]
    out = []
    for d in days:
        lines = [f"  [{c['kind']}] {(c['title'] + ': ') if c['title'] else ''}{(c['body'] or '').strip()}"
                 for c in by_day[d]]
        out.append(f"[{d}]\n" + "\n".join(lines))
    return "\n\n".join(out)


# --- day (cards + meta summary + live activity) ----------------------------
def get_day(day: str | None = None) -> dict:
    day = day or _today()
    con = connect()
    try:
        r = con.execute("SELECT plan, generated_at FROM planner_days WHERE day=?", (day,)).fetchone()
    finally:
        con.close()
    return {"day": day, "cards": list_cards(day),
            "summary": _loads(r["plan"]) if r else {},
            "generated_at": r["generated_at"] if r else None,
            "activity": activity_today(day)}


def set_summary(day: str, summary: dict) -> None:
    con = connect()
    try:
        con.execute(
            "INSERT INTO planner_days(day, plan, generated_at, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET plan=excluded.plan, generated_at=excluded.generated_at, "
            "updated_at=excluded.updated_at",
            (day, json.dumps(summary, ensure_ascii=False), _now(), _now()))
        con.commit()
    finally:
        con.close()


def recent_summaries(n: int = 14, before: str | None = None) -> list[dict]:
    """Earlier days that have cards or a summary — for the 'earlier days' section."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT day FROM (SELECT day FROM planner_cards UNION "
            "SELECT day FROM planner_days WHERE COALESCE(plan,'') NOT IN ('', '{}')) "
            "WHERE day < ? ORDER BY day DESC LIMIT ?", (before or _today(), n)).fetchall()
    finally:
        con.close()
    return [get_day(r["day"]) for r in rows]


def _loads(s: str | None) -> dict:
    try:
        d = json.loads(s or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


# --- deterministic activity (papers read today, per-collection) ------------
def activity_today(day: str | None = None) -> dict:
    """Real, no-LLM activity for the day: papers read (read_at on ``day``) + a per-collection
    breakdown of papers read / added that day. Drives the meta card's hard numbers."""
    day = day or _today()
    from . import library
    con = library.connect()
    try:
        names = {c["slug"]: c["name"] for c in library.list_collections()}
        read = con.execute(
            "SELECT collection_slug, COUNT(*) n FROM collection_papers "
            "WHERE read_at IS NOT NULL AND substr(read_at,1,10)=? GROUP BY collection_slug",
            (day,)).fetchall()
        added = con.execute(
            "SELECT collection_slug, COUNT(*) n FROM collection_papers "
            "WHERE substr(added_at,1,10)=? GROUP BY collection_slug", (day,)).fetchall()
        titles = [r["title"] for r in con.execute(
            "SELECT p.title FROM collection_papers cp JOIN papers p ON p.id=cp.paper_id "
            "WHERE cp.read_at IS NOT NULL AND substr(cp.read_at,1,10)=? ORDER BY cp.read_at DESC LIMIT 12",
            (day,)).fetchall()]
    finally:
        con.close()
    read_by = {r["collection_slug"]: r["n"] for r in read}
    added_by = {r["collection_slug"]: r["n"] for r in added}
    cols = []
    for slug in sorted(set(read_by) | set(added_by)):
        cols.append({"slug": slug, "name": names.get(slug, slug),
                     "read": read_by.get(slug, 0), "added": added_by.get(slug, 0)})
    return {"papers_read": sum(read_by.values()), "read_titles": titles, "collections": cols}


# --- read-only research-state digest (for grounding the summary) -----------
def _state_digest() -> str:
    from . import library, topics as topics_mod
    parts = []
    try:
        tlines = []
        for t in topics_mod.list_topics():
            full = topics_mod.get_topic(t["slug"]) or {}
            exps = [e.get("title", "") for e in (full.get("experiments") or [])][:4]
            steps = [s.get("title", "") for s in ((full.get("generated") or {}).get("next_steps") or [])][:3]
            extra = []
            if exps:
                extra.append("experiments: " + "; ".join(e for e in exps if e))
            if steps:
                extra.append("next: " + "; ".join(s for s in steps if s))
            tlines.append(f"- {t['title']} (/t/{t['slug']}): {full.get('question','')}"
                          + (" — " + " · ".join(extra) if extra else ""))
        if tlines:
            parts.append("RESEARCH TOPICS (for the experiment plan):\n" + "\n".join(tlines))
    except Exception as exc:  # noqa: BLE001
        log.debug("planner topic digest failed: %s", exc)
    return "\n\n".join(parts)


# --- the meta-summary agent ------------------------------------------------
def _validate(data: dict) -> dict:
    def _strs(v):
        out = []
        for x in (v or [])[:_LIST_MAX]:
            s = (x if isinstance(x, str) else (x.get("text", "") if isinstance(x, dict) else "")).strip()
            if s:
                out.append(s)
        return out
    leftover = []
    for x in (data.get("leftover") or [])[:_LIST_MAX]:
        text = (x.get("text", "") if isinstance(x, dict) else str(x)).strip()
        if text:
            age = x.get("age_days") if isinstance(x, dict) else None
            leftover.append({"text": text, "age_days": int(age) if isinstance(age, (int, float)) else 0})
    collections = []
    for x in (data.get("collections") or [])[:_LIST_MAX]:
        if isinstance(x, dict) and (x.get("note") or "").strip():
            collections.append({"slug": (x.get("slug") or "").strip(),
                                "name": (x.get("name") or x.get("slug") or "").strip(),
                                "note": x["note"].strip()})
    return {"summary": (data.get("summary") or "").strip(),
            "collections": collections, "experiments": _strs(data.get("experiments")),
            "leftover": leftover, "tomorrow": _strs(data.get("tomorrow"))}


def generate_summary(day: str | None = None) -> dict:
    """Draft the day's meta summary from its cards + recent cards + real activity + a
    read-only workspace digest. One LLM call. Returns {ok, error, empty}."""
    day = day or _today()
    cards = list_cards(day)
    act = activity_today(day)
    if not cards and act["papers_read"] == 0:
        return {"ok": False, "empty": True, "error": None}   # nothing to summarize yet

    cards_text = "\n\n".join(
        f"[{c['kind']}] {(c['title'] + ': ') if c['title'] else ''}{(c['body'] or '').strip()}"
        for c in cards) or "(no cards today)"
    act_text = (f"Papers read today: {act['papers_read']}"
                + ("\nBy collection: " + "; ".join(
                    f"{c['name']} (read {c['read']}, added {c['added']})" for c in act["collections"])
                   if act["collections"] else ""))
    skill = (agent_skills.skill_body("daily-plan")
             or 'Summarize the day. STRICT JSON: {"summary","collections":[{"slug","name","note"}],'
                '"experiments":[],"leftover":[{"text","age_days"}],"tomorrow":[]}.')
    skill += i18n.output_directive()
    user = (f"TODAY = {day}\n\nTODAY'S CARDS (the user's own notes/plans):\n{cards_text}\n\n"
            f"REAL ACTIVITY (counts are exact — use them, don't invent):\n{act_text}\n\n"
            f"EARLIER DAYS' CARDS (oldest→newest, for rolling unfinished plans forward with age):\n"
            f"{_recent_cards_text(day) or '(none)'}\n\n"
            f"WORKSPACE STATE (read-only — for the experiment plan; don't invent):\n"
            f"{_state_digest() or '(none)'}\n\nDraft the meta summary as STRICT JSON now.")
    try:
        from .config import agent_model
        out = llm.complete([{"role": "system", "content": skill},
                            {"role": "user", "content": user}], model=agent_model())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "empty": False, "error": str(exc)}
    data = _extract_json(out)
    if not data:
        return {"ok": False, "empty": False, "error": "The planner produced no usable output."}
    set_summary(day, _validate(data))
    return {"ok": True, "empty": False, "error": None}


def _extract_json(text: str) -> dict | None:
    from .wiki import _extract_json as _ej
    return _ej(text)


# --- background job (manual "Summarize today") -----------------------------
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def get_job(day: str | None = None) -> dict | None:
    with _LOCK:
        j = _JOBS.get(day or _today())
        return dict(j) if j else None


def clear_job(day: str | None = None) -> None:
    with _LOCK:
        _JOBS.pop(day or _today(), None)


def start_summary_async(day: str | None = None) -> bool:
    day = day or _today()
    with _LOCK:
        if (_JOBS.get(day) or {}).get("status") == "running":
            return False
        _JOBS[day] = {"status": "running", "started_at": _now(), "error": None}

    def runner():
        try:
            res = generate_summary(day)
            err = res.get("error") or ("Nothing logged yet to summarize." if res.get("empty") else None)
            with _LOCK:
                _JOBS[day] = {"status": "failed" if err else "done", "error": err,
                              "finished_at": _now()}
        except Exception as exc:  # noqa: BLE001
            with _LOCK:
                _JOBS[day] = {"status": "failed", "error": str(exc), "finished_at": _now()}

    threading.Thread(target=runner, daemon=True, name=f"planner-{day}").start()
    return True


# --- scheduler (hourly due-check; catches up if the app was off) -----------
_SCHED_STOP = threading.Event()


def _planner_hour() -> int:
    try:
        h = int(load_config().get("planner_hour", "18"))
    except (TypeError, ValueError):
        h = 18
    return min(23, max(0, h))


def _due() -> bool:
    d = get_day()
    if d["generated_at"]:
        return False
    if datetime.now().hour < _planner_hour():
        return False
    return bool(d["cards"] or d["activity"]["papers_read"])


def _tick() -> None:
    try:
        if _due():
            log.info("planner: drafting today's meta summary (scheduled)")
            generate_summary()
    except Exception as exc:  # noqa: BLE001
        log.warning("planner scheduled tick failed: %s", exc)


def start_scheduler(interval_s: int = 1800) -> None:
    import sys
    if "pytest" in sys.modules:
        return

    def loop():
        while not _SCHED_STOP.wait(5):
            _tick()
            _SCHED_STOP.wait(interval_s)

    threading.Thread(target=loop, daemon=True, name="planner-sched").start()
