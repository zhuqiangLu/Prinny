# TODO

## PDF viewer — DECIDED (2026-05-24)

- [x] Adopted the **prebuilt PDF.js viewer** (self-hosted, same-origin iframe) as
      the default and only viewer. It gives zoom/search/page-nav for free, and
      persistent app-side highlights are wired into it (overlay layer drawn in PDF
      coords, redrawn on `pagerendered`/`textlayerrendered`/`pagesloaded`, so it
      survives virtualization + zoom).
- [x] Removed the custom canvas viewer: old `static/annotate.js` replaced,
      `templates/paper_beta.html` and the `/beta` route deleted. `paper.html` now
      hosts the prebuilt viewer + the rich Note modal.
- [ ] The vendored dist `static/pdfjs/` (~8.8 MB) is now **required** at runtime.
      Decide whether to git-ignore it (and document a fetch step) or commit it.

## Cleanup — sibling directories

Decide what to do with the reference/companion folders next to `app/`:

- [ ] **`llm_wiki/`** — safe to remove. Reference implementation only; no runtime
      dependency (verified: only doc/comment mentions in `app/wiki.py` +
      `PROGRESS.md`). The conventions/techniques we used are reimplemented in
      `app/wiki.py`. Keep only if you want it as a crib for future work
      (semantic lint, embeddings/RRF retrieval, page-merge prompt tuning).
      If removed, also drop the stale references in `PROGRESS.md` / `README.md`.
- [ ] **`llm-for-zotero/`** — unused by this project (separate Zotero plugin,
      TypeScript). Not referenced anywhere in `app/`. Remove if you don't use the
      plugin itself.
- [ ] **`zotero-arxiv-daily-local/`** — **KEEP.** This is the upstream feeder for
      triage (Phase 7): it deposits candidate papers into a Zotero inbox
      collection/tag that `triage.py:scan_inbox` reads. Removing it breaks the
      arxiv-daily → triage flow.
