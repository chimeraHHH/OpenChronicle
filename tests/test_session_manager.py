from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from openchronicle.session import manager as manager_mod
from openchronicle.session.manager import SessionManager


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.monotonic_now = 0.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.monotonic_now

    def advance(self, **kwargs: float) -> None:
        delta = timedelta(**kwargs)
        self.now = self.now + delta
        self.monotonic_now += delta.total_seconds()

    def set_wall(self, value: datetime, *, elapsed_seconds: float) -> None:
        self.now = value
        self.monotonic_now += elapsed_seconds


_T0 = datetime(2026, 4, 21, 10, 0, 0, tzinfo=UTC)


def _event(bundle: str = "com.apple.dt.Xcode") -> dict:
    return {"event_type": "AXFocusedWindowChanged", "bundle_id": bundle, "window_title": ""}


def test_session_auto_starts_on_first_event() -> None:
    clock = _FakeClock(_T0)
    m = SessionManager(clock=clock)
    assert m.current_id is None
    m.on_event(_event())
    assert m.current_id is not None


def test_hard_cut_after_idle_gap() -> None:
    clock = _FakeClock(_T0)
    ended: list[tuple[str, datetime, datetime]] = []
    m = SessionManager(clock=clock, on_session_end=lambda s, a, b: ended.append((s, a, b)))

    m.on_event(_event())
    sid = m.current_id
    clock.advance(minutes=6)
    m.check_cuts()

    assert m.current_id is None
    assert len(ended) == 1
    assert ended[0][0] == sid


def test_no_hard_cut_below_threshold() -> None:
    clock = _FakeClock(_T0)
    m = SessionManager(clock=clock, gap_minutes=5)
    m.on_event(_event())
    clock.advance(minutes=4)
    m.check_cuts()
    assert m.current_id is not None


def test_timeout_cut_after_max_hours() -> None:
    clock = _FakeClock(_T0)
    m = SessionManager(clock=clock, max_session_hours=2, gap_minutes=60)
    m.on_event(_event())
    # Keep feeding events so idle-gap rule doesn't fire first.
    for _ in range(12):
        clock.advance(minutes=10)
        m.on_event(_event())
    clock.advance(minutes=1)
    m.check_cuts()
    assert m.current_id is None


def test_soft_cut_on_unrelated_app_switch() -> None:
    clock = _FakeClock(_T0)
    m = SessionManager(clock=clock, soft_cut_minutes=3)
    m.on_event(_event("com.apple.dt.Xcode"))
    clock.advance(seconds=30)
    m.on_event(_event("com.apple.Safari"))  # switch → app_switched_at set, 2 apps
    clock.advance(minutes=4)                 # stay on Safari past soft-cut threshold
    m.check_cuts()
    assert m.current_id is None


def test_frequent_switching_prevents_soft_cut() -> None:
    clock = _FakeClock(_T0)
    m = SessionManager(clock=clock, soft_cut_minutes=3)
    m.on_event(_event("com.apple.dt.Xcode"))
    clock.advance(seconds=20)
    m.on_event(_event("com.apple.Safari"))
    clock.advance(seconds=20)
    m.on_event(_event("com.apple.dt.Xcode"))
    clock.advance(seconds=20)
    m.on_event(_event("com.apple.Safari"))
    # Most recent switch is now — not "same app for 3+ min" — so nothing to cut yet.
    clock.advance(minutes=2)
    m.check_cuts()
    # Two distinct apps seen in the last 2 min → frequent switching → no cut.
    assert m.current_id is not None


def test_force_end_closes_session() -> None:
    clock = _FakeClock(_T0)
    ended: list[str] = []
    m = SessionManager(clock=clock, on_session_end=lambda s, a, b: ended.append(s))
    m.on_event(_event())
    sid = m.current_id
    assert m.force_end(reason="daily-cron") == sid
    assert m.current_id is None
    assert ended == [sid]
    assert m.force_end() is None


def test_force_end_can_persist_without_dispatching_post_close_work() -> None:
    clock = _FakeClock(_T0)
    persisted: list[str] = []
    dispatched: list[str] = []
    m = SessionManager(
        clock=clock,
        on_session_persist=lambda s, _a, _b: persisted.append(s),
        on_session_end=lambda s, _a, _b: dispatched.append(s),
    )
    m.on_event(_event())
    sid = m.current_id

    assert m.force_end(reason="shutdown", run_end_callback=False) == sid
    assert persisted == [sid]
    assert dispatched == []


def test_session_manager_drains_tracked_end_callback_thread() -> None:
    clock = _FakeClock(_T0)
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()

    def dispatch(_sid: str, _start: datetime, _end: datetime) -> threading.Thread:
        def work() -> None:
            worker_started.set()
            assert release_worker.wait(timeout=5)
            worker_finished.set()

        thread = threading.Thread(target=work, name="tracked-session-end", daemon=True)
        thread.start()
        return thread

    m = SessionManager(clock=clock, on_session_end=dispatch)
    m.on_event(_event())
    m.force_end(reason="natural-cut")
    assert worker_started.wait(timeout=5)

    drain_finished = threading.Event()
    drain = threading.Thread(
        target=lambda: (m.drain_end_callbacks(), drain_finished.set()),
        name="drain-session-end",
    )
    drain.start()
    assert not drain_finished.wait(timeout=0.1)
    release_worker.set()
    assert drain_finished.wait(timeout=5)
    drain.join(timeout=5)
    assert not drain.is_alive()
    assert worker_finished.is_set()


def test_check_cuts_noop_when_no_session() -> None:
    clock = _FakeClock(_T0)
    m = SessionManager(clock=clock)
    m.check_cuts()  # must not raise
    assert m.current_id is None


def test_session_end_callback_receives_correct_range() -> None:
    clock = _FakeClock(_T0)
    captured: list[tuple[str, datetime, datetime]] = []
    m = SessionManager(clock=clock, on_session_end=lambda s, a, b: captured.append((s, a, b)))
    m.on_event(_event())
    start = clock.now
    clock.advance(minutes=2)
    m.on_event(_event())
    last_event = clock.now
    clock.advance(minutes=6)
    m.check_cuts()
    assert len(captured) == 1
    _, a, b = captured[0]
    assert a == start
    assert b == last_event


def test_dst_fallback_uses_elapsed_time_for_idle_cut() -> None:
    zone = ZoneInfo("America/New_York")
    first_0130 = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    second_0131 = datetime(2026, 11, 1, 1, 31, tzinfo=zone, fold=1)
    clock = _FakeClock(first_0130)
    manager = SessionManager(clock=clock, gap_minutes=45, max_session_hours=10)
    manager.on_event(_event())

    clock.set_wall(second_0131, elapsed_seconds=61 * 60)
    manager.check_cuts()

    assert manager.current_id is None


def test_dst_spring_gap_does_not_inflate_idle_time() -> None:
    zone = ZoneInfo("America/New_York")
    before_gap = datetime(2026, 3, 8, 1, 30, tzinfo=zone)
    after_gap = datetime(2026, 3, 8, 3, 0, tzinfo=zone)
    clock = _FakeClock(before_gap)
    manager = SessionManager(clock=clock, gap_minutes=45, max_session_hours=10)
    manager.on_event(_event())

    clock.set_wall(after_gap, elapsed_seconds=30 * 60)
    manager.check_cuts()

    assert manager.current_id is not None


def test_backward_clock_event_never_makes_session_end_precede_start() -> None:
    clock = _FakeClock(_T0)
    captured: list[tuple[datetime, datetime]] = []
    manager = SessionManager(
        clock=clock,
        on_session_end=lambda _sid, start, end: captured.append((start, end)),
    )
    manager.on_event(_event())
    clock.set_wall(_T0 - timedelta(hours=2), elapsed_seconds=1)
    manager.on_event(_event("com.apple.Safari"))
    manager.force_end(reason="clock-rollback")

    assert len(captured) == 1
    start, end = captured[0]
    assert end.astimezone(UTC) >= start.astimezone(UTC)


def test_forward_wall_clock_jump_without_elapsed_time_does_not_cut() -> None:
    clock = _FakeClock(_T0)
    manager = SessionManager(clock=clock, gap_minutes=5, max_session_hours=2)
    manager.on_event(_event())

    clock.set_wall(_T0 + timedelta(days=1), elapsed_seconds=1)
    manager.check_cuts()

    assert manager.current_id is not None


def test_forward_event_then_rollback_uses_logical_elapsed_end_time() -> None:
    clock = _FakeClock(_T0)
    captured: list[tuple[datetime, datetime]] = []
    manager = SessionManager(
        clock=clock,
        on_session_end=lambda _sid, start, end: captured.append((start, end)),
    )
    manager.on_event(_event())

    clock.set_wall(_T0 + timedelta(days=1), elapsed_seconds=30)
    manager.on_event(_event("com.apple.Safari"))
    clock.set_wall(_T0 + timedelta(minutes=1), elapsed_seconds=30)
    manager.on_event(_event("com.apple.dt.Xcode"))
    manager.force_end(reason="clock-corrected")

    assert captured == [(_T0, _T0 + timedelta(minutes=1))]


@pytest.mark.parametrize("wall_delta", [timedelta(hours=2), -timedelta(hours=2)])
def test_persisted_capture_timestamp_is_exact_session_boundary_across_wall_jump(
    wall_delta: timedelta,
) -> None:
    clock = _FakeClock(_T0)
    captured: list[tuple[datetime, datetime]] = []
    manager = SessionManager(
        clock=clock,
        on_session_end=lambda _sid, start, end: captured.append((start, end)),
    )
    manager.on_persisted_capture(
        {**_event(), "timestamp": _T0.isoformat(timespec="milliseconds")}
    )

    clock.set_wall(_T0 + wall_delta, elapsed_seconds=60)
    persisted_second = _T0 + timedelta(minutes=1)
    manager.on_persisted_capture(
        {
            **_event("com.apple.Safari"),
            "timestamp": persisted_second.isoformat(timespec="milliseconds"),
        }
    )
    manager.force_end(reason="clock-jump")

    assert captured == [(_T0, persisted_second)]


@pytest.mark.parametrize("timestamp", [None, "", "not-a-time", "2026-08-08T10:00:00"])
def test_persisted_capture_rejects_missing_or_unaware_timestamp(timestamp: object) -> None:
    manager = SessionManager(clock=_FakeClock(_T0))

    with pytest.raises(ValueError, match="persisted capture event"):
        manager.on_persisted_capture({**_event(), "timestamp": timestamp})

    assert manager.current_id is None


def test_wake_capture_cuts_idle_session_before_refreshing_last_event() -> None:
    clock = _FakeClock(_T0)
    ended: list[tuple[str, datetime, datetime]] = []
    manager = SessionManager(
        clock=clock,
        gap_minutes=5,
        on_session_end=lambda sid, start, end: ended.append((sid, start, end)),
    )
    manager.on_persisted_capture({**_event(), "timestamp": _T0.isoformat()})
    old_id = manager.current_id

    wake_time = _T0 + timedelta(hours=8)
    clock.set_wall(wake_time, elapsed_seconds=8 * 3600)
    manager.on_persisted_capture(
        {**_event("com.apple.Safari"), "timestamp": wake_time.isoformat()}
    )

    assert ended == [(old_id, _T0, _T0)]
    snapshot = manager.current_snapshot()
    assert snapshot is not None
    assert snapshot[0] != old_id
    assert snapshot[1] == wake_time


def test_event_after_max_duration_starts_a_new_session_even_without_cut_tick() -> None:
    clock = _FakeClock(_T0)
    ended: list[tuple[datetime, datetime]] = []
    manager = SessionManager(
        clock=clock,
        gap_minutes=24 * 60,
        max_session_hours=2,
        on_session_end=lambda _sid, start, end: ended.append((start, end)),
    )
    manager.on_persisted_capture({**_event(), "timestamp": _T0.isoformat()})

    next_time = _T0 + timedelta(hours=3)
    clock.set_wall(next_time, elapsed_seconds=3 * 3600)
    manager.on_persisted_capture(
        {**_event("com.apple.Safari"), "timestamp": next_time.isoformat()}
    )

    assert ended == [(_T0, _T0)]
    snapshot = manager.current_snapshot()
    assert snapshot is not None and snapshot[1] == next_time


def test_plain_wall_clock_uses_suspend_aware_monotonic_fallback(monkeypatch) -> None:
    ticks = iter([100.0, 100.0 + 6 * 60])
    monkeypatch.setattr(manager_mod, "continuous_monotonic", lambda: next(ticks))
    manager = SessionManager(clock=lambda: _T0, gap_minutes=5)
    manager.on_event(_event())

    manager.check_cuts()

    assert manager.current_id is None


def test_backward_wall_clock_jump_cannot_hide_real_idle_elapsed_time() -> None:
    clock = _FakeClock(_T0)
    manager = SessionManager(clock=clock, gap_minutes=5, max_session_hours=2)
    manager.on_event(_event())

    clock.set_wall(_T0 - timedelta(hours=2), elapsed_seconds=6 * 60)
    manager.check_cuts()

    assert manager.current_id is None
