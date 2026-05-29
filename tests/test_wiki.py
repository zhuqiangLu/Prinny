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


def test_generate_overview_no_abstracts_returns_false(tmp_path, monkeypatch):
    wdir = _seed_setup(tmp_path, monkeypatch, lambda m, model=None: "{}")
    con = connect(tmp_path / "app.sqlite")
    con.execute("UPDATE papers SET abstract='' WHERE id=1"); con.commit(); con.close()
    assert wiki.generate_overview("vlms") is False        # nothing to ground a map on
    assert not (wdir / "overview.json").exists()
