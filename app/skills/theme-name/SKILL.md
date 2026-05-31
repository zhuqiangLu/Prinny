---
name: theme-name
description: Name and one-sentence-describe the structural THEMES of a collection — clusters of co-occurring concepts/problems/methods/beliefs that the knowledge graph found by shared papers. The cluster membership is computed deterministically; your job is only to label it, not to invent the grouping.
---
You are labelling the **themes** of a researcher's paper collection. A theme is
a cluster of ideas (concepts, problems, methods, beliefs) that the knowledge
graph grouped together **because they share papers** — the grouping is already
computed for you. You are NOT deciding what belongs together; you are giving an
already-formed cluster a short, honest name and a one-sentence description.

## Hard rules

- **Name only what's in the cluster.** The name and description must summarize
  the entities listed for that cluster — nothing broader, nothing invented. If
  the cluster is three KV-cache ideas, don't name it "Efficient LLMs."
- **Name = 2-6 words.** A noun phrase a researcher would skim. Title Case.
  e.g. "Context & Memory Constraints", "Real-time Video Processing".
- **Description = ONE sentence, ≤ 25 words.** Describe what ties these specific
  ideas together. Neutral, descriptive — not a claim, not a sales pitch.
  e.g. "How long-context limits, memory banks, and persistent storage shape
  video understanding."
- **Explanation = 2-4 sentences.** A short paragraph shown when the reader
  expands the theme: what these ideas have in common and how they relate via the
  shared papers. Grounded in the listed ideas + binding papers; describe the
  grouping, don't editorialize or rank.
- **No direction, no causality.** The ideas co-occur in papers; they are not a
  pipeline. Don't write "X leads to Y" or "A is solved by B."
- **Distinct names.** Each theme gets a different name. Don't reuse a word as
  the head noun of two themes.
- **Respect the ref.** Echo back each cluster's `ref` integer exactly so we can
  match your label to the right cluster.

## Output: STRICT JSON only (no prose, no code fence)

```json
{
  "themes": [
    {"ref": 0, "name": "Context & Memory Constraints",
     "description": "How long-context limits, memory banks, and persistent storage shape video understanding.",
     "explanation": "These ideas all turn on the cost of holding long video context in memory. The binding papers explore memory banks and persistent stores as ways around fixed context windows, which is why context-length limits, memory banks, and streaming inference keep appearing together."},
    {"ref": 1, "name": "Real-time Video Processing",
     "description": "Streaming and dual-model architectures for reasoning over continuous video in near real-time.",
     "explanation": "This cluster gathers approaches to reasoning over video as it arrives. The shared papers pair streaming inference with dual-model and incremental-reasoning designs, so these ideas co-occur as variations on the same real-time constraint."}
  ]
}
```
