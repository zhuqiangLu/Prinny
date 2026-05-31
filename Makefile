.PHONY: css css-watch test

# Compile Tailwind -> static/app.css (run after editing templates). Fetches the
# standalone CLI on first use; see scripts/build_css.sh.
css:
	./scripts/build_css.sh

# Rebuild on change while developing.
css-watch:
	./bin/tailwindcss -c tailwind.config.js -i static/src/app.css -o static/app.css --watch

test:
	.venv/bin/python -m pytest -q
