"""Authorize activity-event recall rows against canonical Markdown."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from ..activity import store as activity_store
from ..config import Config
from ..memory_candidates import store as candidate_store
from ..provenance.models import content_digest
from ..store import files as files_store
from .context import ContextService


@dataclass(frozen=True, slots=True)
class CanonicalActivityEvent:
    id: str
    source_path: str
    source_entry_id: str
    source_entry_timestamp: str
    source_content_hash: str
    session_id: str
    ordinal: int
    start_time: str
    end_time: str
    app_name: str
    content: str
    summary: str
    previous_event_id: str | None
    next_event_id: str | None
    rank: float | None = None
    query_mode: str | None = None


def canonical_activity_events_locked(
    conn: sqlite3.Connection,
    cfg: Config,
    events: list[activity_store.ActivityEvent | activity_store.ActivityEventHit],
) -> list[CanonicalActivityEvent]:
    """Return only rows that exactly match an authorized source entry.

    The caller holds the global Markdown/store lock. SQLite remains recall
    only; source body, parsed event fields, purge state, and policy are checked
    against the canonical event-daily file before anything is returned.
    """
    context = ContextService(conn, cfg)
    parsed_by_path: dict[str, files_store.ParsedFile | None] = {}
    parsed_events: dict[tuple[str, str], dict[str, activity_store.ActivityEvent]] = {}
    visible: list[CanonicalActivityEvent] = []
    emitted: set[str] = set()
    for event in events:
        if event.id in emitted or not _safe_event(event):
            continue
        if candidate_store.is_tombstoned(
            conn, kind="memory_file", artifact_id=event.source_path
        ) or candidate_store.is_tombstoned(
            conn,
            kind="memory_entry",
            artifact_id=event.source_entry_id,
            path=event.source_path,
        ):
            continue
        if event.source_path not in parsed_by_path:
            try:
                parsed_by_path[event.source_path] = files_store.read_file(
                    files_store.memory_path(event.source_path)
                )
            except (FileNotFoundError, OSError, TypeError, ValueError):
                parsed_by_path[event.source_path] = None
        parsed = parsed_by_path[event.source_path]
        if parsed is None:
            continue
        entry = next(
            (candidate for candidate in parsed.entries if candidate.id == event.source_entry_id),
            None,
        )
        if (
            entry is None
            or entry.timestamp != event.source_entry_timestamp
            or content_digest(entry.body) != event.source_content_hash
            or not context.memory_entry_allowed(path=event.source_path, entry=entry)
        ):
            continue
        source_key = (event.source_path, event.source_entry_id)
        if source_key not in parsed_events:
            parsed_events[source_key] = {
                item.id: item
                for item in activity_store.parse_entry(
                    source_path=event.source_path,
                    entry=entry,
                )
            }
        canonical = parsed_events[source_key].get(event.id)
        if canonical is None or not _projection_matches(event, canonical):
            continue
        visible.append(
            CanonicalActivityEvent(
                id=canonical.id,
                source_path=canonical.source_path,
                source_entry_id=canonical.source_entry_id,
                source_entry_timestamp=canonical.source_entry_timestamp,
                source_content_hash=canonical.source_content_hash,
                session_id=canonical.session_id,
                ordinal=canonical.ordinal,
                start_time=canonical.start_time,
                end_time=canonical.end_time,
                app_name=canonical.app_name,
                content=canonical.content,
                summary=canonical.summary,
                previous_event_id=event.previous_event_id,
                next_event_id=event.next_event_id,
                rank=float(event.rank) if isinstance(event, activity_store.ActivityEventHit) else None,
                query_mode=(
                    event.query_mode if isinstance(event, activity_store.ActivityEventHit) else None
                ),
            )
        )
        emitted.add(event.id)
    return visible


def _safe_event(event: activity_store.ActivityEvent | activity_store.ActivityEventHit) -> bool:
    values = (
        event.id,
        event.source_path,
        event.source_entry_id,
        event.source_entry_timestamp,
        event.source_content_hash,
        event.session_id,
        event.start_time,
        event.end_time,
        event.app_name,
        event.content,
        event.summary,
    )
    if not all(isinstance(value, str) for value in values):
        return False
    if type(event.ordinal) is not int or event.ordinal < 0:
        return False
    if isinstance(event, activity_store.ActivityEventHit):
        return (
            isinstance(event.rank, (int, float))
            and not isinstance(event.rank, bool)
            and math.isfinite(float(event.rank))
            and event.query_mode in {"strict_and", "relaxed_or_after_zero_hits"}
        )
    return True


def _projection_matches(
    projected: activity_store.ActivityEvent | activity_store.ActivityEventHit,
    canonical: activity_store.ActivityEvent,
) -> bool:
    fields = (
        "id",
        "source_path",
        "source_entry_id",
        "source_entry_timestamp",
        "source_content_hash",
        "session_id",
        "ordinal",
        "start_time",
        "end_time",
        "app_name",
        "content",
        "summary",
    )
    return all(getattr(projected, field) == getattr(canonical, field) for field in fields)
