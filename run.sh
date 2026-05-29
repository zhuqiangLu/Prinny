#!/usr/bin/env bash
# Launch Paper Agent (FastAPI + uvicorn) from its own virtualenv.
#
#   ./run.sh            # start on http://localhost:8000
#   ./run.sh 8001       # start on a different port
#   PORT=8001 ./run.sh  # same, via env var
#
# First run creates .venv and installs dependencies; later runs reuse it.
set -euo pipefail

# Work from the repo root (where this script lives), so it runs from anywhere.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PORT="${PORT:-${1:-8000}}"
PY=".venv/bin/python"

# Bootstrap the virtualenv on first run.
if [ ! -x "$PY" ]; then
  echo "→ First run: creating .venv and installing dependencies…"
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e .
fi

URL="http://localhost:${PORT}"
echo "→ Paper Agent → ${URL}   (Ctrl+C to stop)"
echo "  Settings (OpenAI key, Zotero paths) live at ~/.paper-agent/config.toml or the ⚙ in the app."

# Open the browser once the server is up (macOS `open`; harmless elsewhere).
( sleep 1.5; command -v open >/dev/null 2>&1 && open "$URL" ) &

# `exec` so Ctrl+C goes straight to uvicorn. --reload picks up code edits.
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --reload
