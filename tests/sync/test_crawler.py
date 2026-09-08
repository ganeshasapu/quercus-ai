import pytest
import respx

from quercus_mcp.canvas.client import CanvasClient
from quercus_mcp.canvas.errors import AuthError
from quercus_mcp.config import Config, Paths
from quercus_mcp.store import Store
from quercus_mcp.sync.crawler import Syncer
from tests.sync import mock_canvas as mc


async def noop(_):
    pass


@pytest.fixture
def env(tmp_path):
    paths = Paths(tmp_path)
    store = Store(paths.db)
    config = Config(base_url=mc.BASE, max_file_mb=50)
    yield paths, store, config
    store.close()


def make_syncer(env, **kw):
    paths, store, config = env
    client = CanvasClient(mc.BASE, "tok", sleep=noop)
    return client, Syncer(client, store, config, paths, **kw)


@respx.mock
async def test_full_course_sync(env):
    paths, store, _ = env
    routes = mc.mount()
    client, syncer = make_syncer(env)
    async with client:
        summary = await syncer.sync_all()

    assert summary.status == "ok" and summary.errors == []
    assert summary.courses == 1
    course = store.get_course(101)
    assert course.code == "BIO120" and course.files_tab_hidden is False

    kinds = store.counts_by_kind(101)
    assert kinds == {"file": 4, "page": 1, "syllabus": 1, "module": 1, "assignment": 1, "announcement": 1, "discussion": 1}

    files = {d.canvas_id: d for d in store.list_documents(101, "file")}
    assert files["10"].extract_status == "ok" and files["10"].folder_path == ""
    assert files["11"].extract_status == "locked"
    assert files["12"].extract_status == "skipped_size" and files["12"].folder_path == "Videos"
    assert files["30"].extract_status == "ok" and files["30"].module_name == "Week 1"  # discovered via module + syllabus link
    assert summary.downloaded == 2
    assert routes["s3"].call_count == 2

    # extracted PDF text is searchable and written to disk
    hits = store.search("chlorophyll")
    assert {h.kind for h in hits} == {"file"} and len(hits) == 2
    doc10 = store.get_document(files["10"].id)
    assert "--- page 1 ---" in doc10.body
    assert (paths.files_dir / "BIO120" / "lecture1.pdf").exists()
    assert doc10.text_path and doc10.text_path.endswith("Lecture-1--10.md")
    assert (paths.text_dir / "BIO120" / "file" / "Lecture-1--10.md").read_text().startswith("---\ntitle: \"Lecture 1.pdf\"")

    # page links to files are rewritten to doc URIs; page is attached to its module
    page = store.get_document(store.list_documents(101, "page")[0].id)
    assert f"quercus://doc/{files['10'].id}" in page.body
    assert page.module_name == "Week 1" and "**BIO120**" in page.body

    module = store.get_document(store.list_documents(101, "module")[0].id)
    assert "[Page] Welcome" in module.body and f"[File] Outline (quercus://doc/{files['30'].id})" in module.body
    assert "<https://example.org/read>" in module.body

    a = store.list_documents(101, "assignment")[0]
    assert a.due_at == "2026-09-20T03:59:00Z" and a.extra["points_possible"] == 10 and a.extra["submitted"] is False

    ann = store.announcements()[0]
    assert ann.title == "Midterm date" and ann.extra["author"] == "Prof Green" and "Oct 15" in ann.body

    disc = store.get_document(store.list_documents(101, "discussion")[0].id)
    assert "Ask here." in disc.body and "**Sam**" in disc.body and "Is the lab graded?" in disc.body

    syl = store.get_document(store.list_documents(101, "syllabus")[0].id)
    assert syl.body.startswith("## Syllabus")

    runs = store.sync_runs()
    assert runs[0].status == "ok" and runs[0].added == 10


@respx.mock
async def test_second_run_is_incremental(env):
    routes = mc.mount()
    client, syncer = make_syncer(env)
    async with client:
        await syncer.sync_all()
        s3_before = routes["s3"].call_count
        view_before = routes["discussion_view"].call_count
        summary = await syncer.sync_all()
    assert routes["s3"].call_count == s3_before  # no re-downloads
    assert routes["discussion_view"].call_count == view_before  # unchanged thread not re-fetched
    assert summary.added == 0 and summary.updated == 0 and summary.downloaded == 0


@respx.mock
async def test_changed_file_is_redownloaded(env):
    paths, store, _ = env
    routes = mc.mount()
    client, syncer = make_syncer(env)
    async with client:
        await syncer.sync_all()
        newer = {**mc.FILE_10, "modified_at": "2026-09-09T00:00:00Z"}
        routes["files"].mock(return_value=mc._json([newer, mc.FILE_11, mc.FILE_12]))
        before = routes["s3"].call_count
        summary = await syncer.sync_all()
    assert routes["s3"].call_count == before + 1
    assert summary.updated == 1


@respx.mock
async def test_hidden_files_tab_still_finds_module_files(env):
    paths, store, _ = env
    mc.mount(files_tab_hidden=True)
    client, syncer = make_syncer(env)
    async with client:
        summary = await syncer.sync_all()
    assert summary.status == "ok"
    assert store.get_course(101).files_tab_hidden is True
    files = {d.canvas_id: d for d in store.list_documents(101, "file")}
    assert set(files) == {"10", "30"}  # 10 via page link, 30 via module + syllabus
    assert files["30"].extract_status == "ok"


@respx.mock
async def test_removed_documents_are_soft_deleted(env):
    paths, store, _ = env
    routes = mc.mount()
    client, syncer = make_syncer(env)
    async with client:
        await syncer.sync_all()
        routes["pages"].mock(return_value=mc._json([]))
        summary = await syncer.sync_all()
    assert summary.removed == 1
    assert store.list_documents(101, "page") == []
    assert store.search("Welcome", kind="page") == []


@respx.mock
async def test_auth_error_marks_run_failed(env):
    paths, store, _ = env
    mc.mount(courses_status=401)
    client, syncer = make_syncer(env)
    async with client:
        with pytest.raises(AuthError):
            await syncer.sync_all()
    assert store.sync_runs()[0].status == "failed"
    assert store.meta_get("last_auth_error") is not None


@respx.mock
async def test_refresh_volatile_updates_assignments_and_meta(env):
    paths, store, _ = env
    routes = mc.mount()
    client, syncer = make_syncer(env)
    async with client:
        await syncer.sync_all()
        assert syncer.volatile_stale(101) is False  # a full sync counts as fresh
        store.meta_set("volatile:101", "2020-01-01T00:00:00Z")
        assert syncer.volatile_stale(101) is True
        routes["assignments"].mock(return_value=mc._json([{**mc.ASSIGNMENTS[0], "submission": {"workflow_state": "submitted"}}]))
        summary = await syncer.refresh_volatile(101)
    assert summary.updated == 1
    assert store.list_documents(101, "assignment")[0].extra["submitted"] is True
    assert syncer.volatile_stale(101) is False


@respx.mock
async def test_course_filters(env):
    paths, store, config = env
    config.exclude_courses = [101]
    mc.mount()
    client, syncer = make_syncer(env)
    async with client:
        summary = await syncer.sync_all()
    assert summary.courses == 0


@respx.mock
async def test_unlocked_file_gets_downloaded(env):
    paths, store, _ = env
    routes = mc.mount()
    routes["download11"] = respx.get(url__startswith=f"{mc.BASE}/files/11/download").mock(
        return_value=__import__("httpx").Response(302, headers={"Location": mc.S3 + "/11"}))
    client, syncer = make_syncer(env)
    async with client:
        await syncer.sync_all()
        assert {d.canvas_id: d.extract_status for d in store.list_documents(101, "file")}["11"] == "locked"
        unlocked = {**mc.FILE_11, "locked_for_user": False, "size": len(mc.PDF_BYTES)}
        routes["files"].mock(return_value=mc._json([mc.FILE_10, unlocked, mc.FILE_12]))
        before = routes["s3"].call_count
        await syncer.sync_all()
    assert routes["s3"].call_count == before + 1
    assert {d.canvas_id: d.extract_status for d in store.list_documents(101, "file")}["11"] == "ok"


@respx.mock
async def test_401_on_pages_mid_sync_aborts_without_deleting(env):
    import httpx

    paths, store, _ = env
    routes = mc.mount()
    client, syncer = make_syncer(env)
    async with client:
        await syncer.sync_all()
        routes["pages"].mock(return_value=httpx.Response(401, json={"status": "unauthenticated"}))
        with pytest.raises(AuthError):
            await syncer.sync_all()
    assert len(store.list_documents(101, "page")) == 1  # not soft-deleted
    assert store.meta_get("last_auth_error") is not None


@respx.mock
async def test_403_on_pages_skips_kind_without_deleting(env):
    import httpx

    paths, store, _ = env
    routes = mc.mount()
    client, syncer = make_syncer(env)
    async with client:
        await syncer.sync_all()
        routes["pages"].mock(return_value=httpx.Response(403, json={"status": "unauthorized"}))
        summary = await syncer.sync_all()
    assert summary.status == "ok" and summary.removed == 0
    assert len(store.list_documents(101, "page")) == 1


@respx.mock
async def test_download_failure_marks_one_doc_and_course_completes(env):
    import httpx

    paths, store, _ = env
    routes = mc.mount()
    routes["download30"].mock(return_value=httpx.Response(500, text="boom"))
    respx.get(url__startswith=f"{mc.BASE}/api/v1/files/30/public_url").mock(return_value=httpx.Response(500))
    client, syncer = make_syncer(env)
    async with client:
        summary = await syncer.sync_all()
    assert summary.status == "partial" and any("Outline.pdf" in e for e in summary.errors)
    files = {d.canvas_id: d for d in store.list_documents(101, "file")}
    assert files["30"].extract_status == "error" and files["10"].extract_status == "ok"


@respx.mock
async def test_full_redownloads_and_retry_errors(env):
    import httpx

    paths, store, _ = env
    routes = mc.mount()
    routes["download30"].mock(return_value=httpx.Response(500))
    respx.get(url__startswith=f"{mc.BASE}/api/v1/files/30/public_url").mock(return_value=httpx.Response(500))
    client, syncer = make_syncer(env)
    async with client:
        await syncer.sync_all()
        before = routes["s3"].call_count
        await syncer.sync_all(full=True)
        assert routes["s3"].call_count == before + 1  # file 10 re-downloaded
        # heal file 30 and retry errors
        routes["download30"].mock(return_value=httpx.Response(302, headers={"Location": mc.S3 + "/30"}))
        await syncer.sync_all()
        assert {d.canvas_id: d.extract_status for d in store.list_documents(101, "file")}["30"] == "error"  # not retried by default
        syncer.retry_errors = True
        await syncer.sync_all()
    assert {d.canvas_id: d.extract_status for d in store.list_documents(101, "file")}["30"] == "ok"


@respx.mock
async def test_second_run_does_not_rewrite_unchanged_bodies(env):
    paths, store, _ = env
    mc.mount()
    client, syncer = make_syncer(env)
    async with client:
        await syncer.sync_all()
        stamps = {d.id: d.indexed_at for d in store.list_documents(101)}
        await syncer.sync_all()
    assert {d.id: d.indexed_at for d in store.list_documents(101)} == stamps


@respx.mock
async def test_syllabus_links_are_rewritten_and_volatile_marked_fresh(env):
    paths, store, _ = env
    mc.mount()
    client, syncer = make_syncer(env)
    async with client:
        await syncer.sync_all()
    files = {d.canvas_id: d for d in store.list_documents(101, "file")}
    syl = store.get_document(store.list_documents(101, "syllabus")[0].id)
    assert f"quercus://doc/{files['30'].id}" in syl.body
    assert syncer.volatile_stale(101) is False


@respx.mock
async def test_hostile_filenames_stay_inside_files_dir(env):
    import httpx

    paths, store, _ = env
    routes = mc.mount()
    evil_file = {**mc.FILE_10, "filename": "../../evil.pdf", "display_name": "../../evil.pdf"}
    routes["files"].mock(return_value=mc._json([evil_file]))
    routes["folders"].mock(return_value=mc._json([{"id": 1, "full_name": "course files/../../escape", "parent_folder_id": None}]))
    client, syncer = make_syncer(env)
    async with client:
        await syncer.sync_all()
    d = {x.canvas_id: x for x in store.list_documents(101, "file")}["10"]
    local = __import__("pathlib").Path(d.local_path).resolve()
    assert paths.files_dir.resolve() in local.parents
    assert local.name == ".._.._evil.pdf" or local.name.endswith("evil.pdf")
    assert not (paths.home.parent / "evil.pdf").exists()
