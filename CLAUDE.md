# Paper Collection Wiki Agent — Build Specification

## Context for Claude Code

You are building a personal research tool for an ML researcher who maintains paper collections in Zotero. The tool produces and maintains a **personal wiki per collection**, where the wiki reflects **the researcher's own thinking** (notes, reflections, conversations), not an LLM's summary of the papers.

This document is the contract. Do not invent features outside it. When in doubt, ask the user before building.

## Philosophical anchor — read this twice

This project inverts the usual "AI summarizes papers" pattern. The wiki is the user's externalized understanding, with papers as evidence. The LLM is an editor and research assistant, not the author.

Concretely:
- The wiki is generated from **the user's notes + the user's thoughts**, with papers as secondary grounding.
- LLM-proposed wiki changes are **always diffs the user reviews**, never silent mutations.
- The user does the reading. The LLM helps the user read, organize what the user wrote, and surfaces gaps. It does not replace the reading.

If a feature you're about to build lets the LLM produce wiki content without the user having written something first, stop and check with the user.

> **Amendment (2026-05-27 … cognitive-model rewrite 2026-05-31, Phase A):** The wiki is now a
> single page composed of stage-gated sections under `wiki/sections/`, following a cognitive-
> model progression: **Field Model → Belief Model → Research Model** (the user's blueprint,
> 2026-05-31). Phase A ships **Stage 0 only** — the Field Model:
>
>   - `wiki/sections/thesis.md` — Collection Thesis: one paragraph + three callouts
>     (`core_tension`, `key_intuition`, `central_question`).
>   - `wiki/sections/landscape.md` — Research Landscape: four bullet-list columns
>     (Problems / Methods / Debates / Open Questions), each capped at 6 items by the validator
>     to force real clustering. The previous failure mode (17 method "families" on a 26-paper
>     collection — bibliography by another name) is prevented in code, not in the prompt.
>   - Papers (Evidence) row is rendered live from the DB; no agent-written file for it.
>
> Single LLM call (`field-model` skill). Direct-write agent seed, agent-tagged
> (`generated_by: agent` frontmatter). **Non-destructive** of legacy wikis: the prior
> `wiki/starter/*` tree (llm_wiki-pattern top picks) and `wiki/<phase-5-section>/*`
> (notes-based wiki under problems/methods/gaps/benchmarks/synthesis) are no longer read —
> they stay on disk for safety but contribute nothing to the rendered wiki.
>
> **Phase B (2026-05-31)** extends the same `field-model` skill (still one LLM call) to also
> produce two structured artifacts: `wiki/sections/concepts.json` (5–12 named research concepts
> with synonym lists for the deterministic attention scorer) and `wiki/sections/recommended.json`
> (3–5 editorial reading-path picks with `why_now` rationales; positional labels Start here /
> Next / Then assigned by index at render time, not by the LLM). These power two new sections:
>
>   - **Your Current Focus** (Section 1 sidebar, Stage 2) — top concepts ranked by deterministic
>     attention. Threshold-gated: doesn't render at all until at least one concept score crosses
>     `_FOCUS_CONCEPT_FLOOR=3`. The blueprint's "premature inference is anchoring" concern is
>     enforced in code: no inference is shown until there's enough signal to be honest about it.
>     Pure SQL scoring — no LLM in the per-render loop. Counts highlights (×1) and notes (×5)
>     whose text matches any concept synonym via case-insensitive word-boundary regex.
>   - **Recommended Reading** (Section 4) — the agent's editorial 3-pick reading path, always
>     rendered when populated. Each pick gets attention chips (🔥 hot / ✨ new) at render time
>     from the same per-paper scorer as the Papers section.
>
> **Phase C (2026-05-31)** adds the **Belief** layer (Stage 3 of the cognitive-model wiki).
> Beliefs are single-sentence claims a researcher might hold about the collection — agent-
> drafted candidates land in a tray, the user accepts to promote them to the wiki's
> Section 3 "Your Current Understanding" (a hybrid contract: agent suggests, user owns).
>
> Files:
>   - `wiki/sections/beliefs/_candidates/<id>.md` — pending candidates (the tray).
>   - `wiki/sections/beliefs/<slug>.md` — accepted beliefs (Section 3 content).
>
> Each belief has YAML frontmatter: `type: belief`, `status: candidate|accepted`,
> `title`, `confidence: emerging|medium|uncertain`, `supporting_papers: [refs]`,
> `related_concepts: [slugs]`, plus the usual provenance (`generated_by: agent`,
> `generator: belief-draft`).
>
> Pipeline (`wiki.suggest_beliefs`, one LLM call via the `belief-draft` skill):
>   - **Signal floor (`can_suggest_beliefs`)**: the Suggest button only renders when at
>     least one concept score crosses `_BELIEF_SUGGEST_FLOOR=5` OR the user has any
>     non-empty note. Below the floor, neither the button nor the LLM call fires.
>     The blueprint's "premature inference is anchoring" concern is enforced here.
>   - The agent sees: concept space, top 25 highlights, top 15 notes, existing
>     beliefs (to avoid duplicates), valid paper refs.
>   - Validator (`_validate_belief_candidates`): drops candidates without any valid
>     supporting paper (un-cited beliefs); drops titles shorter than 10 chars;
>     dedupes by title slug; clamps confidence to enum; caps at
>     `_BELIEF_CANDIDATES_MAX=5`.
>   - Each surviving candidate written as its own .md file (parallel-safe; partial
>     parse failure still surfaces what survived).
>
> Promote / dismiss:
>   - `accept_belief(slug, cid)` moves the file from `_candidates/<cid>.md` to
>     `beliefs/<title-slug>.md`, bumps `status` and stamps `accepted_at`.
>   - `dismiss_belief(slug, cid)` deletes the candidate.
>   - Routes: `POST /c/<slug>/wiki/beliefs/{suggest,<id>/accept,<id>/dismiss}`.
>
> Section 3 rendering:
>   - Accepted beliefs render as cards with the confidence chip, supporting papers
>     (live attention chips), and related-concept tags.
>   - Candidate tray (amber-tinted) renders below the accepted beliefs with Accept /
>     Dismiss buttons per candidate.
>   - The Suggest button only renders when `can_suggest_beliefs` is True (signal floor).
>   - Empty state: "No beliefs yet. Click ✦ Suggest beliefs above…"
>
> Research Questions (Stage 4) and the post-draft grilling (intent capture) remain
> deferred to later phases.
>
> **Knowledge graph (2026-05-31, Research-Model layer).** A purely structural graph
> over the wiki's entities — `app/graph.py` (pure, embedding-free) + assembly in
> `wiki.build_collection_graph` / render view in `wiki.connection_view`.
>   - Nodes: papers, concepts, problems, methods, accepted beliefs. (Problems/methods
>     were promoted to paper-anchored nodes — landscape.json carries their `papers`.)
>   - Edges: memberships we already compute (concept/problem/method/belief ↔ papers,
>     via LLM assignment ∪ synonym match) + belief→concept links. No paper↔paper
>     "builds-on/critiques" edges (those aren't in abstracts — the hallucination
>     magnet; nashsu doesn't compute them either, and neither do we).
>   - Relatedness adapts nashsu's structural 4-signal score (source overlap / direct
>     link / Adamic-Adar shared neighbors / type-affinity multiplier). No embeddings.
>   - Surfaced in **Section 5 "Connections & themes"**: label-propagation themes
>     (clusters of ≥2 co-occurring entities), strong co-occurrences, and orphan papers
>     (evidence tied to no concept/method/problem yet — a "go map these" nudge).
>     Section gates off when the graph is too sparse to say anything. No LLM in the
>     render loop; recomputed live like the rest of the cognitive model. (Section
>     order is now 1 Thesis · 2 Landscape · 3 Recommended · 4 Understanding ·
>     5 Connections · 6 Papers.)
>
> Migration: legacy `wiki/starter/index.md` OR `wiki/overview.json` on disk → `load_overview`
> returns `{needs_migration: True}` so the panel renders a one-time "schema changed —
> regenerate?" banner. No silent conversion.
>
> **Cleanup (2026-05-31):** the old **notes-based wiki pipeline** (the `gate()` /
> `run_generation` / `process_pages` / `proposal_from_chat` flow, `organizer.py`, the
> `proposed-edits/` review queue + `accept_proposed`/`reject_proposed`, the `run_curator`
> refresh, `lint_wiki`/`wiki_lint.py`, the standalone `wiki.html` hub + `/c/<slug>/wiki/<name>`
> page viewer, the chat→proposal classifier `suggest.py`, and the MCP `submit_proposal` /
> brainstorm write tools) and the **reading-debt** subsystem (`debt.py`) were **removed** — the
> cognitive-model wiki replaced them and beliefs use `accept_belief` (not the gate). The belief
> tray IS the human-in-the-loop gate now. **Kept:** `triage.py` (arXiv paper inbox) and
> `discover.py` (gap-fill, stale detection, add-by-URL) as separate paper-management features,
> still reachable via `/c/<slug>/triage`, `/c/<slug>/wiki/gaps`, `/c/<slug>/stale`. The Phase 5
> feature list below describes that removed pipeline and is retained only as historical spec.

## Existing pieces (do not rebuild)

- **Zotero**: source of truth for papers, collections, PDF paths. Read via local SQLite at `~/Zotero/zotero.sqlite` (read-only — Zotero must not be running) or via the local HTTP API on port 23119 if available. Prefer the HTTP API when Zotero is open; fall back to SQLite when closed. Detect which is usable at startup.
- **zotero-arxiv-daily-local**: existing component (in this repo) that adds candidate papers to Zotero collections. You do not modify it. You read what it writes.
- **llm_wiki** (https://github.com/nashsu/llm_wiki): a reference implementation whose **conventions** we adopt but whose **code** we do not use. Specifically adopt: directory layout, YAML frontmatter with `sources: []`, `[[wikilink]]` syntax, the two-step (analyze → generate) LLM pattern, the Review-queue pattern for human-in-the-loop. Do not adopt: the Tauri app, the Chrome extension. **Knowledge graph (amended 2026-05-31):** we now DO adopt llm_wiki's *structural* graph relevance model — its 4-signal scoring (source overlap / direct link / shared-neighbor Adamic-Adar / type affinity) is purely structural and needs no embeddings, so it satisfies our "no vector store / no embeddings" rule. Reimplemented from scratch in `app/graph.py` (we still don't use their TypeScript/Sigma/React code). See the cognitive-model amendment below.

## Tech stack (locked)

- **Backend**: Python 3.11+, FastAPI, SQLite (via stdlib sqlite3 or sqlalchemy core — your call, no ORM), `pyzotero` only if it adds real value, otherwise raw SQL against `zotero.sqlite`.
- **Frontend**: server-rendered HTML + HTMX + a sprinkle of Alpine.js for client-side reactivity. No React, no build step, no SPA framework. Tailwind via CDN is fine for v1.
- **PDF viewer**: PDF.js via CDN, embedded in an iframe or a `<canvas>` setup. Don't reinvent.
- **LLM**: CLI agents only — Claude Code or Codex, driven as subprocesses through the `Engine` seam (`engine.py`), behind a thin `llm.py` interface (`complete()`, `stream()`). **No API backend and no API key** (the OpenAI SDK was removed): every LLM feature requires the selected CLI agent installed. Model = the CLI's own model id (Claude aliases sonnet/opus/haiku; Codex its model id; blank = engine default). _Supersedes the original "OpenAI SDK / gpt-4o-mini" plan after the agentic pivot._
- **Embeddings/vector store**: do **not** add a vector store in v1. Use full-text search (SQLite FTS5) over notes and wiki content. Revisit only if retrieval quality is demonstrably bad.
- **Markdown**: render via `markdown-it-py` or `mistune`. Wikilinks `[[page]]` resolved server-side to anchor links.

## Storage layout

```
~/.paper-agent/
├── config.toml                    # API key, model, Zotero paths, etc.
├── app.sqlite                     # chat threads, messages, paper notes, sync state, triage queue
└── collections/
    └── <collection-slug>/
        ├── purpose.md             # collection-level mission (rarely changes)
        ├── schema.md              # wiki structure rules (rarely changes)
        ├── thoughts/              # timestamped thought stream
        │   ├── 2026-05-22T14-30-00.md
        │   └── ...
        ├── thoughts-archive/      # consolidated/superseded thoughts
        ├── notes/                 # per-paper notes (one file per paper)
        │   └── <zotero-key>.md
        ├── wiki/                  # generated wiki pages
        │   ├── index.md
        │   ├── log.md
        │   ├── problems/
        │   ├── methods/
        │   ├── gaps/
        │   ├── benchmarks/
        │   └── synthesis/
        ├── proposed-edits/        # pending LLM-proposed wiki diffs, awaiting user review
        └── triage/                # candidate papers awaiting accept/reject (from arxiv-daily)
```

PDFs are **not** copied. Resolve paths to Zotero's storage directory and serve them through a FastAPI endpoint that streams the file.

## Database schema (app.sqlite)

```sql
-- Chat threads: one per collection. Paper-clicks add to the thread's context, not new threads.
CREATE TABLE chat_threads (
  id INTEGER PRIMARY KEY,
  collection_slug TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE chat_messages (
  id INTEGER PRIMARY KEY,
  thread_id INTEGER REFERENCES chat_threads(id),
  role TEXT CHECK(role IN ('user','assistant','system')),
  content TEXT NOT NULL,
  -- JSON array of context refs: {type:'paper'|'wiki'|'note', id:'...'} used in this turn
  context_refs TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Per-paper notes. Structured fields, not freeform.
CREATE TABLE paper_notes (
  zotero_key TEXT PRIMARY KEY,
  collection_slug TEXT NOT NULL,
  summary TEXT,           -- user's TL;DR of the paper
  thoughts TEXT,          -- user's take, criticisms, connections
  key_quotes TEXT,        -- quotes the user wants to remember (markdown list)
  status TEXT CHECK(status IN ('unread','reading','noted','superseded')) DEFAULT 'unread',
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Triage queue for papers found by arxiv-daily, awaiting user accept/reject.
CREATE TABLE triage_items (
  id INTEGER PRIMARY KEY,
  collection_slug TEXT NOT NULL,
  zotero_key TEXT,        -- if already added to Zotero by arxiv-daily
  arxiv_id TEXT,
  title TEXT,
  abstract TEXT,
  authors TEXT,
  llm_relevance_note TEXT,-- LLM's pitch for why this fits the collection's wiki
  status TEXT CHECK(status IN ('pending','accepted','rejected','deferred')) DEFAULT 'pending',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sync state: when was each collection last synced from Zotero, last regenerated, etc.
CREATE TABLE sync_state (
  collection_slug TEXT PRIMARY KEY,
  last_zotero_sync TIMESTAMP,
  last_wiki_regen TIMESTAMP,
  zotero_collection_id INTEGER
);

-- Full-text search over notes and wiki.
CREATE VIRTUAL TABLE notes_fts USING fts5(
  zotero_key, collection_slug, summary, thoughts, key_quotes,
  content='paper_notes', content_rowid='rowid'
);
```

## Feature list (build in phases)

### Phase 0 — Skeleton

**Goal**: prove the stack works, get a page rendering, get to Zotero.

- [ ] Project scaffold: `pyproject.toml`, `app/`, `templates/`, `static/`, `tests/`.
- [ ] FastAPI app with two routes: `GET /` (lists Zotero collections), `GET /healthz`.
- [ ] Zotero adapter (`app/zotero.py`): function `list_collections()` returning `[(id, name, parent_id), ...]`. Try HTTP API first (port 23119, path `/api/users/0/collections`), fall back to SQLite.
- [ ] HTMX-rendered collection list. Click a collection → goes to `/c/<slug>` (404 stub OK).
- [ ] Settings page (`/settings`): OpenAI key, model name, Zotero path. Persists to `config.toml`.
- [ ] Initialize `app.sqlite` with schema on first run.

**Stop after Phase 0 and show me the working skeleton before continuing.**

### Phase 1 — Read a collection, view papers, view a PDF

- [ ] `GET /c/<slug>`: collection landing page. For now, just lists papers in the collection (title, authors, year) pulled from Zotero. No wiki yet.
- [ ] Slug resolution: collection name → slug (`Vision-Language Models` → `vision-language-models`). Store mapping in `sync_state`.
- [ ] `GET /c/<slug>/p/<zotero-key>`: paper view. Two-column layout — PDF on left (PDF.js), placeholder for chat on right.
- [ ] `GET /pdf/<zotero-key>`: streams the PDF from Zotero's storage directory. 404 if no PDF attached.
- [ ] CSS grid for the two-column layout. Resizable divider is nice-to-have, not required for v1.

**Checkpoint with me here.** I want to see how PDF.js feels in the layout before we add chat.

### Phase 2 — Chat (collection-scoped, paper-aware)

- [ ] `app/llm.py`: thin OpenAI wrapper with `complete(messages, model=None) -> str` and streaming variant `stream(messages)`.
- [ ] One chat thread per collection. New thread auto-created on first message.
- [ ] `POST /c/<slug>/chat`: HTMX endpoint, accepts a user message, returns streamed assistant response (server-sent events or htmx-ext-sse). Falls back to non-streaming if too complex.
- [ ] Context assembly for each turn:
  1. System prompt: "You are a research assistant for a collection on `<purpose.md content>`. The user's current thinking is in `<latest 3 thoughts/>`. The wiki currently says: `<index.md + relevant sections>`."
  2. If a paper is currently open in the view, inject: paper metadata, user's notes for it (if any), and the first ~8k chars of the PDF text.
  3. Last N (default 10) messages of the thread.
- [ ] Render messages as markdown. Wikilinks in assistant output become real links.
- [ ] **Important**: chat does **not** auto-update the wiki or notes. It can suggest edits (next phase), but it is read-only with respect to artifacts in this phase.

### Phase 3 — Per-paper notes (structured)

- [ ] `GET /c/<slug>/p/<key>/notes`: structured form with three fields — Summary, Thoughts, Key Quotes (markdown). Status dropdown: unread/reading/noted/superseded.
- [ ] `POST /c/<slug>/p/<key>/notes`: saves to `paper_notes`. Also writes a mirror markdown file to `collections/<slug>/notes/<key>.md` with YAML frontmatter (status, updated_at, zotero_key) — so the user can edit it in Obsidian or any text editor and the next read picks up changes. Last-write-wins; file mtime is the tiebreaker.
- [ ] "Draft notes from chat" button on the paper view: takes the recent chat turns about this paper, asks the LLM to draft Summary / Thoughts / Key Quotes fields, **opens them as a draft for the user to edit and accept**. Never auto-saves.

### Phase 4 — Thoughts stream

- [ ] `GET /c/<slug>/thoughts`: list of timestamped thought entries, newest first. Each is a markdown file in `thoughts/`.
- [ ] `POST /c/<slug>/thoughts`: create a new thought entry. Filename = ISO timestamp.
- [ ] Edit / delete / mark-superseded for each entry. Superseded entries move to `thoughts-archive/` but are not deleted.
- [ ] "Consolidate old thoughts" command: pick a date range, LLM proposes a synthesized markdown summarizing those thoughts. User reviews, accepts → originals move to archive, synthesized doc lands in `thoughts/` as a new entry tagged `consolidated`.

### Phase 5 — Wiki generation (the careful part)

This is the feature that most easily goes wrong. Read this whole section before writing code.

- [ ] Wiki has four mandatory sections plus a free synthesis area:
  - `wiki/problems/` — research problems in the field
  - `wiki/methods/` — how existing methods address them
  - `wiki/gaps/` — unaddressed problems (grounded in papers' stated limitations + user's thoughts; never invented)
  - `wiki/benchmarks/` — datasets/benchmarks in use
  - `wiki/synthesis/` — free-form cross-cutting notes
- [ ] `wiki/index.md` lists all pages with one-line summaries. Auto-maintained.
- [ ] `wiki/log.md` records every generation event with a timestamp, a short reason, and a hash of inputs. Append-only.
- [ ] Every wiki page has YAML frontmatter: `type`, `title`, `sources: [zotero-key, ...]`, `derived_from_notes: [zotero-key, ...]`, `derived_from_thoughts: [timestamp, ...]`, `last_regen`.
- [ ] **Two-step generation, modeled on llm_wiki's analyze→generate**:
  1. *Analyze*: LLM reads `purpose.md`, all `notes/*.md`, recent `thoughts/*.md`, current `wiki/`. Outputs a structured analysis: what's new/changed since last regen, which sections need updates, which papers/notes/thoughts ground each proposed change.
  2. *Generate*: LLM takes the analysis and produces proposed page updates as **diffs**, each tagged with which notes/thoughts/papers it cites.
- [ ] Proposed updates land in `proposed-edits/` as files like `<timestamp>-<page-path>.diff`. They are **not applied automatically**.
- [ ] `GET /c/<slug>/proposed`: review queue. Each item shows the diff with a per-claim "supported by: [note from paper X, thought from date Y]" provenance. Accept / Edit / Reject buttons.
- [ ] Accept: writes the new file, updates `index.md`, appends to `log.md`.
- [ ] Two regeneration triggers:
  - "Full rebuild" — regenerate every section from scratch. Slow, expensive, but the source of truth. Default for first generation.
  - "Incremental" — only consider notes/thoughts changed since last regen. Faster. Always produces diffs against the *current* wiki (not against a previous LLM draft) to avoid drift.
- [ ] Guardrail: the LLM must cite at least one note or thought for each claim it adds. Claims without provenance get filtered out before the diff is shown to the user. This is enforced by post-processing the LLM output, not by trusting the LLM.

### Phase 6 — Update wiki from conversation

- [ ] After every assistant turn in the chat, run a cheap classifier prompt: "did this turn contain corrections, new insights, or claims that should update the wiki? Yes/no + which wiki page."
- [ ] If yes, append a candidate to a per-thread "suggested edits" panel visible next to the chat. The user can click "draft edit" which moves it to the standard `proposed-edits/` review queue with the chat turn as provenance.
- [ ] Same guardrail: edits proposed from chat must cite either a note, a thought, or a paper. Pure LLM-asserted facts without grounding are rejected.

### Phase 7 — Triage (Feature 6)

- [ ] Background job (`uvicorn` doesn't need a separate runner; use FastAPI `BackgroundTasks` or APScheduler) that checks for new papers in a designated Zotero "inbox" location per collection. Configurable in `purpose.md` frontmatter: `inbox_collection: <name>` or `inbox_tag: <tag>`.
- [ ] For each new candidate, LLM generates a 2-3 sentence relevance pitch grounded in the current wiki and purpose. Stored in `triage_items.llm_relevance_note`.
- [ ] `GET /c/<slug>/triage`: list of pending candidates with title, abstract, pitch, and an inline mini-PDF preview (first page only).
- [ ] Accept → moves the paper from inbox to the main collection in Zotero (HTTP API write, requires Zotero running). Reject → tags it in Zotero so arxiv-daily won't re-suggest. Defer → keeps in queue.

### Phase 8 — Gap detection and curation prompts (Feature 3)

- [ ] "Find gaps" command on the wiki page: LLM reads the wiki + recent literature in the field (via arxiv search API, no web search in v1) and proposes papers that would fill stated gaps. Output: a list of arxiv IDs with relevance notes. The user can send any of them to the triage queue.
- [ ] "Stale paper" detector: monthly background check. For each paper in the collection, count its appearances in `notes/`, `thoughts/`, and `wiki/`. Papers with zero appearances after 90 days get flagged. The user gets a list — **no automatic removal**, ever. The user decides.

## Cross-cutting requirements

- **PROGRESS.md** in repo root. After each phase, append a section: date, phase, what was built, what was deferred, any decisions made that weren't in the spec.
- **Git**: init on first commit. Make a commit per phase with a clear message. No remote yet; the user will add one later.
- **Minimal dependencies**: prefer stdlib. Justify each new dependency in PROGRESS.md.
- **Testing**: `pytest`. At minimum, integration tests for the Zotero adapter (using a fixture sqlite) and the diff-proposal pipeline (using a mocked LLM). Don't over-test the HTMX views.
- **Logging**: structured logs to stdout. Every LLM call logged with token counts and latency. Every wiki write logged with what triggered it.
- **No telemetry, no analytics. No remote calls from the app itself**: the LLM runs locally via the Claude Code / Codex CLI (which talk to their own providers); the app's only direct network use is local Zotero and (Phase 8) arxiv.org/openreview.
- **Networked Zotero (deferred but design for it)**: the Zotero adapter must be a class with two implementations — `LocalZotero` (current) and `WebZotero` (stub). The rest of the code only touches the abstract interface. Adding networked support later should be a new file, not a refactor.

## What you must not do without asking

- Don't add a vector store. Don't add embeddings. The user asked specifically.
- Don't write to the wiki directly from any code path other than "user accepts a proposed edit." Including from chat. Including from notes. Including from arxiv-daily. (Sole exception: the user-approved abstract-seeded starter overview `wiki.generate_overview` → `wiki/overview.json` — non-destructive, agent-tagged, ref-validated. See the amendment under "Philosophical anchor".)
- Don't modify Zotero's database directly. Only the HTTP API for writes.
- Don't add a separate frontend framework. HTMX + Alpine is the contract.
- Don't add authentication, user accounts, or multi-user features. Single local user.
- Don't try to make this work without Zotero. Zotero is a hard dependency.
- Don't auto-trigger expensive operations (full wiki rebuild, embedding all PDFs). Always require explicit user action for anything that costs significant tokens.

## When to stop and ask

- Anything that requires a design decision not in this doc.
- Anything where the obvious implementation would silently mutate user data.
- Any time you'd need to add a dependency outside: fastapi, uvicorn, httpx, markdown-it-py (or mistune), pypdf, pyzotero (optional), jinja2, pytest. (No `openai` — the LLM is a CLI subprocess.)
- If a phase took longer than expected and a feature is half-done, stop and report rather than partially shipping.

## First steps for you, Claude Code

1. Read this whole file again.
2. Create `PROGRESS.md` with an empty Phase 0 section.
3. Build Phase 0 only. Do not start Phase 1.
4. Run the app, screenshot or describe what works, and stop.