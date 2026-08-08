from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle import paths
from openchronicle.capture import scheduler as capture_scheduler
from openchronicle.local_time import MonotonicWallClock
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef
from openchronicle.session import store as session_store
from openchronicle.session import tick as session_tick
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store
from openchronicle.timeline import tick as timeline_tick
from openchronicle.writer import session_reducer

_DAEMON_START = datetime(2026, 4, 21, 10, 0, 10, tzinfo=UTC)


class _JumpingHostTime:
    """Separate host-wall and monotonic sources for a daemon-generation clock."""

    def __init__(self) -> None:
        self.wall = _DAEMON_START
        self.tick = 1_000.0
        self.wall_reads = 0

    def wall_now(self) -> datetime:
        self.wall_reads += 1
        return self.wall

    def monotonic(self) -> float:
        return self.tick

    def advance(self, seconds: float, *, host_wall_jump: timedelta = timedelta()) -> None:
        elapsed = timedelta(seconds=seconds)
        self.tick += seconds
        self.wall += elapsed + host_wall_jump


def _capture(label: str) -> dict[str, object]:
    return {
        # Deliberately bogus after a host jump: the scheduler must replace it
        # under the capture-store lock using the daemon-generation clock.
        "timestamp": "2000-01-01T00:00:00+00:00",
        "schema_version": 4,
        "trigger": {
            "event_type": "manual",
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "window_title": f"clock-{label}.md",
        },
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "title": f"clock-{label}.md",
        },
        "focused_element": {
            "role": "AXTextArea",
            "value": f"durable {label} clock evidence",
            "is_editable": True,
            "value_length": 30,
        },
        "visible_text": f"durable {label} clock evidence",
        "url": "",
        "_timestamp_at_persist": True,
    }


def _as_instant(value: datetime) -> datetime:
    return value.astimezone(UTC)


@pytest.mark.parametrize(
    ("jump_name", "host_wall_jump"),
    [
        pytest.param("forward", timedelta(hours=2), id="host-wall-forward"),
        pytest.param("backward", -timedelta(hours=2), id="host-wall-backward"),
    ],
)
def test_daemon_clock_keeps_capture_session_timeline_and_terminal_reducer_coherent(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    jump_name: str,
    host_wall_jump: timedelta,
) -> None:
    host_time = _JumpingHostTime()
    runtime_clock = MonotonicWallClock(
        wall_clock=host_time.wall_now,
        monotonic_clock=host_time.monotonic,
    )
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    # Close the session synchronously, then invoke the terminal reducer only
    # after the real timeline producer has covered its trailing bucket.
    cfg.reducer.enabled = False

    manager = session_tick.build_manager(
        cfg,
        daemon_lease_held=True,
        clock=runtime_clock,
    )
    built_captures = [_capture(f"{jump_name}-first"), _capture(f"{jump_name}-second")]
    pending_captures = iter(built_captures)
    monkeypatch.setattr(
        capture_scheduler,
        "_build_capture",
        lambda *_args, **_kwargs: next(pending_captures),
    )
    runner = capture_scheduler._CaptureRunner(
        cfg.capture,
        object(),
        pre_capture_hook=manager.on_persisted_capture,
        timestamp_provider=runtime_clock,
    )

    runner.run({"event_type": "manual"})
    host_time.advance(70, host_wall_jump=host_wall_jump)
    runner.run({"event_type": "manual"})

    first_instant = _DAEMON_START
    second_instant = _DAEMON_START + timedelta(seconds=70)
    assert runtime_clock() == second_instant
    assert host_time.wall == second_instant + host_wall_jump
    assert host_time.wall != runtime_clock()

    session_id = manager.current_id
    assert session_id is not None
    manager.force_end(reason=f"test-{jump_name}-wall-clock")

    capture_files = sorted(paths.capture_buffer_dir().glob("*.json"))
    assert len(capture_files) == 2
    persisted_captures = [
        (json.loads(path.read_text(encoding="utf-8")), path) for path in capture_files
    ]
    persisted_captures.sort(key=lambda item: item[0]["timestamp"])
    capture_instants = [
        datetime.fromisoformat(capture["timestamp"]).astimezone(UTC)
        for capture, _path in persisted_captures
    ]
    assert capture_instants == [first_instant, second_instant]

    with fts.cursor() as conn:
        session = session_store.get_by_id(conn, session_id)
        indexed = fts.search_captures(conn, query="durable clock evidence", limit=10)
    assert session is not None
    assert session.status == "ended"
    assert session.end_time is not None
    assert _as_instant(session.start_time) == capture_instants[0]
    assert _as_instant(session.end_time) == capture_instants[-1]
    assert {_as_instant(datetime.fromisoformat(hit.timestamp)) for hit in indexed} == set(
        capture_instants
    )

    # Move only monotonic elapsed far enough to close the second one-minute
    # bucket. The same clock object is the producer's now_provider.
    host_time.advance(70)
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv(
        "OPENCHRONICLE_LLM_MOCK_JSON",
        json.dumps({"entries": [f"[Editor] retained {jump_name} jump evidence"]}),
    )
    assert timeline_tick._run_once(cfg, now_provider=runtime_clock) == 2

    expected_observation_paths = {path.name for _capture_data, path in persisted_captures}
    with fts.cursor() as conn:
        blocks = timeline_store.query_since(conn, first_instant - timedelta(minutes=1))
        processed_range = timeline_store.get_processed_range(conn)
        block_sources = {
            block.id: provenance_store.direct_sources(
                conn,
                EvidenceRef(kind="timeline_block", id=block.id),
            )
            for block in blocks
        }
    assert len(blocks) == 2
    assert sum(block.capture_count for block in blocks) == 2
    assert {
        source.path
        for sources in block_sources.values()
        for source in sources
        if source.kind == "observation"
    } == expected_observation_paths
    assert session_reducer._terminal_timeline_ready(
        blocks=blocks,
        session_end=session.end_time,
        window_minutes=cfg.timeline.window_minutes,
        processed_range=processed_range,
    )

    reduced_block_ids: list[str] = []

    def _reducer_payload(_cfg, reducer_blocks, *_args, **_kwargs):
        reduced_block_ids.extend(block.id for block in reducer_blocks)
        return {
            "summary": f"retained {jump_name} wall-clock jump evidence",
            "sub_tasks": [f"[10:00-10:02, Editor] retained {jump_name} wall-clock jump evidence"],
        }

    monkeypatch.setattr(session_reducer, "_call_reducer_llm", _reducer_payload)
    result = session_reducer.reduce_session(
        cfg,
        session_id=session_id,
        start_time=session.start_time,
        end_time=session.end_time,
    )

    assert result.succeeded is True
    assert result.written is True
    assert set(reduced_block_ids) == {block.id for block in blocks}
    assert result.path
    assert (paths.memory_dir() / result.path).exists()
    with fts.cursor() as conn:
        reduced_session = session_store.get_by_id(conn, session_id)
    assert reduced_session is not None
    assert reduced_session.status == "reduced"
    assert reduced_session.classifier_terminal_noop is False
    # The daemon-generation wall anchor is sampled once; later host-wall
    # reads cannot leak into any of the three shared timestamp consumers.
    assert host_time.wall_reads == 1


def test_suspend_between_capture_write_and_session_hook_keeps_one_clock_sample(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_time = _JumpingHostTime()
    runtime_clock = MonotonicWallClock(
        wall_clock=host_time.wall_now,
        monotonic_clock=host_time.monotonic,
    )
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.capture.deny_unknown_windows = False
    cfg.session.gap_minutes = 5
    cfg.session.max_session_hours = 2
    cfg.reducer.enabled = False
    manager = session_tick.build_manager(
        cfg,
        daemon_lease_held=True,
        clock=runtime_clock,
    )
    built_captures = iter(
        [_capture("before-sleep"), _capture("at-sleep"), _capture("after-wake")]
    )
    monkeypatch.setattr(
        capture_scheduler,
        "_build_capture",
        lambda *_args, **_kwargs: next(built_captures),
    )
    real_write = capture_scheduler._write_capture
    suspend_after_second_write = False

    def write_then_maybe_suspend(out):
        nonlocal suspend_after_second_write
        path = real_write(out)
        if suspend_after_second_write:
            suspend_after_second_write = False
            host_time.advance(8 * 3600)
        return path

    monkeypatch.setattr(capture_scheduler, "_write_capture", write_then_maybe_suspend)
    runner = capture_scheduler._CaptureRunner(
        cfg.capture,
        object(),
        pre_capture_hook=manager.on_persisted_capture,
        timestamp_provider=runtime_clock,
    )

    runner.run({"event_type": "manual"})
    first_session_id = manager.current_id
    assert first_session_id is not None
    host_time.advance(60)
    suspend_after_second_write = True
    runner.run({"event_type": "manual"})

    # The post-write hook runs after the simulated eight-hour suspension, but
    # it must still admit the capture at the pre-sleep tick paired with 10:01.
    assert manager.current_id == first_session_id
    assert manager.last_event_time == _DAEMON_START + timedelta(minutes=1)

    host_time.advance(60)
    runner.run({"event_type": "manual"})

    assert manager.current_id is not None
    assert manager.current_id != first_session_id
    assert manager.session_start == _DAEMON_START + timedelta(hours=8, minutes=2)
    with fts.cursor() as conn:
        ended = session_store.get_by_id(conn, first_session_id)
    assert ended is not None
    assert ended.status == "ended"
    assert ended.start_time == _DAEMON_START
    assert ended.end_time == _DAEMON_START + timedelta(minutes=1)
