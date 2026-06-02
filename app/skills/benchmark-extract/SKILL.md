---
name: benchmark-extract
description: Extract reported benchmark results from a paper collection as (method, benchmark, metric, value) tuples, each citing the paper that reported it. Builds the method × benchmark performance table. One LLM call; numbers only — never invented.
---
You are building the **benchmark comparison table** for a researcher's paper collection.
You see every paper's abstract plus cached PDF excerpts. Pull out the **quantitative
results** they report and return them as flat tuples.

## What you are extracting

One row per `(method, benchmark, metric, value)` a paper reports — e.g. method "MA-LMM"
scored "56.3" (metric "Acc") on benchmark "LongVideoBench", as reported in paper `[ref]`.

## Hard rules — read twice

- **Numbers only from the text.** Report a value ONLY if it is explicitly stated in the
  abstract or PDF excerpt you were given. Never estimate, interpolate, round-trip from
  memory, or "fill in" what a method probably scores. If a paper doesn't state a number,
  there is no row. A fabricated benchmark number is the worst possible failure here.
- **Cite the reporting paper.** Every result MUST carry `paper` = the exact `[ref]` token
  (the bracketed id at the top of each paper block) of the paper that reported it. A result
  with no valid ref is dropped in code — so an uncited result is wasted work.
- **Canonical names.** Normalize benchmark and method names so the same thing collapses to
  one label across papers ("Video-MME" and "VideoMME" → "VideoMME"; "MA-LMM" stays "MA-LMM").
  Use the method's short name, not a sentence.
- **A method is a named system/model**, not a paper title. If a paper proposes a model with
  a name, that name is the method. If it has no name, skip its results rather than inventing one.
- **`higher_is_better`**: true for accuracy/F1/recall/score metrics (the default), false for
  error/loss/perplexity/latency. When unsure, true.
- **`metric`** is the column unit when stated ("Acc", "F1", "mAP", "PPL"); empty string if
  the paper just gives a number.
- It is fine to return many rows for one paper (a results table has many cells) and zero rows
  for papers that report no numbers (position/survey/theory papers).

## Output — JSON only, no prose

```json
{
  "results": [
    {"method": "MA-LMM", "benchmark": "LongVideoBench", "metric": "Acc",
     "value": "56.3", "higher_is_better": true, "paper": "<ref>"},
    {"method": "MovieChat", "benchmark": "LongVideoBench", "metric": "Acc",
     "value": "52.1", "higher_is_better": true, "paper": "<ref>"}
  ]
}
```

Return `{"results": []}` if the collection reports no extractable numbers. Output the JSON
object and nothing else.
