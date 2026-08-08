"""SQLite-backed store for work sessions.

Lives in the shared ``index.db`` alongside ``timeline_blocks`` and
``entries``. A session row tracks when a user was actively working and
carries the S2-reducer retry state (so retries survive a daemon
restart and the daily 23:55 safety-net can pick up unfinished work).
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from ..testing import failpoints

SessionStatus = Literal["active", "ended", "reduced", "failed"]

# Process-instance identity complements owner_pid: after a hard restart the OS
# can reuse the previous PID, but a fresh interpreter receives a new token.
_PROCESS_OWNER_TOKEN = uuid.uuid4().hex

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    flush_end TEXT,
    classified_end TEXT,
    classifier_terminal_pending INTEGER NOT NULL DEFAULT 0,
    classifier_terminal_entry_id TEXT NOT NULL DEFAULT '',
    classifier_terminal_path TEXT NOT NULL DEFAULT '',
    classifier_terminal_noop INTEGER NOT NULL DEFAULT 0,
    owner_pid INTEGER,
    owner_token TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_sessions_retry ON sessions(next_retry_at)
    WHERE status = 'failed';
CREATE TABLE IF NOT EXISTS session_schema_migrations (
    name TEXT PRIMARY KEY
);
"""

_TERMINAL_OBLIGATION_MIGRATION = "classifier-terminal-obligation-v1"


def _migrate(conn: sqlite3.Connection) -> None:
    """Backfill columns added after initial schema."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "flush_end" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN flush_end TEXT")
    if "classified_end" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN classified_end TEXT")
    if "classifier_terminal_pending" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN classifier_terminal_pending INTEGER NOT NULL DEFAULT 0"
        )
    if "classifier_terminal_entry_id" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN classifier_terminal_entry_id TEXT NOT NULL DEFAULT ''"
        )
    if "classifier_terminal_path" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN classifier_terminal_path TEXT NOT NULL DEFAULT ''"
        )
    if "classifier_terminal_noop" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN classifier_terminal_noop INTEGER NOT NULL DEFAULT 0"
        )
    if "owner_pid" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN owner_pid INTEGER")
    if "owner_token" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN owner_token TEXT")
    # The completion marker is written only after the idempotent repair. SQLite
    # DDL can commit before a process death; if that happens, the absent marker
    # makes the next open retry instead of permanently losing an obligation.
    migration_done = conn.execute(
        "SELECT 1 FROM session_schema_migrations WHERE name=?",
        (_TERMINAL_OBLIGATION_MIGRATION,),
    ).fetchone()
    if migration_done is not None:
        return
    rows = conn.execute(
        """
        SELECT id, start_time, end_time, classified_end FROM sessions
         WHERE status='reduced' AND end_time IS NOT NULL
           AND classifier_terminal_pending=0
        """
    ).fetchall()
    for row in rows:
        start = _parse_datetime(row["start_time"])
        end = _parse_datetime(row["end_time"])
        classified = _parse_datetime(row["classified_end"])
        if start is None or end is None:
            continue
        cursor = classified or start
        if _instant(cursor) < _instant(end):
            conn.execute(
                """
                UPDATE sessions
                   SET classifier_terminal_pending=1
                 WHERE id=? AND classifier_terminal_pending=0
                """,
                (row["id"],),
            )
    conn.execute(
        "INSERT OR IGNORE INTO session_schema_migrations(name) VALUES (?)",
        (_TERMINAL_OBLIGATION_MIGRATION,),
    )


@dataclass
class SessionRow:
    id: str
    start_time: datetime
    end_time: datetime | None = None
    status: SessionStatus = "active"
    retry_count: int = 0
    next_retry_at: datetime | None = None
    last_error: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    flush_end: datetime | None = None
    classified_end: datetime | None = None
    classifier_terminal_pending: bool = False
    classifier_terminal_entry_id: str = ""
    classifier_terminal_path: str = ""
    classifier_terminal_noop: bool = False
    owner_pid: int | None = None
    owner_token: str | None = None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)


def insert(conn: sqlite3.Connection, row: SessionRow) -> None:
    now = datetime.now().astimezone().isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO sessions
            (id, start_time, end_time, status, retry_count, next_retry_at,
             last_error, created_at, updated_at, owner_pid, owner_token)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.id,
            row.start_time.isoformat(),
            row.end_time.isoformat() if row.end_time else None,
            row.status,
            row.retry_count,
            row.next_retry_at.isoformat() if row.next_retry_at else None,
            row.last_error,
            (row.created_at or datetime.now().astimezone()).isoformat(),
            (row.updated_at or datetime.now().astimezone()).isoformat() or now,
            row.owner_pid,
            row.owner_token,
        ),
    )


def current_owner_token() -> str:
    """Stable identity for active sessions created by this interpreter."""
    return _PROCESS_OWNER_TOKEN


def mark_ended(conn: sqlite3.Connection, session_id: str, end_time: datetime) -> bool:
    """Atomically end an active session, clamping ``end_time`` to its start.

    The status predicate makes repeated recovery attempts idempotent.  The
    clamp protects the reducer's ``[start, end)`` invariant when the wall
    clock moves backwards between session start and daemon restart.
    """
    raw = conn.execute(
        "SELECT start_time FROM sessions WHERE id=? AND status='active'",
        (session_id,),
    ).fetchone()
    if raw is None:
        return False

    try:
        start_time = datetime.fromisoformat(raw[0])
    except (TypeError, ValueError):
        start_time = None
    if start_time is not None and _instant(end_time) < _instant(start_time):
        end_time = start_time

    failpoints.hit("session.status.before_ended")
    result = conn.execute(
        """
        UPDATE sessions
           SET end_time=?, status='ended', updated_at=?
         WHERE id=? AND status='active'
        """,
        (end_time.isoformat(), datetime.now().astimezone().isoformat(), session_id),
    )
    failpoints.hit("session.status.after_ended")
    return result.rowcount == 1


def mark_reduced(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    terminal_entry_id: str = "",
    terminal_path: str = "",
    terminal_noop: bool = False,
) -> None:
    failpoints.hit("session.status.before_reduced")
    conn.execute(
        """
        UPDATE sessions
           SET classifier_terminal_pending=CASE
                   WHEN status!='reduced'
                     OR classifier_terminal_entry_id<>?
                     OR classifier_terminal_path<>?
                     OR classifier_terminal_noop<>?
                   THEN 1 ELSE classifier_terminal_pending END,
               status='reduced',
               classifier_terminal_entry_id=?, classifier_terminal_path=?,
               classifier_terminal_noop=?,
               updated_at=?
         WHERE id=?
        """,
        (
            terminal_entry_id,
            terminal_path,
            1 if terminal_noop else 0,
            terminal_entry_id,
            terminal_path,
            1 if terminal_noop else 0,
            datetime.now().astimezone().isoformat(),
            session_id,
        ),
    )
    failpoints.hit("session.status.after_reduced")


def clear_classifier_terminal_pending(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    terminal_entry_id: str,
) -> bool:
    result = conn.execute(
        """
        UPDATE sessions
           SET classifier_terminal_pending=0, updated_at=?
         WHERE id=? AND classifier_terminal_pending=1
           AND classifier_terminal_entry_id=?
        """,
        (
            datetime.now().astimezone().isoformat(),
            session_id,
            terminal_entry_id,
        ),
    )
    return result.rowcount == 1


def backfill_classifier_terminal_intent(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    terminal_entry_id: str,
    terminal_path: str,
    terminal_noop: bool,
) -> bool:
    """Attach upgrade-time terminal evidence without rearming newer work."""
    if not terminal_path:
        raise ValueError("terminal classifier path is required")
    result = conn.execute(
        """
        UPDATE sessions
           SET classifier_terminal_entry_id=?, classifier_terminal_path=?,
               classifier_terminal_noop=?,
               updated_at=?
         WHERE id=? AND classifier_terminal_pending=1
           AND classifier_terminal_entry_id='' AND classifier_terminal_path=''
        """,
        (
            terminal_entry_id,
            terminal_path,
            1 if terminal_noop else 0,
            datetime.now().astimezone().isoformat(),
            session_id,
        ),
    )
    return result.rowcount == 1


def mark_failed(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    error: str,
    next_retry_at: datetime | None,
) -> None:
    failpoints.hit("session.status.before_failed")
    conn.execute(
        """
        UPDATE sessions
           SET status='failed',
               retry_count = retry_count + 1,
               next_retry_at=?,
               last_error=?,
               updated_at=?
         WHERE id=?
        """,
        (
            next_retry_at.isoformat() if next_retry_at else None,
            error,
            datetime.now().astimezone().isoformat(),
            session_id,
        ),
    )
    failpoints.hit("session.status.after_failed")


def get_by_id(conn: sqlite3.Connection, session_id: str) -> SessionRow | None:
    r = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return _to_row(r) if r else None


def get_open(conn: sqlite3.Connection) -> SessionRow | None:
    r = conn.execute(
        "SELECT * FROM sessions WHERE status='active' ORDER BY start_time DESC LIMIT 1"
    ).fetchone()
    return _to_row(r) if r else None


def set_flush_end(
    conn: sqlite3.Connection,
    session_id: str,
    flush_end: datetime,
) -> None:
    failpoints.hit("session.status.before_flush")
    conn.execute(
        "UPDATE sessions SET flush_end=?, updated_at=? WHERE id=?",
        (
            flush_end.isoformat(),
            datetime.now().astimezone().isoformat(),
            session_id,
        ),
    )
    failpoints.hit("session.status.after_flush")


def set_classified_end(
    conn: sqlite3.Connection,
    session_id: str,
    classified_end: datetime,
) -> None:
    conn.execute(
        "UPDATE sessions SET classified_end=?, updated_at=? WHERE id=?",
        (
            classified_end.isoformat(),
            datetime.now().astimezone().isoformat(),
            session_id,
        ),
    )


def list_active(conn: sqlite3.Connection) -> list[SessionRow]:
    rows = conn.execute(
        "SELECT * FROM sessions WHERE status='active' ORDER BY start_time ASC"
    ).fetchall()
    return [_to_row(r) for r in rows]


def list_reduced_needing_classification(conn: sqlite3.Connection) -> list[SessionRow]:
    """Return terminal sessions whose classifier bookmark has not reached the end.

    Comparisons happen on parsed instants rather than ISO text so rows spanning
    UTC-offset changes cannot be skipped by lexical ordering.
    """
    rows = conn.execute(
        """
        SELECT * FROM sessions
         WHERE status='reduced' AND end_time IS NOT NULL
         ORDER BY start_time ASC
        """
    ).fetchall()
    pending: list[SessionRow] = []
    for raw in rows:
        row = _to_row(raw)
        if row.end_time is None:
            continue
        cursor = row.classified_end or row.start_time
        if row.classifier_terminal_pending or _instant(cursor) < _instant(row.end_time):
            pending.append(row)
    return pending


def next_session_start_after(conn: sqlite3.Connection, start_time: datetime) -> datetime | None:
    """Return the next session start after ``start_time`` by absolute time.

    Recovery uses this as an upper bound so an orphan never claims timeline
    blocks belonging to a later session.  ``julianday`` avoids lexical ISO
    ordering bugs when UTC offsets differ across a daylight-saving change.
    """
    rows = conn.execute(
        """
        SELECT start_time FROM sessions
         WHERE julianday(start_time) > julianday(?) - 2
        """,
        (start_time.isoformat(),),
    ).fetchall()
    start_instant = _instant(start_time)
    candidates = [
        parsed
        for row in rows
        if (parsed := _parse_datetime(row[0])) is not None and _instant(parsed) > start_instant
    ]
    return min(candidates, key=_instant, default=None)


def latest_timeline_end_in_window(
    conn: sqlite3.Connection, *, start: datetime, end: datetime
) -> datetime | None:
    """Latest persisted block end intersecting ``[start, end)``.

    Timeline data is the best durable approximation of the last activity that
    made it to disk before a hard crash.  This query intentionally mirrors the
    reducer's interval-intersection semantics.
    """
    if _instant(end) <= _instant(start):
        return None
    from ..timeline import store as timeline_store

    rows = conn.execute(
        """
        SELECT id FROM timeline_blocks
         WHERE julianday(end_time) > julianday(?) - 2
           AND julianday(start_time) < julianday(?) + 2
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    start_instant = _instant(start)
    end_instant = _instant(end)
    candidates: list[datetime] = []
    for row in rows:
        block = timeline_store.get_by_id(conn, str(row[0] or ""))
        if block is None:
            continue
        if (
            _instant(block.end_time) > start_instant
            and _instant(block.start_time) < end_instant
        ):
            candidates.append(block.end_time)
    return max(candidates, key=_instant, default=None)


def list_due_for_retry(conn: sqlite3.Connection, *, now: datetime) -> list[SessionRow]:
    rows = conn.execute(
        """
        SELECT * FROM sessions
         WHERE status='failed'
        """,
    ).fetchall()
    now_instant = _instant(now)
    due = [
        row
        for raw in rows
        if (row := _to_row(raw)).next_retry_at is None
        or _instant(row.next_retry_at) <= now_instant
    ]
    due.sort(key=lambda row: _instant(row.start_time))
    return due


def list_unfinished_for_date(
    conn: sqlite3.Connection, *, day_start: datetime, day_end: datetime
) -> list[SessionRow]:
    """Sessions that started during [day_start, day_end) and aren't reduced."""
    rows = conn.execute(
        """
        SELECT * FROM sessions
         WHERE start_time >= ?
           AND start_time < ?
           AND status != 'reduced'
         ORDER BY start_time ASC
        """,
        (day_start.isoformat(), day_end.isoformat()),
    ).fetchall()
    return [_to_row(r) for r in rows]


def list_pending_reduction(conn: sqlite3.Connection) -> list[SessionRow]:
    """All non-reduced, non-active rows — the safety-net retry universe.

    Picks up ``ended`` rows whose reducer thread was killed mid-run
    (daemon shutdown) as well as ``failed`` rows. The reducer rechecks
    ``next_retry_at`` while holding the per-session lock, so future failed
    rows are enumerated here but do not consume another attempt early.
    """
    rows = conn.execute(
        """
        SELECT * FROM sessions
         WHERE status IN ('ended', 'failed')
           AND end_time IS NOT NULL
         ORDER BY start_time ASC
        """,
    ).fetchall()
    return [_to_row(r) for r in rows]


def earliest_pending_reduction_start(
    conn: sqlite3.Connection,
    *,
    max_session_hours: int,
) -> datetime | None:
    """Earliest evidence window still required by an ended/failed session.

    ISO strings with different offsets are not chronologically sortable, so
    candidates are parsed and compared as UTC instants.  A valid ``flush_end``
    is the reducer's actual resume point.  Corrupt legacy rows spanning longer
    than the configured hard session bound are clamped to their final bounded
    window so one row cannot force unbounded startup work.
    """
    rows = conn.execute(
        """
        SELECT start_time, end_time, flush_end FROM sessions
         WHERE status IN ('ended', 'failed')
           AND end_time IS NOT NULL
        """
    ).fetchall()
    bounded_hours = max(1, int(max_session_hours))
    candidates: list[datetime] = []
    for row in rows:
        start = _parse_datetime(row[0])
        end = _parse_datetime(row[1])
        flush_end = _parse_datetime(row[2])
        if start is None or end is None or _instant(end) < _instant(start):
            continue
        candidate = start
        if (
            flush_end is not None
            and _instant(start) < _instant(flush_end) < _instant(end)
        ):
            candidate = flush_end
        lower_bound = _add_elapsed(end, -timedelta(hours=bounded_hours))
        if _instant(candidate) < _instant(lower_bound):
            candidate = lower_bound
        candidates.append(candidate)
    return min(candidates, key=_instant, default=None)


def _to_row(r: sqlite3.Row) -> SessionRow:
    def _dt(v: str | None) -> datetime | None:
        if not v:
            return None
        try:
            return datetime.fromisoformat(v)
        except (TypeError, ValueError):
            return None

    # Older rows may not have flush_end / classified_end columns; PRAGMA
    # migration adds them but existing rows default to NULL (→ None).
    flush_end: datetime | None = None
    try:
        flush_end = _dt(r["flush_end"])
    except (IndexError, KeyError):
        flush_end = None
    classified_end: datetime | None = None
    try:
        classified_end = _dt(r["classified_end"])
    except (IndexError, KeyError):
        classified_end = None
    owner_pid: int | None = None
    try:
        owner_pid = int(r["owner_pid"]) if r["owner_pid"] is not None else None
    except (IndexError, KeyError, TypeError, ValueError):
        owner_pid = None
    owner_token: str | None = None
    try:
        owner_token = str(r["owner_token"]) if r["owner_token"] else None
    except (IndexError, KeyError, TypeError, ValueError):
        owner_token = None
    classifier_terminal_pending = False
    classifier_terminal_entry_id = ""
    classifier_terminal_path = ""
    classifier_terminal_noop = False
    try:
        classifier_terminal_pending = bool(r["classifier_terminal_pending"])
        classifier_terminal_entry_id = str(r["classifier_terminal_entry_id"] or "")
        classifier_terminal_path = str(r["classifier_terminal_path"] or "")
        classifier_terminal_noop = bool(r["classifier_terminal_noop"])
    except (IndexError, KeyError, TypeError, ValueError):
        pass
    return SessionRow(
        id=r["id"],
        start_time=_dt(r["start_time"]) or datetime.now().astimezone(),
        end_time=_dt(r["end_time"]),
        status=r["status"] or "active",
        retry_count=r["retry_count"] or 0,
        next_retry_at=_dt(r["next_retry_at"]),
        last_error=r["last_error"] or "",
        created_at=_dt(r["created_at"]),
        updated_at=_dt(r["updated_at"]),
        flush_end=flush_end,
        classified_end=classified_end,
        classifier_terminal_pending=classifier_terminal_pending,
        classifier_terminal_entry_id=classifier_terminal_entry_id,
        classifier_terminal_path=classifier_terminal_path,
        classifier_terminal_noop=classifier_terminal_noop,
        owner_pid=owner_pid,
        owner_token=owner_token,
    )


def _instant(value: datetime) -> datetime:
    """Normalize a datetime for safe comparisons across UTC offsets."""
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _add_elapsed(value: datetime, delta: timedelta) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value + delta
    return (_instant(value) + delta).astimezone(value.tzinfo)


def _parse_datetime(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
