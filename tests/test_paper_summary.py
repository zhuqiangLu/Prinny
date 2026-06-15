"""Auto-summary grounded in agent-created highlights (app/paper_summary.py)."""
import json

import pytest

from app import paper_summary, annotations as ann_mod, note_drafts, library, notes
from app.db import connect, init_db

SCHEME = [
    {"color": "#ffd400", "label": "methodology"},
    {"color": "#6fb3ff", "label": "insight"},
    {"color": "#c800ff", "label": "motivation"},
]


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "app.sqlite"
    init_db(p)
    con = connect(p)
    con.execute("INSERT INTO collections(slug,name) VALUES('c','C')")
    con.execute("INSERT INTO papers(id,title) VALUES(1,'A Paper')")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('c',1)")
    con.commit(); con.close()
    for mod in (ann_mod, note_drafts, library):
        monkeypatch.setattr(mod, "connect", lambda: connect(p))
    monkeypatch.setattr(paper_summary, "highlight_scheme", lambda: SCHEME)
    return p


def test_summarize_creates_highlights_and_draft_and_drops_fabricated(db, monkeypatch):
    pages = ["We propose a contrastive method to compress KV caches.",
             "Our key insight is that frame redundancy is high in long video."]
    monkeypatch.setattr(paper_summary, "_page_texts", lambda pid: pages)
    monkeypatch.setattr(paper_summary.llm, "complete", lambda m, model=None: json.dumps({
        "summary": "A method to compress long-video KV caches via contrastive selection.",
        "highlights": [
            {"meaning": "methodology", "quote": "contrastive method to compress KV caches", "page": 1},
            {"meaning": "insight", "quote": "frame redundancy is high in long video", "page": 2},
            {"meaning": "motivation", "quote": "this sentence is not anywhere in the paper at all", "page": 1},
            {"meaning": "not_in_scheme", "quote": "We propose", "page": 1},
        ]}))
    res = paper_summary.summarize_from_highlights("c", 1)
    assert res["ok"] is True and res["n_highlights"] == 2     # fabricated + off-scheme dropped

    hls = ann_mod.list_app(1, "c")
    assert len(hls) == 2 and all(h["by_agent"] for h in hls)
    colors = {h["color"] for h in hls}
    assert "#ffd400" in colors and "#6fb3ff" in colors        # methodology + insight colors
    assert all((h["selected_text"] or "") for h in hls)

    draft = note_drafts.get("c", 1)
    assert draft and "compress long-video KV caches" in draft  # the readable summary prose
    assert "(p.1)" not in draft                                # NOT a rigid citation list
    # The draft must round-trip through the accept parser into the note's Summary field
    # (bare prose would be silently dropped — the bug this guards).
    parsed = notes._parse_body(draft)
    assert "compress long-video KV caches" in parsed["summary"]
    assert not parsed["thoughts"] and not parsed["key_quotes"]  # agent summarizes; thoughts are yours


def test_rerun_replaces_agent_highlights(db, monkeypatch):
    monkeypatch.setattr(paper_summary, "_page_texts", lambda pid: ["the proposed method works well"])
    monkeypatch.setattr(paper_summary.llm, "complete", lambda m, model=None: json.dumps({
        "summary": "x", "highlights": [{"meaning": "methodology",
                                        "quote": "the proposed method", "page": 1}]}))
    paper_summary.summarize_from_highlights("c", 1)
    paper_summary.summarize_from_highlights("c", 1)
    assert len(ann_mod.list_app(1, "c")) == 1                 # not duplicated


def test_no_pdf_text_errors(db, monkeypatch):
    monkeypatch.setattr(paper_summary, "_page_texts", lambda pid: ["", "  "])
    res = paper_summary.summarize_from_highlights("c", 1)
    assert res["ok"] is False and "No PDF text" in res["error"]


def test_delete_and_keep_agent(db, monkeypatch):
    a = ann_mod.create("c", 1, kind="highlight", color="#ffd400", page=0,
                       position_json="{}", selected_text="q", by_agent=1)
    assert ann_mod.list_app(1, "c")[0]["by_agent"] == 1
    ann_mod.keep_agent(a["id"])
    assert ann_mod.list_app(1, "c")[0]["by_agent"] == 0        # promoted to a user highlight
    a2 = ann_mod.create("c", 1, kind="highlight", color="#6fb3ff", page=0,
                        position_json="{}", selected_text="q2", by_agent=1)
    assert ann_mod.delete_agent(1, "c") == 1                   # only the still-agent one removed
    assert len(ann_mod.list_app(1, "c")) == 1
