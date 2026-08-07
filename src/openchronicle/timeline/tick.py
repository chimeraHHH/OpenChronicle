"""Periodic tick that builds closed timeline windows into TimelineBlocks.

Wall-clock-aligned so windows always line up at :00/:05/:10/... regardless
of when the tick fires. Idempotent via ``store.has_window`` — safe to
re-run or re-schedule. Runs as an asyncio task inside the daemon.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from .. import paths
from ..capture import scheduler as capture_scheduler
from ..config import Config
from ..logger import get
from ..session import store as session_store
from ..store import files as store_files
from ..store import fts
from . import aggregator, store

logger = get("openchronicle.timeline")

# How often to wake up and check for new closed windows. Slightly smaller
# than the window length so closed windows are picked up within one window
# of real time.
_TICK_INTERVAL_SECONDS = 60
# Bound catch-up work per tick without discarding retained evidence. At the
# default one-minute window this advances at most one day per invocation and
# resumes durably from the exact upper bound on the next tick.
_MAX_BACKFILL_WINDOWS_PER_TICK = 1_440


def _now() -> datetime:
    return datetime.now().astimezone()


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _earliest(*values: datetime | None) -> datetime:
    candidates = [value for value in values if value is not None]
    if not candidates:
        raise ValueError("at least one timeline recovery seed is required")
    return min(candidates, key=_instant)


def _run_once(cfg: Config) -> int:
    # A CLI debug tick and the daemon tick may overlap. Serialize producer
    # state without holding either the capture-store or SQLite lock across the
    # model call.
    with store_files.file_lock(paths.root() / "timeline-producer"):
        return _run_once_locked(cfg)


def _run_once_locked(cfg: Config) -> int:
    window_minutes = max(1, int(cfg.timeline.window_minutes))
    lookback_minutes = max(0, int(cfg.timeline.cold_lookback_minutes))
    now = _now()
    current_floor = store.floor_to_window(now, window_minutes)

    # Snapshot membership before opening SQLite: capture writers take the
    # collection lock before opening their FTS transaction, so this preserves
    # the global-lock -> database-lock order and cannot deadlock with them.
    capture_paths = aggregator.capture_paths_by_window(
        current_floor,
        window_minutes,
    )
    earliest_capture = min(capture_paths, key=_instant, default=None)

    with fts.cursor() as conn:
        pending_start = session_store.earliest_pending_reduction_start(
            conn,
            max_session_hours=cfg.session.max_session_hours,
        )
        cold_start = current_floor - timedelta(minutes=lookback_minutes)
        recovery_seed = store.floor_to_window(
            _earliest(cold_start, earliest_capture, pending_start),
            window_minutes,
        )

        processed_range = store.get_processed_range(conn)
        if processed_range is None:
            # A legacy upper-only watermark cannot prove any historical lower
            # bound. Reconstruct from retained evidence and durable work.
            store.initialize_processed_range(conn, recovery_seed)
            cursor = recovery_seed
        else:
            processed_from, processed_through = processed_range
            if _instant(recovery_seed) < _instant(processed_from):
                # Newly restored/imported evidence predates the known range.
                # Rewind safely; existing blocks make replay idempotent.
                store.initialize_processed_range(conn, recovery_seed)
                cursor = recovery_seed
            else:
                cursor = processed_through

    step = timedelta(minutes=window_minutes)
    slice_end = min(
        current_floor,
        cursor + step * _MAX_BACKFILL_WINDOWS_PER_TICK,
        key=_instant,
    )
    capture_windows = aggregator.load_capture_snapshot(
        capture_paths,
        start=cursor,
        end=slice_end,
        window_minutes=window_minutes,
    )

    with fts.cursor() as conn:
        # The producer file lock should make this stable. Fail closed if an
        # explicit maintenance command nevertheless reset state between the
        # planning and materialization phases.
        current_range = store.get_processed_range(conn)
        if (
            current_range is None
            or _instant(current_range[1]) != _instant(cursor)
        ):
            logger.warning("timeline state changed during snapshot; retrying next tick")
            return 0

        produced = 0
        inspected = 0
        while (
            cursor + step <= current_floor
            and inspected < _MAX_BACKFILL_WINDOWS_PER_TICK
        ):
            window_start = cursor
            window_end = cursor + step
            block = aggregator.produce_block_for_window(
                cfg,
                conn,
                start=window_start,
                end=window_end,
                parsed_captures=capture_windows.get(window_start, []),
            )
            if block is not None:
                produced += 1
            # Advance for both populated and empty windows. A crash after a
            # block insert but before this write is safe: has_window() makes
            # the next pass idempotent, then the watermark catches up.
            store.advance_processed_through(
                conn,
                window_end,
                window_start=window_start,
            )
            cursor = window_end
            inspected += 1
        return produced


async def run_forever(cfg: Config) -> None:
    """Daemon task: every minute, materialise any closed windows."""
    logger.info(
        "timeline loop started (window=%d min, tick=%d s)",
        cfg.timeline.window_minutes, _TICK_INTERVAL_SECONDS,
    )
    while True:
        try:
            produced = await asyncio.to_thread(_run_once, cfg)
            if produced:
                logger.info("timeline: produced %d block(s) this tick", produced)
            # Clean buffer files once the aggregator has absorbed them —
            # safe cutoff is the newest block's end_time.
            try:
                with fts.cursor() as conn:
                    safe_end = store.get_processed_through(conn)
                stats = await asyncio.to_thread(
                    capture_scheduler.cleanup_buffer,
                    cfg.capture.buffer_retention_hours,
                    safe_end.isoformat() if safe_end else None,
                    screenshot_retention_hours=cfg.capture.screenshot_retention_hours,
                    max_mb=cfg.capture.buffer_max_mb,
                )
                if any(stats.values()):
                    logger.info(
                        "timeline: buffer hygiene deleted=%d stripped=%d evicted=%d",
                        stats["deleted"], stats["stripped"], stats["evicted"],
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("timeline: buffer cleanup failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("timeline tick failed: %s", exc, exc_info=True)
        await asyncio.sleep(_TICK_INTERVAL_SECONDS)


def tick_now(cfg: Config) -> int:
    """Synchronous one-shot — for CLI debug. Returns blocks produced."""
    return _run_once(cfg)
