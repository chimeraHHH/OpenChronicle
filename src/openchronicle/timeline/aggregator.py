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


def _capture_stem_in_window(stem: str, start: datetime, end: datetime) -> bool:
    """Parse the filename stem back to a datetime and check window membership."""
    ts = _stem_to_dt(stem)
    if ts is None:
        return False
    return start <= ts < end


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
            if timestamp is None or timestamp >= end:
                continue
            window_start = store.floor_to_window(timestamp, window_minutes)
            grouped_paths.setdefault(window_start, []).append((timestamp, path))

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
            if timestamp is None or not (start <= timestamp < end):
                continue
            bucket_index = int((timestamp - start).total_seconds() // step_seconds)
            bucket_start = start + step * bucket_index
            selected.setdefault(bucket_start, []).append((timestamp, path))

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


def _load_captures(
    capture_files: list[Path],
    *,
    drop_screenshot: bool = False,
) -> list[tuple[Path, dict]]:
    """Parse every capture JSON once. Files that fail to read/parse are dropped.

    The window is small (≤30 files) so the entire parsed list stays cheap to
    pass around; the win is avoiding a second ``json.loads`` per file when
    ``_heuristic_entries`` runs after the LLM returns no usable output.
    """
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


def produce_block_for_window(
    cfg: Config,
    conn,
    *,
    start: datetime,
    end: datetime,
    parsed_captures: list[tuple[Path, dict]] | None = None,
) -> store.TimelineBlock | None:
    """Build one block. Returns ``None`` if the window is empty or already done."""
    initial_state = store.window_state(conn, start, end)
    if initial_state == "current":
        logger.debug(
            "timeline: window %s → %s already has a block", start.isoformat(), end.isoformat()
        )
        return None
    if initial_state == "invalid":
        # The unique window is already occupied by a projection whose row or
        # immutable source binding no longer verifies. Do not repeatedly send
        # the same captures to the model only to collide at INSERT time.
        logger.warning(
            "timeline: quarantined invalid block occupies window %s → %s; skipping",
            start.isoformat(),
            end.isoformat(),
        )
        return None

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
        logger.info(
            "timeline: window %s → %s has 0 captures, skipping",
            start.isoformat(),
            end.isoformat(),
        )
        return None

    # Capture policy is an egress boundary, not only an ingestion rule.  A
    # retained observation that was allowed when collected may be excluded by
    # the current config before this delayed model call runs.  Filter the
    # authoritative parsed records before counting, prompt rendering,
    # heuristic fallback, or provenance construction.
    # Explicit cleanup shares the long-lived review fence, so it either wins
    # before prompt authorization or waits for provider egress to finish.
    # Ordinary capture writes remain live: capture-store is held only for the
    # authoritative input snapshot and the final publication recheck below.
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

        # Capture JSON is parsed once; reused for prompt rendering AND the
        # heuristic fallback so an LLM miss doesn't trigger a second read.
        events_text, apps_used = _format_events(parsed)
        capture_count = len(parsed)
        prompt = load_prompt("timeline_block.md").format(
            start_time=_format_window(start),
            end_time=_format_window(end),
            capture_count=capture_count,
            events_text=events_text,
        )

        entries: list[str] = []
        try:
            resp = llm_mod.call_llm(
                cfg,
                "timeline",
                messages=[{"role": "user", "content": prompt}],
                json_mode=True,
            )
            text = llm_mod.extract_text(resp).strip()
            response_data = json.loads(text) if text else {}
            raw = response_data.get("entries") if isinstance(response_data, dict) else None
            if isinstance(raw, list):
                entries = [str(e).strip() for e in raw if str(e).strip()]
        except json.JSONDecodeError as exc:
            logger.warning("timeline: malformed JSON from LLM: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("timeline: LLM call failed: %s", exc)

        if not entries:
            entries = _heuristic_entries(parsed)

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
        if store.window_state(conn, start, end) == "invalid":
            raise TimelineInputChanged(
                "invalid timeline projection occupied the window before publication"
            )
        conn.execute("SAVEPOINT timeline_block_provenance")
        try:
            block, created = store.insert_or_get(conn, block)
            if created:
                _record_block_sources(conn, block, parsed, inside_savepoint=True)
            published = store.get_by_id(conn, block.id)
            if published is None:
                raise TimelineInputChanged(
                    "timeline block source binding changed before publication"
                )
            block = published
            conn.execute("RELEASE SAVEPOINT timeline_block_provenance")
        except BaseException:
            conn.execute("ROLLBACK TO SAVEPOINT timeline_block_provenance")
            conn.execute("RELEASE SAVEPOINT timeline_block_provenance")
            raise
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


def _capture_bindings(parsed: list[tuple[Path, dict]]) -> list[tuple[str, str]]:
    return [
        (
            path.name,
            str(data.get(_SOURCE_DIGEST_MARKER) or observation_digest(data)),
        )
        for path, data in parsed
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
    sources: list[EvidenceRef] = []
    for path, data in parsed:
        observation_id = str(data.get("observation_id") or f"legacy:{path.stem}")
        sources.append(
            EvidenceRef(
                kind="observation",
                id=observation_id,
                path=path.name,
                timestamp=str(data.get("timestamp") or ""),
                content_hash=str(data.get(_SOURCE_DIGEST_MARKER) or observation_digest(data)),
            )
        )
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


def _heuristic_entries(parsed: list[tuple[Path, dict]]) -> list[str]:
    """Cheap fallback when the LLM returns no parseable entries."""
    groups: list[tuple[str, str, int]] = []
    for _p, data in parsed:
        wm = data.get("window_meta") or {}
        app = str(wm.get("app_name") or "Unknown")
        title = str(wm.get("title") or "")
        if groups and groups[-1][0] == app and groups[-1][1] == title:
            groups[-1] = (app, title, groups[-1][2] + 1)
        else:
            groups.append((app, title, 1))

    entries: list[str] = []
    for app, title, _count in groups:
        if title:
            entries.append(f"[{app}] worked in window '{title}', involving —")
        else:
            entries.append(f"[{app}] active, involving —")
    return entries
