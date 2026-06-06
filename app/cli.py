"""Console entrypoint: ``paper-agent`` launches the local web app.

Runs a preflight (Claude/Codex CLI on PATH, Zotero reachable), then starts uvicorn
and — unless ``--no-open`` — opens the browser. Single local user; no auth.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import threading
import webbrowser


def _preflight() -> None:
    """Warn (don't block) on missing pieces: the LLM CLI and Zotero are what the
    app needs, but neither is required just to browse the UI."""
    from .config import load_config

    cfg = load_config()
    engine = cfg.get("engine") or "claude-code"
    binary = cfg.get("codex_bin", "codex") if engine == "codex" else cfg.get("claude_bin", "claude")
    if not shutil.which(binary):
        print(f"  ⚠  '{binary}' not found on PATH — LLM features (chat, wiki drafting, "
              f"suggested reading) need the {engine} CLI installed and authenticated.",
              file=sys.stderr)
    else:
        print(f"  ✓  LLM engine: {engine} ({binary})")

    from pathlib import Path
    zotero = Path.home() / "Zotero" / "zotero.sqlite"
    if zotero.exists():
        print("  ✓  Zotero database found.")
    else:
        print("  ⚠  Zotero not found at ~/Zotero/zotero.sqlite — collections/papers "
              "won't load until Zotero is installed (or the path is set in Settings).",
              file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="paper-agent",
                                description="Personal research-wiki agent over Zotero collections.")
    p.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="bind port (default 8000)")
    p.add_argument("--no-open", action="store_true", help="don't open the browser")
    p.add_argument("--reload", action="store_true", help="auto-reload (development)")
    args = p.parse_args(argv)

    print("Prinny — personal research-wiki agent")
    _preflight()
    url = f"http://{args.host}:{args.port}"
    print(f"  →  {url}")

    if not args.no_open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
