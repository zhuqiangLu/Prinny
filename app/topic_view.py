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
import json

from . import agent_skills, graph as _graph, i18n, llm, topics
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


# ---- agent section editor for the topic Question/Description -----------------
# Same safe loop as the wiki thesis editor: one-shot completion (no tools),
# validators clamp it, UI diffs it, only Apply writes (with one-step undo).
_BASICS_UNDO: dict[str, dict] = {}


def propose_basics_edit(slug: str, instruction: str) -> dict:
    """One LLM call → a revised question/description from the instruction.
    Returns ``{ok, error, current, proposed}``; writes nothing."""
    t = topics.get_topic(slug)
    if not t:
        return {"ok": False, "error": "Topic not found."}
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False, "error": "Tell the agent what to change."}
    cur = {"question": t["question"], "description": t.get("description", "")}
    system = (agent_skills.skill_body("section-edit")
              or "Revise the section's JSON per the instruction; same keys; change only "
                 "what's asked; invent nothing. Output JSON only.")
    user = ("SECTION: Research Topic question\n"
            "SHAPE (return exactly these keys): {question, description}\n\n"
            "CURRENT CONTENT (JSON):\n" + json.dumps(cur, ensure_ascii=False, indent=2) + "\n\n"
            "USER INSTRUCTION:\n" + instruction + "\n")
    try:
        data = _extract_json(llm.complete([{"role": "system", "content": system},
                                           {"role": "user", "content": user}])) or {}
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "The LLM call failed."}
    q = (data.get("question") or "").strip()
    if not q:
        return {"ok": False, "error": "The agent dropped the question."}
    proposed = {"question": q[:400], "description": (data.get("description") or "").strip()[:1000]}
    return {"ok": True, "current": cur, "proposed": proposed}


def apply_basics_edit(slug: str, question: str, description: str) -> dict:
    t = topics.get_topic(slug)
    if not t:
        return {"ok": False, "error": "Topic not found."}
    if not (question or "").strip():
        return {"ok": False, "error": "The question can't be empty."}
    _BASICS_UNDO[slug] = {"question": t["question"], "description": t.get("description", "")}
    topics.update_basics(slug, question=question, description=description)
    topics.log_event(slug, "edited", "question/description via agent")
    return {"ok": True}


def has_basics_undo(slug: str) -> bool:
    return slug in _BASICS_UNDO


def undo_basics_edit(slug: str) -> dict:
    prev = _BASICS_UNDO.pop(slug, None)
    if not prev:
        return {"ok": False, "error": "Nothing to undo."}
    topics.update_basics(slug, question=prev["question"], description=prev["description"])
    topics.log_event(slug, "reverted", "question/description edit undone")
    return {"ok": True}


# ---- fold a single experimental finding into the topic (propose → apply) -----
# A finding "not relevant to the current hypothesis" gets reasoned against the whole
# argument: which hypotheses change status, whether it warrants a NEW hypothesis/assumption,
# and the finding restated as the researcher's own (un-cited) evidence. Proposes a diff the
# user reviews; nothing is written until apply_finding. The pending proposal is held in
# memory keyed by slug (one at a time, latest wins) so apply needn't re-run the LLM.
_FINDING_PENDING: dict[str, dict] = {}
_FOLD_STATUS = ("supported", "mixed", "challenged", "unknown")


def propose_finding(slug: str, finding: str) -> dict:
    """One LLM call → a focused proposal for how this finding updates the topic.
    Returns ``{ok, error, finding, proposal}``; writes nothing (stashes the proposal)."""
    t = topics.get_topic(slug)
    if not t:
        return {"ok": False, "error": "Topic not found."}
    finding = (finding or "").strip()
    if len(finding) < 10:
        return {"ok": False, "error": "Describe the finding first (a sentence or two)."}

    hyps = t.get("hypotheses") or []
    hyp_lines = "\n".join(f"H{i}: {h['text']} [{h.get('status', 'unknown')}]"
                          for i, h in enumerate(hyps, 1)) or "(none yet)"
    asm_lines = "\n".join(f"- {a['text']}" for a in (t.get("assumptions") or [])) or "(none yet)"
    system = (agent_skills.skill_body("topic-fold-finding")
              or 'Output STRICT JSON: {interpretation, revise_hypotheses:[{H,new_status,why}], '
                 'new_hypotheses:[{statement,status}], new_assumptions:[".."], '
                 'evidence:[{claim,H,direction}]}. Ground everything in the finding; invent nothing.')
    system += i18n.output_directive()
    user = (f"RESEARCH QUESTION:\n{t['question']}\n\n"
            f"CURRENT ASSUMPTIONS:\n{asm_lines}\n\n"
            f"CURRENT HYPOTHESES:\n{hyp_lines}\n\n"
            f"THE FINDING (the researcher's own result):\n{finding}\n\n"
            "Work out how this finding updates the argument. Return the STRICT JSON now.")
    from .config import agent_model
    try:
        data = _extract_json(llm.complete([{"role": "system", "content": system},
                                           {"role": "user", "content": user}],
                                          model=agent_model())) or {}
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "The LLM call failed."}

    n = len(hyps)

    def _hidx(v):
        try:
            i = int(str(v).strip().lstrip("Hh#")) - 1
        except (TypeError, ValueError):
            return None
        return i if 0 <= i < n else None

    revise = []
    seen_rev = set()
    for r in (data.get("revise_hypotheses") or []):
        if not isinstance(r, dict):
            continue
        idx = _hidx(r.get("H"))
        st = (r.get("new_status") or "").strip()
        if idx is None or idx in seen_rev or st not in _FOLD_STATUS:
            continue
        if st == hyps[idx].get("status"):
            continue                                # no actual change → skip
        seen_rev.add(idx)
        revise.append({"hyp_id": hyps[idx]["id"], "h_label": f"H{idx + 1}",
                       "text": hyps[idx]["text"], "old_status": hyps[idx].get("status", "unknown"),
                       "new_status": st, "why": (r.get("why") or "").strip()[:240]})

    new_hyps = []
    for h in (data.get("new_hypotheses") or [])[:4]:
        stmt = (h.get("statement") or h.get("text") or "").strip() if isinstance(h, dict) else str(h).strip()
        if len(stmt) < 8:
            continue
        st = (h.get("status") if isinstance(h, dict) else "") or "unknown"
        new_hyps.append({"statement": stmt[:400], "status": st if st in _FOLD_STATUS else "unknown"})

    new_asm = []
    for a in (data.get("new_assumptions") or [])[:4]:
        s = (a if isinstance(a, str) else "").strip()
        if s:
            new_asm.append(s[:300])

    evidence = []
    for e in (data.get("evidence") or [])[:4]:
        if not isinstance(e, dict):
            continue
        claim = (e.get("claim") or "").strip()
        if len(claim) < 8:
            continue
        idx = _hidx(e.get("H"))
        direction = (e.get("direction") or "supporting").strip()
        evidence.append({"claim": claim[:400],
                         "hyp_id": hyps[idx]["id"] if idx is not None else None,
                         "h_label": f"H{idx + 1}" if idx is not None else None,
                         "kind": "counter" if direction == "counter" else "supporting"})

    interpretation = (data.get("interpretation") or "").strip()[:400]
    proposal = {"interpretation": interpretation, "revise": revise,
                "new_hyps": new_hyps, "new_asm": new_asm, "evidence": evidence}
    proposal["empty"] = not (revise or new_hyps or new_asm or evidence)
    _FINDING_PENDING[slug] = {"finding": finding, "proposal": proposal}
    return {"ok": True, "finding": finding, "proposal": proposal}


def apply_finding(slug: str) -> dict:
    """Apply the pending fold-in proposal: revise hypothesis statuses, add new hypotheses /
    assumptions / evidence, and record the finding as a topic note so it persists and informs
    future regenerations. Returns ``{ok, error, counts}``."""
    pending = _FINDING_PENDING.pop(slug, None)
    if not pending:
        return {"ok": False, "error": "Nothing to apply — propose a finding first."}
    p = pending["proposal"]
    counts = {"revised": 0, "hypotheses": 0, "assumptions": 0, "evidence": 0}
    for r in p.get("revise", []):
        if topics.set_hypothesis_status(slug, r["hyp_id"], r["new_status"]):
            counts["revised"] += 1
    for h in p.get("new_hyps", []):
        if topics.add_hypothesis(slug, h["statement"], status=h.get("status", "unknown")):
            counts["hypotheses"] += 1
    for a in p.get("new_asm", []):
        if topics.add_assumption(slug, a):
            counts["assumptions"] += 1
    for e in p.get("evidence", []):
        # The researcher's own data: filed as supporting/counter but UNVERIFIED (not paper-
        # grounded), which also makes it survive a regenerate via the preserve-unverified path.
        if topics.add_evidence(slug, kind=e.get("kind", "supporting"), claim=e["claim"],
                               hypothesis_id=e.get("hyp_id"), unverified=True):
            counts["evidence"] += 1
    # Persist the finding itself so it survives a regenerate and feeds the next investigation.
    topics.add_note(slug, "Finding: " + pending["finding"])
    topics.log_event(slug, "finding folded in",
                     f"{counts['revised']} revised · {counts['hypotheses']} new hyp · "
                     f"{counts['evidence']} evidence")
    return {"ok": True, "counts": counts}


def has_pending_finding(slug: str) -> bool:
    return slug in _FINDING_PENDING


# ---- v2: generate the full scientific investigation (one LLM call) ----------

_HYP_STATUS = ("supported", "mixed", "challenged", "unknown")
_INVEST_PAPERS_PER_COLLECTION = 40
_INVEST_ABSTRACT_CHARS = 320


def _topic_digest(collections: list[str]) -> tuple[str, dict]:
    """Build the generation input: each linked collection's field summary + its
    papers (cited by [ref]). Returns ``(digest, refmap)`` where refmap maps a
    paper ref → {paper_id, collection, title} (the grounding gate for evidence)."""
    from . import wiki
    blocks, refmap = [], {}
    for slug in collections:
        try:
            field = wiki._add_seed(slug)
        except Exception:  # noqa: BLE001
            field = ""
        try:
            papers = wiki._collection_abstracts(slug)[:_INVEST_PAPERS_PER_COLLECTION]
        except Exception:  # noqa: BLE001
            papers = []
        lines = [f"=== COLLECTION: {slug} ==="]
        if field:
            lines.append(field)
        if papers:
            lines.append("PAPERS (cite by the [ref] token):")
            for p in papers:
                ref = p.get("ref")
                if not ref:
                    continue
                refmap[ref] = {"paper_id": p["id"], "collection": slug, "title": p.get("title", "")}
                ab = (p.get("abstract") or "").strip()[:_INVEST_ABSTRACT_CHARS]
                lines.append(f"[{ref}] {p.get('title','')}" + (f" — {ab}" if ab else ""))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), refmap


def _findings_block(t: dict) -> str:
    """The researcher's OWN evidence: experiment results (+ any agent analysis) and topic
    notes. Fed into the investigation so hypotheses reflect what the experiments showed —
    not just what the papers say. These are first-class, un-cited evidence (the user's data)."""
    lines = []
    for x in (t.get("experiments") or []):
        res = (x.get("result") or "").strip()
        if not res:
            continue
        head = x.get("title", "").strip() or "experiment"
        meta = " · ".join(p for p in (x.get("method", "").strip(), x.get("metric", "").strip()) if p)
        entry = f"- EXPERIMENT: {head}" + (f" ({meta})" if meta else "") + f"\n  RESULT: {res[:600]}"
        an = (x.get("analysis") or "").strip()
        if an:
            entry += f"\n  MY READING: {an[:400]}"
        lines.append(entry)
    for n in (t.get("notes") or [])[:10]:
        body = (n.get("body") or "").strip()
        if body:
            lines.append(f"- NOTE: {body[:500]}")
    return "\n".join(lines)


def _confidence(hypotheses: list[dict]) -> dict | None:
    """Deterministic overall confidence from the agent-assigned hypothesis
    statuses: supported=1, mixed=.5, challenged=.15, unknown ignored. Mean →
    {score, label}. None when there's nothing decided yet."""
    w = {"supported": 1.0, "mixed": 0.5, "challenged": 0.15}
    vals = [w[h["status"]] for h in hypotheses if h.get("status") in w]
    if not vals:
        return None
    score = round(sum(vals) / len(vals), 2)
    label = "High" if score >= 0.66 else ("Medium" if score >= 0.4 else "Low")
    return {"score": score, "label": label}


# ---- async generation job (mirrors wiki's start_draft_async) -----------------
# In-memory only: a uvicorn restart wipes any in-flight job (the thread is gone
# too), which is honest — the status endpoint then returns 'idle' and the user
# re-runs. Generation is one opaque LLM call, so stages are coarse (no fake %).
import threading

_GEN_JOBS: dict[str, dict] = {}
_GEN_LOCK = threading.Lock()
_GEN_STAGES = {
    "gathering": "Reading your collections…",
    "drafting":  "Drafting the argument…",
    "writing":   "Grounding the evidence & writing…",
    "done":      "Done.",
    "failed":    "Generation failed.",
}


def _set_gen(slug: str, **kw) -> None:
    with _GEN_LOCK:
        job = _GEN_JOBS.get(slug, {})
        job.update(kw)
        _GEN_JOBS[slug] = job


def get_generate_job(slug: str) -> dict | None:
    with _GEN_LOCK:
        job = _GEN_JOBS.get(slug)
        return dict(job) if job else None


def clear_generate_job(slug: str) -> None:
    with _GEN_LOCK:
        _GEN_JOBS.pop(slug, None)


def gen_stage_label(job: dict | None) -> str:
    return _GEN_STAGES.get((job or {}).get("stage", "gathering"), "Working…")


def start_generate_async(slug: str) -> bool:
    """Kick off generate_investigation on a daemon thread. Returns False if a job
    is already running for this slug. The overlay polls /generate/status."""
    existing = get_generate_job(slug)
    if existing and existing.get("status") == "running":
        return False
    t = topics.get_topic(slug)
    n_coll = len(t["collections"]) if t else 0
    _set_gen(slug, status="running", stage="gathering", started_at=_now(),
             n_collections=n_coll, error=None, finished_at=None)

    def cb(stage):
        _set_gen(slug, stage=stage)

    def runner():
        try:
            res = generate_investigation(slug, stage_cb=cb)
            from . import notify
            if res.get("ok"):
                _set_gen(slug, status="done", stage="done", finished_at=_now())
                notify.add(f"Investigation generated ({slug})", f"/t/{slug}", slug)
            else:
                _set_gen(slug, status="failed", stage="failed",
                         error=res.get("error") or "no usable output", finished_at=_now())
                notify.add(f"Investigation generation failed ({slug})", f"/t/{slug}", slug, ok=False)
        except Exception as exc:  # noqa: BLE001 - publish, don't crash the worker
            _set_gen(slug, status="failed", stage="failed", error=str(exc), finished_at=_now())
            from . import notify
            notify.add(f"Investigation generation failed ({slug})", f"/t/{slug}", slug, ok=False)

    threading.Thread(target=runner, daemon=True, name=f"topicgen-{slug}").start()
    return True


# ---- async suggested-reading job (so Find reading doesn't freeze the page) ----
_READING_JOBS: dict[str, dict] = {}
_READING_LOCK = threading.Lock()


def get_reading_job(slug: str) -> dict | None:
    with _READING_LOCK:
        j = _READING_JOBS.get(slug)
        return dict(j) if j else None


def clear_reading_job(slug: str) -> None:
    with _READING_LOCK:
        _READING_JOBS.pop(slug, None)


def start_reading_async(slug: str, purpose: str = "related", target_id=None,
                        custom: str = "", deep: bool = False, since: str = "") -> bool:
    """Run suggest_reading on a daemon thread; the overlay polls /reading/status."""
    existing = get_reading_job(slug)
    if existing and existing.get("status") == "running":
        return False
    with _READING_LOCK:
        _READING_JOBS[slug] = {"status": "running", "started_at": _now(),
                               "added": 0, "error": None}

    def runner():
        try:
            res = suggest_reading(slug, purpose=purpose, target_id=target_id,
                                  custom=custom, deep=deep, since=since)
            err = res.get("error")
            with _READING_LOCK:
                _READING_JOBS[slug] = {"status": "failed" if err else "done",
                                       "added": res.get("added", 0),
                                       "error": err, "finished_at": _now()}
            from . import notify
            if err:
                notify.add(f"Topic reading failed ({slug})", f"/t/{slug}", slug, ok=False)
            else:
                notify.add(f"Topic reading: {res.get('added', 0)} paper(s) found ({slug})",
                           f"/t/{slug}", slug)
        except Exception as exc:  # noqa: BLE001
            with _READING_LOCK:
                _READING_JOBS[slug] = {"status": "failed", "error": str(exc), "finished_at": _now()}

    threading.Thread(target=runner, daemon=True, name=f"topicread-{slug}").start()
    return True


def linked_collection_changes(slug: str) -> dict:
    """Non-destructive staleness check: which linked collections changed since this topic's
    investigation was last generated. Returns ``{stale, new_papers, collections:[{slug, name,
    new_papers, reasons}]}``. Cheap; empty when the topic was never generated."""
    from . import wiki, library
    t = topics.get_topic(slug)
    if not t:
        return {"stale": False, "new_papers": 0, "collections": []}
    gen_at = str((t.get("generated") or {}).get("generated_at") or "").replace("T", " ")[:19]
    if not gen_at:                       # never generated → nothing to be stale against yet
        return {"stale": False, "new_papers": 0, "collections": []}
    out, total_new = [], 0
    con = library.connect()
    try:
        for cs in t["collections"]:
            n_new = len(wiki._papers_added_since(cs, gen_at))
            thesis_at = (wiki._thesis_generated_at(cs) or "")[:19]
            a = con.execute("SELECT MAX(created_at) FROM annotations WHERE collection_slug=?", (cs,)).fetchone()[0]
            nt = con.execute("SELECT MAX(updated_at) FROM paper_notes WHERE collection_slug=?", (cs,)).fetchone()[0]
            sig = max([str(x)[:19].replace("T", " ") for x in (a, nt) if x] or [""])
            reasons = []
            if n_new:
                reasons.append(f"{n_new} new paper" + ("" if n_new == 1 else "s"))
            if thesis_at and thesis_at > gen_at:
                reasons.append("field model updated")
            if sig and sig > gen_at:
                reasons.append("new highlights/notes")
            if reasons:
                c = library.get_collection(cs)
                out.append({"slug": cs, "name": (c or {}).get("name", cs),
                            "new_papers": n_new, "reasons": reasons})
                total_new += n_new
    finally:
        con.close()
    return {"stale": bool(out), "new_papers": total_new, "collections": out}


def scan_new_papers_for_evidence(slug: str) -> dict:
    """Scan the linked collections' NEW papers (added since the investigation was generated)
    for evidence bearing on the CURRENT hypotheses, landing each as a pending suggestion the
    user accepts → unverified evidence. Non-destructive: hypotheses/experiments/results are
    untouched. One LLM call. Returns ``{added, scanned, error}``."""
    from . import wiki, library
    t = topics.get_topic(slug)
    if not t:
        return {"added": 0, "scanned": 0, "error": "Topic not found."}
    hyps = t["hypotheses"]
    if not hyps:
        return {"added": 0, "scanned": 0, "error": "No hypotheses yet — generate the investigation first."}
    gen_at = str((t.get("generated") or {}).get("generated_at") or "").replace("T", " ")[:19]
    if not gen_at:
        return {"added": 0, "scanned": 0, "error": "Generate the investigation first."}
    # New internal papers we can dedup cleanly on Accept (need an arXiv id or DOI so the
    # re-import resolves to the existing row instead of double-adding).
    papers = []
    for cs in t["collections"]:
        for pid in wiki._papers_added_since(cs, gen_at):
            p = library.get_paper(pid)
            if not p or not (p.get("abstract") or "").strip():
                continue
            if not (p.get("arxiv_id") or p.get("doi")):
                continue
            papers.append({"cs": cs, "title": p.get("title", ""), "abstract": p["abstract"],
                           "arxiv_id": p.get("arxiv_id") or "", "doi": p.get("doi") or "",
                           "authors": p.get("authors") or "", "pdf_url": p.get("pdf_url") or ""})
            if len(papers) >= 30:
                break
    if not papers:
        return {"added": 0, "scanned": 0, "error": None}
    hyp_lines = "\n".join(f"H{i}: {h['text']}" for i, h in enumerate(hyps))
    pap_lines = "\n\n".join(f"P{i} [{p['title']}]: {p['abstract'][:600]}" for i, p in enumerate(papers))
    system = ("You match new papers to a researcher's hypotheses. For each paper that CLEARLY "
              "bears on a hypothesis (from its abstract alone), emit a link; default to omitting "
              "a paper rather than forcing a weak one. STRICT JSON: "
              '{"links":[{"paper":0,"hyp":0,"stance":"supporting|counter",'
              '"claim":"one sentence, grounded in the abstract"}]}.')
    system += i18n.output_directive()
    user = (f"HYPOTHESES:\n{hyp_lines}\n\nNEW PAPERS:\n{pap_lines}\n\n"
            "Emit a link only where the abstract genuinely supports or counters a hypothesis.")
    try:
        data = _extract_json(llm.complete([{"role": "system", "content": system},
                                           {"role": "user", "content": user}])) or {}
    except Exception as exc:  # noqa: BLE001
        return {"added": 0, "scanned": len(papers), "error": f"scan failed: {exc}"}
    pending = topics.pending_suggestion_arxiv(slug)
    seen, added = set(), 0
    for ln in (data.get("links") or []):
        pi, hi = ln.get("paper"), ln.get("hyp")
        if not (isinstance(pi, int) and isinstance(hi, int)):
            continue
        if not (0 <= pi < len(papers) and 0 <= hi < len(hyps)):
            continue
        claim = (ln.get("claim") or "").strip()
        if not claim:
            continue
        stance = ln.get("stance") if ln.get("stance") in ("supporting", "counter") else "supporting"
        p, h = papers[pi], hyps[hi]
        key = (p["arxiv_id"] or p["doi"], hi)
        if key in seen:
            continue
        seen.add(key)
        if p["arxiv_id"] and p["arxiv_id"] in pending:
            continue
        if topics.add_suggestion(slug, arxiv_id=p["arxiv_id"], title=p["title"],
                                 authors=p["authors"], abstract=p["abstract"], note=claim,
                                 purpose="evidence", target_kind="hypothesis", target_id=h["id"],
                                 target_label=h["text"], stance=stance,
                                 doi=p["doi"], pdf_url=p["pdf_url"]):
            added += 1
    topics.log_event(slug, "scanned_new_papers",
                     f"{added} evidence candidate(s) from {len(papers)} new paper(s)")
    return {"added": added, "scanned": len(papers), "error": None}


def start_scan_async(slug: str) -> bool:
    """Run scan_new_papers_for_evidence on a daemon thread, sharing the reading job slot so
    the reading-tab overlay + suggestions tray render the result."""
    existing = get_reading_job(slug)
    if existing and existing.get("status") == "running":
        return False
    with _READING_LOCK:
        _READING_JOBS[slug] = {"status": "running", "started_at": _now(), "added": 0, "error": None}

    def runner():
        try:
            res = scan_new_papers_for_evidence(slug)
            err = res.get("error")
            with _READING_LOCK:
                _READING_JOBS[slug] = {"status": "failed" if err else "done",
                                       "added": res.get("added", 0), "error": err,
                                       "finished_at": _now()}
            from . import notify
            if err:
                notify.add(f"Topic scan failed ({slug})", f"/t/{slug}", slug, ok=False)
            else:
                notify.add(f"Topic scan: {res.get('added', 0)} evidence candidate(s) from new papers ({slug})",
                           f"/t/{slug}", slug)
        except Exception as exc:  # noqa: BLE001
            with _READING_LOCK:
                _READING_JOBS[slug] = {"status": "failed", "error": str(exc), "finished_at": _now()}

    threading.Thread(target=runner, daemon=True, name=f"topicscan-{slug}").start()
    return True


def analyze_experiment(slug: str, eid: int) -> dict:
    """One LLM call → reason whether the logged result supports/refutes the experiment's
    hypothesis, and suggest the next step / a revised plan. Stores the analysis on the
    experiment. Returns {ok, error}."""
    x = topics.get_experiment(slug, eid)
    if not x:
        return {"ok": False, "error": "Experiment not found."}
    if not (x.get("result") or "").strip():
        return {"ok": False, "error": "Log a result first, then Analyze."}
    t = topics.get_topic(slug)
    hyp = next((h for h in (t["hypotheses"] if t else []) if h.get("id") == x.get("hypothesis_id")), None)
    user = (f"RESEARCH QUESTION: {(t or {}).get('question', '')}\n\n"
            f"HYPOTHESIS UNDER TEST: {hyp['text'] if hyp else '(none linked)'}\n\n"
            f"EXPERIMENT: {x['title']}\n"
            f"Method: {x.get('method', '')}\nMetric: {x.get('metric', '')}\n\n"
            f"RESULT (logged by the researcher):\n{x['result']}\n")
    system = (agent_skills.skill_body("experiment-analyze")
              or ("Analyze whether the result supports, refutes, or is inconclusive for the "
                  "hypothesis. Give: a one-line **Verdict**, brief **Reasoning** grounded in the "
                  "result, and a **Next step** (a revised experiment or follow-up). Markdown only; "
                  "no invented numbers."))
    system += i18n.output_directive()
    try:
        out = llm.complete([{"role": "system", "content": system},
                            {"role": "user", "content": user}])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"The LLM call failed: {exc}"}
    out = (out or "").strip()
    if not out:
        return {"ok": False, "error": "The agent produced no analysis."}
    topics.set_experiment(slug, eid, analysis=out)
    return {"ok": True}


def generate_investigation(slug: str, stage_cb=None) -> dict:
    """One LLM call → the full scientific investigation (assumptions, hypotheses
    with status/counts, supporting/counter/missing evidence, unknowns, candidate
    experiments, next steps, key terms), written directly into the topic.

    The grounding gate is enforced in code: supporting/counter evidence MUST cite
    a paper ref that exists in a linked collection; anything ungrounded is dropped
    (the agent is told to express un-citable gaps as 'missing' evidence instead).
    ``stage_cb(stage)`` is the optional progress callback (gathering/drafting/
    writing) used by start_generate_async. Returns ``{ok, error, counts}``."""
    def stage(name):
        if stage_cb:
            try:
                stage_cb(name)
            except Exception:  # noqa: BLE001
                pass

    t = topics.get_topic(slug)
    if not t:
        return {"ok": False, "error": "Topic not found."}
    if not t["collections"]:
        return {"ok": False, "error": "Link at least one collection first."}
    stage("gathering")
    digest, refmap = _topic_digest(t["collections"])
    if not digest.strip():
        return {"ok": False, "error": "Linked collections have no papers/field model yet."}

    valid_refs = "\n".join(f"- {r}" for r in list(refmap)[:120]) or "(none)"
    findings = _findings_block(t)
    user = (f"RESEARCH QUESTION:\n{t['question']}\n\n"
            + (f"DESCRIPTION:\n{t['description']}\n\n" if t.get("description") else "")
            + "LINKED COLLECTIONS (field summaries + papers):\n\n" + digest
            + (("\n\nMY OWN FINDINGS (the researcher's experiment results + notes — treat these "
                "as first-class evidence; they need NO paper citation. Let them shape the "
                "hypotheses' status and, where a finding points beyond the papers, add a new "
                "hypothesis or assumption for it):\n" + findings) if findings else "")
            + "\n\nVALID PAPER REFS (cite paper-based evidence ONLY with these exact tokens):\n"
            + valid_refs)
    stage("drafting")
    system = (agent_skills.skill_body("topic-investigate")
              or 'Output STRICT JSON: {assumptions:[".."], hypotheses:[{statement,status,'
                 'support_count,counter_count}], supporting_evidence:[{claim,paper,hypothesis}],'
                 'counter_evidence:[{claim,paper,hypothesis}], missing_evidence:[{claim,hypothesis}],'
                 'unknowns:[{question,priority,hypothesis}], experiments:[{title,method,metric,'
                 'hypothesis}], next_steps:[{title,detail}], key_terms:[".."]}.')
    system += i18n.output_directive()
    from .config import agent_model
    try:
        data = _extract_json(llm.complete([{"role": "system", "content": system},
                                           {"role": "user", "content": user}],
                                          model=agent_model())) or {}
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "The LLM call failed."}

    # --- validate + ground -------------------------------------------------
    assumptions = [str(a).strip()[:300] for a in (data.get("assumptions") or []) if str(a).strip()][:8]

    hypotheses = []
    for h in (data.get("hypotheses") or [])[:10]:
        if not isinstance(h, dict):
            continue
        text = (h.get("statement") or h.get("text") or "").strip()
        if len(text) < 8:
            continue
        status = h.get("status") if h.get("status") in _HYP_STATUS else "unknown"
        hypotheses.append({"text": text[:400], "status": status,
                           "support_count": max(0, int(h.get("support_count", 0) or 0)),
                           "counter_count": max(0, int(h.get("counter_count", 0) or 0))})

    def _hyp_index(v):
        # The agent references hypotheses by 1-based "H1"/"H2"/index or label.
        if v is None:
            return None
        s = str(v).strip().lstrip("Hh#")
        try:
            i = int(s) - 1
            return i if 0 <= i < len(hypotheses) else None
        except (TypeError, ValueError):
            return None

    evidence = []
    for kind, key in (("supporting", "supporting_evidence"), ("counter", "counter_evidence")):
        for e in (data.get(key) or []):
            if not isinstance(e, dict):
                continue
            claim = (e.get("claim") or "").strip()
            ref = (e.get("paper") or "").strip()
            info = refmap.get(ref)
            if not claim or not info:
                continue                      # ungrounded → drop (gate); agent should use 'missing'
            evidence.append({"kind": kind, "claim": claim[:400], "paper_ref": ref,
                             "paper_id": info["paper_id"], "collection": info["collection"],
                             "hyp_index": _hyp_index(e.get("hypothesis"))})
    for e in (data.get("missing_evidence") or []):
        claim = (e.get("claim") if isinstance(e, dict) else str(e)).strip() if e else ""
        if claim:
            evidence.append({"kind": "missing", "claim": claim[:400], "paper_ref": None,
                             "paper_id": None, "collection": None,
                             "hyp_index": _hyp_index(e.get("hypothesis") if isinstance(e, dict) else None)})

    unknowns = []
    for u in (data.get("unknowns") or [])[:12]:
        text = (u.get("question") or u.get("text") if isinstance(u, dict) else str(u)) or ""
        text = text.strip()
        if not text:
            continue
        pr = (u.get("priority") if isinstance(u, dict) else "") or "medium"
        unknowns.append({"text": text[:300], "priority": pr if pr in ("high", "medium", "low") else "medium",
                         "hyp_index": _hyp_index(u.get("hypothesis") if isinstance(u, dict) else None)})

    experiments = []
    for x in (data.get("experiments") or [])[:8]:
        if not isinstance(x, dict):
            continue
        title = (x.get("title") or "").strip()
        if not title:
            continue
        experiments.append({"title": title[:200], "method": (x.get("method") or "").strip()[:300],
                            "metric": (x.get("metric") or "").strip()[:120], "status": "planned",
                            "hyp_index": _hyp_index(x.get("hypothesis"))})

    next_steps = []
    for n in (data.get("next_steps") or [])[:6]:
        if isinstance(n, dict) and (n.get("title") or "").strip():
            next_steps.append({"title": n["title"].strip()[:160],
                               "detail": (n.get("detail") or "").strip()[:200]})
        elif isinstance(n, str) and n.strip():
            next_steps.append({"title": n.strip()[:160], "detail": ""})
    key_terms = [str(k).strip()[:40] for k in (data.get("key_terms") or []) if str(k).strip()][:12]

    if not hypotheses and not assumptions:
        return {"ok": False, "error": "The agent produced no usable investigation."}

    stage("writing")
    generated = {"next_steps": next_steps, "key_terms": key_terms,
                 "confidence": _confidence(hypotheses), "generated_at": _now()}
    topics.replace_investigation(slug, assumptions=assumptions, hypotheses=hypotheses,
                                 evidence=evidence, unknowns=unknowns,
                                 experiments=experiments, generated=generated)
    counts = {"assumptions": len(assumptions), "hypotheses": len(hypotheses),
              "evidence": len([e for e in evidence if e["kind"] != "missing"]),
              "missing": len([e for e in evidence if e["kind"] == "missing"]),
              "unknowns": len(unknowns), "experiments": len(experiments)}
    topics.log_event(slug, "generated",
                     f"{counts['hypotheses']} hypotheses · {counts['evidence']} evidence · "
                     f"{counts['unknowns']} unknowns")
    return {"ok": True, "counts": counts}


def _now() -> str:
    from .wiki import _now as wnow
    return wnow()


# ---- suggested reading: purpose-driven external (arXiv) discovery ------------

TOPIC_READING_PURPOSES = ("missing", "challenge", "support", "unknown", "broaden", "related", "custom")


def _topic_reading_focus(t: dict) -> str:
    """Free-text focus seeded from the inquiry + a thin slice of linked-collection
    concept vocabulary (decision: inquiry-driven, lightly grounded)."""
    from . import wiki
    parts = [f"RESEARCH QUESTION: {t['question']}"]
    if t.get("description"):
        parts.append(t["description"])
    names: list[str] = []
    for cs in t["collections"]:
        names += wiki._concept_names(cs)
    names = list(dict.fromkeys(names))[:20]
    if names:
        parts.append("Collection concepts: " + ", ".join(names))
    return "\n".join(parts)


def recommend_collection(slug: str, title: str, abstract: str) -> str:
    """Best-fit linked collection for a candidate paper: the one whose field-model
    concept names most overlap the paper's title+abstract (deterministic, no LLM).
    Falls back to the first linked collection. '' if none linked."""
    from . import wiki
    t = topics.get_topic(slug)
    linked = (t["collections"] if t else []) or []
    if not linked:
        return ""
    text = f"{title} {abstract}".lower()
    best, best_score = linked[0], -1
    for cs in linked:
        score = sum(1 for n in wiki._concept_names(cs) if n and n.lower() in text)
        if score > best_score:
            best, best_score = cs, score
    return best


def suggest_reading(slug: str, purpose: str = "broaden", target_id=None,
                    custom: str = "", deep: bool = False, since: str = "") -> dict:
    """Purpose-driven arXiv discovery for a topic. Stores candidates in
    topic_suggestions (pending), tagged with the target so Accept can link them.
    Returns ``{added, error}``."""
    from . import library, discover
    from .config import load_config
    t = topics.get_topic(slug)
    if not t:
        return {"added": 0, "error": "Topic not found."}
    if not t["collections"]:
        return {"added": 0, "error": "Link at least one collection first."}
    if purpose not in TOPIC_READING_PURPOSES:
        purpose = "broaden"
    focus = _topic_reading_focus(t)
    target_kind = target_label = stance = intent = ""
    tid_out = None

    if purpose in ("challenge", "support"):
        h = next((x for x in t["hypotheses"] if x["id"] == target_id), None)
        if not h:
            return {"added": 0, "error": "Pick a hypothesis to target."}
        target_kind, target_label, tid_out = "hypothesis", h["text"], h["id"]
        stance = "counter" if purpose == "challenge" else "supporting"
        verb = "challenge or provide counter-evidence to" if purpose == "challenge" else "support or provide evidence for"
        intent = f"{verb} the claim: “{h['text']}”"
        focus += f"\n\nHYPOTHESIS: {h['text']}"
    elif purpose == "unknown":
        u = next((x for x in t["unknowns"] if x["id"] == target_id), None)
        if not u:
            return {"added": 0, "error": "Pick an unknown to target."}
        target_kind, target_label, tid_out = "unknown", u["text"], u["id"]
        intent = f"help answer the open question: “{u['text']}”"
        focus += f"\n\nOPEN QUESTION: {u['text']}"
    elif purpose == "missing":
        target_kind = "missing"
        m = next((e for e in t["evidence"] if e["kind"] == "missing" and e["id"] == target_id), None)
        if m:
            target_label, tid_out = m["claim"], m["id"]
            intent = f"provide the missing evidence: “{m['claim']}”"
            focus += f"\n\nMISSING EVIDENCE NEEDED: {m['claim']}"
        else:
            miss = [e["claim"] for e in t["evidence"] if e["kind"] == "missing"]
            intent = "fill an evidence gap this investigation still needs"
            if miss:
                focus += "\n\nMISSING EVIDENCE: " + "; ".join(miss[:5])
    elif purpose == "custom":
        intent = (custom or "").strip() or "be worth reading for this investigation"
    else:  # broaden / related — most relevant work for the question
        intent = "be the most relevant key or recent work for this research question"
        if t["hypotheses"]:
            focus += "\n\nHYPOTHESES: " + "; ".join(h["text"] for h in t["hypotheses"][:5])

    hist = topics.reading_history(slug)          # learning: accept/reject memory
    have_titles = set(t.lower() for t in hist["accepted_titles"])   # exclude accepted (hard)
    for cs in t["collections"]:
        for p in library.list_papers(cs):
            have_titles.add((p.get("title") or "").lower())
    try:
        fast_limit = max(1, min(50, int(load_config().get("recommend_count", "15"))))
    except (TypeError, ValueError):
        fast_limit = 15
    limit = 50 if deep else fast_limit       # 🔬 Deep casts a wider net (~50)
    try:
        if deep:                                  # 🔬 Deep search: tool-using sub-agent
            from . import paper_finder
            cands = paper_finder.deep_find(t["collections"][0], focus, intent, limit=limit, since=since)
        else:
            cands = discover.find_related_papers(focus, exclude_titles=have_titles, limit=limit,
                                                 intent=intent, prefer=hist["accepted_titles"],
                                                 avoid=hist["dismissed_titles"], since=since)
        cands = discover.validate_candidates(target_label or intent, cands, intent)  # find → verify
        cands = discover.rerank_by_profile(                                          # learn → re-rank
            cands, discover.preference_profile(hist["accepted_titles"], hist["dismissed_titles"]),
            hist["dismissed_arxiv"])
    except Exception as exc:  # noqa: BLE001
        return {"added": 0, "error": f"arXiv discovery failed: {exc}"}
    pending = topics.pending_suggestion_arxiv(slug) | hist["accepted_arxiv"]
    added = 0
    batch_seen: set = set()
    for c in cands:
        aid = c.get("arxiv_id")
        # A Semantic Scholar (peer-reviewed venue) candidate may have no arXiv id — key
        # dedup on arXiv id, then DOI, then S2 id so those aren't dropped.
        key = aid or c.get("doi") or c.get("s2_id")
        if not key or (aid and aid in pending) or key in batch_seen:
            continue
        # validator-grounded justification becomes the note when it passed
        note = c.get("note", "")
        if c.get("verdict") == "pass" and c.get("justification"):
            note = c["justification"]
        if c.get("seen_before"):
            note = f"↩ seen before · {note}"
        if topics.add_suggestion(slug, arxiv_id=aid or "", title=c.get("title", ""),
                                 authors=c.get("authors", ""), abstract=c.get("summary", ""),
                                 note=note, purpose=purpose, target_kind=target_kind,
                                 target_id=tid_out, target_label=target_label, stance=stance,
                                 verdict=c.get("verdict", ""), confidence=c.get("confidence", 0),
                                 doi=c.get("doi") or "", pdf_url=c.get("pdf_url") or "",
                                 venue=c.get("venue") or "", citation_count=c.get("citation_count") or 0):
            if aid:
                pending.add(aid)
            batch_seen.add(key)
            added += 1
    topics.log_event(slug, "suggested_reading", f"{purpose}: {added} paper(s)")
    return {"added": added, "error": None}


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
    system += i18n.output_directive()
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
    if t.get("assumptions"):
        parts.append("ASSUMPTIONS:\n" + "\n".join(f"- {a['text']}" for a in t["assumptions"]))
    if t["hypotheses"]:
        parts.append("HYPOTHESES (with status):\n" + "\n".join(
            f"- [{h.get('status', 'unknown')}] {h['text']}" for h in t["hypotheses"]))
    if t.get("evidence"):
        ev = t["evidence"]
        ns = sum(1 for e in ev if e["kind"] == "supporting")
        nc = sum(1 for e in ev if e["kind"] == "counter")
        nm = sum(1 for e in ev if e["kind"] == "missing")
        parts.append(f"EVIDENCE: {ns} supporting, {nc} counter, {nm} missing (gaps).")
    if t.get("unknowns"):
        parts.append("OPEN UNKNOWNS:\n" + "\n".join(f"- {u['text']}" for u in t["unknowns"]))
    if t.get("experiments"):
        exl = []
        for x in t["experiments"]:
            line = f"- [{x.get('status', 'planned')}] {x['title']}"
            if (x.get("result") or "").strip():
                line += f" — result: {x['result'][:300]}"
            exl.append(line)
        parts.append("EXPERIMENTS (the researcher's own studies + any logged results):\n"
                     + "\n".join(exl))
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
                 "user's notes, wiki, hypotheses, or collections. "
                 "Answer conversational or general questions DIRECTLY — do NOT call tools "
                 "for greetings or meta questions. ONLY use the read tools when the "
                 "question genuinely needs specifics from the linked collections; prefer "
                 "the fewest tool calls (often zero).")
    return ([{"role": "system", "content": "\n\n".join(parts) + i18n.output_directive()}]
            + history + [{"role": "user", "content": user_msg}])


# ---- chat-driven edits to the investigation (direct-apply on a clear command) -------
# The topic chat can EDIT experiments / hypotheses / assumptions when the user gives a clear
# instruction. A structured extraction pass turns the message into concrete edits, applied
# directly (the user opted out of a review card). Guardrails live in code, not the prompt:
# a logged experiment RESULT is never touched, statuses are clamped, only given ids are valid.
# Each batch is captured for a one-click Undo.
_CHAT_EDIT_UNDO: dict[str, list] = {}
_EXP_STATUS = ("planned", "running", "done")

# Cheap gate: only run the (second) extraction LLM call when the message plausibly asks for an
# edit, or an entity chip is attached — so ordinary questions stay a single call.
_EDIT_CUES = ("set ", "mark ", "change ", "update ", "rename ", "rewrite ", "reword ",
              "make it", "make this", "status", "done", "running", "planned", "supported",
              "challenged", "mixed", "analyze", "analyse", "analysis", "method", "metric",
              "rephrase", "edit ", "title")


def chat_edit_relevant(user_text: str, ref: dict | None) -> bool:
    if ref and (ref.get("kind") in ("experiment", "hypothesis", "assumption")):
        return True
    low = (user_text or "").lower()
    return any(c in low for c in _EDIT_CUES)


def focus_entity_block(slug: str, ref: dict | None) -> str:
    """When the user attached an experiment/hypothesis/assumption chip, return a context block
    with that entity's full detail so the conversational answer can reason about it."""
    if not (ref and ref.get("kind") in ("experiment", "hypothesis", "assumption") and ref.get("key")):
        return ""
    t = topics.get_topic(slug)
    if not t:
        return ""
    try:
        key = int(ref["key"])
    except (TypeError, ValueError):
        return ""
    if ref["kind"] == "experiment":
        x = next((e for e in t["experiments"] if e["id"] == key), None)
        if not x:
            return ""
        bits = [f"EXPERIMENT IN FOCUS: {x['title']} (status: {x.get('status', 'planned')})"]
        if x.get("method"):
            bits.append(f"Method: {x['method']}")
        if x.get("metric"):
            bits.append(f"Metric: {x['metric']}")
        if (x.get("result") or "").strip():
            bits.append(f"Logged result: {x['result']}")
        if (x.get("analysis") or "").strip():
            bits.append(f"Existing analysis: {x['analysis']}")
        return "\n".join(bits)
    if ref["kind"] == "hypothesis":
        h = next((y for y in t["hypotheses"] if y["id"] == key), None)
        return f"HYPOTHESIS IN FOCUS: {h['text']} (status: {h.get('status', 'unknown')})" if h else ""
    a = next((y for y in t["assumptions"] if y["id"] == key), None)
    return f"ASSUMPTION IN FOCUS: {a['text']}" if a else ""


def apply_chat_actions(slug: str, user_text: str, answer_text: str,
                       ref: dict | None = None) -> dict:
    """Extract + DIRECTLY APPLY any clear edits the chat message asked for. Returns
    ``{changes: [human strings], any: bool}``. A logged result is never modified."""
    t = topics.get_topic(slug)
    if not t:
        return {"changes": [], "any": False}
    exps = {x["id"]: x for x in t["experiments"]}
    hyps = {h["id"]: h for h in t["hypotheses"]}
    asms = {a["id"]: a for a in t["assumptions"]}
    if not (exps or hyps or asms):
        return {"changes": [], "any": False}

    ent = []
    if exps:
        ent.append("EXPERIMENTS:\n" + "\n".join(
            f"  id={x['id']} | {x['title']} | status={x.get('status', 'planned')}"
            + (f" | method={x['method']}" if x.get("method") else "")
            + (f" | metric={x['metric']}" if x.get("metric") else "")
            + (" | (has a logged result — do NOT edit the result)" if (x.get("result") or "").strip() else "")
            for x in exps.values()))
    if hyps:
        ent.append("HYPOTHESES:\n" + "\n".join(
            f"  id={h['id']} | {h['text']} | status={h.get('status', 'unknown')}" for h in hyps.values()))
    if asms:
        ent.append("ASSUMPTIONS:\n" + "\n".join(f"  id={a['id']} | {a['text']}" for a in asms.values()))

    system = (agent_skills.skill_body("topic-chat-edit")
              or 'Extract concrete edits the user asked for. STRICT JSON: {experiments:'
                 '[{id,title?,method?,metric?,status?,analysis?,analyze?}], hypotheses:'
                 '[{id,text?,status?}], assumptions:[{id,text?}]}. Never touch a result; '
                 'empty lists if nothing is clearly requested.')
    system += i18n.output_directive()
    focus = ""
    if ref and ref.get("kind") in ("experiment", "hypothesis", "assumption") and ref.get("key"):
        focus = (f"\n\nFOCUS: the user attached {ref['kind']} id={ref['key']} — “it”/“this” in "
                 "the message refers to that entity.")
    user = ("USER MESSAGE:\n" + (user_text or "") + focus + "\n\n"
            "ASSISTANT REPLY (context for any analysis to save):\n" + (answer_text or "")[:2000]
            + "\n\nCURRENT ENTITIES (edit ONLY these ids):\n" + "\n\n".join(ent)
            + "\n\nReturn the STRICT JSON of edits the user clearly asked for.")
    from .config import agent_model
    try:
        data = _extract_json(llm.complete([{"role": "system", "content": system},
                                           {"role": "user", "content": user}],
                                          model=agent_model())) or {}
    except Exception:  # noqa: BLE001
        return {"changes": [], "any": False}

    changes, undo = [], []

    def _txt(v):
        return v.strip() if isinstance(v, str) and v.strip() else None

    for e in (data.get("experiments") or []):
        if not isinstance(e, dict):
            continue
        x = exps.get(e.get("id"))
        if not x:
            continue
        kw, before, parts = {}, {}, []
        for f in ("title", "method", "metric"):
            nv = _txt(e.get(f))
            if nv is not None and nv != (x.get(f) or ""):
                kw[f] = nv; before[f] = x.get(f, ""); parts.append(f"{f}→“{nv[:40]}”")
        st = (e.get("status") or "").strip()
        if st in _EXP_STATUS and st != x.get("status"):
            kw["status"] = st; before["status"] = x.get("status"); parts.append(f"status→{st}")
        # analysis: explicit text, or analyze:true → save the conversational answer
        an = _txt(e.get("analysis")) or (answer_text.strip() if e.get("analyze") and answer_text.strip() else None)
        if an:
            kw["analysis"] = an; before["analysis"] = x.get("analysis", ""); parts.append("analysis saved")
        if kw and topics.set_experiment(slug, x["id"], **kw):
            changes.append(f"Experiment “{x['title'][:40]}”: " + ", ".join(parts))
            undo.append(("experiment", x["id"], before))

    for h in (data.get("hypotheses") or []):
        if not isinstance(h, dict):
            continue
        y = hyps.get(h.get("id"))
        if not y:
            continue
        st = (h.get("status") or "").strip()
        txt = _txt(h.get("text"))
        if st in _HYP_STATUS and st != y.get("status"):
            if topics.set_hypothesis_status(slug, y["id"], st):
                changes.append(f"Hypothesis “{y['text'][:40]}”: status→{st}")
                undo.append(("hypothesis_status", y["id"], y.get("status", "unknown")))
        if txt and txt != y["text"] and topics.edit_hypothesis(slug, y["id"], txt):
            changes.append(f"Hypothesis reworded → “{txt[:50]}”")
            undo.append(("hypothesis_text", y["id"], y["text"]))

    for a in (data.get("assumptions") or []):
        if not isinstance(a, dict):
            continue
        z = asms.get(a.get("id"))
        txt = _txt(a.get("text")) if z else None
        if z and txt and txt != z["text"] and topics.edit_assumption(slug, z["id"], txt):
            changes.append(f"Assumption reworded → “{txt[:50]}”")
            undo.append(("assumption_text", z["id"], z["text"]))

    if undo:
        _CHAT_EDIT_UNDO[slug] = undo
        topics.log_event(slug, "chat edit", f"{len(undo)} change(s) via chat")
    return {"changes": changes, "any": bool(changes)}


def has_chat_edit_undo(slug: str) -> bool:
    return slug in _CHAT_EDIT_UNDO


def undo_chat_edit(slug: str) -> dict:
    """Revert the last batch of chat-applied edits (restore each field's prior value)."""
    batch = _CHAT_EDIT_UNDO.pop(slug, None)
    if not batch:
        return {"ok": False, "error": "Nothing to undo."}
    for kind, eid, before in batch:
        if kind == "experiment":
            topics.set_experiment(slug, eid, **{k: v for k, v in before.items()})
        elif kind == "hypothesis_status":
            topics.set_hypothesis_status(slug, eid, before)
        elif kind == "hypothesis_text":
            topics.edit_hypothesis(slug, eid, before)
        elif kind == "assumption_text":
            topics.edit_assumption(slug, eid, before)
    topics.log_event(slug, "chat edit undone", f"{len(batch)} change(s)")
    return {"ok": True, "n": len(batch)}
