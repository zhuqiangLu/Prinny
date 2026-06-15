"""Personal daily planner (app/planner.py, v2 cards): card CRUD, activity, meta summary."""
import json

import pytest

from app import planner
from app.db import connect, init_db


@pytest.fixture
def plandb(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    monkeypatch.setattr(planner, "connect", lambda: connect(db))
    # activity_today + digest read collections/topics; stub them to an empty workspace.
    monkeypatch.setattr(planner, "activity_today",
                        lambda day=None: {"papers_read": 0, "read_titles": [], "collections": []})
    monkeypatch.setattr(planner, "_state_digest", lambda: "")
    return db


def test_card_crud(plandb):
    cid = planner.add_card("2026-06-15", kind="plan", title="T", body="run ablation")
    cards = planner.list_cards("2026-06-15")
    assert len(cards) == 1 and cards[0]["kind"] == "plan" and cards[0]["body"] == "run ablation"
    planner.update_card(cid, "T2", "run ablation today", kind="note")
    c = planner.list_cards("2026-06-15")[0]
    assert c["title"] == "T2" and c["kind"] == "note" and "today" in c["body"]
    planner.delete_card(cid)
    assert planner.list_cards("2026-06-15") == []


def test_add_card_defaults_unknown_kind_to_note(plandb):
    planner.add_card("2026-06-15", kind="bogus")
    assert planner.list_cards("2026-06-15")[0]["kind"] == "note"


def test_generate_summary_empty_with_no_cards_no_activity(plandb):
    res = planner.generate_summary("2026-06-15")
    assert res["empty"] is True and res["ok"] is False


def test_generate_summary_rolls_up_cards(plandb, monkeypatch):
    planner.add_card("2026-06-15", kind="plan", title="", body="finish the KV-cache ablation")
    monkeypatch.setattr(planner, "activity_today",
                        lambda day=None: {"papers_read": 3, "read_titles": ["A", "B", "C"],
                                          "collections": [{"slug": "lv", "name": "LV", "read": 3, "added": 1}]})
    captured = {}
    def fake_complete(messages, model=None):
        captured["user"] = messages[-1]["content"]
        return json.dumps({
            "summary": "Read 3 papers; pushed the ablation.",
            "collections": [{"slug": "lv", "name": "LV", "note": "3 read"}],
            "experiments": ["Ablate the memory module only"],
            "leftover": [{"text": "finish the KV-cache ablation", "age_days": 1}],
            "tomorrow": ["write up results"], "junk": "x"})
    monkeypatch.setattr(planner.llm, "complete", fake_complete)
    res = planner.generate_summary("2026-06-15")
    assert res["ok"] is True
    s = planner.get_day("2026-06-15")["summary"]
    assert s["summary"].startswith("Read 3")
    assert s["collections"][0]["slug"] == "lv" and s["experiments"]
    assert "junk" not in s                                  # validator drops unknown keys
    assert "Papers read today: 3" in captured["user"]       # real activity reached the prompt
    assert "finish the KV-cache ablation" in captured["user"]   # the card reached the prompt


def test_get_day_shape(plandb):
    d = planner.get_day("2026-06-15")
    assert set(d) == {"day", "cards", "summary", "generated_at", "activity"}
    assert d["cards"] == [] and d["summary"] == {} and d["activity"]["papers_read"] == 0


def test_scheduler_due_gate(plandb, monkeypatch):
    planner.add_card(planner._today(), body="something")
    monkeypatch.setattr(planner, "_planner_hour", lambda: 23)

    class _Dt:
        @staticmethod
        def now():
            import datetime as _d
            return _d.datetime(2026, 6, 15, 9, 0, 0)
    monkeypatch.setattr(planner, "datetime", _Dt)
    assert planner._due() is False                          # 09:00 < 23:00
    monkeypatch.setattr(planner, "_planner_hour", lambda: 8)
    assert planner._due() is True                           # 09:00 >= 08:00, has a card, no summary
