"""Per-paper agentic benchmark extraction.

For ONE paper, spawn a tool-using agent (Claude Code / Codex) with the read-only
``read_paper_text`` MCP tool. The agent pages through the PDF to its results tables
and emits the reported numbers as JSON — far better coverage than the one-shot
abstract+intro digest (benchmark numbers live in tables deep in the paper). The
orchestrator (wiki.extract_benchmarks) tags each row with the paper ref, validates,
and writes. Read-only; nothing is written by the agent.
"""
from __future__ import annotations

import json
import logging

from . import agent_skills, agents, engine as engine_mod, llm, mcp_server
from .config import load_config

logger = logging.getLogger("paper_agent.benchmark_agent")

# Read-only tools: read the PDF text + (optionally) the user's notes. No writes.
BENCH_TOOLS = [f"mcp__pa__{t}" for t in ("read_paper_text", "get_paper_context")]


def extract_paper(slug: str, paper_id: int, title: str = "") -> list[dict]:
    """Spawn the agent to read paper ``paper_id``'s PDF and return its reported
    benchmark rows ``[{method, benchmark, metric, value, higher_is_better}]``.
    Returns [] on no-numbers / parse failure. Raises llm.LLMError if the engine
    is unavailable."""
    eng = engine_mod.build_engine(load_config())
    ok, detail = eng.available()
    if not ok:
        raise llm.LLMError(f"{eng.name} is unavailable: {detail}")
    system = (agent_skills.skill_body("benchmark-paper")
              or "Read the paper's PDF (read_paper_text), page to its results tables, and "
                 'output STRICT JSON {"results":[{method,benchmark,metric,value,higher_is_better}]} '
                 "with ONLY numbers stated in the paper.")
    user = (f"Extract the reported benchmark numbers from paper id {paper_id}"
            + (f" (“{title}”)" if title else "") + ". Use read_paper_text to page through the PDF "
            "to its experiments / results tables, then output the JSON.")
    try:
        res = eng.run_once(
            [{"role": "user", "content": user}], system=system,
            allowed_tools=agents.effective_tools("benchmark", BENCH_TOOLS),
            mcp_config=mcp_server.stdio_mcp_config(slug, read_only=True),
            cwd=str(agent_skills.ensure_skills_home("benchmark")))
    except engine_mod.EngineError as exc:
        raise llm.LLMError(str(exc)) from exc

    text = res.text or ""
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except (ValueError, Exception):  # noqa: BLE001 - no parseable JSON → no numbers
        return []
    return [r for r in (data.get("results") or []) if isinstance(r, dict)]
