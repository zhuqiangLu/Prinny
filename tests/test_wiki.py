"""Phase 5 diff-proposal pipeline with a mocked LLM.

The key property under test: the provenance guardrail is enforced in code —
claims that don't cite a real note/thought are dropped before the user ever
sees the diff, and nothing is written to wiki/ until the user accepts.
"""

from __future__ import annotations

import json

import app.thoughts as thoughts_mod
import app.wiki as wiki
from app.db import connect, init_db


def _fake_llm(messages, model=None):
    """Return analyze JSON or generate JSON depending on the prompt."""
    prompt = messages[-1]["content"]
    if "ALLOWED NOTE KEYS" in prompt:
        return json.dumps(
            {
                "pages": [
                    {
                        "section": "problems",
                        "slug": "efficiency",
                        "title": "Efficiency",
                        "claims": [
                            # attributed: cites its source paper -> ACCEPT
                            {"text": "Grounded claim.", "claim_type": "attributed",
                             "notes": ["1"], "papers": ["1"], "thoughts": []},
                            # cites nothing valid -> REJECT
                            {"text": "Hallucinated claim.", "claim_type": "attributed",
                             "notes": ["GHOST"], "papers": ["GHOST"], "thoughts": []},
                        ],
                    }
                ]
            }
        )
    return json.dumps({"sections": ["problems"], "summary": "x"})


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    con = connect(db)
    con.execute("INSERT INTO papers (id, title, origin) VALUES (1, 'P1', 'zotero-import')")
    con.execute(
        "INSERT INTO paper_notes (paper_id, collection_slug, summary, status) "
        "VALUES (1, 'vlms', 'A note that grounds claims.', 'noted')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(wiki, "connect", lambda: connect(db))
    monkeypatch.setattr(wiki, "COLLECTIONS_DIR", tmp_path / "collections")
    monkeypatch.setattr(thoughts_mod, "COLLECTIONS_DIR", tmp_path / "collections")
    monkeypatch.setattr("app.llm.complete", _fake_llm)


def test_gate_drops_unsupported_and_note_only_attributed(monkeypatch):
    # all refs resolve to (seed, human) unless a test overrides — isolates gate logic.
    monkeypatch.setattr("app.provenance.effective_stamp", lambda ref, slug=None: ("seed", "human"))
    ctx = {"slug": "x", "valid_notes": {"K1"}, "valid_thoughts": set(),
           "valid_papers": {"P1"}, "valid_highlights": set(), "hl_to_paper": {}}
    # attributed citing a paper -> ACCEPT
    assert wiki.gate({"text": "ok", "papers": ["P1"]}, ctx)[0] == wiki.ACCEPT
    # no valid provenance -> REJECT
    assert wiki.gate({"text": "nothing", "notes": ["NOPE"]}, ctx)[0] == wiki.REJECT
    # attributed but cites only a note (not the source) -> REJECT
    assert wiki.gate({"text": "note only", "notes": ["K1"]}, ctx)[0] == wiki.REJECT


def test_run_generation_filters_and_proposes(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    proposals = wiki.run_generation("vlms", "full")

    assert len(proposals) == 1
    p = proposals[0]
    assert p["page_path"] == "problems/efficiency.md"
    # the hallucinated claim (GHOST) was filtered out by the guardrail
    assert "Grounded claim." in p["new_content"]
    assert "Hallucinated claim." not in p["new_content"]
    assert len(p["claims"]) == 1

    # proposal is on disk but NOT applied to the wiki
    assert wiki.list_proposed("vlms")
    assert not (tmp_path / "collections" / "vlms" / "wiki" / "problems" / "efficiency.md").exists()


def test_accept_writes_wiki_index_and_log(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    p = wiki.run_generation("vlms", "full")[0]

    assert wiki.accept_proposed("vlms", p["id"]) is True
    wdir = tmp_path / "collections" / "vlms" / "wiki"
    assert (wdir / "problems" / "efficiency.md").exists()
    assert "Grounded claim." in (wdir / "problems" / "efficiency.md").read_text()
    assert (wdir / "index.md").exists()
    assert (wdir / "log.md").exists()
    assert wiki.list_proposed("vlms") == []  # removed from queue


def test_merge_fallback_never_clobbers(tmp_path):
    old_body = "# Efficiency\n\n- The user's own carefully written point."
    claims = [
        {"text": "The user's own carefully written point.", "notes": ["K1"], "thoughts": []},  # dup
        {"text": "A brand new grounded point.", "notes": ["K2"], "thoughts": []},
    ]
    merged = wiki._merge_fallback_body(old_body, claims)
    assert "The user's own carefully written point." in merged  # preserved
    assert merged.count("carefully written point.") == 1         # not duplicated
    assert "A brand new grounded point." in merged               # new appended


def test_merge_into_unions_provenance(tmp_path):
    old = wiki.frontmatter.dump(
        {"type": "problems", "title": "Efficiency", "sources": ["OLD"],
         "derived_from_notes": ["N0"], "derived_from_thoughts": []},
        "# Efficiency\n\n- Existing line.",
    )
    page = {"section": "problems", "title": "Efficiency"}
    claims = [{"text": "New line.", "notes": ["N1"], "thoughts": ["T1"], "papers": ["P1"]}]
    merged = wiki._merge_into(old, page, claims, use_llm=False)
    meta, body = wiki.frontmatter.parse(merged)
    assert "Existing line." in body                 # user content kept
    assert set(meta["derived_from_notes"]) == {"N0", "N1"}
    assert set(meta["sources"]) == {"OLD", "P1"}


def test_regenerate_merges_existing_page(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # force the deterministic (no-LLM) merge so the assertion is exact
    monkeypatch.setattr(wiki, "_merge_body_llm", lambda *a, **k: None)
    page_dir = tmp_path / "collections" / "vlms" / "wiki" / "problems"
    page_dir.mkdir(parents=True)
    (page_dir / "efficiency.md").write_text(
        wiki.frontmatter.dump(
            {"type": "problems", "title": "Efficiency", "sources": [],
             "derived_from_notes": [], "derived_from_thoughts": []},
            "# Efficiency\n\n- My hand-written analysis I do not want lost.",
        ),
        encoding="utf-8",
    )
    p = wiki.run_generation("vlms", "incremental")[0]
    assert "My hand-written analysis I do not want lost." in p["new_content"]  # preserved
    assert "Grounded claim." in p["new_content"]                              # new merged in


def test_lint_detects_broken_orphan_outlink(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki, "COLLECTIONS_DIR", tmp_path / "collections")
    wdir = tmp_path / "collections" / "vlms" / "wiki" / "problems"
    wdir.mkdir(parents=True)
    (wdir / "alpha.md").write_text(
        wiki.frontmatter.dump({"type": "problems", "title": "Alpha"},
                              "# Alpha\n\nSee [[Beta]] and [[Ghost]]."),
        encoding="utf-8",
    )
    (wdir / "beta.md").write_text(
        wiki.frontmatter.dump({"type": "problems", "title": "Beta"}, "# Beta\n\nNo links."),
        encoding="utf-8",
    )
    issues = wiki.lint_wiki("vlms")
    types = {i["type"] for i in issues}
    assert "broken-link" in types   # [[Ghost]]
    assert "no-outlink" in types     # Beta
    assert "orphan" in types         # Alpha (nothing links to it)


def test_lint_detects_index_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki, "COLLECTIONS_DIR", tmp_path / "collections")
    wdir = tmp_path / "collections" / "vlms" / "wiki"
    (wdir / "problems").mkdir(parents=True)
    for stem, title in (("alpha", "Alpha"), ("beta", "Beta")):
        (wdir / "problems" / f"{stem}.md").write_text(
            wiki.frontmatter.dump({"type": "problems", "title": title}, f"# {title}\n\n[[x]]"),
            encoding="utf-8")
    # index lists alpha + a stale ghost, but NOT beta
    (wdir / "index.md").write_text(
        "# Wiki Index\n\n## Problems\n- [[Alpha]] (`problems/alpha`)\n- [[Ghost]] (`problems/ghost`)\n",
        encoding="utf-8")
    issues = {(i["type"], i["pages"][0]) for i in wiki.lint_wiki("vlms")}
    assert ("index-missing", "problems/beta") in issues     # exists, not in index
    assert ("index-stale", "problems/ghost") in issues       # in index, doesn't exist


def test_select_notes_respects_budget_and_relevance(tmp_path, monkeypatch):
    from app.db import connect as _connect, init_db as _init
    db = tmp_path / "app.sqlite"
    _init(db)
    con = _connect(db)
    notes = []
    for i in range(6):
        body = ("transformer attention " if i == 3 else "unrelated filler ") * 80
        con.execute("INSERT INTO papers (id, title, origin) VALUES (?, ?, 'zotero-import')",
                    (i, f"P{i}"))
        con.execute(
            "INSERT INTO paper_notes (paper_id, collection_slug, summary, status) "
            "VALUES (?, 'vlms', ?, 'noted')",
            (i, body),
        )
        notes.append({"key": str(i), "summary": body, "thoughts": "", "key_quotes": "",
                      "updated_at": f"2026-05-0{i}T00:00:00"})
    con.commit()

    total = sum(len(wiki._note_text(n)) for n in notes)
    selected = wiki._select_notes(con, notes, '"transformer" OR "attention"', total // 3)
    con.close()
    assert 0 < len(selected) < len(notes)                  # budget trimmed it
    assert any(n["key"] == "3" for n in selected)           # most relevant kept
    assert sum(len(wiki._note_text(n)) for n in selected) <= total // 3 + max(
        len(wiki._note_text(n)) for n in notes
    )


def test_proposal_from_chat_requires_grounding(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # no refs -> rejected
    assert wiki.proposal_from_chat("vlms", "some claim", [], "synthesis") is None
    # a paper ref grounds it
    prop = wiki.proposal_from_chat(
        "vlms", "claim from chat", [{"type": "paper", "id": "K1"}], "synthesis"
    )
    assert prop is not None
    assert prop["section"] == "synthesis"


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
        "methods": ["Semantic-anchor approaches", "Diversity-aware compression",
                    "Thought-adaptive pruning"],
        "debates": ["Is importance pruning sufficient?",
                    "Is reasoning information localized?"],
        "open_questions": ["What actually needs to be preserved for reasoning?",
                            "Are reasoning traces compressible?"],
    },
    # Phase B: concepts + recommended_reading
    "concepts": [
        {"name": "Reasoning Preservation", "synonyms": ["reasoning preservation",
            "preserving reasoning", "reasoning-state"], "blurb": "Keep the KV cache that matters."},
        {"name": "Semantic Anchors", "synonyms": ["semantic anchor", "anchor token"],
         "blurb": "Tokens that carry the meaningful structure."},
        {"name": "KV Distillation", "synonyms": ["kv distillation", "distill kv cache"],
         "blurb": "Train a student cache from a teacher cache."},
        {"name": "ab", "synonyms": []},                # too short — dropped
        {"name": "Reasoning Preservation", "synonyms": []},  # duplicate slug — dropped
    ],
    "recommended_reading": [
        {"paper": "2401.00001", "why_now": "Clearest framing of the trade-off."},
        {"paper": "2401.00002", "why_now": "Compare alternative approach."},
        {"paper": "2401.00003", "why_now": "Extends with empirical evals."},
        {"paper": "NOPE",       "why_now": "Hallucinated — should be dropped."},
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
    out = wiki._validate_field_model(_FIELD_MODEL_JSON)
    assert len(out["landscape"]["problems"]) == wiki._LANDSCAPE_MAX_ITEMS
    # 'ab' (length 2) was dropped; the 7th honest item also fell off the end.
    assert "ab" not in out["landscape"]["problems"]
    # Methods came in under the cap — survives intact.
    assert len(out["landscape"]["methods"]) == 3


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
    # Landscape lists round-trip.
    assert "KV cache memory explosion" in loaded["landscape"]["problems"]
    assert "Semantic-anchor approaches" in loaded["landscape"]["methods"]
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

def test_validate_field_model_keeps_valid_concepts_and_recommendations():
    """Validator keeps non-duplicate, named concepts (drops 'ab' and the dup
    slug) and only recommended_reading entries whose ref is in valid_refs."""
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
    # Recommended: NOPE dropped; the three valid refs kept in order.
    refs = [r["paper"] for r in out["recommended"]]
    assert refs == ["2401.00001", "2401.00002", "2401.00003"]


def test_generate_overview_writes_concepts_and_recommended_files(tmp_path, monkeypatch):
    """generate_overview also writes wiki/sections/concepts.json and
    recommended.json. Each has an _meta block + structured payload."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    assert wiki.generate_overview("vlms") is True
    sdir = tmp_path / "collections" / "vlms" / "wiki" / "sections"
    assert (sdir / "concepts.json").is_file()
    assert (sdir / "recommended.json").is_file()
    cdata = json.loads((sdir / "concepts.json").read_text())
    assert cdata["_meta"]["generated_by"] == "agent"
    assert {c["slug"] for c in cdata["concepts"]} >= {"reasoning-preservation",
                                                       "semantic-anchors", "kv-distillation"}
    rdata = json.loads((sdir / "recommended.json").read_text())
    assert rdata["_meta"]["generated_by"] == "agent"
    assert [r["paper"] for r in rdata["picks"]] == ["2401.00001", "2401.00002", "2401.00003"]


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


def test_load_overview_recommended_resolves_paper_refs_and_assigns_labels(tmp_path, monkeypatch):
    """The recommended section resolves each ref to a paper object, attaches
    the live attention chips, and assigns positional labels (Start here /
    Next / Then) by index — not from the LLM."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wiki.generate_overview("vlms")
    loaded = wiki.load_overview("vlms")
    assert loaded["recommended"] is not None
    picks = loaded["recommended"]["picks"]
    assert len(picks) == 3
    assert picks[0]["position_label"] == "Start here"
    assert picks[1]["position_label"] == "Next"
    assert picks[2]["position_label"] == "Then"
    # Each pick carries a resolved paper object (id from the DB).
    assert {pk["paper"]["id"] for pk in picks} == {1, 2, 3}
    # why_now text preserved.
    assert picks[0]["why_now"].startswith("Clearest framing")


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
