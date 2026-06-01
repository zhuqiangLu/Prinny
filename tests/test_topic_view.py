"""Research Topics intelligence layer (app/topic_view.py).

Reuses the wiki field-model fixture (a real 'vlms' collection with concepts/
problems/methods on disk) so the cross-collection union graph + seed→structural
ranking exercise real entities. A topic-aware LLM stub multiplexes the three
prompts (field model / seed anchor / open questions)."""
import json

import pytest

from app import topics, topic_view, wiki
from app.db import connect
from tests.test_wiki import _seed_three_papers, _FIELD_MODEL_JSON


def _stub(messages, model=None):
    user = messages[-1]["content"]
    if "CANDIDATE IDEAS" in user:                       # topic-seed analyze call
        return json.dumps({"seeds": [{"index": 0, "why": "the core idea here"}],
                           "external": [{"name": "Test-Time Training",
                                         "relevance": "high", "reason": "online adaptation"}]})
    if "sub-questions" in user:                         # suggest_questions call
        return json.dumps({"questions": ["What should be adapted?", "How costly is it?"]})
    return json.dumps(_FIELD_MODEL_JSON)                # field-model generation


@pytest.fixture
def topicdb(tmp_path, monkeypatch):
    db = _seed_three_papers(tmp_path, monkeypatch, _stub)
    monkeypatch.setattr(topics, "connect", lambda: connect(db))
    wiki.generate_overview("vlms")                      # writes the field model to disk
    return db


def test_relevant_before_analyze_has_candidates(topicdb):
    slug = topics.create_topic("T", "Can X improve Y?", collections=["vlms"])
    rel = topic_view.relevant_entities(slug)
    assert rel["analyzed"] is False
    assert rel["n_candidates"] > 0          # the vlms field model has ideas to seed from


def test_analyze_then_relevant_ranks_with_seed(topicdb):
    slug = topics.create_topic("T", "Can X improve Y?", collections=["vlms"])
    res = topic_view.analyze(slug)
    assert res.get("seeds", 0) >= 1
    rel = topic_view.relevant_entities(slug)
    assert rel["analyzed"] is True and rel["items"]
    assert any(i["is_seed"] for i in rel["items"])             # the anchor is present
    assert any(i["why"] for i in rel["items"])                 # reasons attached
    # external (cross-pollination) survived the validator
    assert rel["external"] and rel["external"][0]["name"] == "Test-Time Training"
    # entity keys are collection-qualified (cross-collection-safe)
    assert all(i["key"].startswith("vlms::") for i in rel["items"])


def test_suggested_reading_is_grounded(topicdb):
    slug = topics.create_topic("T", "Q?", collections=["vlms"])
    topic_view.analyze(slug)
    reading = topic_view.suggested_reading(slug)
    assert reading
    for r in reading:
        assert r["collection"] == "vlms" and r["why"].startswith("Anchors")
        assert isinstance(r["id"], int)


def test_topic_graph_is_question_centered(topicdb):
    slug = topics.create_topic("T", "Q?", collections=["vlms"])
    topic_view.analyze(slug)
    g = topic_view.topic_graph_view(slug)
    assert g and any(n["id"] == "Q" and n["kind"] == "question" for n in g["nodes"])
    assert all("source" in e and "target" in e for e in g["edges"])
    # no paper nodes in the topic map (ideas only)
    assert all(n["kind"] != "paper" for n in g["nodes"])


def test_suggest_questions_adds_agent_questions(topicdb):
    slug = topics.create_topic("T", "Q?", collections=["vlms"])
    topic_view.analyze(slug)
    assert topic_view.suggest_questions(slug)["added"] >= 1
    qs = topics.get_topic(slug)["questions"]
    assert any(q["source"] == "agent" for q in qs)


def test_no_collections_returns_none(topicdb):
    slug = topics.create_topic("T", "Q?")              # no linked collections
    assert topic_view.relevant_entities(slug) is None
    assert topic_view.analyze(slug).get("error")
