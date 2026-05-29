---
name: compare-to-my-notes
description: Compare the paper to the USER's own notes and highlights — surface agreements, tensions, and gaps.
---
When the user wants to relate the paper to their own thinking:

1. Call `get_paper_context` to load the user's current notes (summary, their thoughts,
   key quotes) and highlights for this paper.
2. Read the relevant parts of the paper with `read_paper_text` to ground the comparison.
3. Surface: where the paper SUPPORTS the user's take, where it's in TENSION with it, and
   what the user noted that the paper doesn't actually address.
4. Be honest and specific; cite the page or the user's note. Do NOT modify their notes —
   you can only read. If they have no notes yet, say so and offer to discuss the paper.
