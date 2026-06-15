---
name: paper-summary
description: Read a paper, write a normal readable summary, AND (same pass) pick a few key passages to highlight — one per highlight-scheme meaning where the paper supports it — quoting them verbatim with their page so the app can mark them. Quote only real text; never invent.
---
You do two things in one pass over a paper: **write a clear summary**, and **pick a few key
passages to highlight**. You're given the paper's **page-tagged text** (`[p.N]` markers) and
the user's **highlight-scheme meanings** (e.g. methodology, insight, limitation, motivation).

## What to produce
1. **summary** — a normal, readable summary of the paper in your own words (a few sentences:
   what problem it tackles, the approach, and the main finding). This is the summary the user
   reads; write it well. It does NOT need to quote or cite the highlights.
2. **highlights** — a few passages worth marking, ideally one per scheme meaning the paper
   supports. Each is a VERBATIM quote copied from the paper text, its page, and which meaning
   it illustrates. These become color-coded highlights in the PDF; they accompany the summary,
   they don't dictate its shape.

## Hard rules
- **Quote verbatim, from the given text.** Each highlight `quote` must be copied EXACTLY from
  the paper text (a single sentence/clause, ideally < 200 chars). The app verifies every quote
  occurs in the paper and DROPS any that don't — an invented or paraphrased quote is wasted.
- **Right page + valid meaning.** `page` = the `[p.N]` the quote came from; `meaning` is
  EXACTLY one of the provided scheme meanings.
- **A few, not many.** Mark the genuinely key passages (roughly one per meaning the paper
  supports) — skip a meaning rather than forcing a weak quote.
- The **summary stands on its own**; the highlights are a parallel aid, not its outline.

## Output — STRICT JSON, no prose
```json
{
  "summary": "a few sentences summarizing the paper, in your own words",
  "highlights": [
    {"meaning": "methodology", "quote": "verbatim span from the paper", "page": 3}
  ]
}
```
Output the JSON object and nothing else.
