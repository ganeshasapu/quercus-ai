"""Live test against real Quercus. Opt in with QUERCUS_INTEGRATION=1 and a stored token.

    QUERCUS_INTEGRATION=1 .venv/bin/pytest tests/integration -q -s
"""

import os

import pytest

from quercus_mcp.canvas.client import CanvasClient
from quercus_mcp.config import Config, Paths, get_token
from quercus_mcp.store import Store
from quercus_mcp.sync.crawler import Syncer

pytestmark = pytest.mark.skipif(os.environ.get("QUERCUS_INTEGRATION") != "1", reason="set QUERCUS_INTEGRATION=1 to run against real Quercus")


@pytest.fixture
def token():
    cfg = Config.load(Paths.default())
    tok = get_token(cfg.host)
    if not tok:
        pytest.skip("no token stored; run `quercus login`")
    return cfg, tok


async def test_token_and_courses(token):
    cfg, tok = token
    async with CanvasClient(cfg.base_url, tok) as c:
        me = await c.get_self()
        assert me.get("id")
        courses = await c.list_courses()
        print(f"\n{me.get('name')}: {len(courses)} active course(s): {[c.code for c in courses]}")
        assert isinstance(courses, list)


async def test_sync_one_course_into_temp_home(token, tmp_path):
    cfg, tok = token
    paths = Paths(tmp_path)
    async with CanvasClient(cfg.base_url, tok) as c:
        courses = await c.list_courses()
        if not courses:
            pytest.skip("no active courses")
        first = courses[0]
        with Store(paths.db) as store:
            syncer = Syncer(c, store, cfg, paths)
            summary = await syncer.sync_all(course_ids=[first.id])
            print(f"\n{first.code}: {summary}")
            counts = store.counts_by_kind(first.id)
            print(counts)
            assert summary.status in ("ok", "partial")
            files = store.list_documents(first.id, "file")
            statuses = {f.extract_status for f in files}
            print("file statuses:", statuses)
            # At least one file should have extracted text if the course has any supported files.
            if any(f.extract_status == "ok" for f in files):
                hits = store.search(files[0].title.split()[0])
                assert hits
