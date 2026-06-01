"""Research Topics — intelligence layer (RESEARCH_TOPICS v1, slice 2+).

Builds the cross-collection view a topic needs, on top of the per-collection
structural graphs:

  * a UNION graph over the topic's linked collections (papers are global and
    shared; entities are collection-qualified so two collections never collide),
  * seed-then-structural ranking of Relevant Entities (one cached LLM call
    anchors the question to a few existing ideas + names missing ones; the
    structural graph ranks the rest — render is no-LLM),
  * Suggested Reading (linked-collection papers ranked by how many relevant
    entities they anchor — structural, with an honest 'why'),
  * the role-banded Topic graph payload,
  * agent open-question suggestions.

Topics own no papers/notes/wiki — everything here references existing knowledge.
"""
from __future__ import annotations

import hashlib

from . import agent_skills, graph as _graph, llm, topics
from .wiki import _extract_json

# ---- cross-collection union graph -------------------------------------------

def build_topic_graph(slugs: list[str]) -> dict:
    """Union the per-collection structural graphs. Papers (global ids) merge into
    one node — a paper in two linked collections becomes a natural bridge.
    Entities are qualified ``<slug>::<kind>:<eslug>`` to avoid cross-collection
    slug collisions. Each node carries its ``collection``."""
    from . import wiki
    papers: dict[int, str] = {}
    paper_coll: dict[int, str] = {}
    entities: list[dict] = []
    coll_of: dict[str, str] = {}
    for slug in slugs:
        try:
            g = wiki.build_collection_graph(slug)
        except Exception:  # noqa: BLE001 - a bad/empty collection shouldn't kill the topic
            continue
        for nid, n in g["nodes"].items():
            if n["kind"] == "paper":
                pid = int(nid.split(":", 1)[1])
                papers[pid] = n["label"]
                paper_coll.setdefault(pid, slug)
            else:
                qkey = f"{slug}::{nid}"
                coll_of[qkey] = slug
                entities.append({"key": qkey, "kind": n["kind"], "label": n["label"],
                                 "paper_ids": sorted(n["papers"]),
                                 "links": [f"{slug}::{ln}" for ln in (n.get("links") or [])]})
    tg = _graph.build_graph([{"id": pid, "title": t} for pid, t in papers.items()], entities)
    for qkey, slug in coll_of.items():
        if qkey in tg["nodes"]:
            tg["nodes"][qkey]["collection"] = slug
    for pid, slug in paper_coll.items():
        nid = f"paper:{pid}"
        if nid in tg["nodes"]:
            tg["nodes"][nid]["collection"] = slug
    return tg


def _candidates(tg: dict) -> list[dict]:
    """Entity nodes (non-paper), the pickable ideas for seeding."""
    out = [{"key": nid, "kind": n["kind"], "label": n["label"],
            "collection": n.get("collection")}
           for nid, n in tg["nodes"].items() if n["kind"] != "paper"]
    out.sort(key=lambda c: (c["kind"], c["label"]))
    return out


def _sig(question: str, hypotheses: list[str], cand_keys: list[str]) -> str:
    h = hashlib.sha1()
    h.update((question or "").encode("utf-8"))
    for x in hypotheses:
        h.update(b"\0" + x.encode("utf-8"))
    for k in sorted(cand_keys):
        h.update(b"\1" + k.encode("utf-8"))
    return h.hexdigest()[:16]


# ---- analyze: one LLM call → seeds + external ideas (cached) -----------------

def analyze(slug: str) -> dict:
    """Anchor the question to existing ideas (seeds) and name missing ones
    (external) via one LLM call; cache on the topic. Returns {seeds, external,
    error}."""
    t = topics.get_topic(slug)
    if not t:
        return {"error": "Topic not found."}
    if not t["collections"]:
        return {"error": "Link at least one collection first."}
    tg = build_topic_graph(t["collections"])
    cands = _candidates(tg)
    if not cands:
        return {"error": "Linked collections have no field model yet — open each "
                         "collection and Regenerate to extract its ideas."}
    hyps = [h["text"] for h in t["hypotheses"]]
    lines = [f"{i}. [{c['kind']}] {c['label']} (in {c['collection']})"
             for i, c in enumerate(cands)]
    user = (f"RESEARCH QUESTION:\n{t['question']}\n\n"
            + (("HYPOTHESES:\n" + "\n".join(f"- {h}" for h in hyps) + "\n\n") if hyps else "")
            + "CANDIDATE IDEAS (index. [kind] label (collection)):\n" + "\n".join(lines)
            + "\n\nPick the seed indices most central to the question, and name up to "
              "4 relevant ideas that are MISSING from the candidates.")
    system = (agent_skills.skill_body("topic-seed")
              or 'Output JSON {seeds:[{index,why}], external:[{name,relevance,reason}]}.')
    try:
        data = _extract_json(llm.complete([{"role": "system", "content": system},
                                           {"role": "user", "content": user}]))
    except Exception:  # noqa: BLE001
        return {"error": "The LLM call failed."}

    seeds, why = [], {}
    for s in (data or {}).get("seeds", []):
        try:
            i = int(s.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(cands):
            k = cands[i]["key"]
            if k not in why:
                seeds.append(k)
                why[k] = (s.get("why") or "").strip()[:200]
    seeds = seeds[:8]
    cand_labels = {c["label"].lower() for c in cands}
    external = []
    for e in (data or {}).get("external", []):
        name = (e.get("name") or "").strip()[:60]
        if not name or name.lower() in cand_labels:
            continue
        rel = e.get("relevance", "medium")
        if rel not in ("high", "medium", "low"):
            rel = "medium"
        external.append({"name": name, "relevance": rel,
                         "reason": (e.get("reason") or "").strip()[:200]})
    external = external[:4]

    sig = _sig(t["question"], hyps, [c["key"] for c in cands])
    topics.save_seed(slug, {"sig": sig, "seeds": seeds, "why": why, "external": external})
    return {"seeds": len(seeds), "external": len(external)}


# ---- relevant entities: cached seed + structural expansion (no LLM) ----------

def relevant_entities(slug: str) -> dict | None:
    """Seed entities (top) + structural expansion via graph.related, grouped by
    kind. {analyzed, stale, items, grouped, external}. None if no collections."""
    t = topics.get_topic(slug)
    if not t or not t["collections"]:
        return None
    tg = build_topic_graph(t["collections"])
    cands = _candidates(tg)
    seed = t["seed"] or {}
    cur_sig = _sig(t["question"], [h["text"] for h in t["hypotheses"]],
                   [c["key"] for c in cands])
    seeds = [k for k in seed.get("seeds", []) if k in tg["nodes"]]
    external = seed.get("external", [])
    if not seeds:
        return {"analyzed": False, "stale": bool(cands), "items": [], "grouped": {},
                "external": external, "n_candidates": len(cands)}

    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for sk in seeds:
        scores[sk] = scores.get(sk, 0) + 100.0
        reasons[sk] = (seed.get("why", {}).get(sk) or "central to the question")
        for nid, sc in _graph.related(tg, sk, k=8):
            if tg["nodes"][nid]["kind"] == "paper":
                continue
            scores[nid] = scores.get(nid, 0) + sc
            reasons.setdefault(nid, f"related to “{tg['nodes'][sk]['label']}” (shared papers)")

    items = []
    for nid, sc in sorted(scores.items(), key=lambda x: (-x[1], x[0])):
        n = tg["nodes"][nid]
        items.append({"key": nid, "kind": n["kind"], "label": n["label"],
                      "collection": n.get("collection"), "is_seed": nid in seeds,
                      "why": reasons.get(nid, ""), "score": round(sc, 1)})
    grouped = {k: [i for i in items if i["kind"] == k]
               for k in ("problem", "method", "concept", "belief")}
    return {"analyzed": True, "stale": seed.get("sig") != cur_sig, "items": items,
            "grouped": grouped, "external": external, "n_seeds": len(seeds)}


# ---- suggested reading: structural, with honest 'why' (no LLM) ---------------

def suggested_reading(slug: str, limit: int = 6) -> list[dict]:
    """Linked-collection papers ranked by how many top relevant-entities they
    anchor. The 'why' lists those ideas — grounded, not asserted."""
    rel = relevant_entities(slug)
    if not rel or not rel.get("analyzed"):
        return []
    t = topics.get_topic(slug)
    tg = build_topic_graph(t["collections"])
    top = rel["items"][:12]
    titles = {int(nid.split(":", 1)[1]): n["label"]
              for nid, n in tg["nodes"].items() if n["kind"] == "paper"}
    pcoll = {int(nid.split(":", 1)[1]): n.get("collection")
             for nid, n in tg["nodes"].items() if n["kind"] == "paper"}
    hits: dict[int, list[str]] = {}
    for it in top:
        for pid in tg["nodes"][it["key"]]["papers"]:
            hits.setdefault(pid, []).append(it["label"])
    ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]
    out = []
    for pid, labels in ranked:
        uniq = list(dict.fromkeys(labels))
        out.append({"id": pid, "collection": pcoll.get(pid),
                    "title": titles.get(pid, str(pid)),
                    "why": "Anchors " + ", ".join(uniq[:3]) + (" …" if len(uniq) > 3 else ""),
                    "n": len(uniq)})
    return out


# ---- topic graph: role-banded, honest edges only -----------------------------

def topic_graph_view(slug: str) -> dict | None:
    """Cytoscape payload: question (center) + relevant entities, ringed by
    relevance (concentric). Edges = question→seed entities (the anchor) + entity↔
    entity shared-paper links (the projection among shown entities). No fabricated
    Problem→Method flow; no paper nodes (collapsed). None until analyzed."""
    rel = relevant_entities(slug)
    if not rel or not rel.get("analyzed") or not rel["items"]:
        return None
    t = topics.get_topic(slug)
    tg = build_topic_graph(t["collections"])
    shown = rel["items"][:24]
    shown_keys = {i["key"] for i in shown}
    seed_keys = {i["key"] for i in shown if i["is_seed"]}

    nodes = [{"id": "Q", "label": t["question"], "kind": "question", "ring": 0}]
    for i in shown:
        nodes.append({"id": i["key"], "label": i["label"], "kind": i["kind"],
                      "ring": 1 if i["is_seed"] else 2})

    edges = [{"source": "Q", "target": k} for k in seed_keys]  # question → its anchors
    seen = set()
    for a in shown_keys:                                       # idea ↔ idea (shared papers)
        pa = tg["nodes"][a]["papers"]
        for b in shown_keys:
            if a >= b:
                continue
            if pa & tg["nodes"][b]["papers"]:
                key = (a, b)
                if key not in seen:
                    seen.add(key)
                    edges.append({"source": a, "target": b})
    return {"nodes": nodes, "edges": edges}


# ---- agent open-questions (one LLM call) -------------------------------------

def suggest_questions(slug: str) -> dict:
    """Draft a few open sub-questions from the topic question + its relevant
    entities; insert them as source='agent'. Returns {added, error}."""
    t = topics.get_topic(slug)
    if not t:
        return {"error": "Topic not found.", "added": 0}
    rel = relevant_entities(slug)
    ideas = ", ".join(i["label"] for i in (rel["items"][:10] if rel and rel.get("items") else []))
    existing = "\n".join(f"- {q['text']}" for q in t["questions"]) or "(none)"
    user = (f"RESEARCH QUESTION:\n{t['question']}\n\n"
            + (f"RELEVANT IDEAS: {ideas}\n\n" if ideas else "")
            + f"EXISTING OPEN QUESTIONS (don't repeat):\n{existing}\n\n"
            "Propose 3-5 sharp, specific open sub-questions this investigation must "
            "answer. STRICT JSON: {questions: [\"...\", ...]}.")
    system = ("You generate open research sub-questions. Concrete and answerable, "
              "grounded in the topic. STRICT JSON only: {questions:[\"...\"]}.")
    try:
        data = _extract_json(llm.complete([{"role": "system", "content": system},
                                           {"role": "user", "content": user}]))
    except Exception:  # noqa: BLE001
        return {"error": "The LLM call failed.", "added": 0}
    seen = {q["text"].strip().lower() for q in t["questions"]}
    added = 0
    for q in (data or {}).get("questions", [])[:5]:
        q = (q or "").strip()
        if q and q.lower() not in seen:
            if topics.add_question(slug, q, "agent"):
                seen.add(q.lower())
                added += 1
    return {"added": added}


# ---- topic assistant: chat grounded in the topic (read-only) -----------------

def chat_messages(slug: str, history: list[dict], user_msg: str) -> list[dict]:
    """Build the LLM messages for the topic assistant: a system prompt grounded
    in the topic (question, hypotheses, relevant ideas, evidence collections) +
    recent history + the user turn. Read-only — the assistant never mutates."""
    t = topics.get_topic(slug)
    rel = relevant_entities(slug)
    parts = ["You are a research assistant for an INVESTIGATION (a research topic), "
             "not a single collection. Help the researcher think about their question.",
             f"QUESTION: {t['question']}"]
    if t.get("description"):
        parts.append(f"DESCRIPTION: {t['description']}")
    if t["hypotheses"]:
        parts.append("HYPOTHESES:\n" + "\n".join(f"- {h['text']}" for h in t["hypotheses"]))
    if t["questions"]:
        parts.append("OPEN QUESTIONS:\n" + "\n".join(f"- {q['text']}" for q in t["questions"]))
    if rel and rel.get("analyzed") and rel.get("items"):
        parts.append("RELEVANT IDEAS (from the linked collections): "
                     + ", ".join(i["label"] for i in rel["items"][:15]))
        if rel.get("external"):
            parts.append("Ideas the collections DON'T cover but the question needs: "
                         + ", ".join(e["name"] for e in rel["external"]))
    if t["collections"]:
        parts.append("EVIDENCE COLLECTIONS: " + ", ".join(t["collections"]))
    parts.append("Be concise and grounded. Name the ideas/papers you reference. If the "
                 "collections don't cover something the question needs, say so honestly "
                 "rather than inventing support. You are read-only: you don't change the "
                 "user's notes, wiki, hypotheses, or collections.")
    return ([{"role": "system", "content": "\n\n".join(parts)}]
            + history + [{"role": "user", "content": user_msg}])
