"""Cognitive-model wiki pipeline with a mocked LLM.

Covers the Field Model (thesis + landscape), concept extraction, attention
scoring, the belief tray, and the async draft job — all driven by a stubbed
``llm.complete`` so no CLI agent is needed.
"""

from __future__ import annotations

import json

import app.wiki as wiki
from app.db import connect, init_db


# --- cognitive-model wiki (Phase A — Field Model, 2026-05-31) tests ----------
# One LLM call now. The stub returns the field-model JSON shape (thesis +
# landscape). Hallucinated/over-long landscape items + missing thesis fields
# exercise the validator's clamp and drop rules.

_FIELD_MODEL_JSON = {
    "thesis": {
        "one_paragraph": "The collection circles efficient long-context VLMs and the compression-vs-recall trade-off.",
        "core_tension": "Reduce memory while preserving reasoning.",
        "key_intuition": "Important reasoning states form structure, not noise.",
        "central_question": "Can we keep what matters and drop what doesn't?",
    },
    "landscape": {
        # 8 problems supplied -> validator clamps to _LANDSCAPE_MAX_ITEMS (6).
        "problems": [
            "KV cache memory explosion",
            "Long-context degradation",
            "Reasoning-state preservation",
            "Cross-modality differences",
            "Quantization fidelity",
            "Throughput at long context",
            "Eviction policy choice",          # 7th — dropped
            "ab",                              # too short — dropped
        ],
        # Methods as paper-anchored objects (new shape) — exercises the
        # problem/method → paper membership the graph engine consumes. NOPE is
        # filtered by the validator.
        "methods": [
            {"text": "Semantic-anchor approaches", "papers": ["2401.00001", "NOPE"]},
            {"text": "Diversity-aware compression", "papers": ["2401.00002"]},
            "Thought-adaptive pruning",          # legacy string form still accepted
        ],
        "debates": ["Is importance pruning sufficient?",
                    "Is reasoning information localized?"],
        "open_questions": ["What actually needs to be preserved for reasoning?",
                            "Are reasoning traces compressible?"],
    },
    # Phase B: concepts (with paper membership)
    "concepts": [
        {"name": "Reasoning Preservation", "synonyms": ["reasoning preservation",
            "preserving reasoning", "reasoning-state"], "blurb": "Keep the KV cache that matters.",
         "papers": ["2401.00002", "NOPE"]},        # NOPE filtered; 2401.00002 kept
        {"name": "Semantic Anchors", "synonyms": ["semantic anchor", "anchor token"],
         "blurb": "Tokens that carry the meaningful structure."},
        {"name": "KV Distillation", "synonyms": ["kv distillation", "distill kv cache"],
         "blurb": "Train a student cache from a teacher cache."},
        {"name": "ab", "synonyms": []},                # too short — dropped
        {"name": "Reasoning Preservation", "synonyms": []},  # duplicate slug — dropped
    ],
}


def _llm_stub(field_json=None):
    """Build a llm.complete stub for the Phase A one-shot pipeline. Returns the
    field-model JSON. ``field_json=None`` simulates LLM failure."""
    fj = field_json if field_json is not None else _FIELD_MODEL_JSON
    def stub(messages, model=None):
        if fj is None:
            raise RuntimeError("simulated LLM failure")
        return json.dumps(fj)
    return stub


def _seed_three_papers(tmp_path, monkeypatch, stub):
    """Shared fixture: a 3-paper collection with the new pipeline's stubbed LLM."""
    import app.library as library
    import app.pdf_store as pdf_store
    db = tmp_path / "app.sqlite"
    init_db(db)
    con = connect(db)
    con.execute("INSERT INTO collections(slug,name) VALUES('vlms','VLMs')")
    for i in (1, 2, 3):
        con.execute("INSERT INTO papers(id,title,abstract,arxiv_id,origin) "
                    f"VALUES({i}, 'P{i}', 'abs{i}', '2401.0000{i}', 'app-created')")
        con.execute(f"INSERT INTO collection_papers(collection_slug,paper_id) VALUES('vlms', {i})")
    con.commit(); con.close()
    monkeypatch.setattr(wiki, "connect", lambda: connect(db))
    monkeypatch.setattr(wiki, "COLLECTIONS_DIR", tmp_path / "collections")
    monkeypatch.setattr(library, "connect", lambda: connect(db))
    monkeypatch.setattr(pdf_store, "load_config", lambda: {"pdf_store_path": str(tmp_path / "store")})
    monkeypatch.setattr("app.llm.complete", stub)
    return db


def test_generate_overview_writes_field_model_files(tmp_path, monkeypatch):
    """The one-shot pipeline writes wiki/sections/{thesis,landscape}.md with
    agent-tagged frontmatter. Legacy wiki/starter/ is NOT touched."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    assert wiki.generate_overview("vlms") is True
    sdir = tmp_path / "collections" / "vlms" / "wiki" / "sections"
    assert (sdir / "thesis.md").is_file()
    assert (sdir / "landscape.md").is_file()
    thesis_meta, _ = wiki.frontmatter.parse((sdir / "thesis.md").read_text())
    assert thesis_meta["generated_by"] == "agent"
    assert thesis_meta["generator"] == "field-model"
    assert thesis_meta["type"] == "thesis"


def test_validate_field_model_caps_long_lists_and_drops_short_items():
    """The validator clamps landscape columns to _LANDSCAPE_MAX_ITEMS and drops
    items shorter than 3 chars (e.g., the 'ab' planted in the fixture)."""
    out = wiki._validate_field_model(_FIELD_MODEL_JSON,
                                      valid_refs={"2401.00001", "2401.00002", "2401.00003"})
    assert len(out["landscape"]["problems"]) == wiki._LANDSCAPE_MAX_ITEMS
    # 'ab' (length 2) was dropped; the 7th honest item also fell off the end.
    assert "ab" not in [p["text"] for p in out["landscape"]["problems"]]
    # Methods came in under the cap — survives intact, as {text, papers} nodes.
    assert len(out["landscape"]["methods"]) == 3
    sem = next(m for m in out["landscape"]["methods"] if m["text"] == "Semantic-anchor approaches")
    assert sem["papers"] == ["2401.00001"]      # NOPE filtered to valid refs only


def test_validate_field_model_empty_input_returns_empty_shape():
    """An empty/malformed LLM payload doesn't crash — every field is empty so
    the pipeline gate (in generate_overview) refuses to write."""
    out = wiki._validate_field_model({})
    assert out["thesis"]["one_paragraph"] == ""
    assert all(out["landscape"][k] == [] for k in
               ("problems", "methods", "debates", "open_questions"))


def test_generate_overview_refuses_empty_field_model(tmp_path, monkeypatch):
    """If the LLM returns nothing usable, the pipeline refuses (returns False,
    writes nothing). Saves the user from empty section pages."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub(field_json={}))
    assert wiki.generate_overview("vlms") is False
    assert not (tmp_path / "collections" / "vlms" / "wiki" / "sections").exists()


def test_generate_overview_is_nondestructive_unless_forced(tmp_path, monkeypatch):
    """A second generate_overview without force=True is a no-op (existing tree
    survives). force=True regenerates."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    assert wiki.generate_overview("vlms") is True
    assert wiki.generate_overview("vlms") is False                  # idempotent
    assert wiki.generate_overview("vlms", force=True) is True       # forced


def test_papers_to_concepts_tags_by_synonym_match(tmp_path, monkeypatch):
    """papers_to_concepts maps each paper to concept(s) whose synonyms appear in
    the paper's title+abstract. Deterministic, no LLM. Papers matching nothing
    are absent (no fake tags)."""
    db = _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    con = connect(db)
    con.execute("UPDATE papers SET title='Streaming Inference for Video', "
                "abstract='online processing of continuous streams' WHERE id=1")
    con.execute("UPDATE papers SET title='Unrelated topic', abstract='nothing matches' WHERE id=2")
    con.commit(); con.close()
    concepts = [
        {"name": "Streaming Inference", "slug": "streaming-inference",
         "synonyms": ["streaming inference", "online processing", "continuous streams"]},
        {"name": "Token Efficiency", "slug": "token-efficiency",
         "synonyms": ["token efficiency", "patch pruning"]},
    ]
    tags = wiki.papers_to_concepts("vlms", concepts)
    assert tags.get(1) == [{"name": "Streaming Inference", "slug": "streaming-inference"}]
    assert 2 not in tags                       # matched nothing → absent, no fake tag


def test_papers_to_concepts_unions_llm_membership_and_synonyms(tmp_path, monkeypatch):
    """LLM-assigned concept.papers membership is honored even when the paper text
    matches no synonym — listed first, then synonym matches fill in."""
    db = _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    con = connect(db)
    con.execute("UPDATE papers SET title='Totally generic title', abstract='no synonyms here' WHERE id=3")
    con.commit(); con.close()
    concepts = [
        {"name": "KV Distillation", "slug": "kv-distillation",
         "synonyms": ["kv distillation"], "papers": ["2401.00003"]},  # LLM assigns paper 3
    ]
    tags = wiki.papers_to_concepts("vlms", concepts)
    # Paper 3 has no synonym match but is LLM-assigned → still tagged.
    assert tags.get(3) == [{"name": "KV Distillation", "slug": "kv-distillation"}]


def test_load_overview_papers_carry_concept_tags(tmp_path, monkeypatch):
    """load_overview attaches concept tags to each paper in the evidence row."""
    db = _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    con = connect(db)
    con.execute("UPDATE papers SET abstract='reasoning preservation matters here' WHERE id=1")
    con.commit(); con.close()
    wiki.generate_overview("vlms")    # writes concepts.json (has 'Reasoning Preservation')
    loaded = wiki.load_overview("vlms")
    p1 = next(p for p in loaded["papers"] if p["id"] == 1)
    assert any(t["slug"] == "reasoning-preservation" for t in p1["tags"])
    # every paper dict has a tags key (possibly empty)
    assert all("tags" in p for p in loaded["papers"])


def test_build_collection_graph_has_concept_and_method_nodes(tmp_path, monkeypatch):
    """After a draft, the knowledge graph carries concept + method nodes wired to
    their papers (problems have no membership in this fixture → no problem nodes)."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wiki.generate_overview("vlms")
    g = wiki.build_collection_graph("vlms")
    kinds = {n["kind"] for n in g["nodes"].values()}
    assert "paper" in kinds and "concept" in kinds and "method" in kinds
    # The Semantic-anchor method (papers=[2401.00001]) is wired to paper id 1.
    method_nodes = [nid for nid, n in g["nodes"].items() if n["kind"] == "method"]
    assert any("paper:1" in g["adj"].get(m, {}) for m in method_nodes)


def test_connection_view_gates_and_formats(tmp_path, monkeypatch):
    """connection_view returns render-ready themes/orphans/co-occurrences, or
    None when the graph is too sparse to say anything."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wiki.generate_overview("vlms")
    cv = wiki.connection_view("vlms")
    # The fixture wires concepts + methods to shared papers, so SOMETHING surfaces.
    assert cv is not None
    assert set(cv) == {"themes", "orphans", "co_occurrences", "graph"}
    # Orphans carry a resolvable paper id + label for linking.
    assert all("id" in o and "label" in o for o in cv["orphans"])
    # The Cytoscape payload has nodes (papers + entities) and undirected edges.
    assert cv["graph"]["nodes"]
    kinds = {n["kind"] for n in cv["graph"]["nodes"]}
    assert "paper" in kinds and "concept" in kinds
    # paper nodes carry a paper_id for click-to-open; entity nodes don't.
    assert all((n["paper_id"] is not None) == (n["kind"] == "paper") for n in cv["graph"]["nodes"])


def test_load_overview_returns_field_model_shape(tmp_path, monkeypatch):
    """load_overview reads wiki/sections/* and returns {thesis, landscape,
    papers, meta}. Thesis callouts round-trip through markdown; landscape lists
    do too; papers come from the live DB (3 in the test fixture)."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wiki.generate_overview("vlms")
    loaded = wiki.load_overview("vlms")
    assert loaded is not None
    assert loaded["needs_migration"] is False
    # Thesis fields round-trip from the markdown file.
    assert loaded["thesis"]["one_paragraph"].startswith("The collection circles")
    assert loaded["thesis"]["core_tension"].startswith("Reduce memory")
    assert loaded["thesis"]["key_intuition"].startswith("Important reasoning")
    assert loaded["thesis"]["central_question"].startswith("Can we keep")
    # Landscape round-trips. Problems/methods are paper-anchored nodes
    # ({text, papers}); debates/open_questions stay plain strings.
    prob_texts = [p["text"] for p in loaded["landscape"]["problems"]]
    assert "KV cache memory explosion" in prob_texts
    method_texts = [m["text"] for m in loaded["landscape"]["methods"]]
    assert "Semantic-anchor approaches" in method_texts
    assert all(isinstance(d, str) for d in loaded["landscape"]["debates"])
    # Paper membership survives generate→landscape.json→load (NOPE filtered).
    sem = next(m for m in loaded["landscape"]["methods"] if m["text"] == "Semantic-anchor approaches")
    assert sem["papers"] == ["2401.00001"]
    # Papers come from the DB (3 fixture papers).
    assert {p["id"] for p in loaded["papers"]} == {1, 2, 3}
    # Each paper carries the attention decoration shape (all zero in this test).
    for p in loaded["papers"]:
        assert "attention_score" in p and "is_hot" in p and "is_new" in p


def test_load_overview_returns_migration_banner_for_legacy_starter(tmp_path, monkeypatch):
    """A legacy wiki/starter/index.md on disk with no new wiki/sections/ tree
    returns {needs_migration: True} so the panel can show a regenerate prompt."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wdir = tmp_path / "collections" / "vlms" / "wiki"
    (wdir / "starter").mkdir(parents=True, exist_ok=True)
    (wdir / "starter" / "index.md").write_text("legacy", encoding="utf-8")
    loaded = wiki.load_overview("vlms")
    assert loaded is not None
    assert loaded["needs_migration"] is True
    assert loaded["papers"] == []


def test_load_overview_returns_migration_banner_for_legacy_overview_json(tmp_path, monkeypatch):
    """A very-old wiki/overview.json (pre-llm_wiki shape) also triggers the
    migration banner — same branch."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wdir = tmp_path / "collections" / "vlms" / "wiki"
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "overview.json").write_text("{}", encoding="utf-8")
    loaded = wiki.load_overview("vlms")
    assert loaded is not None and loaded["needs_migration"] is True


def test_load_overview_returns_none_when_no_wiki(tmp_path, monkeypatch):
    """No new sections AND no legacy tree → None (template shows the
    'No wiki yet' card with the Draft button)."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    assert wiki.load_overview("vlms") is None


# --- Phase B: concepts + Focus + Recommended Reading ------------------------

def test_validate_field_model_keeps_valid_concepts():
    """Validator keeps non-duplicate, named concepts (drops 'ab' and the dup
    slug); concept paper-membership is filtered to real refs."""
    out = wiki._validate_field_model(_FIELD_MODEL_JSON,
                                      valid_refs={"2401.00001", "2401.00002", "2401.00003"})
    slugs = [c["slug"] for c in out["concepts"]]
    assert "reasoning-preservation" in slugs
    assert "semantic-anchors" in slugs
    assert "kv-distillation" in slugs
    # 'ab' was too short; duplicate 'Reasoning Preservation' was dedup'd.
    assert "ab" not in slugs
    assert slugs.count("reasoning-preservation") == 1
    # Each concept's synonyms include the canonical (lowercased) name.
    rp = next(c for c in out["concepts"] if c["slug"] == "reasoning-preservation")
    assert "reasoning preservation" in rp["synonyms"]
    # Concept membership filtered to valid refs (NOPE dropped).
    assert rp["papers"] == ["2401.00002"]
    # The old reading-order 'recommended' is gone from the validator output.
    assert "recommended" not in out


def test_generate_overview_writes_concepts_and_recommended_files(tmp_path, monkeypatch):
    """generate_overview writes wiki/sections/concepts.json (no more
    recommended.json — the reading-order feature was removed)."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    assert wiki.generate_overview("vlms") is True
    sdir = tmp_path / "collections" / "vlms" / "wiki" / "sections"
    assert (sdir / "concepts.json").is_file()
    assert not (sdir / "recommended.json").exists()
    cdata = json.loads((sdir / "concepts.json").read_text())
    assert cdata["_meta"]["generated_by"] == "agent"
    assert {c["slug"] for c in cdata["concepts"]} >= {"reasoning-preservation",
                                                       "semantic-anchors", "kv-distillation"}


def test_attention_per_concept_scores_highlights_and_notes(tmp_path, monkeypatch):
    """The concept scorer counts highlights matching any synonym (×1) and
    notes matching any synonym (×_ATTENTION_NOTE_WEIGHT). Concepts with no
    matches are absent from the result (no fake zeros)."""
    db = _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    concepts = [
        {"name": "Reasoning Preservation", "slug": "reasoning-preservation",
         "synonyms": ["reasoning preservation", "preserving reasoning"]},
        {"name": "KV Distillation", "slug": "kv-distillation",
         "synonyms": ["kv distillation", "distill kv cache"]},
        {"name": "Quantum Mechanics", "slug": "quantum-mechanics",
         "synonyms": ["quantum mechanics"]},  # nobody mentions this
    ]
    con = connect(db)
    # 3 highlights mentioning "reasoning preservation" or its synonyms
    for txt in ("We focus on reasoning preservation in long-context.",
                 "Preserving reasoning across compression is the goal.",
                 "Reasoning preservation matters for tool use."):
        con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, "
                    "position_json, selected_text) VALUES (1, 'vlms', 'highlight', 1, '{}', ?)",
                    (txt,))
    # 1 note mentioning "KV distillation"
    con.execute("INSERT INTO paper_notes(paper_id, collection_slug, thoughts, status) "
                "VALUES (2, 'vlms', 'I want to revisit KV distillation later.', 'noted')")
    con.commit(); con.close()
    scores = wiki.attention_per_concept("vlms", concepts)
    assert scores.get("reasoning-preservation") == 3
    assert scores.get("kv-distillation") == wiki._ATTENTION_NOTE_WEIGHT   # 5
    assert "quantum-mechanics" not in scores                              # no signal


def test_load_overview_focus_renders_above_threshold_only(tmp_path, monkeypatch):
    """The Focus section renders only when at least one concept has a score
    >= _FOCUS_CONCEPT_FLOOR. Below threshold → focus is None (the template
    shows Section 1 full-width, no sidebar)."""
    db = _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wiki.generate_overview("vlms")
    # No attention yet → focus is None.
    loaded = wiki.load_overview("vlms")
    assert loaded["focus"] is None
    # Add just below threshold (_FOCUS_CONCEPT_FLOOR-1 highlights for one concept) → still None.
    con = connect(db)
    for _ in range(wiki._FOCUS_CONCEPT_FLOOR - 1):
        con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, "
                    "position_json, selected_text) VALUES (1, 'vlms', 'highlight', 1, '{}', "
                    "'reasoning preservation matters')")
    con.commit(); con.close()
    assert wiki.load_overview("vlms")["focus"] is None
    # One more highlight → crosses threshold → focus renders.
    con = connect(db)
    con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, "
                "position_json, selected_text) VALUES (1, 'vlms', 'highlight', 1, '{}', "
                "'reasoning preservation again')")
    con.commit(); con.close()
    loaded = wiki.load_overview("vlms")
    assert loaded["focus"] is not None
    assert loaded["focus"]["concepts"][0]["slug"] == "reasoning-preservation"
    assert loaded["focus"]["highlights"] == wiki._FOCUS_CONCEPT_FLOOR


def test_suggest_papers_to_add_enqueues_arxiv_candidates(tmp_path, monkeypatch):
    """suggest_papers_to_add seeds discovery from the field model, enqueues new
    arXiv candidates into triage, and dedupes against papers already present /
    already pending. Surfaced in load_overview as add_candidates."""
    import app.discover as discover, app.triage as triage
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    monkeypatch.setattr(triage, "connect", lambda: connect(tmp_path / "app.sqlite"))
    wiki.generate_overview("vlms")
    # Stub the network discovery: two candidates, one a dupe title of an existing paper.
    monkeypatch.setattr(discover, "find_related_papers",
                        lambda seed, exclude_titles=None: [
                            {"arxiv_id": "2501.11111", "title": "Brand New Paper",
                             "summary": "x", "note": "fills the eval gap"},
                            {"arxiv_id": "2501.22222", "title": "Another New One",
                             "summary": "y", "note": "extends method Z"},
                        ])
    res = wiki.suggest_papers_to_add("vlms")
    assert res["added"] == 2 and res["error"] is None
    loaded = wiki.load_overview("vlms")
    titles = {c["title"] for c in loaded["add_candidates"]}
    assert {"Brand New Paper", "Another New One"} <= titles
    # A second run with the same candidates adds nothing (already pending).
    assert wiki.suggest_papers_to_add("vlms")["added"] == 0


def test_suggest_papers_to_add_needs_a_field_model(tmp_path, monkeypatch):
    """No thesis/concepts → no seed → refuse with a clear error, no network."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())   # no generate_overview
    res = wiki.suggest_papers_to_add("vlms")
    assert res["added"] == 0 and res["error"]


# --- Phase C: beliefs (candidates tray + accepted) --------------------------

_BELIEF_DRAFT_JSON = {
    "candidates": [
        {"title": "Reasoning information is concentrated in a small subset of KV states.",
         "confidence": "emerging",
         "supporting_papers": ["2401.00001", "2401.00002"],
         "related_concepts": ["reasoning-preservation", "kv-distillation"]},
        {"title": "Static importance scores are insufficient for hard reasoning.",
         "confidence": "uncertain",
         "supporting_papers": ["2401.00002"],
         "related_concepts": ["reasoning-preservation"]},
        # Hallucinated paper ref — validator drops the whole candidate.
        {"title": "This belief cites only invalid papers.",
         "confidence": "emerging",
         "supporting_papers": ["NOPE"],
         "related_concepts": []},
        # Too-short title — validator drops it.
        {"title": "Short.",
         "confidence": "medium",
         "supporting_papers": ["2401.00001"],
         "related_concepts": []},
    ],
}


def _two_step_stub(field=None, belief=None):
    """LLM stub that returns different payloads based on the system prompt
    fingerprint. The field-model skill includes "Output JSON: {thesis"; the
    belief-draft skill includes "CONCEPTS in this collection" in the user
    prompt. Either argument is forwarded if non-None; otherwise default JSON."""
    fj = field if field is not None else _FIELD_MODEL_JSON
    bj = belief if belief is not None else _BELIEF_DRAFT_JSON
    def stub(messages, model=None):
        user = messages[-1]["content"]
        if "CONCEPTS in this collection" in user:
            if bj is None:
                raise RuntimeError("simulated belief LLM failure")
            return json.dumps(bj)
        if fj is None:
            raise RuntimeError("simulated field LLM failure")
        return json.dumps(fj)
    return stub


def _seed_with_signal(tmp_path, monkeypatch, stub=None, highlights=6):
    """Fixture: 3 papers + draft Field Model + enough highlights to cross the
    Suggest-beliefs floor. Returns the DB path."""
    db = _seed_three_papers(tmp_path, monkeypatch, stub or _two_step_stub())
    wiki.generate_overview("vlms")
    con = connect(db)
    # Plant `highlights` highlights all matching the "reasoning preservation"
    # synonym so the concept crosses _BELIEF_SUGGEST_FLOOR=5 by default.
    for _ in range(highlights):
        con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, "
                    "position_json, selected_text) VALUES (1, 'vlms', 'highlight', 1, '{}', "
                    "'we focus on reasoning preservation in long-context')")
    con.commit(); con.close()
    return db


def test_validate_belief_candidates_drops_invalid_and_short():
    """Beliefs without a valid supporting paper are dropped; short titles
    dropped; duplicate slugs deduped; cap at _BELIEF_CANDIDATES_MAX."""
    valid = {"2401.00001", "2401.00002", "2401.00003"}
    concepts = {"reasoning-preservation", "kv-distillation"}
    out = wiki._validate_belief_candidates(_BELIEF_DRAFT_JSON, valid, concepts)
    titles = [c["title"] for c in out]
    assert any("Reasoning information is concentrated" in t for t in titles)
    assert any("Static importance" in t for t in titles)
    # Hallucinated-only-refs belief dropped.
    assert not any("invalid papers" in t for t in titles)
    # Too-short title dropped.
    assert not any(t == "Short." for t in titles)
    # All survivors cite at least one valid ref.
    for c in out:
        assert c["supporting_papers"]
        assert all(p in valid for p in c["supporting_papers"])
    # Related concepts filtered to known slugs.
    for c in out:
        assert all(s in concepts for s in c["related_concepts"])


def test_can_suggest_beliefs_respects_signal_floor(tmp_path, monkeypatch):
    """Below the floor, can_suggest_beliefs is False (button hidden, premature
    inference avoided). Above floor → True."""
    db = _seed_three_papers(tmp_path, monkeypatch, _two_step_stub())
    wiki.generate_overview("vlms")
    assert wiki.can_suggest_beliefs("vlms") is False    # no signal yet
    con = connect(db)
    # 4 < _BELIEF_SUGGEST_FLOOR=5 → still False
    for _ in range(wiki._BELIEF_SUGGEST_FLOOR - 1):
        con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, "
                    "position_json, selected_text) VALUES (1, 'vlms', 'highlight', 1, '{}', "
                    "'reasoning preservation matters')")
    con.commit(); con.close()
    assert wiki.can_suggest_beliefs("vlms") is False
    # One more highlight → crosses floor.
    con = connect(db)
    con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, "
                "position_json, selected_text) VALUES (1, 'vlms', 'highlight', 1, '{}', "
                "'reasoning preservation again')")
    con.commit(); con.close()
    assert wiki.can_suggest_beliefs("vlms") is True


def test_can_suggest_beliefs_unblocks_on_any_note(tmp_path, monkeypatch):
    """OR-branch of the signal floor: any non-empty note unblocks the
    Suggest button even without concept matches."""
    db = _seed_three_papers(tmp_path, monkeypatch, _two_step_stub())
    wiki.generate_overview("vlms")
    assert wiki.can_suggest_beliefs("vlms") is False
    con = connect(db)
    con.execute("INSERT INTO paper_notes(paper_id, collection_slug, thoughts, status) "
                "VALUES (1, 'vlms', 'a real thought', 'noted')")
    con.commit(); con.close()
    assert wiki.can_suggest_beliefs("vlms") is True


def test_suggest_beliefs_writes_candidates_to_tray(tmp_path, monkeypatch):
    """The full pipeline: signal crosses floor → suggest_beliefs runs the LLM
    → validates → writes each surviving candidate as its own .md file under
    wiki/sections/beliefs/_candidates/."""
    _seed_with_signal(tmp_path, monkeypatch)
    result = wiki.suggest_beliefs("vlms")
    assert result["generated"] >= 2
    cdir = tmp_path / "collections" / "vlms" / "wiki" / "sections" / "beliefs" / "_candidates"
    files = sorted(cdir.glob("*.md"))
    assert len(files) >= 2
    # Each candidate file has full frontmatter.
    meta, _ = wiki.frontmatter.parse(files[0].read_text())
    assert meta["type"] == "belief"
    assert meta["status"] == "candidate"
    assert meta["generated_by"] == "agent"
    assert meta["generator"] == "belief-draft"
    assert isinstance(meta["supporting_papers"], list) and meta["supporting_papers"]
    assert meta["confidence"] in wiki._BELIEF_CONFIDENCE_VALUES


def test_suggest_beliefs_refuses_below_signal_floor(tmp_path, monkeypatch):
    """Calling suggest_beliefs below the signal floor returns an error and
    writes nothing — same honesty rule as the button-hiding logic."""
    _seed_three_papers(tmp_path, monkeypatch, _two_step_stub())
    wiki.generate_overview("vlms")
    result = wiki.suggest_beliefs("vlms")  # no signal yet
    assert "error" in result and result["generated"] == 0
    cdir = tmp_path / "collections" / "vlms" / "wiki" / "sections" / "beliefs" / "_candidates"
    assert not cdir.exists() or not list(cdir.glob("*.md"))


def test_suggest_beliefs_skips_duplicates(tmp_path, monkeypatch):
    """A second suggest run that returns the same title slugs doesn't write
    duplicate candidate files — the existing-titles check filters them out."""
    _seed_with_signal(tmp_path, monkeypatch)
    first = wiki.suggest_beliefs("vlms")
    assert first["generated"] >= 2
    second = wiki.suggest_beliefs("vlms")     # same stub returns same payload
    assert second["generated"] == 0
    assert second["dropped_dupes"] >= 2


def test_accept_belief_moves_candidate_to_accepted(tmp_path, monkeypatch):
    """accept_belief promotes the candidate file from _candidates/<id>.md to
    beliefs/<title-slug>.md, bumps status='accepted' + accepted_at."""
    _seed_with_signal(tmp_path, monkeypatch)
    wiki.suggest_beliefs("vlms")
    candidates = wiki.list_belief_candidates("vlms")
    cid = candidates[0]["id"]
    assert wiki.accept_belief("vlms", cid) is True
    # Candidate file gone.
    cdir = tmp_path / "collections" / "vlms" / "wiki" / "sections" / "beliefs" / "_candidates"
    assert not (cdir / f"{cid}.md").exists()
    # Accepted file exists in beliefs/ root.
    accepted = wiki.list_accepted_beliefs("vlms")
    assert len(accepted) == 1
    assert accepted[0]["status"] == "accepted"
    assert accepted[0]["accepted_at"]
    # The other candidate is still pending.
    remaining = wiki.list_belief_candidates("vlms")
    assert len(remaining) == len(candidates) - 1


def test_dismiss_belief_deletes_candidate(tmp_path, monkeypatch):
    """dismiss_belief deletes the candidate file without writing anything."""
    _seed_with_signal(tmp_path, monkeypatch)
    wiki.suggest_beliefs("vlms")
    candidates = wiki.list_belief_candidates("vlms")
    cid = candidates[0]["id"]
    assert wiki.dismiss_belief("vlms", cid) is True
    remaining = wiki.list_belief_candidates("vlms")
    assert cid not in {c["id"] for c in remaining}
    # Nothing in accepted.
    assert wiki.list_accepted_beliefs("vlms") == []


def test_load_overview_returns_beliefs_with_resolved_papers(tmp_path, monkeypatch):
    """load_overview decorates candidates and accepted beliefs with resolved
    paper objects and concept names; can_suggest_beliefs reflects signal."""
    _seed_with_signal(tmp_path, monkeypatch)
    wiki.suggest_beliefs("vlms")
    loaded = wiki.load_overview("vlms")
    assert loaded["can_suggest_beliefs"] is True
    assert loaded["belief_candidates"]
    cand = loaded["belief_candidates"][0]
    assert cand["papers"] and cand["papers"][0]["id"] in (1, 2, 3)
    assert cand["related"]      # at least one concept tag resolved
    # Accept one and verify it moves into the beliefs list.
    wiki.accept_belief("vlms", cand["id"])
    loaded2 = wiki.load_overview("vlms")
    assert len(loaded2["beliefs"]) == 1
    assert loaded2["beliefs"][0]["title"] == cand["title"]


def _seed_three_papers_with_starter(tmp_path, monkeypatch):
    """Shared fixture for the attention tests: 3 papers + a generated Field
    Model, so paper-card re-ranking / hot / new badges have something to chew on."""
    db = _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wiki.generate_overview("vlms")
    return db


def test_attention_scores_counts_highlights_and_notes(tmp_path, monkeypatch):
    db = _seed_three_papers_with_starter(tmp_path, monkeypatch)
    con = connect(db)
    # P1: 3 highlights -> 3
    for _ in range(3):
        con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, position_json) "
                    "VALUES (1, 'vlms', 'highlight', 1, '{}')")
    # P2: 1 highlight + a note with thoughts -> 1 + _ATTENTION_NOTE_WEIGHT (5) = 6
    con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, position_json) "
                "VALUES (2, 'vlms', 'highlight', 1, '{}')")
    con.execute("INSERT INTO paper_notes(paper_id, collection_slug, thoughts, status) "
                "VALUES (2, 'vlms', 'my take', 'noted')")
    # P3: nothing -> 0 (absent from the map)
    con.commit(); con.close()
    scores = wiki.attention_scores("vlms")
    assert scores == {1: 3, 2: 6}     # P3 absent: no signal, no fake zero
    # An empty note (no fields populated) shouldn't count.
    con = connect(db); con.execute("DELETE FROM paper_notes WHERE paper_id=2")
    con.execute("INSERT INTO paper_notes(paper_id, collection_slug, status) VALUES (2, 'vlms', 'unread')")
    con.commit(); con.close()
    assert wiki.attention_scores("vlms") == {1: 3, 2: 1}    # back to 1 (just the highlight)


def test_papers_section_reranked_by_attention(tmp_path, monkeypatch):
    """Phase A: the Papers (Evidence) row in the Field Model floats attended
    papers to the front. With zero attention, library's DB order (title-sorted
    in our fixture: P1, P2, P3) is preserved by the stable sort."""
    db = _seed_three_papers_with_starter(tmp_path, monkeypatch)
    con = connect(db)
    # P3 gets the biggest signal -> should float to the front.
    for _ in range(10):
        con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, position_json) "
                    "VALUES (3, 'vlms', 'highlight', 1, '{}')")
    con.execute("INSERT INTO paper_notes(paper_id, collection_slug, thoughts, status) "
                "VALUES (1, 'vlms', 't', 'noted')")    # P1 gets 5
    con.commit(); con.close()
    loaded = wiki.load_overview("vlms")
    order = [p["id"] for p in loaded["papers"]]
    assert order == [3, 1, 2]                     # by score: 10, 5, 0
    # With no signal at all, DB order holds (the stable-sort property).
    con = connect(db); con.execute("DELETE FROM annotations"); con.execute("DELETE FROM paper_notes")
    con.commit(); con.close()
    loaded2 = wiki.load_overview("vlms")
    assert [p["id"] for p in loaded2["papers"]] == [1, 2, 3]


def test_is_hot_and_is_new_badges(tmp_path, monkeypatch):
    """Hot/new badges apply to the Papers (Evidence) row in Phase A."""
    import time
    db = _seed_three_papers_with_starter(tmp_path, monkeypatch)
    baseline = wiki.read_and_bump_viewed("vlms")
    assert baseline is None
    con = connect(db)
    since = con.execute("SELECT last_wiki_viewed_at FROM collections WHERE slug='vlms'").fetchone()[0]
    con.close()
    time.sleep(1.1)
    con = connect(db)
    for _ in range(4):
        con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, position_json) "
                    "VALUES (2, 'vlms', 'highlight', 1, '{}')")
    con.commit(); con.close()
    loaded = wiki.load_overview("vlms", attention_since=since)
    by_id = {p["id"]: p for p in loaded["papers"]}
    assert by_id[2]["is_new"] is True
    assert by_id[1]["is_new"] is False and by_id[3]["is_new"] is False
    assert by_id[2]["is_hot"] is True
    loaded2 = wiki.load_overview("vlms")
    assert all(p["is_new"] is False for p in loaded2["papers"])


def test_stage_callback_fires_through_pipeline(tmp_path, monkeypatch):
    """Phase A one-shot pipeline: gathering → reading_pdfs (with count) →
    drafting → writing (pages_done/pages_total = 2 files)."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    stages: list = []
    assert wiki.generate_overview("vlms", force=True,
                                   stage_cb=lambda n, **kw: stages.append((n, kw))) is True
    names = [s[0] for s in stages]
    assert names[0] == "gathering"
    for expected in ("reading_pdfs", "drafting", "writing"):
        assert expected in names, f"stage {expected!r} missing from {names}"
    rp = next(s for s in stages if s[0] == "reading_pdfs")
    assert "pdfs_done" in rp[1] and "pdfs_total" in rp[1]
    writes = [s for s in stages if s[0] == "writing"]
    assert len(writes) >= 2
    assert all("pages_done" in s[1] and "pages_total" in s[1] for s in writes)


def test_stage_callback_errors_dont_abort_generation(tmp_path, monkeypatch):
    """A broken UI callback can't take down a draft."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    def bad(name, **kw):
        raise RuntimeError("UI exploded")
    assert wiki.generate_overview("vlms", force=True, stage_cb=bad) is True
    assert (tmp_path / "collections" / "vlms" / "wiki" / "sections" / "thesis.md").is_file()


def test_stage_message_single_action_line():
    """_stage_message returns a single human-voice action line per stage."""
    assert wiki._stage_message({"stage": "gathering"})["action"].startswith("I'm collecting")
    rp = wiki._stage_message({"stage": "reading_pdfs", "pdfs_done": 3, "pdfs_total": 12})
    assert rp["action"] == "I'm reading the PDFs (3/12)."
    assert rp["subline"] == ""
    wr = wiki._stage_message({"stage": "writing", "pages_done": 1, "pages_total": 2})
    assert wr["action"] == "I'm writing the page (1/2)."
    assert wiki._stage_message({"stage": "wat"})["action"].startswith("I'm collecting")


def test_stage_progress_monotone_and_capped():
    """_stage_progress climbs through the pipeline's real stages and stays <100
    until status='done'/'failed' lands on 100."""
    p_gather   = wiki._stage_progress({"stage": "gathering"})
    p_pdf_0    = wiki._stage_progress({"stage": "reading_pdfs", "pdfs_done": 0, "pdfs_total": 10})
    p_pdf_10   = wiki._stage_progress({"stage": "reading_pdfs", "pdfs_done": 10, "pdfs_total": 10})
    p_write_0  = wiki._stage_progress({"stage": "writing", "pages_done": 0, "pages_total": 2})
    p_write_2  = wiki._stage_progress({"stage": "writing", "pages_done": 2, "pages_total": 2})
    assert p_gather < p_pdf_0 < p_pdf_10 <= p_write_0 < p_write_2 < 100
    assert wiki._stage_progress({"stage": "writing", "status": "done"}) == 100
    assert wiki._stage_progress({"stage": "drafting", "status": "failed"}) == 100


def test_start_draft_async_publishes_state_and_completes(tmp_path, monkeypatch):
    """The async runner: kicks off in a thread, transitions to 'done' on success,
    writes the wiki/sections/ tree."""
    import time
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wiki.clear_draft_job("vlms")
    assert wiki.start_draft_async("vlms", force=True) is True
    assert wiki.start_draft_async("vlms", force=True) is False
    deadline = time.time() + 5
    while time.time() < deadline:
        job = wiki.get_draft_job("vlms")
        if job and job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    job = wiki.get_draft_job("vlms")
    assert job is not None and job["status"] == "done"
    assert (tmp_path / "collections" / "vlms" / "wiki" / "sections" / "thesis.md").is_file()
    wiki.clear_draft_job("vlms")
    assert wiki.get_draft_job("vlms") is None


def test_start_draft_async_publishes_failure(tmp_path, monkeypatch):
    """If generate_overview returns False (e.g. the LLM returns an empty
    field-model JSON), the job ends as status='failed' with a human-readable
    error."""
    import time
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub(field_json={}))
    wiki.clear_draft_job("vlms")
    wiki.start_draft_async("vlms", force=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        job = wiki.get_draft_job("vlms")
        if job and job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    job = wiki.get_draft_job("vlms")
    assert job is not None and job["status"] == "failed"
    assert job.get("error")
    wiki.clear_draft_job("vlms")


def test_generate_overview_no_abstracts_returns_false(tmp_path, monkeypatch):
    """Without abstracts on any paper, the digest is empty → pipeline refuses."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    con = connect(tmp_path / "app.sqlite")
    con.execute("UPDATE papers SET abstract=''"); con.commit(); con.close()
    assert wiki.generate_overview("vlms", force=True) is False
    assert not (tmp_path / "collections" / "vlms" / "wiki" / "sections").exists()
