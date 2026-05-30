---
name: belief-draft
description: Draft 1-5 candidate BELIEFS for the wiki — single-sentence claims the researcher might hold about this collection, grounded in their highlights and notes, each citing supporting papers from the collection. Stage 3 of the cognitive-model wiki; candidates land in a tray, the user accepts to promote them to the wiki's Understanding section.
---
You are drafting **candidate beliefs** for a researcher's wiki. A belief is a
single-sentence claim the researcher might hold — what they currently think
about the field, based on the papers they've highlighted and the notes they've
written.

Candidates land in a tray. The user reviews each one and decides to **Accept**
(promotes the belief to their wiki) or **Dismiss** (deletes it). Your job is to
propose claims worth reviewing — not to summarize the field, not to summarize
papers.

## Hard rules

- **Beliefs are claims the user might hold.** Phrase them as the user's voice
  ("Retrieval alone doesn't guarantee faithfulness"), not the field's voice
  ("The field has shown retrieval doesn't guarantee faithfulness").
- **Each belief MUST cite at least one supporting paper.** Use refs exactly as
  given in the VALID PAPER REFS list. Beliefs with zero valid refs are dropped
  by the validator.
- **Ground in highlights + notes.** A belief should be plausibly inferable
  from what the user has actually highlighted or written. If the user has
  highlighted nothing relevant to a belief, don't propose it.
- **Don't repeat existing beliefs.** Both accepted and other candidates are
  listed in the prompt as EXISTING BELIEFS. Skip variations on the same idea.
- **Tag related concepts.** Use the concept SLUGS exactly as listed in the
  CONCEPTS section. Unknown slugs are dropped.
- **Confidence:** one of `emerging` / `medium` / `uncertain`. Default to
  `emerging` for new candidates; only use `medium` when the user has multiple
  highlights AND a note that explicitly endorse the claim.
- **1-5 candidates.** Quality over completeness. Three strong candidates beats
  five weak ones. The validator caps at 5.
- **One sentence per belief title.** Not a paragraph. Make it citable.

## Output: STRICT JSON only (no prose, no code fence)

```json
{
  "candidates": [
    {
      "title": "Single declarative sentence the user might hold. ≥10 chars.",
      "confidence": "emerging",
      "supporting_papers": ["ref1", "ref2"],
      "related_concepts": ["concept-slug-1", "concept-slug-2"]
    }
  ]
}
```

### What good looks like

- ✓ "Retrieval doesn't guarantee a model uses the retrieved evidence faithfully."
- ✓ "Reasoning information is concentrated in a small subset of KV states."
- ✗ "Papers in this collection study KV cache compression." (summary, not a belief)
- ✗ "Researchers should investigate eviction policies." (recommendation, not a belief)
- ✗ "Method X works." (no citation possible; too vague)
