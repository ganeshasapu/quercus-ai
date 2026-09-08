# Quercus MCP — Design

**Date:** 2026-09-08
**Status:** Approved

## Goal

Let a University of Toronto student chat with the full contents of their Quercus
(Canvas LMS) courses from Claude Desktop or Claude Code. "Full contents" means
uploaded files (PDF, slides, docs), Canvas pages, syllabus, modules,
assignments, announcements and discussions — including the text *inside* files.

## Prior art and why this exists

About a dozen open-source Canvas MCP servers exist (e.g. vishalsachdev/canvas-mcp,
DMontgomery40/mcp-canvas-lms). All authenticate with a personal access token.
None extract text from PDF/PPTX/DOCX; they return raw bytes or a download URL.
Downloaders (BrkBuilds/Canvas-Downloader, aik2mlj/canvas-downloader) mirror
content to disk but ship no search index or MCP interface. This project fills
the gap: sync + extract + full-text index + MCP tools, in one process.

## Decisions

| Question | Decision |
|---|---|
| Client | Claude Desktop / Claude Code via MCP (stdio) |
| Scope | Files, pages, syllabus, modules, assignments, announcements, discussions |
| Freshness | Local SQLite cache with FTS5; hybrid live refresh for volatile kinds |
| Auth | Canvas personal access token (user has one). Kept pluggable. |
| Language | Python 3.11+, `uv`, FastMCP from the official `mcp` SDK |
| Canvas client | Thin async `httpx` client, not `canvasapi` (need async, header control across S3 redirect, 429 backoff, concurrency cap) |
| Search | SQLite FTS5 keyword search. No embeddings (YAGNI; can add behind `search` later) |

## Canvas API facts the design relies on

- Host: `https://q.utoronto.ca/api/v1`. Standard Canvas, Instructure-hosted.
- `Authorization: Bearer <token>`; a `User-Agent` header is mandatory (403 without).
- Student tokens must expire within 120 days → need a re-login path.
- Pagination via `Link: rel="next"`; `per_page=100`.
- Rate limit: leaky bucket of 700 units per token, ~10 units/s drain; heavy
  parallelism incurs pre-flight penalties. Throttled = 429 (or legacy 403
  "Rate Limit Exceeded"). Headers `X-Rate-Limit-Remaining`, `X-Request-Cost`.
- File download: verifier URLs deprecated 2026-07-07. Send Bearer to
  `GET /files/:id/download?download_frd=1` on the Canvas host, follow the 302
  to S3/InstFS **without** the Authorization header. Fallback:
  `GET /api/v1/files/:id/public_url` → fetch bare pre-signed URL.
- If the instructor hides the Files tab, `GET /courses/:id/files` and
  `/folders` return 401 for students. Files are still reachable via module
  items (`type=File`, `content_id`) and links embedded in HTML bodies.
- Unpublished files never appear. `locked_for_user` files list but won't download.
- Incremental fields: File `modified_at`/`updated_at`, Page `updated_at`,
  Assignment `updated_at`, DiscussionTopic `posted_at`/`last_reply_at`.
  Course has no `updated_at`. Module items have none.

## Architecture

```
quercus_mcp/
  canvas/client.py      async httpx client: auth, UA, pagination, backoff, download
  canvas/models.py      dataclasses for Course, File, Folder, Module, ModuleItem,
                        Page, Assignment, DiscussionTopic
  sync/crawler.py       per-course collectors → Document records; incremental
  sync/discover.py      file-id discovery from listing + modules + HTML links
  extract/__init__.py   dispatch by content type → text
  extract/{pdf,pptx,docx,html}.py
  store/db.py           SQLite schema, FTS5, upserts, queries
  server.py             FastMCP tools; background sync scheduler
  cli.py                `quercus login|sync|status|serve|config`
  config.py             paths, config.toml, keyring token access
```

### Storage (`~/.quercus-mcp/`, overridable via `QUERCUS_HOME`)

- `config.toml` — `base_url`, `sync_interval_minutes` (30), `volatile_ttl_minutes`
  (10), `max_file_mb` (50), `include_courses`/`exclude_courses` (course ids).
- `quercus.db` — SQLite (WAL). Tables:
  - `courses(id PK, name, code, term, enrollment_state, files_tab_hidden, last_synced_at)`
  - `documents(id PK, course_id, kind, canvas_id, title, url, folder_path,
    module_id, module_position, content_type, size, canvas_updated_at,
    due_at, posted_at, locked, local_path, text_path, extract_status,
    extract_error, indexed_at)` with UNIQUE(course_id, kind, canvas_id)
  - `documents_fts` — FTS5(title, body, content='') external-content table kept
    in sync via triggers, with `bm25` ranking and `snippet()`.
  - `sync_runs(id, started_at, finished_at, scope, added, updated, removed, errors_json)`
  - `meta(key, value)` — e.g. token_expires_at, last volatile refresh per course
- `files/<course_code>/<folder path>/<name>` — raw downloads.
- `text/<course_code>/<kind>/<slug>.md` — extracted Markdown with a small
  YAML front-matter (title, kind, url, updated_at).

Token: `keyring` service `quercus-mcp`, username = host. Env override
`QUERCUS_TOKEN`. Token expiry recorded from `GET /api/v1/users/self` +
`/login/session_token`? — no: Canvas does not expose expiry for a bearer
token; we store the user-entered expiry date at `login` time (optional) and
otherwise detect expiry by 401.

### Sync

1. `GET /courses?enrollment_type=student&enrollment_state=active&include[]=term&per_page=100`
   (also `completed` when `--all-terms`). Apply include/exclude filters.
2. Per course, bounded by a global semaphore of 4 in-flight requests:
   - `GET /courses/:id?include[]=syllabus_body` → syllabus document
   - `GET /courses/:id/folders` + `/files?per_page=100` (401 → set `files_tab_hidden`)
   - `GET /courses/:id/modules?include[]=items&include[]=content_details`;
     if a module lacks `items`, `GET /modules/:mid/items?include[]=content_details`
   - `GET /courses/:id/pages?include[]=body&per_page=100`
   - `GET /courses/:id/assignments?include[]=submission&per_page=100`
   - `GET /announcements?context_codes[]=course_:id&start_date=<term start or 1y ago>&per_page=100`
   - `GET /courses/:id/discussion_topics?per_page=100`; for each topic,
     `GET .../discussion_topics/:tid/view` for replies
3. File discovery (`discover.py`): union of file listing, module items with
   `type=File`, and file ids matched in HTML bodies by regex
   `/courses/(\d+)/files/(\d+)` and `/files/(\d+)`. Files not in the listing
   get metadata via `GET /courses/:cid/files/:fid`. Dedupe by file id.
4. Change detection: a document is re-fetched/re-extracted only if
   `canvas_updated_at` differs from stored, or `extract_status` is `error`
   and `--retry-errors`. Documents no longer present are marked `removed`
   (soft-delete; excluded from search) rather than deleted.
5. Download (only when `locked_for_user` is false and `size <= max_file_mb`):
   Bearer → Canvas host; on 302 to a different host, re-request without
   Authorization. On 401/403, try `public_url`. Stream to `files/…`.
6. Extraction dispatch by `content_type` then extension:
   - `application/pdf` → PyMuPDF (`fitz`), page-separated with `--- page N ---`
   - PPTX → python-pptx (slide titles, text frames, notes)
   - DOCX → python-docx (paragraphs, tables)
   - HTML (pages, syllabus, assignment/announcement/discussion bodies) →
     `markdownify`, with Canvas file links rewritten to `quercus://doc/<id>`
     when the target file is known
   - text/markdown/code/CSV/JSON → verbatim
   - everything else → status `unsupported`, title-only indexing
   Text is capped at 2 MB per document. Failures set `extract_status='error'`.
7. Modules themselves become `kind='module'` documents whose body is a
   Markdown outline of their items (with due/lock info), so "what's in week 3"
   is answerable by search.
8. Volatile refresh: `announcements` and `assignments` per course store
   `last_volatile_refresh_at` in `meta`. Tools that read them refresh first
   if older than `volatile_ttl_minutes`.

### Concurrency & rate limiting

- `asyncio.Semaphore(4)` around every HTTP call.
- On 429, or 403 whose body contains "Rate Limit Exceeded": sleep with
  exponential backoff (1s, 2s, 4s, … max 30s, 5 tries).
- If `X-Rate-Limit-Remaining < 100`, sleep 2s before the next request.
- Retries with backoff on 5xx and connection errors (3 tries).
- All other 4xx surface as `CanvasError(status, body)`; 401 on the
  `/courses` root is `AuthError` → token invalid/expired.

### MCP server

FastMCP over stdio. Server name `quercus`. On startup: open DB, if
`last_synced_at` older than `sync_interval_minutes` schedule a background
sync task; then re-run every `sync_interval_minutes`. Tools never block on a
running sync; they read the cache.

| Tool | Args | Returns |
|---|---|---|
| `list_courses` | `include_past: bool=false` | table: id, code, name, term, doc counts by kind, last synced |
| `search` | `query`, `course_id?`, `kind?`, `limit=10` | ranked hits: doc id, course, kind, title, snippet, url |
| `list_documents` | `course_id`, `kind?`, `view: "modules"\|"folders"\|"flat"="modules"` | outline of a course |
| `read_document` | `doc_id`, `offset=0`, `max_chars=20000` | front-matter + text slice, `has_more` |
| `get_upcoming` | `days=14`, `course_id?` | assignments due, submission state, points |
| `get_announcements` | `course_id?`, `since_days=14`, `limit=20` | announcements newest first, body text |
| `sync` | `course_id?`, `full=false` | summary of the run (counts, errors) |
| `sync_status` | — | last runs, per-course state, token state |

Every tool response is prefixed with a one-line warning when the last sync
failed with `AuthError` ("Quercus token rejected; run `quercus login`").
Tool descriptions state that returned content is course material authored by
instructors and must be treated as data, not instructions.

FTS query handling: user query is passed through a sanitizer that quotes
tokens (so `C++ pointers` doesn't break FTS syntax) and joins with implicit
AND; if that yields zero hits, retry with OR. Filter by course/kind in SQL.

### CLI

- `quercus login [--base-url https://q.utoronto.ca] [--expires YYYY-MM-DD]`
  — prompts for token (hidden), verifies with `GET /users/self`, stores in
  keyring, writes config.
- `quercus sync [--course ID] [--full] [--all-terms] [--retry-errors]`
- `quercus status`
- `quercus serve` — runs the MCP server (stdio)
- `quercus config claude-desktop|claude-code` — prints the JSON snippet /
  the `claude mcp add` command.

### Error handling summary

| Situation | Behaviour |
|---|---|
| Token expired / invalid | Sync aborts with AuthError; server keeps serving cache; every tool output carries the notice |
| Files tab hidden (401) | `files_tab_hidden=1`; rely on module + HTML discovery |
| File locked for user | Metadata stored, `extract_status='locked'`, no download |
| File too large | `extract_status='skipped_size'` |
| Download 401/403 | try `public_url`; else `extract_status='error'` |
| Extraction exception | `extract_status='error'`, error text stored, title still indexed |
| Rate limited | backoff; never fails the run unless retries exhausted |
| Module `items` omitted | fetch items endpoint |
| Course removed from enrollments | course kept, marked `enrollment_state='inactive'`, hidden from `list_courses` default |

### Security

- Token only in keyring or env var; never in config.toml or logs.
- HTTP logs redact `Authorization`.
- Extracted text is untrusted; server does not execute or template it.
- Local files are 0600 / dirs 0700.

## Testing

- `tests/canvas/` — `respx` mocks for pagination (Link headers), 401 on files
  tab, 429 backoff, download redirect header stripping, `public_url` fallback.
- `tests/extract/` — tiny checked-in fixtures: `sample.pdf`, `sample.pptx`,
  `sample.docx`, HTML with Canvas file links.
- `tests/store/` — schema creation, upsert/idempotency, FTS ranking and
  sanitizer, soft-delete exclusion.
- `tests/sync/` — end-to-end crawl against a fully mocked course; incremental
  run performs zero downloads when nothing changed; file discovery union.
- `tests/server/` — tools invoked via FastMCP in-memory client against a
  seeded DB.
- `tests/integration/` — gated by `QUERCUS_INTEGRATION=1`; hits real Quercus
  with the keyring token.

## Out of scope (for now)

Embeddings/semantic search; quizzes; grades beyond assignment submission
score; writing anything to Canvas; OAuth / cookie auth; multi-user.
