from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from openchronicle import local_time
from openchronicle.session import manager as session_manager
from openchronicle.timeline import tick as timeline_tick


def test_local_now_keeps_iana_rules_from_tz_environment(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")

    now = local_time.local_now()

    assert isinstance(now.tzinfo, ZoneInfo)
    assert now.tzinfo.key == "America/New_York"


def test_runtime_clocks_share_rule_aware_local_zone(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")

    session_now = session_manager._local_now()
    timeline_now = timeline_tick._now()

    assert isinstance(session_now.tzinfo, ZoneInfo)
    assert isinstance(timeline_now.tzinfo, ZoneInfo)
    assert session_now.tzinfo.key == "America/New_York"
    assert timeline_now.tzinfo.key == "America/New_York"


def test_macos_zoneinfo_symlink_is_recognized(monkeypatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(
        local_time,
        "local_timezone_name",
        lambda: local_time._zone_name_from_path(
            "/var/db/timezone/tz/2026c.1.0/zoneinfo/Asia/Shanghai"
        ),
    )

    zone = local_time.local_timezone()

    assert isinstance(zone, ZoneInfo)
    assert zone.key == "Asia/Shanghai"


def test_monotonic_wall_clock_ignores_later_host_wall_jumps() -> None:
    wall = {"now": datetime(2026, 8, 8, 10, 0, tzinfo=UTC)}
    elapsed = {"seconds": 100.0}
    clock = local_time.MonotonicWallClock(
        wall_clock=lambda: wall["now"],
        monotonic_clock=lambda: elapsed["seconds"],
    )

    wall["now"] += timedelta(hours=12)
    elapsed["seconds"] += 30
    assert clock() == datetime(2026, 8, 8, 10, 0, 30, tzinfo=UTC)

    wall["now"] -= timedelta(days=2)
    elapsed["seconds"] += 30
    assert clock() == datetime(2026, 8, 8, 10, 1, tzinfo=UTC)


def test_monotonic_wall_clock_clamps_a_decreasing_elapsed_source() -> None:
    elapsed = {"seconds": 100.0}
    clock = local_time.MonotonicWallClock(
        wall_clock=lambda: datetime(2026, 8, 8, 10, 0, tzinfo=UTC),
        monotonic_clock=lambda: elapsed["seconds"],
    )
    elapsed["seconds"] = 101.0
    first = clock()
    elapsed["seconds"] = 99.0

    assert clock() == first
    assert clock.monotonic() == 101.0


def test_monotonic_wall_clock_preserves_spring_transition_rules() -> None:
    zone = ZoneInfo("America/New_York")
    elapsed = {"seconds": 0.0}
    clock = local_time.MonotonicWallClock(
        wall_clock=lambda: datetime(2026, 3, 8, 1, 59, tzinfo=zone),
        monotonic_clock=lambda: elapsed["seconds"],
    )
    elapsed["seconds"] = 120.0

    value = clock()

    assert (value.hour, value.minute, value.fold) == (3, 1, 0)
    assert value.utcoffset() == timedelta(hours=-4)
    assert value.astimezone(UTC) == datetime(2026, 3, 8, 7, 1, tzinfo=UTC)


def test_monotonic_wall_clock_preserves_fall_transition_fold() -> None:
    zone = ZoneInfo("America/New_York")
    elapsed = {"seconds": 0.0}
    clock = local_time.MonotonicWallClock(
        wall_clock=lambda: datetime(2026, 11, 1, 1, 59, tzinfo=zone, fold=0),
        monotonic_clock=lambda: elapsed["seconds"],
    )
    elapsed["seconds"] = 120.0

    value = clock()

    assert (value.hour, value.minute, value.fold) == (1, 1, 1)
    assert value.utcoffset() == timedelta(hours=-5)
    assert value.astimezone(UTC) == datetime(2026, 11, 1, 6, 1, tzinfo=UTC)


def test_continuous_monotonic_prefers_suspend_aware_clock(monkeypatch) -> None:
    monkeypatch.setattr(local_time, "_CONTINUOUS_CLOCK_ID", 123)
    monkeypatch.setattr(local_time.time, "clock_gettime", lambda clock_id: 45.5 + clock_id)
    monkeypatch.setattr(
        local_time.time,
        "monotonic",
        lambda: (_ for _ in ()).throw(AssertionError("fallback clock was used")),
    )

    assert local_time.continuous_monotonic() == 168.5


def test_continuous_monotonic_falls_back_when_platform_clock_fails(monkeypatch) -> None:
    monkeypatch.setattr(local_time, "_CONTINUOUS_CLOCK_ID", 123)
    monkeypatch.setattr(
        local_time.time,
        "clock_gettime",
        lambda _clock_id: (_ for _ in ()).throw(OSError("unavailable")),
    )
    monkeypatch.setattr(local_time.time, "monotonic", lambda: 67.5)

    assert local_time.continuous_monotonic() == 67.5
