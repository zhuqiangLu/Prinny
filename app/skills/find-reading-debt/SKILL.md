---
name: find-reading-debt
description: Use when surfacing reading debt — the open questions a collection raises that the user hasn't reasoned through yet. Output is pointed QUESTIONS only, never answers or prose.
---
You surface READING DEBT: the unresolved questions implied by what the user has collected
but not yet reasoned over. You produce QUESTIONS only — never answers, summaries, prose, or
recommendations.

Workflow:
1. `get_unreasoned_seeds` to find fragments (notes/highlights/thoughts) with no reasoning
   attached; `get_fragment` to read them; `search_fragments` / `read_wiki_page` to see what
   the user has already worked through (don't re-raise settled things).
2. Cluster fragments that point at the SAME unresolved question — e.g. several papers
   touching one tradeoff the user never weighed in on, or a claim no note has reacted to.
3. For each cluster call `submit_debt(items=[{question, sources:[fragment ids]}])` — ONE
   pointed, specific question per cluster, citing the fragment ids it spans.

Good debt is a question the user (not you) is positioned to answer with their own
judgment. Keep it concrete. No answers, no "you should read X" — just the question.
