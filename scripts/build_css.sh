#!/usr/bin/env bash
# Compile templates -> static/app.css with the Tailwind v3 standalone CLI.
# Downloads the (gitignored) binary on first run. Re-run after editing templates
# or adding new utility classes. Equivalent: `make css`.
set -euo pipefail
cd "$(dirname "$0")/.."

BIN=bin/tailwindcss
VER=v3.4.17

if [ ! -x "$BIN" ]; then
  mkdir -p bin
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)   ASSET=tailwindcss-macos-arm64 ;;
    Darwin-x86_64)  ASSET=tailwindcss-macos-x64 ;;
    Linux-x86_64)   ASSET=tailwindcss-linux-x64 ;;
    Linux-aarch64)  ASSET=tailwindcss-linux-arm64 ;;
    *) echo "Unsupported platform $(uname -s)-$(uname -m); download tailwindcss $VER to $BIN manually." >&2; exit 1 ;;
  esac
  echo "Downloading tailwindcss $VER ($ASSET)..."
  curl -fsSL -o "$BIN" "https://github.com/tailwindlabs/tailwindcss/releases/download/$VER/$ASSET"
  chmod +x "$BIN"
fi

exec "$BIN" -c tailwind.config.js -i static/src/app.css -o static/app.css --minify "$@"
