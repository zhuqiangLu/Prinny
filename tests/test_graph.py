"""Pure structural knowledge-graph engine (app/graph.py).

Synthetic node/entity fixtures only — no wiki/DB. Verifies the 4-signal
relatedness, deterministic clustering, and the insight detectors.
"""
from __future__ import annotations

import app.graph as graph


def _toy():
    """A small graph:
      papers 1,2,3,4
      concept:anchors  -> papers 1,2     concept:memory -> papers 2,3
      method:pruning   -> papers 1,2     belief:b1 -> paper 1, link concept:anchors
      paper 4 is an orphan (no entity touches it).
    """
    papers = [{"id": i, "title": f"P{i}"} for i in (1, 2, 3, 4)]
    entities = [
        {"key": "concept:anchors", "kind": "concept", "label": "Anchors", "paper_ids": [1, 2]},
        {"key": "concept:memory", "kind": "concept", "label": "Memory", "paper_ids": [2, 3]},
        {"key": "method:pruning", "kind": "method", "label": "Pruning", "paper_ids": [1, 2]},
        {"key": "belief:b1", "kind": "belief", "label": "B1", "paper_ids": [1],
         "links": ["concept:anchors"]},
    ]
    return graph.build_graph(papers, entities)


def test_build_graph_membership_and_link_edges():
    g = _toy()
    # concept:anchors is adjacent to its two papers...
    assert "paper:1" in g["adj"]["concept:anchors"]
    assert "paper:2" in g["adj"]["concept:anchors"]
    # ...and edges are symmetric.
    assert "concept:anchors" in g["adj"]["paper:1"]
    # explicit belief→concept link edge exists both ways.
    assert "concept:anchors" in g["adj"]["belief:b1"]
    assert "belief:b1" in g["adj"]["concept:anchors"]
    # the orphan paper has no adjacency.
    assert "paper:4" not in g["adj"]


def test_related_uses_shared_papers_and_excludes_unrelated():
    g = _toy()
    rel = dict(graph.related(g, "concept:anchors"))
    # anchors shares papers {1,2}; method:pruning shares both → strongest non-paper.
    assert "method:pruning" in rel
    # concept:memory shares only paper 2 → related but weaker than pruning.
    assert rel.get("method:pruning", 0) > rel.get("concept:memory", 0)
    # the orphan paper shares nothing with anchors → excluded.
    assert "paper:4" not in rel


def test_related_unknown_node_is_empty():
    assert graph.related(_toy(), "concept:nope") == []


def test_clusters_are_deterministic_and_drop_singletons():
    g = _toy()
    c1 = graph.clusters(g)
    c2 = graph.clusters(g)
    assert c1 == c2                       # deterministic
    # papers 1,2 + anchors + pruning are densely tied → land together.
    big = max(c1, key=len)
    assert "concept:anchors" in big and "method:pruning" in big
    # every cluster has ≥2 members (no singletons).
    assert all(len(c) >= 2 for c in c1)
    # the orphan paper 4 is in no cluster.
    assert all("paper:4" not in c for c in c1)


def test_insights_orphans_and_cooccurrence():
    g = _toy()
    ins = graph.insights(g)
    # paper 4 is the orphan.
    assert ins["orphans"] == ["paper:4"]
    # anchors {1,2} & pruning {1,2} share 2 papers → a co-occurrence.
    pairs = {(a, b) for a, b, _ in ins["co_occurrences"]}
    assert ("concept:anchors", "method:pruning") in pairs or \
           ("method:pruning", "concept:anchors") in pairs
    # the shared-count is reported and ≥ the floor.
    assert all(n >= graph._CO_OCCUR_FLOOR for _, _, n in ins["co_occurrences"])


def test_empty_graph_is_safe():
    g = graph.build_graph([], [])
    assert graph.clusters(g) == []
    assert graph.related(g, "x") == []
    assert graph.insights(g) == {"orphans": [], "co_occurrences": [], "bridges": []}
