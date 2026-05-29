# Agentic Backend — Build Plan (revised contract)

A sequenced plan to make the Paper Collection Wiki Agent run on a **user-selectable
CLI agent** (Claude Code | Codex | OpenAI), add an **attribution guardrail in code**,
and add **agentic, collection-scoped chat** — without ever letting the agent author
the wiki.

This supersedes the earlier draft. It is a **refactor-and-extend** plan over the real
FastAPI codebase, re-cut after grilling so the phases are correctly ordered and the
facts match the repo. Each phase is independently shippable and leaves the app working.

---

## Operating principles (read before any phase)

- **The app already works. Do not rebuild it.** Every phase extends existing modules
  (`wiki.py`, `thoughts.py`, `notes.py`, `repo.py`, `llm.py`, `context.py`, `main.py`,
  `db.py`). Read the current implementation before modifying it.
- **The guardrail lives in code, never in a prompt.** No phase moves provenance or
  attribution enforcement into agent instructions.
- **The agent never writes files or the wiki.** It may *call* a gated app function
  (`submit_proposal`) that writes the **review queue** (`proposed-edits/`), which is not
  the wiki. The only path that writes `wiki/` remains `wiki.accept_proposed` (user accept).
- **One writer.** Any write goes through an existing module function, never a raw file
  handle or DB connection opened inside agent/MCP code.
- **Tests need no external agent.** `FakeEngine` backs every phase's tests; real engines
  are exercised manually.
- **Each phase ends green:** existing tests pass, new tests added, app runs with the
  selected engine absent (LLM features degrade, non-LLM features keep working).

---

## Repo facts the earlier draft got wrong (now corrected)

- **There is no `thoughts` table.** Thoughts are markdown files with frontmatter
  (`app/thoughts.py`). `paper_notes` is the only capture in SQLite.
- **`kind`/`origin` are already taken.** `annotations` has `kind ∈ {highlight,note}`,
  `origin ∈ {app,zotero}` (`db.py`). The new stamps use **distinct names**:
  `synth_kind ∈ {seed,reasoning}`, `author_origin ∈ {human,agent,external}`.
- **Highlights/papers are not in the wiki pipeline** today; `gather_inputs` feeds
  purpose+notes+thoughts only. Phase 2 wires them in (the gate needs them).
- **`submit_proposal` and the MCP surface both live in Phase 4**, so the agentic
  organizing pass cannot precede them — phases are re-cut accordingly.

---

## Core data model (target state)

Every fragment resolves to an effective `(synth_kind, author_origin)` via one resolver,
`provenance.effective_stamp(ref, slug)`:

| Type | Stored where | Effective stamp |
|---|---|---|
| `highlight` | `annotations` row | constant `(seed, human)` |
| `paper` | `papers` row | constant `(seed, external)` |
| `note` | `paper_notes` row + md mirror | origin = `author_origin` (human); kind = `synth_kind` override if set, else **`reasoning` iff the note's `thoughts` field is non-empty, else `seed`** |
| `thought` | md file frontmatter | both read from frontmatter; door-stamped on create (`agent` unreachable until Phase 6) |

- `synth_kind`: `seed` = an attention signal; `reasoning` = an argument/connection a human made.
- `author_origin`: set by the code path that created the fragment, never assigned by hand.

Two claim types on proposals:

- **`attributed`** — "Paper P reports R." Grounded by the source it attributes to.
- **`synthesis`** — "These share failure mode F." The human's conclusion.

**`claim_type` authority (code floor):** code computes a structural floor —
`synthesis` if the claim cites ≥2 papers or any thought, else `attributed`. The agent
may *propose* a type but can only make a claim **stricter**; the gate uses the stricter
of (structural, agent-proposed). A mislabel can only over-demote (safe), never under-gate.

---

## Phase 1 — Typed captures (`synth_kind` + `author_origin`)  ← DONE

**Goal:** every fragment carries a stamp, set by the creating code path.

- Add `synth_kind TEXT DEFAULT 'auto'` and `author_origin TEXT DEFAULT 'human'` to
  `paper_notes` (`db.py` schema + idempotent `_migrate`). `'auto'` ⇒ heuristic.
- Thoughts: write `synth_kind` + `author_origin` into frontmatter (`thoughts.py`);
  reads default missing → `(seed, human)` so existing files migrate non-destructively.
- Stamp `author_origin` **at the endpoint**, not via a caller-supplied flag. Human
  capture endpoints set `human`; no agent-writing endpoint exists yet (so `agent` is
  correctly unreachable this phase).
- `provenance.effective_stamp(ref, slug)` implementing the table above. Notes leave a
  `# SPAN-TODO` seam for later span-level reasoning marks.
- UI: a `seed | reasoning` toggle on thought capture (default `seed`); a
  `auto | seed | reasoning` override on the note form (default `auto`).

**Acceptance:** new captures carry both stamps; migration is clean and non-destructive;
the markdown mirror round-trips the new frontmatter; tests cover stamp-by-door and the
resolver for all four types.

---

## Phase 2 — The gate (`gate()` replaces flat `_filter_claims`)  ← DONE

**Goal:** classify each proposed claim and enforce the attribution boundary in code.

- Wire **highlights + papers** into `gather_inputs` (`valid_highlights`, `valid_papers`);
  the generator may cite them.
- Add `claim_type` to the proposal schema; compute the structural floor; take the
  stricter of (structural, agent label).
- Replace `_filter_claims` with `gate(claim, provenance)`:
  - **attributed** → `ACCEPT` iff it cites a valid `paper` or `highlight`; else `REJECT`.
  - **synthesis** → `ASSERT` iff some ref resolves to `(reasoning, human)`; else
    `DEMOTE_TO_OPEN_QUESTION` (route into `gaps` as a question — never reject/discard).
- Log every demotion/rejection with its reason.

**Acceptance:** unit tests pin each provenance type's grounding power on the mocked-LLM
harness; existing wiki tests updated to the new gate, still green.

---

## Phase 3 — Engine seam + CLI swap (the dependency flip)  ← DONE

**Goal:** `llm.complete()` runs on the user's chosen CLI; no API key required.

- `engine.py`: abstract `Engine` with `run_once(prompt, *, cwd, allowed_tools,
  system=None, session_id=None) -> EngineResult` and async `stream(...)`. `EngineResult`
  carries parsed events, final text, optional `session_id`.
- Implementations: `FakeEngine` (tests; canned proposals/tokens), `ClaudeCodeEngine`,
  `CodexEngine`, `OpenAIEngine`.
  - Invocation: `claude -p "<prompt>"` (via arg/stdin, **not** `-p @file`),
    `--system-prompt`, `--model`, `--output-format stream-json --verbose`,
    `--session-id`/`--resume`, hard timeout. Tool-less passes allowlist nothing.
- Settings: engine selector (Claude Code | Codex | OpenAI), per-engine binary path +
  model; startup probe; graceful degradation if the selected binary is absent/unauthed
  (mirrors today's no-API-key behavior).
- Repoint `llm.complete`/`stream`: flatten `messages` (system → `--system-prompt`,
  user → prompt, history → session resume) → `engine.run_once(tools=[])`. All existing
  call sites unchanged in signature. Today's analyze→generate now flows through the CLI.

**Acceptance:** every existing feature works on the selected CLI with no API key;
engine swap is a config change; `FakeEngine` green in tests.

---

## Phase 4 — MCP read surface + `submit_proposal` (bounded)  ← DONE (transport: stdio, not HTTP)

**Goal:** expose bounded, validating tools to an agent from the **same FastAPI process**.

- Mount an MCP **streamable-HTTP** endpoint (`/mcp`) on the FastAPI app, loopback-only,
  guarded by a per-run bearer token. Tool bodies call existing module read functions —
  no second DB connection, no raw filesystem.
- Read tools (bounded, paginated, previews only — never full bodies):
  `get_unreasoned_seeds(collection)`, `get_fragment(id)`,
  `search_fragments(collection, query)` (FTS5), `read_wiki_page(collection, page)`.
- `submit_proposal(pages[])`: app code validates schema → structural floor + gate →
  writes survivors to `proposed-edits/*.json`; returns `{written, demoted, rejected}`.
- No `Write`, no built-in `Read`/`Glob`/`Grep`/`Bash`. `allowedTools` is the boundary;
  `cwd` is cosmetic.

**Acceptance:** bounded outputs (size assertions); tools never open files/DB directly;
`main.py:proposed_get` reads the queue unchanged.

---

## Phase 5 — Agentic organizing pass  ← DONE

**Goal:** the agent drives retrieval and returns gated proposals.

- Rewire the organizing pass (extracted from `wiki.analyze/generate`) to
  `engine.run_once(allowed_tools=[mcp read tools + submit_proposal])`; the agent reads
  via MCP and returns via `submit_proposal`. The gate runs inside `submit_proposal`.
- `FakeEngine` still produces canned proposals through the same path.

**Acceptance:** a real pass produces gated proposals from real fragments; engine swap is
config; with no CLI the "Generate" action disables cleanly.

---

## Phase 6 — `/{collection}` agentic chat  ← DONE

**Goal:** opt-in agentic chat; default chat stays simple (CLI-streamed).

- Parse a `/{slug}` prefix; unknown slug → reply listing collections (no silent
  fall-through). Slug autocomplete in UI.
- Un-prefixed chat: `engine.stream(tools=[])`, per-thread session resume (CLI-streamed).
- `/{collection}` chat: `engine.stream(tools=[search_fragments, read_wiki_page])`,
  session keyed per `(thread, collection)`; no raw Read/Write/full-paper-text.
- Capture from agentic chat: one tap creates a `(seed, agent)` thought (verbatim,
  attributed) — cannot ground an assertion (gate). Optional "your take" creates a
  separate `(reasoning, human)` thought linked via `prompted_by` — the only one that can.

**Acceptance:** default-chat behavior unchanged; agentic chat reaches data only via MCP;
sessions isolated per `(thread, collection)`; capture stamps verified by test.

---

## Phase 7 — Debt queue  ← DONE (built; extended per user's 4-action design)

Cluster `seed` fragments into a prioritized list of **questions for the human** (never
prose), feeding `gaps`/a reading-debt view; demotions from Phase 2 surface here. **Do
not build unless you'll open it.**

---

## What stays human (never automated)

Reading & reasoning (generating `(reasoning, human)` fragments); the entailment judgment
in review (cited fragments shown first and full, then the claim, then accept). The agent
may *prompt* reasoning; it may never *supply* it.

## Accepted/deferred holes

Span-level note marks (`# SPAN-TODO`); synthesis-dressed-as-attribution *within a single
source* (caught only by fragments-first review — the one retained human judgment); Codex
event-shape parity.

## Build order

1. Typed captures — pure data (in progress).
2. Gate — attribution boundary in code.
3. Engine seam + CLI swap — the dependency flip (no API key).
4. MCP read surface + `submit_proposal`.
5. Agentic organizing pass.
6. `/{collection}` chat.
7. Debt queue (gated).
