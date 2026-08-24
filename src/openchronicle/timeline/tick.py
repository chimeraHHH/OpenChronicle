"""Periodic tick that builds closed timeline windows into TimelineBlocks.

Wall-clock-aligned so windows always line up at :00/:05/:10/... regardless
of when the tick fires. Idempotent via ``store.has_window`` — safe to
re-run or re-schedule. Runs as an asyncio task inside the daemon.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .. import paths
from ..capture import filenames as capture_filenames
from ..capture import scheduler as capture_scheduler
from ..capture import store_lock as capture_store_lock
from ..config import Config
from ..local_time import local_now
from ..logger import get
from ..privacy import policy as privacy_policy
from ..session import store as session_store
from ..store import files as store_files
from ..store import fts
from ..testing import failpoints
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
    return local_now()


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _in_current_zone(value: datetime | None, current: datetime) -> datetime | None:
    """Restore IANA transition rules lost by an ISO/SQLite round trip."""
    if value is None:
        return None
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        return value
    return _instant(value).astimezone(current.tzinfo)


def _earliest(*values: datetime | None) -> datetime:
    candidates = [value for value in values if value is not None]
    if not candidates:
        raise ValueError("at least one timeline recovery seed is required")
    return min(candidates, key=_instant)


def _run_once(
    cfg: Config,
    *,
    now_provider: Callable[[], datetime] | None = None,
) -> int:
    # A CLI debug tick and the daemon tick may overlap. Serialize producer
    # state without holding either the capture-store or SQLite lock across the
    # model call.
    with store_files.file_lock(paths.root() / "timeline-producer"):
        return _run_once_locked(cfg, now_provider=now_provider)


def _run_once_locked(
    cfg: Config,
    *,
    now_provider: Callable[[], datetime] | None = None,
) -> int:
    # Digest migration is the only legacy path allowed to read raw captures
    # using a v1 hash. Check first without nested locks, then perform any work
    # in the canonical review -> capture -> SQLite order before receipt mode,
    # replay planning, or cleanup can observe the upgraded state.
    with fts.cursor() as conn:
        digest_migration_pending = not store.observation_digests_v2_migrated(conn)
    if digest_migration_pending:
        with (
            store_files.review_operation_lock(),
            capture_store_lock.capture_store_lock(),
            fts.cursor() as conn,
        ):
            store.migrate_observation_digests_v2(conn)

    window_minutes = min(
        int(store.MAX_BLOCK_DURATION.total_seconds() // 60),
        max(1, int(cfg.timeline.window_minutes)),
    )
    lookback_minutes = max(0, int(cfg.timeline.cold_lookback_minutes))
    now = (now_provider or _now)()
    provisional_floor = store.floor_to_window(now, window_minutes)

    # Snapshot membership before opening SQLite: capture writers take the
    # collection lock before opening their FTS transaction, so this preserves
    # the global-lock -> database-lock order and cannot deadlock with them.
    capture_paths = aggregator.capture_paths_by_window(
        now,
        window_minutes,
    )
    capture_path_timestamps = [
        (timestamp, path)
        for paths_in_window in capture_paths.values()
        for path in paths_in_window
        if (timestamp := capture_filenames.parse_capture_stem(path.stem)) is not None
    ]
    capture_bindings = aggregator.capture_binding_snapshot(capture_paths)
    earliest_capture = min(
        (timestamp for timestamp, _path in capture_path_timestamps),
        key=_instant,
        default=None,
    )
    earliest_capture = _in_current_zone(earliest_capture, now)

    with fts.cursor() as conn:
        # Once enabled, retention cleanup requires a receipt for every capture
        # below the watermark.  Activation precedes replay so a crash cannot
        # expose upgraded, still-unreceipted JSON to deletion.
        store.activate_capture_receipts(conn)
        if not store.activate_window_receipt_epoch(conn, window_minutes):
            logger.error(
                "timeline window duration changed while durable outcomes exist; "
                "run explicit timeline clean before changing window_minutes"
            )
            return 0
        replay_range = store.get_replay_range(conn)
        pending_start = session_store.earliest_pending_reduction_start(
            conn,
            max_session_hours=cfg.session.max_session_hours,
        )
        pending_start = _in_current_zone(pending_start, now)

        processed_range = store.get_processed_range(conn)
        if processed_range is None:
            cold_start = store.add_elapsed(
                provisional_floor,
                -timedelta(minutes=lookback_minutes),
            )
            recovery_seed = store.floor_to_window(
                _earliest(cold_start, earliest_capture, pending_start),
                window_minutes,
            )
            current_floor = store.floor_to_grid(
                now,
                recovery_seed,
                window_minutes,
            )
            # A legacy upper-only watermark cannot prove any historical lower
            # bound. Reconstruct from retained evidence and durable work.
            store.initialize_processed_range(conn, recovery_seed)
            cursor = recovery_seed
        else:
            processed_from, processed_through = processed_range
            processed_from = _in_current_zone(processed_from, now) or processed_from
            processed_through = _in_current_zone(processed_through, now) or processed_through
            earliest_invalidated = _in_current_zone(
                store.earliest_invalidated_window(
                    conn,
                    processed_range,
                    policy_digest=privacy_policy.stored_observation_policy_digest(cfg.capture),
                ),
                now,
            )
            receipt_records = store.capture_receipt_records(conn)
            earliest_late_capture = min(
                (
                    timestamp
                    for timestamp, path in capture_path_timestamps
                    if (
                        path.name not in capture_bindings
                        or receipt_records.get(path.name) != capture_bindings[path.name]
                    )
                    and _instant(timestamp) < _instant(processed_through)
                ),
                key=_instant,
                default=None,
            )
            earliest_late_capture = _in_current_zone(earliest_late_capture, now)
            current_floor = store.floor_to_grid(
                now,
                processed_from,
                window_minutes,
            )
            cold_start = store.add_elapsed(
                current_floor,
                -timedelta(minutes=lookback_minutes),
            )
            recovery_seed = store.floor_to_grid(
                _earliest(
                    cold_start,
                    (
                        earliest_capture
                        if earliest_capture is not None
                        and _instant(earliest_capture) < _instant(processed_from)
                        else None
                    ),
                    earliest_late_capture,
                    earliest_invalidated,
                    pending_start,
                ),
                processed_from,
                window_minutes,
            )
            if _instant(current_floor) < _instant(processed_through):
                # A manual/NTP wall-clock rollback invalidates "empty window"
                # proof from the apparent future: new captures can now receive
                # timestamps inside that already-inspected interval. Rewind to
                # the safe current evidence seed. Existing blocks make replay
                # idempotent as the clock catches up again.
                fts.bump_content_generation(conn, "reducer")
                store.remember_replay_range(conn, processed_range)
                replay_range = store.get_replay_range(conn)
                store.initialize_processed_range(conn, recovery_seed)
                cursor = recovery_seed
            elif (
                earliest_invalidated is not None
                and _instant(earliest_invalidated) < _instant(processed_through)
            ) or (
                earliest_late_capture is not None
                and _instant(earliest_late_capture) < _instant(processed_through)
            ):
                # A capture can arrive after its timestamped window was already
                # certified (small wall-clock rollback, import, or crash
                # recovery). Receipts distinguish it from retained evidence
                # that was part of the original snapshot. Rewind before any
                # reducer can use the old empty/source-set proof.
                fts.bump_content_generation(conn, "reducer")
                store.remember_replay_range(conn, processed_range)
                replay_range = store.get_replay_range(conn)
                store.initialize_processed_range(conn, recovery_seed)
                cursor = recovery_seed
            elif _instant(recovery_seed) < _instant(processed_from):
                # Newly restored/imported evidence predates the known range.
                # Rewind safely; existing blocks make replay idempotent.
                fts.bump_content_generation(conn, "reducer")
                store.remember_replay_range(conn, processed_range)
                replay_range = store.get_replay_range(conn)
                store.initialize_processed_range(conn, recovery_seed)
                cursor = recovery_seed
            else:
                cursor = processed_through

    step = timedelta(minutes=window_minutes)
    slice_end = min(
        current_floor,
        store.add_elapsed(cursor, step * _MAX_BACKFILL_WINDOWS_PER_TICK),
        key=_instant,
    )
    capture_windows = aggregator.load_capture_snapshot(
        capture_paths,
        start=cursor,
        end=slice_end,
        window_minutes=window_minutes,
    )

    # Keep one autocommit connection for the bounded replay slice. Acquiring
    # review/capture locks while no SQLite transaction is active preserves the
    # global lock order without paying schema/PRAGMA setup per minute.
    with fts.cursor() as conn:
        current_range = store.get_processed_range(conn)
        if current_range is None or _instant(current_range[1]) != _instant(cursor):
            logger.warning("timeline state changed during snapshot; retrying next tick")
            return 0
        generation = fts.content_generation(conn, "timeline")

        produced = 0
        inspected = 0
        while (
            _instant(store.add_elapsed(cursor, step)) <= _instant(current_floor)
            and inspected < _MAX_BACKFILL_WINDOWS_PER_TICK
        ):
            window_start = cursor
            window_end = store.add_elapsed(cursor, step)
            parsed_window = capture_windows.get(_instant(window_start), [])
            try:
                current_range = store.get_processed_range(conn)
                if (
                    fts.content_generation(conn, "timeline") != generation
                    or current_range is None
                    or _instant(current_range[1]) != _instant(window_start)
                ):
                    logger.info("timeline state generation changed; retrying from fresh state")
                    return 0

                def publish_outcome(
                    outcome_start: datetime = window_start,
                    outcome_end: datetime = window_end,
                    outcome_snapshot: list = parsed_window,
                ) -> None:
                    current = store.get_processed_range(conn)
                    if (
                        fts.content_generation(conn, "timeline") != generation
                        or current is None
                        or _instant(current[1]) != _instant(outcome_start)
                    ):
                        raise aggregator.TimelineInputChanged(
                            "timeline state changed before outcome publication"
                        )
                    receipt, final_parsed = aggregator.window_receipt_for_snapshot(
                        cfg,
                        conn,
                        start=outcome_start,
                        end=outcome_end,
                        parsed_captures=outcome_snapshot,
                    )
                    failpoints.hit("timeline.watermark.before_write")
                    bindings = aggregator.capture_receipt_bindings(final_parsed)
                    store.record_capture_receipts(
                        conn,
                        bindings=bindings,
                        window_start=outcome_start,
                        window_end=outcome_end,
                    )
                    if receipt is not None:
                        store.record_window_receipt(conn, receipt)
                    store.advance_processed_through(
                        conn,
                        outcome_end,
                        window_start=outcome_start,
                    )
                    store.clear_replay_range_if_covered(conn, outcome_end)
                    failpoints.hit("timeline.watermark.after_write")

                block = aggregator.produce_block_for_window(
                    cfg,
                    conn,
                    start=window_start,
                    end=window_end,
                    parsed_captures=parsed_window,
                    previously_inspected=store.replay_range_covers_window(
                        replay_range,
                        window_start,
                        window_end,
                    ),
                    outcome_publisher=publish_outcome,
                )
                current_range = store.get_processed_range(conn)
                outcome_published = bool(
                    current_range is not None and _instant(current_range[1]) == _instant(window_end)
                )
                if not outcome_published:
                    # Empty, all-policy-excluded, and already-current block
                    # paths do not enter the block publication SAVEPOINT. They
                    # use the same publisher in their own short transaction.
                    with (
                        store_files.review_operation_lock(),
                        capture_store_lock.capture_store_lock(),
                    ):
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            publish_outcome()
                            conn.execute("COMMIT")
                        except BaseException:
                            if conn.in_transaction:
                                conn.execute("ROLLBACK")
                            raise
            except aggregator.TimelineGenerationFailed as exc:
                # A populated window must be normalized by the configured
                # model. Keep the watermark before it so the next tick retries
                # instead of publishing a lower-fidelity local substitute.
                logger.warning("timeline generation failed; retaining window for retry: %s", exc)
                return 0
            except (aggregator.TimelineInputChanged, ValueError) as exc:
                # Explicit clean/source mutation or an incompatible receipt
                # epoch leaves the watermark before this window.
                logger.info("timeline input changed; retrying from fresh state: %s", exc)
                return 0
            if block is not None:
                produced += 1
            cursor = window_end
            inspected += 1
        return produced


async def run_forever(
    cfg: Config,
    *,
    now_provider: Callable[[], datetime] | None = None,
) -> None:
    """Daemon task: every minute, materialise any closed windows."""
    logger.info(
        "timeline loop started (window=%d min, tick=%d s)",
        cfg.timeline.window_minutes,
        _TICK_INTERVAL_SECONDS,
    )
    while True:
        try:
            produced = await asyncio.to_thread(
                _run_once,
                cfg,
                now_provider=now_provider,
            )
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
                    capture_config=cfg.capture,
                )
                if any(stats.values()):
                    logger.info(
                        "timeline: buffer hygiene deleted=%d stripped=%d evicted=%d",
                        stats["deleted"],
                        stats["stripped"],
                        stats["evicted"],
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
