"""Turn downloaded course files into plain text / Markdown."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from quercus_mcp.extract.html import html_to_markdown

MAX_TEXT_CHARS = 2_000_000

Status = Literal["ok", "unsupported", "error"]

_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".java", ".c", ".h", ".cpp", ".hpp", ".cc", ".js", ".ts", ".tsx", ".jsx", ".rb", ".go", ".rs",
    ".sql", ".sh", ".r", ".m", ".tex", ".bib", ".log", ".xml", ".ipynb",
}
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIMES = {"application/json", "application/xml", "application/x-tex", "application/x-latex", "application/javascript"}


@dataclass
class ExtractResult:
    status: Status
    text: str = ""
    error: str | None = None


def _kind(path: Path, content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    ext = path.suffix.lower()
    if ct == "application/pdf" or ext == ".pdf":
        return "pdf"
    if ct == "application/vnd.openxmlformats-officedocument.presentationml.presentation" or ext == ".pptx":
        return "pptx"
    if ct == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or ext == ".docx":
        return "docx"
    if ct in ("text/html", "application/xhtml+xml") or ext in (".html", ".htm"):
        return "html"
    if ct.startswith(_TEXT_MIME_PREFIXES) or ct in _TEXT_MIMES or ext in _TEXT_EXTS:
        return "text"
    return "unsupported"


def extract_text(path: Path, content_type: str | None = None) -> ExtractResult:
    kind = _kind(path, content_type)
    if kind == "unsupported":
        return ExtractResult("unsupported")
    try:
        if kind == "pdf":
            from quercus_mcp.extract.pdf import extract_pdf
            text = extract_pdf(path)
        elif kind == "pptx":
            from quercus_mcp.extract.pptx import extract_pptx
            text = extract_pptx(path)
        elif kind == "docx":
            from quercus_mcp.extract.docx import extract_docx
            text = extract_docx(path)
        elif kind == "html":
            text = html_to_markdown(path.read_text(errors="replace"))
        else:
            text = path.read_text(errors="replace")
    except Exception as exc:  # extraction must never abort a sync
        return ExtractResult("error", error=f"{type(exc).__name__}: {exc}"[:500])
    text = text.replace("\x00", "")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n\n[truncated]"
    return ExtractResult("ok", text=text)
