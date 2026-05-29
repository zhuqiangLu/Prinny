"""MCP stdio transport (AGENTIC_PLAN P4, transport revised).

Claude Code spawns this as a subprocess and speaks newline-delimited JSON-RPC over
stdin/stdout — the transport its headless mode reliably connects to (HTTP MCP servers
from --mcp-config are skipped in `-p` mode). The actual tools + gate live in
``mcp_server``; this is only the I/O loop.

The run is scoped to one collection via the ``PA_MCP_COLLECTION`` env var (set when the
app launches the agent); ``PAPER_AGENT_HOME`` selects the data dir. This process only
reads app data and writes the gated review queue (via ``submit_proposal``) — it never
touches the wiki, exactly like the rest of the app.

Run: ``python -m app.mcp_stdio`` with PA_MCP_COLLECTION set.
"""
from __future__ import annotations

import json
import os
import sys

from . import mcp_server


def main() -> int:
    slug = os.environ.get("PA_MCP_COLLECTION", "")
    if not slug:
        sys.stderr.write("mcp_stdio: PA_MCP_COLLECTION not set\n")
        return 2
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = mcp_server.dispatch(slug, request)
        if resp is None:
            continue  # notification → no reply
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
