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


# --- llm_wiki-pattern starter-wiki tests (2026-05-30) ------------------------
# The new pipeline does TWO kinds of LLM calls. The stub branches on user-prompt
# content: analyze calls get a JSON top-picks payload; per-page calls get a
# markdown body. Picks include a hallucinated ref so the validator is exercised;
# per-page body includes a hallucinated [[wikilink]] so _clean_paper_page_body
# is exercised too.

_ANALYZE_JSON = {
    "field_intro": "The collection circles efficient long-context VLMs and the compression-vs-recall trade-off.",
    "top_picks": [
        {"paper": "2401.00001", "why_now": "clearest framing", "focus_on": "§3", "skip": "§6"},
        {"paper": "2401.00002", "why_now": "compare alt", "focus_on": "", "skip": ""},
        {"paper": "2401.00003", "why_now": "extends with evals", "focus_on": "tables", "skip": ""},
        {"paper": "NOPE",       "why_now": "ghost", "focus_on": "", "skip": ""},  # invalid -> dropped
    ],
    "reading_order": ["2401.00001", "2401.00002", "2401.00003", "NOPE"],
}

_PAGE_MD = (
    "## Problem\nKV cache blows up at long context.\n\n"
    "## Key idea\nJointly optimize importance and diversity.\n\n"
    "## Mechanism\nQuantization plus diversity-aware eviction.\n\n"
    "## Evidence\nTested on 32k benchmarks.\n\n"
    "## Limitation\nRecall drops at very high compression.\n\n"
    "## Why read\nClearest framing of the trade-off for newcomers.\n\n"
    "## Connected\n- [[P2]] — extends the per-token scoring idea\n"
    "- [[NonexistentPaper]] — this should be stripped to plain text\n"
)


def _llm_stub(analyze_json=None, page_md=None):
    """Build a llm.complete stub for the two-step pipeline. Branches on whether
    the user prompt names a single paper (page call) or starts with 'Papers:'
    (analyze call). Either argument can be None to simulate that step failing."""
    aj = analyze_json if analyze_json is not None else _ANALYZE_JSON
    pm = page_md if page_md is not None else _PAGE_MD
    def stub(messages, model=None):
        user = messages[-1]["content"]
        if "This paper:" in user:
            if pm is None:
                raise RuntimeError("simulated page-step failure")
            return pm
        if aj is None:
            raise RuntimeError("simulated analyze-step failure")
        return json.dumps(aj)
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


def test_generate_overview_writes_tree_and_drops_hallucinated_refs(tmp_path, monkeypatch):
    """The new two-step pipeline writes wiki/starter/{index.md, papers/*.md}.
    Hallucinated picks (NOPE) are dropped at the analyze step. The starter tree
    is created and the index lists only valid picks in agent order."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    assert wiki.generate_overview("vlms") is True
    sdir = tmp_path / "collections" / "vlms" / "wiki" / "starter"
    assert sdir.is_dir() and (sdir / "index.md").is_file()
    pages = sorted(p.name for p in (sdir / "papers").glob("*.md"))
    # 3 valid picks survived (P1/P2/P3 → slugs p1/p2/p3 in our test fixture);
    # NOPE was dropped at the analyze validator.
    assert pages == ["p1.md", "p2.md", "p3.md"]
    idx_meta, _ = wiki.frontmatter.parse((sdir / "index.md").read_text())
    assert idx_meta["generated_by"] == "agent"
    assert idx_meta["generator"] == "starter-wiki"
    assert idx_meta["top_picks"] == ["p1", "p2", "p3"]


def test_generate_overview_blanks_pdf_only_sections_for_abstract_only(tmp_path, monkeypatch):
    """No PDF is cached in the test fixture → every page is ABSTRACT_ONLY → the
    validator strips Mechanism / Evidence / Limitation sections regardless of
    what the LLM emitted. The defensible-from-abstract sections (Problem / Key
    idea / Why read / Connected) survive."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    assert wiki.generate_overview("vlms") is True
    page_body = (tmp_path / "collections" / "vlms" / "wiki" / "starter" / "papers" / "p1.md").read_text()
    _, body = wiki.frontmatter.parse(page_body)
    assert "## Mechanism" not in body
    assert "## Evidence" not in body
    assert "## Limitation" not in body
    assert "## Problem" in body
    assert "## Key idea" in body
    assert "## Why read" in body


def test_generate_overview_strips_broken_wikilinks(tmp_path, monkeypatch):
    """[[wikilinks]] pointing at page slugs we didn't generate get collapsed to
    plain text (no broken links left in the rendered output). Links to valid
    sibling pages survive."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    assert wiki.generate_overview("vlms") is True
    body = (tmp_path / "collections" / "vlms" / "wiki" / "starter" / "papers" / "p1.md").read_text()
    # [[P2]] points at a valid page (p2) → survives. [[NonexistentPaper]] → collapses.
    assert "[[P2]]" in body
    assert "[[NonexistentPaper]]" not in body
    assert "NonexistentPaper" in body   # collapsed to plain text, not deleted


def test_generate_overview_refuses_too_few_picks(tmp_path, monkeypatch):
    """If the analyze step returns fewer than _STARTER_TOP_PICKS_MIN valid picks,
    the pipeline refuses (returns False, writes nothing). Saves the user from a
    one-pick starter wiki."""
    thin = {**_ANALYZE_JSON, "top_picks": [_ANALYZE_JSON["top_picks"][0]]}  # 1 pick
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub(analyze_json=thin))
    assert wiki.generate_overview("vlms") is False
    sdir = tmp_path / "collections" / "vlms" / "wiki" / "starter"
    assert not sdir.exists()


def test_generate_overview_is_nondestructive_unless_forced(tmp_path, monkeypatch):
    """A second generate_overview without force=True is a no-op (existing tree
    survives). force=True regenerates."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    assert wiki.generate_overview("vlms") is True
    assert wiki.generate_overview("vlms") is False                  # idempotent
    assert wiki.generate_overview("vlms", force=True) is True       # forced


def test_load_overview_reads_tree_and_resolves_refs(tmp_path, monkeypatch):
    """load_overview reads the markdown tree, resolves frontmatter sources to
    live paper objects, returns the top_picks in agent order (with no attention)."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wiki.generate_overview("vlms")
    loaded = wiki.load_overview("vlms")
    assert loaded is not None
    assert loaded["needs_migration"] is False
    assert loaded["field_intro_md"].startswith("The collection circles")
    titles = [pg["title"] for pg in loaded["top_picks"]]
    assert titles == ["P1", "P2", "P3"]
    # Each page carries a resolved paper object and the body markdown.
    for pg in loaded["top_picks"]:
        assert pg["paper"]["id"] in (1, 2, 3)
        assert pg["body_md"].startswith("## Problem")
    # Wikilinks were rewritten to /c/<slug>/p/<paper-id> URLs at load time.
    assert "/c/vlms/p/2" in loaded["top_picks"][0]["body_md"]


def test_load_overview_returns_migration_banner_for_legacy_json(tmp_path, monkeypatch):
    """An old wiki/overview.json on disk with no new wiki/starter/ tree returns
    {needs_migration: True} so the panel can show a regenerate prompt."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wdir = tmp_path / "collections" / "vlms" / "wiki"
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "overview.json").write_text("{}", encoding="utf-8")
    loaded = wiki.load_overview("vlms")
    assert loaded is not None
    assert loaded["needs_migration"] is True
    assert loaded["top_picks"] == []


def test_load_overview_returns_none_when_no_wiki(tmp_path, monkeypatch):
    """No starter tree AND no legacy overview.json → None (template shows the
    'No starter wiki yet' card with the Draft button)."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    assert wiki.load_overview("vlms") is None


def _seed_three_papers_with_starter(tmp_path, monkeypatch):
    """Shared fixture for the attention tests: 3 papers + a generated starter
    wiki, so re-ranking / hot / new badges have something to chew on."""
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


def test_top_picks_reranked_by_attention(tmp_path, monkeypatch):
    db = _seed_three_papers_with_starter(tmp_path, monkeypatch)
    con = connect(db)
    # P3 gets the biggest signal -> should float to the top despite being last editorially.
    for _ in range(10):
        con.execute("INSERT INTO annotations(paper_id, collection_slug, kind, page, position_json) "
                    "VALUES (3, 'vlms', 'highlight', 1, '{}')")
    con.execute("INSERT INTO paper_notes(paper_id, collection_slug, thoughts, status) "
                "VALUES (1, 'vlms', 't', 'noted')")    # P1 gets 5
    con.commit(); con.close()
    loaded = wiki.load_overview("vlms")
    order = [pg["paper"]["id"] for pg in loaded["top_picks"]]
    assert order == [3, 1, 2]                     # by score: 10, 5, 0
    # With no signal at all, agent's reading order holds (the stable-sort property).
    con = connect(db); con.execute("DELETE FROM annotations"); con.execute("DELETE FROM paper_notes")
    con.commit(); con.close()
    loaded2 = wiki.load_overview("vlms")
    assert [pg["paper"]["id"] for pg in loaded2["top_picks"]] == [1, 2, 3]


def test_is_hot_and_is_new_badges(tmp_path, monkeypatch):
    import time
    db = _seed_three_papers_with_starter(tmp_path, monkeypatch)
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
    by_id = {pg["paper"]["id"]: pg for pg in loaded["top_picks"]}
    assert by_id[2]["is_new"] is True                      # had new signal post-baseline
    assert by_id[1]["is_new"] is False and by_id[3]["is_new"] is False
    assert by_id[2]["is_hot"] is True                      # 4 ≥ floor of 2 & is the only nonzero
    # Without a `since`, no card is ever marked new (no fake recency claims).
    loaded2 = wiki.load_overview("vlms")
    assert all(pg["is_new"] is False for pg in loaded2["top_picks"])


def test_stage_callback_fires_through_pipeline(tmp_path, monkeypatch):
    """generate_overview calls stage_cb in the new two-step pipeline's order:
    gathering → reading_pdfs (with count) → analyzing → writing (per page) → linking."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    stages: list = []
    assert wiki.generate_overview("vlms", force=True,
                                   stage_cb=lambda n, **kw: stages.append((n, kw))) is True
    names = [s[0] for s in stages]
    assert names[0] == "gathering"
    for expected in ("reading_pdfs", "analyzing", "writing", "linking"):
        assert expected in names, f"stage {expected!r} missing from {names}"
    # reading_pdfs reports a real pdfs_done/pdfs_total fraction
    rp = next(s for s in stages if s[0] == "reading_pdfs")
    assert "pdfs_done" in rp[1] and "pdfs_total" in rp[1]
    # writing reports a real pages_done/pages_total fraction (one event per pick)
    writes = [s for s in stages if s[0] == "writing"]
    assert len(writes) >= 2                       # at least one before-call, one after
    assert all("pages_done" in s[1] and "pages_total" in s[1] for s in writes)


def test_stage_callback_errors_dont_abort_generation(tmp_path, monkeypatch):
    """A broken UI callback can't take down a draft."""
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    def bad(name, **kw):
        raise RuntimeError("UI exploded")
    assert wiki.generate_overview("vlms", force=True, stage_cb=bad) is True
    assert (tmp_path / "collections" / "vlms" / "wiki" / "starter" / "index.md").is_file()


def test_stage_message_single_action_line():
    """_stage_message returns a single human-voice action line per stage."""
    assert wiki._stage_message({"stage": "gathering"})["action"].startswith("I'm collecting")
    rp = wiki._stage_message({"stage": "reading_pdfs", "pdfs_done": 3, "pdfs_total": 12})
    assert rp["action"] == "I'm reading the PDFs (3/12)."
    assert rp["subline"] == ""
    wr = wiki._stage_message({"stage": "writing", "pages_done": 2, "pages_total": 5})
    assert wr["action"] == "I'm writing the page (2/5)."
    assert wiki._stage_message({"stage": "analyzing"})["action"].startswith("I'm picking")
    assert wiki._stage_message({"stage": "linking"})["action"].startswith("I'm wiring")
    assert wiki._stage_message({"stage": "done"})["action"] == "Done."
    # Unknown stage falls back to gathering voice rather than crashing.
    assert wiki._stage_message({"stage": "wat"})["action"].startswith("I'm collecting")


def test_stage_progress_monotone_and_capped():
    """_stage_progress climbs through the pipeline's real stages and stays <100
    until status='done'/'failed' lands on 100."""
    p_gather   = wiki._stage_progress({"stage": "gathering"})
    p_pdf_0    = wiki._stage_progress({"stage": "reading_pdfs", "pdfs_done": 0, "pdfs_total": 10})
    p_pdf_10   = wiki._stage_progress({"stage": "reading_pdfs", "pdfs_done": 10, "pdfs_total": 10})
    p_analyze  = wiki._stage_progress({"stage": "analyzing"})
    p_write_0  = wiki._stage_progress({"stage": "writing", "pages_done": 0, "pages_total": 5})
    p_write_5  = wiki._stage_progress({"stage": "writing", "pages_done": 5, "pages_total": 5})
    p_link     = wiki._stage_progress({"stage": "linking"})
    assert p_gather < p_pdf_0 < p_pdf_10 <= p_analyze <= p_write_0 < p_write_5 <= p_link < 100
    # Done/failed flip to 100 regardless of stage payload.
    assert wiki._stage_progress({"stage": "writing", "status": "done"}) == 100
    assert wiki._stage_progress({"stage": "linking", "status": "failed"}) == 100


def test_start_draft_async_publishes_state_and_completes(tmp_path, monkeypatch):
    """The async runner: kicks off in a thread, transitions to 'done' on success,
    writes the wiki/starter/ tree. Tests the contract end-to-end."""
    import time
    _seed_three_papers(tmp_path, monkeypatch, _llm_stub())
    wiki.clear_draft_job("vlms")
    assert wiki.start_draft_async("vlms", force=True) is True
    assert wiki.start_draft_async("vlms", force=True) is False     # already running
    deadline = time.time() + 5
    while time.time() < deadline:
        job = wiki.get_draft_job("vlms")
        if job and job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    job = wiki.get_draft_job("vlms")
    assert job is not None and job["status"] == "done"
    assert (tmp_path / "collections" / "vlms" / "wiki" / "starter" / "index.md").is_file()
    wiki.clear_draft_job("vlms")
    assert wiki.get_draft_job("vlms") is None


def test_start_draft_async_publishes_failure(tmp_path, monkeypatch):
    """If generate_overview returns False (e.g. the analyze step returns no picks),
    the job ends as status='failed' with a human-readable error."""
    import time
    _seed_three_papers(tmp_path, monkeypatch,
                        _llm_stub(analyze_json={"top_picks": [], "reading_order": [],
                                                "field_intro": "x"}))
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
    assert not (tmp_path / "collections" / "vlms" / "wiki" / "starter").exists()
