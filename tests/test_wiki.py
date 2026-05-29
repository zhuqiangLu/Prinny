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


def _seed_setup(tmp_path, monkeypatch, llm_fn):
    import app.library as library
    import app.pdf_store as pdf_store
    db = tmp_path / "app.sqlite"
    init_db(db)
    con = connect(db)
    con.execute("INSERT INTO collections(slug,name) VALUES('vlms','VLMs')")
    con.execute("INSERT INTO papers(id,title,abstract,arxiv_id,origin) "
                "VALUES(1,'P1','We study efficient VLMs.','2401.00001','app-created')")
    con.execute("INSERT INTO collection_papers(collection_slug,paper_id) VALUES('vlms',1)")
    con.commit(); con.close()
    monkeypatch.setattr(wiki, "connect", lambda: connect(db))
    monkeypatch.setattr(wiki, "COLLECTIONS_DIR", tmp_path / "collections")
    monkeypatch.setattr(library, "connect", lambda: connect(db))   # for load_overview ref-resolution
    monkeypatch.setattr(pdf_store, "load_config", lambda: {"pdf_store_path": str(tmp_path / "store")})
    monkeypatch.setattr("app.llm.complete", llm_fn)
    return tmp_path / "collections" / "vlms" / "wiki"


# The curiosity-driven starter-wiki shape (2026-05-29). Includes one hallucinated ref
# (NOPE) in each section so the test verifies the ref-validator drops them.
_OVERVIEW_JSON = {
    "field_overview": {
        "one_sentence": "Long-context VLMs are bottlenecked by KV-cache memory, not compute.",
        "one_paragraph": "All about efficient long-context VLMs — what gives and what doesn't when you push the context window.",
        "core_tension": "Compression vs recall.",
        "why_matters": "Deployment costs scale with the cache, not the model size.",
        "what_changed_recently": "",
        "what_newcomer_should_notice": "Read the eval setup before the method — most numbers are setup-dependent.",
    },
    "problems": [{
        "title": "KV cache blows up at long context", "why": "memory-bound deployment",
        "tension": "compression vs recall?",
        "approaches": [{"label": "compression", "papers": ["2401.00001"]},
                       {"label": "ghost", "papers": ["NOPE"]}],          # NOPE ref -> dropped
        "read_first": {"paper": "2401.00001", "why": "clearest framing"},
        "papers": ["2401.00001", "NOPE"],
    }],
    "paper_cards": [
        {
            "paper": "2401.00001", "status": "method", "difficulty": "medium",
            "problem": "KV-cache memory at long context.",
            "idea": "Quantize older entries to cut memory.",
            "method_family": "KV compression",
            "contribution": "First quantization scheme that holds at >32k tokens.",
            "why_read": "Clearest framing of the trade-off.",
            "prerequisites": [], "connected_papers": [],
            # PDF-only fields — agent emits them; validator should BLANK them
            # because this paper was supplied as ABSTRACT_ONLY in the test setup.
            "mechanism": "per-block 4-bit quantization",
            "evidence": "tested on 32k benchmarks",
            "limitation": "recall drops at high compression",
        },
        # hallucinated card -> dropped entirely
        {"paper": "NOPE", "status": "method", "problem": "x", "idea": "y"},
    ],
    "reading_paths": [
        # honest: 3 unique refs (one duplicate to test dedupe), survives the ≥3 gate
        {"name": "Orientation", "for_who": "newcomers", "goal": "lay of the land",
         "ordered_papers": [{"paper": "2401.00001", "why_now": "start here"},
                            {"paper": "2401.00001", "why_now": "dup"},  # dedupe
                            {"paper": "NOPE", "why_now": "ghost"}]},     # invalid -> dropped
        # too few honest refs (only 2401.00001) -> dropped per _READING_PATH_MIN_PAPERS
        {"name": "Critical", "for_who": "skeptics", "goal": "see the cracks",
         "ordered_papers": [{"paper": "2401.00001"}]},
    ],
}


def test_generate_overview_validates_and_drops_hallucinated_refs(tmp_path, monkeypatch):
    wdir = _seed_setup(tmp_path, monkeypatch, lambda m, model=None: json.dumps(_OVERVIEW_JSON))
    assert wiki.generate_overview("vlms") is True
    ov = json.loads((wdir / "overview.json").read_text())
    assert ov["_meta"]["generated_by"] == "agent" and ov["_meta"]["generator"] == "starter-wiki"
    # field_overview migrates from agent output verbatim
    assert ov["field_overview"]["one_sentence"].startswith("Long-context")
    # problem refs filtered to valid only
    prob = ov["problems"][0]
    assert prob["papers"] == ["2401.00001"]              # hallucinated ref dropped
    assert [a["label"] for a in prob["approaches"]] == ["compression"]  # ghost approach dropped
    assert prob["read_first"] == "2401.00001"
    # paper cards: hallucinated card dropped; valid one kept
    assert [c["paper"] for c in ov["paper_cards"]] == ["2401.00001"]
    # No PDF was cached in the test setup → that paper is ABSTRACT_ONLY → PDF-only
    # fields (mechanism / evidence / limitation) MUST be blanked even if the agent emits them.
    card = ov["paper_cards"][0]
    assert card["abstract_only"] is True
    assert card["mechanism"] == "" and card["evidence"] == "" and card["limitation"] == ""
    assert card["idea"] != ""   # but abstract-fair fields are kept
    # reading_paths: ≥3-paper gate kept Orientation (2401.00001 once, NOPE dropped) -> only
    # 1 honest ref so it's actually under the floor → dropped. "Critical" has 1 -> dropped.
    # → no reading_paths survive in this fixture.
    assert ov["reading_paths"] == []
    assert wiki.generate_overview("vlms") is False        # non-destructive
    assert wiki.generate_overview("vlms", force=True) is True  # force regenerates


def test_load_overview_resolves_refs_to_papers(tmp_path, monkeypatch):
    _seed_setup(tmp_path, monkeypatch, lambda m, model=None: json.dumps(_OVERVIEW_JSON))
    wiki.generate_overview("vlms")
    loaded = wiki.load_overview("vlms")
    assert loaded["field_overview"]["one_paragraph"].startswith("All about")
    p0 = loaded["problems"][0]
    assert p0["read_first"]["id"] == 1 and p0["read_first"]["title"] == "P1"   # ref -> paper obj
    assert [pp["id"] for pp in p0["papers"]] == [1]
    # paper card refs are resolved to paper objects too
    pc0 = loaded["paper_cards"][0]
    assert pc0["paper"]["id"] == 1 and pc0["abstract_only"] is True


def test_load_overview_migrates_old_shape(tmp_path, monkeypatch):
    """An overview.json saved before the 2026-05-29 schema bump still renders."""
    wdir = _seed_setup(tmp_path, monkeypatch, lambda m, model=None: "{}")
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "overview.json").write_text(json.dumps({
        "intro": "Old-shape intro.",
        "gaps": [{"title": "Old gap", "body": "x", "papers": ["2401.00001"]}],
        "reading_path": ["2401.00001", "2401.00001", "2401.00001"],
        "_meta": {"generated_by": "agent"},
    }))
    loaded = wiki.load_overview("vlms")
    # intro migrates into field_overview.one_paragraph
    assert loaded["field_overview"]["one_paragraph"] == "Old-shape intro."
    # gaps migrate into open_problems
    assert loaded["open_problems"][0]["title"] == "Old gap"
    # singular reading_path migrates into one "Orientation" reading_paths entry
    assert loaded["reading_paths"][0]["name"] == "Orientation"
    assert loaded["reading_paths"][0]["ordered_papers"][0]["paper"]["id"] == 1


def test_reading_path_min_papers_gate(tmp_path, monkeypatch):
    """A reading path with ≥3 unique valid refs survives; thinner ones are dropped."""
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
    monkeypatch.setattr("app.llm.complete", lambda m, model=None: json.dumps({
        "reading_paths": [
            # 3 unique valid refs -> survives
            {"name": "Orientation", "for_who": "x", "goal": "y",
             "ordered_papers": [{"paper": "2401.00001"}, {"paper": "2401.00002"},
                                {"paper": "2401.00003"}]},
            # only 2 unique valid refs -> dropped
            {"name": "Skeptic", "for_who": "x", "goal": "y",
             "ordered_papers": [{"paper": "2401.00001"}, {"paper": "2401.00002"}]},
        ],
        "paper_cards": [{"paper": "2401.00001", "idea": "x"}],  # so generate accepts
    }))
    assert wiki.generate_overview("vlms") is True
    loaded = wiki.load_overview("vlms")
    assert [rp["name"] for rp in loaded["reading_paths"]] == ["Orientation"]


def _seed_three_papers_with_cards(tmp_path, monkeypatch):
    """Shared fixture for the attention-reweighting tests: 3 papers, an overview whose
    cards are deliberately in 'editorial' order (P1, P2, P3) so any re-rank is visible."""
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
    monkeypatch.setattr("app.llm.complete", lambda m, model=None: json.dumps({
        "paper_cards": [{"paper": f"2401.0000{i}", "idea": f"i{i}"} for i in (1, 2, 3)],
    }))
    wiki.generate_overview("vlms")
    return db


def test_attention_scores_counts_highlights_and_notes(tmp_path, monkeypatch):
    db = _seed_three_papers_with_cards(tmp_path, monkeypatch)
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


def test_paper_cards_reranked_by_attention(tmp_path, monkeypatch):
    db = _seed_three_papers_with_cards(tmp_path, monkeypatch)
    con = connect(db)
    # P3 gets the biggest signal -> should float to the top despite being last editorially.
    for _ in range(10):
        con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, position_json) "
                    "VALUES (3, 'vlms', 'highlight', 1, '{}')")
    con.execute("INSERT INTO paper_notes(paper_id, collection_slug, thoughts, status) "
                "VALUES (1, 'vlms', 't', 'noted')")    # P1 gets 5
    con.commit(); con.close()
    loaded = wiki.load_overview("vlms")
    order = [c["paper"]["id"] for c in loaded["paper_cards"]]
    assert order == [3, 1, 2]                     # by score: 10, 5, 0
    # With no signal at all, editorial order holds (the stable-sort property).
    con = connect(db); con.execute("DELETE FROM annotations"); con.execute("DELETE FROM paper_notes")
    con.commit(); con.close()
    loaded2 = wiki.load_overview("vlms")
    assert [c["paper"]["id"] for c in loaded2["paper_cards"]] == [1, 2, 3]


def test_is_hot_and_is_new_badges(tmp_path, monkeypatch):
    import time
    db = _seed_three_papers_with_cards(tmp_path, monkeypatch)
    # Establish a "last viewed" baseline BEFORE adding the new signal.
    baseline = wiki.read_and_bump_viewed("vlms")
    # The very first call has no prior value -> returns None.
    assert baseline is None
    # Read the just-bumped value so we have a stable "since" timestamp for the test.
    con = connect(db)
    since = con.execute("SELECT last_wiki_viewed_at FROM collections WHERE slug='vlms'").fetchone()[0]
    con.close()
    time.sleep(1.1)    # CURRENT_TIMESTAMP is second-resolution; cross a tick before the new signal
    # New attention AFTER the baseline -> should show up as is_new.
    con = connect(db)
    for _ in range(4):
        con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, position_json) "
                    "VALUES (2, 'vlms', 'highlight', 1, '{}')")
    con.commit(); con.close()
    loaded = wiki.load_overview("vlms", attention_since=since)
    by_id = {c["paper"]["id"]: c for c in loaded["paper_cards"]}
    assert by_id[2]["is_new"] is True                      # had new signal post-baseline
    assert by_id[1]["is_new"] is False and by_id[3]["is_new"] is False
    assert by_id[2]["is_hot"] is True                      # 4 ≥ floor of 2 & is the only nonzero
    # Without a `since`, no card is ever marked new (no fake recency claims).
    loaded2 = wiki.load_overview("vlms")
    assert all(c["is_new"] is False for c in loaded2["paper_cards"])


def test_stage_callback_fires_through_pipeline(tmp_path, monkeypatch):
    """generate_overview calls stage_cb in the right order with the right shape."""
    wdir = _seed_setup(tmp_path, monkeypatch, lambda m, model=None: json.dumps(_OVERVIEW_JSON))
    stages = []
    wiki.generate_overview("vlms", force=True, stage_cb=lambda n, **kw: stages.append((n, kw)))
    names = [s[0] for s in stages]
    # gathering -> at least one reading_pdfs -> drafting -> validating
    assert names[0] == "gathering"
    assert "reading_pdfs" in names
    assert "drafting" in names and "validating" in names
    # reading_pdfs reports pdfs_done/pdfs_total
    rp = next(s for s in stages if s[0] == "reading_pdfs")
    assert "pdfs_done" in rp[1] and "pdfs_total" in rp[1]
    # drafting/validating carry paper_count + pdfs_read
    dr = next(s for s in stages if s[0] == "drafting")
    assert "paper_count" in dr[1] and "pdfs_read" in dr[1]


def test_stage_callback_errors_dont_abort_generation(tmp_path, monkeypatch):
    """A broken UI callback can't take down a draft."""
    wdir = _seed_setup(tmp_path, monkeypatch, lambda m, model=None: json.dumps(_OVERVIEW_JSON))
    def bad(name, **kw):
        raise RuntimeError("UI exploded")
    assert wiki.generate_overview("vlms", force=True, stage_cb=bad) is True
    assert (wdir / "overview.json").exists()


def test_stage_message_single_action_line():
    """_stage_message returns a single human-voice action line per stage and an
    empty subline (kept in the shape for JSON-contract stability). Counts are
    folded into the action itself."""
    assert wiki._stage_message({"stage": "gathering"})["action"].startswith("I'm collecting")
    rp = wiki._stage_message({"stage": "reading_pdfs", "pdfs_done": 3, "pdfs_total": 12})
    assert rp["action"] == "I'm reading the PDFs (3/12)."
    assert rp["subline"] == ""           # always empty in the new shape
    dr = wiki._stage_message({"stage": "drafting", "paper_count": 12, "pdfs_read": 8})
    assert dr["action"] == "I'm drafting the wiki."
    assert dr["subline"] == ""
    assert wiki._stage_message({"stage": "done"})["action"] == "Done."
    # Unknown stage falls back to gathering voice rather than crashing.
    assert wiki._stage_message({"stage": "wat"})["action"].startswith("I'm collecting")


def test_stage_progress_monotone_and_capped():
    """_stage_progress climbs in the right order and stays <100 until status='done'."""
    p_gather = wiki._stage_progress({"stage": "gathering"})
    p_pdf_0  = wiki._stage_progress({"stage": "reading_pdfs", "pdfs_done": 0, "pdfs_total": 10})
    p_pdf_5  = wiki._stage_progress({"stage": "reading_pdfs", "pdfs_done": 5, "pdfs_total": 10})
    p_pdf_10 = wiki._stage_progress({"stage": "reading_pdfs", "pdfs_done": 10, "pdfs_total": 10})
    p_draft  = wiki._stage_progress({"stage": "drafting"})
    p_valid  = wiki._stage_progress({"stage": "validating"})
    # gathering < reading_pdfs band < drafting (no start time → low band) ≤ validating < 100
    assert p_gather < p_pdf_0 < p_pdf_5 < p_pdf_10 <= p_draft < p_valid < 100
    # Done flips to 100 regardless of stage payload.
    assert wiki._stage_progress({"stage": "drafting", "status": "done"}) == 100
    assert wiki._stage_progress({"stage": "validating", "status": "failed"}) == 100


def test_drafting_progress_climbs_over_time():
    """In the drafting stage, the bar climbs asymptotically from ~30 toward 95 as
    drafting_started_at recedes into the past. Fixes the 'stuck at 60% then leaps
    to 100%' UX. Never crosses 95 — the honest cap before status flips to done."""
    import time
    now = time.time()
    p_just_started = wiki._stage_progress({"stage": "drafting", "drafting_started_at": now - 0.1})
    p_mid          = wiki._stage_progress({"stage": "drafting", "drafting_started_at": now - 35})
    p_late         = wiki._stage_progress({"stage": "drafting", "drafting_started_at": now - 120})
    p_runaway      = wiki._stage_progress({"stage": "drafting", "drafting_started_at": now - 9999})
    assert 28 <= p_just_started <= 32     # near LOW (30)
    assert p_mid > p_just_started + 20    # has climbed substantially by t≈τ
    assert p_late > p_mid                 # still climbing
    assert p_runaway == 95                 # asymptote cap holds forever


def test_start_draft_async_publishes_state_and_completes(tmp_path, monkeypatch):
    """The async runner: kicks off in a thread, publishes state via get_draft_job,
    transitions to 'done' on success, writes overview.json. Tests the contract end-
    to-end with a fast in-process llm.complete stub."""
    import time
    wdir = _seed_setup(tmp_path, monkeypatch, lambda m, model=None: json.dumps(_OVERVIEW_JSON))
    wiki.clear_draft_job("vlms")   # ensure clean
    assert wiki.start_draft_async("vlms", force=True) is True
    # A second start while running is rejected.
    assert wiki.start_draft_async("vlms", force=True) is False
    # Wait for completion (the runner is in a daemon thread; this test's LLM stub is sync).
    deadline = time.time() + 5
    while time.time() < deadline:
        job = wiki.get_draft_job("vlms")
        if job and job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    job = wiki.get_draft_job("vlms")
    assert job is not None and job["status"] == "done"
    assert (wdir / "overview.json").exists()
    # Cleared on demand.
    wiki.clear_draft_job("vlms")
    assert wiki.get_draft_job("vlms") is None


def test_start_draft_async_publishes_failure(tmp_path, monkeypatch):
    """If generate_overview returns False (no usable LLM output), the job ends as
    status='failed' with a human-readable error."""
    import time
    wdir = _seed_setup(tmp_path, monkeypatch, lambda m, model=None: "{}")  # empty -> validator rejects
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
    wdir = _seed_setup(tmp_path, monkeypatch, lambda m, model=None: "{}")
    con = connect(tmp_path / "app.sqlite")
    con.execute("UPDATE papers SET abstract='' WHERE id=1"); con.commit(); con.close()
    assert wiki.generate_overview("vlms") is False        # nothing to ground a map on
    assert not (wdir / "overview.json").exists()
