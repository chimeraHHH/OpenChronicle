from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openchronicle import cli
from openchronicle import config as config_mod
from openchronicle.capture import scheduler as capture_scheduler
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef, observation_digest
from openchronicle.session import store as session_store
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store
from openchronicle.timeline import tick as timeline_tick

_TZ = timezone(timedelta(hours=8))


def test_empty_windows_advance_durable_timeline_watermark(ac_root: Path, monkeypatch) -> None:
    now = datetime(2026, 4, 21, 10, 5, tzinfo=_TZ)
    cfg = config_mod.Config()
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 2
    windows: list[tuple[datetime, datetime]] = []

    monkeypatch.setattr(timeline_tick, "_now", lambda: now)

    def empty_window(_cfg, _conn, *, start, end, parsed_captures):
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


def test_clean_timeline_resets_blocks_and_producer_watermark(ac_root: Path) -> None:
    start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=start + timedelta(minutes=1),
            ),
        )
        timeline_store.advance_processed_through(conn, start + timedelta(minutes=1))

    assert cli._clean_timeline() == 1

    with fts.cursor() as conn:
        assert timeline_store.get_latest_end(conn) is None
        assert timeline_store.get_processed_through(conn) is None


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

    def materialize(_cfg, conn, *, start, end, parsed_captures):
        paths = [path for path, _data in parsed_captures]
        if not paths:
            return None
        seen.append((start, paths))
        block = timeline_store.TimelineBlock(
            start_time=start,
            end_time=end,
            entries=["[Editor] recovered old crash evidence"],
            apps_used=["Editor"],
            capture_count=len(paths),
        )
        timeline_store.insert(conn, block)
        return block

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

    def materialize(_cfg, _conn, *, start, end, parsed_captures):
        paths = [path for path, _data in parsed_captures]
        seen.append((start, end, paths))
        return None

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
