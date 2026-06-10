"""Deep-search paper finder (Part 2): the MCP tools + the deep_find parse path.

The live agent spawn can't run in tests; we stub the engine and verify the parts
we own — the read-only MCP tools, and deep_find's JSON parse → metadata fetch →
candidate shape (drop invented ids, dedupe)."""
import json


def test_mcp_arxiv_search_and_recommendation_history(monkeypatch):
    import app.mcp_server as mcp, app.discover as discover, app.triage as triage
    monkeypatch.setattr(discover, "_arxiv_search",
                        lambda q, max_results=10: [{"arxiv_id": "1", "title": "T", "summary": "x" * 500}])
    out = mcp._call_tool("vlms", "arxiv_search", {"query": "vlm memory", "max_results": 3})
    assert out["count"] == 1 and out["results"][0]["arxiv_id"] == "1"
    assert len(out["results"][0]["summary"]) <= mcp.PREVIEW + 1     # previewed, not full

    monkeypatch.setattr(triage, "outcome_history", lambda s: {
        "accepted_titles": ["Kept paper"], "dismissed_titles": ["Dropped paper"],
        "accepted_arxiv": set(), "dismissed_arxiv": set()})
    h = mcp._call_tool("vlms", "recommendation_history", {})
    assert h["kept"] == ["Kept paper"] and h["passed_on"] == ["Dropped paper"]


def test_deep_find_parses_dedupes_and_fetches(monkeypatch, tmp_path):
    import app.paper_finder as pf, app.engine as engine_mod, app.discover as discover
    import app.agent_skills as ag

    class FakeRes:
        text = json.dumps({"papers": [
            {"arxiv_id": "2501.01234", "title": "Real", "why": "fits the purpose"},
            {"arxiv_id": "BOGUS", "title": "Invented", "why": "nope"},
            {"arxiv_id": "2501.01234", "title": "dup", "why": "again"},
        ]})

    class FakeEng:
        name = "fake"
        def available(self): return (True, "")
        def run_once(self, *a, **k): return FakeRes()

    monkeypatch.setattr(engine_mod, "build_engine", lambda cfg: FakeEng())
    monkeypatch.setattr(ag, "ensure_skills_home", lambda home=None: tmp_path)
    monkeypatch.setattr(pf.agents, "effective_tools", lambda k, d: d)
    monkeypatch.setattr(pf.mcp_server, "stdio_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(discover, "normalize_arxiv_id", lambda x: x if str(x).startswith("2501") else "")
    # metadata is fetched in ONE batched call (not one-per-pick) — count invocations
    batch_calls = []
    def fake_batch(ids):
        batch_calls.append(list(ids))
        return {aid: {"arxiv_id": aid, "title": "Fetched", "authors": "A",
                      "year": "2025", "abstract": "the real abstract"}
                for aid in ids if str(aid).startswith("2501")}
    monkeypatch.setattr(discover, "fetch_arxiv_batch", fake_batch)
    out = pf.deep_find("vlms", "focus text", "challenge H2", limit=5)
    assert len(batch_calls) == 1                          # single arXiv request, not per-pick
    assert len(out) == 1                                  # BOGUS dropped (unresolvable), dup deduped
    c = out[0]
    assert c["arxiv_id"] == "2501.01234" and c["summary"] == "the real abstract"
    assert c["note"] == "fits the purpose"                # the agent's "why" carries through


def test_deep_find_resolves_scholar_picks(monkeypatch, tmp_path):
    """A pick cited by s2_id (scholar_search) is resolved via the S2 batch API and
    carries venue/pdf_url through — peer-reviewed venues reachable in deep search."""
    import app.paper_finder as pf, app.engine as engine_mod, app.discover as discover
    import app.agent_skills as ag, app.semantic_scholar as s2

    class FakeRes:
        text = json.dumps({"papers": [
            {"s2_id": "649def34f8be52c8b66281af98ae884c09aef38b", "title": "CVPR work", "why": "peer-reviewed fit"},
            {"arxiv_id": "2501.55555", "title": "Preprint", "why": "fresh"},
        ]})

    class FakeEng:
        name = "fake"
        def available(self): return (True, "")
        def run_once(self, *a, **k): return FakeRes()

    monkeypatch.setattr(engine_mod, "build_engine", lambda cfg: FakeEng())
    monkeypatch.setattr(ag, "ensure_skills_home", lambda home=None: tmp_path)
    monkeypatch.setattr(pf.agents, "effective_tools", lambda k, d: d)
    monkeypatch.setattr(pf.mcp_server, "stdio_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(discover, "fetch_arxiv_batch",
                        lambda ids: {aid: {"arxiv_id": aid, "title": "Pre", "authors": "A",
                                           "year": "2025", "abstract": "preprint abstract"} for aid in ids})
    monkeypatch.setattr(s2, "fetch_batch", lambda ids: {
        ids[0]: {"arxiv_id": None, "doi": "10.1/cvpr", "s2_id": ids[0], "title": "CVPR work",
                 "summary": "the venue abstract", "authors": "B", "year": "2024",
                 "venue": "CVPR", "citation_count": 30, "pdf_url": "https://x/cvpr.pdf"}})
    out = pf.deep_find("vlms", "focus", "most relevant", limit=5)
    by_venue = {c.get("venue", "") for c in out}
    assert len(out) == 2 and "CVPR" in by_venue           # both an arXiv and an S2 pick resolved
    cvpr = next(c for c in out if c.get("venue") == "CVPR")
    assert cvpr["doi"] == "10.1/cvpr" and cvpr["pdf_url"] and cvpr["note"] == "peer-reviewed fit"
