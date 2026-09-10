import httpx
import respx

from quercus_mcp.canvas.client import CanvasClient
from quercus_mcp.canvas.models import flatten_discussion_view

BASE = "https://q.utoronto.ca"


async def noop(_):
    pass


def client():
    return CanvasClient(BASE, "tok", sleep=noop)


@respx.mock
async def test_list_courses_parses_term_and_enrollment():
    respx.get(url__startswith=f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "name": "Intro to CS", "course_code": "CSC108", "term": {"name": "Fall 2026"},
             "enrollments": [{"type": "student", "enrollment_state": "active"}], "syllabus_body": "<p>hi</p>"},
            {"id": 2, "access_restricted_by_date": True},
        ])
    )
    async with client() as c:
        courses = await c.list_courses()
    assert len(courses) == 1
    co = courses[0]
    assert (co.id, co.code, co.term, co.enrollment_state, co.syllabus_body) == (1, "CSC108", "Fall 2026", "active", "<p>hi</p>")


@respx.mock
async def test_list_files_returns_none_when_tab_hidden():
    respx.get(url__startswith=f"{BASE}/api/v1/courses/1/files").mock(
        return_value=httpx.Response(401, json={"status": "unauthorized", "errors": [{"message": "user not authorized"}]})
    )
    async with client() as c:
        assert await c.list_files(1) is None


@respx.mock
async def test_list_files_parses_file():
    respx.get(url__startswith=f"{BASE}/api/v1/courses/1/files").mock(
        return_value=httpx.Response(200, json=[{
            "id": 9, "display_name": "Week 1.pdf", "filename": "week1.pdf", "content-type": "application/pdf",
            "size": 1234, "url": f"{BASE}/files/9/download?download_frd=1", "folder_id": 3,
            "updated_at": "2026-09-01T00:00:00Z", "modified_at": "2026-08-30T00:00:00Z", "locked_for_user": False,
        }])
    )
    async with client() as c:
        files = await c.list_files(1)
    f = files[0]
    assert f.content_type == "application/pdf" and f.folder_id == 3 and not f.locked_for_user
    assert f.version_key == "2026-08-30T00:00:00Z|1234"


@respx.mock
async def test_list_modules_fetches_items_when_omitted():
    respx.get(url__startswith=f"{BASE}/api/v1/courses/1/modules/7/items").mock(
        return_value=httpx.Response(200, json=[{"id": 70, "position": 1, "title": "Slides", "type": "File", "content_id": 9,
                                                 "content_details": {"locked_for_user": True}}])
    )
    respx.get(url__startswith=f"{BASE}/api/v1/courses/1/modules").mock(
        return_value=httpx.Response(200, json=[
            {"id": 5, "name": "Week 1", "position": 1, "items": [{"id": 50, "position": 1, "title": "Intro", "type": "Page", "page_url": "intro"}]},
            {"id": 7, "name": "Week 2", "position": 2},  # items omitted by Canvas
        ])
    )
    async with client() as c:
        mods = await c.list_modules(1)
    assert [m.name for m in mods] == ["Week 1", "Week 2"]
    assert mods[0].items[0].page_url == "intro" and mods[0].items[0].module_id == 5
    assert mods[1].items[0].content_id == 9 and mods[1].items[0].locked_for_user is True and mods[1].items[0].module_id == 7


@respx.mock
async def test_list_pages_fetches_body_if_missing():
    respx.get(url__startswith=f"{BASE}/api/v1/courses/1/pages/intro").mock(
        return_value=httpx.Response(200, json={"url": "intro", "title": "Intro", "body": "<p>full</p>"})
    )
    respx.get(url__startswith=f"{BASE}/api/v1/courses/1/pages").mock(
        return_value=httpx.Response(200, json=[{"url": "intro", "title": "Intro", "updated_at": "x"}])
    )
    async with client() as c:
        pages = await c.list_pages(1)
    assert pages[0].body == "<p>full</p>"


@respx.mock
async def test_assignments_submission_state():
    respx.get(url__startswith=f"{BASE}/api/v1/courses/1/assignments").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "name": "A1", "due_at": "2026-10-01T04:00:00Z", "points_possible": 10,
             "submission": {"workflow_state": "graded", "score": 8.5}},
            {"id": 2, "name": "A2", "submission": {"workflow_state": "unsubmitted"}},
        ])
    )
    async with client() as c:
        a = await c.list_assignments(1)
    assert a[0].submitted and a[0].graded and a[0].score == 8.5
    assert not a[1].submitted and a[1].due_at is None


@respx.mock
async def test_announcements_uses_context_codes():
    route = respx.get(url__startswith=f"{BASE}/api/v1/announcements").mock(
        return_value=httpx.Response(200, json=[{"id": 3, "title": "Welcome", "message": "<p>hello</p>", "posted_at": "2026-09-01T00:00:00Z",
                                                 "author": {"display_name": "Prof X"}}])
    )
    async with client() as c:
        anns = await c.list_announcements(1, start_date="2026-01-01")
    assert anns[0].is_announcement and anns[0].author == "Prof X"
    assert "context_codes%5B%5D=course_1" in str(route.calls.last.request.url)


def test_flatten_discussion_view():
    view = {
        "participants": [{"id": 1, "display_name": "Ann"}, {"id": 2, "display_name": "Bob"}],
        "view": [
            {"id": 10, "user_id": 1, "message": "Q?", "created_at": "t1",
             "replies": [{"id": 11, "user_id": 2, "message": "A.", "created_at": "t2"}, {"id": 12, "deleted": True}]},
        ],
    }
    flat = flatten_discussion_view(view)
    assert [(e["author"], e["depth"]) for e in flat] == [("Ann", 0), ("Bob", 1)]


@respx.mock
async def test_announcements_sends_end_date():
    route = respx.get(url__startswith=f"{BASE}/api/v1/announcements").mock(return_value=httpx.Response(200, json=[]))
    async with client() as c:
        await c.list_announcements(1, start_date="2026-09-01")
    url = str(route.calls.last.request.url)
    assert "start_date=2026-09-01" in url and "end_date=20" in url


@respx.mock
async def test_401_on_pages_is_auth_error_but_403_is_denied():
    from quercus_mcp.canvas.errors import AuthError

    respx.get(url__startswith=f"{BASE}/api/v1/courses/1/pages").mock(return_value=httpx.Response(401, json={"status": "unauthenticated"}))
    respx.get(url__startswith=f"{BASE}/api/v1/courses/2/pages").mock(return_value=httpx.Response(403, json={"status": "unauthorized"}))
    respx.get(url__startswith=f"{BASE}/api/v1/courses/2/modules").mock(return_value=httpx.Response(404, json={}))
    async with client() as c:
        import pytest
        with pytest.raises(AuthError):
            await c.list_pages(1)
        assert await c.list_pages(2) is None
        assert await c.list_modules(2) is None
