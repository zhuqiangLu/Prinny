/** Tailwind v3 config — compiled to static/app.css by the standalone CLI
 *  (see scripts/build_css.sh / `make css`). Replaces the Play CDN so classes
 *  used only in HTMX-swapped fragments are always present (the CDN's runtime
 *  JIT missed them in Safari). Mirrors the old CDN config: darkMode class +
 *  typography plugin.
 *
 *  `content` is scanned as raw text, so class tokens inside Jinja `{% set %}`
 *  dicts (e.g. KIND_STYLE / STRENGTH / PALETTE) and `{% if %}` branches are
 *  picked up too. */
module.exports = {
  darkMode: "class",
  content: [
    "./templates/**/*.html",
    "./static/**/*.js",
    "./app/**/*.py", // some classes are emitted from Python (flash msgs, etc.)
  ],
  theme: { extend: {} },
  plugins: [require("@tailwindcss/typography")],
};
