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
