---
name: find-evidence-for
description: Find where in the paper a given claim is supported or contradicted, with page-level grounding.
---
When the user asks whether/where the paper supports a claim:

1. Read the paper with `read_paper_text` (page through it), looking for the relevant
   results, definitions, or discussion.
2. Report what the paper actually says about it: SUPPORTS, CONTRADICTS, or DOESN'T
   ADDRESS — quoting or paraphrasing the specific passage and naming the page.
3. Never assert support the text doesn't give. "The paper doesn't address this" is a
   valid, useful answer. Distinguish the paper's evidence from your own reasoning.
