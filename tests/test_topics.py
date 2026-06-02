"""Research Topics data layer (app/topics.py)."""
import pytest

from app import topics
from app.db import connect, init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "app.sqlite"
    init_db(p)
    monkeypatch.setattr(topics, "connect", lambda: connect(p))
    return p


def test_create_requires_question(db):
    with pytest.raises(ValueError):
        topics.create_topic("My topic", "   ")


def test_create_and_get_roundtrip(db):
    slug = topics.create_topic(
        "TTT for long video",
        "Can test-time training improve long-video reasoning?",
        collections=["longvideo", "ttt", "longvideo"],  # dup dropped
        description="exploring adaptation under drift")
    t = topics.get_topic(slug)
    assert t["question"].startswith("Can test-time training")
    assert t["status"] == "exploring"
    assert t["collections"] == ["longvideo", "ttt"]   # de-duped, sorted
    assert t["description"] == "exploring adaptation under drift"
    assert t["hypotheses"] == [] and t["questions"] == []


def test_title_defaults_to_question(db):
    slug = topics.create_topic("", "How should memory be represented for streaming VLMs?")
    assert topics.get_topic(slug)["title"].startswith("How should memory")


def test_v2_lifecycle_and_timeline(db):
    slug = topics.create_topic("T", "Q?")
    assert topics.get_topic(slug)["lifecycle"] == "investigation"   # ALTER default
    assert topics.set_lifecycle(slug, "active") is True
    assert topics.set_lifecycle(slug, "bogus") is False
    t = topics.get_topic(slug)
    assert t["lifecycle"] == "active" and t["lifecycle_label"] == "Active Project"
    # set_lifecycle logs a timeline event
    assert any(e["event"] == "status_changed" for e in t["timeline"])


def test_v2_inquiry_crud(db):
    slug = topics.create_topic("T", "Q?")
    assert topics.add_assumption(slug, "An assumption.")
    assert topics.add_unknown(slug, "An unknown?", priority="high")
    assert topics.add_experiment(slug, "Exp", method="m", metric="x")
    assert topics.add_note(slug, "A note.")
    assert topics.add_assumption(slug, "   ") is False          # blank rejected
    t = topics.get_topic(slug)
    assert [a["text"] for a in t["assumptions"]] == ["An assumption."]
    assert t["unknowns"][0]["priority"] == "high"
    assert t["experiments"][0]["metric"] == "x"
    assert t["notes"][0]["body"] == "A note."
    # deletes are topic-scoped
    assert topics.delete_unknown(slug, t["unknowns"][0]["id"])
    assert topics.get_topic(slug)["unknowns"] == []


def test_replace_investigation_links_hypotheses(db):
    slug = topics.create_topic("T", "Q?")
    topics.replace_investigation(
        slug,
        assumptions=["A1"],
        hypotheses=[{"text": "H one", "status": "supported", "support_count": 3, "counter_count": 0},
                    {"text": "H two", "status": "mixed", "support_count": 1, "counter_count": 1}],
        evidence=[{"kind": "supporting", "claim": "c1", "paper_ref": "R", "paper_id": 7,
                   "collection": "c", "hyp_index": 0},
                  {"kind": "missing", "claim": "gap", "hyp_index": 1}],
        unknowns=[{"text": "u?", "priority": "high", "hyp_index": 1}],
        experiments=[{"title": "e", "hyp_index": 0}],
        generated={"key_terms": ["k"], "confidence": {"score": 0.75, "label": "High"}})
    t = topics.get_topic(slug)
    h0, h1 = t["hypotheses"][0], t["hypotheses"][1]
    assert h0["status"] == "supported" and h0["support_count"] == 3
    # hyp_index resolved to real hypothesis ids
    sup = next(e for e in t["evidence"] if e["kind"] == "supporting")
    assert sup["hypothesis_id"] == h0["id"] and sup["paper_id"] == 7
    assert t["unknowns"][0]["hypothesis_id"] == h1["id"]
    assert t["generated"]["confidence"]["label"] == "High"
    # a second generate replaces (not appends)
    topics.replace_investigation(slug, assumptions=[], hypotheses=[], evidence=[],
                                 unknowns=[], experiments=[], generated={})
    assert topics.get_topic(slug)["hypotheses"] == []


def test_slug_uniqueness(db):
    a = topics.create_topic("Memory", "Q1?")
    b = topics.create_topic("Memory", "Q2?")
    assert a != b


def test_hypotheses_crud(db):
    slug = topics.create_topic("T", "Q?")
    assert topics.add_hypothesis(slug, "H1: online adaptation improves retention")
    assert topics.add_hypothesis(slug, "H2: memory drift ~ test-time shift")
    hs = topics.get_topic(slug)["hypotheses"]
    assert [h["text"][:2] for h in hs] == ["H1", "H2"]   # ordered by position
    hid = hs[0]["id"]
    assert topics.edit_hypothesis(slug, hid, "H1 edited")
    assert topics.delete_hypothesis(slug, hs[1]["id"])
    left = topics.get_topic(slug)["hypotheses"]
    assert len(left) == 1 and left[0]["text"] == "H1 edited"
    # empty hypothesis is rejected
    assert topics.add_hypothesis(slug, "   ") is False


def test_questions_crud_and_source(db):
    slug = topics.create_topic("T", "Q?")
    assert topics.add_question(slug, "What should be adapted?", "user")
    assert topics.add_question(slug, "How expensive is TTT?", "agent")
    qs = topics.get_topic(slug)["questions"]
    assert {q["source"] for q in qs} == {"user", "agent"}
    assert topics.add_question(slug, "bad", "nonsense") is False
    assert topics.delete_question(slug, qs[0]["id"])
    assert len(topics.get_topic(slug)["questions"]) == 1


def test_status_and_collections_and_delete(db):
    slug = topics.create_topic("T", "Q?", collections=["a"])
    assert topics.set_status(slug, "active")
    assert topics.set_status(slug, "bogus") is False
    assert topics.get_topic(slug)["status"] == "active"
    assert topics.set_collections(slug, ["x", "y", "x"])
    assert topics.get_topic(slug)["collections"] == ["x", "y"]
    assert topics.delete_topic(slug) is True
    assert topics.get_topic(slug) is None


def test_cascade_delete_clears_children(db):
    slug = topics.create_topic("T", "Q?", collections=["a"])
    topics.add_hypothesis(slug, "H")
    topics.add_question(slug, "OQ")
    topics.delete_topic(slug)
    # a brand-new topic reusing nothing; verify orphan rows are gone
    con = connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM topic_hypotheses").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM topic_questions").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM topic_collections").fetchone()[0] == 0
    finally:
        con.close()


def test_list_topics_orders_recent_first(db):
    topics.create_topic("First", "Q1?")
    s2 = topics.create_topic("Second", "Q2?")
    # touch the first so it becomes most-recent
    topics.add_hypothesis(topics.list_topics()[-1]["slug"], "x")
    names = [t["title"] for t in topics.list_topics()]
    assert set(names) == {"First", "Second"}
    assert all("n_collections" in t for t in topics.list_topics())
