# Prinny — a personal research-wiki agent over Zotero

Prinny maintains a **wiki per Zotero collection** where the wiki reflects **your own
thinking** (notes, highlights, beliefs, conversations), with papers as evidence.

> **Philosophical anchor:** the LLM is an editor and research assistant, not the author.
> It never silently rewrites your wiki — every agent-proposed change is something **you
> review and accept**. You do the reading; the tool helps you organize what you wrote and
> surfaces gaps.

Everything is **local**: data lives in `~/.paper-agent/` as plain Markdown + SQLite. The
LLM runs through your **local Claude Code (or Codex) CLI** — there is no API key and no
hosted backend. The only outbound network calls are to your local Zotero and (on request)
arXiv.

---

## Requirements

- **Python 3.11+**
- **Zotero Desktop** — the source of truth for papers and PDFs (a hard dependency).
- **Claude Code CLI** (or Codex CLI), installed and authenticated — this is the LLM
  backend. Without it the app still runs and you can browse/read/annotate, but chat,
  wiki drafting, suggested reading, and benchmark extraction are disabled.

---

## Install & run

The supported install is an **editable install from a clone** (templates, compiled CSS,
vendored front-end libs, and PDF.js all live in the source tree and are loaded from
there — no build step, no Node, no CDN at runtime).

```bash
git clone <your-repo-url> prinny && cd prinny

# Option A — pipx (isolated):
pipx install --editable .
paper-agent                 # starts the app + opens your browser

# Option B — a venv:
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/paper-agent       # or: .venv/bin/uvicorn app.main:app
```

`paper-agent` runs a quick preflight (checks the LLM CLI is on PATH and Zotero is
reachable), then serves `http://127.0.0.1:8000` and opens it. Flags: `--port`,
`--host`, `--no-open`, `--reload`.

On first run the app creates `~/.paper-agent/` (`config.toml`, `app.sqlite`,
`collections/`). Pick your engine (claude-code / codex) and model on the **Settings**
page if the defaults aren't right.

### Required setup — Zotero local API

In **Zotero Desktop → Settings → Advanced → General**, enable **"Allow other
applications on this computer to communicate with Zotero"** (starts the local HTTP
server on port 23119). Verify:

```bash
curl -s http://127.0.0.1:23119/api/users/0/collections | head -c 200
```

A JSON array means it's working. The app prefers this HTTP API and falls back to reading
`~/Zotero/zotero.sqlite` directly (read-only). The HTTP API is also required for triage
write-back (moving/tagging items in Zotero).

---

## What it does

1. **Point it at a Zotero collection.** It reads the papers + PDFs (read-only) and
   serves them with an embedded PDF.js viewer.
2. **You read.** Highlight PDFs, write per-paper notes, jot thoughts, and chat with an
   agent grounded in *your* notes, the open paper, and the collection.
3. **Draft the Field Model** (one agent pass): a one-paragraph **thesis** + a four-column
   **research landscape** (problems / methods / debates / open questions) + a **concept**
   space. Agent-written, agent-tagged, regenerable.
4. **Build understanding over time** — the wiki is a single page of stage-gated sections:
   - **Thesis** and **Landscape** (the Field Model).
   - **Concepts** — a deterministic, no-LLM attention scorer ("Your Current Focus")
     ranks them by your highlights/notes. You can add/edit/remove concepts; your edits
     survive a regenerate.
   - **Your Understanding (beliefs)** — single-sentence claims you hold. The agent drafts
     candidates into a tray; **you accept** the ones that match your thinking.
   - **Benchmarks** — a method × benchmark table, extracted per-paper by an agent that
     reads each PDF's results tables. Each number cites its paper. *Agent-extracted —
     verify before trusting.*
   - **Connections & themes** — a structural (embedding-free) knowledge graph; card view
     or graph view.
   - **Papers** — the live evidence list with attention chips.
5. **The chat can propose wiki edits** (propose-and-gate): the agentic side-chat may
   propose typed edits (or you run `/updatewiki`); each lands as an inline **Accept /
   Dismiss** card. Accepting is the only path that writes the wiki.
6. **Suggested reading** — find external arXiv papers (related work or a custom search;
   `🔬 Deep` runs a tool-using finder that learns from your accept/reject history). Each
   candidate is validated against its abstract before it's shown; accept imports it.
7. **Research topics** — cross-collection investigations (question → assumptions →
   hypotheses → evidence → unknowns → experiments) seeded from your collections.
8. **Triage / gaps / stale** — curate an inbox of candidate papers, find gap-fillers,
   flag papers you've never engaged with (never auto-removed).

A **notification bell** in the sidebar surfaces background jobs finishing, so you can keep
working while a search or extraction runs.

---

## Chat commands

Type these in the collection's side chat:

- `/help` — list commands.
- `/thought <text>` — save a note to your thought stream.
- `/find [focus]` — find external papers (Suggested reading).
- `/gaps` — find papers that fill the wiki's open questions.
- `/belief <claim>` — propose a belief (you Accept/Dismiss).
- `/updatewiki [instruction]` — ask the agent to propose wiki edits now.
- `/<collection-slug> <question>` — ask about a *different* collection (read-only).

---

## Storage layout

```
~/.paper-agent/
├── config.toml                 # engine, model, Zotero paths
├── app.sqlite                  # threads/messages, notes, triage, annotations, topics, FTS
└── collections/<slug>/
    ├── purpose.md               # optional collection mission
    ├── thoughts/                # timestamped thought stream
    ├── notes/<zotero-key>.md     # mirror of structured notes (editable in Obsidian)
    └── wiki/sections/            # the cognitive-model wiki
        ├── thesis.md  landscape.md (+ landscape.json)  concepts.json
        ├── benchmarks.json
        └── beliefs/  (+ _candidates/ tray)
```

PDFs are **never copied** — they're streamed from Zotero's storage directory.

---

## Tech notes

- **Backend:** Python / FastAPI / Jinja2 + HTMX + Alpine.js. SQLite (stdlib). No ORM.
- **Frontend:** server-rendered HTML; Tailwind **compiled** to `static/app.css`
  (`make css`); HTMX/Alpine/cytoscape/KaTeX/Fuse and PDF.js are **vendored** under
  `static/vendor/` and `static/pdfjs/` (no CDN at runtime).
- **LLM:** the Claude Code / Codex CLI, driven as a subprocess via `engine.py` behind
  `llm.py`. No API key, no hosted backend.
- **No vector store / embeddings** — retrieval is SQLite FTS5; the knowledge graph is
  purely structural.
- **Tests:** `pytest` (`make test`). LLM calls are stubbed; the live agent-spawn paths
  are exercised manually.

---

## Module map (`app/`)

| File | Responsibility |
|---|---|
| `config.py` | `~/.paper-agent/` layout, `config.toml` load/save |
| `db.py` | `app.sqlite` schema + migrations, FTS, `connect()` |
| `zotero.py` | Zotero adapter (`LocalZotero` + `WebZotero` stub) — only Zotero access |
| `library.py` / `repo.py` | local paper store + chat threads/messages |
| `engine.py` / `llm.py` | CLI-agent subprocess seam + thin `complete`/`stream` interface |
| `mcp_server.py` | read-only MCP tools the agents use (search, read PDF, arXiv, propose) |
| `context.py` | chat context assembly |
| `notes.py` / `thoughts.py` / `annotations.py` | per-paper notes, thought stream, PDF highlights |
| `wiki.py` | the cognitive-model wiki (Field Model, concepts, beliefs, benchmarks, graph) |
| `wiki_propose.py` | chat→wiki propose-and-gate engine |
| `agentic_chat.py` / `paper_chat.py` | tool-using collection + paper chat agents |
| `paper_finder.py` / `benchmark_agent.py` | deep-search finder + per-paper benchmark extractor |
| `discover.py` | arXiv search (API + website fallback), gap/stale detection |
| `topics.py` / `topic_view.py` | research topics (cross-collection investigations) |
| `notify.py` | background-job notification feed (sidebar bell) |
| `cli.py` | `paper-agent` console entrypoint + preflight |
| `main.py` | all FastAPI routes |

---

## Single user, local only

No accounts, no auth, no multi-user, no telemetry. It complements Zotero — it does not
replace it, and it does not read papers *for* you. By design, the LLM won't author wiki
content you didn't seed.
