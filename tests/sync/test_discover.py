from quercus_mcp.canvas.models import ModuleItem
from quercus_mcp.sync.discover import collect_file_ids


def mi(type_, content_id):
    return ModuleItem(id=1, module_id=1, position=1, title="t", type=type_, content_id=content_id, page_url=None, external_url=None, html_url=None)


def test_union_and_dedupe():
    ids = collect_file_ids(
        listing_ids=[1, 2],
        module_items=[mi("File", 2), mi("File", 3), mi("Page", 99), mi("File", None)],
        html_bodies=['<a href="/courses/5/files/4/download">x</a>', None, '<a href="/files/1">y</a>'],
    )
    assert ids == {1, 2, 3, 4}
