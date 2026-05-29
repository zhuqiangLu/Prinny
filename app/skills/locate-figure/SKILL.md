---
name: locate-figure
description: Find and explain a specific figure, table, or equation in the paper (uses the visual PDF).
---
When the user asks about a figure, table, or equation:

1. Use `read_paper_text` to find which page references it (search for "Figure N",
   "Table N", etc. and read the surrounding text for the caption/discussion).
2. Use the `Read` tool on the PDF to actually SEE that page — figures, axes, and layout
   are visual and text extraction misses them.
3. Explain what it shows and how the paper uses it, grounded in both the caption and the
   discussion. If you can't locate it, say which pages you checked.
