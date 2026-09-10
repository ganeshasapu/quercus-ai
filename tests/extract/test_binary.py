from pathlib import Path

from quercus_mcp.extract import MAX_TEXT_CHARS, extract_text

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def test_pdf_pages_marked():
    r = extract_text(FIX / "sample.pdf", "application/pdf")
    assert r.status == "ok"
    assert "--- page 1 ---" in r.text and "--- page 2 ---" in r.text
    assert "Photosynthesis converts light energy." in r.text
    assert "chlorophyll" in r.text


def test_pptx_title_body_notes():
    r = extract_text(FIX / "sample.pptx", None)  # extension-based detection
    assert r.status == "ok"
    assert "--- slide 1: Binary Search Trees ---" in r.text
    assert "Average lookup is O(log n)." in r.text
    assert "[notes] Mention balancing." in r.text


def test_docx_headings_and_tables():
    r = extract_text(FIX / "sample.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert r.status == "ok"
    assert "# Essay Guidelines" in r.text
    assert "Submit as PDF by Friday." in r.text
    assert "Criterion | Weight" in r.text


def test_plain_text_and_code(tmp_path):
    p = tmp_path / "notes.py"
    p.write_text("print('hi')\n")
    assert extract_text(p, "text/x-python").text == "print('hi')\n"


def test_html_file(tmp_path):
    p = tmp_path / "x.html"
    p.write_text("<h1>T</h1><p>body</p>")
    assert extract_text(p, "text/html").text == "# T\n\nbody"


def test_unsupported():
    r = extract_text(Path("lecture.mp4"), "video/mp4")
    assert r.status == "unsupported" and r.text == ""


def test_corrupt_pdf_is_error_not_exception(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"not a pdf")
    r = extract_text(p, "application/pdf")
    assert r.status == "error" and r.error


def test_truncation(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("a" * (MAX_TEXT_CHARS + 10))
    r = extract_text(p, "text/plain")
    assert r.text.endswith("[truncated]") and len(r.text) < MAX_TEXT_CHARS + 50
