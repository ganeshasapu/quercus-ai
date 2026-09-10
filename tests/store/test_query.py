from quercus_mcp.store.query import fts_query


def test_quotes_each_token():
    assert fts_query("C++ pointers") == '"C++" "pointers"'


def test_or_mode():
    assert fts_query("a b", mode="or") == '"a" OR "b"'


def test_preserves_user_phrases_and_escapes_quotes():
    assert fts_query('"binary tree" x"y') == '"binary tree" "x""y"'


def test_empty():
    assert fts_query("   ") == ""
