# Prinny — your paper reading sidekick 
<img width="2170" height="725" alt="image" src="https://github.com/user-attachments/assets/2eac12c2-99df-4ad4-ba64-236d14559f06" />

**Turn papers, highlights, notes, and conversations into a living mental model of your research field.**

> **Philosophical anchor:** the LLM is an editor and research assistant, not the author.
> It never silently rewrites your wiki — every agent-proposed change is something **you
> review and accept**. You do the reading; the tool helps you organize what you wrote and
> surfaces gaps.

**TODO**  

 - [ ] link experiment to coding agent


## Requirements

- **Python 3.11+**
- **Claude Code CLI** (or Codex CLI), installed and authenticated — this is the LLM
  backend. Without it the app still runs and you can browse/read/annotate, but chat,
  wiki drafting, suggested reading, and benchmark extraction are disabled.



## Install & run

```bash
git clone <your-repo-url> prinny && cd prinny

# Option A — pipx (isolated):
pipx install --editable .
prinny                 # starts the app + opens your browser

# Option B — a venv:
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/prinny       # or: .venv/bin/uvicorn app.main:app
```

> it is recommended to use Prinny with **Chrome**, safari still have some rendering issue. 


## Preview
<img width="1812" height="909" alt="image" src="https://github.com/user-attachments/assets/e09476ad-37c6-4c61-b332-5ac86966bdc2" />




## Zotero setup (Optional, for importing from Zotero)

<img width="789" height="289" alt="image" src="https://github.com/user-attachments/assets/37d1ddea-6a00-4778-817d-c403b3c08614" />


In **Zotero Desktop → Settings → Advanced → General**, enable **"Allow other
applications on this computer to communicate with Zotero"** (starts the local HTTP
server on port 23119). Verify:

```bash
curl -s http://127.0.0.1:23119/api/users/0/collections | head -c 200
```

A JSON array means it's working. The app prefers this HTTP API and falls back to reading
`~/Zotero/zotero.sqlite` directly (read-only). The HTTP API is also required for triage
write-back (moving/tagging items in Zotero).




<details>

<summary>Storage layout</summary>


```
~/.prinny/
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


</details>



<details>

<summary>Tech notes</summary>


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


</details>



