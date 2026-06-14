"""Personal daily planner (app/planner.py): journal store, agent draft, scheduler gate."""
import json

import pytest

from app import planner
from app.db import connect, init_db


@pytest.fixture
def plandb(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    monkeypatch.setattr(planner, "connect", lambda: connect(db))
    return db


def test_save_and_get_log(plandb):
    planner.save_log("2026-06-14", "did X; still need Y")
    d = planner.get_day("2026-06-14")
    assert d["log"] == "did X; still need Y" and d["plan"] == {} and d["generated_at"] is None


def test_recent_days_orders_newest_first(plandb):
    for day in ("2026-06-10", "2026-06-12", "2026-06-11"):
        planner.save_log(day, f"log {day}")
    days = [d["day"] for d in planner.recent_days(10)]
    assert days == ["2026-06-12", "2026-06-11", "2026-06-10"]
    assert planner.recent_days(10, before="2026-06-11") == \
        planner.recent_days(10, before="2026-06-11")  # deterministic
    assert [d["day"] for d in planner.recent_days(10, before="2026-06-11")] == ["2026-06-10"]


def test_generate_plan_empty_when_no_logs(plandb, monkeypatch):
    monkeypatch.setattr(planner, "_state_digest", lambda: "")
    res = planner.generate_plan("2026-06-14")
    assert res["empty"] is True and res["ok"] is False


def test_generate_plan_drafts_and_stores(plandb, monkeypatch):
    planner.save_log("2026-06-14", "read 2 papers; still need the ablation")
    monkeypatch.setattr(planner, "_state_digest", lambda: "COLLECTIONS:\n- LV (/c/lv; 5 papers)")
    captured = {}
    def fake_complete(messages, model=None):
        captured["sys"] = messages[0]["content"]
        captured["user"] = messages[-1]["content"]
        return json.dumps({
            "done": ["read 2 papers"],
            "leftover": [{"text": "run the ablation", "age_days": 1}],
            "blocked": [],
            "tomorrow": ["run the ablation", "skim LV new papers"],
            "reading": [{"label": "LV collection", "why": "5 papers", "link": "/c/lv"}],
            "junk_field": "ignored",
        })
    monkeypatch.setattr(planner.llm, "complete", fake_complete)
    res = planner.generate_plan("2026-06-14")
    assert res["ok"] is True
    plan = planner.get_day("2026-06-14")["plan"]
    assert plan["done"] == ["read 2 papers"]
    assert plan["leftover"][0]["text"] == "run the ablation"
    assert plan["reading"][0]["link"] == "/c/lv"
    assert "junk_field" not in plan                      # validator drops unknown keys
    assert "still need the ablation" in captured["user"]   # the user's log reached the prompt


def test_validate_caps_and_coerces():
    big = {"tomorrow": [f"t{i}" for i in range(50)], "done": ["a", {"text": "b"}, 7, ""]}
    out = planner._validate(big)
    assert len(out["tomorrow"]) <= planner._TODO_MAX
    assert out["done"] == ["a", "b"]                      # str + dict.text kept; junk dropped


def test_scheduler_due_gate(plandb, monkeypatch):
    # before the configured hour → not due, even with a log
    planner.save_log(planner._today(), "something")
    monkeypatch.setattr(planner, "_planner_hour", lambda: 23)

    class _Dt:
        @staticmethod
        def now():
            import datetime as _d
            return _d.datetime(2026, 6, 14, 9, 0, 0)  # 09:00 < 23:00
    monkeypatch.setattr(planner, "datetime", _Dt)
    assert planner._due() is False
    monkeypatch.setattr(planner, "_planner_hour", lambda: 8)   # now 09:00 >= 08:00
    assert planner._due() is True
