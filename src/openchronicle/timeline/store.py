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
from pathlib import Path
from typing import Any

from .. import paths
from ..provenance.models import (
    EvidenceRef,
    legacy_observation_digest,
    observation_digest,
    timeline_block_projection_digest,
    timeline_block_sources_digest,
    timeline_capture_window_digest,
    timeline_window_receipt_digest,
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

-- Durable memory of coverage invalidated by a late capture. The active
-- watermark rewinds immediately, while this range lets every subsequent
-- replay (including after restart) distinguish an old certified-empty window
-- from an ordinary new frontier until replay safely catches up.
CREATE TABLE IF NOT EXISTS timeline_replay_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    inspected_from TEXT NOT NULL,
    inspected_through TEXT NOT NULL
);

-- A watermark proves only the capture snapshot that was visible when each
-- window was inspected.  Per-capture receipts let the producer distinguish a
-- retained, already-inspected JSON file from evidence that arrived later with
-- an older timestamp (for example after a small wall-clock correction or an
-- import).  The protocol is activated by the producer, not merely by schema
-- creation, so upgraded installations fail closed before their first replay.
CREATE TABLE IF NOT EXISTS timeline_capture_receipt_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL CHECK (enabled = 1)
);
CREATE TABLE IF NOT EXISTS timeline_capture_receipts (
    capture_path TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    capture_time TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    inspected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timeline_capture_receipt_time
    ON timeline_capture_receipts(julianday(capture_time), capture_path);

CREATE TABLE IF NOT EXISTS timeline_window_receipts (
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    window_start_us INTEGER NOT NULL,
    window_end_us INTEGER NOT NULL,
    capture_count INTEGER NOT NULL,
    capture_digest TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    outcome TEXT NOT NULL,
    block_id TEXT NOT NULL DEFAULT '',
    block_projection_digest TEXT NOT NULL DEFAULT '',
    block_source_digest TEXT NOT NULL DEFAULT '',
    raw_state TEXT NOT NULL DEFAULT 'live'
        CHECK (raw_state IN ('live', 'retiring', 'retired')),
    receipt_digest TEXT NOT NULL,
    inspected_at TEXT NOT NULL,
    PRIMARY KEY(window_start_us, window_end_us)
);
-- Changing the window duration while durable receipts exist would silently
-- reinterpret their boundaries.  The explicit timeline clean path resets
-- this epoch; ordinary ticks fail closed on a mismatch.
CREATE TABLE IF NOT EXISTS timeline_window_receipt_epoch (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    window_seconds INTEGER NOT NULL CHECK (window_seconds > 0)
);

-- Historical blocks without a root receipt are upgrade/recovery seeds. Audit
-- them incrementally instead of re-validating every retired receipt on every
-- minute tick. Live and retiring windows are still checked synchronously.
CREATE TABLE IF NOT EXISTS timeline_receipt_audit_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    block_rowid_cursor INTEGER NOT NULL CHECK (block_rowid_cursor >= 0)
);

CREATE TABLE IF NOT EXISTS timeline_schema_migrations (
    name TEXT PRIMARY KEY
);
"""

_PROJECTION_MIGRATION = "projection-source-v1"
_INSTANT_WINDOW_MIGRATION = "instant-window-unique-v1"
_INSTANT_WINDOW_INDEX = "idx_tlb_window_instant_unique"
_OBSERVATION_DIGEST_MIGRATION = "observation-semantic-digest-v2"
_WINDOW_RECEIPT_INSTANT_INDEX = "idx_timeline_window_receipt_instant_unique"
_WINDOW_RECEIPT_AUDIT_BATCH = 256


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


@dataclass(frozen=True, slots=True)
class WindowReceipt:
    window_start: datetime
    window_end: datetime
    capture_count: int
    capture_digest: str
    policy_digest: str
    outcome: str
    block_id: str = ""
    block_projection_digest: str = ""
    block_source_digest: str = ""
    raw_state: str = "live"
    receipt_digest: str = ""


def _make_id(start: datetime) -> str:
    stamp = start.strftime("%Y%m%d-%H%M")
    suffix = hashlib.blake2s(os.urandom(8), digest_size=2).hexdigest()
    return f"tlb-{stamp}-{suffix}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    block_columns = {row[1] for row in conn.execute("PRAGMA table_info(timeline_blocks)")}
    state_columns = {row[1] for row in conn.execute("PRAGMA table_info(timeline_state)")}
    receipt_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(timeline_window_receipts)")
    }
    migration_missing = (
        conn.execute(
            "SELECT 1 FROM timeline_schema_migrations WHERE name=?",
            (_PROJECTION_MIGRATION,),
        ).fetchone()
        is None
    )
    instant_migration_missing = (
        conn.execute(
            "SELECT 1 FROM timeline_schema_migrations WHERE name=?",
            (_INSTANT_WINDOW_MIGRATION,),
        ).fetchone()
        is None
    )
    instant_index_missing = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (_INSTANT_WINDOW_INDEX,),
        ).fetchone()
        is None
    )
    receipt_index_missing = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (_WINDOW_RECEIPT_INSTANT_INDEX,),
        ).fetchone()
        is None
    )
    if not (
        {"projection_digest", "source_digest"} - block_columns
        or "processed_from" not in state_columns
        or {"window_start_us", "window_end_us", "raw_state", "policy_digest"} - receipt_columns
        or migration_missing
        or instant_migration_missing
        or instant_index_missing
        or receipt_index_missing
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
        receipt_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(timeline_window_receipts)")
        }
        if "window_start_us" not in receipt_columns:
            conn.execute("ALTER TABLE timeline_window_receipts ADD COLUMN window_start_us INTEGER")
        if "window_end_us" not in receipt_columns:
            conn.execute("ALTER TABLE timeline_window_receipts ADD COLUMN window_end_us INTEGER")
        if "raw_state" not in receipt_columns:
            conn.execute(
                "ALTER TABLE timeline_window_receipts "
                "ADD COLUMN raw_state TEXT NOT NULL DEFAULT 'live'"
            )
        if "policy_digest" not in receipt_columns:
            # Draft receipts without a policy binding are never current and
            # are replayed from retained evidence.
            conn.execute(
                "ALTER TABLE timeline_window_receipts "
                "ADD COLUMN policy_digest TEXT NOT NULL DEFAULT ''"
            )
        _backfill_window_receipt_instant_keys(conn)
        conn.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {_WINDOW_RECEIPT_INSTANT_INDEX}
            ON timeline_window_receipts(window_start_us, window_end_us)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_timeline_window_receipt_live_start
            ON timeline_window_receipts(raw_state, window_start_us, window_end_us)
            """
        )
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
        if instant_migration_missing or instant_index_missing:
            _quarantine_semantic_window_duplicates(conn)
            conn.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS {_INSTANT_WINDOW_INDEX}
                ON timeline_blocks(julianday(start_time), julianday(end_time))
                WHERE projection_digest <> ''
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO timeline_schema_migrations(name) VALUES (?)",
                (_INSTANT_WINDOW_MIGRATION,),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def observation_digests_v2_migrated(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM timeline_schema_migrations WHERE name=?",
            (_OBSERVATION_DIGEST_MIGRATION,),
        ).fetchone()
        is not None
    )


def migrate_observation_digests_v2(conn: sqlite3.Connection) -> None:
    """Upgrade only fully-current retained bindings to semantic digest v2.

    The caller holds ``capture_store_lock`` before this function starts its
    SQLite transaction. Legacy non-timeline/Markdown references intentionally
    remain strict-fail-closed rather than receiving a permanent v1 bypass.
    """
    if observation_digests_v2_migrated(conn):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if observation_digests_v2_migrated(conn):
            conn.execute("COMMIT")
            return

        block_rows = conn.execute(
            """
            SELECT DISTINCT subject_id
              FROM provenance_edges
             WHERE subject_kind='timeline_block' AND subject_path=''
               AND source_kind='observation'
            """
        ).fetchall()
        for (raw_block_id,) in block_rows:
            if not isinstance(raw_block_id, str) or not raw_block_id:
                continue
            row = conn.execute(
                "SELECT * FROM timeline_blocks WHERE id=? LIMIT 1",
                (raw_block_id,),
            ).fetchone()
            block = _current_block(conn, row) if row is not None else None
            if block is None:
                # Never turn a projection/source mismatch into a trusted row.
                continue
            old_sources = _direct_sources(conn, raw_block_id)
            upgraded_sources: list[EvidenceRef] = []
            edge_updates: list[tuple[str, EvidenceRef]] = []
            eligible = True
            for source in old_sources:
                if source.kind != "observation":
                    upgraded_sources.append(source)
                    continue
                upgraded_hash = _trusted_v2_observation_binding(
                    observation_id=source.id,
                    capture_path=source.path,
                    capture_time=source.timestamp,
                    old_hash=source.content_hash,
                )
                if upgraded_hash is None:
                    eligible = False
                    break
                upgraded_sources.append(
                    EvidenceRef(
                        kind=source.kind,
                        id=source.id,
                        path=source.path,
                        timestamp=source.timestamp,
                        content_hash=upgraded_hash,
                    )
                )
                if upgraded_hash != source.content_hash:
                    edge_updates.append((upgraded_hash, source))
            if not eligible or not edge_updates:
                continue
            for upgraded_hash, source in edge_updates:
                conn.execute(
                    """
                    UPDATE provenance_edges
                       SET source_hash=?
                     WHERE subject_kind='timeline_block' AND subject_id=? AND subject_path=''
                       AND source_kind='observation' AND source_id=? AND source_path=?
                       AND source_timestamp=? AND source_hash=?
                    """,
                    (
                        upgraded_hash,
                        raw_block_id,
                        source.id,
                        source.path,
                        source.timestamp,
                        source.content_hash,
                    ),
                )
            block.source_digest = timeline_block_sources_digest(upgraded_sources)
            block.projection_digest = projection_digest(block)
            conn.execute(
                "UPDATE timeline_blocks SET source_digest=?, projection_digest=? WHERE id=?",
                (block.source_digest, block.projection_digest, raw_block_id),
            )

        conn.execute(
            "INSERT INTO timeline_schema_migrations(name) VALUES (?)",
            (_OBSERVATION_DIGEST_MIGRATION,),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _trusted_v2_observation_binding(
    *,
    observation_id: object,
    capture_path: object,
    capture_time: object,
    old_hash: object,
) -> str | None:
    if not all(
        isinstance(value, str) and value
        for value in (observation_id, capture_path, capture_time, old_hash)
    ):
        return None
    if Path(capture_path).name != capture_path:
        return None
    capture_file = paths.capture_buffer_dir() / capture_path
    if capture_file.is_symlink() or not capture_file.is_file():
        return None
    try:
        raw = json.loads(capture_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    allowed_keys = {
        "timestamp",
        "schema_version",
        "observation_id",
        "trigger",
        "window_meta",
        "privacy",
        "ax_tree",
        "ax_metadata",
        "focused_element",
        "visible_text",
        "url",
        "screenshot",
        "screenshot_stripped",
    }
    if not set(raw).issubset(allowed_keys):
        return None
    current_id = str(raw.get("observation_id") or f"legacy:{Path(capture_path).stem}")
    current_time = raw.get("timestamp")
    if current_id != observation_id or current_time != capture_time:
        return None
    from ..capture import filenames

    filename_time = filenames.parse_capture_stem(Path(capture_path).stem)
    payload_time = filenames.parse_timestamp(current_time)
    if (
        filename_time is None
        or payload_time is None
        or _instant(filename_time) != _instant(payload_time)
        or ("_obs_" in Path(capture_path).stem and not Path(capture_path).stem.endswith(current_id))
    ):
        return None
    new_hash = observation_digest(raw)
    if old_hash == new_hash:
        return new_hash
    return new_hash if old_hash == legacy_observation_digest(raw) else None


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


def _mapping_rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [str(item[0]) for item in cursor.description or ()]
    return [
        {
            name: row[name] if isinstance(row, sqlite3.Row) else row[index]
            for index, name in enumerate(columns)
        }
        for row in cursor.fetchall()
    ]


def _quarantine_semantic_window_duplicates(conn: sqlite3.Connection) -> None:
    """Bind at most one non-quarantined row to each absolute-time window."""
    groups: dict[tuple[datetime, datetime], list[dict[str, Any]]] = {}
    rows = _mapping_rows(
        conn.execute("SELECT * FROM timeline_blocks WHERE projection_digest <> '' ORDER BY id")
    )
    for row in rows:
        try:
            start = datetime.fromisoformat(row["start_time"])
            end = datetime.fromisoformat(row["end_time"])
            if not _duration_is_valid(start, end):
                raise ValueError("invalid timeline duration")
            key = (as_instant(start), as_instant(end))
        except (TypeError, ValueError):
            conn.execute(
                "UPDATE timeline_blocks SET projection_digest='' WHERE id=?",
                (row["id"],),
            )
            continue
        groups.setdefault(key, []).append(row)

    for duplicates in groups.values():
        if len(duplicates) < 2:
            continue
        fully_current = [row for row in duplicates if _current_block(conn, row) is not None]
        projection_valid = [row for row in duplicates if _row_projection_block(row) is not None]
        winner = min(
            fully_current or projection_valid or duplicates,
            key=lambda row: str(row["id"]),
        )
        conn.executemany(
            "UPDATE timeline_blocks SET projection_digest='' WHERE id=?",
            ((row["id"],) for row in duplicates if row["id"] != winner["id"]),
        )


def _backfill_window_receipt_instant_keys(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT rowid, window_start, window_end, raw_state FROM timeline_window_receipts"
    ).fetchall()
    groups: dict[tuple[datetime, datetime], list[tuple[int, datetime, datetime]]] = {}
    invalid_rowids: list[tuple[int]] = []
    for row in rows:
        try:
            start = datetime.fromisoformat(row[1])
            end = datetime.fromisoformat(row[2])
            if not _duration_is_valid(start, end) or row[3] not in {
                "live",
                "retiring",
                "retired",
            }:
                raise ValueError("invalid window receipt")
        except (TypeError, ValueError):
            invalid_rowids.append((row[0],))
            continue
        groups.setdefault((as_instant(start), as_instant(end)), []).append((row[0], start, end))

    # An old text-keyed draft schema admitted multiple offset spellings of one
    # absolute window. Delete every root in such a group before changing any
    # text key: canonicalizing row-by-row would collide with the old
    # ``PRIMARY KEY(window_start, window_end)`` midway through migration. No
    # member is a unique proof, so dropping the whole group is fail-closed and
    # lets retained child receipts or blocks seed replay.
    duplicate_rowids = [
        (rowid,) for group in groups.values() if len(group) > 1 for rowid, _start, _end in group
    ]
    conn.executemany(
        "DELETE FROM timeline_window_receipts WHERE rowid=?",
        [*invalid_rowids, *duplicate_rowids],
    )

    for group in groups.values():
        if len(group) != 1:
            continue
        rowid, start, end = group[0]
        conn.execute(
            """
            UPDATE timeline_window_receipts
               SET window_start=?, window_end=?, window_start_us=?, window_end_us=?
             WHERE rowid=?
            """,
            (
                _canonical_instant(start),
                _canonical_instant(end),
                _instant_us(start),
                _instant_us(end),
                rowid,
            ),
        )


def _semantic_window_rows(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Return every text spelling of one absolute-time window."""
    rows = _mapping_rows(
        conn.execute(
            """
            SELECT * FROM timeline_blocks
             WHERE julianday(start_time)=julianday(?)
               AND julianday(end_time)=julianday(?)
             ORDER BY id
            """,
            (start.isoformat(), end.isoformat()),
        )
    )
    expected = (as_instant(start), as_instant(end))
    semantic: list[dict[str, Any]] = []
    for row in rows:
        try:
            actual = (
                as_instant(datetime.fromisoformat(row["start_time"])),
                as_instant(datetime.fromisoformat(row["end_time"])),
            )
        except (TypeError, ValueError):
            continue
        if actual == expected:
            semantic.append(row)
    return semantic


def has_window(conn: sqlite3.Connection, start: datetime, end: datetime) -> bool:
    return bool(_semantic_window_rows(conn, start, end))


def window_state(conn: sqlite3.Connection, start: datetime, end: datetime) -> str:
    """Return ``missing``, ``current``, or fail-closed ``invalid``."""
    rows = _semantic_window_rows(conn, start, end)
    if not rows:
        return "missing"
    return "current" if any(_current_block(conn, row) is not None for row in rows) else "invalid"


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
    persisted = next(
        (
            parsed
            for row in _semantic_window_rows(conn, block.start_time, block.end_time)
            if (parsed := _row_projection_block(row)) is not None
        ),
        None,
    )
    if persisted is None:
        raise RuntimeError("timeline block insert did not produce a persisted window")
    return persisted, created


def get_window(conn: sqlite3.Connection, start: datetime, end: datetime) -> TimelineBlock | None:
    return next(
        (
            block
            for row in _semantic_window_rows(conn, start, end)
            if (block := _current_block(conn, row)) is not None
        ),
        None,
    )


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


def get_replay_range(conn: sqlite3.Connection) -> tuple[datetime, datetime] | None:
    row = conn.execute(
        "SELECT inspected_from, inspected_through FROM timeline_replay_state WHERE id=1"
    ).fetchone()
    if row is None:
        return None
    try:
        start = datetime.fromisoformat(row[0])
        end = datetime.fromisoformat(row[1])
    except (TypeError, ValueError):
        return None
    return (start, end) if _instant(start) <= _instant(end) else None


def remember_replay_range(
    conn: sqlite3.Connection,
    inspected_range: tuple[datetime, datetime],
) -> None:
    start, end = inspected_range
    existing = get_replay_range(conn)
    if existing is not None:
        start = min((start, existing[0]), key=_instant)
        end = max((end, existing[1]), key=_instant)
    conn.execute(
        """
        INSERT INTO timeline_replay_state(id, inspected_from, inspected_through)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            inspected_from=excluded.inspected_from,
            inspected_through=excluded.inspected_through
        """,
        (start.isoformat(), end.isoformat()),
    )


def clear_replay_range_if_covered(conn: sqlite3.Connection, through: datetime) -> None:
    replay_range = get_replay_range(conn)
    if replay_range is not None and _instant(through) >= _instant(replay_range[1]):
        conn.execute("DELETE FROM timeline_replay_state WHERE id=1")


def replay_range_covers_window(
    replay_range: tuple[datetime, datetime] | None,
    start: datetime,
    end: datetime,
) -> bool:
    return bool(
        replay_range is not None
        and _instant(replay_range[0]) <= _instant(start)
        and _instant(end) <= _instant(replay_range[1])
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


def activate_capture_receipts(conn: sqlite3.Connection) -> None:
    """Enable receipt-gated cleanup before publishing any new coverage."""
    conn.execute("INSERT OR IGNORE INTO timeline_capture_receipt_state(id, enabled) VALUES (1, 1)")


def capture_receipts_enabled(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM timeline_capture_receipt_state WHERE id=1 AND enabled=1"
        ).fetchone()
        is not None
    )


def capture_receipt_paths(conn: sqlite3.Connection) -> set[str]:
    """Return capture filenames already included in an inspected snapshot."""
    return {
        str(row[0])
        for row in conn.execute("SELECT capture_path FROM timeline_capture_receipts")
        if isinstance(row[0], str) and row[0]
    }


def capture_receipt_records(conn: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    """Return path -> (observation id, semantic hash, capture timestamp)."""
    records: dict[str, tuple[str, str, str]] = {}
    for row in conn.execute(
        "SELECT capture_path, observation_id, source_hash, capture_time "
        "FROM timeline_capture_receipts"
    ):
        if not all(isinstance(value, str) and value for value in row):
            continue
        values = tuple(row)
        if len(values) == 4:
            records[values[0]] = (values[1], values[2], values[3])
    return records


def unreceipted_capture_paths(
    conn: sqlite3.Connection,
    bindings: list[tuple[str, str, str, str]],
) -> set[str]:
    """Return paths whose exact semantic identity lacks a receipt."""
    if not bindings:
        return set()
    existing = capture_receipt_records(conn)
    return {
        path
        for path, observation_id, source_hash, capture_time in bindings
        if not all((path, observation_id, source_hash, capture_time))
        or existing.get(path) != (observation_id, source_hash, capture_time)
    }


def record_capture_receipts(
    conn: sqlite3.Connection,
    *,
    bindings: list[tuple[str, str, str, str]],
    window_start: datetime,
    window_end: datetime,
) -> None:
    """Commit exact capture receipts before the matching watermark advance."""
    if not bindings:
        return
    inspected_at = datetime.now().astimezone().isoformat()
    canonical_start = _canonical_instant(window_start)
    canonical_end = _canonical_instant(window_end)
    conn.execute(
        "DELETE FROM timeline_capture_receipts WHERE window_start=? AND window_end=?",
        (canonical_start, canonical_end),
    )
    conn.executemany(
        """
        INSERT INTO timeline_capture_receipts(
            capture_path, observation_id, source_hash, capture_time,
            window_start, window_end, inspected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(capture_path) DO UPDATE SET
            observation_id=excluded.observation_id,
            source_hash=excluded.source_hash,
            capture_time=excluded.capture_time,
            window_start=excluded.window_start,
            window_end=excluded.window_end,
            inspected_at=excluded.inspected_at
        """,
        (
            (
                capture_path,
                observation_id,
                source_hash,
                capture_time,
                canonical_start,
                canonical_end,
                inspected_at,
            )
            for capture_path, observation_id, source_hash, capture_time in bindings
        ),
    )


def make_window_receipt(
    *,
    window_start: datetime,
    window_end: datetime,
    bindings: list[tuple[str, str, str, str]],
    policy_digest: str,
    outcome: str,
    block: TimelineBlock | None = None,
    raw_state: str = "live",
) -> WindowReceipt:
    if outcome not in {"empty", "block", "policy_excluded"}:
        raise ValueError("unsupported timeline window outcome")
    if outcome == "block" and block is None:
        raise ValueError("block outcome requires a current block")
    if outcome != "block" and block is not None:
        raise ValueError("non-block outcome cannot bind a block")
    if raw_state not in {"live", "retiring", "retired"}:
        raise ValueError("unsupported raw capture lifecycle state")
    if not isinstance(policy_digest, str) or not policy_digest:
        raise ValueError("timeline receipt requires a capture policy digest")
    canonical_start = as_instant(window_start)
    canonical_end = as_instant(window_end)
    if not _duration_is_valid(canonical_start, canonical_end):
        raise ValueError("invalid timeline receipt window")
    capture_digest = timeline_capture_window_digest(bindings)
    block_id = block.id if block is not None else ""
    block_projection = str(block.projection_digest or "") if block is not None else ""
    block_sources = block.source_digest if block is not None else ""
    digest = timeline_window_receipt_digest(
        window_start=_canonical_instant(canonical_start),
        window_end=_canonical_instant(canonical_end),
        capture_count=len(bindings),
        capture_digest=capture_digest,
        policy_digest=policy_digest,
        outcome=outcome,
        block_id=block_id,
        block_projection_digest=block_projection,
        block_source_digest=block_sources,
        raw_state=raw_state,
    )
    return WindowReceipt(
        window_start=canonical_start,
        window_end=canonical_end,
        capture_count=len(bindings),
        capture_digest=capture_digest,
        policy_digest=policy_digest,
        outcome=outcome,
        block_id=block_id,
        block_projection_digest=block_projection,
        block_source_digest=block_sources,
        raw_state=raw_state,
        receipt_digest=digest,
    )


def record_window_receipt(conn: sqlite3.Connection, receipt: WindowReceipt) -> None:
    if receipt.outcome == "empty" and receipt.capture_count == 0:
        # Empty windows are already represented compactly by the contiguous
        # producer watermark. Persisting one row per minute would grow without
        # bound while adding no late-capture detection power.
        return
    if not _window_receipt_structure_is_valid(receipt):
        raise ValueError("invalid timeline window receipt")
    _raise_on_overlapping_window_receipt(conn, receipt.window_start, receipt.window_end)
    conn.execute(
        """
        INSERT INTO timeline_window_receipts(
            window_start, window_end, window_start_us, window_end_us,
            capture_count, capture_digest, policy_digest, outcome,
            block_id, block_projection_digest, block_source_digest,
            raw_state, receipt_digest, inspected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(window_start_us, window_end_us) DO UPDATE SET
            window_start=excluded.window_start,
            window_end=excluded.window_end,
            capture_count=excluded.capture_count,
            capture_digest=excluded.capture_digest,
            policy_digest=excluded.policy_digest,
            outcome=excluded.outcome,
            block_id=excluded.block_id,
            block_projection_digest=excluded.block_projection_digest,
            block_source_digest=excluded.block_source_digest,
            raw_state=excluded.raw_state,
            receipt_digest=excluded.receipt_digest,
            inspected_at=excluded.inspected_at
        """,
        (
            _canonical_instant(receipt.window_start),
            _canonical_instant(receipt.window_end),
            _instant_us(receipt.window_start),
            _instant_us(receipt.window_end),
            receipt.capture_count,
            receipt.capture_digest,
            receipt.policy_digest,
            receipt.outcome,
            receipt.block_id,
            receipt.block_projection_digest,
            receipt.block_source_digest,
            receipt.raw_state,
            receipt.receipt_digest,
            datetime.now().astimezone().isoformat(),
        ),
    )


def window_receipt_for(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
) -> WindowReceipt | None:
    rows = conn.execute(
        """
        SELECT * FROM timeline_window_receipts
         WHERE window_start_us=? AND window_end_us=?
        """,
        (_instant_us(start), _instant_us(end)),
    ).fetchall()
    parsed = [_row_to_window_receipt(row) for row in rows]
    valid = [receipt for receipt in parsed if receipt is not None]
    return valid[0] if len(rows) == 1 and len(valid) == 1 else None


def delete_window_receipt_for(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
) -> None:
    conn.execute(
        "DELETE FROM timeline_window_receipts WHERE window_start_us=? AND window_end_us=?",
        (_instant_us(start), _instant_us(end)),
    )


def window_receipt_is_current(conn: sqlite3.Connection, receipt: WindowReceipt) -> bool:
    if not _window_receipt_structure_is_valid(receipt):
        return False
    bindings: list[tuple[str, str, str, str]] = []
    if receipt.raw_state != "retired":
        bindings = capture_bindings_for_window(conn, receipt)
        if (
            len(bindings) != receipt.capture_count
            or timeline_capture_window_digest(bindings) != receipt.capture_digest
        ):
            return False
    state = window_state(conn, receipt.window_start, receipt.window_end)
    if receipt.outcome == "empty":
        return receipt.capture_count == 0 and state == "missing" and not receipt.block_id
    if receipt.outcome == "policy_excluded":
        return receipt.capture_count > 0 and state == "missing" and not receipt.block_id
    block = get_window(conn, receipt.window_start, receipt.window_end)
    if not (
        state == "current"
        and block is not None
        and block.id == receipt.block_id
        and block.projection_digest == receipt.block_projection_digest
        and block.source_digest == receipt.block_source_digest
    ):
        return False
    if receipt.raw_state == "retired":
        return True
    try:
        actual_sources = _direct_sources(conn, block.id)
    except (TypeError, ValueError):
        return False
    manifest_sources = {
        EvidenceRef(
            kind="observation",
            id=observation_id,
            path=capture_path,
            timestamp=capture_time,
            content_hash=source_hash,
        )
        for capture_path, observation_id, source_hash, capture_time in bindings
    }
    return bool(
        actual_sources
        and block.capture_count == len(actual_sources)
        and all(source in manifest_sources for source in actual_sources)
    )


def capture_bindings_for_window(
    conn: sqlite3.Connection,
    receipt: WindowReceipt,
) -> list[tuple[str, str, str, str]]:
    """Return the durable raw manifest retained while a window is live/retiring."""
    rows = conn.execute(
        """
        SELECT capture_path, observation_id, source_hash, capture_time
          FROM timeline_capture_receipts
         WHERE window_start=? AND window_end=?
         ORDER BY capture_path, observation_id, capture_time, source_hash
        """,
        (
            _canonical_instant(receipt.window_start),
            _canonical_instant(receipt.window_end),
        ),
    )
    bindings: list[tuple[str, str, str, str]] = []
    for row in rows:
        if not all(isinstance(value, str) and value for value in row):
            return []
        bindings.append((str(row[0]), str(row[1]), str(row[2]), str(row[3])))
    return bindings


def window_receipts_in_raw_states(
    conn: sqlite3.Connection,
    *raw_states: str,
) -> list[WindowReceipt]:
    if not raw_states or any(state not in {"live", "retiring", "retired"} for state in raw_states):
        return []
    placeholders = ",".join("?" for _ in raw_states)
    rows = conn.execute(
        "SELECT * FROM timeline_window_receipts "
        f"WHERE raw_state IN ({placeholders}) "
        "ORDER BY window_start_us, window_end_us",
        raw_states,
    )
    return [receipt for row in rows if (receipt := _row_to_window_receipt(row)) is not None]


def transition_window_receipt_raw_state(
    conn: sqlite3.Connection,
    receipt: WindowReceipt,
    raw_state: str,
) -> WindowReceipt:
    if raw_state not in {"live", "retiring", "retired"}:
        raise ValueError("unsupported raw capture lifecycle state")
    transitioned = make_window_receipt(
        window_start=receipt.window_start,
        window_end=receipt.window_end,
        bindings=(
            capture_bindings_for_window(conn, receipt) if receipt.raw_state != "retired" else []
        ),
        policy_digest=receipt.policy_digest,
        outcome=receipt.outcome,
        block=(
            get_window(conn, receipt.window_start, receipt.window_end)
            if receipt.outcome == "block"
            else None
        ),
        raw_state=raw_state,
    )
    # ``make_window_receipt`` cannot reconstruct a retired manifest. No
    # transition out of retired is permitted; replay publishes a fresh live
    # receipt instead.
    if receipt.raw_state == "retired":
        raise ValueError("retired receipt cannot transition in place")
    if (
        transitioned.capture_count != receipt.capture_count
        or transitioned.capture_digest != receipt.capture_digest
        or transitioned.policy_digest != receipt.policy_digest
        or transitioned.block_id != receipt.block_id
        or transitioned.block_projection_digest != receipt.block_projection_digest
        or transitioned.block_source_digest != receipt.block_source_digest
    ):
        raise ValueError("window receipt manifest or outcome changed during transition")
    result = conn.execute(
        """
        UPDATE timeline_window_receipts
           SET raw_state=?, receipt_digest=?, inspected_at=?
         WHERE window_start_us=? AND window_end_us=? AND receipt_digest=?
        """,
        (
            raw_state,
            transitioned.receipt_digest,
            datetime.now().astimezone().isoformat(),
            _instant_us(receipt.window_start),
            _instant_us(receipt.window_end),
            receipt.receipt_digest,
        ),
    )
    if result.rowcount != 1:
        raise RuntimeError("timeline window receipt changed during transition")
    return transitioned


def _window_receipt_structure_is_valid(receipt: WindowReceipt) -> bool:
    if (
        type(receipt.capture_count) is not int
        or receipt.capture_count < 0
        or not isinstance(receipt.capture_digest, str)
        or not receipt.capture_digest
        or not isinstance(receipt.policy_digest, str)
        or not receipt.policy_digest
        or receipt.outcome not in {"empty", "block", "policy_excluded"}
        or receipt.raw_state not in {"live", "retiring", "retired"}
        or not _duration_is_valid(receipt.window_start, receipt.window_end)
    ):
        return False
    block_fields = (
        receipt.block_id,
        receipt.block_projection_digest,
        receipt.block_source_digest,
    )
    if receipt.outcome == "block":
        if receipt.capture_count <= 0 or not all(
            isinstance(value, str) and value for value in block_fields
        ):
            return False
    elif (
        any(block_fields)
        or (receipt.outcome == "policy_excluded" and receipt.capture_count <= 0)
        or (
            receipt.outcome == "empty"
            and (
                receipt.capture_count != 0
                or receipt.capture_digest != timeline_capture_window_digest([])
            )
        )
    ):
        return False
    expected = timeline_window_receipt_digest(
        window_start=_canonical_instant(receipt.window_start),
        window_end=_canonical_instant(receipt.window_end),
        capture_count=receipt.capture_count,
        capture_digest=receipt.capture_digest,
        policy_digest=receipt.policy_digest,
        outcome=receipt.outcome,
        block_id=receipt.block_id,
        block_projection_digest=receipt.block_projection_digest,
        block_source_digest=receipt.block_source_digest,
        raw_state=receipt.raw_state,
    )
    return receipt.receipt_digest == expected


def _raise_on_overlapping_window_receipt(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
) -> None:
    row = conn.execute(
        """
        SELECT 1 FROM timeline_window_receipts
         WHERE window_start_us < ? AND window_end_us > ?
           AND NOT (window_start_us=? AND window_end_us=?)
         LIMIT 1
        """,
        (_instant_us(end), _instant_us(start), _instant_us(start), _instant_us(end)),
    ).fetchone()
    if row is not None:
        raise ValueError("timeline window receipt overlaps a different window epoch")


def earliest_invalidated_window(
    conn: sqlite3.Connection,
    processed_range: tuple[datetime, datetime] | None,
    *,
    policy_digest: str | None = None,
) -> datetime | None:
    if processed_range is None:
        return None
    processed_from, processed_through = processed_range
    range_start_us = _instant_us(processed_from)
    range_end_us = _instant_us(processed_through)

    # The producer can repair only windows whose raw manifest still exists.
    # Retired receipts authorize read-time evidence but have no raw captures to
    # replay, so rescanning and deeply validating the entire retired history on
    # every tick is both O(history) and unable to converge. Context readers
    # validate retired receipt/block/source bindings when evidence is used.
    receipt_rows = conn.execute(
        """
        SELECT * FROM timeline_window_receipts
         WHERE window_start_us >= ? AND window_end_us <= ?
           AND raw_state IN ('live', 'retiring')
         ORDER BY window_start_us, window_end_us
        """,
        (range_start_us, range_end_us),
    ).fetchall()
    receipts = [
        receipt for row in receipt_rows if (receipt := _row_to_window_receipt(row)) is not None
    ]
    candidates = [
        receipt.window_start
        for receipt in receipts
        if not window_receipt_is_current(conn, receipt)
        or (
            receipt.raw_state != "retired"
            and policy_digest is not None
            and receipt.policy_digest != policy_digest
        )
    ]
    if len(receipts) != len(receipt_rows):
        candidates.append(processed_from)

    # Upgrades can contain a historical block without its root outcome receipt.
    # A durable rowid cursor audits a fixed-size batch per tick. The hot path is
    # therefore bounded while a complete legacy scan still converges over time.
    # A semantic duplicate beside a rooted winner is not a gap because roots are
    # joined by absolute-time window rather than block identity.
    unrooted_block = _audit_unrooted_timeline_blocks(
        conn,
        processed_from=processed_from,
        processed_through=processed_through,
    )
    if unrooted_block is not None:
        candidates.append(unrooted_block)

    # Upgrades may have exact per-capture rows from an earlier producer but no
    # root outcome receipt. These sparse retained windows, not historical empty
    # minutes, are replay seeds.
    orphan_child = _earliest_unrooted_capture_receipt_window(
        conn,
        processed_from=processed_from,
        processed_through=processed_through,
    )
    if orphan_child is not None:
        candidates.append(orphan_child)
    return min(candidates, key=_instant, default=None)


def _audit_unrooted_timeline_blocks(
    conn: sqlite3.Connection,
    *,
    processed_from: datetime,
    processed_through: datetime,
) -> datetime | None:
    cursor_row = conn.execute(
        "SELECT block_rowid_cursor FROM timeline_receipt_audit_state WHERE id=1"
    ).fetchone()
    cursor = cursor_row[0] if cursor_row is not None else 0
    if type(cursor) is not int or cursor < 0:
        cursor = 0
    rows = conn.execute(
        """
        SELECT rowid, start_time, end_time
          FROM timeline_blocks
         WHERE rowid > ?
         ORDER BY rowid
         LIMIT ?
        """,
        (cursor, _WINDOW_RECEIPT_AUDIT_BATCH),
    ).fetchall()
    if not rows:
        _set_timeline_receipt_audit_cursor(conn, 0)
        return None

    range_start_us = _instant_us(processed_from)
    range_end_us = _instant_us(processed_through)
    parsed: list[tuple[int, datetime, int, int]] = []
    for row in rows:
        rowid = row[0]
        try:
            start = datetime.fromisoformat(row[1])
            end = datetime.fromisoformat(row[2])
            if type(rowid) is not int or rowid <= 0 or not _duration_is_valid(start, end):
                raise ValueError("invalid timeline block audit row")
        except (TypeError, ValueError):
            _set_timeline_receipt_audit_cursor(conn, max(0, int(rowid or 1) - 1))
            return processed_from
        start_us = _instant_us(start)
        end_us = _instant_us(end)
        if range_start_us <= start_us and end_us <= range_end_us:
            parsed.append((rowid, start, start_us, end_us))

    if parsed:
        values = ",".join("(?, ?, ?)" for _ in parsed)
        parameters: list[int] = []
        for rowid, _start, start_us, end_us in parsed:
            parameters.extend((rowid, start_us, end_us))
        missing_rowids = {
            int(row[0])
            for row in conn.execute(
                f"""
                WITH candidates(block_rowid, start_us, end_us) AS (
                    VALUES {values}
                )
                SELECT candidates.block_rowid
                  FROM candidates
                  LEFT JOIN timeline_window_receipts AS roots
                    ON roots.window_start_us = candidates.start_us
                   AND roots.window_end_us = candidates.end_us
                 WHERE roots.window_start_us IS NULL
                """,
                tuple(parameters),
            )
        }
        missing = [item for item in parsed if item[0] in missing_rowids]
        if missing:
            rowid, start, _start_us, _end_us = min(missing, key=lambda item: _instant(item[1]))
            _set_timeline_receipt_audit_cursor(conn, max(0, rowid - 1))
            return start

    _set_timeline_receipt_audit_cursor(conn, int(rows[-1][0]))
    return None


def _set_timeline_receipt_audit_cursor(conn: sqlite3.Connection, rowid: int) -> None:
    conn.execute(
        """
        INSERT INTO timeline_receipt_audit_state(id, block_rowid_cursor)
        VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET block_rowid_cursor=excluded.block_rowid_cursor
        """,
        (rowid,),
    )


def _earliest_unrooted_capture_receipt_window(
    conn: sqlite3.Connection,
    *,
    processed_from: datetime,
    processed_through: datetime,
) -> datetime | None:
    range_start_us = _instant_us(processed_from)
    range_end_us = _instant_us(processed_through)
    windows: dict[tuple[int, int], datetime] = {}
    for row in conn.execute(
        "SELECT DISTINCT window_start, window_end FROM timeline_capture_receipts"
    ):
        try:
            start = datetime.fromisoformat(row[0])
            end = datetime.fromisoformat(row[1])
            if not _duration_is_valid(start, end):
                raise ValueError("invalid capture receipt window")
        except (TypeError, ValueError):
            return processed_from
        start_us = _instant_us(start)
        end_us = _instant_us(end)
        if range_start_us <= start_us and end_us <= range_end_us:
            windows[(start_us, end_us)] = start

    ordered = sorted(
        ((start_us, end_us, start) for (start_us, end_us), start in windows.items()),
        key=lambda item: _instant(item[2]),
    )
    for offset in range(0, len(ordered), _WINDOW_RECEIPT_AUDIT_BATCH):
        batch = ordered[offset : offset + _WINDOW_RECEIPT_AUDIT_BATCH]
        values = ",".join("(?, ?, ?)" for _ in batch)
        parameters: list[int] = []
        for index, (start_us, end_us, _start) in enumerate(batch):
            parameters.extend((index, start_us, end_us))
        missing = conn.execute(
            f"""
            WITH candidates(position, start_us, end_us) AS (
                VALUES {values}
            )
            SELECT candidates.position
              FROM candidates
              LEFT JOIN timeline_window_receipts AS roots
                ON roots.window_start_us = candidates.start_us
               AND roots.window_end_us = candidates.end_us
             WHERE roots.window_start_us IS NULL
             ORDER BY candidates.position
             LIMIT 1
            """,
            tuple(parameters),
        ).fetchone()
        if missing is not None:
            return batch[int(missing[0])][2]
    return None


def activate_window_receipt_epoch(
    conn: sqlite3.Connection,
    window_minutes: int,
) -> bool:
    """Bind receipts to one duration; config changes require explicit clean."""
    seconds = max(1, int(window_minutes)) * 60
    row = conn.execute(
        "SELECT window_seconds FROM timeline_window_receipt_epoch WHERE id=1"
    ).fetchone()
    if row is not None:
        return type(row[0]) is int and row[0] == seconds

    durations: set[int] = set()
    for start_raw, end_raw in conn.execute("SELECT start_time, end_time FROM timeline_blocks"):
        try:
            start = datetime.fromisoformat(start_raw)
            end = datetime.fromisoformat(end_raw)
            duration = int((_instant(end) - _instant(start)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return False
        if duration <= 0:
            return False
        durations.add(duration)
    for start_raw, end_raw in conn.execute(
        "SELECT window_start, window_end FROM timeline_window_receipts"
    ):
        try:
            start = datetime.fromisoformat(start_raw)
            end = datetime.fromisoformat(end_raw)
            duration = int((_instant(end) - _instant(start)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return False
        if duration <= 0:
            return False
        durations.add(duration)
    if any(duration != seconds for duration in durations):
        return False
    conn.execute(
        "INSERT INTO timeline_window_receipt_epoch(id, window_seconds) VALUES (1, ?)",
        (seconds,),
    )
    return True


def _row_to_window_receipt(row: sqlite3.Row | tuple) -> WindowReceipt | None:
    try:
        receipt = WindowReceipt(
            window_start=datetime.fromisoformat(row["window_start"]),
            window_end=datetime.fromisoformat(row["window_end"]),
            capture_count=row["capture_count"],
            capture_digest=row["capture_digest"],
            policy_digest=row["policy_digest"],
            outcome=row["outcome"],
            block_id=row["block_id"],
            block_projection_digest=row["block_projection_digest"],
            block_source_digest=row["block_source_digest"],
            raw_state=row["raw_state"],
            receipt_digest=row["receipt_digest"],
        )
        if row["window_start_us"] != _instant_us(receipt.window_start) or row[
            "window_end_us"
        ] != _instant_us(receipt.window_end):
            return None
        return receipt
    except (KeyError, TypeError, ValueError):
        return None


def _instant(value: datetime) -> datetime:
    return as_instant(value)


def _canonical_instant(value: datetime) -> str:
    return as_instant(value).isoformat(timespec="microseconds")


def _instant_us(value: datetime) -> int:
    instant = as_instant(value)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = instant - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def as_instant(value: datetime) -> datetime:
    """Normalize a local/legacy datetime for ordering and elapsed arithmetic."""
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def add_elapsed(value: datetime, delta: timedelta) -> datetime:
    """Add real elapsed time while retaining the value's display timezone."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value + delta
    return (value.astimezone(UTC) + delta).astimezone(value.tzinfo)


def query_recent(conn: sqlite3.Connection, *, limit: int = 12) -> list[TimelineBlock]:
    """Most recent blocks, oldest first in the returned list."""
    requested = max(0, int(limit))
    if requested == 0:
        return []
    rows = conn.execute(
        "SELECT * FROM timeline_blocks ORDER BY julianday(start_time) DESC, id DESC"
    )
    blocks: list[TimelineBlock] = []
    for row in rows:
        block = _current_block(conn, row)
        if block is None:
            continue
        blocks.append(block)
        if len(blocks) >= requested:
            break
    blocks.sort(key=lambda block: (as_instant(block.start_time), block.id))
    return blocks


def query_since(conn: sqlite3.Connection, since: datetime) -> list[TimelineBlock]:
    """All blocks with end_time > ``since``, chronological order."""
    rows = conn.execute(
        "SELECT * FROM timeline_blocks "
        "WHERE julianday(end_time) > julianday(?) - 2 "
        "ORDER BY julianday(start_time) ASC, id ASC",
        (since.isoformat(),),
    ).fetchall()
    since_instant = as_instant(since)
    blocks = [
        block
        for row in rows
        if (block := _current_block(conn, row)) is not None
        and as_instant(block.end_time) > since_instant
    ]
    blocks.sort(key=lambda block: (as_instant(block.start_time), block.id))
    return blocks


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
    """Floor to a local-midnight-anchored elapsed window.

    Absolute-time arithmetic preserves both occurrences of an ambiguous local
    minute and skips nonexistent spring-forward minutes. At ordinary offsets
    this remains the familiar wall-clock floor (14:07:42 → 14:05 for w=5).
    """
    if window_minutes < 1 or window_minutes > int(MAX_BLOCK_DURATION.total_seconds() // 60):
        raise ValueError("window_minutes must be in [1, 1440]")
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    if moment.tzinfo is None or moment.utcoffset() is None:
        minutes_since_midnight = moment.hour * 60 + moment.minute
        floor_minutes = (minutes_since_midnight // window_minutes) * window_minutes
        return midnight + timedelta(minutes=floor_minutes)
    elapsed_seconds = (as_instant(moment) - as_instant(midnight)).total_seconds()
    window_seconds = window_minutes * 60
    floor_seconds = int(elapsed_seconds // window_seconds) * window_seconds
    return add_elapsed(midnight, timedelta(seconds=floor_seconds))


def floor_to_grid(
    moment: datetime,
    anchor: datetime,
    window_minutes: int,
) -> datetime:
    """Floor ``moment`` to a durable elapsed-time grid rooted at ``anchor``."""
    if window_minutes < 1 or window_minutes > int(MAX_BLOCK_DURATION.total_seconds() // 60):
        raise ValueError("window_minutes must be in [1, 1440]")
    window_seconds = window_minutes * 60
    elapsed_seconds = (as_instant(moment) - as_instant(anchor)).total_seconds()
    floor_steps = int(elapsed_seconds // window_seconds)
    return add_elapsed(anchor, timedelta(seconds=floor_steps * window_seconds))


def ceil_to_grid(
    moment: datetime,
    anchor: datetime,
    window_minutes: int,
) -> datetime:
    """Ceil ``moment`` to a durable elapsed-time grid rooted at ``anchor``."""
    floor = floor_to_grid(moment, anchor, window_minutes)
    if as_instant(floor) == as_instant(moment):
        return floor
    return add_elapsed(floor, timedelta(minutes=window_minutes))


def ceil_to_window(moment: datetime, window_minutes: int) -> datetime:
    """Ceil to the wall-clock boundary that proves ``moment`` was inspected."""
    floor = floor_to_window(moment, window_minutes)
    if as_instant(floor) == as_instant(moment):
        return floor
    return add_elapsed(floor, timedelta(minutes=window_minutes))


def iter_windows(
    start: datetime, end: datetime, window_minutes: int
) -> list[tuple[datetime, datetime]]:
    """Return the list of complete closed windows in ``[start, end)``.

    ``start`` is floored first; windows that would extend past ``end`` are
    not returned (partial trailing windows are left for a later tick).
    """
    cursor = floor_to_window(start, window_minutes)
    if as_instant(cursor) < as_instant(start):
        cursor = start
    step = timedelta(minutes=window_minutes)
    out: list[tuple[datetime, datetime]] = []
    while as_instant(add_elapsed(cursor, step)) <= as_instant(end):
        window_end = add_elapsed(cursor, step)
        out.append((cursor, window_end))
        cursor = window_end
    return out
