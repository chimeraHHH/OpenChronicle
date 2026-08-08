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

import threading
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..logger import get

logger = get("openchronicle.session")

_RECENT_SWITCH_WINDOW = timedelta(minutes=2)


def _local_now() -> datetime:
    return datetime.now().astimezone()


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
    ) -> None:
        self._gap_minutes = gap_minutes
        self._soft_cut_minutes = soft_cut_minutes
        self._max_session_hours = max_session_hours
        self._on_session_start = on_session_start
        self._on_session_persist = on_session_persist
        self._on_session_end = on_session_end
        self._clock = clock

        self._lock = threading.Lock()
        # Session-end dispatch may return a started worker thread. Keep every
        # such handle until daemon shutdown joins it under the singleton
        # lease; fire-and-forget reducers must never overlap a replacement
        # daemon's writes.
        self._end_callback_threads: set[threading.Thread] = set()

        self.current_session_id: str | None = None
        self.session_start: datetime | None = None
        self.is_active: bool = False
        self.last_event_time: datetime | None = None

        self.last_app_bundle_id: str = ""
        self.app_switched_at: datetime | None = None
        self.recent_switches: deque[tuple[datetime, str]] = deque(maxlen=50)
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
        now_dt = self._clock()
        bundle_id = str(trigger.get("bundle_id") or "")

        with self._lock:
            if not self.is_active:
                self._start_locked(now_dt)

            if bundle_id != self.last_app_bundle_id:
                self.recent_switches.append((now_dt, bundle_id))
                self.app_switched_at = now_dt
                self.last_app_bundle_id = bundle_id

            self.last_event_time = now_dt
            self._update_recent_apps_locked(now_dt)

    def check_cuts(self) -> None:
        """Periodic tick. Detects idle gaps, soft cuts, and timeout."""
        with self._lock:
            if not self.is_active or self.last_event_time is None:
                return

            now = self._clock()
            gap = (now - self.last_event_time).total_seconds()

            if gap > self._gap_minutes * 60:
                logger.info(
                    "session hard cut: idle for %.0f min (>%d min)",
                    gap / 60, self._gap_minutes,
                )
                self._end_locked(self.last_event_time)
                return

            if self.session_start is not None:
                duration = (now - self.session_start).total_seconds()
                if duration > self._max_session_hours * 3600:
                    logger.info(
                        "session timeout cut: %.1fh (>%dh)",
                        duration / 3600, self._max_session_hours,
                    )
                    self._end_locked(self.last_event_time)
                    return

            if self.app_switched_at is not None and len(self.recent_switches) >= 2:
                since_switch = (now - self.app_switched_at).total_seconds()
                if since_switch > self._soft_cut_minutes * 60:
                    self._update_recent_apps_locked(now)
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

    def _start_locked(self, timestamp: datetime) -> str:
        self.current_session_id = f"sess_{uuid.uuid4().hex[:12]}"
        self.session_start = timestamp
        self.is_active = True
        self.last_event_time = timestamp
        self.recent_switches.clear()
        self._recent_apps.clear()
        self.last_app_bundle_id = ""
        self.app_switched_at = None
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

        self.is_active = False
        self.current_session_id = None
        self.session_start = None
        self.app_switched_at = None

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

    def _update_recent_apps_locked(self, now: datetime) -> None:
        cutoff = now - _RECENT_SWITCH_WINDOW
        self._recent_apps = {
            bundle for ts, bundle in self.recent_switches if ts >= cutoff and bundle
        }

    def _is_frequent_switching_locked(self) -> bool:
        return len(self._recent_apps) >= 2
