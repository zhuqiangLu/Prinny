"""The Engine seam (AGENTIC_PLAN P3) — the swappable LLM backend.

Every LLM call in the app goes through ``llm.complete``/``stream``, which delegate to
the *selected* Engine here. The backend is a CLI agent (Claude Code or Codex) driven as
a subprocess — no API, no API key. There is no API fallback: every LLM feature requires
one of these installed (FakeEngine covers tests).

The interface is intentionally narrow:
  - ``run_once(messages, ...)`` — one request → ``EngineResult`` (final text + meta).
  - ``stream(messages, ...)`` — yields text deltas (sync Iterator, matching the old
    ``llm.stream``; true CLI token streaming is wired in Phase 6).
  - ``available()`` — is this backend usable right now? (binary on PATH / key set).
  - ``models()`` — best-effort model list for the Settings picker.

``run_once`` also accepts the agentic kwargs (``cwd``, ``allowed_tools``,
``mcp_config``, ``session_id``) used by Phases 5–6; for tool-less completions they are
None and ignored. Only CLI engines honor them.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .config import APP_DIR

logger = logging.getLogger("paper_agent.engine")


def _safe_cwd(cwd: str | None) -> str | None:
    """Working dir for a CLI spawn. Defaults to APP_DIR (no CLAUDE.md/AGENTS.md there)
    rather than the inherited server cwd — otherwise every spawn would auto-load the
    repo's CLAUDE.md (the build spec) as agent context. Never use --bare: it disables
    the keychain/OAuth login we depend on (forces an API key)."""
    if cwd and Path(cwd).is_dir():
        return cwd
    return str(APP_DIR) if APP_DIR.is_dir() else None

# Hard cap on any single CLI invocation so a hung agent can't wedge a request.
CLI_TIMEOUT = 300


class EngineError(RuntimeError):
    """A backend that is misconfigured or failed (missing binary/key, bad exit)."""


@dataclass
class EngineResult:
    text: str
    session_id: str | None = None
    usage: dict | None = None              # {prompt_tokens, completion_tokens}
    events: list = field(default_factory=list)


def safe_text(s: str | None) -> str:
    """Drop lone UTF-16 surrogates (e.g. \\ud835 from a PDF's split mathematical-bold
    glyphs). They live fine in a Python str but raise 'surrogates not allowed' the
    moment we UTF-8 encode — which is exactly what feeding a subprocess stdin does.
    Strip them so a single bad character in an abstract can't kill an LLM call."""
    if not s:
        return s or ""
    return s.encode("utf-8", "ignore").decode("utf-8")


# --- message flattening (for CLI engines) ----------------------------------
def _split_system(messages: list[dict]) -> tuple[str, str]:
    """Flatten a role/content messages list into (system_text, prompt_text) for a
    CLI agent: system messages concatenate into the system prompt; the rest become a
    role-labeled transcript (single-turn calls collapse to just the user content)."""
    systems = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
    rest = [m for m in messages if m.get("role") != "system"]
    if len(rest) == 1:
        prompt = rest[0].get("content", "") or ""
    else:
        prompt = "\n\n".join(f"{m.get('role', 'user').upper()}: {m.get('content', '')}" for m in rest)
    return "\n\n".join(systems), prompt


# --- abstract base ----------------------------------------------------------
class Engine(ABC):
    name = "engine"

    @abstractmethod
    def run_once(self, messages, *, model=None, system=None, cwd=None,
                 allowed_tools=None, mcp_config=None, session_id=None, resume=False) -> EngineResult: ...

    def stream(self, messages, *, model=None, **kw) -> Iterator[str]:
        # Default: no token streaming — emit the whole result once. CLI token
        # streaming arrives in Phase 6; today nothing in the UI calls stream().
        yield self.run_once(messages, model=model, **kw).text

    def available(self) -> tuple[bool, str]:
        return True, ""

    def models(self, force: bool = False) -> list[str]:
        return []


# --- FakeEngine (tests / no external dep) -----------------------------------
class FakeEngine(Engine):
    name = "fake"

    def __init__(self, reply: str = "(fake response)"):
        self.reply = reply
        self.calls: list[dict] = []

    def run_once(self, messages, *, model=None, system=None, cwd=None,
                 allowed_tools=None, mcp_config=None, session_id=None, resume=False) -> EngineResult:
        self.calls.append({"messages": messages, "allowed_tools": allowed_tools,
                           "cwd": cwd, "mcp_config": mcp_config, "session_id": session_id})
        return EngineResult(text=self.reply, session_id=session_id or "fake-session")

    def stream(self, messages, *, model=None, **kw) -> Iterator[str]:
        for tok in self.reply.split(" "):
            yield tok + " "


# --- shared CLI plumbing ----------------------------------------------------
class _CliEngine(Engine):
    """Common subprocess machinery for Claude Code / Codex."""

    bin_default = ""

    def __init__(self, binary: str = "", model: str = ""):
        self.binary = binary or self.bin_default
        self.model = model

    def _resolve(self) -> str:
        path = shutil.which(self.binary) or (self.binary if "/" in self.binary else "")
        if not path:
            raise EngineError(f"{self.name}: '{self.binary}' not found on PATH. "
                              f"Install it or set its path in Settings.")
        return path

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.binary) or (self.binary if "/" in self.binary else "")
        return (bool(path), f"{self.binary} on PATH" if path else f"'{self.binary}' not found")

    def _run(self, argv: list[str], stdin_text: str, cwd: str | None = None) -> subprocess.CompletedProcess:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                argv, input=safe_text(stdin_text), capture_output=True, text=True,
                timeout=CLI_TIMEOUT, env=None, cwd=_safe_cwd(cwd),
            )
        except FileNotFoundError as exc:
            raise EngineError(f"{self.name}: binary not found ({exc}).") from exc
        except subprocess.TimeoutExpired as exc:
            raise EngineError(f"{self.name}: timed out after {CLI_TIMEOUT}s.") from exc
        logger.info("%s exit=%s latency=%.2fs", self.name, proc.returncode, time.monotonic() - t0)
        if proc.returncode != 0:
            # claude-code with --output-format stream-json often writes the actual error to
            # STDOUT (a {"type":"result","is_error":true,...} line), leaving stderr empty —
            # so fall back to stdout (and the last JSON error message in it) for a useful detail.
            detail = proc.stderr.strip() or _last_stream_error(proc.stdout) or proc.stdout.strip()
            raise EngineError(f"{self.name} exited {proc.returncode}: {detail[:300]}")
        return proc


def _last_stream_error(stdout: str) -> str:
    """Pull a human error out of claude-code stream-json stdout when the process failed:
    the `result` line's error text, or the last assistant/system text. '' if none."""
    msg = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("is_error") or ev.get("type") == "result":
            cand = ev.get("result") or ev.get("error") or ev.get("message") or ""
            if isinstance(cand, dict):
                cand = cand.get("message") or cand.get("text") or ""
            if cand:
                msg = str(cand)
    return msg.strip()


def claude_turn_events(lines):
    """Parse Claude stream-json lines for ONE turn → {status|token|done} events, then
    stop at the turn's 'result'. Shared by one-shot streaming and live (persistent) mode.
    'done' carries the turn's final text + session_id. Tokens come from partial deltas,
    falling back to complete assistant text blocks if partials are off."""
    deltas, seen_partial, tool_ids, sid = [], False, set(), None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if ev.get("session_id"):
            sid = ev["session_id"]
        if t == "stream_event":
            inner = ev.get("event", {})
            if inner.get("type") == "content_block_delta":
                d = inner.get("delta", {})
                if d.get("type") == "text_delta" and d.get("text"):
                    seen_partial = True
                    deltas.append(d["text"])
                    yield {"type": "token", "text": d["text"]}
            elif inner.get("type") == "content_block_start":
                blk = inner.get("content_block", {})
                if blk.get("type") == "tool_use":
                    s = _tool_status(blk.get("name", ""))
                    if s:
                        yield {"type": "status", "text": s}
        elif t == "assistant":
            for b in ev.get("message", {}).get("content") or []:
                if b.get("type") == "tool_use" and b.get("id") not in tool_ids:
                    tool_ids.add(b.get("id"))
                    s = _tool_status(b.get("name", ""))
                    if s:
                        yield {"type": "status", "text": s}
                if b.get("type") == "text" and not seen_partial and b.get("text"):
                    deltas.append(b["text"])
                    yield {"type": "token", "text": b["text"]}
        elif t == "result":
            yield {"type": "done", "session_id": sid, "text": ev.get("result", "") or "".join(deltas)}
            return  # one turn complete; leave the stream open for the next (live mode)


def codex_turn_events(lines):
    """Parse `codex exec --json` events for one turn → {status|token|done}. Codex emits
    the final answer as a single agent_message (no token deltas); thread_id (the session
    id, used for resume) comes from thread.started. 'done' carries final text + thread_id."""
    thread_id, final = None, ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue   # skip non-JSON noise (e.g. "Reading additional input…")
        t = ev.get("type")
        if t == "thread.started":
            thread_id = ev.get("thread_id")
        elif t == "item.started":
            it = ev.get("item", {})
            if it.get("type") == "mcp_tool_call":
                s = _tool_status(it.get("tool", ""))
                if s:
                    yield {"type": "status", "text": s}
        elif t == "item.completed":
            it = ev.get("item", {})
            if it.get("type") == "agent_message" and it.get("text"):
                final = it["text"]
        elif t == "turn.completed":
            if final:
                yield {"type": "token", "text": final}
            yield {"type": "done", "session_id": thread_id, "text": final}
            return


def _tool_status(name: str) -> str:
    """Human-readable status for a tool the agent is about to use (shown while it works).
    Returns '' for internal/uninteresting tools (e.g. ToolSearch) so they're not shown."""
    n = (name or "").replace("mcp__pa__", "")
    _INTERNAL = {"ToolSearch", "TodoWrite", "Task"}
    if n in _INTERNAL:
        return ""
    return {
        "Skill": "using a paper-reading skill…",
        "Read": "reading the PDF…",
        "read_paper_text": "reading the paper…",
        "get_paper_context": "checking your notes & highlights…",
        "search_fragments": "searching your fragments…",
        "read_wiki_page": "reading the wiki…",
        "get_fragment": "pulling up a fragment…",
    }.get(n, f"using {n}…")


# --- Claude Code ------------------------------------------------------------
class ClaudeCodeEngine(_CliEngine):
    name = "claude-code"
    bin_default = "claude"

    def _prepare(self, messages, model, system, allowed_tools, mcp_config,
                 session_id, resume, *, extra=()) -> tuple[list[str], str]:
        sys_text, prompt = _split_system(messages)
        if system:
            sys_text = (sys_text + "\n\n" + system).strip() if sys_text else system
        argv = [self._resolve(), "-p", "--output-format", "stream-json", "--verbose", *extra]
        # The shared `model` config is OpenAI-centric by default (gpt-4o-mini); only
        # pass --model when it's actually a Claude model, else let Claude use its own.
        m = model or self.model
        if m and (m in ("sonnet", "opus", "haiku") or m.lower().startswith("claude")):
            argv += ["--model", m]
        if sys_text:
            argv += ["--append-system-prompt", sys_text]
        if allowed_tools:
            argv += ["--allowedTools", ",".join(allowed_tools)]
        if mcp_config:
            cfg_str = mcp_config if isinstance(mcp_config, str) else json.dumps(mcp_config)
            # --strict-mcp-config: load ONLY our server, ignore the user's global ones.
            argv += ["--mcp-config", cfg_str, "--strict-mcp-config"]
        if session_id:
            # resume an existing session, or start a new one with our chosen id.
            argv += ["--resume", session_id] if resume else ["--session-id", session_id]
        return argv, prompt

    def run_once(self, messages, *, model=None, system=None, cwd=None,
                 allowed_tools=None, mcp_config=None, session_id=None, resume=False) -> EngineResult:
        argv, prompt = self._prepare(messages, model, system, allowed_tools, mcp_config,
                                     session_id, resume)
        proc = self._run(argv, prompt, cwd=cwd)
        return _parse_claude_stream_json(proc.stdout)

    def stream_events(self, messages, *, model=None, system=None, cwd=None,
                      allowed_tools=None, mcp_config=None, session_id=None, resume=False):
        """Yield UI events live as Claude works: {'status'|'token'|'done'|'error', ...}.
        Uses --include-partial-messages for token-level deltas; the terminal 'done'
        carries the authoritative final text + session_id for persistence."""
        argv, prompt = self._prepare(messages, model, system, allowed_tools, mcp_config,
                                     session_id, resume, extra=["--include-partial-messages"])
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=_safe_cwd(cwd))
        got_done = False
        try:
            proc.stdin.write(safe_text(prompt)); proc.stdin.close()
            for ev in claude_turn_events(proc.stdout):
                got_done = got_done or ev.get("type") == "done"
                yield ev
            proc.wait()
            if not got_done and proc.returncode not in (0, None):
                err = (proc.stderr.read() or "").strip()[:300]
                yield {"type": "error", "text": f"claude exited {proc.returncode}: {err}"}
        finally:
            if proc.poll() is None:
                proc.kill()

    def live_argv(self, *, model=None, system=None, allowed_tools=None, mcp_config=None) -> list[str]:
        """Argv for a PERSISTENT streaming process (live mode): one process fed many user
        turns over stdin (stream-json in/out). No session id — the process IS the session."""
        argv = [self._resolve(), "-p", "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        m = model or self.model
        if m and (m in ("sonnet", "opus", "haiku") or m.lower().startswith("claude")):
            argv += ["--model", m]
        if system:
            argv += ["--append-system-prompt", system]
        if allowed_tools:
            argv += ["--allowedTools", ",".join(allowed_tools)]
        if mcp_config:
            cfg_str = mcp_config if isinstance(mcp_config, str) else json.dumps(mcp_config)
            argv += ["--mcp-config", cfg_str, "--strict-mcp-config"]
        return argv

    def models(self, force: bool = False) -> list[str]:
        # Claude Code resolves aliases server-side; offer the common ones.
        return ["sonnet", "opus", "haiku"]


def _parse_claude_stream_json(out: str) -> EngineResult:
    """Parse Claude Code's --output-format stream-json (one JSON object per line).

    The terminal ``{"type":"result"}`` event carries the final text, session_id and
    usage; assistant deltas are the fallback if no result event is present.
    """
    text, session_id, usage, deltas, events = "", None, None, [], []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(ev)
        etype = ev.get("type")
        if etype == "result":
            text = ev.get("result", "") or text
            session_id = ev.get("session_id", session_id)
            u = ev.get("usage") or {}
            usage = {"prompt_tokens": u.get("input_tokens", 0),
                     "completion_tokens": u.get("output_tokens", 0)}
        elif etype == "assistant":
            for block in (ev.get("message", {}).get("content") or []):
                if block.get("type") == "text":
                    deltas.append(block.get("text", ""))
            session_id = ev.get("session_id", session_id)
        elif etype == "system" and ev.get("session_id"):
            session_id = ev["session_id"]
    if not text:
        text = "".join(deltas).strip()
    return EngineResult(text=text, session_id=session_id, usage=usage, events=events)


# --- Codex ------------------------------------------------------------------
class CodexEngine(_CliEngine):
    name = "codex"
    bin_default = "codex"

    def run_once(self, messages, *, model=None, system=None, cwd=None,
                 allowed_tools=None, mcp_config=None, session_id=None, resume=False) -> EngineResult:
        sys_text, prompt = _split_system(messages)
        if system:
            sys_text = (sys_text + "\n\n" + system).strip() if sys_text else system
        # Codex `exec` is the non-interactive (headless) entry point. Its rich JSON
        # event shape is deferred (AGENTIC_PLAN open hole); we read the final stdout.
        full = (sys_text + "\n\n" + prompt).strip() if sys_text else prompt
        argv = [self._resolve(), "exec", "--skip-git-repo-check"]
        if model or self.model:
            argv += ["--model", model or self.model]
        argv += [full]
        proc = self._run(argv, "", cwd=cwd)
        return EngineResult(text=proc.stdout.strip())

    def _paper_config_args(self, slug: str, read_tools: list[str]) -> list[str]:
        """Inline -c overrides for a read-only paper-chat run: our stdio MCP server
        (scoped to the collection, PA_MCP_READONLY), no interactive approval, read-only
        sandbox, and per-tool auto-approve for ONLY the read tools (the write tools fall
        back to needing approval → denied headless — a tool allowlist via approval)."""
        repo = str(Path(__file__).resolve().parent.parent)
        env = ('{PA_MCP_COLLECTION="%s",PAPER_AGENT_HOME="%s",PYTHONPATH="%s",PA_MCP_READONLY="1"}'
               % (slug, APP_DIR, repo))
        args = ["-c", f'mcp_servers.pa.command="{sys.executable}"',
                "-c", 'mcp_servers.pa.args=["-m","app.mcp_stdio"]',
                "-c", f"mcp_servers.pa.env={env}",
                "-c", 'approval_policy="never"',
                "-c", 'sandbox_mode="read-only"']
        for t in read_tools:
            args += ["-c", f'mcp_servers.pa.tools.{t}.approval_mode="approve"']
        return args

    def paper_stream(self, *, slug, system, read_tools, cwd, session_id, user_text,
                     image_paths=None):
        """One read-only paper-chat turn via `codex exec` (or `exec resume`). Yields
        {status|token|done|error}; 'done' carries the thread_id as session_id. Codex
        emits the answer in one chunk (no token deltas). Pasted images attach natively
        via `-i <file>` (Codex's own image input)."""
        common = self._paper_config_args(slug, read_tools) + ["--json", "--skip-git-repo-check"]
        imgs = []
        for p in image_paths or []:
            imgs += ["-i", str(p)]
        # `-i` takes a VARIADIC <FILE>... — it would swallow the positional prompt/session,
        # so terminate options with `--` when images are present.
        sep = ["--"] if imgs else []
        base = [self._resolve(), "exec"]
        if session_id:   # resume keeps the session's context; send only the new turn
            argv = base + ["resume"] + common + imgs + sep + [session_id, user_text]
        else:            # first turn: Codex has no --append-system-prompt, so prepend it
            prompt = (system + "\n\n" + user_text).strip() if system else user_text
            argv = base + common + imgs + sep + [prompt]
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1, cwd=_safe_cwd(cwd))
        got_done = False
        try:
            proc.stdin.close()   # nothing on stdin; avoids codex's "reading stdin" hang
            for ev in codex_turn_events(proc.stdout):
                got_done = got_done or ev.get("type") == "done"
                yield ev
            proc.wait()
            if not got_done:
                err = (proc.stderr.read() or "").strip()[:300]
                yield {"type": "error", "text": f"codex exited {proc.returncode}: {err}"}
        finally:
            if proc.poll() is None:
                proc.kill()


# --- selection --------------------------------------------------------------
# CLI agents only: the app drives Claude Code or Codex as a subprocess. There is no
# API backend — every LLM feature requires one of these installed (FakeEngine is tests).
ENGINES = {"claude-code": ClaudeCodeEngine, "codex": CodexEngine}


def select_engine_name(cfg: dict) -> str:
    """The configured engine; defaults to Claude Code when unset/invalid."""
    name = (cfg.get("engine") or "").strip()
    return name if name in ENGINES else "claude-code"


def build_engine(cfg: dict) -> Engine:
    name = select_engine_name(cfg)
    if name == "codex":
        return CodexEngine(cfg.get("codex_bin", ""), cfg.get("model", ""))
    return ClaudeCodeEngine(cfg.get("claude_bin", ""), cfg.get("model", ""))
