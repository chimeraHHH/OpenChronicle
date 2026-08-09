"""SQLite projection of source-to-derived provenance edges."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import paths
from .models import (
    EvidenceRef,
    content_digest,
    observation_digest,
    timeline_block_digest,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS provenance_edges (
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_path TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    source_timestamp TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (
        subject_kind, subject_id, subject_path,
        source_kind, source_id, source_path
    )
);
CREATE INDEX IF NOT EXISTS idx_provenance_source
    ON provenance_edges(source_kind, source_id, source_path);
CREATE INDEX IF NOT EXISTS idx_provenance_subject
    ON provenance_edges(subject_kind, subject_id, subject_path);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def record_sources(
    conn: sqlite3.Connection,
    *,
    subject: EvidenceRef,
    sources: list[EvidenceRef],
) -> None:
    """Idempotently record direct sources for one derived object."""
    now = datetime.now().astimezone().isoformat()
    for ordinal, source in enumerate(_unique_sources(sources)):
        conn.execute(
            """
            INSERT INTO provenance_edges(
                subject_kind, subject_id, subject_path,
                source_kind, source_id, source_path,
                source_timestamp, source_hash, ordinal, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                subject_kind, subject_id, subject_path,
                source_kind, source_id, source_path
            ) DO UPDATE SET
                source_timestamp=excluded.source_timestamp,
                source_hash=excluded.source_hash,
                ordinal=excluded.ordinal
            """,
            (
                subject.kind,
                subject.id,
                subject.path,
                source.kind,
                source.id,
                source.path,
                source.timestamp,
                source.content_hash,
                ordinal,
                now,
            ),
        )


def replace_sources(
    conn: sqlite3.Connection,
    *,
    subject: EvidenceRef,
    sources: list[EvidenceRef],
) -> None:
    conn.execute(
        """
        DELETE FROM provenance_edges
         WHERE subject_kind=? AND subject_id=? AND subject_path=?
        """,
        (subject.kind, subject.id, subject.path),
    )
    record_sources(conn, subject=subject, sources=sources)
    if subject.kind == "timeline_block" and not subject.path:
        # Timeline rows are a SQLite projection of model output. Bind that
        # projection to the exact durable edge set when its provenance is
        # first published. Later edge replacement deliberately cannot rewrite
        # the binding: a mismatch quarantines the row instead of silently
        # re-authorizing it through different evidence.
        from ..timeline import store as timeline_store

        timeline_store.bind_sources(
            conn,
            subject.id,
            direct_sources(conn, subject),
        )


def direct_sources(conn: sqlite3.Connection, subject: EvidenceRef) -> list[EvidenceRef]:
    sources = direct_sources_checked(conn, subject)
    return sources if sources is not None else []


def direct_sources_checked(
    conn: sqlite3.Connection,
    subject: EvidenceRef,
) -> list[EvidenceRef] | None:
    """Return a complete source set, distinguishing malformed rows from empty."""
    rows = conn.execute(
        """
        SELECT source_kind, source_id, source_path, source_timestamp, source_hash
          FROM provenance_edges
         WHERE subject_kind=? AND subject_id=? AND subject_path=?
         ORDER BY ordinal, source_kind, source_path, source_id
        """,
        (subject.kind, subject.id, subject.path),
    ).fetchall()
    try:
        return [
            EvidenceRef(
                kind=row["source_kind"],
                id=row["source_id"],
                path=row["source_path"],
                timestamp=row["source_timestamp"],
                content_hash=row["source_hash"],
            )
            for row in rows
        ]
    except (TypeError, ValueError):
        # One malformed edge invalidates the complete projected source set.
        # Callers then quarantine provenance-bearing artifacts instead of
        # crashing a public read or silently trusting the remaining subset.
        return None


def direct_dependents(conn: sqlite3.Connection, source: EvidenceRef) -> list[EvidenceRef]:
    rows = conn.execute(
        """
        SELECT subject_kind, subject_id, subject_path
          FROM provenance_edges
         WHERE source_kind=? AND source_id=? AND source_path=?
         ORDER BY subject_kind, subject_path, subject_id
        """,
        (source.kind, source.id, source.path),
    ).fetchall()
    return [
        EvidenceRef(kind=row["subject_kind"], id=row["subject_id"], path=row["subject_path"])
        for row in rows
    ]


def trace_sources(
    conn: sqlite3.Connection,
    subject: EvidenceRef,
    *,
    max_depth: int = 4,
) -> list[dict[str, object]]:
    """Breadth-first, cycle-safe provenance trace for source drawers."""
    if max_depth < 1 or max_depth > 16:
        raise ValueError("max_depth must be in [1, 16]")
    queue: list[tuple[EvidenceRef, int]] = [(subject, 0)]
    seen = {(subject.kind, subject.path, subject.id)}
    result: list[dict[str, object]] = []
    while queue:
        current, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for source in direct_sources(conn, current):
            key = (source.kind, source.path, source.id)
            if key in seen:
                continue
            seen.add(key)
            result.append({"depth": depth + 1, "source": source.to_dict()})
            queue.append((source, depth + 1))
    return result


def availability(conn: sqlite3.Connection, ref: EvidenceRef) -> str:
    """Best-effort source-drawer availability without inventing replacements."""
    if ref.kind == "observation":
        if (
            ref.path
            and Path(ref.path).name == ref.path
            and (paths.capture_buffer_dir() / ref.path).exists()
        ):
            return "available"
        row = conn.execute(
            "SELECT 1 FROM captures WHERE observation_id=? OR id=? LIMIT 1",
            (ref.id, Path(ref.path).stem if ref.path else ""),
        ).fetchone()
        return "available" if row else "expired"
    if ref.kind == "timeline_block":
        from ..timeline import store as timeline_store

        return "available" if timeline_store.get_by_id(conn, ref.id) else "missing"
    if ref.kind == "session":
        row = conn.execute("SELECT 1 FROM sessions WHERE id=? LIMIT 1", (ref.id,)).fetchone()
        return "available" if row else "missing"
    if ref.kind == "memory_entry":
        from ..memory_candidates import store as candidate_store

        if candidate_store.is_tombstoned(
            conn, kind="memory_entry", artifact_id=ref.id, path=ref.path
        ):
            return "missing"
        row = conn.execute(
            "SELECT 1 FROM entries WHERE id=? AND path=? LIMIT 1",
            (ref.id, ref.path),
        ).fetchone()
        return "available" if row else "missing"
    if ref.kind == "memory_candidate":
        row = conn.execute(
            "SELECT 1 FROM memory_candidates WHERE id=? LIMIT 1", (ref.id,)
        ).fetchone()
        return "available" if row else "missing"
    if ref.kind == "daily_wrap":
        from ..memory_candidates import store as candidate_store

        if candidate_store.is_tombstoned(conn, kind="daily_wrap", artifact_id=ref.id):
            return "missing"
        row = conn.execute("SELECT 1 FROM daily_wrap_jobs WHERE id=? LIMIT 1", (ref.id,)).fetchone()
        return "available" if row else "missing"
    if ref.kind == "daily_wrap_item":
        row = conn.execute(
            """
            SELECT 1 FROM provenance_edges
             WHERE subject_kind='daily_wrap_item'
               AND subject_id=? AND subject_path=? LIMIT 1
            """,
            (ref.id, ref.path),
        ).fetchone()
        return "available" if row else "missing"
    if ref.kind == "daily_wrap_revision":
        row = conn.execute(
            """
            SELECT 1 FROM daily_wrap_revisions
             WHERE wrap_id=? AND revision=? LIMIT 1
            """,
            (ref.path, _revision_number(ref.id)),
        ).fetchone()
        return "available" if row else "missing"
    if ref.kind == "prompt_rescue_input":
        from ..prompt_rescue import store as prompt_rescue_store

        return "available" if prompt_rescue_store.get(conn, ref.id) else "missing"
    if ref.kind == "reply_rescue_input":
        from ..reply_rescue import store as reply_rescue_store

        return "available" if reply_rescue_store.get(conn, ref.id) else "missing"
    if ref.kind == "resume_profile":
        from ..resume_rescue import store as resume_rescue_store

        current = resume_rescue_store.get_current_profile(conn, ref.id)
        return (
            "available" if current is not None and ref.path == str(current.version) else "missing"
        )
    if ref.kind == "resume_opportunity":
        from ..resume_rescue import store as resume_rescue_store

        return "available" if resume_rescue_store.get_opportunity(conn, ref.id) else "missing"
    if ref.kind == "resume_rescue":
        from ..resume_rescue import store as resume_rescue_store

        return "available" if resume_rescue_store.get_projection(conn, ref.id) else "missing"
    return "unknown"


def current_content_hash(conn: sqlite3.Connection, ref: EvidenceRef) -> str | None:
    """Recompute hashes for source kinds that can authorize memory approval."""
    if ref.kind == "observation":
        if not ref.path or Path(ref.path).name != ref.path:
            return None
        try:
            raw = json.loads((paths.capture_buffer_dir() / ref.path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return observation_digest(raw) if isinstance(raw, dict) else None
    if ref.kind == "timeline_block":
        row = conn.execute(
            """
            SELECT start_time, end_time, entries, apps_used
              FROM timeline_blocks WHERE id=? LIMIT 1
            """,
            (ref.id,),
        ).fetchone()
        if row is None:
            return None
        try:
            entries = json.loads(row["entries"] or "[]")
            apps = json.loads(row["apps_used"] or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(entries, list) or not isinstance(apps, list):
            return None
        return timeline_block_digest(
            start=row["start_time"], end=row["end_time"], entries=entries, apps=apps
        )
    if ref.kind == "session":
        row = conn.execute(
            "SELECT start_time, end_time, status FROM sessions WHERE id=? LIMIT 1",
            (ref.id,),
        ).fetchone()
        if row is None:
            return None
        return content_digest(
            json.dumps(
                {
                    "start": row["start_time"],
                    "end": row["end_time"] or "",
                    "status": row["status"],
                },
                sort_keys=True,
            )
        )
    if ref.kind == "memory_entry":
        from ..store import files as files_store

        path = files_store.memory_path(ref.path)
        if not path.exists():
            return None
        parsed = files_store.read_file(path)
        entry = next((item for item in parsed.entries if item.id == ref.id), None)
        if entry is None or not entry.provenance_valid:
            return None
        return content_digest(entry.body)
    if ref.kind == "prompt_rescue_input":
        from ..prompt_rescue import store as prompt_rescue_store

        row = prompt_rescue_store.get(conn, ref.id)
        return row.source_digest if row is not None else None
    if ref.kind == "reply_rescue_input":
        from ..reply_rescue import store as reply_rescue_store

        row = reply_rescue_store.get(conn, ref.id)
        return row.source_digest if row is not None else None
    if ref.kind == "resume_profile":
        from ..resume_rescue import store as resume_rescue_store

        row = resume_rescue_store.get_current_profile(conn, ref.id)
        return row.digest if row is not None and ref.path == str(row.version) else None
    if ref.kind == "resume_opportunity":
        from ..resume_rescue import store as resume_rescue_store

        row = resume_rescue_store.get_opportunity(conn, ref.id)
        return row.digest if row is not None else None
    if ref.kind == "resume_rescue":
        from ..resume_rescue import store as resume_rescue_store

        row = resume_rescue_store.get_projection(conn, ref.id)
        return row.artifact_digest if row is not None else None
    return None


def is_current(conn: sqlite3.Connection, ref: EvidenceRef) -> bool:
    return bool(
        ref.content_hash
        and availability(conn, ref) == "available"
        and current_content_hash(conn, ref) == ref.content_hash
    )


def delete_subject(conn: sqlite3.Connection, subject: EvidenceRef) -> None:
    conn.execute(
        """
        DELETE FROM provenance_edges
         WHERE subject_kind=? AND subject_id=? AND subject_path=?
        """,
        (subject.kind, subject.id, subject.path),
    )


def delete_source_edges(conn: sqlite3.Connection, source: EvidenceRef) -> None:
    conn.execute(
        """
        DELETE FROM provenance_edges
         WHERE source_kind=? AND source_id=? AND source_path=?
        """,
        (source.kind, source.id, source.path),
    )


def _unique_sources(sources: list[EvidenceRef]) -> list[EvidenceRef]:
    seen: set[tuple[str, str, str]] = set()
    result: list[EvidenceRef] = []
    for source in sources:
        key = (source.kind, source.path, source.id)
        if key in seen:
            continue
        seen.add(key)
        result.append(source)
    return result


def _revision_number(value: str) -> int:
    try:
        return int(value.rsplit(":r", 1)[1])
    except (IndexError, ValueError):
        return -1
