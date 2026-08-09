"""Same-sample, suspend-aware activity gate for proactive display timing."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PersistedActivity:
    timestamp: datetime
    monotonic_tick: float
    bundle_id: str


class CaptureActivityGate:
    """Track the latest persisted capture in one daemon clock generation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: PersistedActivity | None = None
        self._healthy = True

    def on_persisted_capture(self, event: dict[str, object]) -> None:
        """Consume only the scheduler's private wall+tick event envelope."""
        try:
            timestamp = _timestamp(event.get("timestamp"))
            tick = _tick(event.get("_persisted_monotonic_tick"))
            bundle_id = event.get("bundle_id")
            if not isinstance(bundle_id, str):
                raise ValueError("persisted activity bundle id is invalid")
        except (TypeError, ValueError):
            with self._lock:
                self._healthy = False
            raise
        with self._lock:
            if self._latest is not None and tick < self._latest.monotonic_tick:
                self._healthy = False
                raise ValueError("persisted activity monotonic tick moved backwards")
            self._latest = PersistedActivity(timestamp, tick, bundle_id)
            self._healthy = True

    def display_allowed(self, *, now_tick: float, settle_seconds: int) -> bool:
        """Fail closed until one current-generation event has gone quiet."""
        if (
            isinstance(now_tick, bool)
            or not isinstance(now_tick, (int, float))
            or not math.isfinite(float(now_tick))
            or now_tick < 0
            or type(settle_seconds) is not int
            or not 1 <= settle_seconds <= 300
        ):
            return False
        with self._lock:
            latest = self._latest
            healthy = self._healthy
        return bool(
            healthy
            and latest is not None
            and now_tick >= latest.monotonic_tick
            and now_tick - latest.monotonic_tick >= settle_seconds
        )

    def latest(self) -> PersistedActivity | None:
        with self._lock:
            return self._latest


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("persisted activity timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("persisted activity timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted activity timestamp must include an offset")
    return parsed


def _tick(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise ValueError("persisted activity monotonic tick is invalid")
    return float(value)
