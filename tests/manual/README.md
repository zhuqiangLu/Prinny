# Manual tests

Scripts here are **not** run by pytest (they have no `test_` prefix and need a
live server / a real browser / one-time machine setup). Run them by hand.

## `safari_smoke.py` — real Safari.app render check

Verifies the wiki panel's **Section 5 (Connections & themes)** renders correctly
in **Safari** — the two-column layout, the 6-up stat grid, and the live Cytoscape
collection map. This catches WebKit/Safari-specific regressions that Chromium-based
dev tools miss (e.g. the Tailwind-CDN-JIT layout race that once stacked the section).

### One-time setup
```sh
pip install -e '.[safari]'          # installs selenium
sudo safaridriver --enable
# Safari ▸ Settings ▸ Advanced ▸ "Show features for web developers"
# Develop menu ▸ "Allow Remote Automation"  (check it)
```

### Run
```sh
python -m uvicorn app.main:app --port 8765      # in one shell
python tests/manual/safari_smoke.py             # in another
```
Auto-detects the first collection at `/`. Override with `--slug`, `--url`, `--shot`.
Exit code `0` = pass, `1` = a render check failed, `2` = Safari/setup error.

> The script forces `NO_PROXY=*` so a local HTTP proxy doesn't intercept Selenium's
> localhost connection to safaridriver (which otherwise fails with "Bad Gateway").
