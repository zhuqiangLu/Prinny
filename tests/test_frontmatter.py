from app import frontmatter


def test_roundtrip_scalars_and_lists():
    meta = {
        "type": "problem",
        "title": "Efficiency",
        "sources": ["ABC", "DEF"],
        "derived_from_thoughts": [],
    }
    text = frontmatter.dump(meta, "# Body\n\nhello")
    parsed, body = frontmatter.parse(text)
    assert parsed["type"] == "problem"
    assert parsed["title"] == "Efficiency"
    assert parsed["sources"] == ["ABC", "DEF"]
    assert parsed["derived_from_thoughts"] == []
    assert "hello" in body


def test_no_frontmatter():
    meta, body = frontmatter.parse("just text")
    assert meta == {}
    assert body == "just text"
