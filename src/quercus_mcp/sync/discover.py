"""Union of every way a course file can be reached."""

from __future__ import annotations

from collections.abc import Iterable

from quercus_mcp.canvas.models import ModuleItem
from quercus_mcp.extract.html import find_file_ids


def collect_file_ids(
    listing_ids: Iterable[int],
    module_items: Iterable[ModuleItem],
    html_bodies: Iterable[str | None],
) -> set[int]:
    ids: set[int] = set(listing_ids)
    for item in module_items:
        if item.type == "File" and item.content_id is not None:
            ids.add(item.content_id)
    for body in html_bodies:
        ids |= find_file_ids(body)
    return ids
