"""Tests for chat thread storage and context assembly.

These avoid real OpenAI calls entirely: storage is exercised directly, and
context assembly is verified against a fake Zotero backend + temp collection
files, asserting the assembled prompt is grounded in the user's artifacts.
"""

from __future__ import annotations

import app.context as context
import app.library as library
import app.repo as repo
from app.db import connect, init_db


# --- chat storage ----------------------------------------------------------
def test_thread_and_messages(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    monkeypatch.setattr(repo, "connect", lambda: connect(db))

    t1 = repo.get_or_create_thread("vlms")
    t2 = repo.get_or_create_thread("vlms")
    assert t1 == t2  # one thread per collection

    repo.add_message(t1, "user", "hi", [{"type": "paper", "id": "K1"}])
    repo.add_message(t1, "assistant", "hello")

    msgs = repo.get_messages(t1)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hi"

    # limit returns the most recent N, still chronological
    for i in range(5):
        repo.add_message(t1, "user", f"m{i}")
    last2 = repo.get_messages(t1, limit=2)
    assert [m["content"] for m in last2] == ["m3", "m4"]


def test_new_and_delete_per_paper_threads(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    init_db(db)
    monkeypatch.setattr(repo, "connect", lambda: connect(db))

    t1 = repo.get_or_create_thread("vlms", 5)      # this paper's first thread
    repo.add_message(t1, "user", "hi")
    assert repo.thread_message_count(t1) == 1

    # "New chat" → a newer empty thread becomes the active (most-recent) one
    t2 = repo.new_thread("vlms", 5)
    assert t2 != t1
    assert repo.get_or_create_thread("vlms", 5) == t2
    assert repo.thread_message_count(t2) == 0

    # "Switch" back to the older thread → it becomes active again (going back!)
    repo.touch_thread(t1)
    assert repo.get_or_create_thread("vlms", 5) == t1
    threads = repo.list_threads("vlms", 5)
    assert {x["id"] for x in threads} == {t1, t2}
    assert threads[0]["id"] == t1                 # active one listed first
    assert threads[0]["count"] == 1 and threads[1]["count"] == 0

    # "Delete chat" on the active thread → falls back to the other one
    repo.delete_thread(t1)
    assert repo.get_or_create_thread("vlms", 5) == t2

    # deleting the last remaining one → a fresh, empty thread is created on next access
    repo.delete_thread(t2)
    t3 = repo.get_or_create_thread("vlms", 5)
    assert repo.thread_message_count(t3) == 0
    # a different paper keeps its own independent thread
    assert repo.get_or_create_thread("vlms", 6) != t3


# --- context assembly ------------------------------------------------------
# Local-first: build_messages reads paper metadata from the app's own store
# (library.get_paper), not live Zotero.
def test_build_messages_grounds_in_user_artifacts(tmp_path, monkeypatch):
    # temp collection dir with purpose + a thought
    coldir = tmp_path / "collections" / "vlms"
    (coldir / "thoughts").mkdir(parents=True)
    (coldir / "purpose.md").write_text("Track efficient video VLMs.", encoding="utf-8")
    (coldir / "thoughts" / "2026-05-01T00-00-00.md").write_text(
        "I suspect KV-cache compression is the key lever.", encoding="utf-8"
    )
    monkeypatch.setattr(context, "COLLECTIONS_DIR", tmp_path / "collections")

    # isolated app.sqlite shared by every module's connect()
    db = tmp_path / "app.sqlite"
    init_db(db)
    monkeypatch.setattr(context, "connect", lambda: connect(db))
    monkeypatch.setattr(library, "connect", lambda: connect(db))
    monkeypatch.setattr("app.annotations.connect", lambda: connect(db))

    pid = library.upsert_paper(zotero_key="K1", title="StreamingVLM", authors="Xu",
                               year="2025", origin="zotero-import")

    history = [{"role": "user", "content": "earlier"}]

    # paper mode WITHOUT /collection: paper-focused, no wiki/purpose/thoughts
    msgs_paper, refs = context.build_messages(
        "vlms", "VLMs", history, "what's the gap?", paper_id=pid
    )
    joined_paper = " ".join(m["content"] for m in msgs_paper)
    assert "StreamingVLM" in joined_paper            # the paper is in context
    assert "KV-cache compression" not in joined_paper  # collection thought NOT injected
    assert {"type": "paper", "id": pid} in refs
    assert msgs_paper[-1] == {"role": "user", "content": "what's the gap?"}
    assert {"role": "user", "content": "earlier"} in msgs_paper

    # /collection command injects the collection context (purpose + thought)
    msgs_coll, _ = context.build_messages(
        "vlms", "VLMs", history, "what's the gap?", paper_id=pid,
        include_collection=True,
    )
    joined_coll = " ".join(m["content"] for m in msgs_coll)
    assert "Track efficient video VLMs." in joined_coll
    assert "KV-cache compression" in joined_coll


def test_build_messages_notes_images_text_only(tmp_path, monkeypatch):
    """CLI-only: build_messages no longer inlines images (no API vision). It keeps the
    turn as a string and appends a note; images reach the sub-agent via files instead."""
    monkeypatch.setattr(context, "COLLECTIONS_DIR", tmp_path / "collections")
    db = tmp_path / "app.sqlite"
    init_db(db)
    monkeypatch.setattr(context, "connect", lambda: connect(db))

    img = "data:image/png;base64,AAAA"
    messages, _ = context.build_messages(
        "c", "C", [], "what is this?", paper_id=None, images=[img]
    )
    content = messages[-1]["content"]
    assert isinstance(content, str)                        # text-only (no inline vision)
    assert content.startswith("what is this?")
    assert "1 image(s) attached" in content                # just a note

    # no images → plain string content (unchanged behaviour)
    messages2, _ = context.build_messages("c", "C", [], "hi", paper_id=None)
    assert messages2[-1]["content"] == "hi"


def test_build_messages_degrades_without_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "COLLECTIONS_DIR", tmp_path / "collections")
    db = tmp_path / "app.sqlite"
    init_db(db)
    monkeypatch.setattr(context, "connect", lambda: connect(db))

    messages, refs = context.build_messages(
        "new", "New", [], "hello", paper_id=None
    )
    assert refs == []
    assert "not written yet" in messages[0]["content"]
    assert messages[-1]["content"] == "hello"


def test_clear_messages_and_artifact(tmp_path, monkeypatch):
    """Compaction primitive: clear live history but keep the thread + its artifact,
    and exclude the stored system artifact from get_messages."""
    db = tmp_path / "app.sqlite"
    init_db(db)
    monkeypatch.setattr(repo, "connect", lambda: connect(db))

    tid = repo.get_or_create_thread("vlms")          # collection thread
    repo.add_message(tid, "user", "what's the gap?")
    repo.add_message(tid, "assistant", "X is unaddressed.")
    assert repo.get_artifact(tid) is None

    # "Compact": replace history with a single system artifact.
    repo.clear_messages(tid)
    repo.add_message(tid, "system", "## Summary\nGap: X is unaddressed.")

    assert repo.thread_message_count(tid) == 1        # only the artifact remains
    assert repo.get_artifact(tid) == "## Summary\nGap: X is unaddressed."
    assert repo.get_messages(tid) == []               # artifact is not "history"
    # the thread itself is preserved (same id), so the chat continues
    assert repo.get_or_create_thread("vlms") == tid

    # a later turn lives alongside the artifact
    repo.add_message(tid, "user", "and methods?")
    assert [m["content"] for m in repo.get_messages(tid)] == ["and methods?"]
    assert repo.get_artifact(tid) == "## Summary\nGap: X is unaddressed."


def test_build_messages_injects_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "COLLECTIONS_DIR", tmp_path / "collections")
    db = tmp_path / "app.sqlite"
    init_db(db)
    monkeypatch.setattr(context, "connect", lambda: connect(db))

    msgs, _ = context.build_messages(
        "c", "C", [], "follow-up", paper_id=None, artifact="Earlier: we discussed X.",
    )
    systems = [m["content"] for m in msgs if m["role"] == "system"]
    assert any("Earlier: we discussed X." in s for s in systems)
    # artifact precedes the live user turn
    assert msgs[-1] == {"role": "user", "content": "follow-up"}
