from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from openchronicle import cli
from openchronicle import config as config_mod
from openchronicle.capture import scheduler as capture_scheduler
from openchronicle.capture import store_lock as capture_store_lock
from openchronicle.privacy import policy as privacy_policy
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import (
    EvidenceRef,
    legacy_observation_digest,
    observation_digest,
    timeline_block_sources_digest,
)
from openchronicle.session import store as session_store
from openchronicle.store import files as store_files
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store
from openchronicle.timeline import tick as timeline_tick
from openchronicle.writer import session_reducer

_TZ = timezone(timedelta(hours=8))


def _materialize_fake_window(
    conn,
    *,
    start: datetime,
    end: datetime,
    parsed_captures: list[tuple[Path, dict]],
    outcome_publisher=None,
):
    """Test seam that preserves the production block+outcome atomic contract."""
    if not parsed_captures:
        return None
    sources = timeline_tick.aggregator._capture_sources(parsed_captures)
    conn.execute("SAVEPOINT test_timeline_materialize")
    try:
        block = timeline_store.get_window(conn, start, end)
        if block is None:
            block = timeline_store.TimelineBlock(
                start_time=start,
                end_time=end,
                entries=["[Editor] test materialization"],
                apps_used=["Editor"],
                capture_count=len(parsed_captures),
                source_digest=timeline_block_sources_digest(sources),
            )
            timeline_store.insert(conn, block)
        else:
            block.capture_count = len(parsed_captures)
            block.source_digest = timeline_block_sources_digest(sources)
            block.projection_digest = timeline_store.projection_digest(block)
            conn.execute(
                """
                UPDATE timeline_blocks
                   SET capture_count=?, source_digest=?, projection_digest=?
                 WHERE id=?
                """,
                (
                    block.capture_count,
                    block.source_digest,
                    block.projection_digest,
                    block.id,
                ),
            )
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block.id),
            sources=sources,
        )
        assert timeline_store.get_window(conn, start, end) is not None
        assert (
            provenance_store.direct_sources_checked(
                conn,
                EvidenceRef(kind="timeline_block", id=block.id),
            )
            == sources
        )
        if outcome_publisher is not None:
            outcome_publisher()
        conn.execute("RELEASE SAVEPOINT test_timeline_materialize")
        return block
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT test_timeline_materialize")
        conn.execute("RELEASE SAVEPOINT test_timeline_materialize")
        raise


@pytest.mark.parametrize(
    ("now", "expected_start"),
    [
        (
            datetime(2026, 3, 8, 3, 30, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo("America/New_York")),
        ),
        (
            datetime(
                2026,
                11,
                1,
                1,
                30,
                tzinfo=ZoneInfo("America/New_York"),
                fold=1,
            ),
            datetime(
                2026,
                11,
                1,
                1,
                30,
                tzinfo=ZoneInfo("America/New_York"),
                fold=0,
            ),
        ),
    ],
)
def test_cold_lookback_is_one_elapsed_hour_across_dst(
    ac_root: Path,
    monkeypatch,
    now: datetime,
    expected_start: datetime,
) -> None:
    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 60
    monkeypatch.setattr(timeline_tick, "_now", lambda: now)
    monkeypatch.setattr(
        timeline_tick.aggregator,
        "produce_block_for_window",
        lambda *_args, **_kwargs: None,
    )

    assert timeline_tick._run_once(cfg) == 0
    with fts.cursor() as conn:
        processed = timeline_store.get_processed_range(conn)
    assert processed is not None
    assert processed[0].astimezone(UTC) == expected_start.astimezone(UTC)
    assert processed[1].astimezone(UTC) == now.astimezone(UTC)


@pytest.mark.parametrize(
    ("initial", "current", "expected_windows"),
    [
        (
            datetime(2026, 3, 8, 1, 59, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 3, 8, 3, 1, tzinfo=ZoneInfo("America/New_York")),
            [
                ("2026-03-08T01:59:00-05:00", "2026-03-08T03:00:00-04:00", 0, 0),
                ("2026-03-08T03:00:00-04:00", "2026-03-08T03:01:00-04:00", 0, 0),
            ],
        ),
        (
            datetime(
                2026,
                11,
                1,
                1,
                59,
                tzinfo=ZoneInfo("America/New_York"),
                fold=0,
            ),
            datetime(
                2026,
                11,
                1,
                1,
                1,
                tzinfo=ZoneInfo("America/New_York"),
                fold=1,
            ),
            [
                ("2026-11-01T01:59:00-04:00", "2026-11-01T01:00:00-05:00", 0, 1),
                ("2026-11-01T01:00:00-05:00", "2026-11-01T01:01:00-05:00", 1, 1),
            ],
        ),
    ],
)
def test_persisted_watermark_restores_iana_rules_across_dst(
    ac_root: Path,
    monkeypatch,
    initial: datetime,
    current: datetime,
    expected_windows: list[tuple[str, str, int, int]],
) -> None:
    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    clock = {"now": initial}
    windows: list[tuple[str, str, int, int]] = []
    monkeypatch.setattr(timeline_tick, "_now", lambda: clock["now"])
    monkeypatch.setattr(
        timeline_tick.aggregator,
        "produce_block_for_window",
        lambda _cfg, _conn, *, start, end, parsed_captures, **_kwargs: windows.append(
            (start.isoformat(), end.isoformat(), start.fold, end.fold)
        ),
    )

    assert timeline_tick._run_once(cfg) == 0
    clock["now"] = current
    assert timeline_tick._run_once(cfg) == 0

    assert windows == expected_windows
    with fts.cursor() as conn:
        processed = timeline_store.get_processed_range(conn)
    assert processed is not None
    assert processed[1].isoformat() == current.isoformat()


def test_retained_capture_seed_restores_current_iana_zone_before_floor(
    ac_root: Path,
    monkeypatch,
) -> None:
    zone = ZoneInfo("America/New_York")
    current = datetime(2026, 3, 8, 3, 1, tzinfo=zone)
    capture_scheduler._write_capture(_capture_dict("2026-03-08T01:59:30-05:00", "spring seed"))
    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    windows: list[tuple[str, str]] = []
    monkeypatch.setattr(timeline_tick, "_now", lambda: current)

    def record_window(_cfg, conn, *, start, end, parsed_captures, **kwargs):
        windows.append((start.isoformat(), end.isoformat()))
        return _materialize_fake_window(
            conn,
            start=start,
            end=end,
            parsed_captures=parsed_captures,
            outcome_publisher=kwargs.get("outcome_publisher"),
        )

    monkeypatch.setattr(
        timeline_tick.aggregator,
        "produce_block_for_window",
        record_window,
    )

    assert timeline_tick._run_once(cfg) == 1

    assert windows == [
        ("2026-03-08T01:59:00-05:00", "2026-03-08T03:00:00-04:00"),
        ("2026-03-08T03:00:00-04:00", "2026-03-08T03:01:00-04:00"),
    ]


def test_empty_windows_advance_durable_timeline_watermark(ac_root: Path, monkeypatch) -> None:
    now = datetime(2026, 4, 21, 10, 5, tzinfo=_TZ)
    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 2
    windows: list[tuple[datetime, datetime]] = []

    monkeypatch.setattr(timeline_tick, "_now", lambda: now)

    def empty_window(_cfg, _conn, *, start, end, parsed_captures, **_kwargs):
        assert parsed_captures == []
        windows.append((start, end))
        return None

    monkeypatch.setattr(
        timeline_tick.aggregator,
        "produce_block_for_window",
        empty_window,
    )

    assert timeline_tick._run_once(cfg) == 0
    assert windows == [
        (now - timedelta(minutes=2), now - timedelta(minutes=1)),
        (now - timedelta(minutes=1), now),
    ]
    with fts.cursor() as conn:
        assert timeline_store.get_processed_through(conn) == now
        assert timeline_store.get_processed_range(conn) == (
            now - timedelta(minutes=2),
            now,
        )

    windows.clear()
    assert timeline_tick._run_once(cfg) == 0
    assert windows == []


def test_wall_clock_forward_then_rollback_rewinds_future_empty_proof(
    ac_root: Path,
    monkeypatch,
) -> None:
    base = datetime(2026, 4, 21, 12, 0, tzinfo=_TZ)
    clock = {"now": base}
    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    monkeypatch.setattr(timeline_tick, "_now", lambda: clock["now"])
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv(
        "OPENCHRONICLE_LLM_MOCK_JSON",
        json.dumps({"entries": ["[Editor] recovered rollback evidence"]}),
    )

    # Establish a real lower bound, then simulate an erroneous two-hour
    # forward jump that certifies empty windows through 14:00.
    assert timeline_tick._run_once(cfg) == 0
    clock["now"] = base + timedelta(hours=2)
    assert timeline_tick._run_once(cfg) == 0
    with fts.cursor() as conn:
        assert timeline_store.get_processed_range(conn) == (
            base,
            base + timedelta(hours=2),
        )

    # Once wall time is corrected, a newly persisted capture in that apparent
    # future must force safe replay instead of remaining behind the watermark.
    capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=30)).isoformat(), "rollback evidence")
    )
    clock["now"] = base + timedelta(minutes=2)
    assert timeline_tick._run_once(cfg) == 1
    assert timeline_tick._run_once(cfg) == 0

    with fts.cursor() as conn:
        blocks = timeline_store.query_since(conn, base)
        assert len(blocks) == 1
        assert blocks[0].capture_count == 1
        assert timeline_store.get_processed_range(conn) == (
            base,
            base + timedelta(minutes=2),
        )


def test_late_capture_replaces_populated_window_and_cleanup_waits_for_receipt(
    ac_root: Path,
    monkeypatch,
) -> None:
    base = datetime(2026, 4, 21, 12, 0, tzinfo=_TZ)
    end = base + timedelta(minutes=1)
    clock = {"now": end}
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    monkeypatch.setattr(timeline_tick, "_now", lambda: clock["now"])
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv(
        "OPENCHRONICLE_LLM_MOCK_JSON",
        json.dumps({"entries": ["[Editor] receipt-aware window"]}),
    )

    first_path = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=10)).isoformat(), "first evidence")
    )
    assert timeline_tick._run_once(cfg) == 1
    with fts.cursor() as conn:
        first_block = timeline_store.get_window(conn, base, end)
        assert first_block is not None and first_block.capture_count == 1
        assert timeline_store.capture_receipt_paths(conn) == {first_path.name}

    # This capture arrives after the producer certified the same window.  Even
    # with an old mtime, retention must not trust the upper watermark until the
    # exact path receives a receipt from the replayed snapshot.
    second_path = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=40)).isoformat(), "late evidence")
    )
    old_mtime = time.time() - 2 * 3600
    os.utime(first_path, (old_mtime, old_mtime))
    os.utime(second_path, (old_mtime, old_mtime))
    cleanup = capture_scheduler.cleanup_buffer(
        retention_hours=1,
        processed_before_ts=end.isoformat(),
        capture_config=cfg.capture,
    )
    assert cleanup["deleted"] == 0
    assert first_path.exists() and second_path.exists()

    clock["now"] = end + timedelta(seconds=30)
    assert timeline_tick._run_once(cfg) == 1
    with fts.cursor() as conn:
        replacement = timeline_store.get_window(conn, base, end)
        assert replacement is not None
        assert replacement.id != first_block.id
        assert replacement.capture_count == 2
        sources = provenance_store.direct_sources(
            conn,
            EvidenceRef(kind="timeline_block", id=replacement.id),
        )
        assert {source.path for source in sources} == {
            first_path.name,
            second_path.name,
        }
        assert timeline_store.capture_receipt_paths(conn) == {
            first_path.name,
            second_path.name,
        }
        assert timeline_store.get_processed_range(conn) == (base, end)


def test_invalid_window_stops_before_watermark_and_receipt(
    ac_root: Path,
    monkeypatch,
) -> None:
    base = datetime(2026, 4, 21, 12, 0, tzinfo=_TZ)
    end = base + timedelta(minutes=1)
    capture_path = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=20)).isoformat(), "must remain pending")
    )
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    with fts.cursor() as conn:
        block = timeline_store.TimelineBlock(
            id="tlb-invalid-watermark-gap",
            start_time=base,
            end_time=end,
            entries=["valid before tamper"],
            apps_used=["Editor"],
            capture_count=1,
        )
        timeline_store.insert(conn, block)
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block.id),
            sources=[
                EvidenceRef(
                    kind="observation",
                    id=str(capture["observation_id"]),
                    path=capture_path.name,
                    timestamp=str(capture["timestamp"]),
                    content_hash=observation_digest(capture),
                )
            ],
        )
        conn.execute(
            "UPDATE timeline_blocks SET entries='[\"tampered\"]' WHERE id=?",
            (block.id,),
        )
        timeline_store.initialize_processed_range(conn, base)

    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    monkeypatch.setattr(timeline_tick, "_now", lambda: end)

    assert timeline_tick._run_once(cfg) == 0
    with fts.cursor() as conn:
        assert timeline_store.get_processed_range(conn) == (base, base)
        assert timeline_store.capture_receipt_paths(conn) == set()


def test_late_capture_keeps_consumed_block_and_downstream_state_fail_closed(
    ac_root: Path,
    monkeypatch,
) -> None:
    base = datetime(2026, 4, 21, 13, 0, tzinfo=_TZ)
    end = base + timedelta(minutes=1)
    clock = {"now": end}
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    monkeypatch.setattr(timeline_tick, "_now", lambda: clock["now"])
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv(
        "OPENCHRONICLE_LLM_MOCK_JSON",
        json.dumps({"entries": ["[Editor] consumed source set"]}),
    )
    first_path = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=10)).isoformat(), "first")
    )
    assert timeline_tick._run_once(cfg) == 1
    with fts.cursor() as conn:
        block = timeline_store.get_window(conn, base, end)
        assert block is not None
        provenance_store.record_sources(
            conn,
            subject=EvidenceRef(
                kind="memory_entry",
                id="downstream-entry",
                path="event-2026-04-21.md",
            ),
            sources=[EvidenceRef(kind="timeline_block", id=block.id)],
        )

    second_path = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=40)).isoformat(), "late")
    )
    clock["now"] = end + timedelta(seconds=30)
    assert timeline_tick._run_once(cfg) == 0

    with fts.cursor() as conn:
        unchanged = timeline_store.get_window(conn, base, end)
        assert unchanged is not None
        assert unchanged.id == block.id and unchanged.capture_count == 1
        assert timeline_store.get_processed_range(conn) == (base, base)
        assert timeline_store.capture_receipt_paths(conn) == {first_path.name}
    assert second_path.exists()


def test_late_capture_into_terminal_noop_window_stalls_durably(
    ac_root: Path,
    monkeypatch,
) -> None:
    base = datetime(2026, 4, 21, 14, 0, tzinfo=_TZ)
    end = base + timedelta(minutes=1)
    clock = {"now": end}
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 1
    monkeypatch.setattr(timeline_tick, "_now", lambda: clock["now"])

    assert timeline_tick._run_once(cfg) == 0
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_late_after_noop",
                start_time=base,
                end_time=end,
                status="ended",
            ),
        )
    reduced = session_reducer.reduce_session(
        cfg,
        session_id="sess_late_after_noop",
        start_time=base,
        end_time=end,
    )
    assert reduced.succeeded is True and reduced.written is False

    late_path = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=20)).isoformat(), "late after noop")
    )
    clock["now"] = end + timedelta(seconds=30)
    assert timeline_tick._run_once(cfg) == 0
    # The durable replay range keeps a restart/next tick from forgetting that
    # this was previously certified empty and silently creating a new block.
    assert timeline_tick._run_once(cfg) == 0

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_late_after_noop")
        assert row is not None and row.status == "reduced"
        assert row.classifier_terminal_noop is True
        assert timeline_store.get_window(conn, base, end) is None
        assert timeline_store.get_processed_range(conn) == (base, base)
        assert timeline_store.get_replay_range(conn) == (base, end)
        assert timeline_store.capture_receipt_paths(conn) == set()
    assert late_path.exists()


def test_published_partial_wrap_does_not_block_virgin_frontier(
    ac_root: Path,
    monkeypatch,
) -> None:
    base = datetime(2026, 4, 21, 15, 0, tzinfo=_TZ)
    first_end = base + timedelta(minutes=1)
    clock = {"now": first_end}
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 1
    monkeypatch.setattr(timeline_tick, "_now", lambda: clock["now"])
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv(
        "OPENCHRONICLE_LLM_MOCK_JSON",
        json.dumps({"entries": ["[Editor] normal frontier"]}),
    )
    assert timeline_tick._run_once(cfg) == 0

    day_start = base.replace(hour=0, minute=0)
    with fts.cursor() as conn:
        conn.execute(
            """
            INSERT INTO daily_wrap_revisions(
                wrap_id, revision, local_date, timezone, scope,
                window_start_utc, window_end_utc, workflow_version,
                input_digest, coverage_status, source_digest, output_json, created_at
            ) VALUES (?, 1, ?, ?, 'default', ?, ?, 1, 'input', 'partial', '', '{}', ?)
            """,
            (
                "wrap-partial-frontier",
                base.date().isoformat(),
                "+08:00",
                day_start.astimezone(UTC).isoformat(),
                (day_start + timedelta(days=1)).astimezone(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )

    capture_scheduler._write_capture(
        _capture_dict((first_end + timedelta(seconds=10)).isoformat(), "new frontier")
    )
    clock["now"] = base + timedelta(minutes=2)
    assert timeline_tick._run_once(cfg) == 1
    with fts.cursor() as conn:
        block = timeline_store.get_window(
            conn,
            first_end,
            base + timedelta(minutes=2),
        )
    assert block is not None and block.capture_count == 1


def test_replacement_final_fence_rechecks_new_dependent(
    ac_root: Path,
    monkeypatch,
) -> None:
    base = datetime(2026, 4, 21, 16, 0, tzinfo=_TZ)
    end = base + timedelta(minutes=1)
    clock = {"now": end}
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    monkeypatch.setattr(timeline_tick, "_now", lambda: clock["now"])
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv(
        "OPENCHRONICLE_LLM_MOCK_JSON",
        json.dumps({"entries": ["[Editor] final fence"]}),
    )
    first_path = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=10)).isoformat(), "first")
    )
    assert timeline_tick._run_once(cfg) == 1
    with fts.cursor() as conn:
        original = timeline_store.get_window(conn, base, end)
    assert original is not None
    late_path = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=40)).isoformat(), "late")
    )

    first_fence_released = threading.Event()
    allow_final_fence = threading.Event()
    real_model_fence = timeline_tick.aggregator.model_egress_lock
    fence_calls = 0
    fence_calls_lock = threading.Lock()

    @contextmanager
    def gated_model_fence():
        nonlocal fence_calls
        with fence_calls_lock:
            fence_calls += 1
            ordinal = fence_calls
        with real_model_fence():
            yield
        if ordinal == 1:
            first_fence_released.set()
            assert allow_final_fence.wait(timeout=5)

    monkeypatch.setattr(
        timeline_tick.aggregator,
        "model_egress_lock",
        gated_model_fence,
    )
    clock["now"] = end + timedelta(seconds=30)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(timeline_tick._run_once, cfg)
        assert first_fence_released.wait(timeout=5)
        with fts.cursor() as conn:
            provenance_store.record_sources(
                conn,
                subject=EvidenceRef(
                    kind="memory_entry",
                    id="racing-dependent",
                    path="event-2026-04-21.md",
                ),
                sources=[EvidenceRef(kind="timeline_block", id=original.id)],
            )
        allow_final_fence.set()
        assert future.result(timeout=5) == 0

    with fts.cursor() as conn:
        unchanged = timeline_store.get_window(conn, base, end)
        assert unchanged is not None and unchanged.id == original.id
        assert timeline_store.get_processed_range(conn) == (base, base)
        assert timeline_store.capture_receipt_paths(conn) == {first_path.name}
    assert late_path.exists()


@pytest.mark.parametrize("mutation", ["privacy", "schema_version", "observation_id"])
def test_v1_upgrade_then_semantic_mutation_rewinds_and_safe_stalls(
    ac_root: Path,
    monkeypatch,
    mutation: str,
) -> None:
    base = datetime(2026, 4, 21, 17, 0, tzinfo=_TZ)
    end = base + timedelta(minutes=1)
    clock = {"now": end}
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    monkeypatch.setattr(timeline_tick, "_now", lambda: clock["now"])
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv(
        "OPENCHRONICLE_LLM_MOCK_JSON",
        json.dumps({"entries": ["[Editor] upgraded digest"]}),
    )
    capture_path = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=10)).isoformat(), "upgrade fixture")
    )
    assert timeline_tick._run_once(cfg) == 1
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    legacy_hash = legacy_observation_digest(capture)

    # Simulate a pre-v2 block and receipt, then reopen through the one-time
    # trusted migration. The source identity and window projection stay stable.
    with fts.cursor() as conn:
        block = timeline_store.get_window(conn, base, end)
        assert block is not None
        source = provenance_store.direct_sources(
            conn,
            EvidenceRef(kind="timeline_block", id=block.id),
        )[0]
        legacy_source = EvidenceRef(
            kind=source.kind,
            id=source.id,
            path=source.path,
            timestamp=source.timestamp,
            content_hash=legacy_hash,
        )
        conn.execute(
            """
            UPDATE provenance_edges SET source_hash=?
             WHERE subject_kind='timeline_block' AND subject_id=?
               AND source_kind='observation' AND source_path=?
            """,
            (legacy_hash, block.id, capture_path.name),
        )
        block.source_digest = timeline_block_sources_digest([legacy_source])
        block.projection_digest = timeline_store.projection_digest(block)
        conn.execute(
            "UPDATE timeline_blocks SET source_digest=?, projection_digest=? WHERE id=?",
            (block.source_digest, block.projection_digest, block.id),
        )
        conn.execute(
            "UPDATE timeline_capture_receipts SET source_hash=? WHERE capture_path=?",
            (legacy_hash, capture_path.name),
        )
        conn.execute(
            "DELETE FROM timeline_schema_migrations WHERE name='observation-semantic-digest-v2'"
        )

    with (
        store_files.review_operation_lock(),
        capture_store_lock.capture_store_lock(),
        fts.cursor() as conn,
    ):
        timeline_store.migrate_observation_digests_v2(conn)

    with fts.cursor() as conn:
        migrated = timeline_store.get_window(conn, base, end)
        assert migrated is not None and migrated.id == block.id
        migrated_source = provenance_store.direct_sources(
            conn,
            EvidenceRef(kind="timeline_block", id=block.id),
        )[0]
        assert migrated_source.content_hash == observation_digest(capture)
        assert timeline_store.capture_receipt_records(conn)[capture_path.name][1] == (legacy_hash)

    # Receipts are never trusted-upgraded. Their v1 mismatch forces a normal
    # replay, which can backfill v2 without a model call once the migrated
    # block proves the same semantic source set.
    clock["now"] = end + timedelta(seconds=30)
    assert timeline_tick._run_once(cfg) == 0
    with fts.cursor() as conn:
        replayed = timeline_store.get_window(conn, base, end)
        assert replayed is not None and replayed.id == block.id
        prior_receipt = timeline_store.capture_receipt_records(conn)[capture_path.name]
        assert prior_receipt[1] == observation_digest(capture)

    changed = json.loads(capture_path.read_text(encoding="utf-8"))
    if mutation == "privacy":
        changed["privacy"] = {"decision": "allowed", "policy_version": 99}
    elif mutation == "schema_version":
        changed["schema_version"] = 99
    else:
        changed["observation_id"] = "obs_semantic_identity_changed"
    capture_scheduler._atomic_write_json(capture_path, changed)

    assert timeline_tick._run_once(cfg) == 0
    with fts.cursor() as conn:
        unchanged = timeline_store.get_window(conn, base, end)
        assert unchanged is not None and unchanged.id == block.id
        receipt = timeline_store.capture_receipt_records(conn)[capture_path.name]
        assert receipt == prior_receipt
        assert timeline_store.get_processed_range(conn) == (base, base)
    assert capture_path.exists()


def test_late_capture_after_full_window_retirement_rewinds_and_safe_stalls(
    ac_root: Path,
    monkeypatch,
) -> None:
    base = datetime(2026, 4, 21, 12, 30, tzinfo=_TZ)
    end = base + timedelta(minutes=1)
    clock = {"now": end}
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    monkeypatch.setattr(timeline_tick, "_now", lambda: clock["now"])
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    first = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=10)).isoformat(), "retired source")
    )
    assert timeline_tick._run_once(cfg) == 1
    with fts.cursor() as conn:
        original = timeline_store.get_window(conn, base, end)
        assert original is not None

    old = time.time() - 2 * 3600
    os.utime(first, (old, old))
    retired = capture_scheduler.cleanup_buffer(
        retention_hours=1,
        processed_before_ts=end.isoformat(),
        capture_config=cfg.capture,
    )
    assert retired == {"deleted": 1, "stripped": 0, "evicted": 0}
    with fts.cursor() as conn:
        roots = timeline_store.window_receipts_in_raw_states(conn, "retired")
        assert len(roots) == 1
        assert timeline_store.capture_receipt_paths(conn) == set()

    late = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=40)).isoformat(), "late after retirement")
    )
    clock["now"] = end + timedelta(seconds=30)
    assert timeline_tick._run_once(cfg) == 0

    with fts.cursor() as conn:
        unchanged = timeline_store.get_window(conn, base, end)
        assert unchanged is not None and unchanged.id == original.id
        assert timeline_store.get_processed_range(conn) == (base, base)
        assert timeline_store.get_replay_range(conn) == (base, end)
        assert timeline_store.capture_receipt_paths(conn) == set()
    assert late.exists()
    assert capture_scheduler.cleanup_buffer(
        retention_hours=0,
        processed_before_ts=end.isoformat(),
        capture_config=cfg.capture,
    ) == {"deleted": 0, "stripped": 0, "evicted": 0}


def test_v1_migration_never_auto_heals_inconsistent_block_sources(
    ac_root: Path,
    monkeypatch,
) -> None:
    base = datetime(2026, 4, 21, 17, 30, tzinfo=_TZ)
    end = base + timedelta(minutes=1)
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    monkeypatch.setattr(timeline_tick, "_now", lambda: end)
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    capture_path = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(seconds=10)).isoformat(), "invalid legacy source")
    )
    assert timeline_tick._run_once(cfg) == 1
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    legacy_hash = legacy_observation_digest(capture)

    with fts.cursor() as conn:
        block = timeline_store.get_window(conn, base, end)
        assert block is not None
        conn.execute(
            "UPDATE provenance_edges SET source_hash=? WHERE subject_id=?",
            (legacy_hash, block.id),
        )
        conn.execute(
            "UPDATE timeline_capture_receipts SET source_hash=? WHERE capture_path=?",
            (legacy_hash, capture_path.name),
        )
        block.source_digest = "deliberately-inconsistent-source-set"
        block.projection_digest = timeline_store.projection_digest(block)
        conn.execute(
            "UPDATE timeline_blocks SET source_digest=?, projection_digest=? WHERE id=?",
            (block.source_digest, block.projection_digest, block.id),
        )
        assert timeline_store.window_state(conn, base, end) == "invalid"
        conn.execute(
            "DELETE FROM timeline_schema_migrations WHERE name='observation-semantic-digest-v2'"
        )

    with (
        store_files.review_operation_lock(),
        capture_store_lock.capture_store_lock(),
        fts.cursor() as conn,
    ):
        timeline_store.migrate_observation_digests_v2(conn)

    with fts.cursor() as conn:
        assert timeline_store.window_state(conn, base, end) == "invalid"
        source_hash = conn.execute(
            "SELECT source_hash FROM provenance_edges WHERE subject_id=?",
            (block.id,),
        ).fetchone()[0]
        assert source_hash == legacy_hash
        receipt_hash = conn.execute(
            "SELECT source_hash FROM timeline_capture_receipts WHERE capture_path=?",
            (capture_path.name,),
        ).fetchone()[0]
        assert receipt_hash == legacy_hash

    old_mtime = time.time() - 2 * 3600
    os.utime(capture_path, (old_mtime, old_mtime))
    cleanup = capture_scheduler.cleanup_buffer(
        retention_hours=1,
        processed_before_ts=end.isoformat(),
        capture_config=cfg.capture,
    )
    assert cleanup == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert capture_path.exists()


def test_screenshot_strip_preserves_block_and_receipt_semantics(
    ac_root: Path,
    monkeypatch,
) -> None:
    base = datetime(2026, 4, 21, 18, 0, tzinfo=_TZ)
    end = base + timedelta(minutes=1)
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    monkeypatch.setattr(timeline_tick, "_now", lambda: end)
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    capture = _capture_dict((base + timedelta(seconds=10)).isoformat(), "pixels")
    capture["screenshot"] = {"image_base64": "AAAA", "mime_type": "image/jpeg"}
    capture_path = capture_scheduler._write_capture(capture)
    assert timeline_tick._run_once(cfg) == 1
    old_mtime = time.time() - 2 * 3600
    os.utime(capture_path, (old_mtime, old_mtime))

    stripped = capture_scheduler.cleanup_buffer(
        retention_hours=24,
        processed_before_ts=end.isoformat(),
        screenshot_retention_hours=1,
        capture_config=cfg.capture,
    )
    assert stripped == {"deleted": 0, "stripped": 1, "evicted": 0}
    stored = json.loads(capture_path.read_text(encoding="utf-8"))
    assert "screenshot" not in stored and stored["screenshot_stripped"] is True
    with fts.cursor() as conn:
        block = timeline_store.get_window(conn, base, end)
        assert block is not None
        source = provenance_store.direct_sources(
            conn,
            EvidenceRef(kind="timeline_block", id=block.id),
        )[0]
        assert provenance_store.is_current(conn, source)

    deleted = capture_scheduler.cleanup_buffer(
        retention_hours=1,
        processed_before_ts=end.isoformat(),
        capture_config=cfg.capture,
    )
    assert deleted == {"deleted": 1, "stripped": 0, "evicted": 0}
    assert not capture_path.exists()


def test_dst_rollback_rewind_keeps_durable_custom_window_grid(
    ac_root: Path,
    monkeypatch,
) -> None:
    zone = ZoneInfo("America/New_York")
    anchor = datetime(2026, 3, 8, 0, 0, tzinfo=zone)
    old_start = datetime(2026, 3, 8, 3, 0, tzinfo=zone)
    old_end = datetime(2026, 3, 8, 5, 0, tzinfo=zone)
    capture_scheduler._write_capture(
        _capture_dict("2026-03-08T03:30:00-04:00", "rollback grid evidence")
    )
    with fts.cursor() as conn:
        timeline_store.initialize_processed_range(conn, anchor)
        timeline_store.advance_processed_through(conn, old_start, window_start=anchor)
        timeline_store.advance_processed_through(conn, old_end, window_start=old_start)

    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 120
    cfg.timeline.cold_lookback_minutes = 0
    clock = {"now": datetime(2026, 3, 8, 4, 30, tzinfo=zone)}
    windows: list[tuple[str, str]] = []
    monkeypatch.setattr(timeline_tick, "_now", lambda: clock["now"])

    def record_window(_cfg, conn, *, start, end, parsed_captures, **kwargs):
        windows.append((start.isoformat(), end.isoformat()))
        return _materialize_fake_window(
            conn,
            start=start,
            end=end,
            parsed_captures=parsed_captures,
            outcome_publisher=kwargs.get("outcome_publisher"),
        )

    monkeypatch.setattr(
        timeline_tick.aggregator,
        "produce_block_for_window",
        record_window,
    )

    # Rewinding from the old 05:00 proof must land on the original 03:00
    # elapsed grid, not a newly midnight-floored 02:00 fixed-offset spelling.
    assert timeline_tick._run_once(cfg) == 0
    assert windows == []
    clock["now"] = old_end
    assert timeline_tick._run_once(cfg) == 1

    assert windows == [(old_start.isoformat(), old_end.isoformat())]
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0] == 1
        processed = timeline_store.get_processed_range(conn)
    assert processed is not None
    assert tuple(value.astimezone(UTC) for value in processed) == (
        old_start.astimezone(UTC),
        old_end.astimezone(UTC),
    )


def test_clean_timeline_resets_blocks_and_producer_watermark(ac_root: Path) -> None:
    start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    binding = ("clean.json", "obs_clean", "digest", start.isoformat())
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=end,
            ),
        )
        timeline_store.advance_processed_through(conn, end)
        timeline_store.activate_capture_receipts(conn)
        timeline_store.activate_window_receipt_epoch(conn, 1)
        timeline_store.record_capture_receipts(
            conn,
            bindings=[binding],
            window_start=start,
            window_end=end,
        )
        timeline_store.record_window_receipt(
            conn,
            timeline_store.make_window_receipt(
                window_start=start,
                window_end=end,
                bindings=[binding],
                policy_digest=privacy_policy.stored_observation_policy_digest(
                    config_mod.CaptureConfig()
                ),
                outcome="policy_excluded",
            ),
        )

    assert cli._clean_timeline() == 1

    with fts.cursor() as conn:
        assert timeline_store.get_latest_end(conn) is None
        assert timeline_store.get_processed_through(conn) is None
        assert conn.execute("SELECT COUNT(*) FROM timeline_window_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM timeline_capture_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM timeline_window_receipt_epoch").fetchone()[0] == 0


def test_clean_timeline_refuses_to_drop_retiring_capture_manifest(ac_root: Path) -> None:
    start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    binding = ("retiring.json", "obs_retiring", "digest", start.isoformat())
    with fts.cursor() as conn:
        timeline_store.initialize_processed_range(conn, start)
        timeline_store.record_capture_receipts(
            conn,
            bindings=[binding],
            window_start=start,
            window_end=end,
        )
        timeline_store.record_window_receipt(
            conn,
            timeline_store.make_window_receipt(
                window_start=start,
                window_end=end,
                bindings=[binding],
                policy_digest=privacy_policy.stored_observation_policy_digest(
                    config_mod.CaptureConfig()
                ),
                outcome="policy_excluded",
                raw_state="retiring",
            ),
        )

    with pytest.raises(
        RuntimeError,
        match="timeline cleanup blocked by in-progress capture retirement",
    ):
        cli._clean_timeline()

    with fts.cursor() as conn:
        receipt = timeline_store.window_receipt_for(conn, start, end)
        assert receipt is not None and receipt.raw_state == "retiring"
        assert timeline_store.capture_receipt_paths(conn) == {binding[0]}
        assert timeline_store.get_processed_range(conn) == (start, start)


def test_clean_after_existing_window_read_cannot_revive_watermark(
    ac_root: Path,
    monkeypatch,
) -> None:
    start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    capture_path = capture_scheduler._write_capture(
        _capture_dict((start + timedelta(seconds=10)).isoformat(), "retry after clean")
    )
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    with fts.cursor() as conn:
        timeline_store.initialize_processed_range(conn, start)
        block = timeline_store.TimelineBlock(
            start_time=start,
            end_time=end,
            entries=["old block"],
            apps_used=["Editor"],
            capture_count=1,
        )
        timeline_store.insert(conn, block)
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block.id),
            sources=[
                EvidenceRef(
                    kind="observation",
                    id=str(capture["observation_id"]),
                    path=capture_path.name,
                    timestamp=str(capture["timestamp"]),
                    content_hash=observation_digest(capture),
                )
            ],
        )

    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 1
    monkeypatch.setattr(timeline_tick, "_now", lambda: end)
    real_window_state = timeline_store.window_state
    cleaned = False

    def inspect_then_clean(conn, window_start, window_end):
        nonlocal cleaned
        state = real_window_state(conn, window_start, window_end)
        if state == "current" and not cleaned:
            assert cli._clean_timeline() == 1
            cleaned = True
        return state

    monkeypatch.setattr(
        timeline_tick.aggregator.store,
        "window_state",
        inspect_then_clean,
    )
    assert timeline_tick._run_once(cfg) == 0
    assert cleaned
    with fts.cursor() as conn:
        assert timeline_store.get_processed_range(conn) is None
        assert conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0] == 0

    monkeypatch.setattr(
        timeline_tick.aggregator.store,
        "window_state",
        real_window_state,
    )
    monkeypatch.setattr(
        timeline_tick.aggregator.llm_mod,
        "call_llm",
        lambda *_args, **_kwargs: type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type(
                                "Message",
                                (),
                                {"content": '{"entries":["fresh block"]}'},
                            )()
                        },
                    )()
                ]
            },
        )(),
    )
    assert timeline_tick._run_once(cfg) == 1
    with fts.cursor() as conn:
        assert timeline_store.get_processed_range(conn) == (start, end)
        assert conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0] == 1


def _capture_dict(timestamp: str, marker: str = "old crash evidence") -> dict:
    return {
        "timestamp": timestamp,
        "schema_version": 3,
        "trigger": {"event_type": "heartbeat"},
        "window_meta": {
            "app_name": "Editor",
            "title": "recovery.py",
            "bundle_id": "com.example.editor",
        },
        "visible_text": marker,
    }


def test_cold_start_backfills_old_pending_session_and_retained_capture(
    ac_root: Path,
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 21, 10, 5, tzinfo=_TZ)
    old_start = now - timedelta(hours=2)
    old_end = old_start + timedelta(minutes=1)
    capture_path = capture_scheduler._write_capture(
        _capture_dict((old_start + timedelta(seconds=10)).isoformat())
    )
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_long_downtime",
                start_time=old_start,
                end_time=old_end,
                status="ended",
            ),
        )

    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 2
    monkeypatch.setattr(timeline_tick, "_now", lambda: now)

    seen: list[tuple[datetime, list[Path]]] = []

    def materialize(_cfg, conn, *, start, end, parsed_captures, **kwargs):
        paths = [path for path, _data in parsed_captures]
        if not paths:
            return None
        seen.append((start, paths))
        return _materialize_fake_window(
            conn,
            start=start,
            end=end,
            parsed_captures=parsed_captures,
            outcome_publisher=kwargs.get("outcome_publisher"),
        )

    monkeypatch.setattr(
        timeline_tick.aggregator,
        "produce_block_for_window",
        materialize,
    )

    assert timeline_tick._run_once(cfg) == 1
    assert seen == [(old_start, [capture_path])]
    with fts.cursor() as conn:
        processed_range = timeline_store.get_processed_range(conn)
        row = session_store.get_by_id(conn, "sess_long_downtime")
    assert processed_range == (old_start, now)
    assert row is not None and row.status == "ended"


def test_pending_session_without_capture_still_seeds_coverage(
    ac_root: Path,
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 21, 10, 5, tzinfo=_TZ)
    old_start = now - timedelta(hours=1)
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_pending_empty",
                start_time=old_start,
                end_time=old_start + timedelta(minutes=1),
                status="ended",
            ),
        )
    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 2
    monkeypatch.setattr(timeline_tick, "_now", lambda: now)
    monkeypatch.setattr(
        timeline_tick.aggregator,
        "produce_block_for_window",
        lambda *_args, **_kwargs: None,
    )

    assert timeline_tick._run_once(cfg) == 0
    with fts.cursor() as conn:
        assert timeline_store.get_processed_range(conn) == (old_start, now)


def test_backfill_is_bounded_and_resumes_from_durable_upper_bound(
    ac_root: Path,
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 21, 10, 5, tzinfo=_TZ)
    old_start = now - timedelta(minutes=5)
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_paged_backfill",
                start_time=old_start,
                end_time=old_start + timedelta(minutes=1),
                status="ended",
            ),
        )
    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 1
    monkeypatch.setattr(timeline_tick, "_now", lambda: now)
    monkeypatch.setattr(timeline_tick, "_MAX_BACKFILL_WINDOWS_PER_TICK", 2)
    monkeypatch.setattr(
        timeline_tick.aggregator,
        "produce_block_for_window",
        lambda *_args, **_kwargs: None,
    )

    timeline_tick._run_once(cfg)
    with fts.cursor() as conn:
        assert timeline_store.get_processed_range(conn) == (
            old_start,
            old_start + timedelta(minutes=2),
        )

    timeline_tick._run_once(cfg)
    with fts.cursor() as conn:
        assert timeline_store.get_processed_range(conn) == (
            old_start,
            old_start + timedelta(minutes=4),
        )


def test_window_size_change_buckets_capture_from_existing_cursor(
    ac_root: Path,
    monkeypatch,
) -> None:
    base = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    old_upper = base + timedelta(minutes=3)
    capture_path = capture_scheduler._write_capture(
        _capture_dict((base + timedelta(minutes=4)).isoformat(), "after config change")
    )
    with fts.cursor() as conn:
        timeline_store.initialize_processed_range(conn, base)
        timeline_store.advance_processed_through(
            conn,
            old_upper,
            window_start=base,
        )

    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 5
    cfg.timeline.cold_lookback_minutes = 1
    monkeypatch.setattr(
        timeline_tick,
        "_now",
        lambda: base + timedelta(minutes=10),
    )
    seen: list[tuple[datetime, datetime, list[Path]]] = []

    def materialize(_cfg, conn, *, start, end, parsed_captures, **kwargs):
        paths = [path for path, _data in parsed_captures]
        seen.append((start, end, paths))
        return _materialize_fake_window(
            conn,
            start=start,
            end=end,
            parsed_captures=parsed_captures,
            outcome_publisher=kwargs.get("outcome_publisher"),
        )

    monkeypatch.setattr(
        timeline_tick.aggregator,
        "produce_block_for_window",
        materialize,
    )

    timeline_tick._run_once(cfg)

    assert seen == [
        (
            old_upper,
            old_upper + timedelta(minutes=5),
            [capture_path],
        )
    ]
    with fts.cursor() as conn:
        assert timeline_store.get_processed_through(conn) == (old_upper + timedelta(minutes=5))


def test_invalid_temp_and_future_capture_names_do_not_expand_recovery(
    ac_root: Path,
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 21, 10, 5, tzinfo=_TZ)
    buffer_dir = ac_root / "capture-buffer"
    (buffer_dir / "not-a-capture.json").write_text(json.dumps({"timestamp": "old"}))
    (buffer_dir / ".not-a-capture.json.deadbeef.tmp").write_text("private")
    capture_scheduler._write_capture(
        _capture_dict((now + timedelta(minutes=2)).isoformat(), "future")
    )
    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 2
    monkeypatch.setattr(timeline_tick, "_now", lambda: now)
    monkeypatch.setattr(
        timeline_tick.aggregator,
        "produce_block_for_window",
        lambda *_args, **_kwargs: None,
    )

    timeline_tick._run_once(cfg)
    with fts.cursor() as conn:
        assert timeline_store.get_processed_range(conn) == (
            now - timedelta(minutes=2),
            now,
        )


def test_legacy_upper_only_watermark_has_no_historical_coverage(ac_root: Path) -> None:
    with fts.cursor() as conn:
        conn.execute("DROP TABLE timeline_state")
        conn.execute(
            """
            CREATE TABLE timeline_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                processed_through TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO timeline_state(id, processed_through) VALUES (1, ?)",
            ("2026-04-21T10:00:00+08:00",),
        )
        timeline_store.ensure_schema(conn)
        assert timeline_store.get_processed_range(conn) is None
        assert not timeline_store.covers(
            conn,
            datetime(2026, 4, 21, 9, 0, tzinfo=_TZ),
        )


def test_coverage_range_compares_mixed_offsets_by_instant(ac_root: Path) -> None:
    start = datetime.fromisoformat("2026-04-21T10:00:00+14:00")
    end = datetime.fromisoformat("2026-04-21T04:00:00-05:00")
    equivalent_inside = datetime.fromisoformat("2026-04-21T08:00:00+08:00")
    with fts.cursor() as conn:
        timeline_store.initialize_processed_range(conn, start)
        timeline_store.advance_processed_through(conn, end, window_start=start)
        assert timeline_store.covers(conn, equivalent_inside)


def test_coverage_start_is_not_an_inspected_end_boundary(ac_root: Path) -> None:
    start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    with fts.cursor() as conn:
        timeline_store.initialize_processed_range(conn, start)
        assert not timeline_store.covers(conn, start)
        timeline_store.advance_processed_through(conn, end, window_start=start)
        assert not timeline_store.covers(conn, start)
        assert timeline_store.covers(conn, end)
