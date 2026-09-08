"""Typed views over the Canvas JSON objects we care about."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_FILE_ID_IN_URL = re.compile(r"/files/(\d+)")


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class Course:
    id: int
    name: str
    code: str
    term: str | None = None
    enrollment_state: str | None = None
    workflow_state: str | None = None
    syllabus_body: str | None = None
    start_at: str | None = None
    end_at: str | None = None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Course":
        term = d.get("term") or {}
        enrollments = d.get("enrollments") or []
        state = enrollments[0].get("enrollment_state") if enrollments else None
        return cls(
            id=int(d["id"]),
            name=d.get("name") or d.get("course_code") or f"Course {d['id']}",
            code=d.get("course_code") or str(d["id"]),
            term=term.get("name"),
            enrollment_state=state,
            workflow_state=d.get("workflow_state"),
            syllabus_body=d.get("syllabus_body"),
            start_at=d.get("start_at") or term.get("start_at"),
            end_at=d.get("end_at") or term.get("end_at"),
        )


@dataclass
class Folder:
    id: int
    full_name: str
    parent_id: int | None
    updated_at: str | None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Folder":
        return cls(int(d["id"]), d.get("full_name") or d.get("name") or "", _int(d.get("parent_folder_id")), d.get("updated_at"))


@dataclass
class File:
    id: int
    display_name: str
    filename: str
    content_type: str | None
    size: int
    url: str | None
    folder_id: int | None
    updated_at: str | None
    modified_at: str | None
    locked_for_user: bool
    lock_explanation: str | None = None
    html_url: str | None = None

    @property
    def version_key(self) -> str:
        """Value that changes whenever the file's content changes."""
        return f"{self.modified_at or self.updated_at or ''}|{self.size}"

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "File":
        return cls(
            id=int(d["id"]),
            display_name=d.get("display_name") or d.get("filename") or str(d["id"]),
            filename=d.get("filename") or d.get("display_name") or str(d["id"]),
            content_type=d.get("content-type") or d.get("content_type") or d.get("mime_class"),
            size=int(d.get("size") or 0),
            url=d.get("url"),
            folder_id=_int(d.get("folder_id")),
            updated_at=d.get("updated_at"),
            modified_at=d.get("modified_at"),
            locked_for_user=bool(d.get("locked_for_user") or d.get("locked")),
            lock_explanation=d.get("lock_explanation"),
            html_url=d.get("html_url"),
        )


@dataclass
class ModuleItem:
    id: int
    module_id: int
    position: int
    title: str
    type: str
    content_id: int | None
    page_url: str | None
    external_url: str | None
    html_url: str | None
    indent: int = 0
    locked_for_user: bool = False
    due_at: str | None = None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "ModuleItem":
        cd = d.get("content_details") or {}
        return cls(
            id=int(d["id"]),
            module_id=int(d.get("module_id") or 0),
            position=int(d.get("position") or 0),
            title=d.get("title") or "",
            type=d.get("type") or "Unknown",
            content_id=_int(d.get("content_id")),
            page_url=d.get("page_url"),
            external_url=d.get("external_url"),
            html_url=d.get("html_url"),
            indent=int(d.get("indent") or 0),
            locked_for_user=bool(cd.get("locked_for_user")),
            due_at=cd.get("due_at"),
        )


@dataclass
class Module:
    id: int
    name: str
    position: int
    state: str | None
    unlock_at: str | None
    items: list[ModuleItem] | None = field(default=None)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Module":
        items_raw = d.get("items")
        items = None
        if items_raw is not None:
            items = []
            for it in items_raw:
                it = dict(it)
                it.setdefault("module_id", d["id"])
                items.append(ModuleItem.from_api(it))
        return cls(int(d["id"]), d.get("name") or "", int(d.get("position") or 0), d.get("state"), d.get("unlock_at"), items)


@dataclass
class Page:
    url: str
    title: str
    body: str | None
    updated_at: str | None
    published: bool
    html_url: str | None
    page_id: int | None = None
    locked_for_user: bool = False

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Page":
        return cls(
            url=d.get("url") or str(d.get("page_id")),
            title=d.get("title") or "",
            body=d.get("body"),
            updated_at=d.get("updated_at"),
            published=bool(d.get("published", True)),
            html_url=d.get("html_url"),
            page_id=_int(d.get("page_id")),
            locked_for_user=bool(d.get("locked_for_user")),
        )


@dataclass
class Assignment:
    id: int
    name: str
    description: str | None
    due_at: str | None
    updated_at: str | None
    points_possible: float | None
    html_url: str | None
    submission_types: list[str]
    submitted: bool = False
    score: float | None = None
    graded: bool = False
    workflow_state: str | None = None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Assignment":
        sub = d.get("submission") or {}
        return cls(
            id=int(d["id"]),
            name=d.get("name") or "",
            description=d.get("description"),
            due_at=d.get("due_at"),
            updated_at=d.get("updated_at"),
            points_possible=d.get("points_possible"),
            html_url=d.get("html_url"),
            submission_types=list(d.get("submission_types") or []),
            submitted=sub.get("workflow_state") in ("submitted", "graded", "pending_review"),
            score=sub.get("score"),
            graded=sub.get("workflow_state") == "graded",
            workflow_state=d.get("workflow_state"),
        )


@dataclass
class DiscussionTopic:
    id: int
    title: str
    message: str | None
    posted_at: str | None
    last_reply_at: str | None
    html_url: str | None
    is_announcement: bool
    author: str | None = None
    attachments: list[int] = field(default_factory=list)

    @property
    def version_key(self) -> str:
        return f"{self.posted_at or ''}|{self.last_reply_at or ''}"

    @classmethod
    def from_api(cls, d: dict[str, Any], *, is_announcement: bool | None = None) -> "DiscussionTopic":
        author = (d.get("author") or {}).get("display_name")
        attach_ids = [int(a["id"]) for a in d.get("attachments") or [] if a.get("id") is not None]
        return cls(
            id=int(d["id"]),
            title=d.get("title") or "",
            message=d.get("message"),
            posted_at=d.get("posted_at") or d.get("created_at"),
            last_reply_at=d.get("last_reply_at"),
            html_url=d.get("html_url"),
            is_announcement=bool(d.get("is_announcement")) if is_announcement is None else is_announcement,
            author=author,
            attachments=attach_ids,
        )


def flatten_discussion_view(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the nested `view` structure of /discussion_topics/:id/view into
    a list of {author, created_at, message, depth} entries in reading order."""
    participants = {p.get("id"): p.get("display_name") for p in view.get("participants") or []}
    out: list[dict[str, Any]] = []

    def walk(entries: list[dict[str, Any]], depth: int) -> None:
        for e in entries:
            if e.get("deleted"):
                continue
            out.append(
                {
                    "author": participants.get(e.get("user_id")) or "Unknown",
                    "created_at": e.get("created_at"),
                    "message": e.get("message") or "",
                    "depth": depth,
                }
            )
            walk(e.get("replies") or [], depth + 1)

    walk(view.get("view") or [], 0)
    return out
