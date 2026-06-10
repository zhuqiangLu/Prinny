"""Semantic Scholar source: adapter mapping, arXiv→S2 fallback, non-arXiv import."""
import app.semantic_scholar as s2
import app.discover as discover


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


_PAYLOAD = {"data": [
    {"paperId": "abc", "title": "Long-Video VLM Reasoning", "abstract": "We study long video.",
     "year": 2026, "venue": "CVPR", "citationCount": 42,
     "externalIds": {"ArXiv": "2601.01234", "DOI": "10.1/x"},
     "openAccessPdf": {"url": "https://s2.example/pdf/abc.pdf"},
     "authors": [{"name": "A. One"}, {"name": "B. Two"}]},
    {"paperId": "noabs", "title": "No abstract", "abstract": None, "externalIds": {}},
]}


def test_adapter_maps_and_drops_abstractless(monkeypatch):
    monkeypatch.setattr(s2.httpx, "get", lambda *a, **k: _Resp(200, _PAYLOAD))
    monkeypatch.setattr(s2, "load_config", lambda: {})
    out = s2.search("long video vlm", max_results=10)
    assert len(out) == 1                         # the abstract-less one is dropped
    c = out[0]
    assert c["arxiv_id"] == "2601.01234" and c["doi"] == "10.1/x"
    assert c["venue"] == "CVPR" and c["citation_count"] == 42
    assert c["pdf_url"].endswith("abc.pdf") and c["authors"] == "A. One, B. Two"


def test_search_429_raises_s2error(monkeypatch):
    monkeypatch.setattr(s2.httpx, "get", lambda *a, **k: _Resp(429, {}))
    monkeypatch.setattr(s2, "load_config", lambda: {})
    import pytest
    with pytest.raises(s2.S2Error, match="429"):
        s2.search("x")


def test_api_key_header_sent_when_set(monkeypatch):
    seen = {}
    def fake_get(url, **kw):
        seen["headers"] = kw.get("headers", {})
        return _Resp(200, {"data": []})
    monkeypatch.setattr(s2.httpx, "get", fake_get)
    monkeypatch.setattr(s2, "load_config", lambda: {"semantic_scholar_api_key": "KEY123"})
    s2.search("x")
    assert seen["headers"].get("x-api-key") == "KEY123"


def test_merge_candidates_dedupes_and_enriches():
    """arXiv + S2 hits for the same paper merge into one, with S2's venue enriching
    the arXiv hit; a distinct S2-only (DOI) paper is kept."""
    arxiv = [{"arxiv_id": "2401.0001", "title": "Shared", "summary": "short"}]
    s2hits = [{"arxiv_id": "2401.0001", "title": "Shared", "summary": "a much longer abstract",
               "venue": "CVPR", "citation_count": 9, "pdf_url": "https://x/p.pdf"},
              {"arxiv_id": None, "doi": "10.1/y", "title": "S2 only", "summary": "abs",
               "venue": "NeurIPS"}]
    out = discover._merge_candidates(arxiv, s2hits)
    assert len(out) == 2                               # deduped by arXiv id
    shared = next(c for c in out if c.get("arxiv_id") == "2401.0001")
    assert shared["venue"] == "CVPR" and shared["pdf_url"]      # enriched from S2
    assert shared["summary"] == "a much longer abstract"       # longer abstract wins
    assert any(c.get("doi") == "10.1/y" for c in out)          # S2-only kept


def test_find_related_unions_arxiv_and_scholar(monkeypatch):
    """Both sources are queried and merged; an S2-only (peer-reviewed) paper survives."""
    calls = {"n": 0}
    def complete(messages, model=None):
        calls["n"] += 1
        return "query" if calls["n"] == 1 else '{"picks":[{"index":0,"note":"a"},{"index":1,"note":"b"}]}'
    monkeypatch.setattr(discover.llm, "complete", complete)
    monkeypatch.setattr(discover, "_arxiv_search",
                        lambda q, max_results=20: [{"arxiv_id": "2401.0001", "title": "Pre", "summary": "s"}])
    monkeypatch.setattr(s2, "search", lambda q, max_results=20: [
        {"arxiv_id": None, "doi": "10.1/z", "title": "Venue Paper", "summary": "abs",
         "authors": "A", "venue": "ICCV", "citation_count": 12, "pdf_url": "https://x/z.pdf"}])
    out = discover.find_related_papers("seed", intent="related", limit=5)
    titles = {c["title"] for c in out}
    assert "Pre" in titles and "Venue Paper" in titles         # union of both sources
    venue_paper = next(c for c in out if c["title"] == "Venue Paper")
    assert venue_paper["venue"] == "ICCV" and venue_paper["doi"] == "10.1/z"


def test_mcp_scholar_search_tool(monkeypatch):
    import app.mcp_server as mcp
    monkeypatch.setattr(s2, "search", lambda q, max_results=10: [
        {"s2_id": "abc123", "title": "CVPR Paper", "summary": "x" * 500, "venue": "CVPR",
         "year": "2024", "citation_count": 7}])
    out = mcp._call_tool("vlms", "scholar_search", {"query": "long video", "max_results": 5})
    assert out["count"] == 1
    r = out["results"][0]
    assert r["s2_id"] == "abc123" and r["venue"] == "CVPR" and r["citations"] == 7
    assert len(r["summary"]) <= mcp.PREVIEW + 1                # previewed, not full


def test_fetch_batch_resolves_ids(monkeypatch):
    monkeypatch.setattr(s2, "load_config", lambda: {})
    payload = [{"paperId": "p1", "title": "Batch Paper", "abstract": "an abstract",
                "year": 2025, "venue": "ECCV", "citationCount": 3, "externalIds": {"DOI": "10.1/b"},
                "openAccessPdf": {"url": "https://x/b.pdf"}, "authors": [{"name": "C"}]}, None]
    monkeypatch.setattr(s2.httpx, "post", lambda *a, **k: _Resp(200, payload))
    out = s2.fetch_batch(["p1", "p2"])
    assert "p1" in out and out["p1"]["venue"] == "ECCV" and out["p1"]["doi"] == "10.1/b"
    assert "p2" not in out                              # null entry dropped


def test_find_related_falls_back_to_s2_when_arxiv_down(monkeypatch):
    """When arXiv (_arxiv_search) raises, find_related_papers uses Semantic Scholar."""
    monkeypatch.setattr(discover.llm, "complete", lambda *a, **k: '{"picks":[{"index":0,"note":"fits"}]}'
                        if False else "long video vlm")
    # query-build returns a string; pick-step returns JSON. Multiplex by call content.
    calls = {"n": 0}
    def complete(messages, model=None):
        calls["n"] += 1
        return "long video vlm" if calls["n"] == 1 else '{"picks":[{"index":0,"note":"fits"}]}'
    monkeypatch.setattr(discover.llm, "complete", complete)
    def boom(*a, **k):
        raise discover.ArxivError("429")
    monkeypatch.setattr(discover, "_arxiv_search", boom)
    monkeypatch.setattr(s2, "search", lambda q, max_results=20: [
        {"arxiv_id": None, "doi": "10.1/x", "title": "S2 Paper", "summary": "abs",
         "authors": "A", "venue": "CVPR", "citation_count": 5, "pdf_url": "https://s2/x.pdf"}])
    out = discover.find_related_papers("seed", intent="related", limit=5)
    assert out and out[0]["doi"] == "10.1/x" and out[0]["pdf_url"]
