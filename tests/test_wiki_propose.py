"""Chat → wiki proposal engine (propose-and-gate): grounding + per-section apply."""
import json

import pytest

import app.wiki as wiki
import app.wiki_propose as wp
from app.db import connect
from tests.test_wiki import _seed_three_papers, _llm_stub


@pytest.fixture
def wikidb(tmp_path, monkeypatch):
    db = _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    monkeypatch.setattr(wp, "connect", lambda: connect(db))
    wiki.generate_overview("vlms")          # writes thesis / landscape / concepts to disk
    return db


def test_evidence_proposal_requires_a_collection_paper(wikidb):
    # a belief with no cited paper is rejected before it ever persists
    r = wp.create_proposal("vlms", "belief", "add",
                           {"title": "Memory compression preserves reasoning."})
    assert r["ok"] is False and "paper" in r["error"]
    # citing a real collection paper passes
    r = wp.create_proposal("vlms", "belief", "add",
                           {"title": "Memory compression preserves reasoning."},
                           supporting_papers=["2401.00001"])
    assert r["ok"] and r["id"]


def test_thesis_is_grounded_in_conversation_not_a_paper(wikidb):
    cur = wiki.current_thesis("vlms")
    proposed = {**cur, "one_paragraph": "A sharper thesis drawn from our chat."}
    r = wp.create_proposal("vlms", "thesis", "replace", proposed, grounding="we discussed cost")
    assert r["ok"]                                   # no paper required for thesis
    assert wp.accept_proposal(r["id"])["ok"]
    assert wiki.current_thesis("vlms")["one_paragraph"].startswith("A sharper")
    assert wp.get_proposal(r["id"])["status"] == "accepted"


def test_accept_belief_writes_accepted_file(wikidb):
    r = wp.create_proposal("vlms", "belief", "add",
                           {"title": "Reasoning state is structured, not noise.", "confidence": "medium"},
                           supporting_papers=["2401.00002"])
    assert wp.accept_proposal(r["id"])["ok"]
    files = list(wiki._beliefs_dir("vlms").glob("*.md"))
    assert files
    meta, _ = wiki.frontmatter.parse(files[0].read_text(encoding="utf-8"))
    assert meta["status"] == "accepted" and meta["supporting_papers"] == ["2401.00002"]


def test_accept_landscape_add_respects_cap(wikidb):
    r = wp.create_proposal("vlms", "landscape", "add_item",
                           {"column": "problems", "text": "A genuinely new problem item",
                            "papers": ["2401.00001"]}, supporting_papers=["2401.00001"])
    assert r["ok"] and wp.accept_proposal(r["id"])["ok"]
    ls = json.loads(wiki._landscape_json_path("vlms").read_text(encoding="utf-8"))["landscape"]
    assert len(ls["problems"]) <= wiki._LANDSCAPE_MAX_ITEMS      # cap still enforced


def test_accept_concept_then_dedupes(wikidb):
    r = wp.create_proposal("vlms", "concepts", "add_concept",
                           {"name": "Brand New Concept", "blurb": "x", "papers": ["2401.00001"]},
                           supporting_papers=["2401.00001"])
    assert wp.accept_proposal(r["id"])["ok"]
    assert "Brand New Concept" in wiki._concept_names("vlms")
    r2 = wp.create_proposal("vlms", "concepts", "add_concept", {"name": "Brand New Concept"},
                            supporting_papers=["2401.00001"])
    assert wp.accept_proposal(r2["id"])["ok"] is False          # duplicate rejected at apply


def test_dismiss_marks_dismissed(wikidb):
    cur = wiki.current_thesis("vlms")
    r = wp.create_proposal("vlms", "thesis", "replace", {**cur, "one_paragraph": "z"}, grounding="g")
    assert wp.dismiss_proposal(r["id"])["ok"]
    assert wp.get_proposal(r["id"])["status"] == "dismissed"
    assert wp.list_pending("vlms") == []
