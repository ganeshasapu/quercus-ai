from __future__ import annotations

from pathlib import Path


def extract_pdf(path: Path) -> str:
    import pymupdf

    parts: list[str] = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, 1):
            text = page.get_text("text").strip()
            parts.append(f"--- page {i} ---\n{text}")
    return "\n\n".join(parts)
