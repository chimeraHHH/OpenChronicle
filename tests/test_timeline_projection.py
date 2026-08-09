from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from openchronicle import paths
from openchronicle.capture import filenames as capture_filenames
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef, timeline_block_sources_digest
from openchronicle.timeline import aggregator as timeline_aggregator
from openchronicle.timeline import store as timeline_store


def _bind_source(conn: sqlite3.Connection, block_id: str) -> None:
    provenance_store.replace_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id=block_id),
        sources=[
            EvidenceRef(
                kind="observation",
                id=f"source-{block_id}",
                path=f"source-{block_id}.json",
                content_hash=f"digest-{block_id}",
            )
        ],
    )


def test_legacy_timeline_projection_is_bound_once_and_never_auto_healed() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE timeline_blocks (
            id TEXT PRIMARY KEY,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT '',
            entries TEXT NOT NULL,
            apps_used TEXT NOT NULL,
            capture_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(start_time, end_time)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO timeline_blocks(
            id, start_time, end_time, timezone, entries, apps_used,
            capture_count, created_at
        ) VALUES (
            'tlb-legacy',
            '2026-08-08T10:00:00Z',
            '2026-08-08T10:01:00Z',
            'UTC',
            '["Legacy canonical entry"]',
            '["Editor"]',
            1,
            '2026-08-08T10:01:00Z'
        )
        """
    )

    provenance_store.ensure_schema(conn)
    provenance_store.record_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id="tlb-legacy"),
        sources=[
            EvidenceRef(
                kind="observation",
                id="source-tlb-legacy",
                path="source-tlb-legacy.json",
                content_hash="digest-tlb-legacy",
            )
        ],
    )
    timeline_store.ensure_schema(conn)
    migrated = timeline_store.get_by_id(conn, "tlb-legacy")
    assert migrated is not None
    assert migrated.start_time.isoformat() == "2026-08-08T10:00:00+00:00"
    assert (
        timeline_store.window_state(
            conn,
            datetime(2026, 8, 8, 10, 0, tzinfo=UTC),
            datetime(2026, 8, 8, 10, 1, tzinfo=UTC),
        )
        == "current"
    )
    timeline_store.insert(
        conn,
        timeline_store.TimelineBlock(
            id="tlb-equivalent-window",
            start_time=datetime(2026, 8, 8, 10, 0, tzinfo=UTC),
            end_time=datetime(2026, 8, 8, 10, 1, tzinfo=UTC),
        ),
    )
    assert conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0] == 1

    conn.execute(
        "UPDATE timeline_blocks SET entries='[\"TAMPERED_AFTER_MIGRATION\"]' WHERE id='tlb-legacy'"
    )
    timeline_store.ensure_schema(conn)

    assert timeline_store.get_by_id(conn, "tlb-legacy") is None
    conn.close()


def test_migration_quarantines_semantically_duplicate_offset_windows() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE timeline_blocks (
            id TEXT PRIMARY KEY,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT '',
            entries TEXT NOT NULL,
            apps_used TEXT NOT NULL,
            capture_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(start_time, end_time)
        )
        """
    )
    rows = (
        (
            "tlb-fixed-legacy",
            "2026-03-08T02:00:00-05:00",
            "2026-03-08T02:01:00-05:00",
        ),
        (
            "tlb-zone-legacy",
            "2026-03-08T03:00:00-04:00",
            "2026-03-08T03:01:00-04:00",
        ),
    )
    for block_id, start, end in rows:
        conn.execute(
            """
            INSERT INTO timeline_blocks(
                id, start_time, end_time, timezone, entries, apps_used,
                capture_count, created_at
            ) VALUES (?, ?, ?, 'America/New_York', '["legacy"]', '["Editor"]', 1, ?)
            """,
            (block_id, start, end, end),
        )
    provenance_store.ensure_schema(conn)
    for block_id, _start, _end in rows:
        provenance_store.record_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block_id),
            sources=[
                EvidenceRef(
                    kind="observation",
                    id=f"source-{block_id}",
                    path=f"source-{block_id}.json",
                    content_hash=f"digest-{block_id}",
                )
            ],
        )

    timeline_store.ensure_schema(conn)

    projections = conn.execute(
        "SELECT id, projection_digest FROM timeline_blocks ORDER BY id"
    ).fetchall()
    assert [bool(row["projection_digest"]) for row in projections] == [True, False]
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 3, 8, 3, 0, tzinfo=zone)
    end = start + timedelta(minutes=1)
    assert timeline_store.window_state(conn, start, end) == "current"
    assert timeline_store.get_window(conn, start, end).id == "tlb-fixed-legacy"
    assert conn.execute(
        "SELECT 1 FROM timeline_schema_migrations WHERE name='instant-window-unique-v1'"
    ).fetchone()
    conn.close()


def test_legacy_window_receipt_migration_drops_offset_equivalent_roots_first() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Model the text-keyed pre-release draft. Its primary key permits two
    # offset spellings of the same absolute window, but would reject the
    # second row if migration canonicalized them one at a time.
    conn.execute(
        """
        CREATE TABLE timeline_window_receipts (
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            capture_count INTEGER NOT NULL,
            capture_digest TEXT NOT NULL,
            outcome TEXT NOT NULL,
            block_id TEXT NOT NULL DEFAULT '',
            block_projection_digest TEXT NOT NULL DEFAULT '',
            block_source_digest TEXT NOT NULL DEFAULT '',
            receipt_digest TEXT NOT NULL,
            inspected_at TEXT NOT NULL,
            PRIMARY KEY(window_start, window_end)
        )
        """
    )
    rows = (
        ("2026-03-08T02:00:00-05:00", "2026-03-08T02:01:00-05:00"),
        ("2026-03-08T03:00:00-04:00", "2026-03-08T03:01:00-04:00"),
        ("2026-03-08T04:00:00-04:00", "2026-03-08T04:01:00-04:00"),
    )
    conn.executemany(
        """
        INSERT INTO timeline_window_receipts(
            window_start, window_end, capture_count, capture_digest,
            outcome, receipt_digest, inspected_at
        ) VALUES (?, ?, 1, 'capture-digest', 'policy_excluded',
                  'receipt-digest', ?)
        """,
        ((start, end, end) for start, end in rows),
    )

    timeline_store.ensure_schema(conn)
    # Startup is idempotent after the migration has installed instant keys.
    timeline_store.ensure_schema(conn)

    migrated = conn.execute(
        """
        SELECT window_start, window_end, window_start_us, window_end_us
          FROM timeline_window_receipts
        """
    ).fetchall()
    assert [tuple(row) for row in migrated] == [
        (
            "2026-03-08T08:00:00.000000+00:00",
            "2026-03-08T08:01:00.000000+00:00",
            1_772_956_800_000_000,
            1_772_956_860_000_000,
        )
    ]
    assert conn.execute(
        """
        SELECT 1 FROM sqlite_master
         WHERE type='index' AND name='idx_timeline_window_receipt_instant_unique'
        """
    ).fetchone()
    conn.close()


@pytest.mark.parametrize("incomplete_source", ["missing", "mismatched"])
def test_semantic_duplicate_migration_prefers_fully_current_provenance(
    incomplete_source: str,
) -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Model a database that already completed the projection migration but
    # predates semantic instant uniqueness.  Its text UNIQUE key permits two
    # offset spellings for the same absolute-time window.
    conn.executescript(timeline_store.SCHEMA)
    conn.execute("INSERT INTO timeline_schema_migrations(name) VALUES ('projection-source-v1')")
    provenance_store.ensure_schema(conn)

    incomplete_bound_source = EvidenceRef(
        kind="observation",
        id="source-incomplete-bound",
        path="source-incomplete-bound.json",
        content_hash="digest-incomplete-bound",
    )
    incomplete_digest = (
        ""
        if incomplete_source == "missing"
        else timeline_block_sources_digest([incomplete_bound_source])
    )
    incomplete = timeline_store.TimelineBlock(
        id="tlb-a-incomplete",
        start_time=datetime(2026, 3, 8, 2, 0, tzinfo=timezone(timedelta(hours=-5))),
        end_time=datetime(2026, 3, 8, 2, 1, tzinfo=timezone(timedelta(hours=-5))),
        timezone="America/New_York",
        entries=["projection-valid but provenance-incomplete"],
        apps_used=["Editor"],
        capture_count=1,
        created_at=datetime(2026, 3, 8, 2, 1, tzinfo=timezone(timedelta(hours=-5))),
        source_digest=incomplete_digest,
    )
    timeline_store.insert(conn, incomplete)
    if incomplete_source == "mismatched":
        provenance_store.record_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=incomplete.id),
            sources=[
                EvidenceRef(
                    kind="observation",
                    id="source-incomplete-actual",
                    path="source-incomplete-actual.json",
                    content_hash="digest-incomplete-actual",
                )
            ],
        )

    current_source = EvidenceRef(
        kind="observation",
        id="source-current",
        path="source-current.json",
        content_hash="digest-current",
    )
    current = timeline_store.TimelineBlock(
        id="tlb-z-current",
        start_time=datetime(2026, 3, 8, 3, 0, tzinfo=timezone(timedelta(hours=-4))),
        end_time=datetime(2026, 3, 8, 3, 1, tzinfo=timezone(timedelta(hours=-4))),
        timezone="America/New_York",
        entries=["fully current provenance"],
        apps_used=["Editor"],
        capture_count=1,
        created_at=datetime(2026, 3, 8, 3, 1, tzinfo=timezone(timedelta(hours=-4))),
        source_digest=timeline_block_sources_digest([current_source]),
    )
    timeline_store.insert(conn, current)
    provenance_store.record_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id=current.id),
        sources=[current_source],
    )

    timeline_store.ensure_schema(conn)

    projections = conn.execute(
        "SELECT id, projection_digest FROM timeline_blocks ORDER BY id"
    ).fetchall()
    assert [bool(row["projection_digest"]) for row in projections] == [False, True]
    assert timeline_store.window_state(conn, current.start_time, current.end_time) == "current"
    assert timeline_store.get_window(conn, current.start_time, current.end_time).id == current.id
    conn.close()


def test_recent_limit_is_applied_after_invalid_projection_filtering() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    timeline_store.ensure_schema(conn)
    provenance_store.ensure_schema(conn)
    start = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    older = timeline_store.TimelineBlock(
        id="tlb-valid-older",
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=["Valid older block"],
    )
    newer = timeline_store.TimelineBlock(
        id="tlb-invalid-newer",
        start_time=start + timedelta(minutes=1),
        end_time=start + timedelta(minutes=2),
        entries=["Originally valid newer block"],
    )
    timeline_store.insert(conn, older)
    timeline_store.insert(conn, newer)
    _bind_source(conn, older.id)
    _bind_source(conn, newer.id)
    conn.execute(
        "UPDATE timeline_blocks SET entries='[\"TAMPERED_NEWER\"]' WHERE id=?",
        (newer.id,),
    )

    assert [block.id for block in timeline_store.query_recent(conn, limit=1)] == [older.id]
    conn.close()


def test_retired_receipt_history_is_not_rescanned_by_producer(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    timeline_store.ensure_schema(conn)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    total = 2_000
    for index in range(total):
        window_start = start + timedelta(minutes=index)
        binding = (
            f"capture-{index}.json",
            f"obs-{index}",
            f"digest-{index}",
            window_start.isoformat(),
        )
        timeline_store.record_window_receipt(
            conn,
            timeline_store.make_window_receipt(
                window_start=window_start,
                window_end=window_start + timedelta(minutes=1),
                bindings=[binding],
                policy_digest="policy-v1",
                outcome="policy_excluded",
                raw_state="retired",
            ),
        )

    def unexpected_retired_validation(
        _conn: sqlite3.Connection,
        _receipt: timeline_store.WindowReceipt,
    ) -> bool:
        raise AssertionError("retired history must not be revalidated by the producer tick")

    monkeypatch.setattr(
        timeline_store,
        "window_receipt_is_current",
        unexpected_retired_validation,
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    assert (
        timeline_store.earliest_invalidated_window(
            conn,
            (start, start + timedelta(minutes=total)),
            policy_digest="policy-v2",
        )
        is None
    )
    selects = [
        statement for statement in statements if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) <= 8
    conn.close()


def test_live_receipt_is_still_validated_synchronously() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    timeline_store.ensure_schema(conn)
    start = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    binding = ("live.json", "obs-live", "digest-live", start.isoformat())
    timeline_store.record_window_receipt(
        conn,
        timeline_store.make_window_receipt(
            window_start=start,
            window_end=start + timedelta(minutes=1),
            bindings=[binding],
            policy_digest="policy-v1",
            outcome="policy_excluded",
        ),
    )

    assert timeline_store.earliest_invalidated_window(
        conn,
        (start, start + timedelta(minutes=2)),
        policy_digest="policy-v1",
    ) == timeline_store.as_instant(start)
    conn.close()


def test_unrooted_block_audit_is_bounded_and_resumable() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    timeline_store.ensure_schema(conn)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    total = timeline_store._WINDOW_RECEIPT_AUDIT_BATCH + 1
    for index in range(total):
        window_start = start + timedelta(minutes=index)
        block = timeline_store.TimelineBlock(
            id=f"tlb-audit-{index:04d}",
            start_time=window_start,
            end_time=window_start + timedelta(minutes=1),
            entries=[f"audit fixture {index}"],
            capture_count=1,
            source_digest=f"sources-{index}",
        )
        timeline_store.insert(conn, block)
        if index < timeline_store._WINDOW_RECEIPT_AUDIT_BATCH:
            timeline_store.record_window_receipt(
                conn,
                timeline_store.make_window_receipt(
                    window_start=block.start_time,
                    window_end=block.end_time,
                    bindings=[
                        (
                            f"capture-{index}.json",
                            f"obs-{index}",
                            f"digest-{index}",
                            window_start.isoformat(),
                        )
                    ],
                    policy_digest="policy-v1",
                    outcome="block",
                    block=block,
                    raw_state="retired",
                ),
            )

    processed_range = (start, start + timedelta(minutes=total))
    assert timeline_store.earliest_invalidated_window(conn, processed_range) is None
    expected = start + timedelta(minutes=timeline_store._WINDOW_RECEIPT_AUDIT_BATCH)
    assert timeline_store.earliest_invalidated_window(conn, processed_range) == expected
    assert (
        conn.execute(
            "SELECT block_rowid_cursor FROM timeline_receipt_audit_state WHERE id=1"
        ).fetchone()[0]
        == timeline_store._WINDOW_RECEIPT_AUDIT_BATCH
    )
    conn.close()


def test_unrooted_capture_receipt_is_detected_without_history_scan() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    timeline_store.ensure_schema(conn)
    start = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)
    timeline_store.record_capture_receipts(
        conn,
        bindings=[("orphan.json", "obs-orphan", "digest-orphan", start.isoformat())],
        window_start=start,
        window_end=start + timedelta(minutes=1),
    )

    assert timeline_store.earliest_invalidated_window(
        conn,
        (start, start + timedelta(minutes=2)),
    ) == timeline_store.as_instant(start)
    conn.close()


def test_day_candidate_band_uses_expression_index() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    timeline_store.ensure_schema(conn)

    plan = conn.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT id FROM timeline_blocks
         WHERE julianday(start_time) > julianday(?) - 2
           AND julianday(start_time) < julianday(?) + 2
           AND julianday(end_time) > julianday(?) - 2
        """,
        (
            "2026-08-08T00:00:00+00:00",
            "2026-08-09T00:00:00+00:00",
            "2026-08-08T00:00:00+00:00",
        ),
    ).fetchall()

    assert any("idx_tlb_start_jd_id" in str(row["detail"]) for row in plan)
    conn.close()


def test_timeline_block_duration_is_bounded() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    timeline_store.ensure_schema(conn)
    start = datetime(2026, 8, 8, tzinfo=UTC)

    with pytest.raises(ValueError, match="invalid fields or duration"):
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=start + timedelta(days=1, seconds=1),
            ),
        )
    conn.close()


def test_interrupted_projection_migration_resumes_but_never_auto_heals() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE timeline_blocks (
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
        )
        """
    )
    conn.execute(
        """
        INSERT INTO timeline_blocks VALUES (
            'tlb-interrupted',
            '2026-08-08T10:00:00+00:00',
            '2026-08-08T10:01:00+00:00',
            'UTC', '["resumable"]', '["Editor"]', 1,
            '2026-08-08T10:01:00+00:00', '', ''
        )
        """
    )
    provenance_store.ensure_schema(conn)
    provenance_store.record_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id="tlb-interrupted"),
        sources=[EvidenceRef(kind="observation", id="obs-interrupted")],
    )

    timeline_store.ensure_schema(conn)
    assert timeline_store.get_by_id(conn, "tlb-interrupted") is not None
    assert conn.execute(
        "SELECT 1 FROM timeline_schema_migrations WHERE name='projection-source-v1'"
    ).fetchone()

    conn.execute("UPDATE timeline_blocks SET projection_digest='' WHERE id='tlb-interrupted'")
    timeline_store.ensure_schema(conn)
    assert timeline_store.get_by_id(conn, "tlb-interrupted") is None
    conn.close()


def test_large_windows_floor_from_local_midnight() -> None:
    moment = datetime(2026, 8, 8, 14, 7, 42, tzinfo=UTC)

    assert timeline_store.floor_to_window(moment, 120) == datetime(2026, 8, 8, 14, 0, tzinfo=UTC)
    assert timeline_store.floor_to_window(moment, 1440) == datetime(2026, 8, 8, 0, 0, tzinfo=UTC)


def test_floor_to_window_preserves_both_dst_fallback_folds() -> None:
    zone = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 1, 1, 37, tzinfo=zone, fold=0)
    second = datetime(2026, 11, 1, 1, 37, tzinfo=zone, fold=1)

    first_floor = timeline_store.floor_to_window(first, 5)
    second_floor = timeline_store.floor_to_window(second, 5)

    assert (first_floor.hour, first_floor.minute, first_floor.fold) == (1, 35, 0)
    assert (second_floor.hour, second_floor.minute, second_floor.fold) == (1, 35, 1)
    assert first_floor.astimezone(UTC) != second_floor.astimezone(UTC)


def test_capture_snapshot_keeps_fallback_fold_buckets_separate(ac_root: Path) -> None:
    zone = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 1, 1, 35, 30, tzinfo=zone, fold=0)
    second = datetime(2026, 11, 1, 1, 35, 30, tzinfo=zone, fold=1)
    expected: dict[datetime, str] = {}
    for index, timestamp in enumerate((first, second), start=1):
        observation_id = f"obs_{index:032x}"
        name = f"{capture_filenames.capture_stem(timestamp.isoformat(), observation_id)}.json"
        (paths.capture_buffer_dir() / name).write_text(
            json.dumps(
                {
                    "observation_id": observation_id,
                    "timestamp": timestamp.isoformat(),
                    "visible_text": f"public fold fixture {index}",
                }
            ),
            encoding="utf-8",
        )
        expected[timeline_store.as_instant(timestamp.replace(second=0))] = name

    grouped = timeline_aggregator.capture_paths_by_window(
        datetime(2026, 11, 1, 2, 0, tzinfo=zone),
        5,
    )
    snapshot = timeline_aggregator.load_capture_snapshot(
        grouped,
        start=first.replace(second=0),
        end=timeline_store.add_elapsed(second.replace(second=0), timedelta(minutes=5)),
        window_minutes=5,
    )

    assert len(grouped) == len(snapshot) == 2
    assert set(grouped) == set(expected)
    assert {key: captures[0][0].name for key, captures in snapshot.items()} == expected


def test_recent_and_since_order_fallback_folds_by_instant() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    timeline_store.ensure_schema(conn)
    provenance_store.ensure_schema(conn)
    zone = ZoneInfo("America/New_York")
    starts = (
        datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0),
        datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1),
    )
    blocks: list[timeline_store.TimelineBlock] = []
    for index, start in enumerate(starts):
        block = timeline_store.TimelineBlock(
            id=f"tlb-fallback-{index}",
            start_time=start,
            end_time=timeline_store.add_elapsed(start, timedelta(minutes=5)),
            entries=[f"fold {index}"],
        )
        timeline_store.insert(conn, block)
        _bind_source(conn, block.id)
        blocks.append(block)

    assert [block.id for block in timeline_store.query_recent(conn, limit=1)] == [blocks[1].id]
    assert [block.id for block in timeline_store.query_since(conn, blocks[0].end_time)] == [
        blocks[1].id
    ]
    conn.close()


def test_iter_windows_crosses_dst_fallback_without_duplicate_instant() -> None:
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 1, 0, tzinfo=zone, fold=0)
    end = datetime(2026, 11, 1, 2, 0, tzinfo=zone)

    windows = timeline_store.iter_windows(start, end, 60)

    assert len(windows) == 2
    assert windows[0][0].fold == 0 and windows[0][1].fold == 1
    assert windows[1][0].fold == 1 and windows[1][1].hour == 2
    assert all(
        right.astimezone(UTC) - left.astimezone(UTC) == timedelta(hours=1)
        for left, right in windows
    )


def test_spring_forward_floor_skips_nonexistent_boundary() -> None:
    zone = ZoneInfo("America/New_York")
    moment = datetime(2026, 3, 8, 3, 30, tzinfo=zone)

    floored = timeline_store.floor_to_window(moment, 120)

    assert (floored.hour, floored.minute) == (3, 0)
    assert floored.astimezone(UTC) <= moment.astimezone(UTC)


def test_semantic_window_identity_rejects_equivalent_offset_spelling() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    timeline_store.ensure_schema(conn)
    provenance_store.ensure_schema(conn)
    fixed = timezone(timedelta(hours=-5))
    fixed_start = datetime(2026, 3, 8, 2, 0, tzinfo=fixed)
    fixed_end = fixed_start + timedelta(minutes=1)
    existing = timeline_store.TimelineBlock(
        id="tlb-fixed-offset-window",
        start_time=fixed_start,
        end_time=fixed_end,
        entries=["fixed offset spelling"],
    )
    timeline_store.insert(conn, existing)
    _bind_source(conn, existing.id)

    zone = ZoneInfo("America/New_York")
    zone_start = datetime(2026, 3, 8, 3, 0, tzinfo=zone)
    zone_end = zone_start + timedelta(minutes=1)
    assert timeline_store.window_state(conn, zone_start, zone_end) == "current"

    persisted, created = timeline_store.insert_or_get(
        conn,
        timeline_store.TimelineBlock(
            id="tlb-zoneinfo-equivalent-window",
            start_time=zone_start,
            end_time=zone_end,
            entries=["must not duplicate"],
        ),
    )

    assert created is False
    assert persisted.id == existing.id
    assert conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0] == 1
    conn.close()
