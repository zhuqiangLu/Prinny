# PROGRESS

## Phase 0 — Skeleton — 2026-05-22

**Built**
- Project scaffold: `pyproject.toml`, `app/`, `templates/`, `static/`, `tests/`.
- FastAPI app (`app/main.py`): `GET /` (collection list), `GET /healthz`,
  `GET/POST /settings`, `GET /c/{slug}` (Phase 1 stub).
- Zotero adapter (`app/zotero.py`): abstract `ZoteroBackend` + `LocalZotero`
  (HTTP-first, SQLite fallback) + `WebZotero` stub. `list_collections()` returns
  `Collection(id, name, parent_id)`.
- Config (`app/config.py`): load/save `~/.paper-agent/config.toml`
  (OpenAI key, model, Zotero paths). Read via stdlib `tomllib`, written via a
  tiny hand-rolled serializer.
- DB (`app/db.py`): initializes `app.sqlite` with the full Phase-0 schema from
  CLAUDE.md on startup (idempotent).
- HTMX/Tailwind/Alpine templates (CDN, no build step). Collection list links to
  `/c/{slug}`; nesting shown by depth.
- Integration test for the Zotero SQLite read path against a fixture DB.

**Environment findings**
- Zotero is installed; SQLite at `~/Zotero/zotero.sqlite` reads fine in
  `mode=ro&immutable=1` even while Zotero is running.
- Local HTTP API on port 23119: Zotero **is running** (connector `/connector/ping`
  → 200) but the **Local API is disabled** — `/api/users/0/collections` → 403
  "Local API is not enabled". To enable HTTP (needed for Phase 7 write-back):
  Zotero → Settings → Advanced → "Allow other applications on this computer to
  communicate with Zotero". Until then, SQLite fallback is used.
- The earlier "502" was a red herring: the user has `http_proxy`/`https_proxy`
  set to `127.0.0.1:7897`, and curl/httpx routed localhost through it. Fixed in
  the adapter: `LocalZotero` now makes its httpx calls with `trust_env=False` so
  local Zotero calls never go through a proxy. `http_available()` probes the real
  data endpoint and requires HTTP 200.

**Decisions / deviations**
- Added `python-multipart` as a dependency (not in the spec's allowed list) — it
  is required by FastAPI to parse the settings form POST. Justified as a
  mandatory transitive need for HTML forms.
- Slug↔name persistence in `sync_state` is deferred to Phase 1; Phase 0 computes
  slugs deterministically for links only.

**Deferred**
- Everything Phase 1+ (collection contents, papers, PDF, chat, notes, wiki…).

**How to run**
- `pip install -e .` then `uvicorn app.main:app --reload`, open http://127.0.0.1:8000
- Tests: `pytest`

## Phase 1 — Read a collection, view papers, view a PDF — 2026-05-22

**Built**
- `GET /c/{slug}` — real collection page listing papers (title, authors, year,
  PDF badge), pulled from Zotero via the adapter.
- Slug resolution persisted to `sync_state` (`app/repo.py:resolve_collection`):
  slug → Zotero integer `collectionID`, with `last_zotero_sync` stamped.
- `GET /c/{slug}/p/{key}` — two-column paper view (CSS grid): PDF.js canvas
  renderer on the left, Phase-2 chat placeholder on the right.
- `GET /pdf/{key}` — streams the attached PDF from Zotero's storage dir via
  `FileResponse` (inline). 404 when no PDF.
- Zotero adapter extended (`app/zotero.py`): `Paper` dataclass +
  `resolve_collection_id`, `list_papers`, `get_paper`, `pdf_path` (SQLite).
  PDF paths resolve `storage:<file>` → `<zotero>/storage/<attachmentKey>/<file>`;
  linked-file absolute paths supported. Notes/attachments/annotations excluded
  from paper lists; deleted items excluded.
- PDF.js 3.11.174 via cdnjs, rendering each page to a `<canvas>` fit to pane width.
- Tests: paper list (note excluded), `get_paper`, `pdf_path` storage resolution,
  slug→id resolution — against a fuller fixture library DB. 4 tests pass.

**Decisions / deviations**
- Paper reads are **SQLite-canonical**. The HTTP API is disabled in this Zotero
  (Local API off), and reads need the integer `collectionID`, so paper/PDF reads
  go through SQLite even though `list_collections` can dispatch to HTTP. Revisit
  read consistency if/when the HTTP API is enabled (Phase 7 needs it for writes).
- Removed `collection_stub.html` (replaced by real `collection.html`).

**Verified against live Zotero**
- `/c/longvideo` → 13 real papers; `/pdf/<key>` → 200, `application/pdf`, ~28 MB;
  `sync_state` row `('longvideo', 12)` written.

**Deferred**
- Resizable divider between columns (spec: nice-to-have, not required).
- Abstract reads / per-paper metadata beyond title/authors/year (not needed yet).

## Phase 2 — Chat (collection-scoped, paper-aware) — 2026-05-22

**Built**
- `app/llm.py` — thin OpenAI wrapper: `complete(messages, model=None)` and a
  `stream()` generator. Lazy OpenAI import (app boots without a key). Every call
  logs model, latency, and token counts. `LLMError` for missing key. OpenAI uses
  env proxy settings (external service), unlike the proxy-bypassed Zotero calls.
- One chat thread per collection, auto-created on first message
  (`app/repo.py`: `get_or_create_thread`, `add_message`, `get_messages`).
- `POST /c/{slug}/chat` — HTMX endpoint. **Non-streaming** (the spec's sanctioned
  fallback): returns an HTML fragment with the user bubble + assistant reply,
  appended to the chat log via `hx-swap=beforeend`.
- Context assembly (`app/context.py`): system prompt from `purpose.md` + latest 3
  `thoughts/` + `wiki/index.md`; if a paper is open, its metadata + the user's
  notes + ~8k chars of PDF text. Degrades gracefully — those artifacts don't
  exist until later phases. **Strictly read-only** w.r.t. user artifacts.
- PDF text via `app/pdf_text.py` (pypdf, best-effort, budget-capped).
- Markdown rendering with server-side `[[wikilink]]` → `/c/{slug}/wiki/{page}`
  (`app/markdown.py`, markdown-it-py + Tailwind typography plugin).
- Chat panel (`_chat.html`) embedded in both the collection page (collection-
  scoped) and the paper view (paper-aware, passes `paper_key`). Enter-to-send.
- Context refs (`{type, id}`) for the paper/note used are stored on the user
  message row (`chat_messages.context_refs`).
- Guardrail honored: **chat does not write to notes or the wiki.**
- Tests (mocked/LLM-free): thread + message storage and ordering; context
  assembly grounds the prompt in the user's purpose/thought/paper and records
  refs; graceful degradation with no artifacts. 7 tests pass total.

**Decisions / deviations**
- Added deps (approved): `openai`, `markdown-it-py`, `pypdf`. `pypdf` was an
  explicit out-of-list choice the user approved for PDF-text context.
- Non-streaming endpoint instead of SSE — spec permits the fallback; chosen for
  reliability and to render markdown + wikilinks server-side. Streaming is wired
  in `llm.stream()` and can drive the UI later.
- Failed turns (e.g. no API key) persist nothing, so threads never hold an
  orphan user message without a reply.

**Needs the user**
- A real chat reply needs an OpenAI key on the Settings page. No-key path is
  verified (clean error bubble linking to Settings); a live call is the
  checkpoint to try in the browser.

**Deferred**
- True token streaming in the UI; suggested-edits-from-chat (Phase 6).

## Phases 3–8 — built together — 2026-05-22

Implemented in one pass at the user's request. 15 tests pass; all non-LLM routes
verified live against Zotero; LLM routes verified to degrade gracefully without a
key (no 500s).

### Phase 3 — per-paper notes (`app/notes.py`)
- Structured form (Summary / Thoughts / Key Quotes / status) at
  `GET|POST /c/{slug}/p/{key}/notes`. Saves to `paper_notes` AND mirrors
  `collections/{slug}/notes/{key}.md` with frontmatter.
- DB↔file two-way sync, last-write-wins, **file mtime as tiebreaker** — an
  out-of-band Obsidian edit (newer mtime) wins on next read and syncs back to DB.
- FTS index kept current via triggers added to the schema (`db.py`).
- "Draft notes from chat" (`/notes/draft`): LLM drafts the fields into the form;
  **never auto-saves** — the user reviews and saves.

### Phase 4 — thoughts stream (`app/thoughts.py`)
- Timestamped markdown entries under `thoughts/`; list/create/edit/delete.
- Supersede moves to `thoughts-archive/` (not deleted).
- Consolidate: LLM proposes a synthesis of selected entries → user accepts →
  originals archived, new `consolidated`-tagged entry written.

### Phase 5 — wiki generation (`app/wiki.py`) — the careful part
- Two-step **analyze → generate** (modeled on llm_wiki).
- Generation returns structured **claims with provenance**; the guardrail
  `_filter_claims` (pure, unit-tested) **drops any claim not citing a real note
  or thought** — enforced in code, not by trusting the LLM.
- Proposals written to `proposed-edits/*.json` (page, old/new content, per-claim
  provenance, and an embedded unified diff). **Nothing is applied automatically.**
- Review queue `GET /c/{slug}/proposed`: shows the diff + per-claim "supported
  by", with Accept / Accept-edited / Reject.
- `accept_proposed` is the ONLY path that writes into `wiki/`; it also rebuilds
  `index.md` and appends to `log.md` (timestamp, reason, inputs hash).
- Full rebuild and incremental triggers; diffs always against the current wiki.

### Phase 6 — wiki edits from chat (`app/suggest.py` + `wiki.proposal_from_chat`)
- After an assistant turn that cited context, a cheap classifier flags whether it
  should update the wiki and which section; a "Draft edit →" chip appears.
- Drafting routes the turn into the standard review queue. Guardrail: the turn
  must cite a note/thought/paper (carried as the turn's `context_refs`); pure
  LLM assertions are rejected.

### Phase 7 — triage (`app/triage.py`)
- Inbox configured via `purpose.md` frontmatter (`inbox_collection`/`inbox_tag`).
- `Scan inbox` (cheap, no LLM) enqueues new candidates; relevance **pitch is
  generated on demand** (per "don't auto-trigger expensive operations").
- Accept/Reject/Defer update the local queue; Accept→move and Reject→tag attempt
  Zotero write-back and surface a clear notice that the Local API write path is
  unavailable (see deviations).
- First-page PDF preview via the existing `/pdf/{key}` endpoint.

### Phase 8 — gaps + stale (`app/discover.py`)
- `Find gaps`: LLM forms an arXiv query from purpose+gaps, searches arXiv (Atom
  parsed with stdlib `xml.etree`), LLM picks gap-fillers → user can send to triage.
- `Stale papers`: cheap scan; flags papers added >90 days ago with zero
  appearances in notes/thoughts/wiki. **Never removes anything.**

### Decisions / deviations (logged per the spec)
- **Frontmatter parser hand-rolled** (`app/frontmatter.py`) to avoid a PyYAML
  dependency; our frontmatter only uses scalars + flat lists.
- **Proposed edits stored as JSON, not raw `.diff` files** — a unified diff can't
  carry per-claim provenance, which the review queue requires. The diff is
  embedded in the JSON for display.
- **No background scheduler** (would need APScheduler, outside the allowed deps).
  Triage scan, gap-finding, and stale detection are **on-demand buttons** — this
  also honors "don't auto-trigger expensive operations." The spec's "background
  job" framing is the one place I chose a manual trigger instead; flag if you want
  a real scheduler and I'll ask about the dependency.
- **Zotero write-back (triage Accept/Reject) is not yet implemented** — the Local
  API is disabled here so it's untestable; methods raise `ZoteroWriteError` with a
  clear message and the local queue still updates. Implementing the Local API
  write protocol (item versions, If-Unmodified-Since-Version) is the remaining
  work, gated on enabling the API.
- Phase 6 suggestions are transient (no new table); context refs are stored on
  the assistant message so a draft can be grounded.

### Needs the user
- An OpenAI key for any LLM flow (chat, draft-notes, consolidate, wiki generate,
  pitch, gaps). All verified to degrade gracefully without one.
- Zotero Local API enabled for triage write-back to Zotero.

## Addendum Phase 1.5 — Annotation overlay + Zotero read-in — 2026-05-22

Built per the Capture & Annotation addendum. **Annotation authority = the app
(Option A):** we author into our own store and read Zotero's annotations out
one-way; we do NOT write annotations back to Zotero in v1. `# WRITEBACK-TODO`
seams are marked in `app/annotations.py` and the data model mirrors Zotero's
`{pageIndex, rects}` PDF-point shape so write-back can be added later without a
data-model change.

**Built**
- Schema: `annotations` table + `idx_annotations_paper` (in `db.py`).
- `app/annotations.py` — app-side store: create/list/get/update/delete +
  `list_all` (app + Zotero read-in) + `to_client`. Delete/update reject
  Zotero-origin rows (read-only).
- `app/zotero.py: LocalZotero.read_annotations(key)` — reads `itemAnnotations`
  for the paper's PDF attachment (type 1 → highlight, hex color, `{pageIndex,
  rects}` position, text + comment). Read-only.
- Endpoints: `GET/POST /c/{slug}/p/{key}/annotations`, `PATCH /annotations/{id}`,
  `DELETE /annotations/{id}`. JSON API (overlay is positioned client-side from
  PDF coordinates, so HTMX swaps don't fit — documented deviation).
- Viewer (`templates/paper.html` + `static/annotate.js`): each page rendered as
  canvas + selectable **PDF.js text layer** + annotation overlay. Selection shows
  a floating **[Highlight] [Note]** toolbar (no modal). Highlight uses the current
  default color; Note creates the highlight then a one-line inline input (Enter
  saves, Esc keeps the highlight). App highlights are clickable → popover with the
  note + delete. Zotero annotations render with a dashed outline + "Z" badge,
  read-only.
- Color palette/legend (yellow/green/red/blue) sets the default color; **never
  required** — lazy all-yellow path works.

**Verified live**
- On a real VLMs paper with a Zotero annotation: `GET` returns the Zotero
  highlight (correct page/rects/text); `POST` app highlight, `PATCH` note,
  `DELETE` app all work; deleting a missing/Zotero annotation → 403. 18 tests pass
  (incl. app CRUD + Zotero read-in parse against a fixture).

**Deviations**
- Annotation endpoints return JSON (not HTMX fragments) — canvas-coordinate
  overlays are positioned client-side; HTMX swaps can't place them.
- The `[Ask]` toolbar button (highlight→chat) is **Phase 2.5**, not built here.
- Color taxonomy modes (2-color vs 4-color settings toggle) and feeding color
  into wiki generation are Capability 2 / the Phase 5 amendment — not in 1.5.

**Checkpoint**: needs the user to verify highlighting feels right on a real PDF in
the browser before Phase 2.5.

## Wiki hardening — merge / lint / scalable retrieval — 2026-05-22

Closed three gaps found by comparing `app/wiki.py` against `llm_wiki`'s mature
generation engine (analysis via an Explore agent over `llm_wiki/src`).

1. **Page-merge on regenerate** (`wiki.py:_merge_into`, `_merge_fallback_body`,
   `_merge_body_llm`). When generation targets a page that already exists,
   `run_generation` now MERGES rather than rebuilds, so a user's hand-edits are
   never clobbered. Frontmatter provenance lists are unioned deterministically;
   the body is merged by LLM when available, else by a safe deterministic
   fallback that keeps the user's body verbatim and appends only new,
   not-already-present grounded claims under a "## New" heading. The user still
   reviews the old→merged diff before accepting.
2. **Structural lint** (`wiki.py:lint_wiki`) — offline, no LLM: flags broken
   `[[wikilinks]]`, orphan pages (no inbound links), and no-outlink pages.
   Surfaced as a "Wiki health" panel at the top of the review queue
   (`/c/{slug}/proposed`).
3. **Scalable retrieval** (`wiki.py:_select_notes`, `_select_thoughts`,
   `_fts_query`). Instead of dumping every note into the prompt, generation now
   selects the most relevant notes via **FTS5 bm25** ranking (query built from
   purpose + section names) within a char budget (`NOTE_CHAR_BUDGET`), falling
   back to recency for unmatched notes; thoughts are taken most-recent-first
   within `THOUGHT_CHAR_BUDGET`. Small collections still feed everything. This
   honors SPEC.md (FTS5, no vector store) while bounding prompt size.
   Provenance validity is computed against the notes actually shown to the LLM,
   so a cite to an unseen note is treated as a hallucinated cite and dropped.

Tests: +5 (merge no-clobber, frontmatter union, regenerate-merges-existing,
lint broken/orphan/outlink, FTS budget+relevance). **23 tests pass.** All routes
verified live (proposed/wiki 200, generate 303), no errors.

Note: these are quality techniques borrowed from `llm_wiki` that *serve* this
project's "user is the author" anchor (esp. never clobbering hand-edits). We did
NOT adopt llm_wiki's source→entity/concept extraction pipeline — that's the
LLM-as-author pattern SPEC.md inverts.

## Reading-view rework — two chat contexts, per-paper threads, Note modal — 2026-05-23

User-driven redesign of the reading experience (from a hand-drawn UI). **Deviates
from SPEC.md's "one chat thread per collection, paper turns auto-attached"** — a
deliberate, user-requested change.

**New chat model (one mechanism, two contexts):**
- **Collection chat** (collection page): one thread, `zotero_key IS NULL`, context
  = the wiki (+ purpose + thoughts). `context.collection_system_prompt`.
- **Per-paper chat** (reading view): each paper has its **own thread**
  (`chat_threads.zotero_key` set — added via idempotent migration in `db._migrate`).
  Context = that paper's metadata + notes + **highlights** + PDF excerpt + its own
  history. `context.paper_system_prompt` + `paper_block`.
- **`/collection` (alias `/wiki`) command** in a per-paper message injects the
  collection/wiki context for that turn (`main.chat_post` parses it →
  `build_messages(..., include_collection=True)`). Per-turn, not sticky.

**Reading view UI (`templates/paper.html`):** top toolbar = `← Return` ·
highlight-rule color palette · **API-usage token counter** · **📝 Note** button;
two panes (PDF | per-paper chat). Replaces the old plain header.

**Note (renamed from "summary"):** a **modal** (Alpine) on the reading page with
Summary/Thoughts/Quotes/status. **"Draft from highlights + chat"**
(`POST /c/{slug}/p/{key}/note/draft.json`, `_draft_note_fields`) fills the fields
from the paper's highlights + its chat thread; the user edits and **Saves** (via
the existing note store + markdown mirror). No auto-popup on Return (honors
"never nag"). The standalone `/notes` page remains as a secondary path.

**API usage** (`llm.usage()`): cumulative process token tally, shown in the
toolbar and live-updated after each chat turn via an HTMX out-of-band swap of
`#api-usage` (in `_chat_turn.html`).

**Highlights → context:** `context._highlights_block` feeds app highlights (with
color meaning as a soft signal) into both per-paper chat and the note draft.

Tests: chat-context test rewritten for the two-context model (+ `/collection`
injection). **23 pass.** Live-verified: migration adds `zotero_key`; reading view
renders new toolbar; per-paper thread created; note draft + chat degrade
gracefully without a key; no errors.

**Still pending the user:** an OpenAI key to see real per-paper chat / note drafts;
PDF resolution + highlight fixes (`--scale-factor` + supersampling) need a
hard-refresh to confirm in the browser.

## Reading view polish — full-width, text-layer alignment, highlight manager — 2026-05-23

- **Full-width reading view**: `base.html` main width is now an overridable
  `{% block main_width %}`; `paper.html` sets `max-w-none` so the PDF + chat use
  the whole window (other pages keep centered `max-w-6xl`). PDF fit-scale cap
  raised 2→3 so wide panes fill instead of leaving left/right gutters.
- **Text-layer alignment fix**: set `--scale-factor` on the page wrapper (PDF.js
  3.x reads it from an ancestor) and pass `textContentSource` to
  `renderTextLayer`. This addresses selection/highlight misalignment that appeared
  once the fit-scale grew (full-width). NOTE: verified by code/canonical setup,
  not yet eyeball-confirmed in the browser — awaiting user retest.
- **Highlight manager (per-paper)**: 🔖 Highlights toolbar button opens a modal
  listing the paper's app highlights with snippet + note; per-item **recolor**
  (4-color palette), **edit note**, **jump to page**, and **delete** — live-updates
  the PDF overlay (rects tagged `data-ann-id`). Reuses the existing
  GET/PATCH/DELETE annotation endpoints. Zotero-origin highlights remain read-only.

23 tests pass; CRUD + UI elements verified live.

## Spike — prebuilt PDF.js viewer (beta) — 2026-05-23

Evaluating the full prebuilt viewer alongside our custom one (custom stays default).
- Self-hosted PDF.js **3.11.174** prebuilt dist under `static/pdfjs/` (8.8 MB;
  source maps + sample PDF trimmed). Same-origin so the parent can read the
  iframe's text selection.
- `GET /c/{slug}/p/{key}/beta` → `paper_beta.html`: left pane is an `<iframe>` to
  `/static/pdfjs/web/viewer.html?file=/pdf/{key}` (free zoom/search/page-nav/print),
  right pane is the per-paper chat.
- **Selection→chat capture**: on `mouseup` in the same-origin iframe, a parent-side
  "💬 Ask in chat" button appears at the selection; clicking it quotes the selected
  text into the chat box (the easy half of the highlight→chat gesture).
- Reachable via a **"Beta viewer"** link in the custom viewer's toolbar.
Verified live: viewer.html + worker serve (200), beta route renders the iframe +
Ask button, custom viewer links to it. 23 tests pass.

### Persistent highlights ON the prebuilt viewer (`static/beta-annotate.js`)

Wired full highlight authoring + persistence into the beta viewer — the part I
expected to be hard turned out clean because the viewer is same-origin:
- Waits for `PDFViewerApplication.initializedPromise`, then hooks the eventBus.
- Draws an `.app-ann-layer` overlay into each page div; redraws on `pagerendered`,
  `textlayerrendered`, and `pagesloaded`. Because rects are stored in **PDF
  coordinates** and re-projected via the page's current `viewport`, it is
  **zoom- and scroll-safe** (zoom re-renders pages → redraw at new scale) and
  survives page virtualization.
- Selection → floating **[Highlight] [Ask] [Note]** toolbar (parent-side,
  positioned over the iframe). Ask quotes the selection into the per-paper chat.
- Color palette sets the default; reuses the GET/POST/PATCH/DELETE annotation
  endpoints (no Zotero write-back — same Option A store as the custom viewer).
- 🔖 Highlights **manager** modal: list / recolor / edit-note / jump
  (`pdfViewer.scrollPageIntoView`) / delete, with live overlay redraw.
- Zotero-origin highlights render read-only (dashed + no manage controls).

So the beta viewer now has zoom/search/page-nav/print **and** persistent app
highlights. Verified: persistence API round-trips (stored w/ correct pageIndex,
retrievable). Browser-side render/zoom-redraw needs eyeball confirmation.
**Open decision (TODO):** make beta the default and retire the custom viewer?

### Prebuilt viewer promoted to default; custom viewer removed (2026-05-24)

Resolved the open decision: the prebuilt PDF.js viewer is now the default and
only viewer.
- `static/beta-annotate.js` → renamed to `static/annotate.js` (the old custom
  canvas script it replaces). Header comment updated.
- `templates/paper.html` rewritten to host the prebuilt viewer iframe
  (`/static/pdfjs/web/viewer.html?file=/pdf/<key>`) plus the rich Note modal
  (draft-from-highlights+chat) and the highlight manager. The CDN pdf.js
  `<script>`/CSS and custom-viewer markup are gone.
- Deleted `templates/paper_beta.html` and the `/c/<slug>/p/<key>/beta` route.
  The earlier offset bug (page 9px border) stays fixed.

Highlight manager gained two requested features:
- **Color filter** — chips (all / important / agree / disagree / unclear) filter
  the list to one color.
- **Multi-select batch edit** — a checkbox per highlight; selecting any reveals a
  batch bar to **recolor** (palette swatches) or **delete** the selection at once.
  Stale ids are pruned; affected pages redraw live. Per-row jump/note/recolor/
  delete still work.

`static/pdfjs/` (~8.8 MB vendored dist) is now a runtime dependency. 23 tests
pass; app imports clean.

### Highlight rendering + recolor fixes (2026-05-24)

- **"Looks strange" banding fixed.** `drawPage` now runs raw line-rects through
  `mergeRects()`: clusters rects by line (vertical-overlap > 50%), unions each
  line horizontally, and splits any vertical overlap between adjacent lines at
  the midpoint. This removes the `mix-blend-mode:multiply` double-paint banding
  at line boundaries. Applied at draw time → existing highlights look right too,
  no recreate needed.
- **Recolor is now discoverable.** Clicking an app highlight in the PDF opens a
  small inline popup (4 palette swatches + ✎ note + 🗑 delete) positioned at the
  click. Previously recolor was only reachable via tiny swatches in the 🔖
  manager, and the top-toolbar palette only sets the color for *new* highlights
  (unchanged). Zotero-origin highlights remain read-only (no popup).
- Browser-review agent could not run (Bash denied in its sandbox → no Playwright);
  fixes were made from the screenshot + code and verified by JS syntax check +
  app import. User to eyeball in-browser.

### Architecture decision: local-first paper store with Zotero sync (2026-05-25)

Design session (grilled) agreed to **invert the Zotero relationship**. Full plan in
`docs/adr/0001-local-first-paper-store-with-zotero-sync.md`. **Not yet implemented** —
this is a planning record.

In short: the app keeps its **own local store** (paper metadata + a complete PDF store),
populated by **import from Zotero** + **in-app discovery** (`discover.py`); the user works
locally; an explicit, manual **Sync to Zotero** pushes work back (additive by default,
destructive removals are a reviewed opt-in). Papers carry an app-owned id plus
`arxiv_id`/`zotero_key`/`origin`/`sync_status`. PDF store is one uniform directory
(configurable, may be a netdrive); copy-timing (eager/lazy) is a user choice at import.
Refresh is a non-destructive merge with provenance flags.

**Conscious deviations from CLAUDE.md** (recorded so they aren't mistaken for drift):
1. "Zotero is the source of truth / live dependency" → app is the working store; Zotero
   is canonical-of-record + sync target.
2. "PDFs are not copied" → abandoned; app keeps its own complete PDF store (duplicates
   Zotero's PDFs for imported papers — accepted cost).
3. "No write-back to Zotero in v1" → write-back is now a core feature (additive +
   reviewed removals).
4. "App must not work without Zotero" → app can now read/highlight with Zotero closed;
   Zotero is needed only at Sync time.
5. `zotero-arxiv-daily-local` is still **not** modified (we extend `discover.py`) — no
   deviation there.

Still open (not yet designed): data-model migration / re-keying existing
notes·annotations·chat·triage from `zotero_key` to app-id; highlight/note write-back
format; build order. Proposed build order in the ADR.

### Implemented: local-first paper store with Zotero sync (2026-05-25)

Built the full inversion from ADR 0001 (all phases, end-to-end). 31 tests pass;
browser-verified via Playwright MCP (landing cards, collection badges, sync preview,
settings). Clean-reset migration chosen, so old DBs are backed up + recreated.

New / changed:
- **`app/db.py`** — new schema: `papers` (app-owned id + arxiv_id/zotero_key/origin/
  sync_status/pdf_state), `collections`, `collection_papers` (membership +
  source_flag); work tables re-keyed to `paper_id`; FTS re-pointed. `init_db` backs
  up an old (Zotero-keyed) DB to `app.sqlite.bak` and recreates. `_migrate` removed.
- **`app/pdf_store.py`** (new) — uniform local PDF store `<store>/<id>.pdf`; resolver;
  eager copy / lazy `ensure_cached`; arXiv fetch; graceful when the store (netdrive)
  is absent (503 / False / None, never raises, never mkdirs an absent mount root).
- **`app/library.py`** (new) — papers/collections/membership; `upsert_paper` dedupe;
  `activate` + non-destructive `refresh` merge (Zotero wins on metadata; new flagged;
  removed flagged-not-deleted; local curation preserved); `download_all`;
  `sync_candidates`.
- **`app/sync.py`** (new) + real Zotero HTTP writes in `app/zotero.py`
  (`create_item`/`create_collection`/`add_item_to_collection`/`upload_attachment`/
  `remove_item_from_collection` via `_http_write`). `preview` (read-only) + `push`
  (additive by default; reviewed-removals opt-in).
- **Reads repointed off live Zotero** — `main.py` (URL token `/p/{paper_id}`, all
  routes read `library`), `repo.require_collection`, `context`, `annotations`,
  `notes`, `discover.find_stale`, `wiki` FTS/notes, all keyed by `paper_id`.
- **Discovery into the app** — `triage.accept`/`accept_arxiv_into_collection` import
  candidates locally (PDF pulled if eager); reach Zotero only via Sync.
- **UI** — `index.html` landing with collection cards + summaries + "Start a new
  collection" + activate-from-Zotero; `collection.html` provenance/sync badges +
  control strip; new `sync.html`; `paper.html`/`notes.html`/`annotate.js` key→id;
  three new Settings fields (pdf_store_path, zotero_write_api_base/key).

Still untested live (flagged in the ADR): the Zotero local **write** API shapes,
especially the 3-step PDF upload — requires Zotero running with a write key. The rest
is covered by unit tests with a fake Zotero + an end-to-end browser pass.

### Title repair for bare openreview imports (2026-05-25)

Papers imported as standalone openreview.net PDF attachments store the PDF URL as
their "title". Fixed two layers:
- **Standalone-attachment listing bug** (`zotero.py`): `list_papers` excluded all
  attachment-type items, so collections of openreview PDFs showed empty. Now top-level
  PDF attachments are listed as papers and their PDF/title resolve from the item
  itself (`_item_pdf`), with a filename title fallback. Also made `list_papers`
  recurse into subcollections. Regression tests added; validated against the real
  library (distill: 5 papers, memory: 39).
- **Automatic title repair** (`app/openreview.py` + `library.repair_title`): on import,
  a junk title (URL/`*.pdf`/placeholder) triggers an OpenReview API lookup
  (`api2`/`api` `/notes?id=<noteId>`) that returns the official title. `upsert_paper`
  now refuses to clobber a good local title with a junk incoming one, so Refresh won't
  revert the fix. Verified end-to-end on the real library.

**New deviation:** adds **openreview.net** as an allowed external call (alongside
OpenAI + arxiv.org). Best-effort: offline failures leave the URL title and retry on
the next Refresh. Only repairs openreview imports (the user's case); other junk titles
are left as-is.

### Delete a paper's PDF + move papers between collections (2026-05-25)

Two collection-curation actions, multi-select from the collection paper list:
- **Delete PDF** (`library.delete_pdf` → `pdf_store.remove_pdf`): unlinks a paper's
  cached `<store>/<id>.pdf` and marks `pdf_state='absent'`. The **paper row (title,
  metadata) and every collection membership are kept** — per the user's spec, "keep
  the paper title and its collection even though the pdf is removed". For
  Zotero/arXiv-backed papers this reverts them to the lazy/re-fetchable state (next
  open re-downloads); for a purely-local paper (no source key) it removes the only
  copy. Never deletes the paper or touches Zotero.
- **Move** (`library.move_paper`): shifts membership from one collection to another
  (add to target as `local`, remove from source). The PDF lives globally per paper-id,
  so it follows automatically; same-collection move is a no-op.

UI: checkboxes on each paper row in `collection.html` drive an Alpine `selected[]`; a
sticky bulk-action bar (Delete PDF · Move to…) appears when ≥1 is selected and posts to
`POST /c/{slug}/papers/delete-pdf` and `POST /c/{slug}/papers/move`. Delete confirms
first; move redirects to the target collection. 4 unit tests added (40 total pass).
No new dependencies, no schema change.

### Collection chat: compact-into-artifact + remove (2026-05-25)

Two shortcuts in the collection-level chat header (only when `paper_key` is empty):
- **⊟ Compact** (`POST /c/{slug}/chat/compact`): summarizes the current chat into a
  compact "artifact" (LLM, faithful condensation), **then clears the back-and-forth
  history**. The artifact is stored as the thread's single `system` message and shown
  as a pinned "📦 Compacted summary" card at the top of the chat log. It is injected
  into context for every future turn (`context.build_messages(..., artifact=)`), so it
  survives regardless of the 10-message history window — and re-compacting folds the
  prior artifact in (cumulative). History is cleared **only if the LLM call succeeds**,
  so a failed compaction never loses the conversation.
- **Remove** (`POST /c/{slug}/chat/delete`): deletes the current collection chat
  entirely (history + artifact); a fresh empty thread is created on next load.

Supporting changes: `repo.clear_messages` (drop messages, keep thread) and
`repo.get_artifact`; `repo.get_messages` now returns only user/assistant turns (the
stored system artifact is surfaced separately, not treated as history). No schema
change. 2 unit tests added (42 total pass).

### Add paper by arXiv + wiki/queues moved inline into the collection (2026-05-25)

**Add paper (arXiv):** new `discover.fetch_arxiv_metadata` (+ `normalize_arxiv_id` —
accepts bare/versioned ids, abs/pdf URLs, `arXiv:` prefix, old-style ids) and
`triage.add_arxiv_manual(slug, raw_id)`, which fetches metadata and stores the paper as
a **user-curated `app-created` / `local`** member (not a triage "suggested" candidate),
pulling the PDF if the collection is eager. Route `POST /c/{slug}/papers/add`; an "⊕ Add"
button + inline arXiv input on the Papers tab.

**Wiki/Proposed/Triage are now inline** in the collection's left column instead of
separate full pages (the full pages still work for direct URLs). The left column is
tabbed **Papers · Wiki · Triage**, with live count badges (proposed edits on Wiki,
pending candidates on Triage). Selecting Wiki/Triage HTMX-loads a fragment into
`#left-panel`; the wiki panel's "Review queue" opens the Proposed panel in place, and
page links open a single page inline. Mutations (Generate full/incremental, Find gaps,
gaps→add, Proposed accept/reject/edit, Triage scan/pitch/accept/reject/defer) hx-post
and re-render the panel — `HX-Request` branches the existing routes between a fragment
(inline) and the prior redirect (full page). New fragment GET routes:
`/wiki/panel`, `/wiki/{name}/panel`, `/proposed/panel`, `/triage/panel`
(`/wiki/panel` declared before `/wiki/{name}` so it isn't matched as a page name).
New partials: `_wiki_panel.html`, `_wiki_page_panel.html`, `_proposed_panel.html`,
`_triage_panel.html`. Header nav trimmed to keep only "Stale papers" as a link.

Verified in a real browser (Playwright): tab switching, Alpine `@click`/`x-data`
working inside HTMX-swapped panels, in-panel navigation, and KaTeX rendering after swap.
3 unit tests added (arXiv metadata parse + bad id, manual add); 45 total pass. No schema
change, no new dependency (arXiv Atom API already used by gap-finding).

### Sort papers + "show added date" setting (2026-05-25)

- **Sort control** in the collection paper-list header (the select-all bar): Title A–Z/Z–A,
  Added newest/oldest, Year newest/oldest, Authors A–Z. Done **client-side** (Alpine
  `sortPapers()` re-orders the existing `<li>` nodes by `data-title/-authors/-year/-added`)
  so it doesn't reload the page or lose chat/panel state; choice persisted to
  `localStorage` (`cc.sort`) and re-applied on load. `library.list_papers` now also
  returns `added_at`.
- **Show added date** setting (default **true**), config key `show_added_date`
  ("true"/"false"), checkbox in Settings → Display. When on, each paper row shows
  `added YYYY-MM-DD`; `collection_page` passes the resolved bool. Unchecked-checkbox
  semantics handled in `settings_post` (absent field ⇒ "false").

Verified in a real browser: sort by year/added produces order distinct from title (and
reverses correctly), persistence works, and toggling the setting off hides the date.
No schema change, no new dependency. 45 tests still pass.

### Collapse selection + sort behind a chevron (2026-05-25)

The collection paper-list toolbar (Select all + bulk actions + Sort) is now **collapsed
by default**: the bar shows only a centered ▾ chevron. Clicking it reveals the controls
and the per-row selection checkboxes; the ▴ button (or it's implicit) collapses again
and clears any active selection. Per-paper checkboxes are `x-show="toolsOpen"`, so they
don't appear while collapsed. The list still honors the persisted sort even when the
controls are hidden (`sortPapers()` runs on init regardless). Pure template/Alpine
change (`toolsOpen` state, default false, not persisted); 45 tests still pass.

### Paper-view nav icons, cross-collection jump, divider hide-handle, icon Settings (2026-05-25)

Paper-view top toolbar: dropped the token counter and the Highlights/Note text buttons
(both tabs still live in the right pane). Replaced with two **icon buttons** (hover
shows a label):
- **⏭ Next unread** — `GET /c/{slug}/p/{id}/next-unread` → `library.next_unread` finds the
  next paper whose note status is unread (no note row counts as unread), in title order
  after the current one, wrapping; falls back to the collection page if none.
- **🔍 Jump to paper** — opens a modal with a **cross-collection picker** (`GET /jump`,
  `_jump.html`): every collection + its papers (current collection first), with a
  client-side search filter. Loaded lazily via HTMX on first open.

**Hide-panel control moved to the middle of the drag divider** (paper + collection
views): a centered handle button on the resize bar (`@mousedown.stop` so it doesn't
start a drag) collapses the pane; the existing ⟨ rail reopens it. Removed the old ⟩
button from the right-pane tab bar.

**Settings** is now a ⚙ icon in the nav (title tooltip). Removed the `#api-usage` token
span and its out-of-band updater in `_chat_turn.html`.

Browser-verified (Playwright): icons + tooltips, next-unread skipping a noted paper,
cross-collection jump + filter, divider hide-handle collapse/reopen, gear Settings.
1 unit test added (next_unread); 46 total pass. No schema change, no new dependency.

### Fix laggy PDF highlight drawing (2026-05-26)

The highlight overlay (`static/annotate.js drawPage`) redrew on every PDF.js render
event. Two problems made scrolling/zooming janky:
1. **Leftover debug `console.log`s in the hot path** — including one that called
   `getBoundingClientRect()` (forced synchronous layout) on every draw, a boot log that
   dumped the whole annotations array, and a per-highlight create log. Removed all three
   (kept the two `console.warn` error paths).
2. **Double, un-batched redraws** — PDF.js fires both `pagerendered` and
   `textlayerrendered` per page (and rapidly while scrolling), so `drawPage` ran twice
   per page synchronously. Added a `requestAnimationFrame` coalescer (`scheduleDraw`)
   so each page is redrawn at most once per frame.

No behavior/visual change. (If lag persists with very many highlights, the next lever is
the `mix-blend-mode: multiply` on each highlight box, which is composite-heavy.)

### Fix laggy divider resize (iframe swallowing mousemove) + lighter highlight fill (2026-05-26)

The real "lag" was the **resize divider**, not the highlight draw: the PDF lives in an
`<iframe>`, and once the cursor crossed onto it mid-drag the iframe captured the
`mousemove` events, so the parent's drag handler only updated when the mouse was back
over the divider/chat — hence "can't keep up when I move fast" and "snaps when I get
near the lever". Fix: `startDrag` now drops a transparent full-window **shield** overlay
(`position:fixed; inset:0; z-index:9999; cursor:col-resize`) for the duration of the
drag, so mousemove keeps flowing over the parent regardless of iframes underneath;
removed on mouseup. Applied to both paper.html and collection.html (collection has
per-paper preview iframes with the same problem).

Also switched the highlight fill from `mix-blend-mode: multiply` (composite-heavy on
scroll) to a plain `rgba(...,0.32)` translucent fill.

Browser-verified: with an emulated iframe over the drag region, the shield is the
top element (events not stolen) and the pane width tracks continuously across a fast
drag; shield is removed on release. 46 tests still pass.

### Highlights tab: select-all + reflect newly-added highlights (2026-05-26)

- **Select all** added to the highlight manager (`static/annotate.js`): a checkbox above
  the rows toggles selection of all *shown* (filter-aware) highlights; reflects
  checked/indeterminate state. Feeds the existing batch recolor/delete bar.
- **Bug fix:** adding a highlight from the PDF didn't update the Highlights list while
  that tab was open. `createHighlight` pushed to the in-memory array and redrew the page
  but never re-rendered the manager. Exposed the manager's `render` as
  `window.__renderHighlights` and call it after creating a highlight (and after adding a
  note to a fresh highlight).

Browser-verified: select-all selects the rows + shows the batch bar; a newly-pushed
annotation appears in the list via `__renderHighlights`. 46 tests still pass; no
console.logs remain in annotate.js.

### Dark mode (themed UI) + dark PDF with per-paper toggle (2026-05-26)

**App dark mode (themed, not inverted):** Tailwind CDN set to `darkMode:'class'`; a
no-flash inline script in `<head>` applies `.dark` from `localStorage('pa.theme')` or
the system preference before first paint. The whole app is themed by one scoped CSS
block in base.html that remaps the slate palette under `html.dark` (surfaces, text,
borders, hover states, form controls, and the typography `prose` vars) — so every
template inherits dark mode without per-file `dark:` edits, and emoji/icons stay
correct. `bg-slate-900` (primary buttons / active tabs / code+diff blocks) deliberately
stays dark. Nav gets a 🌙/☀️ toggle (Alpine on `<body>`: flips the class, persists,
fires `window.__onThemeChange`).

**Dark PDF:** in the reader, `paperView.applyPdfDark()` injects a `<style>` into the
same-origin PDF.js iframe that darkens the viewer background and applies
`filter:invert(0.92) hue-rotate(180deg)` to the page **canvas only** — text layer and
the highlight overlay sit above it and keep correct colors. Controlled by:
(1) the app theme, (2) a Settings checkbox `pdf_dark` (default on) "Invert the PDF in
dark mode", and (3) a per-paper 🌙/☀️ toggle in the reader toolbar that overrides and
persists to `localStorage('pp.pdfdark.<id>')`. Follows theme changes live.

Browser-verified: theme toggle + persistence, surfaces remap (body→#0f172a,
bg-white→#1e293b), PDF invert injected (viewer #0b1220, canvas filter applied), per-paper
toggle flips it off/on and persists; settings round-trips. 46 tests still pass. No new
dependency.

### Highlight UX: color-on-selection + inline note editor (2026-05-26)

Reworked the highlight creation/note flow in `static/annotate.js` (+ the `#beta-toolbar`
markup in paper.html):
- **Color chosen at selection:** the floating toolbar now shows the 4 color swatches +
  Ask + ✎ Note. Clicking a swatch creates the highlight in that color instantly (and
  remembers it as the default, persisted to `localStorage('pp.hlcolor')`).
- **Inline note editor replaces all three `prompt()` calls.** A reusable
  `openNoteEditor({x,y,text,color,withColor,onSave})` floating popover with a textarea
  (Enter=save, Shift+Enter=newline, Esc=cancel). On creation it also shows color
  swatches (pick color + write note in one step); when editing an existing highlight
  (edit-popup ✎ and the Highlights-tab "note") it's text-only since color is managed
  elsewhere. `createHighlight(color, noteText)` now takes an explicit color.

Browser-verified end-to-end on a text PDF: selection toolbar shows 4 swatches; swatch =
instant colored highlight; ✎ Note opens the editor with swatches, and save creates a
highlight with the chosen color + note; Highlights-tab note edit saves and re-renders.
No `prompt()` / `console.log` left in annotate.js. 46 tests still pass.

### Collection header cleanup + lever-consistent tools grip (2026-05-26)

- **Removed the "Stale papers" header button** (the route/template stay, just unlinked).
- **Refresh / Sync / Download-all are now icon-only buttons** (↻ ⇪ ⤓) moved onto the
  collection-name line, with the label shown as a hover tooltip (same pattern as the
  paper-view icons). The paper-count pill sits at the far right.
- **Source-aware labels (future-proofing):** the labels read "Refresh from {source}" /
  "Sync to {source}" from a new `_collection_source(col)` helper in main.py (today always
  "Zotero"; refresh shown only when the collection is linked). When local-folder / other
  importers land, that one helper returns their label/verbs — no template changes.
- **Tools-bar collapse triangle → a centered grip** styled like the resize lever
  (`h-5 w-12 rounded bg-slate-300 shadow hover:bg-slate-500`, vs the lever's `w-5 h-12`),
  in its own centered row in both states (▾ to show, ▴ to hide) via a new `toggleTools()`.
  Replaces the two tiny off-to-the-side text triangles.

Browser-verified: no Stale; icon buttons with hover-only labels on the name line; grip
centered + sized to match the lever; toggling it shows/hides select-all + sort. 46 tests
still pass. No schema change; local-folder import itself is NOT built (only the label
scaffolding).

### Tools toggle moved next to "Add" (2026-05-26)

Follow-up to the grip: the select/sort toggle is no longer a centered pill in its own
row. It's now a button in the left tab bar **immediately left of "⊕ Add"**, styled
identically to Add (`rounded border border-slate-300 bg-white px-2 py-0.5`), with a
bigger triangle glyph (`text-base`, ▾/▴) inside the same-size button. The select/sort
bar itself is `x-show="toolsOpen"`, so when collapsed there's no empty strip. The toggle
only shows on the Papers view. Browser-verified; 46 tests still pass.

### Landing-page collection cards: red delete icon, declutter, sort + drag-reorder (2026-05-26)

`templates/index.html` collection cards:
1. **Delete → red icon.** Replaced the "Delete" text with an inline SVG trash icon
   (`stroke=currentColor` + `text-rose-600`, so it's genuinely red — an emoji 🗑 would
   ignore the color). Same confirm dialog.
2. **Removed Refresh / Sync / Download / the eager-mode label** from the card footer
   (those actions live on the collection page header now).
3. **Drag to reorder** the cards (HTML5 DnD: cards `draggable`, inner link
   `draggable=false`; live insert-before on dragover by horizontal midpoint; a ⠿ grip
   hint in the footer). Dragging commits a custom order to `localStorage('pa.colorder')`
   and switches the sort to "manual".
4. **Sort dropdown** (Last updated / Name / Papers / Manual), **default Last updated**,
   persisted to `localStorage('pa.colsort')`, applied client-side to the server-rendered
   cards. New `last_activity` field on each collection in `library.list_collections`
   (max member `added_at`, falling back to `last_refresh`/`created_at`).

Browser-verified: red SVG trash (rgb 225,29,72); no refresh/sync/eager; default sort
orders newest-first; name/count sorts; drag reorders + persists as "manual". 46 tests
still pass. No schema change; order/sort are per-browser (localStorage).

### Landing import button made source-agnostic (2026-05-26)

Relabeled the Zotero-specific import affordances now that the app is meant to take more
sources (Zotero is still the only working importer). `templates/index.html`: button
"↧ Import from Zotero" → "↧ Import" (+ count badge), empty-state button likewise; import
panel heading → "From Zotero" (source shown inside the panel); intro text no longer
hardcodes "Zotero is what you import from and sync back to". No backend change — chose
the relabel-only scope; a real local-folder importer is still future work.

### Collection "hotness" — engagement heatmap on cards (2026-05-26)

Each landing card now shows a **GitHub-style mini-heatmap of my engagement over the last
7 days**: 4 rows (💬 chat msgs · ⚡ app highlights · ✎ note edits · 💡 thoughts) × 7 day
columns, single **amber** heat scale (faint = quiet, darker = more), always rendered.

- `library.collection_activity(window_days=7)` computes per-collection / per-type /
  per-day counts on the fly: 3 grouped SQL queries (chat_messages user turns via
  chat_threads, app annotations by created_at, paper_notes by updated_at) + a
  thoughts-dir filename scan. Equal weight, 1 point/event. `list_collections(with_activity=True)`
  attaches `c["activity"]` (per-type day arrays + `total`); only the landing route asks
  for it. `activity_days()` gives the window's ISO dates.
- `index.html`: a `heatcell(n)` macro maps counts→amber buckets (0/1/2-3/4-6/7+); each
  card renders the 4×7 grid under the card link (stays fully clickable). New `data-hot`
  (= 7-day total) drives a new **"Hottest (7d)"** sort option; default stays Last updated.

Browser-verified: 28 cells/card, totals 18/2/0 across seeded collections, amber density
tracks activity, quiet collection all-faint, Hottest sort ranks by total. 1 unit test
added (47 total pass). No schema change, no cache/table — recomputed per landing render.

### Heatmap full-width + custom tags + summary removed (2026-05-26)

- **Heatmap now spans the card edge-to-edge.** Widened to **28 days** (4 weeks) rendered
  as a responsive CSS grid per row (`grid-template-columns:repeat(N,minmax(0,1fr))` +
  `aspect-square`), so cells stretch to both edges yet stay square at any card width.
  `library.collection_activity` window = `ACTIVITY_WINDOW` (28); the **Hottest sort uses
  `hot7`** (sum of the last 7 days), so "hot" still means recent. Template loops the
  per-type array (window-agnostic).
- **Custom per-collection tags.** New `tags` JSON column on `collections` (added in the
  CREATE schema + idempotent `_migrate` ALTER). `library.set_tags`/`_parse_tags` validate
  `{label, color}` (color must be `#rrggbb`); `get_collection`/`list_collections` parse
  them. New `POST /c/{slug}/tags`. Reusable `_tagbar.html` partial + a global `tagBar()`
  Alpine component (in base.html) render colored pills (inline-styled from the hex, so
  any color works), an `× ` remove, and a "+ tag" popover with **8 preset swatches + a
  custom color picker**. Editable in **both** places: on each landing card and in the
  collection-page header. (x-data must be single-quoted — `tojson` emits `"`.)
- **Summary removed** from the UI: off the cards, out of the new-collection form, off the
  collection-page header. (DB column + set_summary left dormant; no migration needed.)

Browser-verified: 4×28 grid fills >80% of card width with square cells, tags render with
custom colors + add/remove persists to the DB, no "No summary" anywhere, 0 console errors.
1 unit test added (tags roundtrip/validation). 48 tests pass.

### Fix: Safari heatmap invisible + card min-width (2026-05-26)

- **Safari heatmap fix.** Cells used `aspect-square` (CSS `aspect-ratio`) inside a
  `grid` with `minmax(0,1fr)` tracks — a known WebKit bug collapses such items to 0
  height, so the rows vanished in Safari (Chromium fine). Replaced with an explicit
  `h-2.5` cell height (width still 1fr/responsive); cells are now ~near-square and always
  render. Also bumped empty cells `bg-slate-100`→`bg-slate-200` so a quiet grid is visible.
- **Card min-width.** The card grid was fixed column counts (`sm:grid-cols-2
  lg:grid-cols-3`) so cards stretched/shrank with the window. Switched to
  `grid-template-columns:repeat(auto-fill,minmax(300px,1fr))` — cards are now ≥300px and
  the column count reflows.

Chromium-verified: cells 10px tall (never 0), activity amber renders, card width ≥300.
(Safari not directly testable via Playwright, but the change removes the documented
WebKit aspect-ratio failure mode.) 48 tests pass.

### Fix: adding a tag on a landing card did nothing (2026-05-26)
The whole collection card was `draggable="true"`. Inputs/buttons nested inside a
draggable element can't be focused or clicked normally in Chrome/Safari — mousedown
starts a drag instead — so the "+ tag" popover's label input and Add button were dead
on the card (but fine on the collection page, which has no draggable ancestor).
Fix: card is non-draggable by default; the drag handle (⠿) sets `draggable="true"` on
its `mousedown` and clears it on `mouseup`/`dragend`. Tag input now works on cards and
reordering still works. Verified in Chromium: tag persists across reload, card draggable
only while the handle is held.

### Export/sync destination popup (2026-05-26)
The collection header's sync action was hard-coded "Sync to <source>" (always Zotero),
which made no sense for a local-only collection. Replaced it with an "Export / sync…"
button (⇪) that opens a destination-chooser modal: Zotero (fully wired — routes to the
existing /c/<slug>/sync/preview flow; copy adapts to whether the collection is already
Zotero-linked), plus Directory and Other source shown as disabled "coming soon" entries
(no backend yet). Modal driven by Alpine `syncOpen` in collectionView(), escape/click-out
to close. Per user choice, dir/other are placeholders rather than dead buttons.

### Directory export (BibTeX) (2026-05-26)
Made the "Directory" destination in the export popup functional (was a placeholder).
New `app/export_dir.py`: `export_dir(slug, dest)` writes `<slug>.bib` into a folder
(created if missing; rejects relative/non-folder paths with a friendly error).
BibTeX rendering: @misc for arXiv papers (eprint/archivePrefix/url) else @article;
citekey = firstAuthor+year+firstTitleWord (deduped); comma-separated authors → ' and '.
Per the user's choices the export is BibTeX-only (no JSON/markdown/PDF copy) and the
destination is a prefilled-but-editable absolute path (default ~/.paper-agent/exports/<slug>/).
Route `POST /c/<slug>/export/dir` returns an inline result fragment (_export_result.html);
the popup's Directory row expands to a path field + "Export .bib" button (HTMX). collection_page
now passes `export_dir_default`. Tests: BibTeX content/citekey/type + relative-path rejection.
50 tests pass. Verified end-to-end in Chromium (default path prefilled, file written, error path).

## AGENTIC_PLAN Phase 1 — Typed captures (synth_kind + author_origin) — 2026-05-26

Built the typed-capture data model from AGENTIC_PLAN.md (the grilled, re-cut agentic
plan). Every fragment now resolves to an effective (synth_kind, author_origin).

- **db.py**: added `synth_kind` ('auto'|'seed'|'reasoning', default 'auto') and
  `author_origin` ('human'|'agent'|'external', default 'human') to `paper_notes`
  (schema + idempotent `_migrate`). Distinct names — `annotations.kind/origin` keep
  their own meanings. Migration is non-destructive (existing rows get defaults).
- **thoughts.py**: stamps written into frontmatter; `create_thought` door-stamps
  (synth_kind from the seed|reasoning toggle, author_origin passed by the endpoint, not
  the form); `update_thought` preserves author_origin (no relaundering); reads default
  missing frontmatter to (seed, human) so pre-P1 files migrate on read.
- **notes.py**: persist/read the new columns + mirror frontmatter; `note_kind(slug,
  paper_id)` resolver — synth_kind 'auto' ⇒ heuristic (reasoning iff the note's
  `thoughts` field is non-empty), explicit override wins. `# SPAN-TODO` seam left for
  span-level marks.
- **provenance.py** (new): `effective_stamp(ref, slug)` — highlight→(seed,human),
  paper→(seed,external), note→note_kind, thought→frontmatter; unknown→safe (seed,human).
- **main.py**: notes/thoughts endpoints accept a `synth_kind` form field and stamp
  `author_origin='human'` at the door (agent origin unreachable until Phase 6).
- **templates**: seed|reasoning select on thought capture (panel + /thoughts page) with
  a reasoning/agent badge on each item; auto|seed|reasoning Kind select on the note form.

Tests: `tests/test_provenance_p1.py` (10) — stamp-by-door, note heuristic + override,
mirror round-trip, thought-missing-frontmatter migration, edit-preserves-origin,
resolver across all four types, non-destructive column migration. **60 tests pass**
(was 50). Live-app smoke verified: stamps round-trip through the real endpoints; UI
toggles/badges render.

Deferred to later phases: span-level note marks; consolidation door still stamps
(seed, human) — fine for now. Next: Phase 2 (the gate + wiring highlights/papers into
gather_inputs).

## AGENTIC_PLAN Phase 2 — The gate — 2026-05-26

Replaced the flat `_filter_claims` guardrail with a typed `gate()` that enforces the
attribution boundary in code, and wired highlights + papers into the wiki pipeline.

- **wiki.py — gather_inputs**: now also loads the collection's papers and highlights;
  exposes `valid_papers`, `valid_highlights`, `hl_to_paper`, `highlights`, and `slug`.
  `valid_papers` = collection membership ∪ note papers ∪ highlight papers (note keys
  are str(paper_id), so they overlap by construction). Highlights fed to the generator
  within a char budget.
- **wiki.py — generate**: prompt now lists ALLOWED PAPER KEYS / HIGHLIGHT IDS, a
  highlights digest, and asks for a `claim_type` (attributed | synthesis) + `highlights`.
- **wiki.py — the gate** (pure, unit-tested): `gate(claim, ctx)` →
  - cleans refs to valid ones (highlight ids coerced to int);
  - computes a **structural claim_type floor** (synthesis if ≥2 papers or any thought,
    else attributed) and takes `_stricter(structural, agent_label)` so the agent can
    only tighten, never loosen;
  - **attributed** → ACCEPT iff it cites a valid paper or highlight (a note is the
    user's interpretation, not the source) else REJECT;
  - **synthesis** → ASSERT iff a cited ref resolves to (reasoning, human) via
    `provenance.effective_stamp`, else DEMOTE.
- **wiki.py — run_generation**: gates every claim; assertions build their page;
  DEMOTEd synthesis is routed (never discarded) into `gaps/open-questions.md`, framed
  as "open question — needs your reasoning"; REJECTs dropped. Every demotion/rejection
  is logged. `proposal_from_chat` now also runs through the gate (chat can't bypass it;
  ungrounded synthesis from chat → open question).
- **Provenance in pages**: `_build_page`/`_merge_into` carry `derived_from_highlights`
  and a richer "supported by" line (papers, hl:ids, notes, thoughts).
- **templates**: proposed-edit review (full page + panel) show the claim_type badge and
  full provenance, defensively (old proposals lacking the new keys still render).

Tests: `tests/test_gate_p2.py` (10) pin each provenance type's grounding power, the
structural floor + agent-can-only-tighten, and demote-vs-assert; `test_wiki.py` updated
to the new contract (the old `_filter_claims` test became a gate test; the fake LLM now
cites sources). **70 tests pass** (was 60). End-to-end verified: an attributed claim
builds its page while an ungrounded 2-paper synthesis demotes into gaps/open-questions;
the live review page renders badges + provenance.

Next: Phase 3 — the Engine seam + CLI swap (Claude Code | Codex | OpenAI), the
dependency flip so llm.complete() no longer needs an API key.

## AGENTIC_PLAN Phase 3 — Engine seam + CLI swap — 2026-05-26

The dependency flip: llm.complete() now runs on a user-selectable CLI agent (Claude
Code | Codex) with no API key; OpenAI kept as an optional third backend.

- **engine.py** (new): `Engine` ABC + `EngineResult`; implementations `FakeEngine`
  (tests), `ClaudeCodeEngine`, `CodexEngine`, `OpenAIEngine`. `run_once(messages, ...)`
  takes the native messages list (no lossy flatten for OpenAI); CLI engines flatten via
  `_split_system` (system msgs → system prompt; rest → role-labeled transcript).
  - ClaudeCodeEngine: `claude -p --output-format stream-json --verbose` over a
    subprocess, prompt via stdin, `--append-system-prompt`, optional `--allowedTools` /
    `--mcp-config` / `--resume` (used in P5/P6), hard 300s timeout; `_parse_claude_stream_json`
    reads the terminal `result` event (final text + session_id + usage), falling back to
    assistant text deltas. **Only passes `--model` when it's a real Claude model** (the
    shared `model` config defaults to the OpenAI-centric gpt-4o-mini, which Claude rejects).
  - CodexEngine: `codex exec` headless, plain stdout (rich event parity deferred).
  - OpenAIEngine: native messages; moved the model-list filtering here.
  - `select_engine_name` / `build_engine`: config `engine` ∈ {claude-code, codex, openai};
    empty => auto (openai iff a key is set, else claude-code) so existing installs keep
    working while new ones default to Claude Code.
- **llm.py**: rewritten as a thin delegator (`complete`/`stream`/`list_models`/`usage`/
  `LLMError` unchanged) → selected engine; wraps `EngineError` as `LLMError` for the
  existing graceful-degradation paths; added `engine_status()`.
- **config.py**: `engine`, `claude_bin`, `codex_bin` defaults.
- **main.py + _settings_form.html**: engine selector (Auto | Claude Code | Codex |
  OpenAI) with per-engine binary/key fields and a live "ready/unavailable" status badge;
  model help generalized; settings_post persists the new fields.

Tests: `tests/test_engine_p3.py` (11) — selection/auto, message flattening, the
stream-json parser (result-event + assistant-delta fallback), missing-binary
degradation, and that `llm.complete` delegates + wraps errors. **81 tests pass** (was 70).
**Live-verified**: with no API key, `llm.complete([...])` ran through Claude Code and
returned "PONG" with token usage recorded; the app boots and Settings shows
"claude-code · ready"; models.json returns the Claude aliases.

Milestone A (the dependency flip) is complete: P1 typed captures + P2 gate + P3 CLI
engine. The app no longer needs an OpenAI key. Next: Milestone B — Phase 4 (MCP read
surface + submit_proposal over loopback HTTP).

### Fix: Model field is engine-aware (2026-05-26)
The Model picker showed an OpenAI id even when a CLI engine was selected. Lifted the
engine state (`eng` + `effEngine`/`isCli`/`engLabel` getters) to the settings <form> so
the Model block reacts: for Claude Code / Codex it shows a disabled box ("<engine> —
uses your CLI's configured model"); for OpenAI it shows the editable picker. The chosen
model always submits via a hidden input. Verified in-browser across all three selections.

## AGENTIC_PLAN Phase 4 — MCP read surface + submit_proposal — 2026-05-26

Bounded, validating tool surface the agent reaches over MCP. **Transport: stdio**, not
the grilled in-process HTTP — see the decision note below.

- **wiki.py**: extracted `process_pages(slug, raw_pages, inputs, mode)` from
  `run_generation` (gate + persist) so the LLM path and the agent's `submit_proposal`
  share the exact same gated path; neither can bypass the gate.
- **mcp_server.py** (new): transport-agnostic tools, all calling existing read fns —
  `get_unreasoned_seeds` (seed-kind fragments grouped by paper, previews, capped),
  `get_fragment` (composite ids note:/thought:/highlight:/paper:), `search_fragments`
  (FTS5 notes + substring thoughts), `read_wiki_page` (path-escape-guarded) — plus
  `submit_proposal` whose body runs the gate and writes only `proposed-edits/` (never
  the wiki). Minimal JSON-RPC `dispatch` (initialize/tools/list/tools/call/ping;
  notifications → no reply). `stdio_mcp_config(slug)` builds the per-run --mcp-config.
- **mcp_stdio.py** (new): `python -m app.mcp_stdio` — newline-delimited JSON-RPC over
  stdin/stdout; scoped to one collection via `PA_MCP_COLLECTION`.

### Decision: stdio transport (overrides the grilled in-process HTTP choice)
Empirically, **Claude Code headless (`-p`) never connects to an HTTP MCP server passed
via `--mcp-config`** — zero requests reach the server (verified with a request-logging
middleware), regardless of protocolVersion echo, SSE framing, `--strict-mcp-config`, or
`enableAllProjectMcpServers`. The config schema matched Claude's own `claude mcp add`
output exactly, and a plain httpx client round-tripped fine — so it's a headless-mode
limitation, not our bug (the official MCP SDK wouldn't help; nothing reaches us). stdio
(how pre-trusted servers like playwright connect) works: verified live end-to-end via
the production `stdio_mcp_config` helper (`pa: connected`; Claude called
`get_unreasoned_seeds` and returned the real seed count). User approved the switch.
Invariant preserved: the stdio process is app code running the gate; it reads app data
and writes only the gated queue — the agent never writes the wiki. Removed the dead
`POST /mcp` route + token machinery.

Tests: `tests/test_mcp_p4.py` (9) — read tools, submit_proposal goes through the gate
(written/rejected; queue not wiki), JSON-RPC dispatch, and `stdio_mcp_config` scoping.
**90 tests pass** (was 81). Next: Phase 5 — wire the organizing pass to drive the agent
over this stdio MCP surface (read tools + submit_proposal), gate inside submit_proposal.

## AGENTIC_PLAN Phase 5 — Agentic organizing pass — 2026-05-26

The wiki "generate" action now drives a real agent over the stdio MCP surface.

- **organizer.py** (new): `organize(slug, mode)`. If the engine is a CLI agent
  (claude-code) it runs the agentic path — `engine.run_once` with a system+user
  organizing prompt, `allowed_tools` = the five `mcp__pa__*` tools, and the per-run
  `stdio_mcp_config(slug)`; the agent reads fragments via MCP and returns proposals by
  calling `submit_proposal` (gate runs in code, writes only the queue). Other engines
  (OpenAI; Codex until verified) fall back to the tool-less `wiki.run_generation` —
  same gate, same queue. Raises `llm.LLMError` if the engine is unavailable.
- **engine.py**: `ClaudeCodeEngine` now passes `--mcp-config <json>` (dict → JSON) plus
  `--strict-mcp-config` so only our server loads.
- **main.py**: `/c/<slug>/wiki/generate` routes through `organizer.organize` (agentic
  when a CLI agent is selected, tool-less otherwise). UI unchanged — the existing
  "Full rebuild"/"Incremental" buttons now drive the agent. LLMError is swallowed and
  surfaced as the (empty) review queue, so the action degrades cleanly with no CLI.

Tests: `tests/test_organizer_p5.py` (3) — agentic path via a FakeAgent that reads the
collection from the MCP config and calls submit_proposal (gate drops the ghost claim;
nothing hits the wiki); tool-less fallback for non-agentic engines; LLMError when the
engine is down. **93 tests pass** (was 90).

**Live-verified** with real Claude over stdio on a seeded robotics collection: the agent
connected, read the fragments, and submitted proposals. The gate accepted two attributed
claims (one citing a highlight, one a paper) into methods/, and **demoted a synthesis
claim to gaps/open-questions because the agent cited only a seed thought** (not human
reasoning) — exactly the intended boundary, against a real agent, with nothing written
to the wiki. Next: Phase 6 — /{collection} agentic chat + attribution-safe capture.

## AGENTIC_PLAN Phase 6 — /{collection} agentic chat + capture — 2026-05-26

A `/<slug>`-prefixed chat turn is answered by the agent over the bounded MCP read tools;
the only way collection thinking leaves a chat is the attribution-safe capture.

- **agentic_chat.py** (new): `parse_prefix` (leading `/<token>`, ignoring the legacy
  /collection /wiki literals); `answer(slug, history, user_text)` → `engine.run_once`
  with `CHAT_TOOLS` = the four read tools (**no submit_proposal** — chat writes nothing)
  + the per-collection stdio MCP config; raises LLMError if the engine is down.
- **thoughts.py**: `create_thought` gained `prompted_by` (links a human take to the
  agent seed); read paths surface it. The capture door is the only one that stamps
  `author_origin='agent'`.
- **main.py**: `chat_post` routes a `/<slug>` prefix to `_agentic_chat_turn` (unknown
  slug → reply listing collections, no LLM); un-prefixed chat unchanged. New
  `POST /c/<slug>/thoughts/capture`: agent reply → (seed, agent) thought; optional
  'your take' → (reasoning, human) thought with `prompted_by` = the seed.
- **_chat_turn.html**: on agentic turns, a "＋ capture to thoughts" popover (agent text
  hidden + a 'your take' field) posting to the capture endpoint.

Tests: `tests/test_agentic_chat_p6.py` (6) — prefix parsing (incl. reserved literals),
answer passes read-only tools (no submit_proposal) scoped to the slug, LLMError when
down, capture stamps (seed/agent + reasoning/human + prompted_by), and the key invariant
that an agent seed resolves to (seed, agent) — can't ground a synthesis — while its
human take resolves to (reasoning, human). **99 tests pass** (was 93).

**Live-verified** with real Claude over stdio: unknown prefix listed the collection (no
LLM); a `/robotics` turn returned a grounded answer (reality gap / sim-to-real / domain
randomization — straight from the seeded note + thought, so the MCP read tools were
used) in ~28s with the capture UI; capture produced a (seed, agent) thought and a
(reasoning, human) thought with prompted_by linking them.

### Milestone B complete (Phases 4–6). Phase 7 (debt queue) remains build-gated — not
built (build only if it'll be opened). The full agentic backend is in place: typed
captures → gate → CLI engine → bounded MCP tools → agentic organizing + agentic chat,
with the attribution boundary enforced in code throughout and the wiki only ever written
by user-accept.

## AGENTIC_PLAN Phase 7 — Reading-debt queue + brainstorm — 2026-05-26

Built the full debt queue, extended (after a grilling session) to the user's 4-action
design: per debt item — Fill / Ignore / Brainstorm — plus Brainstorm-all.

Design decisions (grilled): agent-authored content is quarantined in **wiki/brainstorming/**
(never a grounded section, gate-EXEMPT, labeled '(agent)/speculative'); it still goes
through the review queue + accept ('only accept writes wiki/' preserved). "Let the LLM
reason it" = produce a quarantined brainstorm (same engine as Brainstorm-all, item vs
pile scope) — **never** a pre-filled human-take (frictionless approval refused). Debt is
generated by an on-demand agent pass that emits **questions only** (the one agent role
the thesis allows). "Fill" → a (reasoning, human) thought linked via prompted_by that
grounds the wiki on the next organize. Debt persists with a stable id (hash of source
fragment ids); re-runs dedupe; Ignore sticks (seeds untouched).

- **db.py**: `reading_debt` table (id, slug, question, sources JSON, status
  open|filled|ignored|brainstormed).
- **debt.py** (new): data layer (`upsert_debt` deduped by `debt_id`, `list/get/set_status,
  count_open`), `fill_debt` (→ reasoning,human thought + prompted_by), `find_debt`
  (agentic questions-pass via MCP + deterministic by-paper fallback), `brainstorm`
  (agentic via MCP + tool-less fallback).
- **mcp_server.py**: gate-exempt tools `submit_debt` (records questions; lazy-imports
  debt to avoid a cycle) and `submit_brainstorm` (→ wiki.brainstorm_pages); both
  advertised + dispatched. Three per-pass allowlists now exist (organize: +submit_proposal;
  find: +submit_debt; brainstorm: +submit_brainstorm).
- **wiki.py**: `brainstorm_pages` — gate-exempt persistence of (agent) brainstorm pages
  into the review queue → wiki/brainstorming/, with a visible 'speculative/machine' notice.
- **main.py**: `_debt_panel` + routes `GET /debt/panel`, `POST /debt/find`,
  `/debt/{id}/fill`, `/debt/{id}/ignore`, `/debt/{id}/brainstorm`, `/debt/brainstorm-all`;
  `debt_count` on the collection page.
- **templates**: new `_debt_panel.html` (Find button, per-item Fill/Ignore/Brainstorm,
  Brainstorm-all); a **Debt** left-panel tab with a count badge in `collection.html`.

Tests: `tests/test_debt_p7.py` (5) — dedupe + ignore stickiness, fill→(reasoning,human)
+ prompted_by + resolver, deterministic find fallback, brainstorm quarantine/label +
accept lands under wiki/brainstorming/, and the MCP tools. **104 tests pass** (was 99).

**Live-verified** with real Claude over stdio on a long-context-LLMs collection: the
find pass produced sharp **cross-paper questions** (clustering note pairs + the seed
thought; questions only, no answers); Brainstorm produced 2 quarantined brainstorming/
proposals (debt → brainstormed); Fill created a (reasoning, human) thought (debt →
filled). Thesis held throughout: agent only ever asked questions or produced labeled,
gate-exempt, quarantined speculation — never a grounded assertion.

### AGENTIC_PLAN COMPLETE (Phases 1–7). The full agentic backend ships: typed captures
→ gate → user-selectable CLI engine (no API key) → bounded stdio MCP tools → agentic
organizing, agentic chat, and the reading-debt/brainstorm queue — with the attribution
boundary enforced in code throughout and the wiki only ever written by user-accept.

## PAPER_CHAT_AGENT Phase A (core) — interactive paper sub-agent — 2026-05-26

Per-paper chat (Claude Code + cached PDF) is now a persistent, read-only sub-agent that
reads the actual PDF and the user's live notes — not a stuffed one-shot completion.
NON-streaming first (reuses the HTMX swap); SSE streaming is the next sub-step.

- **db.py / repo.py**: `agent_session_id` on chat_threads + `get_session_id`/`set_session_id`.
- **engine.py**: `run_once(..., resume=False)`; ClaudeCodeEngine uses `--session-id <id>`
  to START a session and `--resume <id>` to continue.
- **mcp_server.py**: `get_paper_context(paper_id)` tool — the user's current notes +
  highlights for one paper (always live).
- **paper_chat.py** (new): `PaperChatAgent` seam + `ClaudeCodePaperAgent`. Per turn it
  sends ONLY the new message to a per-thread Claude session (resume), with tools
  Read + the bounded MCP read tools (NO submit_* — read-only), `cwd` scoped to the PDF
  dir, and a paper-reading system prompt pointing at the PDF path. `get_agent(paper_id)`
  returns the agent only for Claude Code + cached PDF, else None (classic fallback).
- **main.py**: `chat_post` routes an open-paper, text-only turn to the sub-agent when
  eligible (`_paper_subagent_turn`); images and non-eligible cases keep the classic path.

Tests: `tests/test_paper_chat_p8.py` (3) — get_paper_context; eligibility (Claude+PDF
only); first turn STARTS a session sending only the new turn (read-only tools), later
turn RESUMES the stored session. **107 tests pass** (was 104).

**Live-verified** with real Claude over stdio: a hand-built PDF with distinctive text —
the sub-agent Read it and returned the exact unguessable values ("91.3 percent",
"dual-encoder pipeline"), then on a follow-up that sent ONLY the new question, correctly
recalled the prior topic ("Foobar") — proving Read-the-PDF + session resume (no
re-sent history). Next: SSE streaming (the rest of Phase A).

### Fix: paper sub-agent asked for Bash permission to extract PDF text (2026-05-27)
The sub-agent had only the built-in Read tool for the PDF; for a full-paper summary the
model reached for `pdftotext`/`pypdf` via **Bash** — which isn't in its allowlist, so it
got stuck asking the user for permission. Fix (not allowing Bash — that breaks the
bounded surface): gave it a first-class, allowed text tool.
- **pdf_text.extract_pages(path, start_page, count)** — page-range text + total_pages.
- **mcp_server.read_paper_text(paper_id, start_page, pages)** MCP tool (no shell), bounded.
- **paper_chat**: added `read_paper_text` to the sub-agent toolset; system prompt now says
  to read text via read_paper_text (paginated), use Read only for figures/layout, and
  NEVER use a shell/pdftotext/pypdf.
Live-verified on a 3-page PDF: the agent paged through via read_paper_text and returned
the exact per-page values (WidgetFormer / 88.8% on Zorp / 2 tokens-per-sec) with **no
Bash permission prompt**. 107 tests pass.

### Fix: CLI spawns inherited the repo cwd (loaded its CLAUDE.md) (2026-05-27)
`_run` never passed `cwd` to subprocess, so every `claude`/`codex` spawn ran in the
server's working dir — auto-loading the repo's CLAUDE.md (the build spec) as agent
context (noise + tokens), and the agentic cwd-scoping silently never took effect. Fix:
`_safe_cwd()` defaults the spawn cwd to APP_DIR (no CLAUDE.md/AGENTS.md there) when none
is given; `_run` now passes `cwd`, and both CLI engines forward their `cwd`. Deliberately
NOT `--bare` (it disables the keychain/OAuth login we rely on, forcing an API key).
Live-verified: spawn cwd is now ~/.paper-agent, and `complete()` still returns with no
API key (OAuth intact). 107 tests pass.

## PAPER_CHAT_AGENT Phase A — streaming + read_paper_text — 2026-05-27

Finished Phase A: the per-paper sub-agent now streams, with a reliable no-shell PDF
text path.

- **engine.py**: `ClaudeCodeEngine.stream_events()` — Popen + `--include-partial-messages`;
  yields {status|token|done|error}. token deltas from `stream_event/content_block_delta`,
  tool status from tool_use blocks, final text + session_id from the `result` event.
  `_prepare()` shares argv-building with `run_once`. `_tool_status()` maps tools to
  friendly labels and suppresses internal ones (ToolSearch/Task/TodoWrite).
- **paper_chat.py**: `ClaudeCodePaperAgent.stream()` wraps stream_events, persists the
  session id on done; the route persists the chat turn from the done event's text.
- **main.py**: `POST /c/<slug>/p/<id>/chat/stream` → NDJSON StreamingResponse (sub-agent
  when eligible, else a classic one-shot emitted as one token). `paper_page` passes
  `chat_streaming`.
- **_chat.html**: when `chat_streaming`, the composer streams via fetch + ReadableStream
  (live tokens + ⏳ status), then re-renders the final text as markdown via `/render`;
  otherwise the existing HTMX path (collection chat unchanged).

Tests: +`test_tool_status_*` (label + internal suppression). **108 tests pass** (was 107).
**Live-verified** end to end: agent-level stream (status while paging the PDF → token
deltas → done+session; resume turn recalled prior context) and the HTTP route via
`curl -N` (NDJSON status→token→done, answer read from the PDF, ToolSearch noise gone).

### PAPER_CHAT_AGENT status: Phase A DONE. Remaining (per plan): B skills (SKILL.md set),
C live-process mode + chat_session_mode toggle, D Codex sub-agent.

### Fix: paper sub-agent disclaimed "no chat history" (2026-05-27)
NOT a memory bug — session resume preserves history correctly (verified across 3 separate
HTTP requests: a codeword set in turn 1 was recalled in turn 3). The symptom was the model
disclaiming "no prior chat history" while recounting it, because every turn re-injects the
"you are starting to help read ONE paper" system prompt, framing a continuing session as a
fresh start. Fix: added a line to the sub-agent system prompt stating it's a CONTINUING
conversation and the earlier session messages ARE the chat history. Verified: it now
summarizes the prior turns instead of disclaiming them. 108 tests pass.

### Debug mode: show chat session id (2026-05-27)
Added a `debug` config flag + a "Debug mode (show the chat session id)" checkbox in
Settings. When on, the per-paper chat header shows a `session: <id>` line (the thread's
Claude session id); it renders the stored id at page load and updates live from each
streamed turn's `done` event. Off by default; paper-chat only. 108 tests pass.

## PAPER_CHAT_AGENT Phase B — paper-reading skills — 2026-05-27

Shipped a SKILL.md skill set the per-paper sub-agent auto-discovers and invokes.
- **app/skills/**: 6 read-only skills — summarize-section, extract-contributions,
  compare-to-my-notes, list-assumptions, locate-figure, find-evidence-for. Each grounds
  in read_paper_text / Read / get_paper_context and respects no-auto-save (analysis only).
- **agent_skills.py**: `ensure_skills_home()` syncs the bundled skills into
  APP_DIR/agent-home/.claude/skills (stable, no CLAUDE.md); `skill_names()`.
- **paper_chat**: the sub-agent's cwd is now the skills home (Read still uses the absolute
  PDF path), so Claude discovers the skills; system prompt lists them. `_tool_status`
  labels the Skill tool ("using a paper-reading skill…").

Verified (after confirming project-cwd SKILL.md discovery works headless): all 6 skills
appear in the session's skill list, and a live "compare this paper to my notes" turn
invoked the Skill → get_paper_context → read_paper_text and surfaced the dual-vs-single
encoder tension exactly as compare-to-my-notes prescribes. **109 tests pass**.
Note: app/skills/*.md is package data — fine under the editable dev install; a wheel
build would need it declared as package-data.

### Fix: paper chat "No conversation found with session ID" (2026-05-27)
A stored Claude session id can go stale (cwd change between runs — e.g. the Phase B
cwd→agent-home move — session expiry, cleanup, reinstall). We kept trying to `--resume`
the dead id forever, surfacing "claude exited 1: No conversation found with session ID".
Fix: `paper_chat` now self-heals — on a "no conversation found" failure (detected before
any token), it starts a FRESH session and replaces the stored id; `answer` retries once
likewise. Verified resume still works under the new cwd (recall across turns), and that
planting the exact dead id from the report self-heals (reply returns, id replaced, no
error shown). Regression test added. 110 tests pass.

## PAPER_CHAT_AGENT Phase C — live (persistent-process) mode — 2026-05-27

A second persistence mode, Settings-selectable, so resume vs live can be A/B'd.
- **engine.py**: extracted `claude_turn_events()` (shared one-turn stream parser; used by
  one-shot streaming and live mode) and `ClaudeCodeEngine.live_argv()` (persistent
  `--input-format stream-json --output-format stream-json --include-partial-messages`).
- **live_session.py** (new): a per-thread persistent `claude` process driven over stdin
  (stream-json user messages); `LiveSession.turn()` yields {status|token|done|error} for
  one turn; a registry with per-session lock, idle TTL (30m), LRU cap (4), dead-respawn,
  `drop()`, and `shutdown_all()` (atexit + FastAPI shutdown). No per-turn spawn cost;
  conversation is ephemeral (process lifetime) vs resume's durable on-disk session.
- **paper_chat.py**: `LivePaperAgent` (uses live_session); `get_agent` returns it when
  `chat_session_mode == "live"`, else the resume agent.
- **config/main/settings**: `chat_session_mode` (resume|live) default resume; a selector
  in the engine block (Claude only); chat delete drops the thread's live process.

Tests: dispatch (resume vs live) added. **111 tests pass**. Verified `--input-format
stream-json` multi-turn works, then live-tested: LivePaperAgent spawns ONE process,
reuses it across turns (registry stays at 1), recalls a codeword, and T2 (2.2s) is
faster than T1 (4.5s, incl. spawn). cwd-leak/skills/self-heal all still apply.

### PAPER_CHAT_AGENT status: Phases A, B, C DONE. Remaining: D — Codex sub-agent.

## PAPER_CHAT_AGENT Phase D — Codex sub-agent — BLOCKED/DEFERRED — 2026-05-27

Verified Codex's mechanics before building (the discipline that saved us on Claude MCP):
- `codex exec` blocks reading stdin unless stdin is closed (`< /dev/null`). [fixable]
- Codex CONNECTS to our stdio MCP server via inline `-c mcp_servers.pa.{command,args,env}`
  and the tool EXECUTES correctly (returned our real seed count) — but ONLY with
  `--dangerously-bypass-approvals-and-sandbox`.
- With any real sandbox (`-s read-only`/`workspace-write`) + `approval_policy="never"`,
  the MCP tool call is auto-CANCELLED ("user cancelled MCP tool call").

**Why deferred (not shipped):** the only flag that runs MCP tools headless is the full
dangerous bypass, which disables sandboxing entirely; and Codex `exec` has no
`--allowedTools` equivalent, so the model would also be free to run shell/writes —
violating the read-only invariant the paper chat depends on. Shipping that would break a
core guarantee, so per the project's rules we don't.

CONSEQUENCE: Codex users fall back to the classic stuffed paper chat (works on any engine
via llm.complete). The `PaperChatAgent` seam is in place, so a Codex sub-agent can drop in
later IF a safe path is found (likely: register the MCP server in the user's real
config.toml so it's trusted/auto-approved under sandbox, plus a non-global way to scope
the per-collection env). 111 tests pass.

### PAPER_CHAT_AGENT status: A, B, C DONE. D deferred (Codex headless MCP needs an unsafe
bypass). The Claude sub-agent — resume + live + skills + streaming + self-heal + debug —
is the complete, shipped feature.

### Phase D unblocked: Codex MCP runs sandboxed (no bypass) — 2026-05-27
Tested the user's plan: a Codex profile with sandbox_mode="read-only",
approval_policy="never", and per-tool approval_mode="approve" on our MCP tools.
RESULT (Case A): `codex exec --profile` ran paper_agent/get_unreasoned_seeds with
sandbox=read-only and returned the correct count — NO --dangerously-bypass needed. The
real gate was per-MCP-tool approval (separate from approval_policy), which the profile's
approval_mode="approve" auto-grants for only our read-only tools. Auth handled by copying
~/.codex/auth.json into a disposable CODEX_HOME (then removed). So a SAFE Codex sub-agent
is achievable: read-only sandbox blocks writes, tool-allowlist via per-tool approve, no
bypass. Remaining build for a Codex sub-agent: per-run config generation (profile + MCP
env scoped to the collection), session resume, --json event→{status|token|done} mapping,
dispatch + AGENTS.md skills. Per the plan, ship as experimental/off-by-default with
server-side session scoping.

## PAPER_CHAT_AGENT Phase D — Codex sub-agent — DONE (safe, experimental) — 2026-05-27

Built after the user's plan cracked the safe-sandbox path. The Codex sub-agent is SAFE
(no dangerous bypass): read-only sandbox + server read-only mode + read-only tool approvals.
- **mcp_server.py**: `WRITE_TOOLS` + `_readonly()` (PA_MCP_READONLY env). In read-only mode
  tools/list hides submit_*/ and _call_tool denies them — server-side enforcement.
  `stdio_mcp_config(slug, read_only=True)`. Claude/Live paper agents now also pass
  read_only=True (belt + suspenders with --allowedTools).
- **engine.py**: `codex_turn_events()` (parse `codex exec --json`: thread.started→id,
  mcp_tool_call→status, agent_message→final text, turn.completed→done); `CodexEngine.
  paper_stream()` builds inline `-c` config (MCP server scoped + PA_MCP_READONLY,
  approval_policy=never, sandbox read-only, per-tool approve on read tools) and runs
  `codex exec`/`exec resume` with stdin closed; `_paper_config_args()`; run_once gets
  --skip-git-repo-check.
- **paper_chat.py**: `_codex_system` (read_paper_text-based, read-only), `CodexPaperAgent`
  (stream/answer, thread_id via get/set_session_id), `get_agent` dispatches Codex.

Verified (each Codex mechanic first, then end-to-end): per-tool approve runs MCP under a
read-only sandbox on the real CODEX_HOME (no auth copy); resume recalls across turns;
live CodexPaperAgent read a PDF ("42.0") and resumed ("Blarg"). Tests: read-only mode
hides/denies write tools; codex_turn_events parsing; Codex dispatch. **114 tests pass.**

### PAPER_CHAT_AGENT COMPLETE: A (Claude resume), B (skills), C (live mode), D (Codex) —
all done. The per-paper chat is an interactive, streaming, read-only paper-reading
sub-agent on BOTH Claude Code and Codex, with classic stuffed-context fallback for OpenAI
/no-PDF. The read-only invariant holds on every engine.

### 2026-05-27 — Duplicate detection + merge (post-phase)
**Why:** Two same-title Zotero items (different keys) both imported into a collection
showed as separate papers — one with chat history, one empty. Not a sync bug (dedupe is
by zotero_key/arxiv_id; these differ), but the user had no in-app way to resolve it
(only "Delete PDF" existed, which keeps membership).
- **library.py**: `_norm_title`, `paper_engagement` (chat msgs / notes / highlights →
  `has_attention`), `find_duplicate_groups(slug)` (group by normalized title, recommend
  the most-engaged copy as `keep_id`, label `remove` when ≤1 engaged else `merge`),
  `merge_papers(keep_id, drop_ids)` (re-point chat_threads/annotations/triage_items/
  memberships to keep; notes merged so no text is lost; drop's per-id PDF + orphan row
  deleted; keep replaces drop everywhere), `_delete_orphan_paper`.
- **main.py**: `GET /c/{slug}/duplicates` (review panel), `POST /c/{slug}/merge`;
  `dup_count` in collection context.
- **templates**: `_duplicates.html` (per-group form: `keep_id` radio + all member ids as
  `drop_ids`, merge_papers drops the kept id → same form does merge & remove); collection
  toolbar gets an amber "⧉ Duplicates N" button opening a modal (HTMX-loaded body).
- Also fixed: tag popup z-index (footer border bled through the open popup) —
  `_tagbar.html` lifts to z-30 while open.
- Tests: `tests/test_merge.py` (detect/recommend, remove empty + orphan cleanup,
  combine notes + re-point work). **117 tests pass.** Verified live on the real `memory`
  collection: recommends keeping #44 (12 msgs) over the empty #42.

### 2026-05-27 — Re-seed paper sub-agent from stored history on expiry
**Why:** In resume mode the agent's working memory lives in Claude Code's session
transcript, GC'd after `cleanupPeriodDays` (default 30) idle. We always stored the
verbatim transcript in `chat_messages` (every turn, all paths), but the self-heal path
started a *cold* fresh session — so after expiry the displayed history survived while the
agent forgot it.
- **paper_chat.py**: `ClaudeCodePaperAgent` now sends only the new turn when resuming a
  live session, but **re-seeds** a fresh session (no id, or stale id) with the last
  `_RESEED_LIMIT=12` stored messages + the new turn (`_fresh_messages`; `_split_system`
  flattens to a USER:/ASSISTANT: transcript). First turn has no history → collapses to
  just the new turn (existing assertions hold). `_kwargs` no longer carries `messages`.
- Scope: resume mode (the mode in use). Live mode and Codex still start cold on
  respawn/expiry — candidate follow-ups, not done.
- Test: `test_fresh_session_reseeds_from_stored_history` (stale session → fresh call
  receives prior history + new turn last). **119 tests pass.**

### 2026-05-27 — Resume cold sessions via a chat-history TOOL (not prompt-stuffing)
**Why:** Reverted the previous re-seed (which stuffed history into the prompt). Per the
user's design: a fresh/expired session should LEARN that history exists and READ it
on demand via a tool, so both Claude Code and Codex recover context the same way.
- **mcp_server.py**: new read tool `get_chat_history(paper_id, limit=200)` — reads the
  paper's most-recent thread from `chat_messages`, returns a markdown transcript
  (truncates to most-recent N with a note). Registered in `_TOOLS` + `_call_tool`; it's a
  READ tool so it survives PA_MCP_READONLY mode. No file on disk (single source of truth).
- **paper_chat.py**: added `mcp__pa__get_chat_history` to Claude `_TOOLS` and Codex
  `READ_TOOLS`. `_resume_hint(paper_id, n_prior)` adds a one-line "RESUMING: call
  get_chat_history" note to the system prompt ONLY when a fresh session has stored history
  (`thread_message_count`); brand-new chats and live resumes get no hint. Reverted the
  prompt-stuffing `_fresh_messages`; the agent now always sends only the new turn.
- **Hardened the stream self-heal**: `claude --resume <stale-id>` exits 0 with an EMPTY
  `done` (no error) — so the old `st["done"]` check returned an empty reply. Now a
  non-terminal (resume) pass that ends with no tokens + empty done is swallowed and we
  fall back to a fresh session; the terminal (fresh) pass always finalizes. `answer()`
  gets the same guard (empty resume reply -> fresh).
- **Skill**: `app/skills/resume-paper-chat/SKILL.md` (Claude) teaches when to pull history;
  Codex gets the same instruction inline in `_codex_system` (skills are Claude-only).
- Tests: `get_chat_history` transcript + empty-thread case; fresh-with-history hints the
  tool and sends only the new turn (no stuffing); brand-new session has no hint. **121
  pass.** Live-verified on real paper 44: cold/stale resume -> self-heal -> agent calls
  get_chat_history -> recovers planted codeword "ZEBRA99"; stale session id replaced.
- Scope: Claude resume mode fully wired + verified. Codex tool+prompt wired (not live-run
  this round). Live mode unchanged (ephemeral by design).

### 2026-05-27 — Per-sub-agent skills (karpathy-wiki reviewed, contract kept)
Reviewed the GitHub `karpathy-llm-wiki` skill; it encodes the INVERSE philosophy
("LLM writes & maintains the wiki; human reads") and assumes web+filesystem write tools
our agents don't have. User decision: keep our contract, borrow only mechanics; deliver
via per-purpose skills-homes (Claude); enforcement stays in CODE (gate + allowlist),
skills are advisory.
- **agent_skills.py**: generalized to per-purpose homes via a manifest `_HOMES`
  (paper→agent-home keeps legacy name for session stability; organizer/debt/brainstorm/chat
  → <name>-home). `ensure_skills_home(home)` copies only that home's scoped skills;
  `skill_names(home)` likewise.
- **New skills** (re-expressed through propose→gate→accept, never direct writes):
  `organize-wiki` (organizer — placement into fixed sections, cascade to related pages,
  conflict-annotation with attribution, submit_proposal), `find-reading-debt` (debt —
  cluster seeds → pointed questions only), `brainstorm-open-questions` (brainstorm —
  speculative, gate-exempt), `answer-from-collection` (chat — grounded read-only Q&A).
- **Wiring**: organizer/debt(find+brainstorm)/agentic_chat cwd switched from the
  collection dir to their skills-home (they use MCP for data, so cwd was only for skill
  discovery / avoiding stray CLAUDE.md). Dropped now-unused COLLECTIONS_DIR imports.
- Tests: `test_per_agent_skills_homes_are_scoped`; fixed organizer/debt fixtures to
  monkeypatch agent_skills.APP_DIR (no more module COLLECTIONS_DIR). **122 pass.**
- Live-verified: a claude session in organizer-home discovers `organize-wiki`. NOTE: it
  also sees the user's global/plugin skills — pre-existing (paper-chat too), and harmless
  because the tool allowlist (not the skill) bounds what any agent can do.

### 2026-05-27 — Removed OpenAI entirely (CLI-only) + images for Claude & Codex
**Decision:** the app is now CLI-agent-only (Claude Code / Codex). No API backend, no API
key — every LLM feature requires a CLI agent installed (FakeEngine for tests).
- **engine.py**: deleted OpenAIEngine + the model-list helpers; `ENGINES={claude-code,codex}`;
  `select_engine_name` defaults claude-code (no key branch); removed `import re`.
- **config.py**: dropped `openai_api_key`; `model` default "" (engine default); reworded.
- **pyproject.toml**: removed the `openai` dependency.
- **main.py / _settings_form.html**: removed the API-key field + OpenAI engine option;
  model picker kept and now WORKS for CLI (Claude → sonnet/opus/haiku select; Codex →
  free-text). settings_post drops openai_api_key.
- **context.build_messages**: removed the inline image_url (vision) branch — CLI agents
  can't take inline images; the text-only classic path just notes attachments.
- **CLAUDE.md**: updated the locked "LLM: OpenAI SDK" stack line, the remote-calls rule,
  and the dependency allowlist to reflect CLI-only.
**Images now reach the paper sub-agent (both engines):**
- Client `stream()` sends `images_json`; `paper_chat_stream` + `chat_post` accept it.
- `paper_chat._materialize_images` decodes base64 data-URLs to temp files (cleaned up
  after the turn). Claude/Live: paths injected into the turn + "use Read to view"
  (Read is allowlisted). Codex: attached natively via `codex exec -i <file>` —
  `paper_stream` adds a `--` separator so the variadic `-i` doesn't swallow the prompt.
- Live-verified end-to-end on real paper 44: a pasted magenta PNG → Claude answered
  "Magenta", Codex answered "Magenta". **121 tests pass** (updated engine/organizer/debt/
  paper_chat/chat tests for the removal + the text-only build_messages).

### 2026-05-27 — Wiki lint (report-only): deterministic in code + heuristic agent pass
Salvaged karpathy's Lint, re-expressed for our model: it REPORTS, never auto-edits.
- **Deterministic (code, `wiki.lint_wiki`)**: already had broken-link / orphan / no-outlink;
  added **index drift** — section pages missing from index.md (`index-missing`) and index
  entries pointing at gone pages (`index-stale`).
- **Heuristic (`wiki_lint.deep_lint`)**: a READ-ONLY agent pass (read_wiki_page /
  search_fragments / get_fragment / get_unreasoned_seeds; NO submit tools; read_only MCP;
  `lint` skills-home with the new `lint-wiki` skill). Reports contradictions, stale claims,
  missing conflict annotations, coverage gaps — grounded, never proposes/writes. Agentic on
  claude-code; non-agentic engines get the deterministic checks only.
- **Route/UI**: `POST /c/{slug}/wiki/lint` → `_lint_panel.html` (structure findings +
  content-review markdown, report-only); "Health check" button in the Wiki panel.
- Tests: index-drift (`test_lint_detects_index_drift`); deep_lint dispatch + read-only
  allowlist (`tests/test_wiki_lint.py`). **124 pass.** Live-verified: seeded two
  contradicting pages → the agent caught the contradiction AND the missing conflict
  annotation, then the throwaway wiki was removed.

### 2026-05-27 — Real "Remove from collection" action (Delete PDF ≠ remove)
**Bug report:** "removed a paper, still in collection." Root cause: 🗑 Delete PDF keeps the
paper by design (PDF-only), and there was no standalone remove — only the duplicate-merge.
Also note `library.refresh()` re-imports every Zotero paper, so any in-app removal of a
paper still in Zotero returns on Refresh.
**Decision (user):** Zotero stays the source of truth (no tombstone); add a distinct
Remove action.
- **main.py**: `POST /c/{slug}/papers/remove` → `library.remove_membership` per id
  (membership only; paper/PDF/notes/chat kept globally).
- **collection.html**: bulk bar now has "Delete PDF" (neutral, PDF-only) AND a rose
  "✕ Remove from collection" with `removePapers()` — confirm dialog warns it's kept
  globally and that a Zotero paper returns on Refresh (remove in Zotero to remove for good).
- Test: `test_remove_membership_drops_from_collection_only`. **125 pass.**

### 2026-05-27 — Staged removal + Sync deletes from source (Delete PDF → one Remove)
**User model:** removal is staged locally (hides now, survives Refresh) and applied to the
source only on Sync, which DELETES the item from Zotero. One Remove action replaces the
old Delete-PDF + Remove buttons. (Supersedes the earlier "no tombstone" answer.)
- **db.py**: new `pending_removals` table (added via SCHEMA IF NOT EXISTS — no reset).
- **library.py**: `stage_removal` (queue + drop cached PDF), `list_pending_removals`,
  `pending_changes`; `list_papers` and the landing paper-count exclude staged papers;
  `sync_candidates` gains `to_delete`; `remove_membership` also clears the pending row.
- **zotero.py**: `LocalZotero.delete_item` (DELETE /items/{key}, optimistic version, 404 ok).
- **sync.py**: `push` applies `to_delete` (delete_item → remove_membership), reports
  `deleted`; `preview` carries `to_delete`.
- **main.py**: `/c/{slug}/papers/remove` → `stage_removal`; `pending` in collection ctx.
- **UI**: bulk bar = single "🗑 Remove" (staged-removal confirm); Sync button shows an
  amber pending badge ("Sync — N add, M remove"); sync.html has an "Apply sync" button +
  a rose "Delete from Zotero" preview with a destructive confirm; done shows `deleted`.
- Note: dir export needs no delete step — it re-writes a snapshot, so a removed paper is
  simply absent next export. Refresh leaves staged papers hidden (membership row kept).
- Tests: `test_stage_removal_hides_keeps_row_and_queues`,
  `test_sync_push_applies_staged_deletion` (+FakeZotero.delete_item). **127 pass.**
  (Zotero delete not live-tested — destructive; covered by the fake.)

### 2026-05-27 — Two-way Sync (Phase 1 of the Sync/Export split)
Per the user's vision: Sync becomes two-way (pull + push) with its own button; the ⇪ icon
becomes a one-shot Export (Phase 2, next). Refresh button removed (Sync subsumes pull).
- **library.py**: `pull_preview(z, slug)` — read-only diff of the linked Zotero collection
  vs local (incoming_new / incoming_gone), no writes.
- **sync.py**: `two_way_preview(slug)` (incoming + outgoing, Zotero-down → outgoing only);
  `start(two_way=True)` pulls (refresh) then pushes, reporting pulled_new/pulled_removed.
- **main.py**: `sync_preview` shows the two-way preview; `POST /sync` runs `start(two_way=True)`.
- **collection.html**: removed the ↻ Refresh button; new ⇅ "Sync with Zotero" button (linked
  collections) carrying the pending badge; ⇪ relabeled "Export…" (popup reshape is Phase 2).
- **sync.html**: two-way view — ⬇ Incoming (new from Zotero; dropped-in-Zotero kept+flagged)
  and ⬆ Create/Delete; one "Apply sync" (pull→push); done shows pulled + created + deleted.
  Zotero-side drops stay flag-and-keep (non-destructive).
- Test: `test_pull_preview_diffs_zotero_vs_local`. **128 pass.**
- DEFERRED to Phase 2: reshape ⇪ Export → new Zotero collection (copy, unlinked) /
  directory of PDFs (no bibtex) / BibTeX-as-copyable-text.

### 2026-05-27 — Sync as a modal + reshaped Export (Phase 2)
**Sync modal:** the two-way Sync is now a popup, not a page. `_sync_panel.html` is the
fragment; `sync.html` includes it for direct nav; `sync_preview`/`sync_push` return the
fragment on HX-Request. The ⇅ Sync button opens a modal (`syncWith`) loading the fragment;
the Apply form hx-posts back into `#sync-modal-body` and polls /status in place.
**Export (⇪) reshaped** into a one-shot copy with three modes (none ongoing/linked):
- **New Zotero collection** — `sync.export_to_new_zotero(slug, name)`: creates a fresh
  collection; existing items added to it (no dupe), local-only papers created (+PDF);
  does NOT link this collection. Route `POST /export/zotero`.
- **Directory of PDFs** — `export_dir.export_pdfs(slug, dest)` clones every cached PDF
  (filename = citekey.pdf; ensure_cached fetches missing). Replaces the old .bib-file
  export. Route `POST /export/dir` (reshaped).
- **BibTeX text** — `GET /export/bibtex` returns `to_bibtex` as text; popup shows it in a
  readonly textarea with Copy (no file).
- Tests updated/added: `test_export_to_bibtex_text`, `test_export_pdfs_copies_and_rejects_relative`,
  `test_export_to_new_zotero_copies_unlinked` (+FakeZotero.add_item_to_collection). **129 pass.**

### 2026-05-27 — Paper-list UX: selection-click, read state, per-paper tags
1. **Selection mode click** — with the ▾ tools open, clicking a paper row now toggles its
   checkbox (`toggleOne`) instead of opening the paper; the Preview/no-PDF buttons are
   hidden in selection mode.
2. **Read/unread** — `collection_papers.read_at` (migration, additive). Opening a paper
   marks it read (`mark_read_if_unread` in paper_page); unread rows show a sky dot + bold
   title, read rows dim. Bulk bar gains "Mark read / unread" (`POST /papers/mark-read`).
3. **Per-paper tags** — `collection_papers.tags` (JSON). A compact tag bar under each row
   (`paperTagBar` in base.html → `POST /c/{slug}/p/{id}/tags`). Tags COUNT AS ATTENTION:
   `paper_engagement(paper_id, slug)` includes the tag count, and `find_duplicate_groups`
   passes slug, so a tagged duplicate is the recommended keep.
- Tests: `test_mark_read_open_and_toggle`, `test_paper_tags_count_as_attention`. **131 pass.**
  Migration verified on the real app.sqlite (read_at + tags added, no reset).

### 2026-05-27 — Paper-list UX follow-ups
- **Mark read/unread no longer reloads**: read state is reactive (`collectionView(readMap)`,
  `read` map); the unread dot, the "new" badge, and title dim all bind to it. Marking
  read clears the "new" label in place.
- **Added date** setting removed (config/settings/form); the date now auto-shows on each
  row only in selection mode (▾ toggled).
- **Per-paper tag adding** moved to the paper page (a tag bar under its toolbar). On the
  collection list the "+ tag" button shows only in selection mode; pills stay visible.
- `get_paper_tags(slug, pid)` added; paper_page passes `paper_tags`. **131 pass.**

### 2026-05-27 — Configurable highlight scheme + uncolored paper tags + unread counts
**Highlight scheme (user-editable):** `config.highlight_scheme()` + `DEFAULT_HIGHLIGHT_SCHEME`
(Yellow=core claim, Blue=evidence, Green=understood, Pink=confusing, Orange=connection).
Settings has a `schemeEditor` (rows of color+label, add/remove). On save, if colors changed,
`annotations.remap_to_scheme()` recolors every highlight to the nearest new color. The
legend moved to the app header (base.html `header_center` block, paper.html fills it) and is
now DISPLAY-ONLY (the `.ann-color` click handler is gone; pick color from the selection
popup). `annotate.js` reads PALETTE/defaultColor from the frame's `data-scheme`; the
selection-toolbar swatches render from the scheme.
**Paper tags reworked:** now plain uncolored hashtags (strings), with recommended defaults
(`DEFAULT_PAPER_TAGS`) + a "most used" history (`frequent_tags`/`tag_suggestions`) shown as
quick-pick chips. `_load_tags`/`set_paper_tags` normalize to deduped strings (tolerate the
old {label,color}). Tag bar on the paper page (primary) + collection rows ("+tag" only in
selection mode). Tags still count as attention in duplicate merge.
**Landing cards:** removed the "N new" badge; show "N unread" next to "N papers"
(`unread` added to the collections aggregation).
- Tests: remap-to-scheme, tag dedupe/suggestions (+ earlier read/tags). **133 pass.**
  Live-verified: landing unread, settings scheme editor, paper header legend + data-scheme.

### 2026-05-27 — Reading log (Previous paper), Tags tab, 3 legend options
- **Reading log (per-collection, browser-style walk-back)**: `reading_log` table (recency
  order, opened_at bumped on open, pruned to `reading_log_cap` default 100, set in
  Settings). `log_open`/`previous_in_log`; paper_page logs opens except `?nav=back`
  (preserves walk-back order). New ⏮ button → `GET /p/{id}/prev` (JSON) → navigates with
  `?nav=back`, or alerts "first paper you've read" at the start.
- **Tags → a "Tags" tab** in the right pane (removed the toolbar tags row); uncolored
  hashtags + suggestions.
- **Highlight legend: all 3 placements shipped to compare** — (1) floating card over the
  PDF, (2) top of the Highlights tab, (3) header popover. User will pick one to keep.
- Read/unread no longer dims the title (kept the unread dot only).
- Tests: reading-log walk-back + cap. **134 pass.**

### 2026-05-27 — UI consistency pass (legend, tags removed, graveyard, merge→sync, dialogs, new-collection)
- **Per-paper tags removed entirely** (user reverted the idea): dropped the Tags tab, tag
  bars, `paperTagBar`, `/p/{id}/tags`, tag library fns, and tag-attention counting. The
  `collection_papers.tags` column stays (dormant, ignored).
- **Highlight legend** now lives inline in the MIDDLE of the paper toolbar (display-only;
  removed the floating/header/tab options). New Setting "Show the highlight legend"
  (default on, `show_highlight_legend`).
- **Read/unread** no longer dims the title (unread dot only).
- **Graveyard** (🪦 header button + badge): staged removals, newest first, metadata
  preview, Select-all → Restore (restore-only). PDF stays dropped (metadata-only preview).
- **Move** is now a popup (single "Move to…" → collection picker).
- **Merge → Sync**: a merged-away duplicate WITH a Zotero item is now staged SILENTLY for
  deletion (`stage_removal(..., silent=True)`; `pending_removals.silent`), so the next Sync
  deletes the redundant Zotero item; it's hidden from the graveyard. Local-only drops are
  hard-deleted. `merge_papers(slug, keep, drops)` now takes the slug.
- **New collection** is a source-chooser popup (Create locally / Import from Zotero /
  Directory + Other "coming soon"); folded in the old Import button.
- **In-app dialogs**: `window.paConfirm`/`paAlert` modal in base.html replaces every native
  confirm()/alert(); `htmx:confirm` is intercepted so hx-confirm uses it too. Converted all
  call sites (collection/chat/thought deletes, remove, merge, sync, prev-paper).
- Migrations (additive): `pending_removals.silent`, `reading_log` (prior). **134 pass.**

### 2026-05-27 — New-collection import wizard (2-layer) + directory import
- **2-layer wizard** (index.html `newWizard`): layer 1 = source (Create local / Import from
  Zotero / Directory / Other-soon); layer 2 = configure the chosen source — editable name,
  optional colored collection tags, and a live scrolling paper/file preview (no PDF preview)
  before importing. Replaced the old multi-select Zotero import.
- **Zotero (1:1, sync-able)**: `GET /collections/zotero-preview/{slug}` (titles/authors),
  `POST /collections/import-zotero` (slug,name,tags,copy_mode) → `activate(name=…)` (custom
  name; link is by collection id) + `set_tags`.
- **Directory import (new)**: `library.import_directory(name, path, tags)` scans a folder's
  *.pdf, makes a local collection, each PDF → a paper (title from PDF metadata else filename)
  with the file copied into the store; `scan_directory_pdfs` previews filenames.
  Routes `POST /collections/dir-preview` + `/dir-import`. (origin app-created; PDFs copied
  eagerly since the source folder may move.)
- Fixed: added `from pathlib import Path` to library.py; dir papers use origin app-created
  (CHECK constraint). Test `test_import_directory_creates_local_collection`. **135 pass.**

## 2026-05-27 — Pull-only Zotero model + two-tier graveyard

**Root cause found:** Zotero's *local* API (`localhost:23119`) is **read-only** — `DELETE`/`PATCH`
return 501 "Method not implemented", `POST` returns 400. So every write-back operation
(create item, delete item, add/remove from collection, upload PDF) silently failed. The
spec's assumption that "the HTTP API" could write was wrong for the local API. (Also fixed
discover.py: arXiv API was `http://` → 301 redirect not followed → "Add paper" failed; now
`https://` + `follow_redirects=True`.)

**Decision (user):** drop write-back entirely; **pull-only**. Import + manual Pull bring in
newly-added Zotero papers; nothing is ever pushed/deleted in Zotero.

What changed:
- `pending_removals` gains `status` (`graveyard`|`deleted`); `silent` stays for merged dups.
  Migration adds the column (default `graveyard`).
- **Pull** (`library.refresh` / `pull_preview`): auto-adds only truly-new papers. Papers
  matching a prior removal (by zotero_key → arXiv → normalized title) are **held** into a
  re-add picker (select-all); `refresh(..., readd_keys=[...])` restores the picked ones.
- **Two-tier graveyard**: Graveyard (Restore / Permanently delete) + Permanently-deleted
  tombstones (Restore / Purge). Permanent delete keeps the paper's work (recoverable);
  Purge forgets the tombstone + all work (`_purge_orphan_paper`) so a Pull may re-add fresh.
  New: `list_deleted`, `permanently_delete`, `purge_removals`, `removed_index`,
  `_removed_paper_ids`. Routes `/graveyard/delete` + `/graveyard/purge`.
- Removed push paths: `sync.push`, `export_to_new_zotero`, `/sync/removals`, `/export/zotero`,
  `sync_candidates`, `pending_changes`. `sync.py` is now pull-only; Sync button → "Pull".
  Settings: dropped the write-API base/key fields (unused).
- `merge_papers` now returns `remembered` (was `staged_for_delete`); silent removal suppresses
  re-add on Pull instead of "delete on sync".
- Templates: `_sync_panel.html` rewritten (pull + held picker), `_graveyard.html` two tabs,
  copy updated. **134 pass.** Verified Pull + graveyard modals in-browser (0 console errors).

Deferred/decisions: arXiv-fallback re-add of a title-matched item with a *new* key creates a
fresh copy (old tombstone stays) — acceptable edge case. Zotero write methods remain on
LocalZotero as dead code (kept for the future WebZotero web-API path per the ADR).

## 2026-05-27 — Add-paper progress, collection identity, import selection, UI polish

- **Add paper**: the arXiv entry is inserted immediately; its PDF downloads in a background
  thread (`pdf_store.fetch_arxiv_pdf_async` + in-memory `_FETCHING` set). The row shows a
  "downloading…" spinner that self-polls `GET /c/{slug}/p/{id}/pdf-status` (fragment
  `_pdf_status.html`) and flips to Preview when the file lands.
- **Removed push-era badges**: paper-row "local only" and card "X to sync"/"in sync"
  (pull-only model; only a "● local" marker for unlinked collections remains).
- **Pull icon** ⇅ → ⬇; chat header "Chat" → "model".
- **Collection identity** (decisions: shared papers + delete-cascades; delete spares papers
  in other collections; rename in header keeps picker):
  - Slug is now a stable unique id (`_unique_slug`), decoupled from the display name. Pull
    resolves the linked Zotero collection by stored `zotero_name` (new column) so a local
    rename never breaks the link. `activate` creates a NEW collection per import (returns the
    new slug) — importing the same source twice yields two independent collections.
  - **Inline rename** (`rename_collection`, unique-name enforced) via a pencil on each card
    (`cardEdit()` Alpine; `POST /c/{slug}/rename`).
  - **Duplicate** (`duplicate_collection`) clones membership, tags, read-state, reading log,
    highlights and chat into a fresh "(copy)"; notes stay shared (global per paper).
    Card duplicate icon → `POST /c/{slug}/duplicate`.
  - **Delete cascades**: removes this collection's work + memberships, then purges papers
    that belong to no other collection (`_purge_orphan_paper`) — so a re-import is fresh.
    Papers still in another collection are kept there.
- **Import selection**: the wizard's Zotero and Directory previews now have per-paper/-file
  checkboxes (default all, select-all header, import button disabled at 0). `activate
  (only_keys=)` / `import_directory(only_files=)` honor the subset. Directory import already
  had name + tags (shown after Scan).
- Removed the dead write-API-key settings fields (pull-only).
- Tests: lifecycle (unique slug, rename, duplicate, delete-cascade) added; activate now
  returns the new slug (callers/tests updated). **138 pass.** Verified in-browser (0 console
  errors): wizard selection, card rename/duplicate, Pull ⬇, "model" label, pdf-status poll.

Deferred: unselected papers at import aren't remembered as removals — they'll appear as
incoming_new on a later Pull (acceptable; "default all" is the norm).

## 2026-05-27 — Add-paper wizard (multi-URL → parse → pick → download with % ring)

Replaced the single-line arXiv Add box with a popup wizard:
- Paste a chunk of arXiv **and** OpenReview URLs/ids (newline- or comma-separated) →
  `POST /c/{slug}/papers/parse` (`discover.parse_add_input`) fetches each title/authors and
  flags unparseable tokens + ones already in the collection.
- A checklist (default **none** selected, with select-all); only valid, non-dup entries are
  selectable. `POST /c/{slug}/papers/add` (`triage.add_entries`) creates the selected papers
  and starts a background streaming download for each.
- **Real % progress ring**: `pdf_store.start_download` streams the PDF (arXiv or OpenReview),
  tracking received/total bytes (`_DOWNLOADS`); falls back to an indeterminate spinner if no
  Content-Length. The whole paper row is re-rendered by a self-chaining poll (`_paper_row.html`,
  `GET …/pdf-status`) so the ring, title and Preview update together.
- **Downloading papers are not openable**: while fetching/failed the title is plain text (no
  link) and there's no Preview. On **failure**: the row shows **Retry** (`…/retry-download`)
  and **Remove** (`…/drop` → `library.drop_paper`, a hard delete that does NOT go to the
  graveyard). On success the ring → Preview and the title becomes a link.
- New: `papers.openreview_id` column (+migration); `openreview.fetch_metadata`;
  `upsert_paper(openreview_id=…)`; `_has_pdf` counts OpenReview; `get_collection_paper`.
- Tests: parse (arXiv/OpenReview/bad), add_entries (+OpenReview id, eager download kicked off),
  drop_paper (no graveyard), download percent/states. **142 pass.** Verified in-browser
  (0 console errors): wizard parse + selection, and ring/failed/done row fragments.

## 2026-05-27 — Add-wizard: adding a removed paper restores it

Fixed an edge case: pasting a paper that's currently in the Graveyard or Permanently-deleted
tier used to silently do nothing (it re-used the existing row but left the `pending_removals`
row in place, so the paper stayed hidden). Now a manual Add is treated as explicit intent and
**restores** the paper in either tier — `triage.add_entries` calls `restore_removal` after
adding, clearing the removal (the tombstone only suppresses *automatic* Pull re-add, not a
deliberate add). The wizard labels such entries via `library.removal_tier`: "in graveyard —
adding restores it" / "permanently deleted — adding restores it" (selectable). Test
`test_adding_a_removed_paper_restores_it`. **143 pass.**

## 2026-05-27 — Fix: highlights leaking across collections

Bug: app highlights are stored with a `collection_slug`, but `annotations.list_app`/`list_all`
queried by `paper_id` only. Since the same Zotero paper shared by two collections is one
`papers` row, highlights made in collection A showed up when viewing the same paper in B.
Fix: `list_app(paper_id, slug)` / `list_all(paper_id, slug)` now filter by `collection_slug`;
threaded the slug through every display caller (annotations JSON endpoint, chat-context
`_highlights_block`, MCP `get_paper_context`). Zotero-origin read-in stays per-paper (it's the
same external PDF's annotations). Test `test_highlights_are_scoped_per_collection`. **144 pass.**

## 2026-05-27 — Import wizard: show all Zotero collections + folder explorer

- **Zotero picker no longer hides already-imported collections** (re-import makes an
  independent collection now). The landing route offers ALL Zotero collections.
- **Zotero picker is a scrollable, filterable list** (was a `<select>`): a search box +
  max-h scroll list of clickable collections; the picked one highlights. (Fixed a
  tojson-in-double-quoted-attribute crash by passing the name via `data-cname`/`$el.dataset`.)
- **Directory import is now a folder explorer** (was a single path input): `browse_directory`
  + `GET /collections/dir-browse` list one level (subfolders + PDFs); the UI has an ↑ up
  button, an editable path bar, a clickable subfolder list, and the current folder's PDFs as a
  select-all checklist that feeds the import. Starts at the home directory.
- Tests: `test_browse_directory_lists_dirs_and_pdfs`; fixed a flaky delete-cascade test that
  wrote a note via the un-wired notes module. **145 pass.** Verified in-browser (0 errors):
  Zotero list incl. already-imported, folder navigation up/down.

## 2026-05-27 — New-collection wizard polish

- **Zotero collection list redesigned**: card rows (amber 📚 icon tile + name + "Zotero
  collection" subtitle + hover chevron) in a scrollable area with a search-icon filter box —
  replaces the plain text list.
- **Picking a collection now opens a dedicated settings layer** (`step='zsettings'`): name,
  tags, PDF copy mode and per-paper selection live there, with ← back to the list. The list
  layer is now just the picker.
- **Reopening always resets to the top layer**: both "+ New collection" buttons call a new
  `open()` that sets `step='source'` and clears prior Zotero/directory/tag state, so the
  wizard never reopens mid-flow. Back button is layer-aware (`back()`: zsettings→zotero,
  else→source). **145 pass**; verified in-browser (0 console errors): open→source,
  pick→zsettings, back→zotero, reopen→source.

## 2026-05-27 — Directory import: deterministic PDF metadata extraction

Directory-imported PDFs now get real metadata without an LLM, in priority order:
1. **arXiv id** — from the filename (e.g. `2409.14485v4.pdf`) or the page-1 `arXiv:NNNN.NNNNN`
   watermark → authoritative title/authors/year via the arXiv API (`fetch_arxiv_batch`, one
   chunked request for the whole folder).
2. **Embedded PDF metadata** title/author (XMP/Info).
3. **First-page-text heuristic** (`_heuristic_pdf_meta`): title = leading line(s) before
   "Abstract" (a line ending in ':' joins the next — handles wrapped titles); authors = the
   next non-email line. Verified on a real folder: `2409.14485v4.pdf` → "Video-XL…" (arXiv),
   `DeepSeek_V4.pdf` → "DeepSeek-V4: Towards Highly Efficient Million-Token Context
   Intelligence" / "DeepSeek-AI" (heuristic).
4. Filename stem (last resort).
Tests: `test_heuristic_pdf_title_authors_from_first_page`,
`test_import_directory_resolves_arxiv_metadata_from_filename`. **147 pass.**
Also: removed the stale plaintext `openai_api_key` from `~/.paper-agent/config.toml`.

## 2026-05-27 — Import wizard: checkbox consistency, unique names, async import

- **Checkbox consistency**: the directory/Zotero pick lists used index-based `x-for` keys, so
  navigating reused checkbox DOM with stale `checked` (header said "all selected" but rows looked
  unchecked). Fixed with stable keys (`:key="n"` / `p.key`) + explicit `:checked="sel.includes(...)"`
  + `@change` toggle (directory submits via hidden inputs; Zotero keeps `name="paper_keys"`).
- **Unique collection names**: client-side warning + disabled submit on every create path
  (create-local, Zotero import, directory import) via `nameTaken` (case-insensitive); server-side
  backstop `_require_unique_name` → 409. (Rename already enforced uniqueness.)
- **Async import**: `import_directory_async` / `activate_async` create the collection row
  immediately and parse/copy in a background thread (`_IMPORTS` tracker + `is_importing`). The
  routes redirect to the landing right away; the new card renders grayed with a spinner
  ("Parsing… importing papers"), isn't clickable, and polls `GET /collections/{slug}/import-status`,
  reloading into a normal card when state != running. `import_directory`/`activate` kept
  synchronous for tests. Tests: `test_import_directory_async_marks_importing_then_done`. **148 pass.**
  Verified the full flow in-browser (parsing card → clickable card; 409 on dup; client warning).

## 2026-05-27 — Starter wiki seeded on import (contract amendment)

User-approved exception to the "wiki = the user's thinking / only accepted proposed edits write
the wiki" contract: a default-on (uncheckable) import option drafts a starter wiki from the
papers' ABSTRACTS via the LLM. `wiki.draft_from_abstracts(slug)`:
- **Non-destructive** — only runs when the wiki has no pages, so it never overwrites the user's
  own writing.
- **Honestly tagged** — each seeded section page carries `author_origin: agent`,
  `generator: abstracts`, and an in-body banner ("Starter draft… not your own synthesis yet").
- **Graceful fallback** — no abstracts or no CLI agent → falls back to an empty scaffold
  (index/log + section dirs); the import never fails on it.
Runs inside the async-import background thread (covered by the "Parsing…" card). Threaded
through `import_directory_async` / `activate_async` and the import routes (`draft_wiki` form
field); a "Draft a starter wiki from the abstracts" checkbox (default checked) in both wizard
layers. CLAUDE.md amended to document the exception. Tests:
`test_draft_from_abstracts_writes_tagged_seed`, `test_draft_falls_back_to_scaffold_without_llm`.
**150 pass.**

## 2026-05-27 — Fix: starter wiki produced nothing for abstract-less imports

"no wiki" repro: a Zotero collection of bare PDF *attachments* (no `abstractNote`) imported with
empty abstracts, so `draft_from_abstracts` had no material and (correctly) fell back to an empty
scaffold. Fix: when the DB/Zotero abstract is empty, extract the abstract from the paper's cached
PDF — `wiki._pdf_abstract` reads pages 1–2 and `_extract_abstract` takes the text after an
"Abstract" heading up to Introduction/Keywords. Verified on a real collection: 5 papers → real
1.4–1.8k-char abstracts → a genuine wiki (problems/methods/… bulleted with `[ref]` citations).
Also: capped the LLM digest (≤60 papers / 36k chars); added a "✦ Draft starter wiki from
abstracts" button on the empty Wiki tab (`POST /c/{slug}/wiki/draft`) so existing collections can
be seeded without re-import; `rebuild_index` summaries now skip the banner/blockquote line.
**150 pass.**

## 2026-05-27 — Starter wiki reworked: problem-oriented interactive overview

Replaced the dry "bulleted abstract summary" seed with a problem-oriented, reading-inspiring
**open-problems map**, per the user's redirect.
- **Skill**: `app/skills/starter-wiki/SKILL.md` (a real project skill; added to the `wiki` home).
  Instructs the model to act as an editor, not a summarizer: frame the collection as 3–6 open
  problems, each with a hook (why it's hard/matters), competing approaches, a "read first" paper
  + why, and an open tension; plus an intro hook and a reading path. Strict JSON, abstract-grounded.
- **Generation**: `wiki.generate_overview(slug, force=)` loads the skill body as the system prompt,
  feeds the abstract digest (capped), validates the JSON, and **drops any ref not in the
  collection** (hallucination guard). Stored as structured `wiki/overview.json` tagged
  `_meta.generated_by=agent`. `wiki.load_overview` resolves every ref → paper object.
- **Interactive HTML** (`_wiki_overview.html`): collapsible problem cards (Alpine), a "Start here"
  reading path, read-first/approaches/tension callouts, and every paper a link that opens the
  reader. The notes-based Phase-5 wiki moved under a collapsible "Build from your own notes" section.
- **Abstracts for bare-PDF Zotero imports**: `_collection_abstracts` falls back to extracting the
  abstract from the cached PDF (`_pdf_abstract` / `_extract_abstract`) when Zotero has none — the
  reason the first attempt produced "no wiki".
- Wired into async import (default-on `draft_wiki`) and the on-demand "Draft starter wiki" button.
  CLAUDE.md amendment updated. Tests: validate/drop-refs, ref-resolution, no-abstracts → False.
  **151 pass.** Verified end-to-end with a real `claude` run on the `distill` collection:
  3 question-framed problems, reading path, clickable papers — 0 console errors.

## 2026-05-27 — Settings UI: layered sidebar + panel

Reorganized the one-big-form settings modal into a sidebar+panel layout (macOS System Settings
style) without changing the backend. `_settings_form.html` now has a category sidebar
(Engine & model · Zotero · Storage · Reading & display · Advanced) and a panel area; Alpine
`cat` selects the visible panel via `x-show` (NOT x-if — every input stays in the DOM, so the
single Save still submits all categories). Verified in-browser: switching categories, editing a
field in a hidden panel, and the form still carrying every field (reading_log_cap, model,
highlight_scheme, pdf_store_path) on submit. Modal widened to 760px; standalone /settings page to
max-w-3xl. **151 pass.**

## 2026-05-27 — Agents page (sub-agent skills/tools/permissions; editable skills)

New 🤖 nav icon (next to ⚙) → `/agents`, a full page listing all 7 sub-agents (paper, chat,
organizer, debt, brainstorm, lint, wiki). Each card shows the agent's **job**, its **MCP tool
allowlist + permissions read-only** (the lethal-trifecta boundary stays enforced in code), and
its **editable skills**.
- Registry `app/agents.py` resolves each agent's tools from the *real* spawn-site constants
  (`paper_chat._TOOLS`, `agentic_chat.CHAT_TOOLS`, `debt._FIND_TOOLS`/`_BRAINSTORM_TOOLS`,
  `organizer._TOOLS`, `wiki_lint._TOOLS`) + descriptions from `mcp_server._TOOLS`; write tools
  (submit_*) flagged — so the page can't drift from what agents actually run with.
- Skill editing is a **per-user override layer**: edits save to `~/.paper-agent/skills/<name>/
  SKILL.md`; `agent_skills.{read_skill,save_skill,reset_skill,is_customized,effective_skill_file}`
  prefer the override (loader + `ensure_skills_home` overlay it), with a per-skill **Reset to
  default**. Shipped defaults are never mutated and survive updates. Routes
  `GET /agents`, `POST /agents/skill/{name}`, `.../reset` (HTMX swaps the skill card).
- Tests `tests/test_agents.py` (registry shape + real tools; override save/reset; loader prefers
  override; unknown-skill guard). **154 pass.** Verified in-browser (0 console errors).

## 2026-05-27 — Agents page: tabs + click-to-edit skill modal

Reworked the Agents page from one long scroll into tabs: one tab per sub-agent (Paper reader,
Collection chat, Wiki organizer, Reading-debt finder, Brainstormer, Wiki health check, Starter
overview); the selected tab shows that agent's job, read-only MCP tools/permissions, and a
compact skills list (name + description only). Clicking a skill opens a modal editor (loaded via
`GET /agents/skill/{name}/edit` → `_skill_editor.html`); Save/Reset re-render inside the modal.
Routes unchanged except the new edit-fragment GET and POST/reset now return `_skill_editor.html`
(removed `_agent_skill.html`). **154 pass.** Verified in-browser (0 console errors).

## 2026-05-27 — Agents page: carded sections

Each agent panel is now two cards: a **Scope** card (🔒 permissions + MCP tool chips, tagged
"read-only · enforced in code") and a **Skills** card (tagged "editable · click to edit"), with
the job as a lead line. Makes the locked-vs-editable split obvious at a glance. Template-only
change. **154 pass.**

---

## 2026-05-27 — Research Workspace landing redesign + image-driven theming

Redesigned the landing page to the "Research Workspace" mockup and added a user-uploadable
color theme.

**Theming (the headline feature).** A user uploads an image; the palette is extracted
**client-side** in a `<canvas>` (`extractPalette` in `index.html` — pixel quantization +
HSL derivation), **no server image library and no LLM** (chosen over a Pillow script / vision
agent: deterministic, zero new deps, zero tokens, live preview). Only the resulting hex values
+ the image file are persisted (`app/theme.py` → flat `theme_*` keys in `config.toml`, image at
`~/.paper-agent/hero.<ext>`). The palette is applied **app-wide in light mode** via CSS variables
(`base.html` `<style id="pa-theme-vars">`, scoped `html.pa-themed:not(.dark)`): backgrounds
(`bg-slate-50`/`bg-white`) and the primary accent (`bg-slate-900` buttons/active states) are
remapped; heading text stays near-black for readability. **Dark mode is untouched** (the custom
palette is a light-mode skin). Fully reversible: "Reset to default look" (`POST /theme/reset`).
The uploaded image doubles as the hero illustration (`GET /hero-image`; default inline SVG until
one is uploaded). `pa_theme` is a Jinja render-time global so every page themes without per-route
plumbing.

**Rest of the new look.** Serif "Research Workspace" hero + calm copy; aggregate **stats bar**
(Papers/Highlights/Notes/Unread — real counts via `library.workspace_stats()`); a **search bar**
wired to FTS over papers + notes (`GET /search` → `library.search()`, `_search_results.html`),
focusable with **⌘K** (handler in `base.html`); a **grid/list view toggle** (persisted in
`localStorage`, list mode = single column + heatmap hidden); a **"Recently opened"** line per
card (most-recent `reading_log` entry, `library._ago()` relative time).

New routes: `GET /hero-image`, `GET /search`, `POST /theme`, `POST /theme/reset`. New module
`app/theme.py`; new templates `_search_results.html`; `index.html`/`base.html` reworked. New
deps: none. Tests: `tests/test_theme.py` (theme roundtrip/reset, hex validation, stats, search).
Verified in-browser (Playwright): palette extraction returns valid hexes, apply themes the app
end-to-end (body bg + accent button recolored, hero swapped), reset reverts cleanly, list/grid
toggle works, 0 console errors. **157 pass.**

### Addendum — paired wallpaper presets

Extended theming with **full-page background wallpapers** offered as a **preset** (default
stays plain — user opts in via 🎨 Customize). Shipped a matched light/dark pair in
`static/themes/{light,dark}.png`: the light cream watercolor paints the page background in
light mode, the moonlit-lake scene in dark mode, **auto-switching with the 🌙 toggle**
(`base.html` `html.pa-themed:not(.dark) body` / `html.pa-themed.dark body` background-image
rules; fixed cover; cards/panels stay opaque on top for legibility). The preset derives a
cohesive palette client-side — backgrounds/ink from the light image, **accent from the dark
image** (moonlit blue) so both modes share one accent. `app/theme.py` now stores
`theme_bg_light_url` / `theme_bg_dark_url` (an uploaded image still auto-becomes the light
wallpaper; background URLs are validated same-origin only — no remote). The hero's decorative
SVG shows only when no theme is active. `POST /theme` gained `bg_light_url`/`bg_dark_url` form
fields. Verified in-browser (Playwright): preset applies, light/dark wallpapers swap correctly,
text legible over both, 0 console errors. Tests added for preset + remote-URL rejection +
upload-overrides-preset. **160 pass.**

### Addendum — preset as default, app rename (Prinny), editable branding, custom assets

- **Wallpaper preset is now the default look.** `theme.load_theme()` returns the shipped
  "Reading desk" pair when no custom theme is set (`DEFAULT_THEME` baked in Python since
  extraction is client-side); `is_custom` distinguishes default vs user theme. The app is
  always themed; "Reset to default" reverts to this preset. Removed the old plain look + the
  decorative hero SVG.
- **🎨 Customizer moved into Settings → new "Appearance" panel.** Rewritten formless (the
  picker POSTs to `/theme` via `fetch` + reload, so it nests inside the single settings form
  without an illegal nested `<form>`; the file input is unnamed so it never leaks into Save).
  `themeWizard()` + `extractPalette()` moved from `index.html` to `base.html` (global) so they
  work inside the HTMX-swapped settings fragment.
- **Editable copy + rename.** App name defaults to **Prinny** (was "Paper Agent"); the landing
  "Research Workspace" title and subtitle are editable — all three are config keys
  (`app_name`, `workspace_title`, `workspace_subtitle`) saved via `/settings` and surfaced
  through a `pa_branding()` Jinja global used in `base.html` (nav brand + footer) and the hero.
- **Bring-your-own logo & button icons.** `theme._branding_assets()` scans `static/branding/`
  for `logo.*`, `icon-moon.*`, `icon-sun.*`, `icon-agents.*`, `icon-settings.*`
  (`.svg/.png/.webp/.jpg`, cache-busted) and the nav uses them when present, else the default
  emoji/SVG. The Appearance panel documents the exact filenames/location. No upload UI yet (drop
  files in the folder). Verified in-browser; **161 pass.**

## 2026-05-28 — Everything-search, Jump-to redesign, PDF outline default-closed, MCP toggles

- **Fuzzy everything-search.** The landing search is now client-side fuzzy (Fuse.js via CDN)
  over a flat index spanning **papers, notes, thoughts, wiki, and chat** (`GET /search-index`
  → `library.search_index()`; DB for papers/notes/chat, file reads for thoughts/wiki). Results
  group into **type tabs** (All / Papers / Notes / Thoughts / Wiki / Chat) with live counts, in
  the inline dropdown under the search box. Lexical only (no embeddings — CLAUDE.md). The old
  FTS `/search` route + `_search_results.html` were removed (`library.search()` kept for tests).
- **Jump-to-paper redesign.** Fixed-height modal (72vh) with an internal scrollable list; a
  **collection dropdown** defaults to the current collection and scopes the list, while typing
  searches across ALL collections. `jumpPicker()` moved into `base.html` (global) so Alpine
  resolves it the instant the HTMX fragment swaps in (the inline-script-in-fragment race).
- **PDF outline sidebar closed by default.** The PDF.js iframe now loads with `#pagemode=none`,
  overriding documents that embed `/PageMode /UseOutlines`. Still toggleable.
- **Per-agent MCP tool checkboxes (read editable, write locked).** The Agents page renders each
  tool as a checkbox: **read tools** toggle on/off (persisted in `config.toml`
  `agent_tool_overrides`, applied via `agents.effective_tools(key, defaults)` at all six spawn
  sites); **write/mutating tools** (`submit_proposal/debt/brainstorm`) stay checked + disabled —
  they can't be granted or revoked from the UI, preserving the lethal-trifecta boundary. New
  route `POST /agents/{key}/tool`. New dep: Fuse.js (CDN, frontend; user-approved). **163 pass.**

## 2026-05-28 — Context gauge, +Add-tool, layout fixes; usage-metering ruled out

- **Per-chat context gauge.** A slim, clearly-labeled ("est.") bar under each chat header
  showing a ROUGH estimate of conversation fullness: visible transcript chars ÷ 4 vs the model's
  window (1M if the model id mentions "1m", else 200k). Computed client-side (`ctxGauge` in
  `_chat.html`) with a MutationObserver so it updates live as bubbles stream/append. Green/amber/
  rose at 75%/90%. Honest about not seeing the CLI's real caching/compaction/tool-output.
- **+Add-tool for sub-agents.** The Agents Scope card gained a "+ Add tool" button → a picker
  listing the full MCP catalog: READ tools are grantable (those already on show "✓ added", others
  a "+ Add"), WRITE tools shown but **locked** ("🔒 write — locked", no add), each with a `?`
  tooltip; a warning banner explains the trade-off. Override model reworked: per-agent override is
  now the COMPLETE enabled read-tool set (`agents.read_universe()`/`all_mcp_tools()`/reworked
  `effective_tools`/`set_tool_enabled`), so reads can be added beyond code defaults while write
  tools remain code-only (lethal-trifecta boundary intact). New route context `all_tools`.
- **Layout fixes.** (1) Scrollbar now sits at the window's right edge: `<main>` is the full-width
  scroll container with a centered max-width wrapper inside (`main_inner` block; `paper.html`/
  `collection.html` set it to `h-full` to keep their full-height panes). (2) PDF-reader top toolbar
  is now a true 3-section flex so the highlight legend is dead-centered.
- **5h/weekly account usage: ruled out.** Investigated the claude-hud plugin (source of the
  statusline screenshot): it reads the user's Claude OAuth credentials + hits Anthropic's usage
  endpoint, and caches only context/transcript data locally (not the 5h/weekly). The app is barred
  from credential access + remote calls, and there's no local file to piggyback on — so the header
  usage was dropped (user chose "skip"). **164 pass.**

### Still queued
- Move the Agents UI into a Settings category + roomier Settings layout (the modal is fine at wide
  width; "strange" was narrow-viewport wrapping). Deferred — it's a sizable refactor; doing it next.

### Agents folded into Settings

The Agents UI now lives as an **"Agents" category inside Settings** (and the nav 🤖 opens
Settings on that category via `/settings/form?cat=agents`). The standalone `/agents` page still
works. Implementation:
- Extracted the agents body to `templates/_agents_body.html` (tabs use `agentTab`, not `cat`, so
  it nests cleanly inside the Settings form's x-data); `agents.html` is now a thin wrapper that
  includes it, and `_settings_form.html` includes it under an "Agents" sidebar category.
- **Nested-form fix:** the skill-editor modal is a `<form>`, and the Settings panel is wrapped in
  a `<form>` — so the skill-editor modal + its `skillOpen`/`skillTitle` state were hoisted to the
  body level in `base.html` (outside any form, `z-[60]` above the Settings modal). The agents tool
  checkboxes/+Add use unnamed inputs + `fetch`, so nothing leaks into the Settings Save.
- Settings surface widened (modal `w-[min(1000px,95vw)]`; full page `max-w-5xl`) so neither the
  config fields nor the agents content feel cramped. `_settings_ctx()` helper now feeds
  `agents`/`all_tools`/`initial_cat` to all three settings renders.
- Verified in-browser: 🤖 → Settings/Agents; tabs, scope, +Add tool, and the skill editor all work
  in both the modal and the standalone page; skill `<form>` is NOT nested in the Settings form.
  **164 pass, 0 console errors.**

### Settings/Agents polish (Apple-style)

- **Apple-style Settings.** Sidebar restyled: icon chip + label, rounded selection, more breathing
  room (gap-5 → gap-8). The panel is now a grey rounded field holding **white section cards** —
  each sub-setting group is its own card (e.g. Engine and Model are separate cards; the Model field
  no longer floats outside a card, fixing the cramped look).
- **Appearance preset bug fixed.** The Light/Dark labels on the paired-wallpaper preset moved to the
  top of each half (`top-1`) so they no longer overlap the "Reading desk · light + dark" caption.
- **+Add-tool explanation is now a dropdown.** In the picker, each tool's description is hidden
  behind a `?` toggle (click to expand inline) instead of shown directly — keeps rows compact.
- **Consistent section badges.** The Scope and Skills badges now share identical styling
  (`bg-slate-100 text-slate-500`), removing the green/grey + length mismatch.
- **Reset for tools & skills.** Each agent's Scope card shows **Reset tools** (clears the tool
  override → code defaults) and the Skills card shows **Reset skills** (resets all the agent's
  skills to shipped), each shown only when customized and behind a confirm. New routes
  `POST /agents/{key}/tools/reset` and `/agents/{key}/skills/reset`; `agents.reset_tools`/`reset_skills`
  + `tools_customized`/`skills_customized` flags. Verified in-browser; **164 pass, 0 console errors.**

## 2026-05-29 — Initial wiki broadened to the full Phase-5 sections

The starter wiki now drafts **all five Phase-5 sections** from the abstracts — problems,
methods, gaps, benchmarks, and synthesis — rather than just the open-problems map. Still
agent-tagged, abstracts-only, ref-validated, non-destructive. CLAUDE.md amendment broadened
to reflect the wider content scope (one-line edit; same exception boundary).

- `app/skills/starter-wiki/SKILL.md` — rewritten to ask for the five sections + intro +
  reading_path in a strict JSON shape; same editorial principles ("make the reader curious,
  never invent, ground everything in the abstracts").
- `wiki._validate_overview` — now validates the structured shape: problems / methods (title,
  key_idea, body, papers) / gaps (title, body, sources) / benchmarks (same as methods) /
  synthesis (title, body). Hallucinated refs dropped per section; empty sections allowed.
- `wiki.load_overview` — resolves refs in every section to paper objects; falls back gracefully
  for old-schema overviews (missing sections → empty arrays).
- `wiki.generate_overview` — gate loosened: success now means ANY section is non-empty (not
  just problems).
- `templates/_wiki_overview.html` — rewritten with a **tab strip** (Problems / Methods / Gaps /
  Benchmarks / Synthesis) showing only non-empty sections. Methods + Benchmarks share a card
  shape (title / key_idea / body / paper chips); Gaps highlighted with rose hairlines; the
  Problems tab keeps its rich rendering (read-first / approaches / tension / all papers).
- Backward compatibility: existing single-problems overview.json files render unchanged — only
  the Problems tab appears, others stay hidden.
- **164 tests pass; 0 console errors; existing `/c/<slug>/wiki/panel` renders 200 with the new
  template.** Phase 1B (manual refresh curator + popup with propose / debt / triage) and
  Phase 2 (arxiv search, daily auto, LLM-decide banner, PDF deepen) still queued.

### Phase 1B — manual "Update wiki" curator

The wiki page gained a single **✦ Update wiki** primary action that orchestrates the
existing pipelines in one pass, returning a popup-style summary the user reviews — no
new agentic write surface, no contract changes.

- `wiki.run_curator(slug)` — runs `organizer.organize(slug, "incremental")` (proposed wiki
  edits → review queue), `debt.find_debt(slug)` (open questions → reading_debt), and
  snapshots `triage.list_triage(slug)`. Per-pipeline errors are isolated (one failing
  pipeline doesn't abort the others) and surfaced in the popup. Returns
  `{edits:{new,total_pending}, questions:{new,open_total}, triage:{pending_total,items}, errors}`.
- `POST /c/{slug}/wiki/refresh` → calls `run_curator`, returns `_wiki_refresh_popup.html`
  HTMX-swapped into `#refresh-result` on the wiki page.
- `templates/_wiki_refresh_popup.html` — three count cards (new proposed edits / new
  questions / pending triage) linking to the existing surfaces (`/c/<slug>/proposed`,
  `/c/<slug>/debt/panel`, `/c/<slug>/triage`), with collapsible "show new edits / show
  new questions" details. Footnote spells out the contract: edits land in the review
  queue, nothing was written to the wiki directly.
- `templates/wiki.html` — adds the **✦ Update wiki** primary button (HTMX-driven, with
  spinner indicator); demotes Full rebuild / Incremental / Find gaps to secondary.
- Verified: POST returns 200; popup renders all three count cards + the contract
  footnote; **164 pass; 0 console errors.**

### Still queued — Phase 2
- arxiv search → gap-fill triage candidates (the "third source").
- Triage re-rank + flag under-read in-library papers from the wiki gaps.
- Daily scheduled "refresh suggested" banner (opt-in via Settings; cheap check only —
  expensive curator stays user-initiated, honoring the CLAUDE.md no-auto-trigger rule).
- LLM-decide signal (same banner mechanism).
- "Deepen with PDFs" opt-in for richer drafts on a per-collection basis.

### Phase 2A — arxiv gap-fill + under-read flag in the curator

The unified **✦ Update wiki** curator now also calls arxiv (via the existing `discover.find_gaps`)
to file new triage candidates, and flags **under-read papers** in the collection — both surfaced
in the same review popup.

- `wiki.run_curator(slug)` extended with two new steps:
  - **arxiv gap-fill**: `discover.find_gaps(slug)` queries arxiv from the wiki's stated gaps;
    up to 5 results are filed into `triage_items` via `triage.add_from_arxiv()`. Captured in
    `out["arxiv"] = {"added": N, "items": [...]}`. Rate-limit / search errors are caught per-step
    (CLI logged a transient 429 during testing; popup still rendered with `+0 from arXiv`).
  - **under-read flag**: new `wiki._flag_underread(slug)` — deterministic SQL that lists collection
    papers with NO highlights AND no notes content (most-recent-added first). Captured in
    `out["underread"] = {"papers": [...]}`.
- Popup (`_wiki_refresh_popup.html`) gained an inline **"+N from arXiv"** indicator on the triage
  card and an **amber "Go back to these"** panel listing up to 5 under-read papers (each linking
  to the paper view).
- Jinja gotcha hit + fixed: `summary.underread.items` collided with `dict.items()` (Jinja resolves
  attribute before key) → renamed the schema key from `items` to `papers`.
- Verified in-browser: refresh returns 200; popup shows arXiv indicator + under-read list (5 found
  in distill). **164 pass, 0 console errors.**

### Still queued — Phase 2B
- **Daily scheduled "refresh suggested" banner** (opt-in via Settings; cheap signal only — the
  expensive curator stays user-initiated, honoring the no-auto-trigger rule).
- **LLM-decide signal** (same banner mechanism).
- **"Deepen with PDFs"** opt-in for richer drafts on a per-collection basis.

### Phase 2B — refresh-suggested banner + "Deepen with PDFs"

Phase 2B lands two of the three queued items. The third (a literal *daily background scheduler*)
was redesigned into an **on-demand cheap signal** computed at page-load — no background thread,
no opt-in toggle, no token cost, no auto-trigger of expensive ops (still honors CLAUDE.md).

- **`wiki.refresh_signal(slug)`** — deterministic, on-demand check returning
  `{suggest, reasons, since}`. Triggers when the wiki was never refreshed, is older than 7
  days, has new notes/thoughts since last refresh, or has new papers added since.
- **Amber "Refresh suggested" banner** on `wiki.html` — appears when `signal.suggest`; shows
  the reasons inline and a one-click **"✦ Refresh now"** that fires the existing
  `POST /c/<slug>/wiki/refresh` (the unified curator).
- **`wiki._touch_last_regen(slug)`** — bumps `collections.last_wiki_regen` at the end of
  `run_curator`, so the banner clears until the next real change.
- **"Deepen with PDFs"** — `wiki.generate_overview(slug, force, deep=False)` extended with a
  deep mode: digest includes each cached PDF's first ~2000 chars on top of abstracts (capped
  to 30 papers, 60k chars total). New `_pdf_excerpt(paper_id, max_chars)` helper.
  `POST /c/<slug>/wiki/draft` accepts `deep=true`; the overview banner gains a
  **"↻ Deepen (PDFs)"** button alongside "↻ Regenerate" with a tooltip explaining the cost.
  Overview `_meta` tagged with `deep: true|false` for honesty.
- Trade-off note on the third trigger: the user originally asked for an LLM-decide signal.
  This MVP uses a deterministic churn signal (zero LLM cost per page load). An LLM-driven
  enhancement is a clean future add: a periodic cheap LLM call that scores "is it worth
  refreshing now?" — same banner mechanism, swap the heuristic.
- Verified in-browser: cleared `last_wiki_regen` → banner appeared with "Never refreshed.
  · 5 paper(s) added since last refresh." + ✦ Refresh now button. **164 pass, 0 console errors.**

### Phase 3 — wiki page becomes the operations hub

User-driven consolidation: collapsed the four "Build the wiki" buttons into a single ✦ Update
wiki, surfaced open questions + recommended papers inline on the wiki, and dropped the dedicated
Triage and Debt tabs from the collection nav. The wiki page is now the single hub.

- **One button replaces four.** `wiki.html` and `_wiki_panel.html` now show a single ✦ Update
  wiki with an inline tagline ("drafts proposed edits + open questions + paper recs + health
  check, one pass") and a tucked-away **advanced ▾** expander (only Full rebuild remains, as
  the "start clean" escape). Find gaps / Incremental / Health check are gone — folded into the
  curator. The "Build the wiki from your own notes & thoughts" section header is gone too.
- **Health check folded into the curator.** `wiki.run_curator` now also runs `lint_wiki`;
  findings show as a rose-tinted **🩺 Wiki health** card in the refresh popup.
- **Inline panels on the wiki page.** Two side-by-side cards — **Open questions** (with per-row
  *Fill* text-input + *Ignore*) and **Recommended papers** (Accept / Defer / Reject) — wired
  directly to the existing `/c/<slug>/debt/{id}/fill`, `/ignore`, and `/c/<slug>/triage/{id}/{action}`
  endpoints. Quick actions reload via `hx-swap="none" + hx-on::after-request="window.location.reload()"`.
- **Triage + Debt tabs dropped from the collection nav.** `collection.html` now has only
  Papers + Wiki tabs; the Wiki tab carries a combined `triageCount + debtCount` badge so the
  signal isn't lost. Old `localStorage["cc.left"]=triage|debt` is normalized to "wiki" on
  load, and `openLeft("triage"|"debt")` routes to the wiki panel — no breakage for users with
  a stale stored state.
- **Inline `_wiki_panel.html` gets the same treatment** (one ✦ Update wiki button + "Open full
  hub →" link to the standalone page). Dedicated `#wiki-refresh-result` slot so the popup
  doesn't clobber the panel.
- Verified in-browser: `/c/<slug>/wiki` 200 with new layout; collection nav only has Papers +
  Wiki; inline panel shows ✦ Update wiki + Open full hub link; **164 pass, 0 console errors.**

## 2026-05-31 — Compile Tailwind (drop the Play CDN)

**What & why:** Safari rendered the HTMX-swapped wiki panel with white-on-white
buttons, invisible legend dots, and oversized text. Root cause: the Tailwind
**Play CDN** (`cdn.tailwindcss.com`) only generates CSS for classes present in
the DOM at initial load; classes unique to a later HTMX `innerHTML` swap (colors,
sizes, arbitrary values like `text-[9px]`) were never generated — reliably on
Chromium, not on Safari/WebKit. Confirmed via real Safari: `.bg-emerald-600`,
`.w-2`, `.text-[9px]` had no CSS rule while page-load classes did.

**Fix:** compile Tailwind to a static stylesheet (the CDN is dev-only by design).
- `tailwind.config.js` (v3, darkMode:class, typography) scans templates/static/app.
- `static/src/app.css` → `static/app.css` (committed; served via `<link>` in base.html).
- `scripts/build_css.sh` / `make css` fetch the **Tailwind v3.4.17 standalone CLI**
  (no npm/node project) and rebuild. Binary is gitignored (`bin/tailwindcss`, 76MB).
- Pinned v3 (not v4) to match the old CDN exactly — delivery change only, no
  version bump / visual drift.
- Removed the interim color backstop (`static/tw-accent.css`, `scripts/gen_accent_css.py`,
  `tests/test_accent_css.py`) — superseded by the full compiled sheet.

**Workflow change:** run `make css` after editing templates (or `make css-watch`
while developing). Generated `static/app.css` is committed so the app runs without
the build tool; rebuild only when classes change.

**Verified:** real Safari now renders the legend dots (8×8px), `text-[9px]` (9px),
and the emerald button (visible); page loads `/static/app.css`, CDN gone. 161 tests pass.
