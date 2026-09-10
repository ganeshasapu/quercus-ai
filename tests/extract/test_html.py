from quercus_mcp.extract.html import find_file_ids, html_to_markdown


def test_find_file_ids_both_forms():
    html = ('<a href="https://q.utoronto.ca/courses/12/files/345/download?wrap=1">a</a>'
            '<a href="/files/678?verifier=abc">b</a> <img src="/courses/12/files/345/preview">'
            '<a href="/courses/12/pages/files-overview">not a file</a>')
    assert find_file_ids(html) == {345, 678}
    assert find_file_ids(None) == set()


def test_html_to_markdown_basic():
    md = html_to_markdown("<h2>Week 1</h2><p>Read <b>chapter</b> 1.</p><ul><li>a</li><li>b</li></ul><script>x()</script>")
    assert md.startswith("## Week 1")
    assert "Read **chapter** 1." in md
    assert "- a\n- b" in md
    assert "x()" not in md


def test_html_to_markdown_rewrites_links():
    md = html_to_markdown('<a href="/courses/1/files/9/download">Slides</a>', link_rewriter=lambda h: "quercus://doc/42" if "/files/9" in h else h)
    assert "[Slides](quercus://doc/42)" in md


def test_images_and_iframes_become_placeholders():
    md = html_to_markdown('<p><img src="x.png" alt="diagram"><iframe src="https://play.library.utoronto.ca/v"></iframe></p>')
    assert "[image: diagram]" in md and "[embedded: https://play.library.utoronto.ca/v]" in md


def test_empty():
    assert html_to_markdown(None) == "" and html_to_markdown("") == ""
