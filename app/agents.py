"""Sub-agent registry for the Agents page.

Each entry is one scoped Claude Code / Codex run: a skills "home" (advisory instructions the
user can edit), a fixed MCP tool allowlist, and permissions. The tool lists are imported from
the real spawn-site constants so this page can't drift from what the agents actually run with.
"""
from __future__ import annotations

import json

# (key, name, job, home, "perms"). Tools are resolved live from each module's constant.
_DEFS = [
    ("paper", "Paper reader",
     "Answers questions about the open paper, grounded in its PDF and your own notes/highlights.",
     "paper", "Read-only sandbox. The Read tool + read-only MCP only — it can never write your "
     "notes, wiki, or Zotero."),
    ("chat", "Collection chat",
     "Answers across the whole collection (papers, your notes, the wiki) for the side chat.",
     "chat", "Read-only. Surfaces and connects what you've written; never mutates artifacts."),
    ("wiki", "Starter overview",
     "Drafts the problem-oriented starter overview from the papers' abstracts on import.",
     "wiki", "One-shot completion over abstracts — no tools/MCP. Writes the agent-tagged "
     "overview map (the user-approved CLAUDE.md amendment); your own pages are untouched."),
    ("finder", "Paper finder (deep search)",
     "Suggested reading's 🔬 Deep search: iteratively searches arXiv, cross-checks your "
     "library, and learns from your accept/reject history to propose papers.",
     "finder", "Read-only: arXiv search + your collection (read) + accept/reject history. "
     "Proposes candidates you review; never adds or writes anything."),
]


def _resolve_tools(key: str) -> list[str]:
    """The MCP/Read tool allowlist for an agent, from its real spawn-site constant."""
    if key == "paper":
        from .paper_chat import _TOOLS
        return list(_TOOLS)
    if key == "chat":
        from .agentic_chat import CHAT_TOOLS
        return list(CHAT_TOOLS)
    if key == "finder":
        from .paper_finder import FINDER_TOOLS
        return list(FINDER_TOOLS)
    return []   # wiki: one-shot, no tools


def _base(tool: str) -> str:
    return tool.replace("mcp__pa__", "")


def _full(base: str) -> str:
    """Full allowlist token for a tool base ('Read' is a built-in; the rest are MCP)."""
    return base if base == "Read" else f"mcp__pa__{base}"


def read_universe() -> list[str]:
    """Every READ tool the user is allowed to grant: the Read built-in + all non-write MCP
    tools. Write/mutating tools are deliberately excluded — they're never UI-grantable."""
    from . import mcp_server
    return ["Read"] + [t["name"] for t in mcp_server._TOOLS if t["name"] not in mcp_server.WRITE_TOOLS]


def all_mcp_tools() -> list[dict]:
    """The full tool catalog for the '+ Add tool' picker: ``[{name, write, desc}]`` (reads
    first, then the locked write tools)."""
    from . import mcp_server
    desc = {t["name"]: t["description"] for t in mcp_server._TOOLS}
    reads = [{"name": "Read", "write": False, "desc": "Read a paper's PDF (figures/tables/layout)."}]
    reads += [{"name": t["name"], "write": False, "desc": desc[t["name"]]}
              for t in mcp_server._TOOLS if t["name"] not in mcp_server.WRITE_TOOLS]
    writes = [{"name": n, "write": True, "desc": desc.get(n, "")} for n in sorted(mcp_server.WRITE_TOOLS)]
    return reads + writes


def _overrides() -> dict:
    """Per-agent ENABLED read-tool set: ``{agent_key: [tool_base, ...]}`` — the complete read
    allowlist the user wants (added and/or removed vs the code default). Absent ⇒ use defaults.
    Write tools are never stored here (always taken from code)."""
    from .config import load_config
    try:
        d = json.loads(load_config().get("agent_tool_overrides") or "")
    except (TypeError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def _default_read_bases(key: str) -> list[str]:
    from . import mcp_server
    return [_base(t) for t in _resolve_tools(key) if _base(t) not in mcp_server.WRITE_TOOLS]


def effective_tools(key: str, default_tools: list[str]) -> list[str]:
    """The allowlist an agent actually spawns with. Write tools stay exactly as defined in code
    (the lethal-trifecta boundary — never user-addable/removable). Read tools follow the user's
    override (which may add tools beyond, or remove tools from, the code defaults)."""
    from . import mcp_server
    writes = [t for t in default_tools if _base(t) in mcp_server.WRITE_TOOLS]
    ov = _overrides().get(key)
    if ov is None:
        reads = [t for t in default_tools if _base(t) not in mcp_server.WRITE_TOOLS]
    else:
        uni = set(read_universe())
        reads = [_full(b) for b in ov if b in uni]      # ignore anything not a valid read tool
    return reads + writes


def set_tool_enabled(key: str, tool_base: str, enabled: bool) -> None:
    """Grant/revoke a READ tool for an agent. No-ops for write tools (locked) or anything outside
    the read universe (can't invent tools). Read tools may be added beyond the code defaults."""
    if tool_base not in read_universe():                # rejects write tools + unknown names
        return
    ov = _overrides()
    cur = set(ov.get(key, _default_read_bases(key)))
    cur.add(tool_base) if enabled else cur.discard(tool_base)
    if cur == set(_default_read_bases(key)):
        ov.pop(key, None)                               # back to default ⇒ drop the override
    else:
        ov[key] = sorted(cur)
    from .config import save_config
    save_config({"agent_tool_overrides": json.dumps(ov)})


def reset_tools(key: str) -> None:
    """Drop an agent's tool override → back to the code-defined defaults."""
    from .config import save_config
    ov = _overrides()
    if ov.pop(key, None) is not None:
        save_config({"agent_tool_overrides": json.dumps(ov)})


def _home_for(key: str) -> str | None:
    return next((home for k, _n, _j, home, _p in _DEFS if k == key), None)


def reset_skills(key: str) -> None:
    """Reset every skill of an agent to its shipped default (drops user overrides)."""
    from . import agent_skills
    home = _home_for(key)
    for name in agent_skills._HOMES.get(home, []):
        agent_skills.reset_skill(name)


def list_agents() -> list[dict]:
    """Each sub-agent with its job, editable skills, MCP tools (with enabled/write state),
    and permissions. Read tools are toggleable; write tools are locked in code."""
    from . import agent_skills, mcp_server
    tool_desc = {t["name"]: t["description"] for t in mcp_server._TOOLS}
    tool_desc["Read"] = "Read a paper's PDF (figures/tables/layout)."
    writes = mcp_server.WRITE_TOOLS
    out = []
    for key, name, job, home, perms in _DEFS:
        skills = [s for s in (agent_skills.read_skill(n) for n in agent_skills._HOMES.get(home, [])) if s]
        eff = effective_tools(key, _resolve_tools(key))      # what it actually spawns with
        enabled = [_base(t) for t in eff]
        tools = [{"name": _base(t), "write": _base(t) in writes, "enabled": True,
                  "desc": tool_desc.get(_base(t), "")} for t in eff]
        out.append({"key": key, "name": name, "job": job, "perms": perms,
                    "skills": skills, "tools": tools, "enabled": enabled,
                    "tools_customized": key in _overrides(),
                    "skills_customized": any(s.get("customized") for s in skills)})
    return out
