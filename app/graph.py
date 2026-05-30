"""Knowledge-graph relevance + insight engine over the cognitive-model wiki.

A **purely structural** graph — NO embeddings / vector store (CLAUDE.md). Nodes
are the addressable entities of the wiki: papers, concepts, beliefs, problems,
methods. Edges are the relationships we already compute deterministically:
  * membership   — a concept/problem/method/belief ↔ the papers it covers
  * link         — an explicit entity→entity tie (e.g. belief → related concept)
Relatedness between two nodes adapts nashsu/llm_wiki's structural 4-signal model
(source overlap / shared neighbors / type affinity) — structural only, so it
needs no embeddings and stays honest (it never invents a relationship the data
doesn't already encode).

This module is pure: ``build_graph`` takes already-resolved node/entity data and
the higher layers (wiki.build_collection_graph) feed it. That keeps the graph
math unit-testable with synthetic data and free of any wiki/DB import.
"""
from __future__ import annotations

import math
from collections import defaultdict

# Edge weights for relatedness scoring (adapted from nashsu/llm_wiki).
W_SOURCE = 4.0      # shared papers (source overlap) — the strongest structural signal
W_LINK = 3.0        # an explicit entity↔entity edge between the two nodes
W_NEIGHBOR = 1.5    # Adamic-Adar over shared neighbors
# typeAffinity is a MULTIPLIER on the structural score (never an additive
# baseline) so two nodes with zero structural overlap score 0 — no "related to
# everything via type" noise.
TYPE_AFFINITY: dict[str, dict[str, float]] = {
    "paper":   {"paper": 0.6, "concept": 1.2, "problem": 1.2, "method": 1.2, "belief": 1.1},
    "concept": {"paper": 1.2, "concept": 1.0, "problem": 1.1, "method": 1.1, "belief": 1.2},
    "problem": {"paper": 1.2, "concept": 1.1, "problem": 0.9, "method": 1.2, "belief": 1.1},
    "method":  {"paper": 1.2, "concept": 1.1, "problem": 1.2, "method": 0.9, "belief": 1.0},
    "belief":  {"paper": 1.1, "concept": 1.2, "problem": 1.1, "method": 1.0, "belief": 0.8},
}

# Membership edge weight (entity ↔ its papers).
_W_MEMBER = 4.0

# How many shared papers two entities need before "co-occurrence" is an insight.
_CO_OCCUR_FLOOR = 2


def build_graph(papers: list[dict], entities: list[dict]) -> dict:
    """Assemble the weighted undirected graph.

    ``papers``   : ``[{id:int, title:str}]``
    ``entities`` : ``[{key:str, kind:str, label:str, paper_ids:Iterable[int],
                       links:Iterable[str]}]`` — key is a unique node id like
                   ``"concept:semantic-anchors"``; links are other entity keys.

    Returns ``{nodes, adj}`` where nodes maps id→node and adj is a symmetric
    ``{id: {neighbor_id: weight}}``."""
    nodes: dict[str, dict] = {}
    for p in papers:
        nid = f"paper:{p['id']}"
        nodes[nid] = {"id": nid, "kind": "paper", "label": p.get("title", ""),
                      "papers": {p["id"]}}
    for e in entities:
        nodes[e["key"]] = {
            "id": e["key"], "kind": e["kind"], "label": e.get("label", ""),
            "papers": {pid for pid in (e.get("paper_ids") or [])},
            "links": {ln for ln in (e.get("links") or [])},
        }

    adj: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in entities:
        ek = e["key"]
        if ek not in nodes:
            continue
        for pid in nodes[ek]["papers"]:
            pn = f"paper:{pid}"
            if pn in nodes:
                adj[ek][pn] += _W_MEMBER
                adj[pn][ek] += _W_MEMBER
        for ln in nodes[ek]["links"]:
            if ln in nodes and ln != ek:
                adj[ek][ln] += W_LINK
                adj[ln][ek] += W_LINK
    return {"nodes": nodes, "adj": {k: dict(v) for k, v in adj.items()}}


def _degrees(graph: dict) -> dict[str, int]:
    adj = graph["adj"]
    return {n: len(adj.get(n, {})) for n in graph["nodes"]}


def related(graph: dict, node_id: str, k: int = 5) -> list[tuple[str, float]]:
    """Top-``k`` nodes most related to ``node_id`` by the structural 4-signal
    score. Nodes with no structural overlap (no shared papers, no direct edge,
    no shared neighbors) are excluded — type affinity alone never qualifies."""
    nodes, adj = graph["nodes"], graph["adj"]
    if node_id not in nodes:
        return []
    a = nodes[node_id]
    a_papers, a_nbrs = a["papers"], set(adj.get(node_id, {}))
    deg = _degrees(graph)
    scored: list[tuple[str, float]] = []
    for bid, b in nodes.items():
        if bid == node_id:
            continue
        src = len(a_papers & b["papers"])
        direct = 1.0 if bid in a_nbrs else 0.0
        shared = a_nbrs & set(adj.get(bid, {}))
        aa = sum(1.0 / math.log(deg[n] + 1.0001) for n in shared if deg.get(n, 0) > 0)
        structural = W_SOURCE * src + W_LINK * direct + W_NEIGHBOR * aa
        if structural <= 0:
            continue  # no real link → not related (type affinity can't conjure one)
        ta = TYPE_AFFINITY.get(a["kind"], {}).get(b["kind"], 1.0)
        scored.append((bid, structural * ta))
    # Sort by score desc, tie-break by node id for determinism.
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:k]


def clusters(graph: dict) -> list[list[str]]:
    """Deterministic label-propagation communities ("themes"). Each node adopts
    the highest-weighted label among its neighbors; ties break by label id;
    nodes iterate in sorted order so the result is stable across runs. Returns
    clusters of ≥2 nodes (singletons aren't themes), each sorted, outer list
    ordered by size desc then first member."""
    adj = graph["adj"]
    order = sorted(graph["nodes"])
    label = {n: n for n in order}
    for _ in range(50):
        changed = False
        for n in order:
            nbrs = adj.get(n, {})
            if not nbrs:
                continue
            tally: dict[str, float] = defaultdict(float)
            for m, w in nbrs.items():
                tally[label[m]] += w
            # Highest weight; tie-break by label id for determinism.
            best = min(tally, key=lambda lab: (-tally[lab], lab))
            if label[n] != best:
                label[n] = best
                changed = True
        if not changed:
            break
    groups: dict[str, list[str]] = defaultdict(list)
    for n, lab in label.items():
        groups[lab].append(n)
    out = [sorted(v) for v in groups.values() if len(v) > 1]
    out.sort(key=lambda g: (-len(g), g[0]))
    return out


def insights(graph: dict) -> dict:
    """Surface three structural observations, all honest (derived, not inferred):

      * ``orphans``       — paper nodes tied to no concept/problem/method/belief
                            (evidence not yet connected to the field model).
      * ``co_occurrences``— non-paper entity pairs sharing ≥ _CO_OCCUR_FLOOR
                            papers (they keep showing up together).
      * ``bridges``       — nodes that touch ≥2 distinct clusters (they connect
                            otherwise-separate themes).
    """
    nodes, adj = graph["nodes"], graph["adj"]

    orphans = [nid for nid, n in nodes.items()
               if n["kind"] == "paper" and not adj.get(nid)]

    ents = [nid for nid, n in nodes.items() if n["kind"] != "paper"]
    co: list[tuple[str, str, int]] = []
    for i, a in enumerate(ents):
        for b in ents[i + 1:]:
            shared = len(nodes[a]["papers"] & nodes[b]["papers"])
            if shared >= _CO_OCCUR_FLOOR:
                co.append((a, b, shared))
    co.sort(key=lambda x: (-x[2], x[0], x[1]))

    cluster_of: dict[str, int] = {}
    for ci, members in enumerate(clusters(graph)):
        for m in members:
            cluster_of[m] = ci
    bridges = []
    for nid in nodes:
        touched = {cluster_of[m] for m in adj.get(nid, {}) if m in cluster_of}
        if len(touched) >= 2:
            bridges.append((nid, sorted(touched)))
    bridges.sort(key=lambda x: (-len(x[1]), x[0]))

    return {"orphans": sorted(orphans), "co_occurrences": co, "bridges": bridges}
