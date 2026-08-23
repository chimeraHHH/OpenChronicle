"""S2 session reducer: closed session → event-YYYY-MM-DD.md entry.

Ported from Einsia-Partner's ``s2_aggregator`` but writes to Markdown
files instead of a session DB table. For a session that just ended:

  1. Query ``timeline_blocks`` in ``[start, end)``.
  2. Render them into a prompt, call the LLM (stage ``reducer``).
  3. Parse ``{summary, sub_tasks}`` and append one entry to
     ``event-<session-start-local-date>.md`` — creating the file if it
     doesn't exist yet.
  4. On LLM success mark the session row ``reduced``; on failure keep the
     session ``failed`` and schedule another retry. Provider failures never
     materialize locally guessed activity as memory.

This module is called from two places:

  * The SessionManager's ``on_session_end`` callback — spawns
    ``reduce_session`` on a daemon thread so the dispatcher doesn't
    block on LLM latency.
  * The daily 23:55 cron / retry tick — calls ``retry_due`` which
    picks up any ``failed`` rows whose ``next_retry_at`` has elapsed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import paths
from ..capture import filenames as capture_filenames
from ..config import Config
from ..local_time import local_timezone
from ..logger import get
from ..memory_candidates import store as candidate_store
from ..privacy.egress import model_egress_lock, privacy_egress_lock
from ..prompts import load as load_prompt
from ..provenance.models import EvidenceRef, content_digest, timeline_block_digest
from ..services.context import ContextService
from ..session import store as session_store
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts
from ..timeline import store as timeline_store
from . import llm as llm_mod
from . import tools as tools_mod

# Number of preceding entries from the same event-YYYY-MM-DD.md file to show
# the reducer as context. Lets a new session summary align with / avoid
# duplicating entries that earlier sessions (or earlier flushes of this same
# session) already wrote for the same day.
_PRECEDING_ENTRY_LIMIT = 6

logger = get("openchronicle.writer")

# The index into this tuple is the attempt counter *before* the retry — a
# freshly-failed row (retry_count=0) schedules at 5 minutes. Once every step
# has been used, retries continue at the final interval. A provider outage may
# leave work pending, but it must never manufacture a local summary.
_RETRY_BACKOFF_MINUTES: tuple[int, ...] = (5, 15, 30, 60, 120)
_REDUCTION_LOCK_SHARDS = 256


class ReducerInputChanged(RuntimeError):
    """A privacy reset invalidated evidence read by an in-flight reducer."""


class TimelineProjectionInvalid(RuntimeError):
    """An intersecting timeline row cannot safely prove content or emptiness."""


@contextmanager
def _publish_fence(conn: sqlite3.Connection, generation: int):
    """Serialize final authorization and publish with every explicit cleanup.

    Provider I/O happens before callers enter this short fence.  Holding both
    stores in the canonical review→capture order keeps raw-retention cleanup
    from crossing the final policy check, Markdown append, and reducer progress
    commit.
    """
    with privacy_egress_lock():
        if fts.content_generation(conn, "reducer") != generation:
            raise ReducerInputChanged("reducer input was invalidated by explicit cleanup")
        yield


@dataclass
class ReduceResult:
    session_id: str
    succeeded: bool  # LLM produced parseable output
    written: bool  # entry landed in event-YYYY-MM-DD.md
    entry_id: str = ""
    path: str = ""
    sub_tasks: list[str] = field(default_factory=list)
    summary: str = ""
    # Session window this reduction covered.
    start_time: datetime | None = None
    end_time: datetime | None = None
    # False for incremental flushes inside an active session, True for the
    # terminal reduction at session end (or the catch-up path). Drives
    # whether the caller should fire the classifier.
    is_final: bool = True


def reduce_session(
    cfg: Config,
    *,
    session_id: str,
    start_time: datetime,
    end_time: datetime,
) -> ReduceResult:
    """Terminal reduce for a session that has already ended.

    Covers only the trailing window since the last flush (or the full
    session if no flush has happened). Opens its own DB connection so
    this is safe to call from a background thread.
    """
    with files_mod.file_lock(_reduction_lock_path(session_id)), fts.cursor() as conn:
        generation = fts.content_generation(conn, "reducer")
        existing = session_store.get_by_id(conn, session_id)
        if existing is None:
            with _publish_fence(conn, generation):
                session_store.insert(
                    conn,
                    session_store.SessionRow(
                        id=session_id,
                        start_time=start_time,
                        end_time=end_time,
                        status="ended",
                    ),
                )
            existing = session_store.get_by_id(conn, session_id)
        flush_end = existing.flush_end if existing and existing.flush_end else None
        with _publish_fence(conn, generation):
            recovered_flush_end = _recover_materialized_flushes(
                conn,
                session_id=session_id,
                session_start=start_time,
                upper_bound=end_time,
            )
            if recovered_flush_end is not None and (
                flush_end is None or _instant(recovered_flush_end) > _instant(flush_end)
            ):
                session_store.set_flush_end(conn, session_id, recovered_flush_end)
                flush_end = recovered_flush_end
        window_start = (
            flush_end
            if flush_end is not None and _instant(flush_end) > _instant(start_time)
            else start_time
        )
        return _reduce_window_locked(
            cfg,
            conn,
            session_id=session_id,
            session_start=start_time,
            session_end=end_time,
            window_start=window_start,
            window_end=end_time,
            is_final=True,
            generation=generation,
        )


def flush_active_session(
    cfg: Config,
    *,
    session_id: str,
    session_start: datetime,
    now: datetime,
) -> ReduceResult | None:
    """Run an incremental reduce on an active session.

    Reduces any closed timeline blocks in ``[flush_end or session_start, now)``
    and appends a partial entry to the event-daily file. Returns ``None``
    if there are no new blocks to reduce yet (common during short
    sessions) or if the LLM call failed (no retry bookkeeping — the next
    flush covers the missed window).
    """
    with files_mod.file_lock(_reduction_lock_path(session_id)), fts.cursor() as conn:
        generation = fts.content_generation(conn, "reducer")
        existing = session_store.get_by_id(conn, session_id)
        if existing is None:
            with _publish_fence(conn, generation):
                session_store.insert(
                    conn,
                    session_store.SessionRow(
                        id=session_id,
                        start_time=session_start,
                        status="active",
                    ),
                )
            existing = session_store.get_by_id(conn, session_id)

        if existing is not None and existing.status in ("reduced", "ended"):
            # Session already closed from under us — nothing to flush.
            return None

        flush_end = existing.flush_end if existing and existing.flush_end else None
        with _publish_fence(conn, generation):
            recovered_flush_end = _recover_materialized_flushes(
                conn,
                session_id=session_id,
                session_start=session_start,
                upper_bound=now,
            )
            if recovered_flush_end is not None and (
                flush_end is None or _instant(recovered_flush_end) > _instant(flush_end)
            ):
                session_store.set_flush_end(conn, session_id, recovered_flush_end)
                flush_end = recovered_flush_end
        window_start = (
            flush_end
            if flush_end is not None and _instant(flush_end) > _instant(session_start)
            else session_start
        )
        if _instant(now) <= _instant(window_start):
            return None

        result = _reduce_window_locked(
            cfg,
            conn,
            session_id=session_id,
            session_start=session_start,
            session_end=None,
            window_start=window_start,
            window_end=now,
            is_final=False,
            generation=generation,
        )
        return result if result.written else None


def _reduction_lock_path(session_id: str) -> Path:
    digest = hashlib.blake2s(session_id.encode("utf-8"), digest_size=2).digest()
    shard = int.from_bytes(digest, "big") % _REDUCTION_LOCK_SHARDS
    return paths.root() / ".reduction-locks" / f"shard-{shard:03d}"


def _reduce_window_locked(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    session_id: str,
    session_start: datetime,
    session_end: datetime | None,
    window_start: datetime,
    window_end: datetime,
    is_final: bool,
    generation: int,
) -> ReduceResult:
    existing = session_store.get_by_id(conn, session_id)
    if existing is None and is_final and session_end is not None:
        with _publish_fence(conn, generation):
            session_store.insert(
                conn,
                session_store.SessionRow(
                    id=session_id,
                    start_time=session_start,
                    end_time=session_end,
                    status="ended",
                ),
            )
        existing = session_store.get_by_id(conn, session_id)

    # Read coverage before blocks. If the producer commits between these two
    # reads we either see its new block (safe bridge below) or retain the older
    # range and defer. Reading blocks first and a newer watermark second could
    # incorrectly certify an empty stale block snapshot.
    processed_range = timeline_store.get_processed_range(conn) if is_final else None
    try:
        blocks = _blocks_for_session(
            conn,
            window_start,
            window_end,
            complete_only=not is_final,
        )
    except TimelineProjectionInvalid as exc:
        # A continuous watermark says the producer inspected this interval; it
        # does not turn a damaged populated row into a proven-empty window.
        # Keep terminal rows pending (and active flush cursors unchanged) until
        # the derived projection is repaired or explicitly cleaned/replayed.
        logger.warning("session %s: timeline projection gap: %s", session_id, exc)
        return ReduceResult(
            session_id=session_id,
            succeeded=False,
            written=False,
            start_time=session_start,
            end_time=session_end,
            is_final=is_final,
        )
    materialized_end = (
        window_end
        if is_final
        else max((block.end_time for block in blocks), key=_instant, default=window_start)
    )
    # A session owns one event-daily file even when an incremental flush
    # crosses local midnight. This keeps downstream delivery keyed to the
    # session rather than to whichever window happened to run last.
    event_daily_name = _event_daily_name(session_start)
    stable_id = _event_entry_id(
        session_id=session_id,
        start_time=window_start,
        end_time=materialized_end,
        is_final=is_final,
    )
    materialized_entry: files_mod.ParsedEntry | None = None
    candidate_names = [
        event_daily_name,
        *(
            name
            for name in _event_daily_names_between(session_start, window_end)
            if name != event_daily_name
        ),
    ]
    with _publish_fence(conn, generation):
        for candidate_name in candidate_names:
            materialized_entry = _repair_existing_event_entry(
                conn,
                name=candidate_name,
                entry_id=stable_id,
            )
            if materialized_entry is not None:
                # Upgrades may find a pre-outbox cross-midnight entry in the
                # old window-start daily file. Keep its authoritative location
                # rather than copying the deterministic ID into the new path.
                event_daily_name = candidate_name
                break
        if materialized_entry is not None:
            already_reduced = existing is not None and existing.status == "reduced"
            if is_final:
                session_store.mark_reduced(
                    conn,
                    session_id,
                    terminal_entry_id=stable_id,
                    terminal_path=event_daily_name,
                )
            else:
                session_store.set_flush_end(conn, session_id, materialized_end)
        else:
            already_reduced = False
    if materialized_entry is not None:
        logger.info(
            "session %s replay recovered materialized entry %s",
            session_id,
            stable_id,
        )
        return ReduceResult(
            session_id=session_id,
            succeeded="heuristic" not in materialized_entry.tags,
            written=not already_reduced,
            entry_id=stable_id,
            path=event_daily_name,
            start_time=session_start,
            end_time=session_end,
            is_final=is_final,
        )

    if existing is not None and existing.status == "reduced":
        logger.info("session %s already reduced, skipping", session_id)
        return ReduceResult(
            session_id=session_id,
            succeeded=True,
            written=False,
            start_time=session_start,
            end_time=session_end,
            is_final=is_final,
        )

    if is_final and not _terminal_timeline_ready(
        blocks=blocks,
        session_end=window_end,
        window_minutes=min(
            int(timeline_store.MAX_BLOCK_DURATION.total_seconds() // 60),
            max(1, int(cfg.timeline.window_minutes)),
        ),
        processed_range=processed_range,
    ):
        # The terminal callback can beat the timeline producer for the bucket
        # containing the final event. Keep the durable row at ended/failed so
        # the pending-reducer tick retries after the producer watermark moves.
        logger.info(
            "session %s: terminal reduce deferred until timeline covers %s",
            session_id,
            window_end.isoformat(),
        )
        return ReduceResult(
            session_id=session_id,
            succeeded=False,
            written=False,
            start_time=session_start,
            end_time=session_end,
            is_final=True,
        )

    if not blocks:
        if is_final:
            logger.info(
                "session %s: terminal reduce has 0 blocks in %s → %s, marking reduced (no-op)",
                session_id,
                window_start.isoformat(),
                window_end.isoformat(),
            )
            with _publish_fence(conn, generation):
                session_store.mark_reduced(
                    conn,
                    session_id,
                    terminal_path=event_daily_name,
                    terminal_noop=True,
                )
        else:
            logger.debug(
                "session %s: flush has 0 new blocks since %s",
                session_id,
                window_start.isoformat(),
            )
        return ReduceResult(
            session_id=session_id,
            succeeded=True,
            written=False,
            start_time=session_start,
            end_time=session_end,
            is_final=is_final,
        )

    if (
        is_final
        and existing is not None
        and existing.status == "failed"
        and existing.next_retry_at is not None
        and _instant(existing.next_retry_at) > _instant(datetime.now().astimezone())
    ):
        logger.info(
            "session %s retry is not due until %s, skipping queued attempt",
            session_id,
            existing.next_retry_at.isoformat(),
        )
        return ReduceResult(
            session_id=session_id,
            succeeded=False,
            written=False,
            start_time=session_start,
            end_time=session_end,
            is_final=True,
        )

    blocks = _policy_allowed_blocks(conn, cfg, blocks)
    if not blocks:
        logger.info(
            "session %s: %s has 0 policy-allowed timeline blocks, skipping model egress",
            session_id,
            "terminal reduce" if is_final else "flush",
        )
        with _publish_fence(conn, generation):
            if is_final:
                session_store.mark_reduced(
                    conn,
                    session_id,
                    terminal_path=event_daily_name,
                    terminal_noop=True,
                )
            else:
                # Consume the closed, policy-excluded interval so every flush
                # does not reconsider the same denied blocks forever.
                session_store.set_flush_end(conn, session_id, materialized_end)
        return ReduceResult(
            session_id=session_id,
            succeeded=True,
            written=False,
            start_time=session_start,
            end_time=session_end,
            is_final=is_final,
        )

    stable_entry_id = _event_entry_id(
        session_id=session_id,
        start_time=window_start,
        end_time=materialized_end,
        is_final=is_final,
    )
    preceding_text, preceding_evidence = _load_preceding_entries(
        conn,
        cfg,
        event_daily_name,
        _PRECEDING_ENTRY_LIMIT,
        exclude_entry_id=stable_entry_id,
    )
    payload = _call_reducer_llm(
        cfg,
        blocks,
        window_start,
        materialized_end,
        conn=conn,
        event_daily_name=event_daily_name,
        preceding_text=preceding_text,
        preceding_evidence=preceding_evidence,
        exclude_entry_id=stable_entry_id,
    )

    if payload is not None:
        sub_tasks = [
            str(task).strip() for task in (payload.get("sub_tasks") or []) if str(task).strip()
        ]
        if not sub_tasks:
            logger.warning("session %s: reducer returned no sub_tasks", session_id)
            payload = None

    if payload is None:
        if not is_final:
            # Flush failures don't schedule retries — the next flush tick
            # naturally covers a bigger window.
            logger.warning(
                "session %s: flush reducer LLM failed at window %s → %s, will retry on next tick",
                session_id,
                window_start.isoformat(),
                window_end.isoformat(),
            )
            return ReduceResult(
                session_id=session_id,
                succeeded=False,
                written=False,
                start_time=session_start,
                end_time=session_end,
                is_final=False,
            )
        retry_count = existing.retry_count if existing else 0
        retry_delay = _RETRY_BACKOFF_MINUTES[min(retry_count, len(_RETRY_BACKOFF_MINUTES) - 1)]
        next_retry_at = datetime.now().astimezone() + timedelta(minutes=retry_delay)
        with _publish_fence(conn, generation):
            session_store.mark_failed(
                conn,
                session_id,
                error="reducer LLM call failed or returned unparseable JSON",
                next_retry_at=next_retry_at,
            )
        logger.warning(
            "session %s: reducer failed (attempt %d), retrying in %d minutes at %s",
            session_id,
            retry_count + 1,
            retry_delay,
            next_retry_at.isoformat(),
        )
        return ReduceResult(
            session_id=session_id,
            succeeded=False,
            written=False,
            start_time=session_start,
            end_time=session_end,
            is_final=True,
        )
    else:
        succeeded = True

    summary = str(payload.get("summary") or "").strip()
    sub_tasks = [_attach_drill_down_breadcrumb(s) for s in sub_tasks]

    with _publish_fence(conn, generation):
        # Retention cleanup may remove raw observations while network I/O is in
        # flight. Re-authorize the exact block and preceding-memory snapshots
        # under the short capture lock before materializing provider output.
        current_blocks = _policy_allowed_blocks(conn, cfg, blocks)
        if _block_bindings(current_blocks) != _block_bindings(blocks):
            raise ReducerInputChanged("reducer timeline input changed before publication")
        current_preceding, current_preceding_evidence = _load_preceding_entries(
            conn,
            cfg,
            event_daily_name,
            _PRECEDING_ENTRY_LIMIT,
            exclude_entry_id=stable_entry_id,
        )
        if current_preceding != preceding_text or current_preceding_evidence != preceding_evidence:
            raise ReducerInputChanged("reducer memory input changed before publication")
        entry_id, path_name, entry_created = _append_event_entry(
            conn,
            event_daily_name=event_daily_name,
            session_id=session_id,
            start_time=window_start,
            end_time=materialized_end,
            summary=summary,
            sub_tasks=sub_tasks,
            heuristic=False,
            is_final=is_final,
            blocks=blocks,
            preceding_evidence=preceding_evidence,
        )

        if is_final:
            # ``flush_end`` is incremental progress, not terminal completion.
            # Keep the append and durable terminal intent under one reset
            # generation fence so explicit cleanup cannot split them.
            session_store.mark_reduced(
                conn,
                session_id,
                terminal_entry_id=entry_id,
                terminal_path=path_name,
            )
        else:
            session_store.set_flush_end(conn, session_id, materialized_end)

    if not entry_created:
        logger.info("session %s replay reused existing entry %s", session_id, entry_id)

    logger.info(
        "session %s %s → %s#%s (%d sub_tasks, window %s-%s, llm_ok=%s)",
        session_id,
        "reduced" if is_final else "flushed",
        path_name,
        entry_id,
        len(sub_tasks),
        window_start.strftime("%H:%M"),
        materialized_end.strftime("%H:%M"),
        succeeded,
    )
    return ReduceResult(
        session_id=session_id,
        succeeded=succeeded,
        written=True,
        entry_id=entry_id,
        path=path_name,
        sub_tasks=sub_tasks,
        summary=summary,
        start_time=session_start,
        end_time=session_end,
        is_final=is_final,
    )


def reduce_session_async(
    cfg: Config,
    *,
    session_id: str,
    start_time: datetime,
    end_time: datetime,
    on_done: callable | None = None,  # type: ignore[valid-type]
) -> threading.Thread:
    """Spawn a daemon thread that reduces the session. Fire-and-forget."""

    def _run() -> None:
        try:
            result = reduce_session(
                cfg,
                session_id=session_id,
                start_time=start_time,
                end_time=end_time,
            )
            if on_done is not None:
                try:
                    on_done(result)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("session %s: on_done callback failed: %s", session_id, exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("session %s: reducer thread crashed: %s", session_id, exc, exc_info=True)

    t = threading.Thread(target=_run, name=f"reduce-{session_id}", daemon=True)
    t.start()
    return t


def retry_due(cfg: Config) -> list[ReduceResult]:
    """Pick up any ``failed`` session rows whose ``next_retry_at`` has elapsed."""
    now = datetime.now().astimezone()
    results: list[ReduceResult] = []
    with fts.cursor() as conn:
        due = session_store.list_due_for_retry(conn, now=now)
    for row in due:
        if row.end_time is None:
            logger.warning("session %s: failed row has no end_time, skipping", row.id)
            continue
        results.append(
            reduce_session(
                cfg,
                session_id=row.id,
                start_time=row.start_time,
                end_time=row.end_time,
            )
        )
    return results


def reduce_all_pending(cfg: Config) -> list[ReduceResult]:
    """Catch up every non-reduced ended session and every due failed session.

    Called from the daily 23:55 safety-net. Covers ``ended`` rows
    whose async reducer thread got killed at shutdown. Failed rows are
    rechecked under their session lock and skipped until ``next_retry_at``;
    this prevents already-queued workers from consuming the retry budget in
    a burst after one worker schedules backoff.
    """
    with fts.cursor() as conn:
        rows = session_store.list_pending_reduction(conn)
    out: list[ReduceResult] = []
    for row in rows:
        if row.end_time is None:
            continue
        out.append(
            reduce_session(
                cfg,
                session_id=row.id,
                start_time=row.start_time,
                end_time=row.end_time,
            )
        )
    return out


# ─── Block selection + prompt rendering ─────────────────────────────────────


def _blocks_for_session(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    *,
    complete_only: bool,
) -> list[timeline_store.TimelineBlock]:
    """Return timeline blocks whose window intersects ``[start, end)``.

    Active-session flushes require complete blocks so their durable watermark
    never jumps past materialized evidence. A terminal reduction includes a
    block that straddles the exact session end: session boundaries are event
    timestamps while timeline blocks are wall-clock buckets, so requiring
    ``block.end <= session.end`` would silently lose every short/trailing
    session slice that ends between bucket boundaries.

    ISO strings with different UTC offsets do not sort chronologically, so the
    SQL predicate deliberately selects a broad two-day candidate band and the
    exact comparison happens in Python after normalizing every timestamp to
    UTC. Timeline windows are at most a few minutes; two days safely covers
    the full legal UTC offset range and legacy naive local timestamps.
    """
    rows = conn.execute(
        """
        SELECT id, start_time, end_time FROM timeline_blocks
         WHERE julianday(start_time) > julianday(?) - 2
           AND julianday(start_time) < julianday(?) + 2
           AND julianday(end_time) > julianday(?) - 2
        """,
        (start.isoformat(), end.isoformat(), start.isoformat()),
    ).fetchall()
    blocks: list[timeline_store.TimelineBlock] = []
    for r in rows:
        block_id = r["id"]
        if not isinstance(block_id, str):
            raise TimelineProjectionInvalid("corrupt timeline block identity")
        try:
            raw_start = datetime.fromisoformat(r["start_time"])
            raw_end = datetime.fromisoformat(r["end_time"])
            intersects = _instant(raw_end) > _instant(start) and _instant(raw_start) < _instant(end)
        except (TypeError, ValueError) as exc:
            raise TimelineProjectionInvalid(
                f"timeline block {block_id} has an invalid window"
            ) from exc
        if not intersects:
            continue
        block = timeline_store.get_by_id(conn, block_id)
        if block is None:
            raise TimelineProjectionInvalid(
                f"timeline block {block_id} has stale projection or sources"
            )
        complete = _instant(block.end_time) <= _instant(end)
        if intersects and (complete or not complete_only):
            blocks.append(block)
    blocks.sort(key=lambda block: (_instant(block.start_time), _instant(block.end_time), block.id))
    return blocks


def _terminal_timeline_ready(
    *,
    blocks: list[timeline_store.TimelineBlock],
    session_end: datetime,
    window_minutes: int,
    processed_range: tuple[datetime, datetime] | None,
) -> bool:
    """Prove the bucket containing ``session_end`` is no longer pending."""
    if processed_range is not None:
        # The producer persists its original grid anchor as processed_from and
        # keeps that elapsed grid through DST, rollback, and restart. Deriving
        # readiness from the same anchor avoids both fixed-offset round-trip
        # errors and an unnecessary extra empty window after a transition.
        target = timeline_store.ceil_to_grid(
            session_end,
            processed_range[0],
            window_minutes,
        )
    else:
        # Legacy databases without a lower bound cannot reconstruct the old
        # grid. Re-enter the current IANA zone for the migration block bridge.
        timeline_end = _instant(session_end).astimezone(local_timezone())
        target = timeline_store.ceil_to_window(timeline_end, window_minutes)
    if timeline_store.range_covers(processed_range, target):
        return True

    # Migration/crash bridge: a block that reaches the exact session end is
    # itself durable proof that the relevant bucket materialized, even if an
    # older database has no producer watermark yet or the producer crashed
    # between block insert and watermark advancement.
    return any(
        _instant(block.start_time) < _instant(session_end)
        and _instant(block.end_time) >= _instant(session_end)
        for block in blocks
    )


def _instant(value: datetime) -> datetime:
    """Normalize aware and legacy naive local timestamps for comparison."""
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _policy_allowed_blocks(
    conn: sqlite3.Connection,
    cfg: Config,
    blocks: list[timeline_store.TimelineBlock],
) -> list[timeline_store.TimelineBlock]:
    """Drop whole derived blocks whose raw ancestry is no longer allowed."""
    authorizer = ContextService(conn, cfg)
    allowed: list[timeline_store.TimelineBlock] = []
    for block in blocks:
        ref = EvidenceRef(
            kind="timeline_block",
            id=block.id,
            timestamp=block.start_time.isoformat(),
            content_hash=timeline_block_digest(
                start=block.start_time.isoformat(),
                end=block.end_time.isoformat(),
                entries=block.entries,
                apps=block.apps_used,
            ),
        )
        if authorizer.evidence_allowed(ref):
            allowed.append(block)
    return allowed


def _format_blocks(blocks: list[timeline_store.TimelineBlock]) -> str:
    out: list[str] = []
    for b in blocks:
        header = f"[{b.start_time.strftime('%H:%M')}-{b.end_time.strftime('%H:%M')}]"
        entries = list(b.entries) if b.entries else []
        if not entries:
            out.append(f"{header} (no notable activity)")
            continue
        lines = "\n".join(f"  - {e}" for e in entries)
        out.append(f"{header}\n{lines}")
    return "\n".join(out)


def _format_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


# Pattern that matches the canonical sub_task prefix the reducer prompt asks
# the LLM to emit: "[HH:MM-HH:MM, <app>] …". The trailing app token is
# greedy-matched up to the closing bracket.
_SUBTASK_PREFIX_RE = re.compile(
    r"^\s*\[\s*(\d{2}):(\d{2})\s*[-–—]\s*(\d{2}):(\d{2})\s*,\s*([^\]]+?)\s*\]"
)


def _attach_drill_down_breadcrumb(sub_task: str) -> str:
    """Append a ``read_recent_capture`` breadcrumb to a sub_task line.

    Parses the canonical ``[HH:MM-HH:MM, <app>]`` prefix and appends
    ``— raw: read_recent_capture(at="HH:MM", app_name="<app>")`` using the
    *start* minute of the range. Lines that don't match the prefix are
    returned unchanged (no breadcrumb noise on heuristic / malformed lines).
    Already-breadcrumbed lines are left alone too.
    """
    if "read_recent_capture(" in sub_task:
        return sub_task
    m = _SUBTASK_PREFIX_RE.match(sub_task)
    if not m:
        return sub_task
    start_h, start_m, _end_h, _end_m, app_raw = m.groups()
    app = app_raw.strip().replace('"', "'")
    breadcrumb = f' — raw: read_recent_capture(at="{start_h}:{start_m}", app_name="{app}")'
    return sub_task.rstrip() + breadcrumb


def _load_preceding_entries(
    conn: sqlite3.Connection,
    cfg: Config,
    file_name: str,
    limit: int,
    *,
    exclude_entry_id: str = "",
) -> tuple[str, list[EvidenceRef]]:
    """Return the last ``limit`` entries of ``file_name`` as a single string.

    Used to give the reducer context about what's already been written to
    today's event-daily file — both from earlier sessions and from earlier
    flushes of the current session — so the new summary can align with or
    explicitly supersede them instead of silently duplicating.
    """
    path = files_mod.memory_path(file_name)
    if candidate_store.is_tombstoned(
        conn,
        kind="memory_file",
        artifact_id=file_name,
    ):
        return "(no prior entries today)", []
    if not path.exists():
        return "(no prior entries today)", []
    try:
        parsed = files_mod.read_file(path)
    except Exception:  # noqa: BLE001
        return "(prior entries unavailable)", []
    entries = parsed.entries
    if exclude_entry_id:
        existing_index = next(
            (index for index, entry in enumerate(entries) if entry.id == exclude_entry_id),
            None,
        )
        if existing_index is not None:
            # A deterministic replay must not make the entry depend on itself
            # (or on entries appended after it).
            entries = entries[:existing_index]
    visible = [
        entry
        for entry in entries
        if not candidate_store.is_tombstoned(
            conn,
            kind="memory_entry",
            artifact_id=entry.id,
            path=file_name,
        )
        and tools_mod.memory_entry_allowed(
            conn,
            cfg,
            path=file_name,
            entry=entry,
        )
    ]
    if not visible:
        return "(no prior entries today)", []
    tail = visible[-limit:]
    out: list[str] = []
    evidence: list[EvidenceRef] = []
    for e in tail:
        ref = EvidenceRef(
            kind="memory_entry",
            id=e.id,
            path=file_name,
            timestamp=e.timestamp,
            content_hash=content_digest(e.body),
        )
        evidence.append(ref)
        out.append(f"### [{e.timestamp}] {{id: {e.id}}}")
        body = e.body.strip()
        if body:
            out.append(body)
        out.append("")
    return "\n".join(out).strip(), evidence


def _call_reducer_llm(
    cfg: Config,
    blocks: list[timeline_store.TimelineBlock],
    start_time: datetime,
    end_time: datetime,
    *,
    conn: sqlite3.Connection,
    event_daily_name: str,
    preceding_text: str | None = None,
    preceding_evidence: list[EvidenceRef] | None = None,
    exclude_entry_id: str = "",
) -> dict[str, Any] | None:
    if preceding_text is None:
        preceding_text, _ = _load_preceding_entries(
            conn,
            cfg,
            event_daily_name,
            _PRECEDING_ENTRY_LIMIT,
        )
    with model_egress_lock():
        current_blocks = _policy_allowed_blocks(conn, cfg, blocks)
        if _block_bindings(current_blocks) != _block_bindings(blocks):
            raise ReducerInputChanged("reducer timeline input changed before provider egress")
        current_preceding, current_evidence = _load_preceding_entries(
            conn,
            cfg,
            event_daily_name,
            _PRECEDING_ENTRY_LIMIT,
            exclude_entry_id=exclude_entry_id,
        )
        if preceding_evidence is not None and (
            current_preceding != preceding_text or current_evidence != preceding_evidence
        ):
            raise ReducerInputChanged("reducer memory input changed before provider egress")
        prompt = load_prompt("session_reduce.md").format(
            start_time=_format_time(start_time),
            end_time=_format_time(end_time),
            block_count=len(blocks),
            capture_count=sum(b.capture_count for b in blocks),
            blocks_text=_format_blocks(blocks),
            preceding_text=current_preceding,
            event_daily_name=event_daily_name,
        )
        try:
            resp = llm_mod.call_llm(
                cfg,
                "reducer",
                messages=[{"role": "user", "content": prompt}],
                json_mode=True,
            )
            text = llm_mod.extract_text(resp).strip()
            if not text:
                return None
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            return None
        except json.JSONDecodeError as exc:
            logger.warning("reducer: malformed JSON from LLM: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("reducer: LLM call failed: %s", exc)
            return None


def _block_bindings(
    blocks: list[timeline_store.TimelineBlock],
) -> list[tuple[str, str]]:
    return [
        (
            block.id,
            timeline_block_digest(
                start=block.start_time.isoformat(),
                end=block.end_time.isoformat(),
                entries=block.entries,
                apps=block.apps_used,
            ),
        )
        for block in blocks
    ]


# ─── Entry writing ──────────────────────────────────────────────────────────


def _event_daily_name(start_time: datetime) -> str:
    return f"event-{start_time.strftime('%Y-%m-%d')}.md"


def _event_daily_names_between(start_time: datetime, end_time: datetime) -> list[str]:
    """Return plausible daily files for a session, including offset changes."""
    start_local = capture_filenames.normalize_datetime(start_time)
    end_local = capture_filenames.normalize_datetime(end_time)
    dates = {
        start_local.date(),
        end_local.date(),
        end_local.astimezone(start_local.tzinfo).date(),
        start_local.astimezone(end_local.tzinfo).date(),
    }
    first = min(dates)
    last = max(dates)
    span_days = (last - first).days
    if span_days > 370:
        # A corrupt legacy session must not make recovery loop through years of
        # nonexistent dates. Existing files are the only useful candidates.
        memory_dir = paths.memory_dir()
        if not memory_dir.exists():
            return []
        return sorted(
            path.name for path in memory_dir.glob("event-????-??-??.md") if path.is_file()
        )

    names: list[str] = []
    current: date = first
    while current <= last:
        names.append(f"event-{current.isoformat()}.md")
        current += timedelta(days=1)
    return names


def recover_legacy_terminal_intent(
    conn: sqlite3.Connection,
    session: session_store.SessionRow,
) -> tuple[str, str, bool] | None:
    """Recover the exact terminal reducer entry for a pre-outbox session.

    Older ``reduced`` rows did not persist the final entry ID or path. The
    deterministic reducer ID lets an upgrade bind that evidence without a new
    model call, including the former cross-midnight path convention. ``None``
    means the row cannot yet be proved complete and must remain pending.
    """
    if session.end_time is None:
        return None
    window_start = session.flush_end or session.start_time
    expected_id = _event_entry_id(
        session_id=session.id,
        start_time=window_start,
        end_time=session.end_time,
        is_final=True,
    )
    legacy_path = _event_daily_name(window_start)
    canonical_path = _event_daily_name(session.start_time)
    candidates = [
        legacy_path,
        canonical_path,
        *(
            name
            for name in _event_daily_names_between(session.start_time, session.end_time)
            if name not in (legacy_path, canonical_path)
        ),
    ]
    for name in candidates:
        path = files_mod.memory_path(name)
        if not path.exists():
            continue
        try:
            parsed = files_mod.read_file(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session %s cannot verify legacy terminal file %s: %s",
                session.id,
                name,
                exc,
            )
            return None
        entry = next((item for item in parsed.entries if item.id == expected_id), None)
        if entry is not None:
            if f"sid:{session.id}" not in entry.tags:
                logger.warning(
                    "session %s legacy terminal entry %s has the wrong session tag",
                    session.id,
                    expected_id,
                )
                return None
            return expected_id, name, False

    indexed = conn.execute(
        "SELECT path FROM entries WHERE id=? LIMIT 1",
        (expected_id,),
    ).fetchone()
    if indexed is not None:
        logger.warning(
            "session %s legacy terminal entry %s is indexed but not materialized",
            session.id,
            expected_id,
        )
        return None

    # A terminal reducer with any trailing timeline evidence always wrote an
    # entry (using a heuristic fallback after exhausted model retries). Only a
    # truly empty trailing window is allowed to bind an empty terminal ID.
    if _blocks_for_session(
        conn,
        window_start,
        session.end_time,
        complete_only=False,
    ):
        logger.warning(
            "session %s has terminal evidence but no recoverable reducer entry",
            session.id,
        )
        return None
    return "", canonical_path, True


def _window_end_tag(end_time: datetime) -> str:
    return f"oc-window-end:{capture_filenames.safe_timestamp(end_time.isoformat())}"


def _event_entry_id(
    *,
    session_id: str,
    start_time: datetime,
    end_time: datetime,
    is_final: bool,
) -> str:
    """Stable identity for one materialized reducer window."""
    stable_key = "\x1f".join(
        [
            session_id,
            _instant(start_time).isoformat(),
            _instant(end_time).isoformat(),
            str(int(is_final)),
        ]
    )
    digest = hashlib.blake2s(stable_key.encode("utf-8"), digest_size=12).hexdigest()
    return f"session-{digest}"


def _repair_existing_event_entry(
    conn: sqlite3.Connection,
    *,
    name: str,
    entry_id: str,
) -> files_mod.ParsedEntry | None:
    """Repair the SQLite projection of an already-materialized Markdown entry."""
    path = files_mod.memory_path(name)
    if not path.exists():
        return None
    try:
        parsed = files_mod.read_file(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("session reducer could not inspect %s: %s", name, exc)
        return None
    entry = next((candidate for candidate in parsed.entries if candidate.id == entry_id), None)
    if entry is None:
        return None
    entries_mod.append_entry_once(
        conn,
        name=name,
        content=entry.body,
        tags=entry.tags,
        entry_id=entry.id,
        evidence_refs=entry.evidence_refs,
    )
    return entry


def _recover_materialized_flushes(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    session_start: datetime,
    upper_bound: datetime,
) -> datetime | None:
    """Repair durable flush entries and return their newest covered boundary.

    Markdown is written atomically before its SQLite projection/progress update.
    A process death in that gap therefore leaves enough information to finish
    the commit without calling the LLM again. Invalid or out-of-session tags are
    ignored so a corrupt heading cannot jump the durable watermark forward.
    """
    sid_tag = f"sid:{session_id}"
    latest: datetime | None = None
    for name in _event_daily_names_between(session_start, upper_bound):
        path = files_mod.memory_path(name)
        if not path.exists():
            continue
        try:
            parsed = files_mod.read_file(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("session reducer could not recover %s: %s", name, exc)
            continue
        for entry in parsed.entries:
            if sid_tag not in entry.tags or "flush" not in entry.tags:
                continue
            encoded_end = next(
                (
                    tag.removeprefix("oc-window-end:")
                    for tag in entry.tags
                    if tag.startswith("oc-window-end:")
                ),
                "",
            )
            materialized_end = capture_filenames.parse_capture_stem(encoded_end)
            if materialized_end is None:
                continue
            if not (_instant(session_start) < _instant(materialized_end) <= _instant(upper_bound)):
                logger.warning(
                    "session %s ignored out-of-range flush boundary %s",
                    session_id,
                    materialized_end.isoformat(),
                )
                continue

            # append_entry_once sees the existing Markdown block and only
            # repairs missing FTS/files rows. If that repair fails, propagate
            # the exception and leave the DB watermark unchanged (fail closed).
            entries_mod.append_entry_once(
                conn,
                name=name,
                content=entry.body,
                tags=entry.tags,
                entry_id=entry.id,
                evidence_refs=entry.evidence_refs,
            )
            if latest is None or _instant(materialized_end) > _instant(latest):
                latest = materialized_end
    return latest


def _ensure_event_daily_file(conn: sqlite3.Connection, name: str, *, day: str) -> None:
    path = files_mod.memory_path(name)
    if path.exists():
        return
    # Another session may create today's shared file while this reducer waits
    # on the cross-process path lock.
    with suppress(FileExistsError):
        entries_mod.create_file(
            conn,
            name=name,
            description=(
                f"Session-level activity log for {day} — one entry per reduced work "
                "session, each carrying a time-ranged sub-task list produced by the "
                "S2 reducer."
            ),
            tags=["event", "session", "daily"],
        )


def _append_event_entry(
    conn: sqlite3.Connection,
    *,
    event_daily_name: str,
    session_id: str,
    start_time: datetime,
    end_time: datetime,
    summary: str,
    sub_tasks: list[str],
    heuristic: bool,
    is_final: bool,
    blocks: list[timeline_store.TimelineBlock],
    preceding_evidence: list[EvidenceRef] | None = None,
) -> tuple[str, str, bool]:
    name = event_daily_name
    day = name.removeprefix("event-").removesuffix(".md")
    _ensure_event_daily_file(conn, name, day=day)

    marker = "" if is_final else " [flush]"
    header = (
        f"**Session {session_id}{marker}** "
        f"({start_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')})"
    )
    body_parts = [header]
    if summary:
        body_parts.append("")
        body_parts.append(summary)
    body_parts.append("")
    body_parts.extend(f"- {s}" for s in sub_tasks)
    body = "\n".join(body_parts)

    tags = ["session", f"sid:{session_id}", _window_end_tag(end_time)]
    if not is_final:
        tags.append("flush")
    if heuristic:
        tags.append("heuristic")

    stable_id = _event_entry_id(
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        is_final=is_final,
    )
    # Session rows are mutable orchestration state, not immutable content.
    # Timeline blocks and preceding entries fully ground the reducer output;
    # do not attach a pseudo hash that cannot be recomputed after status/window
    # progress changes.
    evidence_refs = list(
        EvidenceRef(
            kind="timeline_block",
            id=block.id,
            timestamp=block.start_time.isoformat(),
            content_hash=timeline_block_digest(
                start=block.start_time.isoformat(),
                end=block.end_time.isoformat(),
                entries=block.entries,
                apps=block.apps_used,
            ),
        )
        for block in blocks
    )
    evidence_refs.extend(preceding_evidence or [])
    entry_id, created = entries_mod.append_entry_once(
        conn,
        name=name,
        content=body,
        tags=tags,
        entry_id=stable_id,
        evidence_refs=evidence_refs,
    )
    return entry_id, name, created
