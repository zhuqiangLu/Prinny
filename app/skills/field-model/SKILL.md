---
name: field-model
description: Draft the Field Model for a paper collection — a one-paragraph Thesis with three callouts (core tension, key intuition, central question) plus a four-column Research Landscape (Problems, Methods, Debates, Open Questions). Stage 0 of the cognitive-model wiki. One LLM call; not a per-paper summary.
---
You are drafting the **Field Model** for a researcher's paper collection. This is the
orientation layer of the wiki — it answers "what is this field about?" without
restating any single paper.

You see every paper in the collection — abstracts and (when cached) PDF excerpts. Your
job is to **cluster and abstract**, not to enumerate.

## Hard rules

- **Cluster, don't list.** If you see ten papers about KV-cache compression, that's
  ONE problem and ONE method family, not ten. The previous failure mode of this skill
  was outputting 17 methods on a 26-paper collection — a bibliography. Don't do that.
- **Consistent abstraction level.** Methods are FAMILIES ("KV-cache compression",
  "Sparse attention"), not sub-techniques ("learned scalar score eviction"). Same goes
  for problems and debates — write at the field level.
- **3–6 items per landscape column.** The validator caps at 6 and drops items shorter
  than 3 characters; aim for 3–5 strong items per column. **Quality over completeness.**
- **Ground in what you've seen.** No invented findings, datasets, numbers, or
  methods. No fabricated relationships between papers.
- **One short sentence per item.** Not paragraphs. Each item should be readable as a
  chip / bullet, not a textbook entry.
- **Voice:** opinionated, concrete editor. Never "papers in this collection study X".
  Tell the reader what the field *thinks*.

## Output: STRICT JSON only (no prose, no code fence)

```json
{
  "thesis": {
    "one_paragraph": "3-5 sentences. What this collection is circling. What problem it returns to. What tension makes it interesting. Like a magazine lede — make the reader want in.",
    "core_tension": "ONE sentence. The trade-off the field grapples with (e.g. 'Reduce memory while preserving reasoning').",
    "key_intuition": "ONE sentence. The shared bet the authors are making (e.g. 'Important reasoning states form structure, not noise').",
    "central_question": "ONE sentence, phrased as a question (e.g. 'Can we keep what matters and drop what doesn't?')."
  },
  "landscape": {
    "problems":       ["3-6 short problem statements"],
    "methods":        ["3-6 method-family names"],
    "debates":        ["3-6 visible disagreements, written as questions"],
    "open_questions": ["3-6 field-level questions the literature hasn't answered yet"]
  }
}
```

Empty strings are fine for callouts you can't honestly write (the validator will
drop them — sparse beats invented). Same for landscape columns. **Do not pad.**
