---
name: list-assumptions
description: Surface the assumptions and limitations the paper relies on, grounded in the text.
---
When the user wants the paper's assumptions or limitations:

1. Read the setup/method and any limitations/discussion sections via `read_paper_text`.
2. List the assumptions the work depends on (data, setting, scale, baselines, metrics)
   and the limitations the authors acknowledge — separately.
3. Ground each in the text (page/section). Distinguish what the paper STATES from what
   you INFER, and label inferences clearly. Don't manufacture weaknesses the text doesn't
   support; the user's own critical judgment is what counts.
