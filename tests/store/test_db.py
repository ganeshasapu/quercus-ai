from datetime import datetime, timezone

import pytest

from quercus_mcp.store import CourseRow, DocumentRow, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_course(CourseRow(id=1, name="Intro CS", code="CSC108", term="Fall 2026"))
    s.upsert_course(CourseRow(id=2, name="Bio", code="BIO120", term="Fall 2026"))
    yield s
    s.close()


def doc(**kw):
    base = dict(course_id=1, kind="file", canvas_id="9", title="Week 1 slides", version_key="v1")
    base.update(kw)
    return DocumentRow(**base)


def test_upsert_new_then_unchanged_then_changed(store):
    doc_id, changed = store.upsert_document(doc())
    assert changed is True
    store.set_document_text(doc_id, body="hello world", status="ok")
    same_id, changed = store.upsert_document(doc())
    assert same_id == doc_id and changed is False
    assert store.get_document(doc_id).body == "hello world"  # body preserved
    _, changed = store.upsert_document(doc(version_key="v2"))
    assert changed is True


def test_non_file_kinds_update_body_on_change(store):
    doc_id, _ = store.upsert_document(doc(kind="page", canvas_id="intro", body="old", extract_status="ok"))
    store.upsert_document(doc(kind="page", canvas_id="intro", body="new", version_key="v2", extract_status="ok"))
    assert store.get_document(doc_id).body == "new"


def test_search_ranks_and_snippets(store):
    a, _ = store.upsert_document(doc(canvas_id="1", title="Photosynthesis lecture", body="Chlorophyll absorbs light. " * 5, extract_status="ok"))
    b, _ = store.upsert_document(doc(canvas_id="2", title="Unrelated", body="Sorting algorithms and heaps.", extract_status="ok"))
    hits = store.search("chlorophyll")
    assert [h.doc_id for h in hits] == [a]
    assert "[Chlorophyll]" in hits[0].snippet
    assert hits[0].course_code == "CSC108"


def test_search_and_then_or_fallback(store):
    a, _ = store.upsert_document(doc(canvas_id="1", title="Heaps", body="binary heap operations", extract_status="ok"))
    b, _ = store.upsert_document(doc(canvas_id="2", title="Trees", body="red black tree rotations", extract_status="ok"))
    # AND matches nothing (no doc has both), OR fallback returns both
    ids = {h.doc_id for h in store.search("heap rotations")}
    assert ids == {a, b}


def test_search_filters_course_kind_and_removed(store):
    a, _ = store.upsert_document(doc(course_id=1, canvas_id="1", body="entropy", extract_status="ok"))
    b, _ = store.upsert_document(doc(course_id=2, kind="page", canvas_id="p", body="entropy", extract_status="ok"))
    assert {h.doc_id for h in store.search("entropy")} == {a, b}
    assert [h.doc_id for h in store.search("entropy", course_id=2)] == [b]
    assert [h.doc_id for h in store.search("entropy", kind="page")] == [b]
    store.mark_removed(1, "file", keep_canvas_ids=set())
    assert [h.doc_id for h in store.search("entropy")] == [b]


def test_search_bad_syntax_does_not_raise(store):
    assert store.search('"unterminated OR (') == []


def test_mark_removed_keeps_listed(store):
    store.upsert_document(doc(canvas_id="1"))
    store.upsert_document(doc(canvas_id="2"))
    n = store.mark_removed(1, "file", keep_canvas_ids={"1"})
    assert n == 1
    assert [d.canvas_id for d in store.list_documents(1, "file")] == ["1"]
    # re-upserting a removed doc revives it
    _, changed = store.upsert_document(doc(canvas_id="2"))
    assert changed is True and len(store.list_documents(1, "file")) == 2


def test_upcoming_window(store):
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    store.upsert_document(doc(kind="assignment", canvas_id="a1", title="A1", due_at="2026-09-10T04:00:00Z"))
    store.upsert_document(doc(kind="assignment", canvas_id="a2", title="A2", due_at="2026-10-30T04:00:00Z"))
    store.upsert_document(doc(kind="assignment", canvas_id="a0", title="Old", due_at="2026-09-01T04:00:00Z"))
    assert [d.title for d in store.upcoming(days=14, now=now)] == ["A1"]
    assert [d.title for d in store.upcoming(days=60, now=now)] == ["A1", "A2"]


def test_announcements_order_and_since(store):
    store.upsert_document(doc(kind="announcement", canvas_id="1", title="Old", posted_at="2026-08-01T00:00:00Z", body="x"))
    store.upsert_document(doc(kind="announcement", canvas_id="2", title="New", posted_at="2026-09-05T00:00:00Z", body="y"))
    assert [d.title for d in store.announcements()] == ["New", "Old"]
    assert [d.title for d in store.announcements(since=datetime(2026, 9, 1, tzinfo=timezone.utc))] == ["New"]
    assert store.announcements()[0].body == "y"


def test_counts_and_courses(store):
    store.upsert_document(doc(canvas_id="1"))
    store.upsert_document(doc(kind="page", canvas_id="p"))
    assert store.counts_by_kind(1) == {"file": 1, "page": 1}
    assert store.deactivate_courses_not_in({1}) == 1
    assert [c.id for c in store.courses()] == [1]
    assert len(store.courses(include_inactive=True)) == 2


def test_sync_runs_and_meta(store):
    rid = store.start_sync_run("all")
    store.finish_sync_run(rid, status="ok", added=3, updated=1, removed=0, errors=[])
    runs = store.sync_runs()
    assert runs[0].status == "ok" and runs[0].added == 3
    assert store.last_successful_sync() == runs[0].finished_at
    store.meta_set("k", "v")
    assert store.meta_get("k") == "v"
    store.meta_set("k", None)
    assert store.meta_get("k") is None
