"""Personal meta-journal + daily planner — a standalone, top-level subsystem.

Deliberately ISOLATED: it READS your collections / research topics / papers (read-only,
to ground summaries and reading pointers), but nothing in those subsystems references it.
One freeform log per calendar day; an agent drafts a plan (done / leftover / blocked /
tomorrow / reading) once a day — scheduled via a lightweight due-check thread that fires
after a configurable hour and CATCHES UP whenever the app is next running (a local app
isn't on 24/7, so a strict cron would silently miss days). A manual "Plan now" also works.

Contract, mirroring the rest of the app: the agent SUMMARIZES what you wrote — it never
fabricates "done" items — and its plan is a draft you edit/clear, not a silent mutation.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta

from . import agent_skills, i18n, llm
from .config import load_config
from .db import connect

log = logging.getLogger("paper_agent.planner")

_RECENT_DAYS = 7          # history window the agent sees (for leftover roll-forward + aging)
_TODO_MAX = 12            # cap each plan list so the draft stays a plan, not a brain-dump


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --- storage ---------------------------------------------------------------
def get_day(day: str | None = None) -> dict:
    """The journal row for ``day`` (default today): {day, log, plan(dict), generated_at}."""
    day = day or _today()
    con = connect()
    try:
        r = con.execute("SELECT day, log, plan, generated_at FROM planner_days WHERE day=?",
                        (day,)).fetchone()
    finally:
        con.close()
    if not r:
        return {"day": day, "log": "", "plan": {}, "generated_at": None}
    return {"day": r["day"], "log": r["log"] or "", "plan": _loads(r["plan"]),
            "generated_at": r["generated_at"]}


def save_log(day: str, text: str) -> None:
    """Upsert the user's freeform log for ``day`` (does NOT touch the drafted plan)."""
    con = connect()
    try:
        con.execute(
            "INSERT INTO planner_days(day, log, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET log=excluded.log, updated_at=excluded.updated_at",
            (day, text or "", _now()))
        con.commit()
    finally:
        con.close()


def set_plan(day: str, plan: dict) -> None:
    con = connect()
    try:
        con.execute(
            "INSERT INTO planner_days(day, plan, generated_at, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET plan=excluded.plan, generated_at=excluded.generated_at, "
            "updated_at=excluded.updated_at",
            (day, json.dumps(plan, ensure_ascii=False), _now(), _now()))
        con.commit()
    finally:
        con.close()


def recent_days(n: int = _RECENT_DAYS, before: str | None = None) -> list[dict]:
    """The most recent ``n`` journal rows (newest first), optionally strictly before a day."""
    con = connect()
    try:
        if before:
            rows = con.execute(
                "SELECT day, log, plan, generated_at FROM planner_days WHERE day<? "
                "ORDER BY day DESC LIMIT ?", (before, n)).fetchall()
        else:
            rows = con.execute(
                "SELECT day, log, plan, generated_at FROM planner_days "
                "ORDER BY day DESC LIMIT ?", (n,)).fetchall()
    finally:
        con.close()
    return [{"day": r["day"], "log": r["log"] or "", "plan": _loads(r["plan"]),
             "generated_at": r["generated_at"]} for r in rows]


def _loads(s: str | None) -> dict:
    try:
        d = json.loads(s or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


# --- read-only research-state digest (the planner KNOWS about collections/topics) ----
def _state_digest() -> str:
    """A compact, read-only snapshot of the workspace the agent can ground 'tomorrow' and
    'reading' suggestions in — collections (with new-paper signal) and topics (open work).
    Best-effort: any failure degrades to an empty section rather than breaking the plan."""
    from . import library, wiki, topics as topics_mod
    parts = []
    try:
        cols = []
        for c in library.list_collections(with_activity=True):
            slug = c["slug"]
            tag = ""
            try:
                if wiki.update_available(slug):
                    tag = " · NEW papers/notes since the wiki was drafted"
            except Exception:  # noqa: BLE001
                pass
            cols.append(f"- {c['name']} (/c/{slug}; {c.get('paper_count') or 0} papers){tag}")
        if cols:
            parts.append("COLLECTIONS:\n" + "\n".join(cols))
    except Exception as exc:  # noqa: BLE001
        log.debug("planner collection digest failed: %s", exc)
    try:
        tlines = []
        for t in topics_mod.list_topics():
            full = topics_mod.get_topic(t["slug"]) or {}
            steps = [s.get("title", "") for s in ((full.get("generated") or {}).get("next_steps") or [])][:3]
            pend = len([s for s in topics_mod.list_suggestions(t["slug"], "pending")])
            extra = []
            if steps:
                extra.append("next: " + "; ".join(s for s in steps if s))
            if pend:
                extra.append(f"{pend} pending reading")
            tlines.append(f"- {t['title']} (/t/{t['slug']}): {full.get('question','')}"
                          + (" — " + " · ".join(extra) if extra else ""))
        if tlines:
            parts.append("RESEARCH TOPICS:\n" + "\n".join(tlines))
    except Exception as exc:  # noqa: BLE001
        log.debug("planner topic digest failed: %s", exc)
    return "\n\n".join(parts)


# --- the daily agent draft -------------------------------------------------
def _validate(data: dict) -> dict:
    """Coerce the LLM JSON into the plan shape, dropping junk and capping list sizes.
    done/blocked/tomorrow are plain strings; leftover carries {text, age_days};
    reading carries {label, why, link}."""
    def _strs(v):
        out = []
        for x in (v or [])[:_TODO_MAX]:
            s = (x if isinstance(x, str) else (x.get("text", "") if isinstance(x, dict) else "")).strip()
            if s:
                out.append(s)
        return out

    leftover = []
    for x in (data.get("leftover") or [])[:_TODO_MAX]:
        text = (x.get("text", "") if isinstance(x, dict) else str(x)).strip()
        if text:
            age = x.get("age_days") if isinstance(x, dict) else None
            leftover.append({"text": text, "age_days": int(age) if isinstance(age, (int, float)) else 0})

    reading = []
    for x in (data.get("reading") or [])[:_TODO_MAX]:
        if not isinstance(x, dict):
            continue
        label = (x.get("label") or x.get("text") or "").strip()
        if label:
            reading.append({"label": label, "why": (x.get("why") or "").strip(),
                            "link": (x.get("link") or "").strip()})

    return {"done": _strs(data.get("done")), "leftover": leftover,
            "blocked": _strs(data.get("blocked")), "tomorrow": _strs(data.get("tomorrow")),
            "reading": reading}


def generate_plan(day: str | None = None) -> dict:
    """Draft the plan for ``day`` (default today) from the user's recent logs + a read-only
    research-state digest. One LLM call. Returns ``{ok, error, empty}``. Stores the plan on
    the day's row. ``empty`` when there's nothing to plan from (no logs at all)."""
    day = day or _today()
    today = get_day(day)
    history = recent_days(_RECENT_DAYS, before=day)
    if not (today["log"].strip() or any(h["log"].strip() for h in history)):
        return {"ok": False, "empty": True, "error": None}   # nothing written yet — don't draft

    hist_text = "\n\n".join(
        f"[{h['day']}]\n{h['log'].strip()}" for h in reversed(history) if h["log"].strip())
    digest = _state_digest()
    skill = (agent_skills.skill_body("daily-plan")
             or 'Summarize the journal and draft a plan. STRICT JSON: '
                '{"done":[],"leftover":[{"text","age_days"}],"blocked":[],"tomorrow":[],'
                '"reading":[{"label","why","link"}]}.')
    skill += i18n.output_directive()
    user = (f"TODAY ({day}) — the user's freeform log:\n{today['log'].strip() or '(empty)'}\n\n"
            f"EARLIER DAYS (oldest→newest), for rolling unfinished items forward with age:\n"
            f"{hist_text or '(none)'}\n\n"
            f"WORKSPACE STATE (read-only — for grounding 'tomorrow' and 'reading'; do NOT invent "
            f"papers, only point at what's listed):\n{digest or '(none)'}\n\n"
            "Draft the plan as STRICT JSON now.")
    try:
        from .config import agent_model
        out = llm.complete([{"role": "system", "content": skill},
                            {"role": "user", "content": user}], model=agent_model())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "empty": False, "error": str(exc)}
    data = _extract_json(out)
    if not data:
        return {"ok": False, "empty": False, "error": "The planner produced no usable output."}
    set_plan(day, _validate(data))
    return {"ok": True, "empty": False, "error": None}


def _extract_json(text: str) -> dict | None:
    from .wiki import _extract_json as _ej
    return _ej(text)


# --- background job (manual "Plan now") ------------------------------------
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def get_job(day: str | None = None) -> dict | None:
    with _LOCK:
        j = _JOBS.get(day or _today())
        return dict(j) if j else None


def clear_job(day: str | None = None) -> None:
    with _LOCK:
        _JOBS.pop(day or _today(), None)


def start_plan_async(day: str | None = None) -> bool:
    """Draft the plan on a daemon thread; the page polls /planner/status."""
    day = day or _today()
    with _LOCK:
        if (_JOBS.get(day) or {}).get("status") == "running":
            return False
        _JOBS[day] = {"status": "running", "started_at": _now(), "error": None}

    def runner():
        try:
            res = generate_plan(day)
            err = res.get("error") or ("Nothing logged yet to plan from." if res.get("empty") else None)
            with _LOCK:
                _JOBS[day] = {"status": "failed" if err else "done", "error": err,
                              "finished_at": _now()}
        except Exception as exc:  # noqa: BLE001
            with _LOCK:
                _JOBS[day] = {"status": "failed", "error": str(exc), "finished_at": _now()}

    threading.Thread(target=runner, daemon=True, name=f"planner-{day}").start()
    return True


# --- scheduler (hourly due-check; catches up if the app was off) ------------
_SCHED_STOP = threading.Event()


def _planner_hour() -> int:
    try:
        h = int(load_config().get("planner_hour", "18"))
    except (TypeError, ValueError):
        h = 18
    return min(23, max(0, h))


def _due() -> bool:
    """Today's plan should run: we're past the configured hour, it hasn't been drafted yet,
    and there's something to plan from (today's log, or a prior day's)."""
    d = get_day()
    if d["generated_at"]:
        return False
    if datetime.now().hour < _planner_hour():
        return False
    return bool(d["log"].strip() or any(h["log"].strip() for h in recent_days(_RECENT_DAYS, before=d["day"])))


def _tick() -> None:
    try:
        if _due():
            log.info("planner: drafting today's plan (scheduled)")
            generate_plan()
    except Exception as exc:  # noqa: BLE001
        log.warning("planner scheduled tick failed: %s", exc)


def start_scheduler(interval_s: int = 1800) -> None:
    """Start the daily due-check loop (idempotent). Ticks every ``interval_s`` and drafts
    today's plan once, after the configured hour — catching up if the app was closed at the
    scheduled time. No-op under pytest (a test must call generate_plan() explicitly)."""
    import sys
    if "pytest" in sys.modules:
        return

    def loop():
        while not _SCHED_STOP.wait(5):      # small initial delay so startup isn't blocked
            _tick()
            _SCHED_STOP.wait(interval_s)

    threading.Thread(target=loop, daemon=True, name="planner-sched").start()
