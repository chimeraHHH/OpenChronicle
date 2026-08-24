"""Build one TimelineBlock from a short (default 1-minute) window of captures.

Reads capture-buffer JSON files whose ``timestamp`` falls inside the
window, renders them into a prompt, and asks the LLM to produce a
small list of self-contained ``[App] …`` lines. Idempotent: skips
windows that already have a block.

The prompt reads the structured S1 fields (``focused_element``,
``visible_text``, ``url``) written by ``capture/s1_parser.py`` rather
than re-rendering the raw AX tree. Pre-v2 captures without a bounded
``visible_text`` projection contribute metadata only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from .. import paths
from ..capture import filenames, store_lock
from ..config import Config
from ..logger import get
from ..memory_candidates import store as candidate_store
from ..privacy import policy as privacy_policy
from ..privacy.egress import model_egress_lock
from ..prompts import load as load_prompt
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, observation_digest
from ..store import fts
from ..testing import failpoints
from ..writer import llm as llm_mod
from . import store

logger = get("openchronicle.timeline")

# Per-capture slice that goes into the timeline prompt. S1 parser
# already caps visible_text at 10k; the timeline prompt is now a
# verbatim-preserving normalizer, so we want to keep as much as the
# context budget allows. 1-min windows rarely carry more than ~6
# captures in practice.
_PER_CAPTURE_TEXT_LIMIT = 4000
# Defensive ceiling: if something goes haywire and a 1-min window has
# 30+ captures, keep the newest ones. Later events are more recent and
# tend to be more informative.
_MAX_EVENTS_PER_WINDOW = 30
# ``load_capture_snapshot`` drops screenshot bytes before handing its bounded
# snapshot to the producer.  Remember their former presence so an old or
# malformed screenshot-bearing record cannot masquerade as the exact
# content-free schema required after URL policy becomes active.
_DROPPED_SCREENSHOT_MARKER = "__openchronicle_dropped_screenshot"
_SOURCE_DIGEST_MARKER = "__openchronicle_source_digest"


class TimelineInputChanged(RuntimeError):
    """A cleanup or source change invalidated an in-flight timeline result."""


class TimelineGenerationFailed(RuntimeError):
    """A populated timeline window could not be normalized by its model."""


def _capture_stem_in_window(stem: str, start: datetime, end: datetime) -> bool:
    """Parse the filename stem back to a datetime and check window membership."""
    ts = _stem_to_dt(stem)
    if ts is None:
        return False
    return store.as_instant(start) <= store.as_instant(ts) < store.as_instant(end)


def _stem_to_dt(stem: str) -> datetime | None:
    return filenames.parse_capture_stem(stem)


def captures_in_window(start: datetime, end: datetime) -> list[Path]:
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return []
    files: list[Path] = []
    for p in sorted(buf.iterdir()):
        if p.suffix != ".json" or p.is_symlink() or not p.is_file():
            continue
        if _capture_stem_in_window(p.stem, start, end):
            files.append(p)
    return files


def capture_paths_by_window(
    end: datetime,
    window_minutes: int,
) -> dict[datetime, list[Path]]:
    """Snapshot valid retained capture paths before ``end`` by wall window.

    The capture writer assigns its authoritative timestamp while holding the
    same collection lock.  Consequently, a writer that finishes before this
    snapshot is included, while one that starts afterwards receives a current
    timestamp and cannot appear late in an already-closed window.
    """
    grouped_paths: dict[datetime, list[tuple[datetime, Path]]] = {}
    with store_lock.capture_store_lock():
        buf = paths.capture_buffer_dir()
        if not buf.exists():
            return {}
        for path in buf.iterdir():
            if path.suffix != ".json" or path.is_symlink() or not path.is_file():
                continue
            timestamp = filenames.parse_capture_stem(path.stem)
            if timestamp is None or store.as_instant(timestamp) >= store.as_instant(end):
                continue
            window_start = store.floor_to_window(timestamp, window_minutes)
            # ZoneInfo's two fall-back folds compare equal and share a hash
            # when they carry the same tzinfo object. Projection dictionaries
            # therefore use UTC instants as keys so the repeated hour cannot
            # collapse into one bucket.
            window_key = store.as_instant(window_start)
            grouped_paths.setdefault(window_key, []).append((timestamp, path))

        return {
            window_start: [
                path
                for _timestamp, path in sorted(
                    items,
                    key=lambda item: (item[0].timestamp(), item[1].name),
                )
            ]
            for window_start, items in grouped_paths.items()
        }


def load_capture_snapshot(
    capture_windows: dict[datetime, list[Path]],
    *,
    start: datetime,
    end: datetime,
    window_minutes: int,
) -> dict[datetime, list[tuple[Path, dict]]]:
    """Read the bounded slice and bucket it relative to the durable cursor.

    The cursor may not align with the current wall-clock window after a user
    changes ``timeline.window_minutes``. Grouping by actual membership in
    ``[cursor, cursor + step)`` prevents a capture floored under the new size
    from being skipped behind an older, differently aligned watermark.
    """
    step = timedelta(minutes=max(1, int(window_minutes)))
    step_seconds = step.total_seconds()
    selected: dict[datetime, list[tuple[datetime, Path]]] = {}
    for paths_in_window in capture_windows.values():
        for path in paths_in_window:
            timestamp = filenames.parse_capture_stem(path.stem)
            if timestamp is None or not (
                store.as_instant(start) <= store.as_instant(timestamp) < store.as_instant(end)
            ):
                continue
            bucket_index = int(
                (store.as_instant(timestamp) - store.as_instant(start)).total_seconds()
                // step_seconds
            )
            bucket_start = store.add_elapsed(start, step * bucket_index)
            selected.setdefault(store.as_instant(bucket_start), []).append((timestamp, path))

    with store_lock.capture_store_lock():
        return {
            window_start: _load_captures(
                [
                    path
                    for _timestamp, path in sorted(
                        items,
                        key=lambda item: (item[0].timestamp(), item[1].name),
                    )
                ],
                drop_screenshot=True,
            )
            for window_start, items in selected.items()
        }


def capture_binding_snapshot(
    capture_windows: dict[datetime, list[Path]],
) -> dict[str, tuple[str, str, str]]:
    """Snapshot exact receipt bindings for retained paths before DB planning."""
    capture_files = sorted(
        {path for window_paths in capture_windows.values() for path in window_paths},
        key=lambda path: path.name,
    )
    with store_lock.capture_store_lock():
        parsed = _load_captures(capture_files, drop_screenshot=True)
    return {
        capture_path: (observation_id, source_hash, capture_time)
        for capture_path, observation_id, source_hash, capture_time in (
            capture_receipt_bindings(parsed)
        )
    }


def _load_captures(
    capture_files: list[Path],
    *,
    drop_screenshot: bool = False,
) -> list[tuple[Path, dict]]:
    """Parse every capture JSON once. Files that fail to read/parse are dropped."""
    parsed: list[tuple[Path, dict]] = []
    for p in capture_files:
        if p.is_symlink() or not p.is_file():
            continue
        # read_bytes() + json.loads handles BOM/encoding sniffing; read_text()
        # would raise UnicodeDecodeError (a ValueError, not OSError) on a
        # mis-encoded file and crash the aggregator instead of dropping it.
        try:
            data = json.loads(p.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("timeline: failed to load capture %s: %s", p.name, exc)
            continue
        if not isinstance(data, dict):
            logger.warning("timeline: capture %s is not a JSON object", p.name)
            continue
        if _DROPPED_SCREENSHOT_MARKER in data or _SOURCE_DIGEST_MARKER in data:
            logger.warning("timeline: capture %s contains a reserved field", p.name)
            continue
        if drop_screenshot and "screenshot" in data:
            data[_SOURCE_DIGEST_MARKER] = observation_digest(data)
            data.pop("screenshot", None)
            data[_DROPPED_SCREENSHOT_MARKER] = True
        parsed.append((p, data))
    return parsed


def _format_events(parsed: list[tuple[Path, dict]]) -> tuple[str, list[str]]:
    """Render captures for the timeline prompt. Returns (events_text, apps_used).

    Reads the structured S1 fields written by ``capture/s1_parser.py`` —
    ``focused_element``, ``visible_text``, ``url`` — and lays them out in
    the one-line-per-capture format matching Einsia's S1 prompt rendering.
    Pre-v2 captures without those fields remain metadata-only. Re-rendering a
    historical app-wide AX tree could reintroduce an excluded sibling window.
    """
    lines: list[str] = []
    apps: set[str] = set()

    files = parsed[-_MAX_EVENTS_PER_WINDOW:]
    for i, (p, data) in enumerate(files, 1):
        ts_raw = str(data.get("timestamp", p.stem))
        ts = _short_time(ts_raw)

        wm = data.get("window_meta") or {}
        app = str(wm.get("app_name") or "Unknown")
        title = str(wm.get("title") or "")
        bundle = str(wm.get("bundle_id") or "")
        if app:
            apps.add(app)

        trigger = data.get("trigger") or {}
        event_type = str(trigger.get("event_type") or "")

        parts = [f"{i}. [{ts}] {app}"]
        if title:
            parts.append(f"— {title}")
        if bundle:
            parts.append(f"({bundle})")

        url = data.get("url")
        if url:
            privacy = data.get("privacy")
            url_metadata_only = (
                isinstance(privacy, dict) and privacy.get("content_mode") == "url_metadata_only"
            )
            if url_metadata_only:
                parts.append(
                    "(APPROVED ADDRESS-CONTROL VALUE; MAY BE UNCOMMITTED; "
                    f"DO NOT INFER VISIT/READ: {url})"
                )
            else:
                parts.append(f"(URL: {url})")

        fe = data.get("focused_element") or {}
        role = str(fe.get("role") or "")
        if role:
            role_desc = f"[{role}]"
            if fe.get("is_editable"):
                role_desc += " (editing)"
            fe_title = str(fe.get("title") or "")
            if fe_title:
                role_desc += f" title={fe_title[:80]}"
            value_length = int(fe.get("value_length") or 0)
            if value_length:
                role_desc += f" len={value_length}"
            value = str(fe.get("value") or "")
            if value:
                role_desc += f": {value}"
            parts.append(role_desc)

        if event_type:
            parts.append(f"<{event_type}>")

        lines.append(" ".join(parts))

        visible_text = data.get("visible_text")
        visible_text = visible_text.strip() if isinstance(visible_text, str) else ""
        if visible_text:
            if len(visible_text) > _PER_CAPTURE_TEXT_LIMIT:
                visible_text = visible_text[:_PER_CAPTURE_TEXT_LIMIT] + "\n…(truncated)"
            preview = visible_text.replace("\n", " ")
            lines.append(f"| {preview}")

        lines.append("")
    return "\n".join(lines).strip(), sorted(apps)


def _short_time(ts: str) -> str:
    """`2026-04-21T17:07:32+08:00` → `17:07:32`. Best-effort only."""
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return ts[:19]


def _format_window(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _parsed_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _intersects_window(
    left_start: datetime,
    left_end: datetime,
    right_start: datetime,
    right_end: datetime,
) -> bool:
    return store.as_instant(left_start) < store.as_instant(right_end) and store.as_instant(
        left_end
    ) > store.as_instant(right_start)


def _materialized_window_consumers(
    conn,
    *,
    start: datetime,
    end: datetime,
    block_id: str | None,
) -> list[str]:
    """Find durable consumers that cannot be silently replayed or cascaded."""
    consumers: set[str] = set()
    if block_id is not None:
        consumers.update(
            f"{dependent.kind}:{dependent.id}"
            for dependent in provenance_store.direct_dependents(
                conn,
                EvidenceRef(kind="timeline_block", id=block_id),
            )
        )

    session_rows = conn.execute(
        """
        SELECT id, start_time, end_time, status, flush_end, classified_end,
               classifier_terminal_pending, classifier_terminal_entry_id,
               classifier_terminal_path, classifier_terminal_noop
          FROM sessions
         WHERE status='reduced'
            OR flush_end IS NOT NULL
            OR classified_end IS NOT NULL
            OR classifier_terminal_pending<>0
            OR classifier_terminal_entry_id<>''
            OR classifier_terminal_path<>''
            OR classifier_terminal_noop<>0
        """
    ).fetchall()
    for row in session_rows:
        session_id = str(row["id"] or "unknown")
        session_start = _parsed_instant(row["start_time"])
        if session_start is None:
            consumers.add(f"session:{session_id}")
            continue
        if store.as_instant(session_start) >= store.as_instant(end):
            continue

        terminal_intent = bool(
            row["classifier_terminal_pending"]
            or row["classifier_terminal_entry_id"]
            or row["classifier_terminal_path"]
            or row["classifier_terminal_noop"]
        )
        raw_progress_ends = [
            value for value in (row["flush_end"], row["classified_end"]) if value is not None
        ]
        if row["status"] == "reduced" or terminal_intent:
            raw_progress_ends.append(row["end_time"])
        progress_ends = [_parsed_instant(value) for value in raw_progress_ends]
        if not progress_ends or any(value is None for value in progress_ends):
            consumers.add(f"session:{session_id}")
            continue
        progress_end = max(
            (value for value in progress_ends if value is not None),
            key=store.as_instant,
        )
        if store.as_instant(progress_end) <= store.as_instant(session_start) or _intersects_window(
            session_start, progress_end, start, end
        ):
            consumers.add(f"session:{session_id}")

    # A published Daily Wrap may have consumed a timeline block in its input
    # digest even when the model cited no records, leaving no provenance edge.
    for row in conn.execute(
        """
        SELECT wrap_id, revision, window_start_utc, window_end_utc
          FROM daily_wrap_revisions
        """
    ):
        wrap_start = _parsed_instant(row["window_start_utc"])
        wrap_end = _parsed_instant(row["window_end_utc"])
        if (
            wrap_start is not None
            and wrap_end is not None
            and _intersects_window(wrap_start, wrap_end, start, end)
        ):
            consumers.add(f"daily_wrap_revision:{row['wrap_id']}:r{row['revision']}")
    return sorted(consumers)


def _raise_if_window_consumed(
    conn,
    *,
    start: datetime,
    end: datetime,
    block_id: str | None,
) -> None:
    consumers = _materialized_window_consumers(
        conn,
        start=start,
        end=end,
        block_id=block_id,
    )
    if consumers:
        raise TimelineInputChanged(
            "late capture intersects durable downstream progress: " + ", ".join(consumers)
        )


def produce_block_for_window(
    cfg: Config,
    conn,
    *,
    start: datetime,
    end: datetime,
    parsed_captures: list[tuple[Path, dict]] | None = None,
    previously_inspected: bool = False,
    outcome_publisher: Callable[[], None] | None = None,
) -> store.TimelineBlock | None:
    """Build one block. Returns ``None`` if the window is empty or already done."""
    initial_state = store.window_state(conn, start, end)
    if initial_state == "invalid":
        # An invalid projection is a durable coverage gap, never evidence that
        # the inspected window was empty.  The tick catches this typed failure
        # and leaves its watermark before the damaged window.
        raise TimelineInputChanged(
            "quarantined invalid timeline projection occupies "
            f"{start.isoformat()} → {end.isoformat()}"
        )

    if parsed_captures is None:
        # Standalone/debug callers still receive an atomic directory snapshot.
        with store_lock.capture_store_lock():
            capture_files = captures_in_window(start, end)
            parsed = _load_captures(capture_files)
    else:
        # The periodic tick snapshots bytes before opening SQLite, preserving
        # the capture-store -> database lock order used by writers.
        parsed = parsed_captures
        capture_files = [path for path, _data in parsed]
    if not capture_files:
        if initial_state == "current":
            logger.debug(
                "timeline: window %s → %s already has a block",
                start.isoformat(),
                end.isoformat(),
            )
            return None
        logger.info(
            "timeline: window %s → %s has 0 captures, skipping",
            start.isoformat(),
            end.isoformat(),
        )
        return None

    # Capture policy is an egress boundary, not only an ingestion rule.  A
    # retained observation that was allowed when collected may be excluded by
    # the current config before this delayed model call runs.  Filter the
    # authoritative parsed records before counting, prompt rendering, or
    # provenance construction.
    # Explicit cleanup shares the long-lived review fence, so it either wins
    # before prompt authorization or waits for provider egress to finish.
    # Ordinary capture writes remain live: capture-store is held only for the
    # authoritative input snapshot and the final publication recheck below.
    replacement_id: str | None = None
    replacement_sources: list[EvidenceRef] | None = None
    with model_egress_lock():
        generation = fts.content_generation(conn, "timeline")
        with store_lock.capture_store_lock():
            parsed = _currently_authorized_captures(conn, cfg, parsed)
        if not parsed:
            logger.info(
                "timeline: window %s → %s has 0 policy-allowed captures, skipping",
                start.isoformat(),
                end.isoformat(),
            )
            return None

        if initial_state == "current":
            existing = store.get_window(conn, start, end)
            if existing is None:
                raise TimelineInputChanged(
                    "timeline projection changed before late-capture comparison"
                )
            current_sources = provenance_store.direct_sources_checked(
                conn,
                EvidenceRef(kind="timeline_block", id=existing.id),
            )
            expected_sources = _capture_sources(parsed)
            unreceipted = store.unreceipted_capture_paths(
                conn,
                capture_receipt_bindings(parsed),
            )
            if not unreceipted or current_sources == expected_sources:
                # The first receipt-enabled replay on an upgraded database sees
                # old captures as unreceipted.  Equal immutable source sets prove
                # the existing block already contains them, so no model call or
                # replacement is required; the tick will backfill receipts.
                logger.debug(
                    "timeline: window %s → %s already has the current source set",
                    start.isoformat(),
                    end.isoformat(),
                )
                return None
            if current_sources is None or any(
                source not in expected_sources for source in current_sources
            ):
                raise TimelineInputChanged(
                    "timeline replacement snapshot no longer contains every old source"
                )
            _raise_if_window_consumed(
                conn,
                start=start,
                end=end,
                block_id=existing.id,
            )
            replacement_id = existing.id
            replacement_sources = current_sources
        elif previously_inspected:
            # A previously empty inspected window can still have been consumed
            # by a terminal no-op reducer or an uncited Daily Wrap. Without a
            # cascade protocol, materializing late evidence must stall safely.
            _raise_if_window_consumed(
                conn,
                start=start,
                end=end,
                block_id=None,
            )

        # Capture JSON is parsed once and reused for prompt rendering.
        events_text, apps_used = _format_events(parsed)
        capture_count = len(parsed)
        prompt = load_prompt("timeline_block.md").format(
            start_time=_format_window(start),
            end_time=_format_window(end),
            capture_count=capture_count,
            events_text=events_text,
        )

        try:
            resp = llm_mod.call_llm(
                cfg,
                "timeline",
                messages=[{"role": "user", "content": prompt}],
                json_mode=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("timeline: LLM call failed: %s", exc)
            raise TimelineGenerationFailed("timeline model call failed") from exc

        text = llm_mod.extract_text(resp).strip()
        try:
            response_data = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            logger.warning("timeline: malformed JSON from LLM: %s", exc)
            raise TimelineGenerationFailed("timeline model returned invalid JSON") from exc

        raw = response_data.get("entries") if isinstance(response_data, dict) else None
        entries = (
            [entry.strip() for entry in raw if isinstance(entry, str) and entry.strip()]
            if isinstance(raw, list)
            else []
        )
        if not entries:
            raise TimelineGenerationFailed("timeline model returned no usable entries")

    block = store.TimelineBlock(
        start_time=start,
        end_time=end,
        timezone=start.tzname() or "",
        entries=entries,
        apps_used=apps_used,
        capture_count=capture_count,
    )
    with model_egress_lock(), store_lock.capture_store_lock():
        # Revalidate immediately before durable publication. A cleanup that
        # won the lock after provider I/O prevents a derived block from being
        # materialized from newly tombstoned or changed bytes.
        if fts.content_generation(conn, "timeline") != generation:
            raise TimelineInputChanged(
                "timeline content generation changed before block publication"
            )
        publish_parsed = _currently_authorized_captures(conn, cfg, parsed)
        if _capture_bindings(publish_parsed) != _capture_bindings(parsed):
            raise TimelineInputChanged("timeline capture inputs changed before block publication")
        final_state = store.window_state(conn, start, end)
        if final_state == "invalid":
            raise TimelineInputChanged(
                "invalid timeline projection occupied the window before publication"
            )
        failpoints.hit("timeline.block.before_commit")
        conn.execute("SAVEPOINT timeline_block_provenance")
        try:
            # Recheck under the final review/capture fence and SQLite
            # transaction. A reducer can publish in the short gap after model
            # egress; deleting or creating the block without this check would
            # silently strand that newly durable progress.
            if replacement_id is not None or previously_inspected:
                _raise_if_window_consumed(
                    conn,
                    start=start,
                    end=end,
                    block_id=replacement_id,
                )
            if replacement_id is not None:
                if replacement_sources is None or any(
                    source not in _capture_sources(publish_parsed) for source in replacement_sources
                ):
                    raise TimelineInputChanged(
                        "timeline replacement lost an old source before publication"
                    )
                replaced = store.get_window(conn, start, end)
                replaced_sources = (
                    provenance_store.direct_sources_checked(
                        conn,
                        EvidenceRef(kind="timeline_block", id=replaced.id),
                    )
                    if replaced is not None
                    else None
                )
                if (
                    replaced is None
                    or replaced.id != replacement_id
                    or replaced_sources != replacement_sources
                ):
                    raise TimelineInputChanged(
                        "timeline replacement target changed before publication"
                    )
                # Fence reducers that may have snapshotted the old block.  The
                # watermark was already rewound by the producer, while this
                # generation bump protects a worker that began just before it.
                fts.bump_content_generation(conn, "reducer")
                provenance_store.delete_subject(
                    conn,
                    EvidenceRef(kind="timeline_block", id=replacement_id),
                )
                conn.execute("DELETE FROM timeline_blocks WHERE id=?", (replacement_id,))
            block, created = store.insert_or_get(conn, block)
            if created:
                _record_block_sources(conn, block, parsed, inside_savepoint=True)
            elif provenance_store.direct_sources_checked(
                conn,
                EvidenceRef(kind="timeline_block", id=block.id),
            ) != _capture_sources(parsed):
                raise TimelineInputChanged(
                    "competing timeline block omitted the inspected capture snapshot"
                )
            published = store.get_by_id(conn, block.id)
            if published is None:
                raise TimelineInputChanged(
                    "timeline block source binding changed before publication"
                )
            block = published
            if outcome_publisher is not None:
                # The periodic producer injects its final full-window receipt
                # publisher here. It runs inside this same capture fence and
                # SAVEPOINT, so an extra late path rolls back the block and
                # provenance rather than exposing a partial source set.
                outcome_publisher()
            conn.execute("RELEASE SAVEPOINT timeline_block_provenance")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK TO SAVEPOINT timeline_block_provenance")
                conn.execute("RELEASE SAVEPOINT timeline_block_provenance")
            raise
        failpoints.hit("timeline.block.after_commit")
    logger.info(
        "timeline: stored block %s — %s → %s (%d entries, %d captures, apps=%s)",
        block.id,
        start.isoformat(),
        end.isoformat(),
        len(entries),
        capture_count,
        ", ".join(apps_used),
    )
    return block


def window_receipt_for_snapshot(
    cfg: Config,
    conn,
    *,
    start: datetime,
    end: datetime,
    parsed_captures: list[tuple[Path, dict]],
) -> tuple[store.WindowReceipt | None, list[tuple[Path, dict]]]:
    """Re-read the complete physical window and build its typed outcome.

    The caller holds the capture-store lock. Any path/content/tombstone or
    membership change from the producer snapshot is an input race, never a
    policy exclusion or an empty-window proof.
    """
    buffer_dir = paths.capture_buffer_dir()
    physical: list[Path] = []
    if buffer_dir.exists():
        for path in sorted(buffer_dir.iterdir(), key=lambda candidate: candidate.name):
            if path.suffix != ".json":
                continue
            timestamp = filenames.parse_capture_stem(path.stem)
            if timestamp is not None and (
                store.as_instant(start) <= store.as_instant(timestamp) < store.as_instant(end)
            ):
                physical.append(path)
    current = _load_captures(physical, drop_screenshot=True)
    expected_bindings = sorted(capture_receipt_bindings(parsed_captures))
    current_bindings = sorted(capture_receipt_bindings(current))
    if [path for path, _data in current] != physical or current_bindings != expected_bindings:
        raise TimelineInputChanged("timeline physical capture manifest changed")

    allowed: list[tuple[Path, dict]] = []
    for path, snapshot in current:
        if candidate_store.is_tombstoned(
            conn,
            kind="capture_file",
            artifact_id=path.name,
        ):
            raise TimelineInputChanged("timeline capture became tombstoned")
        timestamp_raw = snapshot.get("timestamp")
        observation_id = snapshot.get("observation_id")
        timestamp = (
            filenames.parse_timestamp(timestamp_raw) if isinstance(timestamp_raw, str) else None
        )
        stem_timestamp = filenames.parse_capture_stem(path.stem)
        if (
            timestamp is None
            or stem_timestamp is None
            or store.as_instant(timestamp) != store.as_instant(stem_timestamp)
            or not isinstance(observation_id, str)
            or not observation_id.startswith("obs_")
        ):
            raise TimelineInputChanged("timeline capture identity is malformed")
        try:
            canonical_stem = filenames.capture_stem(timestamp_raw, observation_id)
            legacy_stem = filenames.safe_timestamp(timestamp_raw)
        except ValueError as exc:
            raise TimelineInputChanged("timeline capture identity is malformed") from exc
        if path.stem not in {canonical_stem, legacy_stem}:
            raise TimelineInputChanged("timeline filename/payload identity mismatch")
        if privacy_policy.evaluate_stored_observation(
            cfg.capture,
            observation=snapshot,
        ).allowed:
            allowed.append((path, snapshot))

    all_bindings = current_bindings
    block = store.get_window(conn, start, end)
    if allowed:
        expected_sources = _capture_sources(allowed)
        if (
            block is None
            or provenance_store.direct_sources_checked(
                conn,
                EvidenceRef(kind="timeline_block", id=block.id),
            )
            != expected_sources
        ):
            raise TimelineInputChanged(
                "authorized capture snapshot has no exact current block outcome"
            )
        return (
            store.make_window_receipt(
                window_start=start,
                window_end=end,
                bindings=all_bindings,
                policy_digest=privacy_policy.stored_observation_policy_digest(cfg.capture),
                outcome="block",
                block=block,
            ),
            current,
        )
    if current:
        if store.window_state(conn, start, end) != "missing":
            raise TimelineInputChanged("policy-excluded snapshot intersects a block")
        return (
            store.make_window_receipt(
                window_start=start,
                window_end=end,
                bindings=all_bindings,
                policy_digest=privacy_policy.stored_observation_policy_digest(cfg.capture),
                outcome="policy_excluded",
            ),
            current,
        )
    if block is not None or store.window_state(conn, start, end) != "missing":
        raise TimelineInputChanged("empty snapshot cannot prove an occupied window")
    return None, current


def _capture_bindings(parsed: list[tuple[Path, dict]]) -> list[tuple[str, str]]:
    return [
        (
            path.name,
            str(data.get(_SOURCE_DIGEST_MARKER) or observation_digest(data)),
        )
        for path, data in parsed
    ]


def capture_receipt_bindings(
    parsed: list[tuple[Path, dict]],
) -> list[tuple[str, str, str, str]]:
    """Return the exact capture snapshot fields persisted by receipt rows."""
    return [
        (
            path.name,
            str(data.get("observation_id") or f"legacy:{path.stem}"),
            str(data.get(_SOURCE_DIGEST_MARKER) or observation_digest(data)),
            str(data.get("timestamp") or ""),
        )
        for path, data in parsed
    ]


def _capture_sources(parsed: list[tuple[Path, dict]]) -> list[EvidenceRef]:
    return [
        EvidenceRef(
            kind="observation",
            id=observation_id,
            path=capture_path,
            timestamp=capture_time,
            content_hash=source_hash,
        )
        for capture_path, observation_id, source_hash, capture_time in capture_receipt_bindings(
            parsed
        )
    ]


def _currently_authorized_captures(
    conn,
    cfg: Config,
    parsed: list[tuple[Path, dict]],
) -> list[tuple[Path, dict]]:
    authorized: list[tuple[Path, dict]] = []
    buffer_dir = paths.capture_buffer_dir()
    for path, snapshot in parsed:
        if (
            path.parent != buffer_dir
            or Path(path.name).name != path.name
            or path.suffix != ".json"
            or path.is_symlink()
            or not path.is_file()
            or candidate_store.is_tombstoned(conn, kind="capture_file", artifact_id=path.name)
        ):
            continue
        try:
            current = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError):
            continue
        expected_digest = str(snapshot.get(_SOURCE_DIGEST_MARKER) or observation_digest(snapshot))
        if (
            not isinstance(current, dict)
            or observation_digest(current) != expected_digest
            or str(current.get("observation_id") or f"legacy:{path.stem}")
            != str(snapshot.get("observation_id") or f"legacy:{path.stem}")
            or not privacy_policy.evaluate_stored_observation(
                cfg.capture, observation=current
            ).allowed
            or candidate_store.is_tombstoned(conn, kind="capture_file", artifact_id=path.name)
        ):
            continue
        authorized.append((path, snapshot))
    return authorized


def _record_block_sources(
    conn,
    block: store.TimelineBlock,
    parsed: list[tuple[Path, dict]],
    *,
    inside_savepoint: bool = False,
) -> None:
    sources = _capture_sources(parsed)
    if inside_savepoint:
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block.id),
            sources=sources,
        )
        return
    conn.execute("SAVEPOINT timeline_block_provenance_repair")
    try:
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block.id),
            sources=sources,
        )
        conn.execute("RELEASE SAVEPOINT timeline_block_provenance_repair")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT timeline_block_provenance_repair")
        conn.execute("RELEASE SAVEPOINT timeline_block_provenance_repair")
        raise
