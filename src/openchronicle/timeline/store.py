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

from ..provenance.models import (
    EvidenceRef,
    timeline_block_projection_digest,
    timeline_block_sources_digest,
)

MAX_BLOCK_DURATION = timedelta(days=1)

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
    source_digest TEXT NOT NULL DEFAULT '',
    projection_digest TEXT NOT NULL DEFAULT '',
    UNIQUE(start_time, end_time)
);
CREATE INDEX IF NOT EXISTS idx_tlb_start ON timeline_blocks(start_time);
CREATE INDEX IF NOT EXISTS idx_tlb_end ON timeline_blocks(end_time);
CREATE INDEX IF NOT EXISTS idx_tlb_start_jd_id
    ON timeline_blocks(julianday(start_time), id);
CREATE INDEX IF NOT EXISTS idx_tlb_end_jd_id
    ON timeline_blocks(julianday(end_time), id);

-- Durable producer watermark. Unlike MAX(timeline_blocks.end_time), this also
-- advances across closed windows with zero captures, letting consumers prove
-- that a missing block is truly empty rather than merely late.
CREATE TABLE IF NOT EXISTS timeline_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    processed_from TEXT,
    processed_through TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline_schema_migrations (
    name TEXT PRIMARY KEY
);
"""

_PROJECTION_MIGRATION = "projection-source-v1"


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
    source_digest: str = ""
    # ``None`` means a newly constructed trusted value and is initialized
    # below.  An empty string read from SQLite remains empty and fails closed.
    projection_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = _make_id(self.start_time)
        if self.created_at is None:
            self.created_at = datetime.now().astimezone()
        if self.projection_digest is None:
            self.projection_digest = projection_digest(self)


def _make_id(start: datetime) -> str:
    stamp = start.strftime("%Y%m%d-%H%M")
    suffix = hashlib.blake2s(os.urandom(8), digest_size=2).hexdigest()
    return f"tlb-{stamp}-{suffix}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    block_columns = {row[1] for row in conn.execute("PRAGMA table_info(timeline_blocks)")}
    state_columns = {row[1] for row in conn.execute("PRAGMA table_info(timeline_state)")}
    migration_missing = (
        conn.execute(
            "SELECT 1 FROM timeline_schema_migrations WHERE name=?",
            (_PROJECTION_MIGRATION,),
        ).fetchone()
        is None
    )
    if not (
        {"projection_digest", "source_digest"} - block_columns
        or "processed_from" not in state_columns
        or migration_missing
    ):
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Every schema decision is repeated after taking the writer lock. Two
        # upgrading processes can both observe the old schema, but only one
        # executes each transactional ALTER/backfill.
        block_columns = {row[1] for row in conn.execute("PRAGMA table_info(timeline_blocks)")}
        if "projection_digest" not in block_columns:
            # Trust existing rows exactly once at the explicit migration
            # boundary. Later blank values are never auto-healed.
            conn.execute(
                "ALTER TABLE timeline_blocks ADD COLUMN projection_digest TEXT NOT NULL DEFAULT ''"
            )
        if "source_digest" not in block_columns:
            conn.execute(
                "ALTER TABLE timeline_blocks ADD COLUMN source_digest TEXT NOT NULL DEFAULT ''"
            )
        state_columns = {row[1] for row in conn.execute("PRAGMA table_info(timeline_state)")}
        if "processed_from" not in state_columns:
            # An old upper bound cannot prove which earlier windows were
            # inspected. Leave the new lower bound NULL for safe recovery.
            conn.execute("ALTER TABLE timeline_state ADD COLUMN processed_from TEXT")
        if (
            conn.execute(
                "SELECT 1 FROM timeline_schema_migrations WHERE name=?",
                (_PROJECTION_MIGRATION,),
            ).fetchone()
            is None
        ):
            _backfill_projection_migration(conn)
            conn.execute(
                "INSERT INTO timeline_schema_migrations(name) VALUES (?)",
                (_PROJECTION_MIGRATION,),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _backfill_projection_migration(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, start_time, end_time, timezone, entries, apps_used,
               capture_count, created_at
          FROM timeline_blocks
         ORDER BY id
        """
    ).fetchall()
    for row in rows:
        try:
            if (
                not isinstance(row[0], str)
                or not row[0]
                or not isinstance(row[1], str)
                or not row[1]
                or not isinstance(row[2], str)
                or not row[2]
                or not isinstance(row[3], str)
                or not isinstance(row[4], str)
                or not row[4]
                or not isinstance(row[5], str)
                or not row[5]
                or not isinstance(row[7], str)
                or not row[7]
            ):
                continue
            entries = json.loads(row[4])
            apps = json.loads(row[5])
            start_value = datetime.fromisoformat(row[1])
            end_value = datetime.fromisoformat(row[2])
            created_at_value = datetime.fromisoformat(row[7])
            if not _duration_is_valid(start_value, end_value):
                continue
            start = start_value.isoformat()
            end = end_value.isoformat()
            created_at = created_at_value.isoformat()
            capture_count = row[6]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            type(capture_count) is not int
            or capture_count < 0
            or not isinstance(entries, list)
            or any(not isinstance(value, str) for value in entries)
            or not isinstance(apps, list)
            or any(not isinstance(value, str) for value in apps)
        ):
            continue
        try:
            sources = _direct_sources(conn, row[0])
        except (TypeError, ValueError):
            continue
        source_digest = timeline_block_sources_digest(sources) if sources else ""
        digest = timeline_block_projection_digest(
            block_id=row[0],
            start=start,
            end=end,
            timezone=row[3],
            entries=entries,
            apps=apps,
            capture_count=capture_count,
            created_at=created_at,
            source_digest=source_digest,
        )
        try:
            conn.execute(
                """
                UPDATE timeline_blocks
                   SET start_time=?, end_time=?, created_at=?,
                       source_digest=?, projection_digest=?
                 WHERE id=?
                """,
                (start, end, created_at, source_digest, digest, row[0]),
            )
        except sqlite3.IntegrityError:
            # Semantically duplicate legacy time spellings can coexist under
            # the old text UNIQUE key. Keep the colliding row unbound and
            # non-canonical so all ordinary reads quarantine it.
            continue


def has_window(conn: sqlite3.Connection, start: datetime, end: datetime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM timeline_blocks WHERE start_time=? AND end_time=? LIMIT 1",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return row is not None


def window_state(conn: sqlite3.Connection, start: datetime, end: datetime) -> str:
    """Return ``missing``, ``current``, or fail-closed ``invalid``."""
    row = conn.execute(
        "SELECT * FROM timeline_blocks WHERE start_time=? AND end_time=? LIMIT 1",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    if row is None:
        return "missing"
    return "current" if _current_block(conn, row) is not None else "invalid"


def insert(conn: sqlite3.Connection, block: TimelineBlock) -> None:
    if not _semantic_types_are_valid(block):
        raise ValueError("timeline block has invalid fields or duration")
    block.projection_digest = projection_digest(block)
    conn.execute(
        """
        INSERT OR IGNORE INTO timeline_blocks
            (id, start_time, end_time, timezone, entries, apps_used, capture_count,
             created_at, source_digest, projection_digest)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            block.source_digest,
            block.projection_digest,
        ),
    )


def insert_or_get(conn: sqlite3.Connection, block: TimelineBlock) -> tuple[TimelineBlock, bool]:
    """Insert a window once and return the identity that actually persisted."""
    before = conn.total_changes
    insert(conn, block)
    created = conn.total_changes > before
    row = conn.execute(
        "SELECT * FROM timeline_blocks WHERE start_time=? AND end_time=? LIMIT 1",
        (block.start_time.isoformat(), block.end_time.isoformat()),
    ).fetchone()
    persisted = _row_projection_block(row) if row else None
    if persisted is None:
        raise RuntimeError("timeline block insert did not produce a persisted window")
    return persisted, created


def get_window(conn: sqlite3.Connection, start: datetime, end: datetime) -> TimelineBlock | None:
    row = conn.execute(
        "SELECT * FROM timeline_blocks WHERE start_time=? AND end_time=? LIMIT 1",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return _current_block(conn, row) if row else None


def get_by_id(conn: sqlite3.Connection, block_id: str) -> TimelineBlock | None:
    """Return one exact timeline block for trusted evidence inspection."""
    row = conn.execute("SELECT * FROM timeline_blocks WHERE id=? LIMIT 1", (block_id,)).fetchone()
    return _current_block(conn, row) if row else None


def get_latest_end(conn: sqlite3.Connection) -> datetime | None:
    rows = conn.execute("SELECT * FROM timeline_blocks ORDER BY julianday(end_time) DESC")
    for row in rows:
        block = _current_block(conn, row)
        if block is not None:
            return block.end_time
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
    requested = max(0, int(limit))
    if requested == 0:
        return []
    rows = conn.execute("SELECT * FROM timeline_blocks ORDER BY start_time DESC")
    blocks: list[TimelineBlock] = []
    for row in rows:
        block = _current_block(conn, row)
        if block is None:
            continue
        blocks.append(block)
        if len(blocks) >= requested:
            break
    blocks.reverse()
    return blocks


def query_since(conn: sqlite3.Connection, since: datetime) -> list[TimelineBlock]:
    """All blocks with end_time > ``since``, chronological order."""
    rows = conn.execute(
        "SELECT * FROM timeline_blocks WHERE end_time > ? ORDER BY start_time ASC",
        (since.isoformat(),),
    ).fetchall()
    return [block for row in rows if (block := _current_block(conn, row)) is not None]


def _row_to_block(row: sqlite3.Row | tuple) -> TimelineBlock:
    # Row indexing works for both sqlite3.Row and tuple
    get = row.__getitem__
    entries_raw = get("entries")
    apps_raw = get("apps_used")
    start_raw = get("start_time")
    end_raw = get("end_time")
    created_at_raw = get("created_at")
    if (
        not isinstance(entries_raw, str)
        or not entries_raw
        or not isinstance(apps_raw, str)
        or not apps_raw
        or not isinstance(start_raw, str)
        or not start_raw
        or not isinstance(end_raw, str)
        or not end_raw
        or not isinstance(created_at_raw, str)
        or not created_at_raw
    ):
        raise ValueError("timeline projection fields must be non-empty strings")
    start = datetime.fromisoformat(start_raw)
    end = datetime.fromisoformat(end_raw)
    created_at = datetime.fromisoformat(created_at_raw)
    if (
        start_raw != start.isoformat()
        or end_raw != end.isoformat()
        or created_at_raw != created_at.isoformat()
    ):
        raise ValueError("timeline timestamps must use canonical ISO format")
    return TimelineBlock(
        id=get("id"),
        start_time=start,
        end_time=end,
        timezone=get("timezone"),
        entries=json.loads(entries_raw),
        apps_used=json.loads(apps_raw),
        capture_count=get("capture_count"),
        created_at=created_at,
        source_digest=get("source_digest"),
        projection_digest=get("projection_digest"),
    )


def projection_digest(block: TimelineBlock) -> str:
    """Return the immutable digest for a parsed timeline block."""
    return timeline_block_projection_digest(
        block_id=block.id,
        start=block.start_time.isoformat(),
        end=block.end_time.isoformat(),
        timezone=block.timezone,
        entries=block.entries,
        apps=block.apps_used,
        capture_count=block.capture_count,
        created_at=block.created_at.isoformat() if block.created_at else "",
        source_digest=block.source_digest,
    )


def projection_is_current(block: TimelineBlock) -> bool:
    """Fail closed when any persisted timeline projection field changed."""
    try:
        return bool(
            _semantic_types_are_valid(block)
            and block.projection_digest
            and block.projection_digest == projection_digest(block)
        )
    except (TypeError, ValueError):
        return False


def bind_sources(
    conn: sqlite3.Connection,
    block_id: str,
    sources: list[EvidenceRef],
) -> bool:
    """Set a block's immutable source binding exactly once."""
    if not sources:
        return False
    row = conn.execute(
        "SELECT * FROM timeline_blocks WHERE id=? LIMIT 1",
        (block_id,),
    ).fetchone()
    block = _row_projection_block(row) if row else None
    if block is None:
        return False
    digest = timeline_block_sources_digest(sources)
    if block.source_digest:
        return block.source_digest == digest
    old_projection = block.projection_digest
    block.source_digest = digest
    block.projection_digest = projection_digest(block)
    result = conn.execute(
        """
        UPDATE timeline_blocks
           SET source_digest=?, projection_digest=?
         WHERE id=? AND source_digest='' AND projection_digest=?
        """,
        (digest, block.projection_digest, block_id, old_projection),
    )
    return result.rowcount == 1


def sources_are_current(conn: sqlite3.Connection, block: TimelineBlock) -> bool:
    try:
        sources = _direct_sources(conn, block.id)
    except (TypeError, ValueError):
        return False
    return bool(
        sources
        and block.source_digest
        and block.source_digest == timeline_block_sources_digest(sources)
    )


def _semantic_types_are_valid(block: TimelineBlock) -> bool:
    return bool(
        isinstance(block.id, str)
        and block.id
        and isinstance(block.start_time, datetime)
        and isinstance(block.end_time, datetime)
        and isinstance(block.timezone, str)
        and isinstance(block.entries, list)
        and all(isinstance(value, str) for value in block.entries)
        and isinstance(block.apps_used, list)
        and all(isinstance(value, str) for value in block.apps_used)
        and type(block.capture_count) is int
        and block.capture_count >= 0
        and isinstance(block.created_at, datetime)
        and isinstance(block.source_digest, str)
        and isinstance(block.projection_digest, str)
        and _duration_is_valid(block.start_time, block.end_time)
    )


def _duration_is_valid(start: datetime, end: datetime) -> bool:
    try:
        duration = _instant(end) - _instant(start)
    except (TypeError, ValueError):
        return False
    return timedelta(0) < duration <= MAX_BLOCK_DURATION


def _direct_sources(conn: sqlite3.Connection, block_id: str) -> list[EvidenceRef]:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provenance_edges'"
    ).fetchone()
    if table is None:
        return []
    rows = conn.execute(
        """
        SELECT source_kind, source_id, source_path, source_timestamp, source_hash
          FROM provenance_edges
         WHERE subject_kind='timeline_block' AND subject_id=? AND subject_path=''
         ORDER BY ordinal, source_kind, source_path, source_id
        """,
        (block_id,),
    ).fetchall()
    return [
        EvidenceRef(
            kind=row[0],
            id=row[1],
            path=row[2],
            timestamp=row[3],
            content_hash=row[4],
        )
        for row in rows
    ]


def _row_projection_block(row: sqlite3.Row | tuple) -> TimelineBlock | None:
    try:
        block = _row_to_block(row)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return block if projection_is_current(block) else None


def _current_block(
    conn: sqlite3.Connection,
    row: sqlite3.Row | tuple,
) -> TimelineBlock | None:
    block = _row_projection_block(row)
    return block if block is not None and sources_are_current(conn, block) else None


def floor_to_window(moment: datetime, window_minutes: int) -> datetime:
    """Floor to the wall-clock window boundary. 14:07:42 → 14:05:00 (w=5)."""
    if window_minutes < 1 or window_minutes > int(MAX_BLOCK_DURATION.total_seconds() // 60):
        raise ValueError("window_minutes must be in [1, 1440]")
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since_midnight = moment.hour * 60 + moment.minute
    floor_minutes = (minutes_since_midnight // window_minutes) * window_minutes
    return midnight + timedelta(minutes=floor_minutes)


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
