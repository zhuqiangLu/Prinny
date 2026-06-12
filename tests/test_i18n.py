"""i18n foundation + CJK-safety fixes (Phase 1 of multi-language support)."""
from app import i18n, wiki, discover, db


# --- UI string translation ---------------------------------------------------
def test_t_translates_with_english_fallback():
    assert i18n.t("Collections", lang="zh") == "合集"        # in the catalog
    assert i18n.t("Collections", lang="en") == "Collections"  # English is identity
    assert i18n.t("Not in catalog yet", lang="zh") == "Not in catalog yet"  # graceful fallback


def test_output_directive_only_for_nonenglish():
    assert i18n.output_directive("en") == ""                  # default → no directive
    d = i18n.output_directive("zh")
    assert "中文" in d and "JSON" in d                        # writes prose in Chinese, keeps JSON


# --- CJK-safety: concept synonym matching (\b breaks on CJK) ------------------
def test_synonym_regex_matches_chinese_substring():
    pat = wiki._synonym_regex(["KV缓存"])
    assert pat.search("使用KV缓存技术压缩")                    # would FAIL with \b…\b boundaries
    none = wiki._synonym_regex(["完全不同"])
    assert not none.search("使用KV缓存技术压缩")


def test_synonym_regex_keeps_english_word_boundaries():
    pat = wiki._synonym_regex(["gap"])
    assert pat.search("there is a gap here")
    assert not pat.search("agape feeling")                    # \b still guards ASCII terms


# --- CJK-safety: preference tokenizer ([a-z0-9] extracts nothing) -------------
def test_tokens_extracts_cjk_bigrams():
    toks = discover._tokens("深度学习模型")
    assert "深度" in toks and "学习" in toks                  # Han bigrams captured
    assert "machine" in discover._tokens("machine vision")    # English still works


# --- CJK-safety: FTS5 trigram tokenizer --------------------------------------
def _seed_note(con, summary):
    con.execute("INSERT INTO collections(slug,name) VALUES('c','C')")
    con.execute("INSERT INTO papers(id,title) VALUES(1,'T')")
    con.execute("INSERT INTO paper_notes(paper_id,collection_slug,summary,thoughts,key_quotes,status)"
                " VALUES(1,'c',?,'','','noted')", (summary,))
    con.commit()


def test_fts_trigram_matches_chinese(tmp_path):
    p = tmp_path / "app.sqlite"
    db.init_db(p)
    con = db.connect(p)
    _seed_note(con, "深度学习的注意力机制研究")
    rows = con.execute("SELECT paper_id FROM notes_fts WHERE notes_fts MATCH ?", ("注意力",)).fetchall()
    con.close()
    assert rows and rows[0][0] == 1            # Chinese substring search hits (trigram)


def test_fts_migration_upgrades_to_trigram(tmp_path):
    p = tmp_path / "app.sqlite"
    db.init_db(p)
    con = db.connect(p)
    # Simulate a pre-upgrade index built with the default tokenizer.
    con.execute("DROP TABLE notes_fts")
    con.execute("CREATE VIRTUAL TABLE notes_fts USING fts5(paper_id, collection_slug, summary, "
                "thoughts, key_quotes, content='paper_notes', content_rowid='rowid')")
    con.commit()
    db._migrate(con)
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='notes_fts'").fetchone()[0]
    con.close()
    assert "trigram" in sql.lower()            # migration rebuilt it CJK-safe
