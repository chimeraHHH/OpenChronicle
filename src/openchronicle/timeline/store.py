"""SQLite-backed store for timeline blocks (default 1-min wall-clock windows).

Lives in the shared ``index.db`` so users still have one file to back
up. The schema enforces a uniqueness constraint on
``(start_time, end_time)`` so the aggregator tick is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS timeline_blocks (
    id TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT '',
    entries TEXT NOT NULL,
    apps_used TEXT NOT NULL,
    capture_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(start_time, end_time)
);
CREATE INDEX IF NOT EXISTS idx_tlb_start ON timeline_blocks(start_time);
CREATE INDEX IF NOT EXISTS idx_tlb_end ON timeline_blocks(end_time);

-- Durable producer watermark. Unlike MAX(timeline_blocks.end_time), this also
-- advances across closed windows with zero captures, letting consumers prove
-- that a missing block is truly empty rather than merely late.
CREATE TABLE IF NOT EXISTS timeline_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    processed_from TEXT,
    processed_through TEXT NOT NULL
);
"""


@dataclass
class TimelineBlock:
    start_time: datetime
    end_time: datetime
    timezone: str = ""
    entries: list[str] = field(default_factory=list)
    apps_used: list[str] = field(default_factory=list)
    capture_count: int = 0
    id: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = _make_id(self.start_time)
        if self.created_at is None:
            self.created_at = datetime.now().astimezone()


def _make_id(start: datetime) -> str:
    stamp = start.strftime("%Y%m%d-%H%M")
    suffix = hashlib.blake2s(os.urandom(8), digest_size=2).hexdigest()
    return f"tlb-{stamp}-{suffix}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(timeline_state)")}
    if "processed_from" not in columns:
        # An upper bound created by an older build cannot prove which earlier
        # windows were actually inspected.  Leave the new lower bound NULL so
        # the producer safely reconstructs its coverage range.
        conn.execute("ALTER TABLE timeline_state ADD COLUMN processed_from TEXT")


def has_window(conn: sqlite3.Connection, start: datetime, end: datetime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM timeline_blocks WHERE start_time=? AND end_time=? LIMIT 1",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return row is not None


def insert(conn: sqlite3.Connection, block: TimelineBlock) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO timeline_blocks
            (id, start_time, end_time, timezone, entries, apps_used, capture_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            block.id,
            block.start_time.isoformat(),
            block.end_time.isoformat(),
            block.timezone,
            json.dumps(block.entries, ensure_ascii=False),
            json.dumps(block.apps_used, ensure_ascii=False),
            block.capture_count,
            (block.created_at or datetime.now().astimezone()).isoformat(),
        ),
    )


def insert_or_get(conn: sqlite3.Connection, block: TimelineBlock) -> tuple[TimelineBlock, bool]:
    """Insert a window once and return the identity that actually persisted."""
    before = conn.total_changes
    insert(conn, block)
    created = conn.total_changes > before
    persisted = get_window(conn, block.start_time, block.end_time)
    if persisted is None:
        raise RuntimeError("timeline block insert did not produce a persisted window")
    return persisted, created


def get_window(conn: sqlite3.Connection, start: datetime, end: datetime) -> TimelineBlock | None:
    row = conn.execute(
        "SELECT * FROM timeline_blocks WHERE start_time=? AND end_time=? LIMIT 1",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return _row_to_block(row) if row else None


def get_by_id(conn: sqlite3.Connection, block_id: str) -> TimelineBlock | None:
    """Return one exact timeline block for trusted evidence inspection."""
    row = conn.execute("SELECT * FROM timeline_blocks WHERE id=? LIMIT 1", (block_id,)).fetchone()
    return _row_to_block(row) if row else None


def get_latest_end(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        "SELECT end_time FROM timeline_blocks ORDER BY julianday(end_time) DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except (TypeError, ValueError):
        return None


def get_processed_through(conn: sqlite3.Connection) -> datetime | None:
    """Return the upper bound only when a complete coverage range is known."""
    processed_range = get_processed_range(conn)
    return processed_range[1] if processed_range is not None else None


def get_processed_range(
    conn: sqlite3.Connection,
) -> tuple[datetime, datetime] | None:
    """Return ``[processed_from, processed_through]`` or fail closed."""
    row = conn.execute(
        "SELECT processed_from, processed_through FROM timeline_state WHERE id=1"
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return None
    try:
        start = datetime.fromisoformat(row[0])
        end = datetime.fromisoformat(row[1])
    except (TypeError, ValueError):
        return None
    if _instant(start) > _instant(end):
        return None
    return start, end


def initialize_processed_range(conn: sqlite3.Connection, start: datetime) -> None:
    """Reset producer coverage to the safe recovery seed ``start``."""
    value = start.isoformat()
    conn.execute(
        """
        INSERT INTO timeline_state(id, processed_from, processed_through)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            processed_from=excluded.processed_from,
            processed_through=excluded.processed_through
        """,
        (value, value),
    )


def advance_processed_through(
    conn: sqlite3.Connection,
    value: datetime,
    *,
    window_start: datetime | None = None,
) -> None:
    """Extend a contiguous coverage range through ``value``.

    ``window_start`` is supplied by the producer and must not leave a gap after
    the current upper bound.  The optional form preserves the small public API
    used by tests/debug tooling and records a point range on first use.
    """
    processed_range = get_processed_range(conn)
    if processed_range is None:
        initialize_processed_range(conn, window_start or value)
        processed_range = get_processed_range(conn)
    assert processed_range is not None
    _processed_from, processed_through = processed_range
    start = window_start or processed_through
    if _instant(start) > _instant(processed_through):
        raise ValueError("timeline coverage cannot advance across an uninspected gap")
    if _instant(value) <= _instant(processed_through):
        return
    conn.execute(
        "UPDATE timeline_state SET processed_through=? WHERE id=1",
        (value.isoformat(),),
    )


def range_covers(
    processed_range: tuple[datetime, datetime] | None,
    moment: datetime,
) -> bool:
    """Return whether an inspected-window range proves an end boundary.

    ``processed_from`` is the start of the first inspected window, not an
    inspected end boundary itself, so coverage is ``(from, through]``.
    """
    if processed_range is None:
        return False
    start, end = processed_range
    instant = _instant(moment)
    return _instant(start) < instant <= _instant(end)


def covers(conn: sqlite3.Connection, moment: datetime) -> bool:
    """Return whether the durable producer range covers ``moment``."""
    return range_covers(get_processed_range(conn), moment)


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def query_recent(conn: sqlite3.Connection, *, limit: int = 12) -> list[TimelineBlock]:
    """Most recent blocks, oldest first in the returned list."""
    rows = conn.execute(
        "SELECT * FROM timeline_blocks ORDER BY start_time DESC LIMIT ?",
        (limit,),
    ).fetchall()
    blocks = [_row_to_block(r) for r in rows]
    blocks.reverse()
    return blocks


def query_since(conn: sqlite3.Connection, since: datetime) -> list[TimelineBlock]:
    """All blocks with end_time > ``since``, chronological order."""
    rows = conn.execute(
        "SELECT * FROM timeline_blocks WHERE end_time > ? ORDER BY start_time ASC",
        (since.isoformat(),),
    ).fetchall()
    return [_row_to_block(r) for r in rows]


def _row_to_block(row: sqlite3.Row | tuple) -> TimelineBlock:
    # Row indexing works for both sqlite3.Row and tuple
    get = row.__getitem__
    return TimelineBlock(
        id=get("id"),
        start_time=datetime.fromisoformat(get("start_time")),
        end_time=datetime.fromisoformat(get("end_time")),
        timezone=get("timezone") or "",
        entries=json.loads(get("entries") or "[]"),
        apps_used=json.loads(get("apps_used") or "[]"),
        capture_count=get("capture_count") or 0,
        created_at=datetime.fromisoformat(get("created_at")) if get("created_at") else None,
    )


def floor_to_window(moment: datetime, window_minutes: int) -> datetime:
    """Floor to the wall-clock window boundary. 14:07:42 → 14:05:00 (w=5)."""
    floor_min = (moment.minute // window_minutes) * window_minutes
    return moment.replace(minute=floor_min, second=0, microsecond=0)


def ceil_to_window(moment: datetime, window_minutes: int) -> datetime:
    """Ceil to the wall-clock boundary that proves ``moment`` was inspected."""
    floor = floor_to_window(moment, window_minutes)
    if floor == moment:
        return floor
    return floor + timedelta(minutes=window_minutes)


def iter_windows(
    start: datetime, end: datetime, window_minutes: int
) -> list[tuple[datetime, datetime]]:
    """Return the list of complete closed windows in ``[start, end)``.

    ``start`` is floored first; windows that would extend past ``end`` are
    not returned (partial trailing windows are left for a later tick).
    """
    cursor = floor_to_window(start, window_minutes)
    if cursor < start:
        cursor = start
    step = timedelta(minutes=window_minutes)
    out: list[tuple[datetime, datetime]] = []
    while cursor + step <= end:
        out.append((cursor, cursor + step))
        cursor = cursor + step
    return out
