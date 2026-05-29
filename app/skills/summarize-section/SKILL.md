---
name: summarize-section
description: Summarize a specific section (or the whole) of the open paper, faithfully and grounded in the actual text.
---
When the user asks for a summary of a section (or the paper):

1. Read the relevant pages with `read_paper_text` (it returns total_pages; page through
   as needed). Use the `Read` tool only if a figure/table matters.
2. Summarize ONLY what the text says — no outside knowledge, no embellishment. Preserve
   the authors' claims and hedges; don't upgrade "suggests" into "proves".
3. Keep it tight: a few sentences or bullets. If the user named a section, stay within it.
4. If something is ambiguous or you couldn't find it, say so rather than guessing.
