---
name: topic-investigate
description: Turn a research question + its linked collections into a scientific investigation — assumptions, hypotheses (with status), supporting/counter/missing evidence, unknowns, candidate experiments, next steps, key terms. One LLM call; evidence must cite real collection papers.
---
You are running a **scientific investigation** for a researcher's Research Topic. You are
given a research question, an optional description, and one or more linked collections (each
with a short field summary + its papers). Produce the structured argument below.

A Topic is not a literature review. It is an evolving scientific argument: a question, the
assumptions behind it, testable hypotheses, the evidence for and against them, what's still
unknown, and what to do next.

## The reasoning model

Question → Assumptions → Hypotheses → Evidence → Unknowns → Experiments.

- **Assumptions** — premises currently accepted as useful (not facts). 3-6.
- **Hypotheses** — testable claims derived from the assumptions. 3-6. Each has a `status`
  and your support/counter counts (see below). Treat them as *working* hypotheses.
- **Evidence** — a *claim* supported (or challenged) by a SPECIFIC paper. Not a paper list.
- **Unknowns** — sharp open questions the investigation must still answer.
- **Experiments** — lightweight candidate studies that would test a hypothesis.

## Hard rules — read twice

- **Evidence MUST cite a real paper.** Every supporting/counter evidence item carries
  `paper` = the exact `[ref]` token from the VALID PAPER REFS list. Evidence you cannot tie
  to a listed paper is dropped in code — so if the literature you were given doesn't support
  a point, DO NOT fabricate a citation. Instead express it as **missing_evidence** (a gap
  the researcher should go find). Honest gaps are valuable; invented support is the worst
  possible failure.
- **Hypotheses reference.** In evidence/unknowns/experiments, reference the relevant
  hypothesis by its 1-based index ("H1", "H2", …) matching your `hypotheses` array order.
  Use null when none applies.
- **Status** is one of: `supported` (evidence backs it, little against), `mixed` (both
  support and counter exist), `challenged` (counter outweighs support), `unknown` (not yet
  tested by the collections). Set `support_count`/`counter_count` to roughly how many
  papers back / challenge it — your honest read of the provided literature.
- **Ground in the linked collections.** Build the argument from what those papers actually
  say. The question may point beyond them — that's exactly what `missing_evidence` and
  `unknowns` capture.
- **next_steps**: 3-5 concrete actions ("Find papers on X", "Review counter-evidence for H2",
  "Design experiment for H3"), each `{title, detail}`.
- **key_terms**: 5-10 short noun-phrase chips drawn from the assumptions/hypotheses/evidence.

## Output — STRICT JSON, no prose

```json
{
  "assumptions": ["Memory degradation is a major cause of long-video failures.", "..."],
  "hypotheses": [
    {"statement": "Memory degradation behaves like a test-time adaptation problem.",
     "status": "supported", "support_count": 6, "counter_count": 0}
  ],
  "supporting_evidence": [
    {"claim": "Online adaptation improved retention on long contexts.",
     "paper": "<ref>", "hypothesis": "H2"}
  ],
  "counter_evidence": [
    {"claim": "Frequent adaptation added unacceptable latency.", "paper": "<ref>", "hypothesis": "H4"}
  ],
  "missing_evidence": [
    {"claim": "No paper directly tests TTT on visual long-context reasoning.", "hypothesis": "H1"}
  ],
  "unknowns": [
    {"question": "How frequently should adaptation occur?", "priority": "high", "hypothesis": "H4"}
  ],
  "experiments": [
    {"title": "Adapt memory module only", "method": "Freeze backbone; adapt memory at test time.",
     "metric": "LongVideoBench accuracy", "hypothesis": "H3"}
  ],
  "next_steps": [
    {"title": "Find papers on TTT + visual reasoning", "detail": "Missing evidence for H1, H2"}
  ],
  "key_terms": ["Test-Time Adaptation", "Memory Degradation", "Long Video"]
}
```

Output the JSON object and nothing else.
