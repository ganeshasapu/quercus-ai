"""HTML → Markdown conversion and Canvas file-link discovery."""

from __future__ import annotations

import re
from collections.abc import Callable

from markdownify import MarkdownConverter

# Matches /courses/123/files/456 and /files/456 (optionally followed by /download, ?..., etc.)
_FILE_LINK_RE = re.compile(r"/files/(\d+)(?=[/?#\"'\s)]|$)")
_WS_RE = re.compile(r"\n{3,}")


class _Converter(MarkdownConverter):
    def convert_script(self, el, text, parent_tags):  # type: ignore[override]
        return ""

    def convert_style(self, el, text, parent_tags):  # type: ignore[override]
        return ""

    def convert_img(self, el, text, parent_tags):  # type: ignore[override]
        alt = el.attrs.get("alt") or ""
        return f"[image: {alt}]" if alt else ""

    def convert_iframe(self, el, text, parent_tags):  # type: ignore[override]
        src = el.attrs.get("src")
        return f"[embedded: {src}]\n" if src else ""


def find_file_ids(html: str | None) -> set[int]:
    if not html:
        return set()
    return {int(m) for m in _FILE_LINK_RE.findall(html)}


def html_to_markdown(html: str | None, link_rewriter: Callable[[str], str] | None = None) -> str:
    if not html:
        return ""
    conv = _Converter(heading_style="ATX", bullets="-")
    if link_rewriter is not None:
        original = conv.convert_a

        def convert_a(el, text, parent_tags):  # type: ignore[no-untyped-def]
            href = el.attrs.get("href")
            if href:
                el.attrs["href"] = link_rewriter(href)
            return original(el, text, parent_tags)

        conv.convert_a = convert_a  # type: ignore[method-assign]
    md = conv.convert(html)
    md = _WS_RE.sub("\n\n", md)
    return md.strip()
