"""Guard: static/tw-accent.css must define every accent utility the templates use.

The Tailwind Play CDN can miss accent classes that only appear in HTMX-swapped
fragments (white-on-white on Safari); static/tw-accent.css is the plain-CSS
backstop. If a template gains a new accent class without regenerating the file,
the backstop goes stale and Safari breaks again — this test catches that.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "gen_accent_css", ROOT / "scripts" / "gen_accent_css.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_accent_css_is_up_to_date():
    """static/tw-accent.css == freshly generated. If this fails, run:
    python scripts/gen_accent_css.py"""
    expected = gen.build_css(gen.collect_tokens())
    actual = (ROOT / "static" / "tw-accent.css").read_text(encoding="utf-8")
    assert actual == expected, (
        "static/tw-accent.css is stale — run `python scripts/gen_accent_css.py` "
        "and commit the result.")


def test_known_swapped_accents_are_covered():
    """The button that originally broke on Safari (bg-emerald-600 +
    hover:bg-emerald-700) must be present."""
    css = (ROOT / "static" / "tw-accent.css").read_text(encoding="utf-8")
    assert ".bg-emerald-600{" in css
    assert r".hover\:bg-emerald-700:hover{" in css


def test_backstop_is_linked_in_base():
    """base.html must load the backstop stylesheet."""
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert "/static/tw-accent.css" in base
