"""Durable, user-authored cues for resuming one parked task."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from ..provenance.models import EvidenceRef, canonical_digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS resume_cues (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('parked', 'resumed', 'dismissed')),
    task_label TEXT NOT NULL,
    next_step TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    projection_digest TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resume_cues_one_parked
    ON resume_cues(status) WHERE status='parked';
CREATE INDEX IF NOT EXISTS idx_resume_cues_recent
    ON resume_cues(created_at DESC, id DESC);
"""

VALID_STATUSES = {"parked", "resumed", "dismissed"}
TERMINAL_STATUSES = {"resumed", "dismissed"}


@dataclass(frozen=True, slots=True)
class ResumeCue:
    id: str
    status: str
    task_label: str
    next_step: str
    created_at: str
    updated_at: str
    version: int
    projection_digest: str


class ResumeCueConflict(RuntimeError):
    """Raised when the active cue or its expected version changed."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def create(
    conn: sqlite3.Connection,
    *,
    task_label: str,
    next_step: str,
    now: datetime | None = None,
) -> ResumeCue:
    ensure_schema(conn)
    _validate_text(task_label, field="task label", limit=120)
    _validate_text(next_step, field="next step", limit=1_000)
    instant = _aware(now or datetime.now(UTC)).isoformat(timespec="microseconds")
    cue_id = f"rc-{uuid.uuid4().hex}"
    digest = _projection_digest(
        cue_id=cue_id,
        status="parked",
        task_label=task_label,
        next_step=next_step,
        created_at=instant,
        updated_at=instant,
        version=1,
    )
    try:
        conn.execute(
            """
            INSERT INTO resume_cues(
                id, status, task_label, next_step, created_at,
                updated_at, version, projection_digest
            ) VALUES (?, 'parked', ?, ?, ?, ?, 1, ?)
            """,
            (cue_id, task_label, next_step, instant, instant, digest),
        )
    except sqlite3.IntegrityError as exc:
        raise ResumeCueConflict("another task is already parked") from exc
    created = get(conn, cue_id)
    if created is None:
        raise RuntimeError("resume cue insert did not produce a current row")
    return created


def get(conn: sqlite3.Connection, cue_id: str) -> ResumeCue | None:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM resume_cues WHERE id=?", (cue_id,)).fetchone()
    return _to_cue(row) if row is not None else None


def get_parked(conn: sqlite3.Connection) -> ResumeCue | None:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM resume_cues WHERE status='parked' LIMIT 1").fetchone()
    return _to_cue(row) if row is not None else None


def parked_before(conn: sqlite3.Connection, instant: datetime) -> ResumeCue | None:
    cue = get_parked(conn)
    if cue is None:
        return None
    return cue if _aware(datetime.fromisoformat(cue.created_at)) <= _aware(instant) else None


def list_cues(
    conn: sqlite3.Connection,
    *,
    statuses: list[str] | None = None,
    limit: int = 100,
) -> list[ResumeCue]:
    ensure_schema(conn)
    if limit < 1 or limit > 1_000:
        raise ValueError("limit must be in [1, 1000]")
    args: list[object] = []
    where = ""
    if statuses:
        if set(statuses) - VALID_STATUSES:
            raise ValueError("invalid resume cue status")
        where = f"WHERE status IN ({','.join('?' for _ in statuses)})"
        args.extend(statuses)
    args.append(limit)
    rows = conn.execute(
        f"""
        SELECT * FROM resume_cues {where}
         ORDER BY created_at DESC, id DESC
         LIMIT ?
        """,
        tuple(args),
    ).fetchall()
    return [cue for row in rows if (cue := _to_cue(row)) is not None]


def transition(
    conn: sqlite3.Connection,
    *,
    cue_id: str,
    expected_version: int,
    to_status: str,
    now: datetime | None = None,
) -> ResumeCue:
    if to_status not in TERMINAL_STATUSES:
        raise ValueError("unsupported resume cue transition")
    current = get(conn, cue_id)
    if current is None or current.status != "parked" or current.version != expected_version:
        raise ResumeCueConflict("resume cue changed")
    updated_at = _aware(now or datetime.now(UTC)).isoformat(timespec="microseconds")
    if _aware(datetime.fromisoformat(updated_at)) < _aware(
        datetime.fromisoformat(current.created_at)
    ):
        raise ValueError("resume cue update precedes creation")
    next_version = current.version + 1
    digest = _projection_digest(
        cue_id=current.id,
        status=to_status,
        task_label=current.task_label,
        next_step=current.next_step,
        created_at=current.created_at,
        updated_at=updated_at,
        version=next_version,
    )
    result = conn.execute(
        """
        UPDATE resume_cues
           SET status=?, updated_at=?, version=?, projection_digest=?
         WHERE id=? AND status='parked' AND version=?
        """,
        (to_status, updated_at, next_version, digest, current.id, current.version),
    )
    if result.rowcount != 1:
        raise ResumeCueConflict("resume cue changed")
    updated = get(conn, cue_id)
    if updated is None:
        raise ResumeCueConflict("resume cue projection changed")
    return updated


def evidence_ref(cue: ResumeCue) -> EvidenceRef:
    return EvidenceRef(
        kind="resume_cue",
        id=cue.id,
        timestamp=cue.created_at,
        content_hash=cue.projection_digest,
    )


def ref_is_current(conn: sqlite3.Connection, ref: EvidenceRef) -> bool:
    if ref.kind != "resume_cue" or ref.path or not ref.content_hash:
        return False
    cue = get(conn, ref.id)
    return bool(
        cue is not None
        and cue.status == "parked"
        and cue.created_at == ref.timestamp
        and cue.projection_digest == ref.content_hash
    )


def _to_cue(row: sqlite3.Row | tuple) -> ResumeCue | None:
    try:
        cue = ResumeCue(
            id=row["id"],
            status=row["status"],
            task_label=row["task_label"],
            next_step=row["next_step"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=row["version"],
            projection_digest=row["projection_digest"],
        )
        created = _aware(datetime.fromisoformat(cue.created_at))
        updated = _aware(datetime.fromisoformat(cue.updated_at))
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(cue.id, str)
        or not cue.id.startswith("rc-")
        or cue.status not in VALID_STATUSES
        or type(cue.version) is not int
        or cue.version < 1
        or updated < created
    ):
        return None
    try:
        _validate_text(cue.task_label, field="task label", limit=120)
        _validate_text(cue.next_step, field="next step", limit=1_000)
    except ValueError:
        return None
    expected = _projection_digest(
        cue_id=cue.id,
        status=cue.status,
        task_label=cue.task_label,
        next_step=cue.next_step,
        created_at=cue.created_at,
        updated_at=cue.updated_at,
        version=cue.version,
    )
    return cue if cue.projection_digest == expected else None


def _projection_digest(
    *,
    cue_id: str,
    status: str,
    task_label: str,
    next_step: str,
    created_at: str,
    updated_at: str,
    version: int,
) -> str:
    return canonical_digest(
        {
            "schema": "resume-cue-projection-v1",
            "id": cue_id,
            "status": status,
            "task_label": task_label,
            "next_step": next_step,
            "created_at": created_at,
            "updated_at": updated_at,
            "version": version,
        }
    )


def _validate_text(value: str, *, field: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"invalid {field}")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("resume cue time must be timezone-aware")
    return value.astimezone(UTC)
