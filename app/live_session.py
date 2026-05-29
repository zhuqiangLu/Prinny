"""Live (persistent-process) chat sessions (PAPER_CHAT_AGENT Phase C).

The alternative to resume mode: keep ONE long-lived `claude` process per paper thread
(stream-json input/output) and feed it each turn over stdin — no per-turn spawn cost.
Trade-off vs resume: faster first token, but the conversation lives only in the running
process (lost on idle-eviction or app restart), whereas resume persists to disk. The
displayed history is still stored in the DB either way.

A small registry caps live processes and reaps idle/dead ones; one turn at a time per
session (a lock). ``shutdown_all`` is wired to process exit so we don't orphan children.
"""
from __future__ import annotations

import atexit
import json
import logging
import subprocess
import threading
import time

from .engine import claude_turn_events

logger = logging.getLogger("paper_agent.live")

IDLE_TTL = 1800        # evict a live session after 30 min idle
MAX_LIVE = 4           # cap concurrent live processes (LRU-evict beyond this)

_SESSIONS: dict[int, "LiveSession"] = {}
_REG = threading.Lock()


class LiveSession:
    def __init__(self, thread_id: int, argv: list[str], cwd: str | None):
        self.thread_id = thread_id
        self.last_used = time.time()
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=cwd,
        )

    def alive(self) -> bool:
        return self.proc.poll() is None

    def turn(self, user_text: str):
        """Feed one user turn; yield {status|token|done|error} until the turn's result."""
        with self.lock:
            if not self.alive():
                yield {"type": "error", "text": "live session ended; please resend"}
                return
            self.last_used = time.time()
            msg = {"type": "user", "message": {"role": "user",
                   "content": [{"type": "text", "text": user_text}]}}
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                yield {"type": "error", "text": f"live session write failed: {exc}"}
                return
            got_done = False
            for ev in claude_turn_events(self.proc.stdout):
                got_done = got_done or ev.get("type") == "done"
                yield ev
            self.last_used = time.time()
            if not got_done:   # stream ended without a result → the process died
                yield {"type": "error", "text": "live session ended mid-turn; please resend"}

    def close(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.kill()


def _reap_locked() -> None:
    now = time.time()
    for tid, s in list(_SESSIONS.items()):
        if not s.alive() or now - s.last_used > IDLE_TTL:
            s.close()
            _SESSIONS.pop(tid, None)


def get_or_spawn(thread_id: int, argv: list[str], cwd: str | None) -> LiveSession:
    """Return the thread's live session, spawning (or replacing a dead one) as needed."""
    with _REG:
        _reap_locked()
        s = _SESSIONS.get(thread_id)
        if s and s.alive():
            return s
        if s:
            s.close()
            _SESSIONS.pop(thread_id, None)
        while len(_SESSIONS) >= MAX_LIVE:   # evict least-recently-used
            lru = min(_SESSIONS.values(), key=lambda x: x.last_used)
            lru.close()
            _SESSIONS.pop(lru.thread_id, None)
        s = LiveSession(thread_id, argv, cwd)
        _SESSIONS[thread_id] = s
        logger.info("live session spawned thread=%s (live=%d)", thread_id, len(_SESSIONS))
        return s


def drop(thread_id: int) -> None:
    """Close a thread's live session (e.g. on 'new chat'/delete)."""
    with _REG:
        s = _SESSIONS.pop(thread_id, None)
        if s:
            s.close()


def shutdown_all() -> None:
    with _REG:
        for s in _SESSIONS.values():
            s.close()
        _SESSIONS.clear()


atexit.register(shutdown_all)
