#!/usr/bin/env python
"""Manual smoke test: render the wiki panel in REAL Safari.app and check Section 5.

This is NOT a pytest test (no ``test_`` prefix, lives under tests/manual/) — it
needs a running app server, Safari, and a one-time Safari setting, so it can't run
in CI. It drives the actual Safari application via ``safaridriver`` + Selenium
WebDriver, which catches engine-specific regressions the Chromium dev tools miss
(notably the Tailwind-CDN-JIT layout race that stacked Section 5 on WebKit).

Prerequisites
-------------
1. ``pip install -e '.[safari]'``  (installs selenium)
2. Enable Safari WebDriver (one time):
     - ``sudo safaridriver --enable``
     - Safari ▸ Settings ▸ Advanced ▸ "Show features for web developers"
     - Develop menu ▸ "Allow Remote Automation"  (checked)
3. Start the app:  ``python -m uvicorn app.main:app --port 8765``

Usage
-----
    python tests/manual/safari_smoke.py
    python tests/manual/safari_smoke.py --url http://localhost:8765 --slug longvideo
    python tests/manual/safari_smoke.py --shot /tmp/safari.png

Exit code 0 = all checks passed, 1 = a render check failed, 2 = Safari/setup error.

Note: NO_PROXY is forced to "*" below because a local HTTP proxy (if present)
otherwise intercepts Selenium's localhost connection to safaridriver ("Bad
Gateway"). This must be set before selenium imports its HTTP transport.
"""
from __future__ import annotations

import os

# Must precede the selenium import: the WebDriver HTTP client reads proxy env at
# import/connect time. Bypass any local proxy for the driver + app connections.
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")
for _v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_v, None)

import argparse
import json
import re
import sys
import time
import urllib.request

CHECK_JS = r"""
const sec=[...document.querySelectorAll('section')].find(s=>/Connections/.test(s.querySelector('h2')?.textContent||''));
if (sec) sec.scrollIntoView({block:'start'});
const cyto=document.getElementById('kg-cyto');
const papersSec=[...document.querySelectorAll('section')].find(s=>/Papers/.test(s.querySelector('h2')?.textContent||''));
const grid = sec && sec.querySelector('.kg-2col');
const stats = sec && sec.querySelector('.kg-stats');
return {
  sectionPresent: !!sec,
  heroCards: sec ? sec.querySelectorAll('.kg-2col-main > .rounded-xl').length : 0,
  twoColTracks: grid ? getComputedStyle(grid).gridTemplateColumns.split(' ').length : 0,
  statsCols: stats ? getComputedStyle(stats).gridTemplateColumns.split(' ').length : 0,
  mapInited: cyto ? (cyto.dataset.kgInit || 'unset') : 'none',
  mapCanvases: cyto ? cyto.querySelectorAll('canvas').length : 0,
  mapW: cyto ? cyto.offsetWidth : 0,
  mapH: cyto ? cyto.offsetHeight : 0,
  filterButtons: papersSec ? [...papersSec.querySelectorAll('button')].filter(b=>/Theme|All|Unmapped/.test(b.textContent)).length : 0,
};
"""


def _autodetect_slug(base_url: str) -> str | None:
    try:
        with urllib.request.urlopen(base_url + "/", timeout=5) as r:
            html = r.read().decode("utf-8", "replace")
    except OSError as e:
        print(f"Could not reach {base_url} to auto-detect a collection: {e}")
        return None
    m = re.search(r'/c/([a-z0-9][a-z0-9-]*)', html)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Real-Safari smoke test for Section 5.")
    ap.add_argument("--url", default="http://localhost:8765", help="App base URL.")
    ap.add_argument("--slug", default=None, help="Collection slug (auto-detected if omitted).")
    ap.add_argument("--shot", default="/tmp/safari_smoke.png", help="Screenshot path.")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    args = ap.parse_args()

    slug = args.slug or _autodetect_slug(args.url)
    if not slug:
        print("No collection slug. Pass --slug or ensure the app lists one at /.")
        return 2

    try:
        from selenium import webdriver
        from selenium.webdriver.safari.options import Options
    except ImportError:
        print("selenium not installed. Run:  pip install -e '.[safari]'")
        return 2

    try:
        driver = webdriver.Safari(options=Options())
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        print(f"Could not start Safari WebDriver: {msg}")
        if "remote automation" in msg.lower():
            print("→ Enable: Safari ▸ Develop ▸ Allow Remote Automation "
                  "(and `sudo safaridriver --enable`).")
        return 2

    try:
        driver.set_window_size(args.width, args.height)
        driver.get(f"{args.url}/c/{slug}")
        time.sleep(2.5)                              # HTMX panel + Cytoscape settle
        driver.execute_script("document.documentElement.classList.remove('dark');")
        time.sleep(0.8)
        r = driver.execute_script(CHECK_JS)
        time.sleep(0.2)
        driver.get_screenshot_as_file(args.shot)
    finally:
        driver.quit()

    print("Safari render metrics:\n" + json.dumps(r, indent=2))
    print(f"Screenshot: {args.shot}")

    # --- Checks. Layout ones are the regression guard; map/themes are gated on
    # the collection actually having a knowledge graph. ---
    checks: list[tuple[str, bool]] = [
        ("Section 5 present", r["sectionPresent"]),
        ("stat grid is 6 columns", r["statsCols"] == 6),
        ("two-column layout (not stacked)", r["twoColTracks"] == 2),
    ]
    if r["heroCards"]:
        checks.append(("collection map initialized", r["mapInited"] == "1"))
        checks.append(("map drew a canvas", r["mapCanvases"] >= 1))
        checks.append(("map sized to its column (>0 width)", r["mapW"] > 0))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if failed:
        print(f"\n{len(failed)} check(s) FAILED.")
        return 1
    print(f"\nAll {len(checks)} checks PASSED in real Safari.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
