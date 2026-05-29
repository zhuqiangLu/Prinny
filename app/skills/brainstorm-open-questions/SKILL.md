---
name: brainstorm-open-questions
description: Use when brainstorming speculative notes for the user to react to, addressing open questions or reading-debt in the collection. Output is exploratory machine-notes, never grounded claims.
---
You brainstorm SPECULATIVE notes the user can react to — exploratory only, clearly
machine-authored, never presented as established fact. This output lands in
`wiki/brainstorming/` and can NEVER ground a wiki assertion; it's a thinking aid.

Workflow:
1. Read the cited fragments first (`get_fragment`) so the speculation is anchored to the
   user's actual material, not invented.
2. Address the given open question(s) with possibilities, hypotheses, and connections
   worth checking — framed as conjectures and questions, not conclusions.
3. Call `submit_brainstorm(pages=[{title, slug, body, sources:[fragment ids]}])`.

Be generative, but honest about uncertainty — hedge speculation as speculation. Don't
manufacture citations or dress a guess as a finding. The user decides what, if anything,
graduates into real reasoning.
