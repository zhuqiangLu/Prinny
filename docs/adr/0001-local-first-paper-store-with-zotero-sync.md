# ADR 0001 — Local-first paper store with Zotero sync

- **Status:** Implemented (2026-05-25)
- **Date:** 2026-05-25
- **Supersedes:** the "Zotero is the live source of truth" model in `CLAUDE.md`
- **Decision owner:** user (ML researcher); grilled and agreed in design session

## Context

The original contract (`CLAUDE.md`) makes Zotero the **live source of truth**: the app
reads papers/collections/PDFs from Zotero on every request (SQLite when closed, HTTP
API when open), never copies PDFs, and never writes back to Zotero in v1.

The user wants two things this model can't give cleanly:

- **(b) Write-back** — highlights/notes/new-collections created in the app should land
  back in Zotero so Zotero stays the canonical library.
- **(d) Curation** — assemble/reorganize collections freely in the app (add/remove/
  regroup papers, accept arxiv suggestions) *without* mutating Zotero until ready,
  then push the result.

Decoupling/availability (a) and performance (c) were explicitly **not** the drivers.

## Decision

Invert the architecture: the app maintains its **own local store** (paper metadata +
a complete PDF store), populated by **import from Zotero** and by **in-app discovery**.
Work happens locally; an explicit, manual **Sync to Zotero** pushes work back.

### 1. Ownership boundary

| Data | Owner | Direction |
|---|---|---|
| Paper identity, core metadata, canonical PDF of record | **Zotero** | import → app (metadata read-only in app) |
| Working PDF copy (app's own store) | **App** | — |
| Collection membership / regrouping / new collections | **App** | app → Zotero (additive by default) |
| Highlights, notes | **App** | app → Zotero (later phase) |
| Wiki, thoughts, chat | **App only** | never sync |

The app never re-authors paper metadata → no metadata-merge conflicts. The app *may*
mint new papers.

### 2. Paper identity & lifecycle

- **Local primary key = app-owned id.** Nullable natural keys: `arxiv_id`, `zotero_key`.
  Dedupe on `arxiv_id`/DOI; fill `zotero_key` only after sync.
- `origin` ∈ {`zotero-import`, `arxiv-suggested`, `app-created`}.
- `sync_status` ∈ {`local-only`, `synced`, `dirty`}.
- Lifecycle: `suggested (arxiv_id) → accepted into app collection (local-only) →
  Sync → synced (has zotero_key)`.

### 3. PDF store (local-first)

- **One uniform store**: a configurable directory path (may be a NAS/cloud mount that
  looks local). Every paper → `<store>/<app-id>.pdf`, regardless of origin. `/pdf/<id>`
  resolves there — no origin branching.
- **Copy timing chosen by the user at import**: **eager** (copy all now — default) or
  **lazy** (copy on first open); remembered as a preference; a "download all now"
  action upgrades a lazy collection.
- **Never auto-delete** from the store. Treat the store as **possibly-absent**
  (netdrive disconnects) and fail gracefully.
- **Accepted cost:** this duplicates Zotero's PDFs for imported papers (two copies).
  Bought for uniform highlight fidelity + offline reads.

### 4. Import / Refresh (re-runnable, per-collection)

- User **activates** a collection; only activated collections are imported/tracked.
- **Refresh = merge with provenance flags, never destructive:**
  - Metadata → **Zotero wins**, silent update (app never edits metadata).
  - New-in-Zotero items → added locally, flagged "new since last refresh."
  - Removed-in-Zotero / deleted → flagged, never auto-removed.
  - Unsynced local curation (`local-only`/`dirty`) → always preserved.

### 5. Discovery (extend `discover.py`; do **not** fork arxiv-daily)

- `discover.py` advises **related papers per collection**, grounded in that collection's
  wiki/purpose, via the arxiv API (no web search).
- Accepted suggestions land **in the app collection** as `arxiv-suggested` papers
  (`local-only`), PDF pulled into the store. They reach Zotero only via Sync.

### 6. Sync to Zotero (explicit, manual)

- Transport: Zotero **local HTTP API (:23119), Zotero running, write-enabled key**.
  Never automatic.
- **Additive by default:**
  - New app-native papers → create Zotero item + **upload the PDF** + add to collection
    → write back `zotero_key`, mark `synced`.
  - New collections → create in Zotero if missing.
- **Destructive curation (removals/regrouping) = explicit, reviewed opt-in** — shown as
  a list before it runs; nothing removed from Zotero silently.

## Consequences

### Deviations from `CLAUDE.md` (conscious)

1. "Zotero is the source of truth / live dependency" → app is the working store; Zotero
   is canonical-of-record + sync target.
2. "PDFs are not copied" → abandoned; app keeps its own complete PDF store.
3. "No write-back to Zotero in v1" → write-back is now a core feature (additive +
   reviewed removals).
4. "App must not work without Zotero" → app can now read/highlight with Zotero closed
   (Zotero needed only at Sync time).
5. `zotero-arxiv-daily-local` is **not** modified (we extend `discover.py`) — no deviation.

### Data-model & migration impact (not yet designed)

- New `papers` table (local store) + PDF-store path config + `activated_collections`.
- Existing tables keyed on `zotero_key` (`paper_notes`, `annotations`, `chat_threads`,
  `triage_items`) must re-key to the **app-id** (with `zotero_key` secondary). Real
  migration of live data.
- `app/zotero.py` "read live" usage moves behind an **import/sync service**; most routes
  read the local store instead of Zotero.

### Effect on the in-flight UI task

"**Start a new collection**" now fits cleanly: create a collection **locally**
(name + purpose + summary), curate into it, Sync to Zotero when ready. The per-collection
**summary** and the **landing-page/card redesign** sit on top of the local store.

## Open questions (next design sessions)

1. **Migration strategy & phasing** — big-bang vs. incremental; re-keying existing
   notes/highlights without loss.
2. **Highlight/note write-back format** — Zotero annotations vs. child notes.
3. **Build order** — proposed: (1) `papers` table + import + uniform PDF store + re-key
   existing data → (2) local-first reads across the app → (3) discovery-into-app →
   (4) Sync engine → (5) landing/cards/summaries on top.
