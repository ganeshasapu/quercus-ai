"""SQLite-backed document store with FTS5 full-text search."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from quercus_mcp.store.query import fts_query

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

KINDS = ("file", "page", "syllabus", "module", "assignment", "announcement", "discussion")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class CourseRow:
    id: int
    name: str
    code: str
    term: str | None = None
    enrollment_state: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    files_tab_hidden: bool = False
    active: bool = True
    last_synced_at: str | None = None


@dataclass
class DocumentRow:
    course_id: int
    kind: str
    canvas_id: str
    title: str
    url: str | None = None
    folder_path: str | None = None
    module_id: int | None = None
    module_name: str | None = None
    module_position: int | None = None
    content_type: str | None = None
    size: int | None = None
    version_key: str | None = None
    due_at: str | None = None
    posted_at: str | None = None
    locked: bool = False
    local_path: str | None = None
    text_path: str | None = None
    extract_status: str = "pending"
    extract_error: str | None = None
    body: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    removed: bool = False
    id: int | None = None
    first_seen_at: str | None = None
    indexed_at: str | None = None

    @classmethod
    def from_sql(cls, r: sqlite3.Row) -> "DocumentRow":
        d = dict(r)
        d["extra"] = json.loads(d.get("extra") or "{}")
        d["locked"] = bool(d.get("locked"))
        d["removed"] = bool(d.get("removed"))
        d.setdefault("body", "")
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Hit:
    doc_id: int
    course_id: int
    course_code: str
    kind: str
    title: str
    snippet: str
    url: str | None
    score: float


@dataclass
class SyncRun:
    id: int
    started_at: str
    finished_at: str | None
    scope: str
    status: str
    added: int
    updated: int
    removed: int
    errors: list[str]


_DOC_COLS_NO_BODY = (
    "id, course_id, kind, canvas_id, title, url, folder_path, module_id, module_name, module_position, "
    "content_type, size, version_key, due_at, posted_at, locked, local_path, text_path, extract_status, "
    "extract_error, extra, removed, first_seen_at, indexed_at"
)


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----------------------------------------------------------- courses

    def upsert_course(self, c: CourseRow) -> None:
        self._db.execute(
            """INSERT INTO courses(id, name, code, term, enrollment_state, start_at, end_at, files_tab_hidden, active, last_synced_at)
               VALUES (:id, :name, :code, :term, :enrollment_state, :start_at, :end_at, :files_tab_hidden, :active, :last_synced_at)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, code=excluded.code, term=excluded.term,
                 enrollment_state=excluded.enrollment_state, start_at=excluded.start_at, end_at=excluded.end_at,
                 files_tab_hidden=excluded.files_tab_hidden, active=excluded.active,
                 last_synced_at=COALESCE(excluded.last_synced_at, courses.last_synced_at)""",
            {**asdict(c), "files_tab_hidden": int(c.files_tab_hidden), "active": int(c.active)},
        )
        self._db.commit()

    def set_course_synced(self, course_id: int, *, files_tab_hidden: bool | None = None) -> None:
        if files_tab_hidden is None:
            self._db.execute("UPDATE courses SET last_synced_at=? WHERE id=?", (utcnow(), course_id))
        else:
            self._db.execute("UPDATE courses SET last_synced_at=?, files_tab_hidden=? WHERE id=?", (utcnow(), int(files_tab_hidden), course_id))
        self._db.commit()

    def deactivate_courses_not_in(self, keep_ids: set[int]) -> int:
        if keep_ids:
            marks = ",".join("?" * len(keep_ids))
            cur = self._db.execute(f"UPDATE courses SET active=0 WHERE active=1 AND id NOT IN ({marks})", tuple(keep_ids))
        else:
            cur = self._db.execute("UPDATE courses SET active=0 WHERE active=1")
        self._db.commit()
        return cur.rowcount

    def courses(self, *, include_inactive: bool = False) -> list[CourseRow]:
        sql = "SELECT * FROM courses" + ("" if include_inactive else " WHERE active=1") + " ORDER BY term DESC, code"
        return [self._course_row(r) for r in self._db.execute(sql)]

    def get_course(self, course_id: int) -> CourseRow | None:
        r = self._db.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        return self._course_row(r) if r else None

    @staticmethod
    def _course_row(r: sqlite3.Row) -> CourseRow:
        d = dict(r)
        d["files_tab_hidden"] = bool(d["files_tab_hidden"])
        d["active"] = bool(d["active"])
        return CourseRow(**d)

    def counts_by_kind(self, course_id: int) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT kind, COUNT(*) AS n FROM documents WHERE course_id=? AND removed=0 GROUP BY kind", (course_id,)
        )
        return {r["kind"]: r["n"] for r in rows}

    # --------------------------------------------------------- documents

    def get_document_meta(self, course_id: int, kind: str, canvas_id: str) -> DocumentRow | None:
        r = self._db.execute(
            f"SELECT {_DOC_COLS_NO_BODY} FROM documents WHERE course_id=? AND kind=? AND canvas_id=?",
            (course_id, kind, str(canvas_id)),
        ).fetchone()
        return DocumentRow.from_sql(r) if r else None

    def upsert_document(self, d: DocumentRow) -> tuple[int, bool]:
        """Insert or update metadata. Returns (doc_id, changed) where changed is
        True for new documents or when version_key differs from the stored one.
        Body/extract fields are only written when the document is new or
        `d.body` is non-empty (callers set text via set_document_text)."""
        existing = self.get_document_meta(d.course_id, d.kind, d.canvas_id)
        now = utcnow()
        if existing is None:
            cur = self._db.execute(
                """INSERT INTO documents(course_id, kind, canvas_id, title, url, folder_path, module_id, module_name,
                     module_position, content_type, size, version_key, due_at, posted_at, locked, local_path, text_path,
                     extract_status, extract_error, body, extra, removed, first_seen_at, indexed_at)
                   VALUES (:course_id, :kind, :canvas_id, :title, :url, :folder_path, :module_id, :module_name,
                     :module_position, :content_type, :size, :version_key, :due_at, :posted_at, :locked, :local_path,
                     :text_path, :extract_status, :extract_error, :body, :extra, 0, :now, :indexed_at)""",
                self._doc_params(d, now),
            )
            self._db.commit()
            return int(cur.lastrowid), True

        changed = (existing.version_key != d.version_key) or existing.removed or existing.extract_status == "pending"
        params = self._doc_params(d, now)
        params["id"] = existing.id
        set_body = bool(d.body) or (changed and d.kind != "file")
        self._db.execute(
            """UPDATE documents SET title=:title, url=:url, folder_path=:folder_path, module_id=:module_id,
                 module_name=:module_name, module_position=:module_position, content_type=:content_type, size=:size,
                 version_key=:version_key, due_at=:due_at, posted_at=:posted_at, locked=:locked, extra=:extra, removed=0
               WHERE id=:id""",
            params,
        )
        if set_body:
            self._db.execute(
                "UPDATE documents SET body=:body, extract_status=:extract_status, extract_error=:extract_error, indexed_at=:indexed_at WHERE id=:id",
                params,
            )
        self._db.commit()
        return int(existing.id), bool(changed)

    @staticmethod
    def _doc_params(d: DocumentRow, now: str) -> dict[str, Any]:
        p = asdict(d)
        p["canvas_id"] = str(d.canvas_id)
        p["locked"] = int(d.locked)
        p["extra"] = json.dumps(d.extra, default=str)
        p["now"] = now
        p["indexed_at"] = now if d.body else d.indexed_at
        return p

    def set_document_text(
        self,
        doc_id: int,
        *,
        body: str,
        status: str,
        error: str | None = None,
        local_path: str | None = None,
        text_path: str | None = None,
    ) -> None:
        self._db.execute(
            """UPDATE documents SET body=?, extract_status=?, extract_error=?, indexed_at=?,
                 local_path=COALESCE(?, local_path), text_path=COALESCE(?, text_path) WHERE id=?""",
            (body, status, error, utcnow(), local_path, text_path, doc_id),
        )
        self._db.commit()

    def set_document_status(self, doc_id: int, status: str, error: str | None = None) -> None:
        self._db.execute("UPDATE documents SET extract_status=?, extract_error=? WHERE id=?", (status, error, doc_id))
        self._db.commit()

    def mark_removed(self, course_id: int, kind: str, keep_canvas_ids: set[str]) -> int:
        ids = [str(x) for x in keep_canvas_ids]
        if ids:
            marks = ",".join("?" * len(ids))
            cur = self._db.execute(
                f"UPDATE documents SET removed=1 WHERE course_id=? AND kind=? AND removed=0 AND canvas_id NOT IN ({marks})",
                (course_id, kind, *ids),
            )
        else:
            cur = self._db.execute("UPDATE documents SET removed=1 WHERE course_id=? AND kind=? AND removed=0", (course_id, kind))
        self._db.commit()
        return cur.rowcount

    def get_document(self, doc_id: int) -> DocumentRow | None:
        r = self._db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return DocumentRow.from_sql(r) if r else None

    def list_documents(self, course_id: int, kind: str | None = None, *, include_removed: bool = False) -> list[DocumentRow]:
        sql = f"SELECT {_DOC_COLS_NO_BODY} FROM documents WHERE course_id=?"
        args: list[Any] = [course_id]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if not include_removed:
            sql += " AND removed=0"
        sql += " ORDER BY module_position, folder_path, title"
        return [DocumentRow.from_sql(r) for r in self._db.execute(sql, args)]

    def documents_by_canvas_ids(self, course_id: int, kind: str) -> dict[str, int]:
        rows = self._db.execute("SELECT canvas_id, id FROM documents WHERE course_id=? AND kind=?", (course_id, kind))
        return {r["canvas_id"]: r["id"] for r in rows}

    # ------------------------------------------------------------ search

    def search(self, query: str, *, course_id: int | None = None, kind: str | None = None, limit: int = 10) -> list[Hit]:
        hits = self._search(fts_query(query, mode="and"), course_id, kind, limit)
        if not hits and len(query.split()) > 1:
            hits = self._search(fts_query(query, mode="or"), course_id, kind, limit)
        return hits

    def _search(self, match: str, course_id: int | None, kind: str | None, limit: int) -> list[Hit]:
        if not match:
            return []
        sql = """SELECT d.id, d.course_id, c.code AS course_code, d.kind, d.title, d.url,
                        snippet(documents_fts, 1, '[', ']', ' … ', 32) AS snip,
                        bm25(documents_fts, 5.0, 1.0) AS score
                 FROM documents_fts JOIN documents d ON d.id = documents_fts.rowid
                 JOIN courses c ON c.id = d.course_id
                 WHERE documents_fts MATCH ? AND d.removed = 0"""
        args: list[Any] = [match]
        if course_id is not None:
            sql += " AND d.course_id = ?"
            args.append(course_id)
        if kind:
            sql += " AND d.kind = ?"
            args.append(kind)
        sql += " ORDER BY score LIMIT ?"
        args.append(limit)
        try:
            rows = self._db.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            snip = r["snip"] or ""
            if not snip.strip():
                snip = r["title"]
            out.append(Hit(r["id"], r["course_id"], r["course_code"], r["kind"], r["title"], snip.replace("\n", " ").strip(), r["url"], float(r["score"])))
        return out

    # ----------------------------------------------------- volatile views

    def upcoming(self, *, days: int = 14, course_id: int | None = None, now: datetime | None = None) -> list[DocumentRow]:
        now = now or datetime.now(timezone.utc)
        lo = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        hi = (now + timedelta(days=days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        sql = f"SELECT {_DOC_COLS_NO_BODY} FROM documents WHERE kind='assignment' AND removed=0 AND due_at IS NOT NULL AND due_at >= ? AND due_at <= ?"
        args: list[Any] = [lo, hi]
        if course_id is not None:
            sql += " AND course_id=?"
            args.append(course_id)
        sql += " ORDER BY due_at"
        return [DocumentRow.from_sql(r) for r in self._db.execute(sql, args)]

    def announcements(self, *, course_id: int | None = None, since: datetime | None = None, limit: int = 20) -> list[DocumentRow]:
        sql = "SELECT * FROM documents WHERE kind='announcement' AND removed=0"
        args: list[Any] = []
        if course_id is not None:
            sql += " AND course_id=?"
            args.append(course_id)
        if since is not None:
            sql += " AND posted_at >= ?"
            args.append(since.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
        sql += " ORDER BY posted_at DESC LIMIT ?"
        args.append(limit)
        return [DocumentRow.from_sql(r) for r in self._db.execute(sql, args)]

    # --------------------------------------------------------- sync runs

    def start_sync_run(self, scope: str) -> int:
        cur = self._db.execute("INSERT INTO sync_runs(started_at, scope) VALUES (?, ?)", (utcnow(), scope))
        self._db.commit()
        return int(cur.lastrowid)

    def finish_sync_run(self, run_id: int, *, status: str, added: int, updated: int, removed: int, errors: list[str]) -> None:
        self._db.execute(
            "UPDATE sync_runs SET finished_at=?, status=?, added=?, updated=?, removed=?, errors=? WHERE id=?",
            (utcnow(), status, added, updated, removed, json.dumps(errors[:50]), run_id),
        )
        self._db.commit()

    def sync_runs(self, limit: int = 5) -> list[SyncRun]:
        rows = self._db.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [SyncRun(r["id"], r["started_at"], r["finished_at"], r["scope"], r["status"], r["added"], r["updated"], r["removed"], json.loads(r["errors"])) for r in rows]

    def last_successful_sync(self) -> str | None:
        r = self._db.execute("SELECT finished_at FROM sync_runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
        return r["finished_at"] if r else None

    # -------------------------------------------------------------- meta

    def meta_get(self, key: str) -> str | None:
        r = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def meta_set(self, key: str, value: str | None) -> None:
        if value is None:
            self._db.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            self._db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self._db.commit()
