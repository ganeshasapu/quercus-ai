"""Pull a student's courses from Canvas into the local store."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quercus_mcp.canvas.client import CanvasClient
from quercus_mcp.canvas.errors import AuthError, CanvasError
from quercus_mcp.canvas.models import Assignment, Course, DiscussionTopic, File, Module, ModuleItem, Page, flatten_discussion_view
from quercus_mcp.config import Config, Paths
from quercus_mcp.extract import extract_text
from quercus_mcp.extract.html import find_file_ids, html_to_markdown
from quercus_mcp.store import CourseRow, DocumentRow, Store, utcnow
from quercus_mcp.sync.discover import collect_file_ids

log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
VOLATILE_KINDS = ("assignment", "announcement")


def slugify(s: str, max_len: int = 80) -> str:
    s = _SLUG_RE.sub("-", s.strip()).strip("-.")
    return (s or "untitled")[:max_len]


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:16]


def volatile_stale(store: Store, config: Config, course_id: int) -> bool:
    ts = store.meta_get(f"volatile:{course_id}")
    if ts is None:
        return True
    try:
        last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last > timedelta(minutes=config.volatile_ttl_minutes)


@dataclass
class SyncSummary:
    added: int = 0
    updated: int = 0
    removed: int = 0
    downloaded: int = 0
    errors: list[str] = field(default_factory=list)
    courses: int = 0
    status: str = "ok"

    def merge(self, other: "SyncSummary") -> None:
        self.added += other.added
        self.updated += other.updated
        self.removed += other.removed
        self.downloaded += other.downloaded
        self.errors.extend(other.errors)


@dataclass
class _CourseData:
    course: Course
    folders: dict[int, str]
    files_tab_hidden: bool
    files: dict[int, File]
    modules: list[Module]
    pages: list[Page]
    assignments: list[Assignment]
    announcements: list[DiscussionTopic]
    discussions: list[DiscussionTopic]
    denied: set[str] = field(default_factory=set)  # kinds whose endpoint returned 403/404


class Syncer:
    def __init__(self, client: CanvasClient, store: Store, config: Config, paths: Paths, *, retry_errors: bool = False):
        self.client = client
        self.store = store
        self.config = config
        self.paths = paths
        self.retry_errors = retry_errors
        self.paths.ensure()

    # ------------------------------------------------------------ public

    async def sync_all(self, *, full: bool = False, course_ids: list[int] | None = None) -> SyncSummary:
        scope = "all" if not course_ids else "courses:" + ",".join(map(str, course_ids))
        run_id = self.store.start_sync_run(scope)
        summary = SyncSummary()
        try:
            listed = await self.client.list_courses(include_completed=self.config.all_terms or bool(course_ids))
            courses = [c for c in listed if self._course_selected(c, course_ids)]
            if course_ids:
                # Explicit ids not in the enrollment listing (e.g. past terms): fetch directly.
                have = {c.id for c in courses}
                extra = await asyncio.gather(*(self.client.get_course(cid) for cid in course_ids if cid not in have))
                courses.extend(c for c in extra if c is not None)
            else:
                self.store.deactivate_courses_not_in({c.id for c in courses})
            summary.courses = len(courses)
            sem = asyncio.Semaphore(2)

            async def one(c: Course) -> SyncSummary:
                async with sem:
                    return await self.sync_course(c, full=full)

            results = await asyncio.gather(*(one(c) for c in courses), return_exceptions=True)
            for c, r in zip(courses, results):
                if isinstance(r, AuthError):
                    raise r
                if isinstance(r, BaseException):
                    log.error("course %s failed", c.id, exc_info=r)
                    summary.errors.append(f"{c.code}: {type(r).__name__}: {r}")
                else:
                    summary.merge(r)
            summary.status = "ok" if not summary.errors else "partial"
            self.store.meta_set("last_auth_error", None)
        except AuthError as exc:
            summary.status = "failed"
            summary.errors.append(f"authentication failed: {exc}")
            self.store.meta_set("last_auth_error", utcnow())
            self.store.finish_sync_run(run_id, status="failed", added=summary.added, updated=summary.updated, removed=summary.removed, errors=summary.errors)
            raise
        except Exception as exc:  # noqa: BLE001
            summary.status = "failed"
            summary.errors.append(f"{type(exc).__name__}: {exc}")
            self.store.finish_sync_run(run_id, status="failed", added=summary.added, updated=summary.updated, removed=summary.removed, errors=summary.errors)
            raise
        self.store.finish_sync_run(run_id, status=summary.status, added=summary.added, updated=summary.updated, removed=summary.removed, errors=summary.errors)
        return summary

    async def sync_course(self, course: Course, *, full: bool = False) -> SyncSummary:
        summary = SyncSummary(courses=1)
        self.store.upsert_course(_course_row(course))
        data = await self._fetch_course(course)
        file_docs = await self._sync_files(data, summary, full=full)
        rewriter = self._link_rewriter(file_docs)
        await self._sync_syllabus(data, rewriter, summary)
        await self._sync_pages(data, rewriter, summary)
        await self._sync_modules(data, file_docs, summary)
        await self._sync_assignments(data, rewriter, summary)
        await self._sync_announcements(data, rewriter, summary)
        await self._sync_discussions(data, rewriter, summary)
        self.store.set_course_synced(course.id, files_tab_hidden=data.files_tab_hidden)
        self.store.meta_set(f"volatile:{course.id}", utcnow())
        return summary

    async def refresh_volatile(self, course_id: int) -> SyncSummary:
        """Re-fetch announcements and assignments for one course."""
        summary = SyncSummary(courses=1)
        row = self.store.get_course(course_id)
        if row is None:
            return summary
        course = Course(id=row.id, name=row.name, code=row.code, term=row.term, start_at=row.start_at)
        assignments, announcements = await asyncio.gather(
            self.client.list_assignments(course.id),
            self.client.list_announcements(course.id, start_date=self._announcement_start(course)),
        )
        denied = {k for k, v in (("assignment", assignments), ("announcement", announcements)) if v is None}
        data = _CourseData(course, {}, False, {}, [], [], assignments or [], announcements or [], [], denied)
        file_docs = {int(k): v for k, v in self.store.documents_by_canvas_ids(course.id, "file").items() if k.isdigit()}
        rewriter = self._link_rewriter(file_docs)
        await self._sync_assignments(data, rewriter, summary)
        await self._sync_announcements(data, rewriter, summary)
        self.store.meta_set(f"volatile:{course_id}", utcnow())
        return summary

    def volatile_stale(self, course_id: int) -> bool:
        return volatile_stale(self.store, self.config, course_id)

    # ------------------------------------------------------------- fetch

    def _course_selected(self, c: Course, course_ids: list[int] | None) -> bool:
        if course_ids is not None:
            return c.id in course_ids
        if self.config.include_courses and c.id not in self.config.include_courses:
            return False
        return c.id not in self.config.exclude_courses

    @staticmethod
    def _announcement_start(course: Course) -> str:
        if course.start_at:
            return course.start_at[:10]
        return (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()

    async def _fetch_course(self, course: Course) -> _CourseData:
        cid = course.id
        folders_raw, files_raw, modules, pages, assignments, announcements, discussions = await asyncio.gather(
            self.client.list_folders(cid),
            self.client.list_files(cid),
            self.client.list_modules(cid),
            self.client.list_pages(cid),
            self.client.list_assignments(cid),
            self.client.list_announcements(cid, start_date=self._announcement_start(course)),
            self.client.list_discussions(cid),
        )
        denied = {k for k, v in (("module", modules), ("page", pages), ("assignment", assignments),
                                 ("announcement", announcements), ("discussion", discussions)) if v is None}
        modules = modules or []
        pages = pages or []
        assignments = assignments or []
        announcements = announcements or []
        discussions = discussions or []
        folders = {f.id: _clean_folder(f.full_name) for f in (folders_raw or [])}
        files_tab_hidden = files_raw is None
        files: dict[int, File] = {f.id: f for f in (files_raw or [])}

        html_bodies = [course.syllabus_body, *(p.body for p in pages), *(a.description for a in assignments),
                       *(t.message for t in announcements), *(t.message for t in discussions)]
        module_items = [it for m in modules for it in (m.items or [])]
        wanted = collect_file_ids(files.keys(), module_items, html_bodies)
        for t in (*announcements, *discussions):
            wanted.update(t.attachments)
        missing = [fid for fid in wanted if fid not in files]
        fetched = await asyncio.gather(*(self.client.get_file(cid, fid) for fid in missing))
        for f in fetched:
            if f is not None:
                files[f.id] = f
        return _CourseData(course, folders, files_tab_hidden, files, modules, pages, assignments, announcements, discussions, denied)

    # ---------------------------------------------------------- per kind

    async def _sync_syllabus(self, data: _CourseData, rewriter, summary: SyncSummary) -> None:  # type: ignore[no-untyped-def]
        c = data.course
        if not (c.syllabus_body or "").strip():
            self.store.mark_removed(c.id, "syllabus", set())
            return
        md = html_to_markdown(c.syllabus_body, rewriter)
        row = DocumentRow(course_id=c.id, kind="syllabus", canvas_id="syllabus", title=f"{c.code} — Syllabus",
                          url=f"{self.client.base_url}/courses/{c.id}/assignments/syllabus", version_key=_hash(md),
                          body=md, extract_status="ok")
        await self._upsert_text_doc(row, summary)

    async def _sync_files(self, data: _CourseData, summary: SyncSummary, *, full: bool) -> dict[int, int]:
        c = data.course
        module_of: dict[int, ModuleItem] = {}
        module_names = {m.id: m for m in data.modules}
        for m in data.modules:
            for it in m.items or []:
                if it.type == "File" and it.content_id is not None and it.content_id not in module_of:
                    module_of[it.content_id] = it
        doc_ids: dict[int, int] = {}
        jobs = []
        for f in data.files.values():
            it = module_of.get(f.id)
            mod = module_names.get(it.module_id) if it else None
            folder = data.folders.get(f.folder_id or -1)
            row = DocumentRow(
                course_id=c.id, kind="file", canvas_id=str(f.id), title=f.display_name, url=f.html_url or f"{self.client.base_url}/courses/{c.id}/files/{f.id}",
                folder_path=folder, module_id=mod.id if mod else None, module_name=mod.name if mod else None,
                module_position=(mod.position * 1000 + it.position) if (mod and it) else None,
                content_type=f.content_type, size=f.size, version_key=f.version_key, locked=f.locked_for_user,
            )
            res = self.store.upsert_document(row)
            doc_ids[f.id] = res.doc_id
            if res.created:
                summary.added += 1
            elif res.changed:
                summary.updated += 1
            jobs.append(self._maybe_download(c, f, folder, res.doc_id, res.changed, full, summary))
        # Downloads fan out; the client's semaphore bounds real concurrency.
        await asyncio.gather(*jobs)
        summary.removed += self.store.mark_removed(c.id, "file", {str(i) for i in data.files})
        return doc_ids

    async def _maybe_download(self, c: Course, f: File, folder: str | None, doc_id: int, changed: bool, full: bool, summary: SyncSummary) -> None:
        try:
            await self._download_and_extract(c, f, folder, doc_id, changed, full, summary)
        except CanvasError as exc:
            self.store.set_document_text(doc_id, body="", status="error", error=f"download failed: HTTP {exc.status}")
            summary.errors.append(f"{c.code}: download {f.display_name}: HTTP {exc.status}")
        except Exception as exc:  # noqa: BLE001  one bad file must not abort the course
            log.exception("file %s in %s failed", f.id, c.code)
            self.store.set_document_text(doc_id, body="", status="error", error=f"{type(exc).__name__}: {exc}"[:500])
            summary.errors.append(f"{c.code}: {f.display_name}: {type(exc).__name__}: {exc}")

    async def _download_and_extract(self, c: Course, f: File, folder: str | None, doc_id: int, changed: bool, full: bool, summary: SyncSummary) -> None:
        if f.locked_for_user:
            self.store.set_document_status(doc_id, "locked", f.lock_explanation)
            return
        if f.size and f.size > self.config.max_file_mb * 1024 * 1024:
            self.store.set_document_status(doc_id, "skipped_size", f"{f.size} bytes > {self.config.max_file_mb} MB")
            return
        existing = self.store.get_document(doc_id)
        need = changed or full or existing is None or existing.extract_status == "pending"
        if existing is not None:
            if existing.extract_status in ("locked", "skipped_size"):
                need = True  # it was blocked before and the checks above now pass
            elif existing.extract_status == "error" and self.retry_errors:
                need = True
            elif existing.extract_status == "ok" and existing.local_path and not Path(existing.local_path).exists():
                need = True
        if not need:
            return
        dest = self.paths.files_dir / slugify(c.code) / (folder or "") / _safe_filename(f.filename or f.display_name)
        await self.client.download_file(f.id, dest, course_id=c.id)
        summary.downloaded += 1
        result = await asyncio.to_thread(extract_text, dest, f.content_type)
        text_path = None
        if result.status == "ok":
            text_path = await asyncio.to_thread(self._write_text, c.code, "file", f.display_name, str(f.id), result.text,
                                                title=f.display_name, url=f.html_url, updated=f.modified_at or f.updated_at)
        self.store.set_document_text(doc_id, body=result.text, status=result.status, error=result.error, local_path=str(dest), text_path=text_path)

    async def _sync_pages(self, data: _CourseData, rewriter, summary: SyncSummary) -> None:  # type: ignore[no-untyped-def]
        if "page" in data.denied:
            return
        c = data.course
        page_module: dict[str, tuple[Module, ModuleItem]] = {}
        for m in data.modules:
            for it in m.items or []:
                if it.type == "Page" and it.page_url and it.page_url not in page_module:
                    page_module[it.page_url] = (m, it)
        seen: set[str] = set()
        for p in data.pages:
            seen.add(p.url)
            md = html_to_markdown(p.body, rewriter)
            mod_it = page_module.get(p.url)
            row = DocumentRow(course_id=c.id, kind="page", canvas_id=p.url, title=p.title, url=p.html_url,
                              module_id=mod_it[0].id if mod_it else None, module_name=mod_it[0].name if mod_it else None,
                              module_position=(mod_it[0].position * 1000 + mod_it[1].position) if mod_it else None,
                              version_key=p.updated_at or _hash(md), body=md, extract_status="ok", locked=p.locked_for_user)
            await self._upsert_text_doc(row, summary)
        summary.removed += self.store.mark_removed(c.id, "page", seen)

    async def _sync_modules(self, data: _CourseData, file_docs: dict[int, int], summary: SyncSummary) -> None:
        if "module" in data.denied:
            return
        c = data.course
        seen: set[str] = set()
        for m in data.modules:
            seen.add(str(m.id))
            lines = [f"# {m.name}"]
            if m.state:
                lines.append(f"State: {m.state}" + (f" (unlocks {m.unlock_at})" if m.unlock_at else ""))
            for it in m.items or []:
                indent = "  " * it.indent
                if it.type == "SubHeader":
                    lines.append(f"{indent}**{it.title}**")
                    continue
                label = f"{indent}- [{it.type}] {it.title}"
                if it.type == "File" and it.content_id in file_docs:
                    label += f" (quercus://doc/{file_docs[it.content_id]})"
                elif it.type == "ExternalUrl" and it.external_url:
                    label += f" <{it.external_url}>"
                elif it.html_url and it.type not in ("File",):
                    label += f" <{it.html_url}>"
                if it.due_at:
                    label += f" — due {it.due_at}"
                if it.locked_for_user:
                    label += " [locked]"
                lines.append(label)
            body = "\n".join(lines)
            row = DocumentRow(course_id=c.id, kind="module", canvas_id=str(m.id), title=m.name,
                              url=f"{self.client.base_url}/courses/{c.id}/modules#module_{m.id}",
                              module_id=m.id, module_name=m.name, module_position=m.position * 1000,
                              version_key=_hash(body), body=body, extract_status="ok")
            await self._upsert_text_doc(row, summary)
        summary.removed += self.store.mark_removed(c.id, "module", seen)

    async def _sync_assignments(self, data: _CourseData, rewriter, summary: SyncSummary) -> None:  # type: ignore[no-untyped-def]
        if "assignment" in data.denied:
            return
        c = data.course
        seen: set[str] = set()
        for a in data.assignments:
            seen.add(str(a.id))
            md = html_to_markdown(a.description, rewriter)
            extra = {"points_possible": a.points_possible, "submitted": a.submitted, "graded": a.graded, "score": a.score,
                     "submission_types": a.submission_types}
            row = DocumentRow(course_id=c.id, kind="assignment", canvas_id=str(a.id), title=a.name, url=a.html_url,
                              due_at=a.due_at, version_key=f"{a.updated_at}|{a.submitted}|{a.score}|{a.due_at}",
                              body=md, extra=extra, extract_status="ok")
            await self._upsert_text_doc(row, summary)
        summary.removed += self.store.mark_removed(c.id, "assignment", seen)

    async def _sync_announcements(self, data: _CourseData, rewriter, summary: SyncSummary) -> None:  # type: ignore[no-untyped-def]
        if "announcement" in data.denied:
            return
        c = data.course
        seen: set[str] = set()
        for t in data.announcements:
            seen.add(str(t.id))
            md = html_to_markdown(t.message, rewriter)
            row = DocumentRow(course_id=c.id, kind="announcement", canvas_id=str(t.id), title=t.title, url=t.html_url,
                              posted_at=t.posted_at, version_key=t.version_key + "|" + _hash(md), body=md,
                              extra={"author": t.author}, extract_status="ok")
            await self._upsert_text_doc(row, summary)
        summary.removed += self.store.mark_removed(c.id, "announcement", seen)

    async def _sync_discussions(self, data: _CourseData, rewriter, summary: SyncSummary) -> None:  # type: ignore[no-untyped-def]
        if "discussion" in data.denied:
            return
        c = data.course
        seen: set[str] = set()
        for t in data.discussions:
            seen.add(str(t.id))
            existing = self.store.get_document_meta(c.id, "discussion", str(t.id))
            if existing is not None and existing.version_key == t.version_key and existing.extract_status == "ok" and not existing.removed:
                # Unchanged: touch metadata only.
                self.store.upsert_document(DocumentRow(course_id=c.id, kind="discussion", canvas_id=str(t.id), title=t.title,
                                                       url=t.html_url, posted_at=t.posted_at, version_key=t.version_key,
                                                       extra={"author": t.author}))
                continue
            parts = [html_to_markdown(t.message, rewriter)]
            view = await self.client.get_discussion_view(c.id, t.id)
            for e in flatten_discussion_view(view):
                quote = "> " * e["depth"]
                parts.append(f"{quote}**{e['author']}** ({e['created_at']}):\n{quote}{html_to_markdown(e['message'], rewriter).replace(chr(10), chr(10) + quote)}")
            body = "\n\n".join(p for p in parts if p)
            row = DocumentRow(course_id=c.id, kind="discussion", canvas_id=str(t.id), title=t.title, url=t.html_url,
                              posted_at=t.posted_at, version_key=t.version_key, body=body, extra={"author": t.author},
                              extract_status="ok")
            await self._upsert_text_doc(row, summary)
        summary.removed += self.store.mark_removed(c.id, "discussion", seen)

    # ----------------------------------------------------------- helpers

    async def _upsert_text_doc(self, row: DocumentRow, summary: SyncSummary) -> int:
        res = self.store.upsert_document(row)  # writes body + FTS when changed
        if res.created:
            summary.added += 1
        elif res.changed:
            summary.updated += 1
        if res.changed:
            course = self.store.get_course(row.course_id)
            code = course.code if course else str(row.course_id)
            try:
                path = await asyncio.to_thread(self._write_text, code, row.kind, row.title, row.canvas_id, row.body,
                                               title=row.title, url=row.url, updated=row.version_key)
                self.store.set_text_path(res.doc_id, path)
            except OSError as exc:
                log.warning("could not write text file for %s/%s: %s", code, row.title, exc)
                summary.errors.append(f"{code}: write {row.title}: {exc}")
        return res.doc_id

    def _write_text(self, code: str, kind: str, name: str, canvas_id: str, text: str, *, title: str, url: str | None, updated: str | None) -> str:
        d = self.paths.text_dir / slugify(code) / kind
        d.mkdir(parents=True, exist_ok=True)
        base = slugify(Path(name).stem if kind == "file" else name, max_len=60)
        suffix = canvas_id if canvas_id.isdigit() else _hash(canvas_id)[:8]
        path = d / (f"{base}--{suffix}.md" if kind != "syllabus" else f"{base}.md")
        front = "\n".join(
            ["---", f"title: {_yaml_str(title)}", f"kind: {kind}", f"course: {_yaml_str(code)}", f"url: {_yaml_str(url or '')}",
             f"updated: {_yaml_str(updated or '')}", "---", ""]
        )
        path.write_text(front + text + "\n")
        return str(path)

    def _link_rewriter(self, file_docs: dict[int, int]):  # type: ignore[no-untyped-def]
        def rewrite(href: str) -> str:
            ids = find_file_ids(href)
            if len(ids) == 1:
                fid = next(iter(ids))
                if fid in file_docs:
                    return f"quercus://doc/{file_docs[fid]}"
            return href
        return rewrite


def _course_row(c: Course) -> CourseRow:
    return CourseRow(id=c.id, name=c.name, code=c.code, term=c.term, enrollment_state=c.enrollment_state,
                     start_at=c.start_at, end_at=c.end_at, active=True)


def _clean_folder(full_name: str) -> str:
    parts = [p for p in full_name.split("/") if p]
    if parts and parts[0].lower() == "course files":
        parts = parts[1:]
    return "/".join(_safe_filename(p) for p in parts)


def _safe_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").replace("\x00", "")
    name = name.strip().strip(".") or "file"
    return name[:150]


def _yaml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
