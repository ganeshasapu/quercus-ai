PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS courses (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    code             TEXT NOT NULL,
    term             TEXT,
    enrollment_state TEXT,
    start_at         TEXT,
    end_at           TEXT,
    files_tab_hidden INTEGER NOT NULL DEFAULT 0,
    active           INTEGER NOT NULL DEFAULT 1,
    last_synced_at   TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id       INTEGER NOT NULL REFERENCES courses(id),
    kind            TEXT NOT NULL,          -- file|page|syllabus|module|assignment|announcement|discussion
    canvas_id       TEXT NOT NULL,          -- file id, page url, assignment id, ... ('syllabus' for syllabus)
    title           TEXT NOT NULL,
    url             TEXT,
    folder_path     TEXT,
    module_id       INTEGER,
    module_name     TEXT,
    module_position INTEGER,
    content_type    TEXT,
    size            INTEGER,
    version_key     TEXT,                   -- changes whenever Canvas content changes
    due_at          TEXT,
    posted_at       TEXT,
    locked          INTEGER NOT NULL DEFAULT 0,
    local_path      TEXT,
    text_path       TEXT,
    extract_status  TEXT NOT NULL DEFAULT 'pending', -- pending|ok|unsupported|error|locked|skipped_size
    extract_error   TEXT,
    body            TEXT NOT NULL DEFAULT '',
    extra           TEXT NOT NULL DEFAULT '{}',      -- JSON: points, submitted, score, author, ...
    removed         INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    indexed_at      TEXT,
    UNIQUE (course_id, kind, canvas_id)
);

CREATE INDEX IF NOT EXISTS idx_documents_course_kind ON documents(course_id, kind, removed);
CREATE INDEX IF NOT EXISTS idx_documents_due ON documents(kind, due_at);
CREATE INDEX IF NOT EXISTS idx_documents_posted ON documents(kind, posted_at);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, body,
    content='documents', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE OF title, body ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO documents_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    scope       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running', -- running|ok|failed
    added       INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    errors      TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
