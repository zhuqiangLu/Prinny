# Paper Collection Wiki Agent

A personal research tool that maintains a **wiki per Zotero collection** — where the
wiki reflects **your own thinking** (notes, thoughts, highlights, conversations),
with papers as evidence.

> **Philosophical anchor:** the LLM is an editor and research assistant, not the
> author. It never produces wiki content unless you wrote something first, and
> every LLM-proposed change is a **diff you review**, never a silent mutation.

---

## What it's for

**Purpose.** Help a single researcher *externalize their own understanding* of a body
of papers. The papers are evidence; the wiki is your synthesis. The tool lowers the
friction of capturing notes, thoughts, and highlights, then helps you turn **what you
wrote** into a structured, cited wiki — it does not read the papers for you.

**How it works (the loop).**

1. Point it at a **Zotero collection**; it reads the papers and PDFs (read-only).
2. You **read**: highlight PDFs, write per-paper notes, jot timestamped thoughts, and
   chat with an assistant grounded in *your* notes + the open paper.
3. On request, a two-step LLM pass (analyze → generate) turns **your notes + thoughts**
   into proposed wiki edits — `problems / methods / gaps / benchmarks / synthesis` —
   each as a **diff with per-claim provenance**.
4. You **review and accept**. Accepting is the *only* code path that writes the wiki;
   claims that don't cite a real note/thought/highlight/paper are filtered out in code.
5. Optionally: triage candidate papers into the collection, search arXiv for gap-fillers,
   and export the collection to BibTeX.

Everything is local: data lives in `~/.paper-agent/` as plain Markdown + SQLite (editable
in Obsidian or any editor). The only outbound calls are to OpenAI and arXiv.

**Good for**

- Researchers who keep collections in Zotero and want a **personal wiki that reflects
  their own thinking**, with papers as grounding.
- Active reading: structured per-paper notes, PDF highlights, a thought stream, and
  collection-scoped chat that's aware of the open paper and your notes.
- Producing a **traceable** literature synthesis — every wiki claim links back to
  something you wrote or a paper you cited.
- A **single local user** who wants their data in plain files and minimal external calls.
- Curating an inbox of candidate papers (e.g. from `zotero-arxiv-daily`) and exporting
  to `.bib`.

**Not good for**

- Having the LLM **read and summarize papers for you** — by design it won't author wiki
  content you didn't seed. You still do the reading.
- Working **without Zotero** — Zotero is a hard dependency (source of truth for papers
  and PDFs).
- **Teams / multi-user / sharing / auth** — it's single-user and local only.
- **Semantic search over a huge corpus** — retrieval is SQLite FTS5 keyword search; there
  is no vector store or embeddings (a deliberate v1 choice).
- **Hands-off automation** — expensive operations (full wiki rebuild, gap-finding) are
  on-demand and human-reviewed; there is no background scheduler.
- Replacing Zotero as your reference manager — it **complements** Zotero, not replaces it.

---

- **Backend:** Python 3.11+, FastAPI, SQLite (no ORM)
- **Frontend:** server-rendered HTML + HTMX + Alpine.js + Tailwind (CDN, no build step)
- **PDF:** PDF.js (CDN) with a custom annotation overlay
- **LLM:** OpenAI SDK behind a thin interface
- **Search:** SQLite FTS5 (no vector store)
- **Zotero:** read via local SQLite (read-only, `immutable=1`) or the Local HTTP API when enabled

---

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn app.main:app --reload   # http://127.0.0.1:8000
.venv/bin/pytest -q                        # tests
```

On first run the app creates `~/.paper-agent/` (config, `app.sqlite`, `collections/`).
Add your OpenAI key on the **Settings** page to enable any LLM feature; everything
LLM-dependent degrades gracefully without one.

### Required setup — Zotero local API

In **Zotero Desktop**:

1. Open **Settings / Preferences**
2. Go to **Advanced → General**
3. Enable **"Allow other applications on this computer to communicate with Zotero"**

This starts the local HTTP server on port **23119**. Verify it's up:

```bash
curl -s --noproxy '*' http://127.0.0.1:23119/api/users/0/collections | head -c 200
```

A JSON array means it's working (a `403 "Local API is not enabled"` means the
setting is still off).

### Zotero connection notes

- The app prefers the **Local HTTP API** (port 23119) and falls back to reading
  `~/Zotero/zotero.sqlite` directly (read-only, `immutable=1` — safe while Zotero runs).
  Detection is automatic at startup; no config change is needed when you toggle the API.
- The HTTP API is also required for **triage write-back** (moving/tagging items in Zotero).
- Local Zotero calls bypass any `http_proxy`/`https_proxy`; OpenAI calls use them.

---

## Storage layout

```
~/.paper-agent/
├── config.toml                 # key, model, Zotero paths
├── app.sqlite                  # threads, messages, notes, triage, annotations, sync, FTS
└── collections/<slug>/
    ├── purpose.md  schema.md    # mission + structure (optional, rarely change)
    ├── thoughts/  thoughts-archive/
    ├── notes/<zotero-key>.md    # mirror of structured notes (editable in Obsidian)
    ├── wiki/{problems,methods,gaps,benchmarks,synthesis}/  index.md  log.md
    └── proposed-edits/*.json    # pending LLM diffs awaiting review
```

PDFs are **never copied** — streamed from Zotero's storage dir.

---

## Module map (`app/`)

| File | Responsibility |
|---|---|
| `config.py` | `~/.paper-agent/` layout, `config.toml` load/save |
| `db.py` | `app.sqlite` schema + init, FTS triggers, `connect()` |
| `zotero.py` | Zotero adapter (abstract `ZoteroBackend` + `LocalZotero` + `WebZotero` stub) — only Zotero access |
| `slugs.py` | `slugify()` name→slug |
| `repo.py` | app.sqlite helpers: collection mapping + chat threads/messages |
| `llm.py` | thin OpenAI wrapper (`complete`/`stream`), token+latency logging |
| `markdown.py` | markdown + `[[wikilink]]` resolution |
| `pdf_text.py` | pypdf text extraction for chat grounding |
| `frontmatter.py` | hand-rolled YAML-frontmatter parse/dump (no PyYAML) |
| `context.py` | chat context assembly |
| `notes.py` | per-paper structured notes, DB↔markdown sync |
| `thoughts.py` | timestamped thought stream + consolidation |
| `wiki.py` | wiki generation pipeline, guardrail, review queue |
| `suggest.py` | chat→wiki classifier |
| `triage.py` | candidate-paper inbox triage |
| `discover.py` | arXiv gap detection + stale-paper flagging |
| `annotations.py` | app-authored PDF annotations + Zotero read-in |
| `main.py` | all FastAPI routes |
| `static/annotate.js` | persistent highlights on the prebuilt PDF.js viewer (overlay + selection toolbar + manager) |
| `static/pdfjs/` | vendored PDF.js prebuilt viewer (self-hosted, same-origin) — runtime dependency |

---

## Features → file → function

### Browsing, papers, PDF (Phases 0–1)
| Feature | File:function |
|---|---|
| List collections (HTTP→SQLite fallback) | `zotero.py:LocalZotero.list_collections`, `http_available` |
| Slug ↔ collection persistence | `repo.py:resolve_collection` → `sync_state` |
| Paper list / metadata | `zotero.py:list_papers`, `get_paper`, `paper_full` |
| PDF resolution + streaming | `zotero.py:pdf_path`, `main.py:pdf_stream` |
| Settings | `config.py:save_config`, `main.py:settings_post` |

### Chat — collection-scoped, paper-aware, read-only re: artifacts (Phase 2)
| Feature | File:function |
|---|---|
| LLM call | `llm.py:complete` / `stream` |
| One thread per collection | `repo.py:get_or_create_thread`, `add_message`, `get_messages` |
| Context assembly | `context.py:build_messages`, `system_prompt`, `paper_block` |
| Chat endpoint + render | `main.py:chat_post`, `markdown.py:render` |

### Per-paper notes (Phase 3) — `notes.py`
| Feature | Function |
|---|---|
| Save to DB + mirror `.md` | `save_note` |
| Two-way sync (file mtime tiebreak) | `get_note` |
| Draft from chat (never auto-saves) | `main.py:notes_draft` |

### Thoughts stream (Phase 4) — `thoughts.py`
`create_thought` · `update_thought` · `delete_thought` · `supersede_thought` (→archive) · `propose_consolidation` (LLM) · `accept_consolidation`.

### Wiki generation — the careful part (Phase 5) — `wiki.py`
| Step | Function |
|---|---|
| Gather inputs + valid provenance set | `gather_inputs` |
| Two-step analyze → generate | `analyze`, `generate` |
| **Guardrail: drop claims lacking a real note/thought (in code)** | `_filter_claims` |
| Build page + frontmatter | `_build_page` |
| Write proposals (never applied) | `run_generation` → `proposed-edits/*.json` |
| Review queue | `main.py:proposed_get` |
| **Accept = the only path that writes `wiki/`** | `accept_proposed` → `rebuild_index`, `_append_log` |

### Wiki edits from chat (Phase 6)
`suggest.py:classify` flags a turn → `wiki.py:proposal_from_chat` (guardrail: turn must cite a note/thought/paper from its `context_refs`).

### Triage (Phase 7) — `triage.py`
`scan_inbox` (cheap, no LLM) · `generate_pitch` (on-demand LLM) · `accept`/`reject`/`defer` (Zotero write-back stubbed via `ZoteroWriteError`) · `add_from_arxiv`. Inbox configured in `purpose.md` frontmatter (`inbox_collection` / `inbox_tag`).

### Gap detection + stale papers (Phase 8) — `discover.py`
`find_gaps` (LLM query → `_arxiv_search` → LLM picks gap-fillers) · `find_stale` (`_appearance_count`, 90-day cutoff, **never removes**).

### PDF annotations (Phase 1.5) — `annotations.py` + `static/annotate.js`
| Feature | Where |
|---|---|
| App CRUD (Zotero-origin is read-only) | `annotations.py:create/update/delete/list_all` |
| Zotero read-in (one-way) | `zotero.py:read_annotations` |
| Prebuilt PDF.js viewer (zoom/search/page-nav) + persistent highlight overlay + `[Highlight] [Ask] [Note]` selection toolbar | `static/annotate.js`, `templates/paper.html` |
| Highlight manager: color filter + multi-select batch recolor/delete, plus per-row jump/note/recolor/delete | `static/annotate.js` (`wireManager`) |
| Endpoints | `main.py:annotations_create/list/update/delete` |

**Annotation authority = the app.** We author into our own store and read Zotero's
annotations out one-way; we do **not** write back to Zotero in v1. Positions mirror
Zotero's `{pageIndex, rects}` PDF-point shape and `# WRITEBACK-TODO` seams are marked
so write-back can be added later without a data-model change.

---

## Overall pipeline

```
ZOTERO (source of truth, read-only)
  ├─ zotero.sqlite (immutable, proxy-bypassed)
  └─ itemAnnotations / Local HTTP API
                    │
                    ▼
            zotero.py adapter ──────────────────────────────────────────┐
                    │                                                     │
   ┌────────────────┼─────────────────────────────┐                      │
   ▼                ▼                               ▼                      ▼
 BROWSE          CAPTURE (low friction)         ORGANIZE              DISCOVER
 papers/PDF      • notes      (notes.py)         thoughts (thoughts.py)  • gaps (discover.py→arXiv)
 (main.py,       • highlights (annotations.py)   wiki    (wiki.py)        • stale (discover.py)
  paper.html)    • chat       (context+llm)                              • triage (triage.py)
                    │                               │
                    └──────────────┬────────────────┘
                                   ▼
                       WIKI GENERATION  (wiki.py, two-step)
                       analyze ─► generate ─► _filter_claims (guardrail)
                                   │
                                   ▼
                       proposed-edits/*.json   (nothing applied yet)
                                   │
                          user reviews diff + provenance  (main.py:proposed_get)
                                   │  accept / edit / reject
                                   ▼
                       accept_proposed()  ── the ONLY writer into wiki/
                                   │
                                   ▼
                       wiki/<section>/*.md + index.md + log.md
```

**Invariant enforced everywhere:** the only code path that writes a wiki page is a
user accepting a proposed edit. Every wiki claim must cite a real note, thought,
highlight, or paper — unsupported claims are filtered out in code before you ever
see the diff.

---

## External capture (Phase 4.5 — planned, not yet built)

For "shower thoughts" away from the desk, the app will **harvest** a watched
location rather than be the capture tool. Suggested zero-build setups:

1. **Apple Notes / Obsidian synced folder** — a single `inbox.md` you append lines to from your phone.
2. **Dropbox/iCloud folder** — drop a `.txt` per thought; the watcher ingests new files.
3. **Telegram/email → file bridge** (e.g. a Shortcut or IFTTT) writing to that folder.

Each captured line becomes an unassigned thought; an `/inbox` triage step proposes a
collection/paper for you to confirm.

---

## Testing & status

- `pytest` — 50 tests: Zotero adapter, notes sync, frontmatter, chat context, the
  wiki diff-proposal **guardrail** (mocked LLM), annotations (app CRUD + Zotero read-in),
  local-first store (clean-reset, non-destructive refresh), custom tags, and BibTeX export.
- Built & checkpointed: Phases 0–8 + addendum Phase 1.5. See **PROGRESS.md** for
  per-phase detail and documented deviations.

### Known limitations
- Zotero **write-back** (triage accept/reject moving/tagging items) is stubbed —
  requires enabling the Local API and implementing the write protocol.
- Highlight→chat gesture and silent chat auto-attach (**Phase 2.5**) and external
  inbox ingestion (**Phase 4.5**) are not yet built.
- Triage scan, gap-finding, and stale detection are **on-demand** (no background
  scheduler) — by design, to avoid auto-triggering expensive operations.
```
