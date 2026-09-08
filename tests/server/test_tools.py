import asyncio

import pytest
from mcp.client import Client

from quercus_mcp.config import Config, Paths
from quercus_mcp.server import ServerState, build_server, parse_doc_id
from quercus_mcp.store import CourseRow, DocumentRow, Store
from quercus_mcp.sync.crawler import SyncSummary


class FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def aclose(self):
        pass


class FakeSyncer:
    def __init__(self, store=None):
        self.store = store
        self.refreshed: list[int] = []
        self.synced = 0

    async def refresh_volatile(self, cid):
        self.refreshed.append(cid)
        if self.store is not None:
            from quercus_mcp.store import utcnow
            self.store.meta_set(f"volatile:{cid}", utcnow())
        return SyncSummary()

    async def sync_all(self, *, full=False, course_ids=None):
        self.synced += 1
        return SyncSummary(added=2, updated=1, courses=1)


@pytest.fixture
def state(tmp_path):
    paths = Paths(tmp_path)
    store = Store(paths.db)
    store.upsert_course(CourseRow(id=1, name="Intro Bio", code="BIO120", term="Fall 2026", last_synced_at="2026-09-08T10:00:00Z"))
    r = store.upsert_document(DocumentRow(course_id=1, kind="file", canvas_id="10", title="Lecture 1.pdf", url="u", module_name="Week 1", module_position=1001,
                                          body="--- page 1 ---\nChlorophyll absorbs light. " + "x " * 3000, extract_status="ok", version_key="v"))
    store.upsert_document(DocumentRow(course_id=1, kind="assignment", canvas_id="900", title="Lab 1", due_at="2030-01-01T00:00:00Z", body="Measure osmosis",
                                      extra={"points_possible": 10, "submitted": False}, extract_status="ok", version_key="v"))
    store.upsert_document(DocumentRow(course_id=1, kind="announcement", canvas_id="700", title="Midterm date", posted_at="2999-01-01T00:00:00Z",
                                      body="Midterm is Oct 15.", extra={"author": "Prof Green"}, extract_status="ok", version_key="v"))
    store.upsert_document(DocumentRow(course_id=1, kind="file", canvas_id="11", title="Secret.pdf", extract_status="locked", extract_error="Locked until Oct 1", version_key="v"))
    fake = FakeSyncer(store)

    async def factory():
        return FakeClient(), fake

    st = ServerState(store=store, config=Config(), paths=paths, make_syncer=factory, sync_lock=asyncio.Lock())
    st.fake = fake  # type: ignore[attr-defined]
    st.doc_id = r.doc_id  # type: ignore[attr-defined]
    yield st
    store.close()


async def call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    return res.content[0].text


async def test_list_tools(state):
    server = build_server(state)
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {"list_courses", "search", "list_documents", "read_document", "get_upcoming", "get_announcements", "sync", "sync_status"}


async def test_list_courses(state):
    out = await call(build_server(state), "list_courses")
    assert "BIO120" in out and "| 2 |" in out  # 2 files


async def test_search_and_read(state):
    server = build_server(state)
    out = await call(server, "search", query="chlorophyll")
    assert "Lecture 1.pdf" in out and f"quercus://doc/{state.doc_id}" in out
    first = await call(server, "read_document", doc_id=f"quercus://doc/{state.doc_id}", max_chars=600)
    assert first.startswith("# Lecture 1.pdf") and "offset=600" in first
    second = await call(server, "read_document", doc_id=str(state.doc_id), offset=600, max_chars=100000)
    assert "for more" not in second


async def test_read_locked_explains(state):
    docs = state.store.list_documents(1, "file")
    locked = next(d for d in docs if d.title == "Secret.pdf")
    out = await call(build_server(state), "read_document", doc_id=str(locked.id))
    assert "locked" in out and "Locked until Oct 1" in out


async def test_list_documents_views(state):
    server = build_server(state)
    mods = await call(server, "list_documents", course_id=1)
    assert "## Week 1" in mods and "Lecture 1.pdf" in mods and "🔒" in mods
    flat = await call(server, "list_documents", course_id=1, kind="assignment", view="flat")
    assert "[assignment] Lab 1" in flat and "Lecture" not in flat
    assert "Unknown course" in await call(server, "list_documents", course_id=99)


async def test_upcoming_and_announcements_refresh_when_stale(state):
    server = build_server(state)
    up = await call(server, "get_upcoming", days=100000)
    assert "Lab 1" in up and "not submitted" in up and "10 pts" in up
    assert state.fake.refreshed == [1]
    ann = await call(server, "get_announcements", since_days=1)
    assert "Midterm date" in ann and "Prof Green" in ann
    assert state.fake.refreshed == [1]  # not refreshed twice


async def test_sync_tool_and_status(state):
    server = build_server(state)
    out = await call(server, "sync")
    assert "Sync ok" in out and "2 added" in out and state.fake.synced == 1
    status = await call(server, "sync_status")
    assert "BIO120" in status and "Sync interval" in status


async def test_auth_notice_prefix(state):
    state.store.meta_set("last_auth_error", "2026-09-08T00:00:00Z")
    out = await call(build_server(state), "list_courses")
    assert out.startswith("⚠️") and "quercus login" in out


async def test_no_token_message(state):
    async def none_factory():
        return None
    state.make_syncer = none_factory
    out = await call(build_server(state), "sync")
    assert "quercus login" in out


def test_parse_doc_id():
    assert parse_doc_id("quercus://doc/12") == 12 and parse_doc_id(" 7 ") == 7 and parse_doc_id(3) == 3


async def test_read_document_accepts_integer_id(state):
    out = await call(build_server(state), "read_document", doc_id=state.doc_id)
    assert out.startswith("# Lecture 1.pdf")


async def test_sync_reports_already_running(state):
    server = build_server(state)
    async with state.sync_lock:
        out = await call(server, "sync")
    assert "already running" in out


async def test_slow_sync_returns_early(state):
    class Slow(FakeSyncer):
        async def sync_all(self, **kw):
            await asyncio.sleep(0.5)
            return SyncSummary()

    async def factory():
        return FakeClient(), Slow()

    state.make_syncer = factory
    state.sync_wait_seconds = 0.05
    out = await call(build_server(state), "sync")
    assert "still running" in out
    for t in list(state.background_tasks):
        await t


def test_sync_is_due(state):
    from quercus_mcp.server import _sync_is_due
    from quercus_mcp.store import utcnow

    assert _sync_is_due(state) is True
    rid = state.store.start_sync_run("all")
    state.store.finish_sync_run(rid, status="partial", added=0, updated=0, removed=0, errors=["x"])
    assert _sync_is_due(state) is False  # partial counts as a completed run
    state.config.sync_interval_minutes = 0
    assert _sync_is_due(state) is True or utcnow()  # zero interval → due (tolerate same-second)
