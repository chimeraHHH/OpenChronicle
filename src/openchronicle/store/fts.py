"""SQLite FTS5 index for fast BM25 search over memory entries."""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths
from ..logger import get

logger = get("openchronicle.store")

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5(
    id UNINDEXED,
    path UNINDEXED,
    prefix UNINDEXED,
    timestamp UNINDEXED,
    tags,
    content,
    superseded UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    prefix TEXT,
    description TEXT,
    tags TEXT,
    status TEXT,
    entry_count INTEGER,
    created TEXT,
    updated TEXT,
    needs_compact INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_prefix ON files(prefix);

CREATE TABLE IF NOT EXISTS content_generations (
    scope TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO content_generations(scope, generation)
VALUES ('reducer', 0);
INSERT OR IGNORE INTO content_generations(scope, generation)
VALUES ('timeline', 0);

-- Mirrors capture-buffer/*.json S1 fields for keyword search. The JSON file on
-- disk stays authoritative for screenshots (not duplicated here). Populated
-- write-through from capture/scheduler; rows removed by cleanup_buffer when the
-- JSON is deleted. Screenshot-strip leaves this untouched (text unchanged).
CREATE TABLE IF NOT EXISTS captures (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    observation_id TEXT,
    timestamp TEXT NOT NULL,
    app_name TEXT,
    bundle_id TEXT,
    window_title TEXT,
    focused_role TEXT,
    focused_value TEXT,
    visible_text TEXT,
    url TEXT
);

CREATE INDEX IF NOT EXISTS idx_captures_ts  ON captures(timestamp);
CREATE INDEX IF NOT EXISTS idx_captures_app ON captures(app_name);

CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts USING fts5(
    app_name, window_title, focused_value, visible_text, url,
    content='captures', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS captures_ai AFTER INSERT ON captures BEGIN
    INSERT INTO captures_fts(rowid, app_name, window_title, focused_value, visible_text, url)
    VALUES (new.rowid, new.app_name, new.window_title, new.focused_value, new.visible_text, new.url);
END;
CREATE TRIGGER IF NOT EXISTS captures_ad AFTER DELETE ON captures BEGIN
    INSERT INTO captures_fts(captures_fts, rowid, app_name, window_title, focused_value, visible_text, url)
    VALUES ('delete', old.rowid, old.app_name, old.window_title, old.focused_value, old.visible_text, old.url);
END;
CREATE TRIGGER IF NOT EXISTS captures_au AFTER UPDATE ON captures BEGIN
    INSERT INTO captures_fts(captures_fts, rowid, app_name, window_title, focused_value, visible_text, url)
    VALUES ('delete', old.rowid, old.app_name, old.window_title, old.focused_value, old.visible_text, old.url);
    INSERT INTO captures_fts(rowid, app_name, window_title, focused_value, visible_text, url)
    VALUES (new.rowid, new.app_name, new.window_title, new.focused_value, new.visible_text, new.url);
END;
"""


@dataclass
class EntryHit:
    id: str
    path: str
    prefix: str
    timestamp: str
    tags: str
    content: str
    superseded: int
    rank: float


@dataclass
class FileRow:
    path: str
    prefix: str
    description: str
    tags: str
    status: str
    entry_count: int
    created: str
    updated: str
    needs_compact: int


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    if db_path is None:
        paths.ensure_dirs()
        db_path = paths.index_db()
    else:
        # A caller-provided database may live under a shared/system parent;
        # create a missing directory privately but never chmod an existing
        # arbitrary parent such as /tmp.
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(db_path, 0o600)
    else:
        os.close(fd)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Best-effort local erasure: overwrite deleted SQLite cells instead of
    # leaving plaintext in freelist pages. WAL truncation is requested after
    # explicit forget operations; filesystem snapshots remain outside this
    # process's guarantees.
    conn.execute("PRAGMA secure_delete=ON")
    # Make the auto-checkpoint pages explicit (this is also the SQLite default).
    # Auto-checkpoint resets the WAL pointer but never shrinks the file —
    # the daemon calls ``checkpoint()`` from the daily tick so the
    # ``.db-wal`` and ``.db-shm`` sidecars don't drift unbounded.
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.executescript(SCHEMA)
    _migrate_capture_schema(conn)
    from ..daily_wrap import store as daily_wrap_store
    from ..memory_candidates import store as candidate_store
    from ..prompt_rescue import store as prompt_rescue_store
    from ..provenance import store as provenance_store
    from ..reply_rescue import store as reply_rescue_store
    from ..resume_rescue import store as resume_rescue_store
    from ..session import store as session_store
    from ..suggestions import store as suggestion_store
    from ..timeline import store as timeline_store
    from ..writer import classifier_jobs

    timeline_store.ensure_schema(conn)
    session_store.ensure_schema(conn)
    provenance_store.ensure_schema(conn)
    candidate_store.ensure_schema(conn)
    prompt_rescue_store.ensure_schema(conn)
    reply_rescue_store.ensure_schema(conn)
    resume_rescue_store.ensure_schema(conn)
    daily_wrap_store.ensure_schema(conn)
    suggestion_store.ensure_schema(conn)
    classifier_jobs.ensure_schema(conn)
    _secure_db_files(db_path)
    return conn


def _migrate_capture_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(captures)")}
    if "observation_id" not in columns:
        conn.execute("ALTER TABLE captures ADD COLUMN observation_id TEXT")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_captures_observation
        ON captures(observation_id)
        WHERE observation_id IS NOT NULL AND observation_id <> ''
        """
    )


def _secure_db_files(db_path: Path) -> None:
    """Restrict the database and any live WAL sidecars to the current user."""
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.chmod(f"{db_path}{suffix}", 0o600)


@contextmanager
def cursor(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def checkpoint(mode: str = "TRUNCATE") -> tuple[int, int, int]:
    """Run ``PRAGMA wal_checkpoint(<mode>)`` and return (busy, log, checkpointed).

    ``TRUNCATE`` is the form that actually shrinks the ``.db-wal`` sidecar;
    ``PASSIVE`` (default in auto-checkpoint) only advances the read pointer
    without touching the file. Best invoked from a periodic tick when the
    daemon is otherwise quiet so we don't fight active readers.
    """
    valid = ("PASSIVE", "FULL", "RESTART", "TRUNCATE")
    mode = mode.upper()
    if mode not in valid:
        raise ValueError(f"invalid checkpoint mode {mode!r}; expected one of {valid}")
    with cursor() as conn:
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        if row is None:
            return (0, 0, 0)
        return (int(row[0]), int(row[1]), int(row[2]))


def content_generation(conn: sqlite3.Connection, scope: str) -> int:
    row = conn.execute(
        "SELECT generation FROM content_generations WHERE scope=?",
        (scope,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown content generation scope: {scope}")
    return int(row[0])


def bump_content_generation(conn: sqlite3.Connection, scope: str) -> int:
    result = conn.execute(
        "UPDATE content_generations SET generation=generation+1 WHERE scope=?",
        (scope,),
    )
    if result.rowcount != 1:
        raise ValueError(f"unknown content generation scope: {scope}")
    return content_generation(conn, scope)


# ─── files table ───────────────────────────────────────────────────────────


def upsert_file(conn: sqlite3.Connection, row: FileRow) -> None:
    conn.execute(
        """
        INSERT INTO files(path, prefix, description, tags, status, entry_count,
                          created, updated, needs_compact)
        VALUES (:path, :prefix, :description, :tags, :status, :entry_count,
                :created, :updated, :needs_compact)
        ON CONFLICT(path) DO UPDATE SET
            prefix=excluded.prefix,
            description=excluded.description,
            tags=excluded.tags,
            status=excluded.status,
            entry_count=excluded.entry_count,
            created=excluded.created,
            updated=excluded.updated,
            needs_compact=excluded.needs_compact
        """,
        row.__dict__,
    )


def get_file(conn: sqlite3.Connection, path: str) -> FileRow | None:
    r = conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
    return _to_file_row(r) if r else None


def list_files(
    conn: sqlite3.Connection,
    *,
    include_dormant: bool = False,
    include_archived: bool = False,
) -> list[FileRow]:
    statuses = ["active"]
    if include_dormant:
        statuses.append("dormant")
    if include_archived:
        statuses.append("archived")
    placeholders = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM files WHERE status IN ({placeholders}) ORDER BY updated DESC",
        statuses,
    ).fetchall()
    return [_to_file_row(r) for r in rows]


def set_needs_compact(conn: sqlite3.Connection, path: str, value: bool) -> None:
    conn.execute("UPDATE files SET needs_compact=? WHERE path=?", (1 if value else 0, path))


def files_needing_compact(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT path FROM files WHERE needs_compact=1").fetchall()
    return [r["path"] for r in rows]


def _to_file_row(r: sqlite3.Row) -> FileRow:
    return FileRow(
        path=r["path"],
        prefix=r["prefix"] or "",
        description=r["description"] or "",
        tags=r["tags"] or "",
        status=r["status"] or "active",
        entry_count=r["entry_count"] or 0,
        created=r["created"] or "",
        updated=r["updated"] or "",
        needs_compact=r["needs_compact"] or 0,
    )


# ─── entries (FTS5) ────────────────────────────────────────────────────────


def insert_entry(
    conn: sqlite3.Connection,
    *,
    id: str,
    path: str,
    prefix: str,
    timestamp: str,
    tags: str,
    content: str,
    superseded: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO entries(id, path, prefix, timestamp, tags, content, superseded)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (id, path, prefix, timestamp, tags, content, superseded),
    )


def mark_superseded(
    conn: sqlite3.Connection,
    entry_id: str,
    *,
    path: str,
    prefix: str,
    timestamp: str,
    tags: str,
    content: str,
) -> None:
    """Update the complete index projection for a superseded entry."""
    conn.execute(
        """
        UPDATE entries
           SET prefix=?, timestamp=?, tags=?, content=?, superseded=1
         WHERE id=? AND path=?
        """,
        (prefix, timestamp, tags, content, entry_id, path),
    )


def delete_entries_for(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM entries WHERE path=?", (path,))


def delete_file_row(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM files WHERE path=?", (path,))


_FTS5_SPECIALS = set('":*()^+-')


def _safe_fts_query(query: str) -> str:
    """Turn an LLM-written query into a safe FTS5 MATCH expression.

    FTS5 treats ``col:value``, quotes, parens, etc. as syntax. An LLM that
    writes ``"interview 20:00"`` otherwise crashes the search. We tokenize
    on whitespace, strip special chars from each token, and wrap every
    surviving token as a quoted phrase (implicit AND).
    """
    tokens: list[str] = []
    for raw in query.split():
        cleaned = "".join(c for c in raw if c not in _FTS5_SPECIALS)
        if cleaned:
            tokens.append(f'"{cleaned}"')
    return " ".join(tokens) if tokens else '""'


def search(
    conn: sqlite3.Connection,
    *,
    query: str,
    path_patterns: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    top_k: int = 5,
    offset: int = 0,
    include_superseded: bool = False,
) -> list[EntryHit]:
    safe_query = _safe_fts_query(query)
    if not safe_query or safe_query == '""':
        return []
    clauses = ["entries MATCH ?"]
    args: list[Any] = [safe_query]
    if path_patterns:
        path_clauses = []
        for pat in path_patterns:
            path_clauses.append("path GLOB ?")
            args.append(pat)
        clauses.append("(" + " OR ".join(path_clauses) + ")")
    if since is not None:
        clauses.append("timestamp >= ?")
        args.append(since)
    if until is not None:
        clauses.append("timestamp <= ?")
        args.append(until)
    if not include_superseded:
        clauses.append("superseded = 0")

    sql = (
        "SELECT id, path, prefix, timestamp, tags, content, superseded, "
        "       bm25(entries) AS rank "
        "FROM entries WHERE " + " AND ".join(clauses) + " ORDER BY rank, rowid LIMIT ? OFFSET ?"
    )
    args.extend((top_k, offset))
    rows = conn.execute(sql, args).fetchall()
    return [
        EntryHit(
            id=r["id"],
            path=r["path"],
            prefix=r["prefix"],
            timestamp=r["timestamp"],
            tags=r["tags"],
            content=r["content"],
            superseded=r["superseded"],
            rank=r["rank"],
        )
        for r in rows
    ]


# ─── captures (FTS5) ───────────────────────────────────────────────────────


@dataclass
class CaptureHit:
    """A captures-table row paired with its FTS rank + snippet."""

    id: str  # capture file stem
    timestamp: str
    app_name: str
    bundle_id: str
    window_title: str
    focused_role: str
    focused_value: str
    url: str
    snippet: str  # FTS5 snippet() with the matched tokens highlighted
    rank: float  # bm25 score (lower = better); 0.0 for non-search recent()
    observation_id: str = ""


def insert_capture(
    conn: sqlite3.Connection,
    *,
    id: str,
    timestamp: str,
    app_name: str,
    bundle_id: str,
    window_title: str,
    focused_role: str,
    focused_value: str,
    visible_text: str,
    url: str,
    observation_id: str = "",
) -> None:
    """Upsert one capture row. Triggers keep captures_fts in sync."""
    conn.execute(
        """
        INSERT INTO captures
            (id, observation_id, timestamp, app_name, bundle_id, window_title,
             focused_role, focused_value, visible_text, url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            observation_id=excluded.observation_id,
            timestamp=excluded.timestamp,
            app_name=excluded.app_name,
            bundle_id=excluded.bundle_id,
            window_title=excluded.window_title,
            focused_role=excluded.focused_role,
            focused_value=excluded.focused_value,
            visible_text=excluded.visible_text,
            url=excluded.url
        """,
        (
            id,
            observation_id or None,
            timestamp,
            app_name,
            bundle_id,
            window_title,
            focused_role,
            focused_value,
            visible_text,
            url,
        ),
    )


def delete_capture(conn: sqlite3.Connection, capture_id: str) -> None:
    conn.execute("DELETE FROM captures WHERE id=?", (capture_id,))


def search_captures(
    conn: sqlite3.Connection,
    *,
    query: str,
    since: str | None = None,
    until: str | None = None,
    app_name: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> list[CaptureHit]:
    """BM25 + snippet search over capture S1 fields.

    The ``app_name`` filter is a case-insensitive substring match on the
    ``captures.app_name`` column (not via FTS), so callers can filter by
    "Cursor" without competing for FTS slots.
    """
    safe_query = _safe_fts_query(query)
    if not safe_query or safe_query == '""':
        return []
    clauses = [
        "captures_fts MATCH ?",
        "NOT EXISTS ("
        "SELECT 1 FROM purge_tombstones AS p "
        "WHERE p.kind='capture_file' AND p.artifact_id=(c.id || '.json')"
        ")",
    ]
    args: list[Any] = [safe_query]
    if since is not None:
        clauses.append("c.timestamp >= ?")
        args.append(since)
    if until is not None:
        clauses.append("c.timestamp <= ?")
        args.append(until)
    if app_name:
        clauses.append("LOWER(c.app_name) LIKE ?")
        args.append(f"%{app_name.lower()}%")
    sql = (
        "SELECT c.id, c.observation_id, c.timestamp, c.app_name, c.bundle_id, c.window_title, "
        "       c.focused_role, c.focused_value, c.url, "
        "       snippet(captures_fts, -1, '[', ']', '…', 16) AS snippet, "
        "       bm25(captures_fts) AS rank "
        "  FROM captures c "
        "  JOIN captures_fts ON captures_fts.rowid = c.rowid "
        " WHERE " + " AND ".join(clauses) + " ORDER BY rank, c.rowid LIMIT ? OFFSET ?"
    )
    args.extend((limit, offset))
    rows = conn.execute(sql, args).fetchall()
    return [
        CaptureHit(
            id=r["id"],
            timestamp=r["timestamp"],
            app_name=r["app_name"] or "",
            bundle_id=r["bundle_id"] or "",
            window_title=r["window_title"] or "",
            focused_role=r["focused_role"] or "",
            focused_value=r["focused_value"] or "",
            url=r["url"] or "",
            snippet=r["snippet"] or "",
            rank=r["rank"],
            observation_id=r["observation_id"] or "",
        )
        for r in rows
    ]


def recent_captures(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    app_name: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[CaptureHit]:
    """Newest-first capture rows without keyword filtering — used by current_context."""
    clauses: list[str] = [
        "NOT EXISTS ("
        "SELECT 1 FROM purge_tombstones AS p "
        "WHERE p.kind='capture_file' AND p.artifact_id=(captures.id || '.json')"
        ")"
    ]
    args: list[Any] = []
    if since is not None:
        clauses.append("timestamp >= ?")
        args.append(since)
    if until is not None:
        clauses.append("timestamp <= ?")
        args.append(until)
    if app_name:
        clauses.append("LOWER(app_name) LIKE ?")
        args.append(f"%{app_name.lower()}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT id, observation_id, timestamp, app_name, bundle_id, window_title, "
        "       focused_role, focused_value, url "
        f"  FROM captures {where} "
        " ORDER BY timestamp DESC, rowid DESC LIMIT ? OFFSET ?"
    )
    args.extend((limit, offset))
    rows = conn.execute(sql, args).fetchall()
    return [
        CaptureHit(
            id=r["id"],
            timestamp=r["timestamp"],
            app_name=r["app_name"] or "",
            bundle_id=r["bundle_id"] or "",
            window_title=r["window_title"] or "",
            focused_role=r["focused_role"] or "",
            focused_value=r["focused_value"] or "",
            url=r["url"] or "",
            snippet="",
            rank=0.0,
            observation_id=r["observation_id"] or "",
        )
        for r in rows
    ]


def get_capture_visible_text(conn: sqlite3.Connection, capture_id: str) -> str:
    """Read just the visible_text field for a capture. Used by current_context."""
    r = conn.execute("SELECT visible_text FROM captures WHERE id=?", (capture_id,)).fetchone()
    return (r["visible_text"] if r else "") or ""


# ─── memory entries (FTS5) — read paths ────────────────────────────────────


def recent(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    limit: int = 20,
    offset: int = 0,
    prefix_filter: list[str] | None = None,
    include_superseded: bool = False,
) -> list[EntryHit]:
    clauses: list[str] = []
    args: list[Any] = []
    if since is not None:
        clauses.append("timestamp >= ?")
        args.append(since)
    if prefix_filter:
        placeholders = ",".join("?" * len(prefix_filter))
        clauses.append(f"prefix IN ({placeholders})")
        args.extend(prefix_filter)
    if not include_superseded:
        clauses.append("superseded = 0")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"SELECT id, path, prefix, timestamp, tags, content, superseded, "
        f"       0.0 AS rank FROM entries {where} "
        "ORDER BY timestamp DESC, rowid DESC LIMIT ? OFFSET ?"
    )
    args.extend((limit, offset))
    rows = conn.execute(sql, args).fetchall()
    return [
        EntryHit(
            id=r["id"],
            path=r["path"],
            prefix=r["prefix"],
            timestamp=r["timestamp"],
            tags=r["tags"],
            content=r["content"],
            superseded=r["superseded"],
            rank=0.0,
        )
        for r in rows
    ]
