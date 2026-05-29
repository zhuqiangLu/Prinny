# Per-Paper Chat → Interactive Paper Sub-Agent (Phase 8 — build plan)

Turns the per-paper chat from a stateless "stuff context into one completion" into an
**interactive, read-only paper-reading sub-agent**: a persistent conversation that reads
the actual PDF and has predefined paper-reading skills. Grilled design; build phased.

## Decisions (from grilling)

| Dimension | Decision |
|---|---|
| **What** | Per-paper chat becomes a `PaperChatAgent` (read-only) per paper thread. |
| **Persistence** | **Two modes, Settings-selectable** (`chat_session_mode`): `resume` (spawn `--resume` per turn; stateless process, stateful session) and `live` (one long-lived process). Build resume first, live later. Session id stored on the thread row. |
| **PDF** | Claude: built-in **Read** on the cached PDF (figures/tables, on demand). Codex: render PDF pages → **images via `-i`** (+ `read_paper_text` MCP tool for text). `cwd` scoped to the paper dir. |
| **Skills** | Claude: shipped **`SKILL.md`** set (summarize-section, extract-contributions, compare-to-my-notes, list-assumptions, locate-figure, find-evidence-for…), model-invoked + `/name`. Codex: `AGENTS.md`/role-prompt equivalent. |
| **Your notes/highlights** | On-demand MCP **`get_paper_context(paper_id)`** — always current (reflects mid-chat annotations). |
| **Streaming** | Yes — SSE chat UI; tool-call status surfaced; both persistence modes stream. |
| **Engine scope** | Build **Claude + Codex** behind one seam. OpenAI and any **no-PDF** case → today's classic stuffed chat (kept as fallback). |
| **Guardrail (unchanged)** | Read-only. **No auto-save.** Chat → artifacts only via "draft notes" (you edit/accept) or Phase-6 capture. `chat_messages` becomes display + draft-source; the engine *session* owns the model-facing transcript. |

## Codex verify-gate findings (codex-cli 0.131.0) — Codex CAN be a sub-agent
- `codex exec resume <uuid|thread-name> [prompt]` (or `--last`) → persistent session ✓
- `codex mcp add <name> --env K=V -- <cmd>` and inline `-c mcp_servers.*` → stdio MCP + env ✓ (also streamable-HTTP)
- `-i/--image <FILE>` → attach rendered PDF page images → **visual** paper reading ✓ (needs a PDF→image render step; cleanest at session start)
- `--json` + `-o <file>` → parse events + final message/session id
- No `SKILL.md` → Codex "skills" via `AGENTS.md` + role prompt (the gap vs Claude)

## Build order (each phase usable on its own)
0. **Verify Codex** — DONE (findings above).
1. **Phase A — Claude sub-agent, resume mode + streaming.** ← DONE
   `PaperChatAgent` seam + `ClaudeCodePaperAgent` (`--session-id`/`--resume`, Read on the
   PDF + `read_paper_text`/`get_paper_context` MCP tools), NDJSON streaming chat (live
   token + tool-status), session id on the thread, classic fallback for no-PDF/OpenAI.
2. **Phase B — Skills.** ← DONE — shipped the SKILL.md set in app/skills/, materialized
   into APP_DIR/agent-home/.claude/skills (the sub-agent's cwd) for discovery.
3. **Phase C — Live-process mode** + the `chat_session_mode` Settings toggle. ← DONE
4. **Phase D — Codex sub-agent.** ← DONE (experimental; opted in by selecting Codex).
   `CodexPaperAgent` runs `codex exec`/`exec resume --json` via inline `-c` overrides on
   the user's real CODEX_HOME (auth intact): our stdio MCP server (PA_MCP_READONLY),
   `approval_policy="never"`, `sandbox_mode="read-only"`, and per-tool
   `approval_mode="approve"` on ONLY the read tools (writes need approval → denied). PDF
   read as text via read_paper_text (no visual Read on Codex). Session resume via the
   `thread.started` thread_id. SAFE: read-only sandbox + server read-only mode + read-only
   tool approvals — NO `--dangerously-bypass-approvals-and-sandbox`.

## Invariants carried in from AGENTIC_PLAN
- The agent never writes the wiki/notes; only user actions do. Chat is read-only.
- Anything the agent produces that the user keeps is stamped `(seed, agent)` (Phase-6
  capture) or lands as an editable draft — never an assertion, never auto-saved.
- Built-in `Read` is fs-wide; we scope `cwd` to the paper dir and accept that it is not a
  hard sandbox (chat is read-only, single local user, own papers).
