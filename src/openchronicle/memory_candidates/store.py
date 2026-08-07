"""Durable review inbox for proposed long-term memories."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_candidates (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    proposal_digest TEXT NOT NULL DEFAULT '',
    producer_run_key TEXT NOT NULL DEFAULT '',
    proposal_slot INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT 'append',
    target_path TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL,
    conflict_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    applied_entry_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT,
    review_reason TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_inbox
    ON memory_candidates(status, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_conflict
    ON memory_candidates(target_path, conflict_key, status);

-- Content-free crash-recovery intents. Rebuild paths must skip tombstoned
-- memory entries until the authorized purge finishes.
CREATE TABLE IF NOT EXISTS purge_tombstones (
    kind TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    plan_json TEXT NOT NULL DEFAULT '{}',
    requested_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(kind, artifact_id, path)
);
"""

VALID_STATUSES = {"pending", "conflict", "applying", "accepted", "rejected"}


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    id: str
    idempotency_key: str
    proposal_digest: str
    producer_run_key: str
    proposal_slot: int
    kind: str
    operation: str
    target_path: str
    content: str
    content_hash: str
    tags: list[str]
    confidence: float | None
    conflict_key: str
    status: str
    version: int
    applied_entry_id: str | None
    created_at: str
    updated_at: str
    reviewed_at: str | None
    review_reason: str
    last_error: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "proposal_digest": self.proposal_digest,
            "producer_run_key": self.producer_run_key,
            "proposal_slot": self.proposal_slot,
            "kind": self.kind,
            "operation": self.operation,
            "target_path": self.target_path,
            "content": self.content,
            "content_hash": self.content_hash,
            "tags": self.tags,
            "confidence": self.confidence,
            "conflict_key": self.conflict_key,
            "status": self.status,
            "version": self.version,
            "applied_entry_id": self.applied_entry_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reviewed_at": self.reviewed_at,
            "review_reason": self.review_reason,
            "last_error": self.last_error,
        }


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_candidates)")}
    for name, declaration in (
        ("proposal_digest", "TEXT NOT NULL DEFAULT ''"),
        ("producer_run_key", "TEXT NOT NULL DEFAULT ''"),
        ("proposal_slot", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE memory_candidates ADD COLUMN {name} {declaration}")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_candidates_run_slot
        ON memory_candidates(producer_run_key, proposal_slot)
        WHERE producer_run_key <> ''
        """
    )


def insert(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    idempotency_key: str,
    proposal_digest: str,
    producer_run_key: str,
    proposal_slot: int,
    kind: str,
    operation: str,
    target_path: str,
    content: str,
    content_hash: str,
    tags: list[str],
    confidence: float | None,
    conflict_key: str,
    status: str,
) -> tuple[MemoryCandidate, bool]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid candidate status: {status}")
    now = datetime.now().astimezone().isoformat()
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO memory_candidates(
            id, idempotency_key, proposal_digest, producer_run_key,
            proposal_slot, kind, operation, target_path, content,
            content_hash, tags_json, confidence, conflict_key, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            idempotency_key,
            proposal_digest,
            producer_run_key,
            proposal_slot,
            kind,
            operation,
            target_path,
            content,
            content_hash,
            json.dumps(tags, ensure_ascii=False),
            confidence,
            conflict_key,
            status,
            now,
            now,
        ),
    )
    row = get_by_idempotency_key(conn, idempotency_key)
    if row is None:
        raise RuntimeError("candidate insert did not produce a row")
    return row, conn.total_changes > before


def record_replay_mismatch(conn: sqlite3.Connection, candidate_id: str) -> None:
    conn.execute(
        """
        UPDATE memory_candidates
           SET last_error='replay proposal differed; preserved first durable proposal'
         WHERE id=?
        """,
        (candidate_id,),
    )


def get(conn: sqlite3.Connection, candidate_id: str) -> MemoryCandidate | None:
    row = conn.execute("SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)).fetchone()
    return _to_candidate(row) if row else None


def get_by_idempotency_key(
    conn: sqlite3.Connection, idempotency_key: str
) -> MemoryCandidate | None:
    row = conn.execute(
        "SELECT * FROM memory_candidates WHERE idempotency_key=?", (idempotency_key,)
    ).fetchone()
    return _to_candidate(row) if row else None


def list_candidates(
    conn: sqlite3.Connection,
    *,
    statuses: list[str] | None = None,
    limit: int = 100,
) -> list[MemoryCandidate]:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be in [1, 1000]")
    args: list[object] = []
    where = ""
    if statuses:
        invalid = set(statuses) - VALID_STATUSES
        if invalid:
            raise ValueError(f"invalid candidate statuses: {sorted(invalid)}")
        placeholders = ",".join("?" for _ in statuses)
        where = f"WHERE status IN ({placeholders})"
        args.extend(statuses)
    args.append(limit)
    rows = conn.execute(
        f"SELECT * FROM memory_candidates {where} ORDER BY created_at, id LIMIT ?", args
    ).fetchall()
    return [_to_candidate(row) for row in rows]


def list_review_snapshot(conn: sqlite3.Connection, *, limit: int = 100) -> list[MemoryCandidate]:
    """Prioritize actionable inbox rows, then append newest review history."""
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be in [1, 1000]")
    actionable = conn.execute(
        """
        SELECT * FROM memory_candidates
         WHERE status IN ('pending', 'conflict', 'applying')
         ORDER BY CASE status
                    WHEN 'pending' THEN 0
                    WHEN 'conflict' THEN 1
                    ELSE 2
                  END,
                  created_at, id
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    remaining = limit - len(actionable)
    history = (
        conn.execute(
            """
            SELECT * FROM memory_candidates
             WHERE status IN ('accepted', 'rejected')
             ORDER BY updated_at DESC, id
             LIMIT ?
            """,
            (remaining,),
        ).fetchall()
        if remaining
        else []
    )
    return [_to_candidate(row) for row in [*actionable, *history]]


def active_conflicts(
    conn: sqlite3.Connection,
    *,
    target_path: str,
    conflict_key: str,
    content_hash: str,
) -> list[MemoryCandidate]:
    if not conflict_key:
        return []
    rows = conn.execute(
        """
        SELECT * FROM memory_candidates
         WHERE target_path=? AND conflict_key=? AND content_hash<>?
           AND status IN ('pending', 'conflict', 'applying', 'accepted')
         ORDER BY created_at, id
        """,
        (target_path, conflict_key, content_hash),
    ).fetchall()
    return [_to_candidate(row) for row in rows]


def update_content(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    expected_version: int,
    content: str,
    content_hash: str,
    tags: list[str],
    conflict_key: str,
    status: str,
) -> MemoryCandidate:
    now = datetime.now().astimezone().isoformat()
    result = conn.execute(
        """
        UPDATE memory_candidates
           SET content=?, content_hash=?, tags_json=?, conflict_key=?,
               status=?, version=version+1, updated_at=?, last_error=''
         WHERE id=? AND version=? AND status IN ('pending', 'conflict')
        """,
        (
            content,
            content_hash,
            json.dumps(tags, ensure_ascii=False),
            conflict_key,
            status,
            now,
            candidate_id,
            expected_version,
        ),
    )
    if result.rowcount != 1:
        raise CandidateConflict("candidate changed or is no longer editable")
    row = get(conn, candidate_id)
    assert row is not None
    return row


def transition(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    expected_version: int,
    from_statuses: tuple[str, ...],
    to_status: str,
    applied_entry_id: str | None = None,
    reason: str = "",
    error: str = "",
) -> MemoryCandidate:
    if to_status not in VALID_STATUSES:
        raise ValueError(f"invalid candidate status: {to_status}")
    placeholders = ",".join("?" for _ in from_statuses)
    now = datetime.now().astimezone().isoformat()
    reviewed_at = now if to_status in {"accepted", "rejected"} else None
    args: list[object] = [
        to_status,
        applied_entry_id,
        now,
        reviewed_at,
        reason,
        error,
        candidate_id,
        expected_version,
        *from_statuses,
    ]
    result = conn.execute(
        f"""
        UPDATE memory_candidates
           SET status=?, applied_entry_id=COALESCE(?, applied_entry_id),
               version=version+1, updated_at=?, reviewed_at=COALESCE(?, reviewed_at),
               review_reason=?, last_error=?
         WHERE id=? AND version=? AND status IN ({placeholders})
        """,
        args,
    )
    if result.rowcount != 1:
        raise CandidateConflict("candidate changed or cannot make that transition")
    row = get(conn, candidate_id)
    assert row is not None
    return row


def set_last_error(conn: sqlite3.Connection, candidate_id: str, error: str) -> None:
    conn.execute(
        """
        UPDATE memory_candidates
           SET last_error=?, updated_at=?
         WHERE id=?
        """,
        (error[:1000], datetime.now().astimezone().isoformat(), candidate_id),
    )


def delete(conn: sqlite3.Connection, candidate_id: str) -> None:
    conn.execute("DELETE FROM memory_candidates WHERE id=?", (candidate_id,))


class CandidateConflict(RuntimeError):
    """Optimistic concurrency or lifecycle conflict."""


@dataclass(frozen=True, slots=True)
class PurgeTombstone:
    kind: str
    artifact_id: str
    path: str
    plan: dict[str, object]
    requested_at: str
    last_error: str


def put_tombstone(
    conn: sqlite3.Connection,
    *,
    kind: str,
    artifact_id: str,
    path: str = "",
    plan: dict[str, object] | None = None,
) -> None:
    now = datetime.now().astimezone().isoformat()
    conn.execute(
        """
        INSERT INTO purge_tombstones(
            kind, artifact_id, path, plan_json, requested_at, last_error
        ) VALUES (?, ?, ?, ?, ?, '')
        ON CONFLICT(kind, artifact_id, path) DO UPDATE SET
            plan_json=excluded.plan_json
        """,
        (
            kind,
            artifact_id,
            path,
            json.dumps(plan or {}, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )


def list_tombstones(conn: sqlite3.Connection, *, kind: str | None = None) -> list[PurgeTombstone]:
    if kind is None:
        rows = conn.execute(
            "SELECT * FROM purge_tombstones ORDER BY requested_at, kind, path, artifact_id"
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM purge_tombstones
             WHERE kind=? ORDER BY requested_at, path, artifact_id
            """,
            (kind,),
        ).fetchall()
    result: list[PurgeTombstone] = []
    for row in rows:
        try:
            plan = json.loads(row["plan_json"] or "{}")
        except json.JSONDecodeError:
            plan = {}
        result.append(
            PurgeTombstone(
                kind=row["kind"],
                artifact_id=row["artifact_id"],
                path=row["path"],
                plan=plan if isinstance(plan, dict) else {},
                requested_at=row["requested_at"],
                last_error=row["last_error"] or "",
            )
        )
    return result


def delete_tombstone(
    conn: sqlite3.Connection, *, kind: str, artifact_id: str, path: str = ""
) -> None:
    conn.execute(
        "DELETE FROM purge_tombstones WHERE kind=? AND artifact_id=? AND path=?",
        (kind, artifact_id, path),
    )


def set_tombstone_error(
    conn: sqlite3.Connection,
    *,
    kind: str,
    artifact_id: str,
    path: str,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE purge_tombstones SET last_error=?
         WHERE kind=? AND artifact_id=? AND path=?
        """,
        (error[:1000], kind, artifact_id, path),
    )


def is_tombstoned(conn: sqlite3.Connection, *, kind: str, artifact_id: str, path: str = "") -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM purge_tombstones
         WHERE kind=? AND artifact_id=? AND path=? LIMIT 1
        """,
        (kind, artifact_id, path),
    ).fetchone()
    return row is not None


def _to_candidate(row: sqlite3.Row) -> MemoryCandidate:
    try:
        tags = json.loads(row["tags_json"] or "[]")
    except json.JSONDecodeError:
        tags = []
    return MemoryCandidate(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        proposal_digest=row["proposal_digest"] or "",
        producer_run_key=row["producer_run_key"] or "",
        proposal_slot=int(row["proposal_slot"] or 0),
        kind=row["kind"],
        operation=row["operation"],
        target_path=row["target_path"],
        content=row["content"],
        content_hash=row["content_hash"],
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        conflict_key=row["conflict_key"] or "",
        status=row["status"],
        version=int(row["version"]),
        applied_entry_id=row["applied_entry_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        reviewed_at=row["reviewed_at"],
        review_reason=row["review_reason"] or "",
        last_error=row["last_error"] or "",
    )
