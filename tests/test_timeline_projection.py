from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef
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
