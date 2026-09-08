# quercus-mcp

Chat with your **Quercus** (University of Toronto's Canvas LMS) courses from Claude Desktop or
Claude Code.

`quercus-mcp` syncs everything you can see in your courses into a local cache, extracts the text
out of PDFs, slides and Word documents, indexes it all with SQLite full-text search, and exposes
it to Claude through the [Model Context Protocol](https://modelcontextprotocol.io).

What gets synced per course:

| Kind | Source | Notes |
|---|---|---|
| `file` | Files tab, module items, links inside pages/assignments/announcements/syllabus | PDF, PPTX, DOCX, text/code extracted; other types indexed by name |
| `page` | Course pages | HTML → Markdown |
| `syllabus` | Syllabus body | |
| `module` | Modules and their items | Outline with due dates and lock state |
| `assignment` | Assignments incl. your submission state | Refreshed live when stale |
| `announcement` | Announcements | Refreshed live when stale |
| `discussion` | Discussion topics and replies | |

Everything lives in `~/.quercus-mcp/` (override with `QUERCUS_HOME`): `quercus.db` (the index),
`files/<COURSE>/…` (raw downloads) and `text/<COURSE>/<kind>/*.md` (extracted Markdown you can
also open directly or point Claude Code at).

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> quercus-mcp && cd quercus-mcp
uv sync
uv tool install .          # puts a `quercus` command on your PATH
```

## Set up

1. In Quercus: **Account → Settings → Approved Integrations → “+ New Access Token”**. Give it a
   purpose and an expiry (Canvas requires students to pick one, max 120 days). Copy the token.
2. Store and verify it (kept in the macOS keychain, never on disk in plain text):

   ```bash
   quercus login --expires 2027-01-06
   ```

3. Do the first sync (a few minutes for a full term of PDFs):

   ```bash
   quercus sync
   quercus status
   ```

4. Register the server with your client:

   ```bash
   quercus config claude-desktop   # prints JSON for claude_desktop_config.json
   quercus config claude-code      # prints the `claude mcp add …` command
   ```

Restart Claude Desktop. Ask things like *"What does the CSC108 syllabus say about late
penalties?"*, *"Summarise week 3's slides for BIO120"*, or *"What's due this week?"*.

## Tools exposed to Claude

| Tool | Purpose |
|---|---|
| `list_courses` | Courses with document counts and last sync time |
| `search(query, course_id?, kind?, limit?)` | Ranked full-text search with snippets and `quercus://doc/<id>` ids |
| `list_documents(course_id, kind?, view?)` | Browse a course by module, folder or flat list |
| `read_document(doc_id, offset?, max_chars?)` | Read extracted text, paging through long files |
| `get_upcoming(days?, course_id?)` | Assignments due soon with submission state |
| `get_announcements(course_id?, since_days?, limit?)` | Recent announcements |
| `sync(course_id?, full?)` | Trigger a sync now |
| `sync_status` | Sync history, per-course state, token warnings |

The server also re-syncs in the background on startup and every 30 minutes (configurable), and
refreshes announcements/assignments live if the cache is older than 10 minutes when asked.

## Configuration

`~/.quercus-mcp/config.toml`:

```toml
base_url = "https://q.utoronto.ca"
sync_interval_minutes = 30
volatile_ttl_minutes = 10
max_file_mb = 50
include_courses = []      # course ids; empty = all active courses
exclude_courses = []
all_terms = false         # also sync completed courses
```

Environment: `QUERCUS_TOKEN` (overrides the keychain), `QUERCUS_HOME`.

## CLI

```
quercus login [--base-url URL] [--expires YYYY-MM-DD]
quercus sync [--course ID ...] [--full] [--all-terms] [--retry-errors]
quercus status
quercus serve                 # what the MCP client runs
quercus config claude-desktop|claude-code|show
quercus logout
```

## How it deals with Canvas quirks

- **Hidden Files tab.** Instructors often hide it; the listing then returns 401 for students. Files
  are still discovered through module items and links in pages, assignments, announcements and the
  syllabus, and fetched individually.
- **Downloads without verifier URLs.** Since mid-2026 Canvas file links need the bearer token. The
  client sends it to the Canvas host and drops it on the redirect to S3; if that fails it falls back
  to the `public_url` endpoint.
- **Locked / oversized / unsupported files** are indexed by title with a status you can see in
  `list_documents` and `read_document`.
- **Rate limits.** At most 4 concurrent requests, exponential backoff on 429/403 throttling, and a
  pause when the remaining quota gets low.
- **Expired token.** Student tokens expire within 120 days. The server keeps serving the cache and
  prefixes every answer with a warning until you run `quercus login` again.

## Development

```bash
uv sync
.venv/bin/pytest                          # unit tests (mocked Canvas)
QUERCUS_INTEGRATION=1 .venv/bin/pytest tests/integration -s   # against real Quercus
.venv/bin/python tests/fixtures/make_fixtures.py             # regenerate sample PDF/PPTX/DOCX
```

Design notes: `docs/superpowers/specs/2026-09-08-quercus-mcp-design.md`.

## Security notes

Your token grants full read/write access as you. It is stored only in the OS keychain (or the
`QUERCUS_TOKEN` env var) and is redacted from logs. This tool only ever issues GET requests.
Extracted course content is untrusted text; the server never executes it.
