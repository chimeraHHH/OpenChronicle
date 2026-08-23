"""Event-level SQLite projection over canonical ``event-YYYY-MM-DD.md`` entries.

The reducer already groups consecutive timeline blocks into topic/app-specific
``sub_tasks``.  This module turns those bullets into independently searchable
events without creating a second authority: every public field can be rebuilt
and verified against the source Markdown entry.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..local_time import local_timezone
from ..provenance.models import content_digest
from ..store import files as files_store
from ..store.fts import _safe_fts_or_query, _safe_fts_query

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_events (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    source_entry_timestamp TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    app_name TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL,
    previous_event_id TEXT,
    next_event_id TEXT,
    UNIQUE(source_path, source_entry_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_activity_events_source
ON activity_events(source_path, source_entry_id);
CREATE INDEX IF NOT EXISTS idx_activity_events_time
ON activity_events(start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_activity_events_session
ON activity_events(session_id, start_time);

CREATE VIRTUAL TABLE IF NOT EXISTS activity_events_fts USING fts5(
    id UNINDEXED,
    app_name,
    content,
    summary,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

_EVENT_PATH_RE = re.compile(r"^event-(?P<day>\d{4}-\d{2}-\d{2})\.md$")
_SESSION_HEADER_RE = re.compile(
    r"^\*\*Session\s+[^*]+\*\*\s*\(\s*(?P<start>\d{2}:\d{2})\s*[\-–]\s*"
    r"(?P<end>\d{2}:\d{2})\s*\)\s*$",
    re.MULTILINE,
)
_SUBTASK_RE = re.compile(
    r"^-\s*\[(?P<start>\d{2}:\d{2})\s*[\-–]\s*(?P<end>\d{2}:\d{2})\s*,\s*"
    r"(?P<app>[^\]\r\n]+?)\s*\]\s*(?P<content>.*)$",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class ActivityEvent:
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
    previous_event_id: str | None = None
    next_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class ActivityEventHit(ActivityEvent):
    rank: float = 0.0
    query_mode: str = "strict_and"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def parse_entry(
    *,
    source_path: str,
    entry: files_store.ParsedEntry,
) -> list[ActivityEvent]:
    """Parse one canonical reducer entry into stable event units.

    Legacy event entries without structured sub-task bullets remain searchable
    as one coarse event. Superseded and heuristic entries intentionally produce
    no projection rows.
    """
    path_match = _EVENT_PATH_RE.fullmatch(source_path)
    if (
        path_match is None
        or entry.superseded_by is not None
        or "heuristic" in entry.tags
        or not entry.provenance_valid
    ):
        return []
    day = date.fromisoformat(path_match.group("day"))
    session_id = next(
        (tag.removeprefix("sid:") for tag in entry.tags if tag.startswith("sid:")),
        "",
    )
    source_hash = content_digest(entry.body)
    matches = list(_SUBTASK_RE.finditer(entry.body))
    summary = _summary_before_first_subtask(entry.body, matches)
    events: list[ActivityEvent] = []
    for ordinal, match in enumerate(matches):
        content_end = matches[ordinal + 1].start() if ordinal + 1 < len(matches) else len(entry.body)
        continuation = entry.body[match.end() : content_end].strip()
        content = match.group("content").strip()
        if continuation:
            content = f"{content}\n{continuation}".strip()
        start, end = _event_window(day, match.group("start"), match.group("end"))
        events.append(
            ActivityEvent(
                id=_event_id(source_path, entry.id, ordinal),
                source_path=source_path,
                source_entry_id=entry.id,
                source_entry_timestamp=entry.timestamp,
                source_content_hash=source_hash,
                session_id=session_id,
                ordinal=ordinal,
                start_time=start,
                end_time=end,
                app_name=match.group("app").strip(),
                content=content,
                summary=summary,
            )
        )
    if events:
        return events

    body = entry.body.strip()
    if not body:
        return []
    header = _SESSION_HEADER_RE.search(body)
    if header is not None:
        start, end = _event_window(day, header.group("start"), header.group("end"))
    else:
        start = _entry_time(entry.timestamp, day).isoformat(timespec="minutes")
        end = start
    return [
        ActivityEvent(
            id=_event_id(source_path, entry.id, 0),
            source_path=source_path,
            source_entry_id=entry.id,
            source_entry_timestamp=entry.timestamp,
            source_content_hash=source_hash,
            session_id=session_id,
            ordinal=0,
            start_time=start,
            end_time=end,
            app_name="",
            content=body,
            summary="",
        )
    ]


def replace_source_entry(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    entry: files_store.ParsedEntry,
    refresh_links: bool = True,
) -> int:
    """Make one entry's event projection exactly match canonical Markdown."""
    delete_source_entry(conn, source_path=source_path, source_entry_id=entry.id, refresh_links=False)
    events = parse_entry(source_path=source_path, entry=entry)
    for event in events:
        _insert_event(conn, event)
    if refresh_links and source_path.startswith("event-"):
        refresh_path_links(conn, source_path)
    return len(events)


def replace_path(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    entries: list[files_store.ParsedEntry],
) -> int:
    """Replace every event row for one canonical event-daily file."""
    ids = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM activity_events WHERE source_path=?",
            (source_path,),
        )
    ]
    if ids:
        conn.executemany("DELETE FROM activity_events_fts WHERE id=?", [(item,) for item in ids])
    conn.execute("DELETE FROM activity_events WHERE source_path=?", (source_path,))
    count = 0
    for entry in entries:
        for event in parse_entry(source_path=source_path, entry=entry):
            _insert_event(conn, event)
            count += 1
    if source_path.startswith("event-"):
        refresh_path_links(conn, source_path)
    return count


def delete_source_entry(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    source_entry_id: str,
    refresh_links: bool = True,
) -> None:
    ids = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM activity_events WHERE source_path=? AND source_entry_id=?",
            (source_path, source_entry_id),
        )
    ]
    if ids:
        conn.executemany("DELETE FROM activity_events_fts WHERE id=?", [(event_id,) for event_id in ids])
    conn.execute(
        "DELETE FROM activity_events WHERE source_path=? AND source_entry_id=?",
        (source_path, source_entry_id),
    )
    if refresh_links and source_path.startswith("event-"):
        refresh_path_links(conn, source_path)


def clear(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM activity_events_fts")
    conn.execute("DELETE FROM activity_events")


def refresh_path_links(conn: sqlite3.Connection, source_path: str) -> None:
    """Materialize previous/next relations for one local activity day."""
    rows = conn.execute(
        """
        SELECT id FROM activity_events
         WHERE source_path=?
         ORDER BY start_time, end_time, source_entry_timestamp, source_entry_id, ordinal, id
        """,
        (source_path,),
    ).fetchall()
    ids = [str(row["id"]) for row in rows]
    for index, event_id in enumerate(ids):
        conn.execute(
            "UPDATE activity_events SET previous_event_id=?, next_event_id=? WHERE id=?",
            (
                ids[index - 1] if index > 0 else None,
                ids[index + 1] if index + 1 < len(ids) else None,
                event_id,
            ),
        )


def search(
    conn: sqlite3.Connection,
    *,
    query: str,
    top_k: int = 10,
    offset: int = 0,
    since: str | None = None,
    until: str | None = None,
) -> list[ActivityEventHit]:
    safe_query = _safe_fts_query(query)
    if safe_query == '\"\"' or top_k <= 0:
        return []
    clauses = ["activity_events_fts MATCH ?"]
    args: list[object] = [safe_query]
    if since is not None:
        clauses.append("e.end_time >= ?")
        args.append(since)
    if until is not None:
        clauses.append("e.start_time <= ?")
        args.append(until)
    sql = """
        SELECT e.*, bm25(activity_events_fts) AS rank
          FROM activity_events_fts
          JOIN activity_events AS e ON e.id=activity_events_fts.id
         WHERE {where}
         ORDER BY rank, e.start_time, e.id
         LIMIT ? OFFSET ?
    """.format(where=" AND ".join(clauses))
    args.extend((top_k, offset))
    rows = conn.execute(sql, args).fetchall()
    if rows or offset > 0:
        return [_row_to_hit(row, query_mode="strict_and") for row in rows]

    # Event units are intentionally narrower than whole sessions. A strict
    # query can therefore split its terms across the matching event and an
    # adjacent event. Relax only after a zero-hit AND query; BM25 still ranks
    # the local OR candidates and canonical authorization remains unchanged.
    relaxed_args = list(args)
    relaxed_args[0] = _safe_fts_or_query(query)
    return [
        _row_to_hit(row, query_mode="relaxed_or_after_zero_hits")
        for row in conn.execute(sql, relaxed_args)
    ]


def get(conn: sqlite3.Connection, event_id: str) -> ActivityEvent | None:
    row = conn.execute("SELECT * FROM activity_events WHERE id=?", (event_id,)).fetchone()
    return _row_to_event(row) if row is not None else None


def neighbors(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    radius: int = 1,
) -> list[tuple[str, int, ActivityEvent]]:
    """Return ordered previous/next events up to ``radius`` hops away."""
    if type(radius) is not int or not 0 <= radius <= 3:
        raise ValueError("radius must be an integer in [0, 3]")
    origin = get(conn, event_id)
    if origin is None or radius == 0:
        return []
    result: list[tuple[str, int, ActivityEvent]] = []
    cursor = origin
    previous: list[tuple[str, int, ActivityEvent]] = []
    for distance in range(1, radius + 1):
        if not cursor.previous_event_id:
            break
        candidate = get(conn, cursor.previous_event_id)
        if candidate is None:
            break
        previous.append(("previous", distance, candidate))
        cursor = candidate
    result.extend(reversed(previous))
    cursor = origin
    for distance in range(1, radius + 1):
        if not cursor.next_event_id:
            break
        candidate = get(conn, cursor.next_event_id)
        if candidate is None:
            break
        result.append(("next", distance, candidate))
        cursor = candidate
    return result


def _insert_event(conn: sqlite3.Connection, event: ActivityEvent) -> None:
    conn.execute(
        """
        INSERT INTO activity_events(
            id, source_path, source_entry_id, source_entry_timestamp,
            source_content_hash, session_id, ordinal, start_time, end_time,
            app_name, content, summary, previous_event_id, next_event_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.id,
            event.source_path,
            event.source_entry_id,
            event.source_entry_timestamp,
            event.source_content_hash,
            event.session_id,
            event.ordinal,
            event.start_time,
            event.end_time,
            event.app_name,
            event.content,
            event.summary,
            event.previous_event_id,
            event.next_event_id,
        ),
    )
    conn.execute(
        "INSERT INTO activity_events_fts(id, app_name, content, summary) VALUES (?, ?, ?, ?)",
        (event.id, event.app_name, event.content, event.summary),
    )


def _summary_before_first_subtask(body: str, matches: list[re.Match[str]]) -> str:
    prefix = body[: matches[0].start()] if matches else body
    lines = prefix.splitlines()
    if lines and _SESSION_HEADER_RE.fullmatch(lines[0].strip()):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _event_window(day: date, start_text: str, end_text: str) -> tuple[str, str]:
    zone = local_timezone()
    start_clock = time.fromisoformat(start_text)
    end_clock = time.fromisoformat(end_text)
    start = datetime.combine(day, start_clock, tzinfo=zone)
    end = datetime.combine(day, end_clock, tzinfo=zone)
    if end < start:
        end += timedelta(days=1)
    return start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")


def _entry_time(value: str, fallback_day: date) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return datetime.combine(fallback_day, time.min, tzinfo=local_timezone())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_timezone())
    return parsed


def _event_id(source_path: str, source_entry_id: str, ordinal: int) -> str:
    stable = f"{source_path}\x1f{source_entry_id}\x1f{ordinal}"
    digest = hashlib.blake2s(stable.encode("utf-8"), digest_size=12).hexdigest()
    return f"activity-{digest}"


def _row_to_event(row: sqlite3.Row) -> ActivityEvent:
    return ActivityEvent(
        id=str(row["id"]),
        source_path=str(row["source_path"]),
        source_entry_id=str(row["source_entry_id"]),
        source_entry_timestamp=str(row["source_entry_timestamp"]),
        source_content_hash=str(row["source_content_hash"]),
        session_id=str(row["session_id"]),
        ordinal=int(row["ordinal"]),
        start_time=str(row["start_time"]),
        end_time=str(row["end_time"]),
        app_name=str(row["app_name"]),
        content=str(row["content"]),
        summary=str(row["summary"]),
        previous_event_id=str(row["previous_event_id"]) if row["previous_event_id"] else None,
        next_event_id=str(row["next_event_id"]) if row["next_event_id"] else None,
    )


def _row_to_hit(row: sqlite3.Row, *, query_mode: str) -> ActivityEventHit:
    event = _row_to_event(row)
    return ActivityEventHit(
        **{field: getattr(event, field) for field in event.__dataclass_fields__},
        rank=float(row["rank"]),
        query_mode=query_mode,
    )
