#!/usr/bin/env bash
# Re-download the vendored front-end libraries into static/vendor/. Run when you
# bump a version below. The app loads these locally (no CDN at runtime) so it works
# offline — consistent with the local-first design.
set -euo pipefail
cd "$(dirname "$0")/../static/vendor"

HTMX=1.9.12
FUSE=7.0.0
CYTOSCAPE=3.30.2
ALPINE=3.14.1
KATEX=0.16.9

echo "Vendoring front-end libs…"
curl -sSL -o htmx.min.js      "https://unpkg.com/htmx.org@${HTMX}/dist/htmx.min.js"
curl -sSL -o fuse.min.js      "https://cdn.jsdelivr.net/npm/fuse.js@${FUSE}/dist/fuse.min.js"
curl -sSL -o cytoscape.min.js "https://cdn.jsdelivr.net/npm/cytoscape@${CYTOSCAPE}/dist/cytoscape.min.js"
curl -sSL -o alpine.min.js    "https://cdn.jsdelivr.net/npm/alpinejs@${ALPINE}/dist/cdn.min.js"

mkdir -p katex/contrib katex/fonts
curl -sSL -o katex/katex.min.css "https://cdn.jsdelivr.net/npm/katex@${KATEX}/dist/katex.min.css"
curl -sSL -o katex/katex.min.js  "https://cdn.jsdelivr.net/npm/katex@${KATEX}/dist/katex.min.js"
curl -sSL -o katex/contrib/auto-render.min.js \
  "https://cdn.jsdelivr.net/npm/katex@${KATEX}/dist/contrib/auto-render.min.js"
# KaTeX's CSS requests its fonts by relative path — fetch the woff2 set it references.
for f in $(grep -oE "fonts/KaTeX_[A-Za-z0-9_-]+\.woff2" katex/katex.min.css | sort -u | sed 's#fonts/##'); do
  curl -sSL -o "katex/fonts/$f" "https://cdn.jsdelivr.net/npm/katex@${KATEX}/dist/fonts/$f"
done
echo "Done. Vendored into static/vendor/."
