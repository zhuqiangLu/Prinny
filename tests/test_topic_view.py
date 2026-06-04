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


def test_generate_investigation_grounds_and_persists(topicdb, monkeypatch):
    """The big v2 generation: evidence must cite a real linked-collection paper
    (ungrounded dropped), hypotheses carry agent status, confidence is computed."""
    slug = topics.create_topic("T", "Can TTT improve long-video reasoning?", collections=["vlms"])
    _, refmap = topic_view._topic_digest(["vlms"])
    assert refmap                                       # the vlms field model has papers
    ref = next(iter(refmap))
    payload = {
        "assumptions": ["A premise."],
        "hypotheses": [{"statement": "First is supported.", "status": "supported",
                        "support_count": 2, "counter_count": 0},
                       {"statement": "Second is mixed.", "status": "mixed",
                        "support_count": 1, "counter_count": 1}],
        "supporting_evidence": [{"claim": "Grounded claim.", "paper": ref, "hypothesis": "H1"},
                                {"claim": "Ungrounded — drop me.", "paper": "NOPE", "hypothesis": "H1"}],
        "counter_evidence": [{"claim": "A counter.", "paper": ref, "hypothesis": "H2"}],
        "missing_evidence": [{"claim": "A gap.", "hypothesis": "H1"}],
        "unknowns": [{"question": "An unknown?", "priority": "high", "hypothesis": "H2"}],
        "experiments": [{"title": "Exp", "method": "m", "metric": "x", "hypothesis": "H2"}],
        "next_steps": [{"title": "Do X", "detail": "why"}],
        "key_terms": ["Alpha", "Beta"],
    }
    monkeypatch.setattr(topic_view.llm, "complete", lambda *a, **k: json.dumps(payload))
    res = topic_view.generate_investigation(slug)
    assert res["ok"] is True

    t = topics.get_topic(slug)
    assert len(t["assumptions"]) == 1 and len(t["hypotheses"]) == 2
    kinds = [e["kind"] for e in t["evidence"]]
    assert kinds.count("supporting") == 1               # the ungrounded one was dropped (the gate)
    assert kinds.count("counter") == 1 and kinds.count("missing") == 1
    sup = next(e for e in t["evidence"] if e["kind"] == "supporting")
    assert sup["paper_id"] and sup["collection"] == "vlms"      # resolved to a real paper
    assert t["hypotheses"][0]["status"] == "supported"
    assert t["generated"]["confidence"]["label"] == "High"     # mean(1, .5) = .75
    assert t["generated"]["key_terms"] == ["Alpha", "Beta"]
    assert any(e["hypothesis_id"] for e in t["evidence"])       # linked to a hypothesis


def test_suggest_reading_accept_links_unverified_and_survives_regenerate(topicdb, monkeypatch):
    """Purpose=challenge stores a hypothesis-targeted suggestion; Accept imports the
    paper and files an UNVERIFIED counter-evidence row; a later regenerate preserves
    that row and re-links it to the rebuilt hypothesis by text."""
    import app.discover as discover, app.library as library, app.triage as triage
    slug = topics.create_topic("T", "Can TTT help?", collections=["vlms"])
    # one hypothesis to target (no LLM)
    topics.replace_investigation(slug, assumptions=[],
        hypotheses=[{"text": "Adapting memory beats full model.", "status": "mixed",
                     "support_count": 1, "counter_count": 1}],
        evidence=[], unknowns=[], experiments=[], generated={})
    hyp = topics.get_topic(slug)["hypotheses"][0]

    monkeypatch.setattr(library, "list_papers", lambda s: [])
    monkeypatch.setattr(discover, "find_related_papers",
                        lambda focus, exclude_titles=None, limit=10, intent="": [
                            {"arxiv_id": "2502.0001", "title": "Counter Paper",
                             "summary": "abs", "note": "argues full-model adaptation wins"}])
    res = topic_view.suggest_reading(slug, purpose="challenge", target_id=hyp["id"])
    assert res["added"] == 1
    sug = topics.list_suggestions(slug)[0]
    assert sug["target_kind"] == "hypothesis" and sug["stance"] == "counter"

    # Accept → import (stubbed) + unverified counter-evidence on the hypothesis
    monkeypatch.setattr(triage, "accept_arxiv_into_collection",
                        lambda *a, **k: 4242)
    acc = topics.accept_suggestion(slug, sug["id"], "vlms")
    assert acc["ok"] and acc["linked_evidence"] is True
    ev = topics.get_topic(slug)["evidence"]
    unv = [e for e in ev if e["unverified"]]
    assert len(unv) == 1 and unv[0]["kind"] == "counter" and unv[0]["hypothesis_id"] == hyp["id"]
    assert topics.list_suggestions(slug) == []          # no longer pending

    # Regenerate with the target hypothesis SECOND (so it gets a different id) —
    # the unverified row must survive and re-link to it BY TEXT, not by old id.
    topics.replace_investigation(slug, assumptions=["A"],
        hypotheses=[{"text": "An unrelated new hypothesis.", "status": "unknown",
                     "support_count": 0, "counter_count": 0},
                    {"text": "Adapting memory beats full model.", "status": "supported",
                     "support_count": 3, "counter_count": 0}],
        evidence=[], unknowns=[], experiments=[], generated={})
    t2 = topics.get_topic(slug)
    target = next(h for h in t2["hypotheses"] if h["text"] == "Adapting memory beats full model.")
    other = next(h for h in t2["hypotheses"] if h["text"] == "An unrelated new hypothesis.")
    unv2 = [e for e in t2["evidence"] if e["unverified"]]
    assert len(unv2) == 1
    assert unv2[0]["hypothesis_id"] == target["id"] != other["id"]   # re-linked by text


def test_recommend_collection_returns_linked_or_empty(topicdb):
    """Best-fit picker default: returns a linked collection (overlap or fallback),
    and '' when nothing is linked."""
    slug = topics.create_topic("T", "Q?", collections=["vlms"])
    assert topic_view.recommend_collection(slug, "vision language models", "about VLMs") == "vlms"
    bare = topics.create_topic("T2", "Q?")
    assert topic_view.recommend_collection(bare, "x", "y") == ""


def test_generate_async_job_lifecycle(topicdb, monkeypatch):
    """start_generate_async runs on a thread and publishes running → done state
    that the overlay polls; clear removes it."""
    import threading
    slug = topics.create_topic("T", "Q?", collections=["vlms"])
    release = threading.Event()

    def slow_complete(*a, **k):
        release.wait(2)                                  # hold the 'running' window open
        return json.dumps({"assumptions": ["A premise."], "hypotheses": [
            {"statement": "Online adaptation helps retention.", "status": "supported",
             "support_count": 1, "counter_count": 0}]})
    monkeypatch.setattr(topic_view.llm, "complete", slow_complete)

    assert topic_view.start_generate_async(slug) is True
    assert topic_view.start_generate_async(slug) is False      # already running → no double-start
    job = topic_view.get_generate_job(slug)
    assert job["status"] == "running"
    assert topic_view.gen_stage_label(job)                     # honest label, no %
    release.set()
    for _ in range(100):                                       # wait for the worker to finish
        job = topic_view.get_generate_job(slug)
        if job and job["status"] == "done":
            break
        import time; time.sleep(0.02)
    assert job["status"] == "done"
    assert topics.get_topic(slug)["hypotheses"]                # it actually wrote
    topic_view.clear_generate_job(slug)
    assert topic_view.get_generate_job(slug) is None
