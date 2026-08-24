"""Session boundary state machine.

Ported from Einsia-Partner's ``session_manager.py`` — same three rules:

    1. Hard cut:    no events for ``gap_minutes`` (default 5)
    2. Soft cut:    focused on one unrelated app for ``soft_cut_minutes`` (default 3)
                    unless the user is frequently switching between ≥2 apps
                    in the last 2 min (that reads as one multi-app task)
    3. Timeout:     session exceeds ``max_session_hours`` (default 2)

The manager is driven by two callbacks:

  * ``on_event(trigger)`` — called from the event dispatcher for every
    capture-worthy event. Auto-starts a session when needed.
  * ``check_cuts()``      — called on a 30 s tick so idle gaps are
    detected even when no new events come in.

On session end, ``on_session_persist(session_id, start, end)`` is always fired
synchronously. ``on_session_end`` then dispatches ordinary post-close work;
the daemon can suppress that second callback during final shutdown after the
durable row has been written.
"""

from __future__ import annotations

import math
import threading
import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..local_time import continuous_monotonic, local_now
from ..logger import get

logger = get("openchronicle.session")

_RECENT_SWITCH_WINDOW = timedelta(minutes=2)


def _local_now() -> datetime:
    return local_now()


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _not_before(value: datetime, floor: datetime) -> datetime:
    """Clamp a backwards wall-clock observation to durable session order."""
    return floor if _instant(value) < _instant(floor) else value


def _elapsed_timestamp(
    start: datetime,
    *,
    elapsed_seconds: float,
    display_time: datetime,
) -> datetime:
    """Project monotonic elapsed time onto the session's starting instant."""
    delta = timedelta(seconds=max(0.0, elapsed_seconds))
    if start.tzinfo is None or start.utcoffset() is None:
        return start + delta
    display_zone = (
        display_time.tzinfo
        if display_time.tzinfo is not None and display_time.utcoffset() is not None
        else start.tzinfo
    )
    return (_instant(start) + delta).astimezone(display_zone)


def _parse_persisted_timestamp(value: object) -> datetime:
    """Parse the scheduler-owned timestamp carried by a persisted capture."""
    if not isinstance(value, str) or not value:
        raise ValueError("persisted capture event has no timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("persisted capture event has an invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted capture event timestamp must include an offset")
    return parsed


def _parse_persisted_monotonic_tick(value: object) -> float | None:
    """Parse the scheduler-private tick paired with a durable timestamp."""
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise ValueError("persisted capture event has an invalid monotonic tick")
    return float(value)


class SessionManager:
    """Tracks the current work session and decides when to cut.

    Thread-safe: ``on_event`` runs on the event-dispatcher thread while
    ``check_cuts`` runs on the tick thread. A single lock serialises both.
    """

    def __init__(
        self,
        *,
        gap_minutes: int = 5,
        soft_cut_minutes: int = 3,
        max_session_hours: int = 2,
        on_session_start: Callable[[str, datetime], None] | None = None,
        on_session_persist: Callable[[str, datetime, datetime], None] | None = None,
        on_session_end: Callable[[str, datetime, datetime], object | None] | None = None,
        clock: Callable[[], datetime] = _local_now,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._gap_minutes = gap_minutes
        self._soft_cut_minutes = soft_cut_minutes
        self._max_session_hours = max_session_hours
        self._on_session_start = on_session_start
        self._on_session_persist = on_session_persist
        self._on_session_end = on_session_end
        self._clock = clock
        inferred_monotonic = getattr(clock, "monotonic", None)
        self._monotonic_clock = monotonic_clock or (
            inferred_monotonic if callable(inferred_monotonic) else continuous_monotonic
        )

        self._lock = threading.Lock()
        # Session-end dispatch may return a started worker thread. Keep every
        # such handle until daemon shutdown joins it under the singleton
        # lease; fire-and-forget reducers must never overlap a replacement
        # daemon's writes.
        self._end_callback_threads: set[threading.Thread] = set()

        self.current_session_id: str | None = None
        self.session_start: datetime | None = None
        self._session_start_tick: float | None = None
        self.is_active: bool = False
        self.last_event_time: datetime | None = None
        self._last_event_tick: float | None = None

        self.last_app_bundle_id: str = ""
        self.app_switched_at: datetime | None = None
        self._app_switched_tick: float | None = None
        self.recent_switches: deque[tuple[float, str]] = deque(maxlen=50)
        self._recent_apps: set[str] = set()

    @property
    def current_id(self) -> str | None:
        with self._lock:
            return self.current_session_id if self.is_active else None

    def current_snapshot(self) -> tuple[str, datetime] | None:
        """Atomic ``(session_id, session_start)`` for the active session, or None."""
        with self._lock:
            if not self.is_active or self.current_session_id is None or self.session_start is None:
                return None
            return self.current_session_id, self.session_start

    def drain_end_callbacks(self) -> None:
        """Join every tracked session-end worker, including late additions."""
        while True:
            with self._lock:
                threads = tuple(self._end_callback_threads)
            if not threads:
                return
            current = threading.current_thread()
            for thread in threads:
                if thread is not current:
                    thread.join()
            with self._lock:
                self._end_callback_threads.difference_update(
                    thread for thread in threads if not thread.is_alive()
                )
                # A current-thread handle cannot be joined. This method is a
                # daemon-owner API and should not be called by an end worker;
                # fail loudly instead of silently releasing the lease.
                if current in self._end_callback_threads:
                    raise RuntimeError("an end-callback thread cannot drain itself")

    def on_event(self, trigger: dict[str, Any]) -> None:
        """Called for every capture-worthy event from the dispatcher."""
        self._record_event(
            trigger,
            persisted_timestamp=None,
            persisted_monotonic_tick=None,
        )

    def on_persisted_capture(self, trigger: dict[str, Any]) -> None:
        """Record a successfully written capture at its exact durable instant.

        Capture persistence assigns the timestamp while holding the capture
        collection lock.  Reusing that instant here prevents the first frame
        of a session from falling just before ``session_start`` and keeps the
        reducer interval identical to the evidence time domain.
        """
        self._record_event(
            trigger,
            persisted_timestamp=_parse_persisted_timestamp(trigger.get("timestamp")),
            persisted_monotonic_tick=_parse_persisted_monotonic_tick(
                trigger.get("_persisted_monotonic_tick")
            ),
        )

    def _record_event(
        self,
        trigger: dict[str, Any],
        *,
        persisted_timestamp: datetime | None,
        persisted_monotonic_tick: float | None,
    ) -> None:
        observed_dt = persisted_timestamp or self._clock()
        now_tick = (
            persisted_monotonic_tick
            if persisted_timestamp is not None and persisted_monotonic_tick is not None
            else self._monotonic_clock()
        )
        bundle_id = str(trigger.get("bundle_id") or "")

        with self._lock:
            self._cut_stale_session_before_event_locked(now_tick)
            if not self.is_active:
                self._start_locked(observed_dt, now_tick)
                now_dt = observed_dt
            elif persisted_timestamp is not None:
                # This value came from the scheduler's private post-write
                # envelope, not watcher details. Preserve the exact durable
                # capture instant while retaining monotonic ticks for cuts.
                if self._last_event_tick is not None:
                    now_tick = max(now_tick, self._last_event_tick)
                if self._session_start_tick is not None:
                    now_tick = max(now_tick, self._session_start_tick)
                now_dt = _not_before(
                    persisted_timestamp,
                    self.last_event_time or self.session_start or persisted_timestamp,
                )
            elif self.session_start is not None and self._session_start_tick is not None:
                # Once a session has started, its persisted event instants are
                # derived from monotonic elapsed time. A forward wall jump on
                # one event therefore cannot strand all later boundaries in
                # the apparent future after the clock is corrected.
                if self._last_event_tick is not None:
                    now_tick = max(now_tick, self._last_event_tick)
                now_tick = max(now_tick, self._session_start_tick)
                now_dt = _elapsed_timestamp(
                    self.session_start,
                    elapsed_seconds=now_tick - self._session_start_tick,
                    display_time=observed_dt,
                )
            else:
                now_dt = _not_before(observed_dt, self.last_event_time or observed_dt)

            if bundle_id != self.last_app_bundle_id:
                self.recent_switches.append((now_tick, bundle_id))
                self.app_switched_at = now_dt
                self._app_switched_tick = now_tick
                self.last_app_bundle_id = bundle_id

            self.last_event_time = now_dt
            self._last_event_tick = now_tick
            self._update_recent_apps_locked(now_tick)

    def _cut_stale_session_before_event_locked(self, now_tick: float) -> None:
        """Close an idle/expired session before a wake-up event can refresh it."""
        if (
            not self.is_active
            or self.last_event_time is None
            or self._last_event_tick is None
        ):
            return
        now_tick = max(now_tick, self._last_event_tick)
        idle_seconds = now_tick - self._last_event_tick
        if idle_seconds > self._gap_minutes * 60:
            logger.info(
                "session hard cut before event: idle for %.0f min (>%d min)",
                idle_seconds / 60,
                self._gap_minutes,
            )
            self._end_locked(self.last_event_time)
            return
        if self._session_start_tick is None:
            return
        duration = now_tick - self._session_start_tick
        if duration > self._max_session_hours * 3600:
            logger.info(
                "session timeout cut before event: %.1fh (>%dh)",
                duration / 3600,
                self._max_session_hours,
            )
            self._end_locked(self.last_event_time)

    def check_cuts(self) -> None:
        """Periodic tick. Detects idle gaps, soft cuts, and timeout."""
        with self._lock:
            if (
                not self.is_active
                or self.last_event_time is None
                or self._last_event_tick is None
            ):
                return

            now_tick = max(self._monotonic_clock(), self._last_event_tick)
            gap = now_tick - self._last_event_tick

            if gap > self._gap_minutes * 60:
                logger.info(
                    "session hard cut: idle for %.0f min (>%d min)",
                    gap / 60, self._gap_minutes,
                )
                self._end_locked(self.last_event_time)
                return

            if self.session_start is not None and self._session_start_tick is not None:
                duration = now_tick - self._session_start_tick
                if duration > self._max_session_hours * 3600:
                    logger.info(
                        "session timeout cut: %.1fh (>%dh)",
                        duration / 3600, self._max_session_hours,
                    )
                    self._end_locked(self.last_event_time)
                    return

            if self._app_switched_tick is not None and len(self.recent_switches) >= 2:
                since_switch = now_tick - self._app_switched_tick
                if since_switch > self._soft_cut_minutes * 60:
                    self._update_recent_apps_locked(now_tick)
                    if not self._is_frequent_switching_locked():
                        logger.info(
                            "session soft cut: app %s for %.0f min",
                            self.last_app_bundle_id, since_switch / 60,
                        )
                        self._end_locked(self.last_event_time)

    def force_end(
        self,
        *,
        reason: str = "forced",
        run_end_callback: bool = True,
    ) -> str | None:
        """Close the session, optionally suppressing post-persist dispatch."""
        with self._lock:
            if not self.is_active:
                return None
            logger.info("session force-ended: %s", reason)
            end = self.last_event_time or self._clock()
            return self._end_locked(end, run_end_callback=run_end_callback)

    def _start_locked(self, timestamp: datetime, monotonic_at: float) -> str:
        self.current_session_id = f"sess_{uuid.uuid4().hex[:12]}"
        self.session_start = timestamp
        self._session_start_tick = monotonic_at
        self.is_active = True
        self.last_event_time = timestamp
        self._last_event_tick = monotonic_at
        self.recent_switches.clear()
        self._recent_apps.clear()
        self.last_app_bundle_id = ""
        self.app_switched_at = None
        self._app_switched_tick = None
        logger.info(
            "session started: %s at %s",
            self.current_session_id, timestamp.isoformat(),
        )
        if self._on_session_start is not None:
            try:
                self._on_session_start(self.current_session_id, timestamp)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_session_start callback failed: %s", exc)
        return self.current_session_id

    def _end_locked(
        self,
        end_time: datetime,
        *,
        run_end_callback: bool = True,
    ) -> str | None:
        if not self.is_active:
            return None
        session_id = self.current_session_id
        start_time = self.session_start
        if start_time is not None:
            end_time = _not_before(end_time, start_time)

        self.is_active = False
        self.current_session_id = None
        self.session_start = None
        self._session_start_tick = None
        self._last_event_tick = None
        self.app_switched_at = None
        self._app_switched_tick = None

        logger.info(
            "session ended: %s (%s → %s)",
            session_id,
            start_time.isoformat() if start_time else "?",
            end_time.isoformat(),
        )

        if self._on_session_persist and session_id and start_time is not None:
            try:
                self._on_session_persist(session_id, start_time, end_time)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_session_persist callback failed: %s", exc)

        if (
            run_end_callback
            and self._on_session_end
            and session_id
            and start_time is not None
        ):
            try:
                callback_result = self._on_session_end(
                    session_id,
                    start_time,
                    end_time,
                )
                if isinstance(callback_result, threading.Thread):
                    self._end_callback_threads = {
                        thread
                        for thread in self._end_callback_threads
                        if thread.is_alive()
                    }
                    self._end_callback_threads.add(callback_result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_session_end callback failed: %s", exc)

        return session_id

    def _update_recent_apps_locked(self, now_tick: float) -> None:
        self._recent_apps = {
            bundle
            for tick, bundle in self.recent_switches
            if bundle and 0 <= now_tick - tick <= _RECENT_SWITCH_WINDOW.total_seconds()
        }

    def _is_frequent_switching_locked(self) -> bool:
        return len(self._recent_apps) >= 2
