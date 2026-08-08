"""Async daemon wiring for the session/reducer pipeline.

Five periodic asyncio tasks live here:

  * ``run_check_cuts`` — calls ``SessionManager.check_cuts`` every
    ``session.tick_seconds`` so idle gaps / soft cuts fire even when
    the dispatcher is quiet.
  * ``run_daily_safety_net`` — once per local day at HH:MM (from
    ``reducer.daily_tick_hour/minute``), force-ends the currently open
    session, retries any ``failed`` sessions, and covers the edge case
    where the process was offline across midnight.
  * ``run_flush_tick`` — materializes new active-session timeline blocks.
  * ``run_classifier_tick`` — best-effort periodic durable-fact extraction.
  * ``run_pending_reduction_tick`` — retries ended sessions after the timeline
    producer watermark reaches their terminal bucket.
  * ``build_manager`` — factory that wires ``on_session_end`` to
    persist a ``sessions`` row and spawn the S2 reducer thread.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ..config import Config
from ..logger import get
from ..store import fts
from ..writer import classifier_delivery, classifier_jobs, session_reducer
from . import store as session_store
from .manager import SessionManager

logger = get("openchronicle.session")

_EMPTY_SESSION_FALLBACK = timedelta(minutes=1)


def _pid_is_alive(pid: int) -> bool:
    """Return whether ``pid`` still names a process we must not disturb."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        # A process owned by another user is still live; inability to signal it
        # is a reason to protect the row, not to declare it orphaned.
        return True
    except OSError:
        # Unknown platform-specific errors are handled conservatively: a
        # possibly-live owner must not be force-ended.
        return True
    return True


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _earlier(left: datetime, right: datetime) -> datetime:
    return left if _instant(left) <= _instant(right) else right


def _later(left: datetime, right: datetime) -> datetime:
    return left if _instant(left) >= _instant(right) else right


def _infer_recovery_end(
    conn,
    *,
    start: datetime,
    restart_time: datetime,
    max_session_hours: int,
) -> datetime:
    """Infer a crash boundary without crossing a later session or restart."""
    # A backwards wall-clock jump must never create end < start.  Conversely,
    # a fast restart must not use the upstream one-minute fallback beyond the
    # restart boundary, where it could overlap the new boot's first session.
    upper_bound = _later(start, restart_time)
    ceiling = start + timedelta(hours=max(0, max_session_hours))
    upper_bound = _earlier(upper_bound, ceiling)

    next_start = session_store.next_session_start_after(conn, start)
    if next_start is not None and _instant(next_start) > _instant(start):
        upper_bound = _earlier(upper_bound, next_start)

    block_end = session_store.latest_timeline_end_in_window(
        conn,
        start=start,
        end=upper_bound,
    )
    candidate = block_end or (start + _EMPTY_SESSION_FALLBACK)
    return _earlier(upper_bound, _later(start, candidate))


def recover_orphan_sessions(
    cfg: Config,
    *,
    now: datetime | None = None,
    pid_is_alive: Callable[[int], bool] | None = None,
    daemon_lease_held: bool = False,
) -> int:
    """End active rows whose owning daemon is gone and make them reducible.

    This runs synchronously before the new ``SessionManager`` is constructed.
    When the caller holds the singleton daemon lease, no different process can
    legitimately own an active row for this store; treating a merely-live PID
    as authoritative there would let PID reuse strand a crash survivor forever.
    Without that lease, rows owned by a live PID are deliberately skipped,
    preserving the conservative behavior for direct/library callers.
    Updates are conditional on ``status='active'``, so retrying after a crash
    during recovery is idempotent.
    """
    restart_time = now or datetime.now().astimezone()
    owner_is_alive = pid_is_alive or _pid_is_alive
    recovered = 0

    with fts.cursor() as conn:
        for row in session_store.list_active(conn):
            owned_by_this_process = (
                row.owner_pid == os.getpid()
                and row.owner_token == session_store.current_owner_token()
            )
            owner_is_another_live_process = (
                not daemon_lease_held
                and row.owner_pid is not None
                and row.owner_pid != os.getpid()
                and owner_is_alive(row.owner_pid)
            )
            if owned_by_this_process or owner_is_another_live_process:
                logger.info(
                    "session recovery skipped live owner: session=%s pid=%s",
                    row.id,
                    row.owner_pid,
                )
                continue

            end_time = _infer_recovery_end(
                conn,
                start=row.start_time,
                restart_time=restart_time,
                max_session_hours=cfg.session.max_session_hours,
            )
            if session_store.mark_ended(conn, row.id, end_time):
                recovered += 1
                logger.info(
                    "recovered orphan session %s: %s -> %s",
                    row.id,
                    row.start_time.isoformat(),
                    end_time.isoformat(),
                )

    return recovered


def build_manager(
    cfg: Config,
    *,
    daemon_lease_held: bool = False,
) -> SessionManager:
    """Construct a SessionManager whose end-callback wires the reducer."""

    # This is the daemon's restart boundary: no manager from this boot exists
    # yet, so any unowned/dead-owner active row is a hard-crash survivor.
    try:
        recovered = recover_orphan_sessions(
            cfg,
            daemon_lease_held=daemon_lease_held,
        )
        if recovered:
            logger.info(
                "session startup recovery moved %d row(s) to pending reduction",
                recovered,
            )
    except Exception as exc:  # noqa: BLE001
        # Capture should still start if recovery encounters corrupt legacy
        # data.  The untouched active row remains available for a later retry.
        logger.error("session startup recovery failed: %s", exc, exc_info=True)

    def _on_start(session_id: str, start: datetime) -> None:
        """Persist an 'active' row immediately so crashes are recoverable."""
        with fts.cursor() as conn:
            session_store.insert(
                conn,
                session_store.SessionRow(
                    id=session_id,
                    start_time=start,
                    status="active",
                    owner_pid=os.getpid(),
                    owner_token=session_store.current_owner_token(),
                ),
            )

    def _persist_end(session_id: str, start: datetime, end: datetime) -> None:
        """Durably close the row before any optional reducer dispatch."""
        with fts.cursor() as conn:
            existing = session_store.get_by_id(conn, session_id)
            if existing is None:
                session_store.insert(
                    conn,
                    session_store.SessionRow(
                        id=session_id,
                        start_time=start,
                        end_time=end,
                        status="ended",
                    ),
                )
            else:
                session_store.mark_ended(conn, session_id, end)

    def _on_end(session_id: str, start: datetime, end: datetime):
        if not cfg.reducer.enabled:
            logger.info("reducer disabled — session %s stored without reduce", session_id)
            return

        return session_reducer.reduce_session_async(
            cfg,
            session_id=session_id,
            start_time=start,
            end_time=end,
            on_done=_after_reduce,
        )

    def _after_reduce(result: session_reducer.ReduceResult) -> None:
        """Drain the terminal intent persisted by the reducer."""
        if not result.is_final:
            # Incremental flushes are handled by run_classifier_tick on its
            # own cadence — the reducer callback only fires the terminal
            # catch-up for any trailing window the tick hadn't reached yet.
            return
        try:
            classifier_delivery.run_recovery_pass(cfg, limit=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "classifier delivery %s crashed: %s",
                result.session_id,
                exc,
                exc_info=True,
            )

    return SessionManager(
        gap_minutes=cfg.session.gap_minutes,
        soft_cut_minutes=cfg.session.soft_cut_minutes,
        max_session_hours=cfg.session.max_session_hours,
        on_session_start=_on_start,
        on_session_persist=_persist_end,
        on_session_end=_on_end,
    )


async def run_check_cuts(cfg: Config, manager: SessionManager) -> None:
    """Periodic check_cuts tick."""
    interval = max(5, int(cfg.session.tick_seconds))
    logger.info("session check_cuts loop started (every %ds)", interval)
    while True:
        try:
            await asyncio.to_thread(manager.check_cuts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("session check_cuts failed: %s", exc, exc_info=True)
        await asyncio.sleep(interval)


async def run_flush_tick(cfg: Config, manager: SessionManager) -> None:
    """Incremental reducer tick for the active session.

    Every ``session.flush_minutes`` (min 5) checks for an active session and
    reduces any closed timeline blocks since the last flush into a partial
    entry in the event-daily file. This loop does not invoke the classifier;
    a separate periodic classifier task and a terminal catch-up do that work.
    """
    if not cfg.reducer.enabled:
        logger.info("flush tick loop not started (reducer disabled)")
        return
    interval = max(300, int(cfg.session.flush_minutes) * 60)
    logger.info("session flush loop started (every %ds)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            snap = manager.current_snapshot()
            if snap is None:
                continue
            session_id, session_start = snap
            await asyncio.to_thread(
                session_reducer.flush_active_session,
                cfg,
                session_id=session_id,
                session_start=session_start,
                now=datetime.now().astimezone(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("session flush tick failed: %s", exc, exc_info=True)


async def run_classifier_tick(cfg: Config, manager: SessionManager) -> None:
    """Poll durable classifier requests and enqueue proven flush coverage."""
    if not cfg.reducer.enabled:
        logger.info("classifier tick loop not started (reducer disabled)")
        return
    interval = max(300, int(cfg.classifier.interval_minutes) * 60)
    poll_seconds = max(
        5,
        min(60, int(getattr(cfg.classifier, "retry_seconds", 60))),
    )
    logger.info(
        "classifier delivery loop started (cadence=%ds, retry poll=%ds)",
        interval,
        poll_seconds,
    )
    while True:
        try:
            snap = manager.current_snapshot()
            if snap is not None:
                session_id, _ = snap
                with fts.cursor() as conn:
                    row = session_store.get_by_id(conn, session_id)
                    if row is not None and row.flush_end is not None:
                        cursor = row.classified_end or row.start_time
                        if (
                            _instant(row.flush_end) > _instant(cursor)
                            and (_instant(row.flush_end) - _instant(cursor)).total_seconds()
                            >= interval
                        ):
                            classifier_jobs.request(
                                conn,
                                session_id=session_id,
                                requested_end=row.flush_end,
                                include_prior_day=row.classified_end is None,
                            )
            await asyncio.to_thread(classifier_delivery.run_recovery_pass, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("classifier delivery tick failed: %s", exc, exc_info=True)
        await asyncio.sleep(poll_seconds)


async def run_pending_reduction_tick(cfg: Config) -> None:
    """Retry durable ended/failed rows after late timeline blocks arrive."""
    if not cfg.reducer.enabled:
        logger.info("pending reducer loop not started (reducer disabled)")
        return
    interval = 60
    logger.info("pending reducer loop started (every %ds)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(session_reducer.reduce_all_pending, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("pending reducer tick failed: %s", exc, exc_info=True)


def _seconds_until_next_local(hour: int, minute: int) -> float:
    """Seconds from now until the next local-time HH:MM."""
    now = datetime.now().astimezone()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


async def run_daily_safety_net(cfg: Config, manager: SessionManager) -> None:
    """Once per local day at HH:MM, force-end open session + retry failed."""
    hour = cfg.reducer.daily_tick_hour
    minute = cfg.reducer.daily_tick_minute
    logger.info("daily safety-net loop started (fires at %02d:%02d local)", hour, minute)
    while True:
        try:
            wait = _seconds_until_next_local(hour, minute)
            await asyncio.sleep(wait)
            logger.info("daily safety-net tick: force-ending open session + reducing pending rows")
            await asyncio.to_thread(manager.force_end, reason="daily-safety-net")
            if cfg.reducer.enabled:
                # Give the just-force-ended session's async reducer thread a
                # chance to finish before the catch-up pass would re-process it.
                await asyncio.sleep(2)
                await asyncio.to_thread(session_reducer.reduce_all_pending, cfg)
            # Truncate the WAL sidecar after the heavy daily writes settle —
            # auto-checkpoint resets the WAL pointer but never shrinks the
            # file, so without this the sidecar drifts unbounded.
            try:
                busy, log_pages, ckpt_pages = await asyncio.to_thread(fts.checkpoint)
                logger.info(
                    "daily wal_checkpoint(TRUNCATE): busy=%d log=%d checkpointed=%d",
                    busy,
                    log_pages,
                    ckpt_pages,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("daily wal_checkpoint failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("daily safety-net failed: %s", exc, exc_info=True)
            # Sleep a minute so a tight error loop doesn't hammer the CPU.
            await asyncio.sleep(60)
