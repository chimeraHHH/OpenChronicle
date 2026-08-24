"""Resolve the host's local IANA timezone for wall-clock scheduling."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if sys.platform == "darwin":
    # On Darwin, Python's ordinary monotonic clock is mach_absolute_time and
    # pauses during system sleep. CLOCK_MONOTONIC_RAW is backed by the
    # continuous clock and includes that elapsed suspension interval.
    _CONTINUOUS_CLOCK_ID = getattr(time, "CLOCK_MONOTONIC_RAW", None)
else:
    # Linux CLOCK_BOOTTIME includes suspend; other platforms fall back to the
    # strongest monotonic source exposed by the standard library.
    _CONTINUOUS_CLOCK_ID = getattr(time, "CLOCK_BOOTTIME", None)


def continuous_monotonic() -> float:
    """Return non-adjustable elapsed time that includes system suspension."""
    if _CONTINUOUS_CLOCK_ID is not None:
        try:
            return float(time.clock_gettime(_CONTINUOUS_CLOCK_ID))
        except (OSError, ValueError):
            pass
    return time.monotonic()


def _zone_name_from_path(value: str | Path) -> str | None:
    text = str(value)
    marker = "zoneinfo/"
    if marker not in text:
        return None
    name = text.rsplit(marker, 1)[1].strip("/")
    return name or None


def _valid_zone_name(value: str) -> str | None:
    candidate = value.strip()
    if candidate.startswith(":"):
        candidate = candidate[1:]
    if candidate.startswith("/"):
        candidate = _zone_name_from_path(candidate) or ""
    if not candidate:
        return None
    try:
        ZoneInfo(candidate)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return candidate


def local_timezone_name() -> str | None:
    """Return the best available IANA zone key for the current host."""
    configured = _valid_zone_name(os.environ.get("TZ", ""))
    if configured is not None:
        return configured

    current = datetime.now().astimezone().tzinfo
    key = _valid_zone_name(str(getattr(current, "key", "")))
    if key is not None:
        return key

    for path in (Path("/etc/localtime"), Path("/var/db/timezone/zoneinfo")):
        with contextlib.suppress(OSError):
            key = _valid_zone_name(_zone_name_from_path(path.resolve()) or "")
            if key is not None:
                return key

    with contextlib.suppress(OSError):
        key = _valid_zone_name(Path("/etc/timezone").read_text().strip())
        if key is not None:
            return key
    return None


def local_timezone() -> tzinfo:
    """Return an IANA local zone when discoverable, with a safe fallback."""
    name = local_timezone_name()
    if name is not None:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo or UTC


def local_now() -> datetime:
    """Return now in the host zone, retaining future DST transition rules."""
    return datetime.now(local_timezone())


@dataclass(frozen=True, slots=True)
class ClockSample:
    """One linearized logical-wall and suspend-aware monotonic observation."""

    wall_time: datetime
    monotonic_tick: float


class MonotonicWallClock:
    """A process-generation wall clock immune to later system-clock jumps.

    The daemon anchors one wall-clock instant to ``time.monotonic`` at startup
    and shares this object across capture persistence, session boundaries, and
    timeline production.  All three pipelines therefore observe one ordered
    time domain even if NTP or a manual clock change moves the host wall clock
    while the process is running.  A daemon restart deliberately takes a new
    wall anchor; durable replay then reconciles evidence across generations.
    """

    def __init__(
        self,
        *,
        wall_clock: Callable[[], datetime] = local_now,
        monotonic_clock: Callable[[], float] = continuous_monotonic,
    ) -> None:
        anchor = wall_clock()
        if anchor.tzinfo is None or anchor.utcoffset() is None:
            anchor = anchor.astimezone()
        self._anchor_instant = anchor.astimezone(UTC)
        self._display_zone = anchor.tzinfo or UTC
        self._monotonic_clock = monotonic_clock
        self._lock = threading.Lock()
        self._anchor_tick = float(monotonic_clock())
        self._last_tick = self._anchor_tick

    def _tick_locked(self) -> float:
        self._last_tick = max(self._last_tick, float(self._monotonic_clock()))
        return self._last_tick

    def monotonic(self) -> float:
        """Return this clock's non-decreasing elapsed-time source."""
        with self._lock:
            return self._tick_locked()

    def sample(self) -> ClockSample:
        """Atomically sample logical wall time and its exact elapsed-time tick."""
        with self._lock:
            tick = self._tick_locked()
            elapsed = tick - self._anchor_tick
            wall_time = (
                self._anchor_instant + timedelta(seconds=elapsed)
            ).astimezone(self._display_zone)
            return ClockSample(wall_time=wall_time, monotonic_tick=tick)

    def __call__(self) -> datetime:
        """Return the anchored logical wall time in the startup IANA zone."""
        return self.sample().wall_time
