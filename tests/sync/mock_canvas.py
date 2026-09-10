"""A fully mocked Canvas course for crawler tests (respx routes)."""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

BASE = "https://q.utoronto.ca"
FIX = Path(__file__).resolve().parent.parent / "fixtures"
PDF_BYTES = (FIX / "sample.pdf").read_bytes()
S3 = "https://inst-fs.s3.amazonaws.com/blob"

COURSE = {
    "id": 101, "name": "Intro to Biology", "course_code": "BIO120", "term": {"name": "Fall 2026"},
    "enrollments": [{"type": "student", "enrollment_state": "active"}], "start_at": "2026-09-01T00:00:00Z",
    "syllabus_body": '<h2>Syllabus</h2><p>See <a href="/courses/101/files/30/download">course outline</a>.</p>',
}

FILE_10 = {"id": 10, "display_name": "Lecture 1.pdf", "filename": "lecture1.pdf", "content-type": "application/pdf", "size": len(PDF_BYTES),
           "folder_id": 1, "updated_at": "2026-09-02T00:00:00Z", "modified_at": "2026-09-02T00:00:00Z", "locked_for_user": False,
           "html_url": f"{BASE}/courses/101/files/10"}
FILE_11 = {"id": 11, "display_name": "Secret.pdf", "filename": "secret.pdf", "content-type": "application/pdf", "size": 100,
           "folder_id": 1, "updated_at": "2026-09-02T00:00:00Z", "locked_for_user": True, "lock_explanation": "Locked until Oct 1"}
FILE_12 = {"id": 12, "display_name": "Huge.mp4", "filename": "huge.mp4", "content-type": "video/mp4", "size": 900 * 1024 * 1024,
           "folder_id": 2, "updated_at": "2026-09-02T00:00:00Z", "locked_for_user": False}
FILE_30 = {"id": 30, "display_name": "Outline.pdf", "filename": "outline.pdf", "content-type": "application/pdf", "size": len(PDF_BYTES),
           "folder_id": 1, "updated_at": "2026-08-20T00:00:00Z", "locked_for_user": False}

FOLDERS = [{"id": 1, "full_name": "course files", "parent_folder_id": None}, {"id": 2, "full_name": "course files/Videos", "parent_folder_id": 1}]

MODULES = [
    {"id": 5, "name": "Week 1", "position": 1, "state": "completed", "items": [
        {"id": 50, "position": 1, "title": "Welcome", "type": "Page", "page_url": "welcome", "html_url": f"{BASE}/courses/101/pages/welcome"},
        {"id": 51, "position": 2, "title": "Outline", "type": "File", "content_id": 30},
        {"id": 52, "position": 3, "title": "Reading", "type": "ExternalUrl", "external_url": "https://example.org/read"},
    ]},
]

PAGES = [{"url": "welcome", "title": "Welcome", "body": "<p>Welcome to <b>BIO120</b>. Slides: <a href=\"/courses/101/files/10/download\">L1</a></p>",
          "updated_at": "2026-09-01T00:00:00Z", "html_url": f"{BASE}/courses/101/pages/welcome", "published": True}]

ASSIGNMENTS = [{"id": 900, "name": "Lab 1", "description": "<p>Measure osmosis.</p>", "due_at": "2026-09-20T03:59:00Z", "updated_at": "2026-09-01T00:00:00Z",
                "points_possible": 10, "html_url": f"{BASE}/courses/101/assignments/900", "submission_types": ["online_upload"],
                "submission": {"workflow_state": "unsubmitted"}}]

ANNOUNCEMENTS = [{"id": 700, "title": "Midterm date", "message": "<p>Midterm is Oct 15.</p>", "posted_at": "2026-09-05T12:00:00Z",
                  "html_url": f"{BASE}/courses/101/discussion_topics/700", "author": {"display_name": "Prof Green"}}]

DISCUSSIONS = [{"id": 800, "title": "Q&A", "message": "<p>Ask here.</p>", "posted_at": "2026-09-03T00:00:00Z", "last_reply_at": "2026-09-04T00:00:00Z",
                "html_url": f"{BASE}/courses/101/discussion_topics/800"}]
DISCUSSION_VIEW = {"participants": [{"id": 1, "display_name": "Sam"}],
                   "view": [{"id": 1, "user_id": 1, "message": "<p>Is the lab graded?</p>", "created_at": "2026-09-04T00:00:00Z", "replies": []}]}


def _json(data):
    return httpx.Response(200, json=data)


def mount(*, files_tab_hidden: bool = False, courses_status: int = 200) -> dict[str, respx.Route]:
    r: dict[str, respx.Route] = {}
    if courses_status == 200:
        r["courses"] = respx.get(url__startswith=f"{BASE}/api/v1/courses?").mock(return_value=_json([COURSE]))
    else:
        r["courses"] = respx.get(url__startswith=f"{BASE}/api/v1/courses?").mock(return_value=httpx.Response(courses_status, json={"status": "unauthenticated"}))
    r["folders"] = respx.get(url__startswith=f"{BASE}/api/v1/courses/101/folders").mock(return_value=_json(FOLDERS))
    if files_tab_hidden:
        r["files"] = respx.get(url__regex=rf"{BASE}/api/v1/courses/101/files\?.*").mock(return_value=httpx.Response(401, json={"status": "unauthorized"}))
    else:
        r["files"] = respx.get(url__regex=rf"{BASE}/api/v1/courses/101/files\?.*").mock(return_value=_json([FILE_10, FILE_11, FILE_12]))
    r["file30"] = respx.get(url__startswith=f"{BASE}/api/v1/courses/101/files/30").mock(return_value=_json(FILE_30))
    r["file10"] = respx.get(url__startswith=f"{BASE}/api/v1/courses/101/files/10").mock(return_value=_json(FILE_10))
    r["modules"] = respx.get(url__startswith=f"{BASE}/api/v1/courses/101/modules").mock(return_value=_json(MODULES))
    r["pages"] = respx.get(url__startswith=f"{BASE}/api/v1/courses/101/pages").mock(return_value=_json(PAGES))
    r["assignments"] = respx.get(url__startswith=f"{BASE}/api/v1/courses/101/assignments").mock(return_value=_json(ASSIGNMENTS))
    r["announcements"] = respx.get(url__startswith=f"{BASE}/api/v1/announcements").mock(return_value=_json(ANNOUNCEMENTS))
    r["discussion_view"] = respx.get(url__startswith=f"{BASE}/api/v1/courses/101/discussion_topics/800/view").mock(return_value=_json(DISCUSSION_VIEW))
    r["discussions"] = respx.get(url__startswith=f"{BASE}/api/v1/courses/101/discussion_topics").mock(return_value=_json(DISCUSSIONS))
    r["download10"] = respx.get(url__startswith=f"{BASE}/files/10/download").mock(return_value=httpx.Response(302, headers={"Location": S3 + "/10"}))
    r["download30"] = respx.get(url__startswith=f"{BASE}/files/30/download").mock(return_value=httpx.Response(302, headers={"Location": S3 + "/30"}))
    r["s3"] = respx.get(url__startswith=S3).mock(return_value=httpx.Response(200, content=PDF_BYTES))
    return r
