# Quercus MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python MCP server that syncs a UofT student's Quercus (Canvas) courses into a local SQLite/FTS5 cache with extracted file text, and exposes search/read/upcoming/announcement tools to Claude.

**Architecture:** One package `quercus_mcp`. A thin async httpx Canvas client feeds a crawler that produces `Document` rows; an extraction layer turns PDF/PPTX/DOCX/HTML into Markdown; SQLite with FTS5 stores and indexes; FastMCP exposes tools; a background task re-syncs on an interval.

**Tech Stack:** Python 3.11+, uv, httpx, mcp (FastMCP), PyMuPDF, python-pptx, python-docx, markdownify, keyring, typer, pytest + pytest-asyncio + respx.

**Spec:** `docs/superpowers/specs/2026-09-08-quercus-mcp-design.md`

## Global Constraints

- Python >= 3.11; project managed with `uv`; `src/` layout; package `quercus_mcp`; console script `quercus`.
- Every Canvas request sends `Authorization: Bearer <token>` and a `User-Agent: quercus-mcp/<version> (+https://github.com/...)`.
- Max 4 concurrent HTTP requests. Backoff on 429 and on 403 containing "Rate Limit Exceeded".
- Authorization header must be stripped when following a redirect to another host.
- Token never written to disk except via `keyring`; never logged.
- State dir `~/.quercus-mcp/`, overridable via `QUERCUS_HOME`.
- Text per document capped at 2_000_000 chars; downloads skipped above `max_file_mb` (default 50).
- Soft delete: removed docs get `removed=1`, excluded from search.

## File structure

```
pyproject.toml
src/quercus_mcp/__init__.py            __version__
src/quercus_mcp/config.py              Paths, Config (toml), token get/set (keyring/env)
src/quercus_mcp/canvas/__init__.py
src/quercus_mcp/canvas/errors.py       CanvasError, AuthError, RateLimitError
src/quercus_mcp/canvas/client.py       CanvasClient (async): get, paginate, download, course endpoints
src/quercus_mcp/canvas/models.py       Course, Folder, File, Module, ModuleItem, Page, Assignment, DiscussionTopic (from_api)
src/quercus_mcp/extract/__init__.py    extract_text(path, content_type) -> ExtractResult
src/quercus_mcp/extract/html.py        html_to_markdown(html, link_rewriter) ; find_file_ids(html)
src/quercus_mcp/extract/pdf.py, pptx.py, docx.py
src/quercus_mcp/store/__init__.py
src/quercus_mcp/store/schema.sql
src/quercus_mcp/store/db.py            Store: open, upsert_course, upsert_document, mark_removed, search, get_document, list_documents, upcoming, announcements, sync_runs, meta
src/quercus_mcp/store/query.py         fts_query(user_query) -> str
src/quercus_mcp/sync/__init__.py
src/quercus_mcp/sync/discover.py       collect_file_ids(listing, module_items, html_bodies) -> set[int]
src/quercus_mcp/sync/crawler.py        Syncer: sync_all, sync_course, refresh_volatile
src/quercus_mcp/server.py              FastMCP tools + scheduler
src/quercus_mcp/cli.py                 typer app
tests/...
```

---

### Task 1: Project scaffold, config and token storage
**Files:** pyproject.toml, src/quercus_mcp/__init__.py, config.py, tests/test_config.py
**Produces:** `Paths(home).db / files_dir / text_dir / config_path`; `Config.load(paths) -> Config(base_url, sync_interval_minutes, volatile_ttl_minutes, max_file_mb, include_courses, exclude_courses, all_terms)`; `Config.save(paths)`; `get_token(host) -> str|None` (env `QUERCUS_TOKEN` first, then keyring); `set_token(host, token)`.
**Tests:** paths honour `QUERCUS_HOME`; config round-trips; env token wins over keyring (keyring monkeypatched).

### Task 2: Canvas errors + client core (auth, UA, pagination, backoff)
**Files:** canvas/errors.py, canvas/client.py, tests/canvas/test_client.py
**Produces:** `CanvasClient(base_url, token, *, max_concurrency=4, user_agent=...)` async context manager; `await client.get_json(path, params) -> Any`; `client.paginate(path, params) -> AsyncIterator[dict]` following `Link rel="next"`, `per_page=100` default; raises `AuthError` on 401, `CanvasError(status, body)` otherwise; retries 429/rate-limit-403/5xx with backoff (sleep injectable).
**Tests (respx):** bearer + UA headers sent; two-page Link traversal; 401 → AuthError; 429 then 200 → success with one sleep; 403 "Rate Limit Exceeded" retried; 403 other → CanvasError.

### Task 3: File download with redirect header stripping and public_url fallback
**Files:** canvas/client.py, tests/canvas/test_download.py
**Produces:** `await client.download_file(file_id, dest: Path, *, course_id=None) -> int` (bytes written). Follows redirects manually: same-host keeps Authorization, cross-host drops it. On 401/403 from download, calls `GET /api/v1/files/{id}/public_url`, fetches `public_url` bare. Raises `CanvasError` if both fail.
**Tests:** redirect to s3 host receives no Authorization header; 403 → public_url path; streamed content bytes match.

### Task 4: Canvas models + course endpoint helpers
**Files:** canvas/models.py, canvas/client.py, tests/canvas/test_models.py, tests/canvas/test_endpoints.py
**Produces:** dataclasses with `from_api(dict)`; client methods: `list_courses(include_completed=False)`, `get_course(id, syllabus=True)`, `list_folders(cid)`, `list_files(cid)` (returns `None` on 401 = tab hidden), `get_file(cid, fid)`, `list_modules(cid)` (fills items via fallback endpoint when omitted), `list_pages(cid)` (bodies included), `list_assignments(cid)`, `list_announcements(cid, start_date)`, `list_discussions(cid)`, `get_discussion_view(cid, tid)`.
**Tests:** parsing fixtures; files 401 → None; module items fallback fetch.

### Task 5: HTML → Markdown + file-id discovery
**Files:** extract/html.py, sync/discover.py, tests/extract/test_html.py, tests/sync/test_discover.py
**Produces:** `html_to_markdown(html: str, link_rewriter: Callable[[str], str]|None=None) -> str`; `find_file_ids(html) -> set[int]` matching `/courses/\d+/files/(\d+)` and `/files/(\d+)`; `collect_file_ids(listing_ids, module_items, html_bodies) -> set[int]`.
**Tests:** tags stripped, links preserved, Canvas file links rewritten; regex both forms; union/dedupe.

### Task 6: Binary extractors and dispatch
**Files:** extract/pdf.py, pptx.py, docx.py, extract/__init__.py, tests/extract/test_binary.py, tests/fixtures/{sample.pdf,sample.pptx,sample.docx} (generated in a fixture builder script `tests/fixtures/make_fixtures.py`)
**Produces:** `ExtractResult(status: Literal["ok","unsupported","error"], text: str, error: str|None)`; `extract_text(path: Path, content_type: str|None) -> ExtractResult`; page markers `--- page N ---` for PDF; slide markers for PPTX; 2M char cap.
**Tests:** each format yields known sentence; unknown ext → unsupported; corrupt file → error not exception.

### Task 7: Store: schema, upserts, search, queries
**Files:** store/schema.sql, store/db.py, store/query.py, tests/store/test_db.py, tests/store/test_query.py
**Produces:** `Store(path)`; `upsert_course(CourseRow)`; `upsert_document(DocumentRow) -> (doc_id, changed: bool)` (changed when `canvas_updated_at` or `text` differs); `set_document_text(doc_id, text, text_path, status, error)`; `mark_removed(course_id, kind, keep_canvas_ids)`; `search(q, course_id, kind, limit) -> list[Hit]` (AND then OR fallback, bm25 ranking, snippets); `get_document(doc_id)`; `list_documents(course_id, kind)`; `upcoming(days, course_id)`; `announcements(course_id, since, limit)`; `start_sync_run/finish_sync_run`; `meta_get/meta_set`; `fts_query("C++ pointers") == '"C++" "pointers"'`.
**Tests:** idempotent upsert; changed detection; removed excluded from search; ranking; sanitizer.

### Task 8: Crawler (sync engine)
**Files:** sync/crawler.py, tests/sync/test_crawler.py
**Produces:** `Syncer(client, store, config, paths)`; `await sync_all(full=False) -> SyncSummary(added, updated, removed, errors: list[str])`; `await sync_course(course, full)`; `await refresh_volatile(course_id)` (announcements + assignments only, updates meta `volatile:<cid>`); document kinds: file, page, syllabus, module, assignment, announcement, discussion.
**Tests:** fully mocked course → expected docs; second run no downloads; hidden Files tab still finds module files; locked file no download; oversized file skipped; AuthError propagates and marks run failed.

### Task 9: MCP server
**Files:** server.py, tests/server/test_tools.py
**Produces:** `build_server(store, syncer_factory, config) -> FastMCP`; tools per spec; auth-notice prefix; startup + interval scheduler (`asyncio` task) via `run_server()`.
**Tests:** in-memory client calls each tool against seeded store; auth notice appears when meta `last_auth_error` set.

### Task 10: CLI + docs
**Files:** cli.py, README.md, pyproject entry point
**Produces:** `quercus login|sync|status|serve|config`.
**Tests:** typer CliRunner for `status` and `config claude-desktop` output.

### Task 11: Integration test (gated) and end-to-end check
**Files:** tests/integration/test_live.py (skip unless `QUERCUS_INTEGRATION=1`).
Run full suite; run `quercus config`; smoke `quercus serve` via MCP inspector-less in-memory call.
