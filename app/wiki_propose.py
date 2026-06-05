"""Chat → wiki proposal engine (propose-and-gate).

The agentic chat can PROPOSE typed edits to the editable wiki sections; nothing is
written until the user Accepts (inline in the chat). This module is the gate:

  - create_proposal() validates the section/op/grounding and persists a *pending*
    row (never touches the wiki). Evidence sections (concepts, beliefs, landscape
    problems/methods) require ≥1 cited collection paper; the thesis is grounded in
    the conversation instead (the project's "evidence = papers, thinking = the
    user's words" line).
  - accept_proposal() snapshots the target section (one-step undo), applies the
    typed op, and RE-VALIDATES with the same rules the generators use (landscape
    cap, concept dedupe, belief title length) — never trusting the round-trip.
  - dismiss_proposal() drops it.

Computed sections (connections, papers) are not editable — there's no file.
"""
from __future__ import annotations

import json
import uuid

from . import library, wiki
from .db import connect

# section -> the ops it accepts
_OPS: dict[str, set[str]] = {
    "thesis": {"replace"},
    "landscape": {"add_item", "remove_item"},
    "concepts": {"add_concept"},
    "belief": {"add"},
}
_LANDSCAPE_COLUMNS = {"problems", "methods", "debates", "open_questions"}
_LANDSCAPE_EVIDENCE_COLUMNS = {"problems", "methods"}     # these cite papers
_BELIEF_CONFIDENCE = {"emerging", "medium", "uncertain"}


def _valid_refs(slug: str) -> set:
    """Paper refs the collection actually has (what a proposal may cite)."""
    try:
        return set(wiki._ref_map(slug).keys())
    except Exception:  # noqa: BLE001
        return {p.get("arxiv_id") for p in library.list_papers(slug) if p.get("arxiv_id")}


def _needs_paper(section: str, op: str, content: dict) -> bool:
    if section in ("concepts", "belief"):
        return True
    if section == "landscape" and op == "add_item":
        return (content or {}).get("column") in _LANDSCAPE_EVIDENCE_COLUMNS
    return False


def _summarize(section: str, op: str, content: dict) -> str:
    c = content or {}
    if section == "thesis":
        return "Rewrite the Collection Thesis"
    if section == "landscape":
        verb = "Add to" if op == "add_item" else "Remove from"
        return f"{verb} Landscape · {c.get('column', '?')}: “{(c.get('text') or '')[:80]}”"
    if section == "concepts":
        return f"Add concept “{c.get('name', '')}”"
    if section == "belief":
        return f"Add belief: “{(c.get('title') or '')[:90]}”"
    return f"{section}/{op}"


# --- create / read / dismiss -------------------------------------------------
def create_proposal(slug: str, section: str, op: str, content: dict | None,
                     supporting_papers: list | None = None, grounding: str = "",
                     origin: str = "proactive") -> dict:
    """Validate + persist a pending proposal. Returns {ok, id, summary} or {ok:False, error}."""
    section = (section or "").strip().lower()
    op = (op or "").strip().lower()
    content = content or {}
    if section not in _OPS or op not in _OPS[section]:
        return {"ok": False, "error": f"unknown section/op '{section}/{op}'"}

    refs = _valid_refs(slug)
    cited = [p for p in (supporting_papers or []) if p in refs]
    if _needs_paper(section, op, content) and not cited:
        return {"ok": False, "error": "this edit must cite at least one paper that's in the collection"}
    if section == "thesis":
        grounding = (grounding or "").strip() or "from the conversation"

    summary = _summarize(section, op, content)
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO wiki_proposals(collection_slug, section, op, content, "
            "supporting_papers, grounding, summary, origin) VALUES (?,?,?,?,?,?,?,?)",
            (slug, section, op, json.dumps(content), json.dumps(cited),
             grounding, summary, origin))
        con.commit()
        pid = cur.lastrowid
    finally:
        con.close()
    return {"ok": True, "id": pid, "summary": summary}


def _row(r) -> dict:
    d = dict(r)
    d["content"] = json.loads(d.get("content") or "{}")
    d["supporting_papers"] = json.loads(d.get("supporting_papers") or "[]")
    return d


def list_pending(slug: str) -> list[dict]:
    con = connect()
    try:
        rows = con.execute("SELECT * FROM wiki_proposals WHERE collection_slug=? AND "
                           "status='pending' ORDER BY id", (slug,)).fetchall()
    finally:
        con.close()
    return [_row(r) for r in rows]


def get_proposal(pid: int) -> dict | None:
    con = connect()
    try:
        r = con.execute("SELECT * FROM wiki_proposals WHERE id=?", (pid,)).fetchone()
    finally:
        con.close()
    return _row(r) if r else None


def _set_status(pid: int, status: str) -> None:
    con = connect()
    try:
        con.execute("UPDATE wiki_proposals SET status=? WHERE id=?", (status, pid))
        con.commit()
    finally:
        con.close()


def dismiss_proposal(pid: int) -> dict:
    p = get_proposal(pid)
    if not p or p["status"] != "pending":
        return {"ok": False, "error": "not a pending proposal"}
    _set_status(pid, "dismissed")
    return {"ok": True}


# --- accept (snapshot → apply → re-validate) ---------------------------------
def accept_proposal(pid: int) -> dict:
    p = get_proposal(pid)
    if not p or p["status"] != "pending":
        return {"ok": False, "error": "not a pending proposal"}
    slug, section, op, content = p["collection_slug"], p["section"], p["op"], p["content"]
    cited = p["supporting_papers"]
    if section == "thesis":
        res = wiki.apply_thesis_edit(slug, content)          # snapshots + re-validates
    elif section == "landscape":
        res = _apply_landscape(slug, op, content)
    elif section == "concepts":
        res = _apply_concept(slug, content)
    elif section == "belief":
        res = _apply_belief(slug, content, cited)
    else:
        res = {"ok": False, "error": "uneditable section"}
    if res.get("ok"):
        _set_status(pid, "accepted")
    return res


# --- per-section apply (each re-validates with the generator's rules) --------
def _history_dir(slug):
    return wiki._sections_dir(slug) / ".history"


def _snapshot_files(slug: str, *paths) -> None:
    h = _history_dir(slug)
    h.mkdir(parents=True, exist_ok=True)
    for f in paths:
        if f.is_file():
            (h / (f.name + ".bak")).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")


def _apply_landscape(slug: str, op: str, content: dict) -> dict:
    col = (content or {}).get("column")
    if col not in _LANDSCAPE_COLUMNS:
        return {"ok": False, "error": f"bad landscape column '{col}'"}
    jp = wiki._landscape_json_path(slug)
    if not jp.is_file():
        return {"ok": False, "error": "no landscape to edit yet — draft the Field Model first"}
    landscape = json.loads(jp.read_text(encoding="utf-8")).get("landscape") or {}
    items = list(landscape.get(col) or [])
    text = (content.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty item text"}
    if op == "add_item":
        if col in _LANDSCAPE_EVIDENCE_COLUMNS:
            items.append({"text": text, "papers": content.get("papers") or content.get("supporting_papers") or []})
        else:
            items.append(text)
    else:  # remove_item
        def _t(it):
            return (it.get("text") if isinstance(it, dict) else it or "").strip().lower()
        items = [it for it in items if _t(it) != text.lower()]
    landscape[col] = items
    # Re-validate exactly like the generator (caps to 6, drops too-short, refs).
    base_thesis = wiki.current_thesis(slug) or {}
    valid = wiki._validate_field_model({"thesis": base_thesis, "landscape": landscape},
                                       valid_refs=_valid_refs(slug)).get("landscape") or {}
    _snapshot_files(slug, wiki._landscape_path(slug), wiki._landscape_json_path(slug))
    wiki._write_landscape_page(slug, valid, {})
    wiki._append_log(slug, f"chat edit: landscape {op} ({col})", text[:200])
    return {"ok": True}


def _apply_concept(slug: str, content: dict) -> dict:
    name = (content.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "concept needs a name"}
    cp = wiki._concepts_path(slug)
    concepts = []
    if cp.is_file():
        concepts = json.loads(cp.read_text(encoding="utf-8")).get("concepts") or []
    new_slug = wiki._SLUG_RE.sub("-", name.lower()).strip("-")
    if any(wiki._SLUG_RE.sub("-", (c.get("name") or "").lower()).strip("-") == new_slug for c in concepts):
        return {"ok": False, "error": f"concept “{name}” already exists"}
    concepts.append({"name": name, "synonyms": content.get("synonyms") or [],
                     "blurb": (content.get("blurb") or "").strip(),
                     "papers": content.get("papers") or content.get("supporting_papers") or [],
                     "user_owned": True})   # accepted by the user → survives regenerate
    _snapshot_files(slug, wiki._concepts_path(slug))
    wiki._write_concepts_file(slug, concepts)
    wiki._append_log(slug, "chat edit: added concept", name[:200])
    return {"ok": True}


def _apply_belief(slug: str, content: dict, cited: list) -> dict:
    title = (content.get("title") or "").strip()
    if len(title) < 10:
        return {"ok": False, "error": "belief is too short to be a real claim"}
    conf = content.get("confidence")
    meta = {"id": uuid.uuid4().hex[:10], "type": "belief", "status": "accepted",
            "title": title, "confidence": conf if conf in _BELIEF_CONFIDENCE else "emerging",
            "supporting_papers": cited, "related_concepts": content.get("related_concepts") or [],
            "generated_by": "agent", "generator": "chat-propose",
            "generated_at": wiki._now(), "accepted_at": wiki._now()}
    tslug = wiki._SLUG_RE.sub("-", title.lower()).strip("-")[:60] or meta["id"]
    wiki._beliefs_dir(slug).mkdir(parents=True, exist_ok=True)
    (wiki._beliefs_dir(slug) / f"{tslug}.md").write_text(
        wiki.frontmatter.dump(meta, ""), encoding="utf-8")
    wiki._append_log(slug, "chat edit: accepted belief", title[:200])
    return {"ok": True}
