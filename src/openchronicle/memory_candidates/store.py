"""Durable review inbox for proposed long-term memories."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..provenance.models import EvidenceRef

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_candidates (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    proposal_digest TEXT NOT NULL DEFAULT '',
    projection_digest TEXT NOT NULL DEFAULT '',
    producer_run_key TEXT NOT NULL DEFAULT '',
    proposal_slot INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT 'append',
    target_path TEXT NOT NULL,
    target_entry_id TEXT NOT NULL DEFAULT '',
    target_entry_hash TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    claim_evidence_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL,
    conflict_key TEXT NOT NULL DEFAULT '',
    subject_key TEXT NOT NULL DEFAULT '',
    assertion_kind TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL DEFAULT '',
    valid_to TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS memory_candidate_schema_migrations (
    name TEXT PRIMARY KEY
);

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

_PROJECTION_MIGRATION = "projection-provenance-v1"


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    id: str
    idempotency_key: str
    proposal_digest: str
    projection_digest: str
    producer_run_key: str
    proposal_slot: int
    kind: str
    operation: str
    target_path: str
    target_entry_id: str
    target_entry_hash: str
    content: str
    content_hash: str
    claim_evidence: list[EvidenceRef]
    tags: list[str]
    confidence: float | None
    conflict_key: str
    subject_key: str
    assertion_kind: str
    valid_from: str
    valid_to: str
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
            "target_entry_id": self.target_entry_id,
            "target_entry_hash": self.target_entry_hash,
            "content": self.content,
            "content_hash": self.content_hash,
            "claim_evidence": [ref.to_dict() for ref in self.claim_evidence],
            "tags": self.tags,
            "confidence": self.confidence,
            "conflict_key": self.conflict_key,
            "subject_key": self.subject_key,
            "assertion_kind": self.assertion_kind,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
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
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Keep schema discovery, DDL, the one-time trust backfill, and its
        # completion marker under one writer lock.  The marker is deliberately
        # separate from column existence: a killed older migration may have
        # durable columns but no completed backfill.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_candidates)")}
        for name, declaration in (
            ("proposal_digest", "TEXT NOT NULL DEFAULT ''"),
            ("projection_digest", "TEXT NOT NULL DEFAULT ''"),
            ("producer_run_key", "TEXT NOT NULL DEFAULT ''"),
            ("proposal_slot", "INTEGER NOT NULL DEFAULT 0"),
            ("target_entry_id", "TEXT NOT NULL DEFAULT ''"),
            ("target_entry_hash", "TEXT NOT NULL DEFAULT ''"),
            ("claim_evidence_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("subject_key", "TEXT NOT NULL DEFAULT ''"),
            ("assertion_kind", "TEXT NOT NULL DEFAULT ''"),
            ("valid_from", "TEXT NOT NULL DEFAULT ''"),
            ("valid_to", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE memory_candidates ADD COLUMN {name} {declaration}")

        migration_done = conn.execute(
            "SELECT 1 FROM memory_candidate_schema_migrations WHERE name=?",
            (_PROJECTION_MIGRATION,),
        ).fetchone()
        if migration_done is None:
            _backfill_projection_migration(conn)
            conn.execute(
                "INSERT INTO memory_candidate_schema_migrations(name) VALUES (?)",
                (_PROJECTION_MIGRATION,),
            )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_candidates_run_slot
            ON memory_candidates(producer_run_key, proposal_slot)
            WHERE producer_run_key <> ''
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_candidates_subject
            ON memory_candidates(subject_key, status)
            WHERE subject_key <> ''
            """
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _backfill_projection_migration(conn: sqlite3.Connection) -> None:
    """Bind valid legacy rows once; later startup never repairs blank digests."""
    rows = conn.execute(
        """
        SELECT id, kind, operation, target_path, target_entry_id,
               target_entry_hash, content, content_hash, claim_evidence_json,
               tags_json, confidence, conflict_key, subject_key,
               assertion_kind, valid_from, valid_to
          FROM memory_candidates
         ORDER BY id
        """
    ).fetchall()
    for row in rows:
        values = _legacy_projection_values(row)
        if values is None:
            # A malformed legacy row cannot safely become a trusted
            # projection. Leave both digests blank so all readers quarantine
            # it instead of normalizing attacker-controlled SQLite values.
            continue
        current_projection_digest = projection_digest(**values)
        sources = _legacy_candidate_sources(conn, row["id"])
        current_proposal_digest = (
            proposal_digest(
                kind=values["kind"],
                operation=values["operation"],
                target_path=values["target_path"],
                target_entry_id=values["target_entry_id"],
                target_entry_hash=values["target_entry_hash"],
                content_hash=values["content_hash"],
                tags=values["tags"],
                evidence=sources,
                claim_evidence=values["claim_evidence"] or None,
                subject_key=values["subject_key"],
                assertion_kind=values["assertion_kind"],
                valid_from=values["valid_from"],
                valid_to=values["valid_to"],
            )
            if sources and all(source.content_hash for source in sources)
            else ""
        )
        conn.execute(
            """
            UPDATE memory_candidates
               SET proposal_digest=?, projection_digest=?
             WHERE id=?
            """,
            (current_proposal_digest, current_projection_digest, row["id"]),
        )


def _legacy_projection_values(row: sqlite3.Row) -> dict[str, object] | None:
    string_fields = (
        "id",
        "kind",
        "operation",
        "target_path",
        "target_entry_id",
        "target_entry_hash",
        "content",
        "content_hash",
        "claim_evidence_json",
        "tags_json",
        "conflict_key",
        "subject_key",
        "assertion_kind",
        "valid_from",
        "valid_to",
    )
    if any(not isinstance(row[name], str) for name in string_fields):
        return None
    if not all(row[name] for name in ("id", "kind", "operation", "target_path")):
        return None
    if hashlib.sha256(row["content"].encode()).hexdigest() != row["content_hash"]:
        return None
    try:
        tags = json.loads(row["tags_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        return None
    claim_evidence = _parse_evidence_refs(row["claim_evidence_json"])
    if claim_evidence is None:
        return None
    confidence = row["confidence"]
    if confidence is not None:
        if type(confidence) not in (int, float):
            return None
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            return None
    return {
        "kind": row["kind"],
        "operation": row["operation"],
        "target_path": row["target_path"],
        "target_entry_id": row["target_entry_id"],
        "target_entry_hash": row["target_entry_hash"],
        "content": row["content"],
        "content_hash": row["content_hash"],
        "claim_evidence": claim_evidence,
        "tags": tags,
        "confidence": confidence,
        "conflict_key": row["conflict_key"],
        "subject_key": row["subject_key"],
        "assertion_kind": row["assertion_kind"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
    }


def _legacy_candidate_sources(
    conn: sqlite3.Connection,
    candidate_id: str,
) -> list[EvidenceRef]:
    try:
        rows = conn.execute(
            """
            SELECT source_kind, source_id, source_path,
                   source_timestamp, source_hash
              FROM provenance_edges
             WHERE subject_kind='memory_candidate'
               AND subject_id=? AND subject_path=''
             ORDER BY ordinal, source_kind, source_path, source_id
            """,
            (candidate_id,),
        ).fetchall()
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
    except (sqlite3.DatabaseError, TypeError, ValueError):
        # A missing/legacy provenance table or one malformed edge must never
        # create a partially bound proposal digest.
        return []


def _parse_evidence_refs(raw: object) -> list[EvidenceRef] | None:
    if not isinstance(raw, str):
        return None
    try:
        values = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(values, list):
        return None
    try:
        refs = [EvidenceRef.from_dict(value) for value in values]
    except (TypeError, ValueError):
        return None
    identities = {(ref.kind, ref.path, ref.id) for ref in refs}
    return refs if len(identities) == len(refs) else None


def _evidence_refs_json(refs: list[EvidenceRef]) -> str:
    return json.dumps(
        [ref.to_dict() for ref in refs],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def projection_digest(
    *,
    kind: str,
    operation: str,
    target_path: str,
    content: str,
    content_hash: str,
    tags: list[str],
    confidence: float | None,
    conflict_key: str,
    target_entry_id: str = "",
    target_entry_hash: str = "",
    claim_evidence: list[EvidenceRef] | None = None,
    subject_key: str = "",
    assertion_kind: str = "",
    valid_from: str = "",
    valid_to: str = "",
) -> str:
    payload = {
        "kind": kind,
        "operation": operation,
        "target_path": target_path,
        "content": content,
        "content_hash": content_hash,
        "tags": tags,
        "confidence": confidence,
        "conflict_key": conflict_key,
    }
    if operation != "append" or target_entry_id or target_entry_hash:
        payload["target_entry_id"] = target_entry_id
        payload["target_entry_hash"] = target_entry_hash
    if claim_evidence:
        payload["claim_evidence"] = [
            ref.to_dict()
            for ref in sorted(
                claim_evidence,
                key=lambda ref: (
                    ref.kind,
                    ref.path,
                    ref.id,
                    ref.timestamp,
                    ref.content_hash,
                ),
            )
        ]
    if subject_key or assertion_kind or valid_from or valid_to:
        payload["fact"] = {
            "subject_key": subject_key,
            "assertion_kind": assertion_kind,
            "valid_from": valid_from,
            "valid_to": valid_to,
        }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def projection_is_current(candidate: MemoryCandidate) -> bool:
    return bool(
        candidate.content_hash == hashlib.sha256(candidate.content.encode()).hexdigest()
        and candidate.projection_digest
        == projection_digest(
            kind=candidate.kind,
            operation=candidate.operation,
            target_path=candidate.target_path,
            content=candidate.content,
            content_hash=candidate.content_hash,
            tags=candidate.tags,
            confidence=candidate.confidence,
            conflict_key=candidate.conflict_key,
            target_entry_id=candidate.target_entry_id,
            target_entry_hash=candidate.target_entry_hash,
            claim_evidence=candidate.claim_evidence,
            subject_key=candidate.subject_key,
            assertion_kind=candidate.assertion_kind,
            valid_from=candidate.valid_from,
            valid_to=candidate.valid_to,
        )
    )


def proposal_digest(
    *,
    kind: str,
    target_path: str,
    content_hash: str,
    tags: list[str],
    evidence: list[EvidenceRef],
    operation: str = "append",
    target_entry_id: str = "",
    target_entry_hash: str = "",
    claim_evidence: list[EvidenceRef] | None = None,
    subject_key: str = "",
    assertion_kind: str = "",
    valid_from: str = "",
    valid_to: str = "",
) -> str:
    """Bind a candidate proposal to the exact source revisions it saw."""
    source_keys = sorted(
        f"{ref.kind}\0{ref.path}\0{ref.id}\0{ref.content_hash}" for ref in evidence
    )
    claim_keys = (
        sorted(
            f"{ref.kind}\0{ref.path}\0{ref.id}\0{ref.content_hash}"
            for ref in claim_evidence
        )
        if claim_evidence is not None
        else source_keys
    )
    if subject_key or assertion_kind or valid_from or valid_to:
        fields = [
            "memory-candidate-v4",
            kind,
            operation,
            target_path,
            target_entry_id,
            target_entry_hash,
            content_hash,
            subject_key,
            assertion_kind,
            valid_from,
            valid_to,
            *sorted(tags),
            "claim-sources",
            *claim_keys,
            "flow-sources",
            *source_keys,
        ]
        return hashlib.sha256("\0".join(fields).encode()).hexdigest()
    if claim_keys != source_keys:
        fields = [
            "memory-candidate-v3",
            kind,
            operation,
            target_path,
            target_entry_id,
            target_entry_hash,
            content_hash,
            *sorted(tags),
            "claim-sources",
            *claim_keys,
            "flow-sources",
            *source_keys,
        ]
        return hashlib.sha256("\0".join(fields).encode()).hexdigest()
    if operation == "append" and not target_entry_id and not target_entry_hash:
        fields = ["memory-candidate-v1", kind, target_path, content_hash]
    else:
        fields = [
            "memory-candidate-v2",
            kind,
            operation,
            target_path,
            target_entry_id,
            target_entry_hash,
            content_hash,
        ]
    payload = "\0".join([*fields, *sorted(tags), *source_keys])
    return hashlib.sha256(payload.encode()).hexdigest()


def proposal_is_current(
    candidate: MemoryCandidate,
    evidence: list[EvidenceRef],
) -> bool:
    """Reject a source-edge swap even when every replacement is current."""
    return bool(
        candidate.proposal_digest
        and candidate.proposal_digest
        == proposal_digest(
            kind=candidate.kind,
            operation=candidate.operation,
            target_path=candidate.target_path,
            target_entry_id=candidate.target_entry_id,
            target_entry_hash=candidate.target_entry_hash,
            content_hash=candidate.content_hash,
            tags=candidate.tags,
            evidence=evidence,
            claim_evidence=candidate.claim_evidence or None,
            subject_key=candidate.subject_key,
            assertion_kind=candidate.assertion_kind,
            valid_from=candidate.valid_from,
            valid_to=candidate.valid_to,
        )
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
    target_entry_id: str,
    target_entry_hash: str,
    content: str,
    content_hash: str,
    claim_evidence: list[EvidenceRef],
    tags: list[str],
    confidence: float | None,
    conflict_key: str,
    subject_key: str,
    assertion_kind: str,
    valid_from: str,
    valid_to: str,
    status: str,
) -> tuple[MemoryCandidate, bool]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid candidate status: {status}")
    now = datetime.now().astimezone().isoformat()
    current_projection_digest = projection_digest(
        kind=kind,
        operation=operation,
        target_path=target_path,
        content=content,
        content_hash=content_hash,
        tags=tags,
        confidence=confidence,
        conflict_key=conflict_key,
        target_entry_id=target_entry_id,
        target_entry_hash=target_entry_hash,
        claim_evidence=claim_evidence,
        subject_key=subject_key,
        assertion_kind=assertion_kind,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO memory_candidates(
            id, idempotency_key, proposal_digest, projection_digest, producer_run_key,
            proposal_slot, kind, operation, target_path,
            target_entry_id, target_entry_hash, content,
            content_hash, claim_evidence_json, tags_json, confidence, conflict_key,
            subject_key, assertion_kind, valid_from, valid_to, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            idempotency_key,
            proposal_digest,
            current_projection_digest,
            producer_run_key,
            proposal_slot,
            kind,
            operation,
            target_path,
            target_entry_id,
            target_entry_hash,
            content,
            content_hash,
            _evidence_refs_json(claim_evidence),
            json.dumps(tags, ensure_ascii=False),
            confidence,
            conflict_key,
            subject_key,
            assertion_kind,
            valid_from,
            valid_to,
            status,
            now,
            now,
        ),
    )
    row = get_by_idempotency_key(conn, idempotency_key)
    if row is None:
        raise RuntimeError("candidate insert did not produce a row")
    if not projection_is_current(row):
        raise ValueError("candidate semantic projection is missing or changed")
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


def active_subject_conflicts(
    conn: sqlite3.Connection,
    *,
    subject_key: str,
    content_hash: str,
) -> list[MemoryCandidate]:
    if not subject_key:
        return []
    rows = conn.execute(
        """
        SELECT * FROM memory_candidates
         WHERE subject_key=? AND content_hash<>?
           AND status IN ('pending', 'conflict', 'applying', 'accepted')
         ORDER BY created_at, id
        """,
        (subject_key, content_hash),
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
    proposal_digest: str,
    status: str,
) -> MemoryCandidate:
    now = datetime.now().astimezone().isoformat()
    current = get(conn, candidate_id)
    if current is None or not projection_is_current(current):
        raise CandidateConflict("candidate semantic projection changed")
    next_projection_digest = projection_digest(
        kind=current.kind,
        operation=current.operation,
        target_path=current.target_path,
        content=content,
        content_hash=content_hash,
        tags=tags,
        confidence=current.confidence,
        conflict_key=conflict_key,
        target_entry_id=current.target_entry_id,
        target_entry_hash=current.target_entry_hash,
        claim_evidence=current.claim_evidence,
        subject_key=current.subject_key,
        assertion_kind=current.assertion_kind,
        valid_from=current.valid_from,
        valid_to=current.valid_to,
    )
    result = conn.execute(
        """
        UPDATE memory_candidates
           SET content=?, content_hash=?, tags_json=?, conflict_key=?, proposal_digest=?,
               projection_digest=?,
               status=?, version=version+1, updated_at=?, last_error=''
         WHERE id=? AND version=? AND status IN ('pending', 'conflict')
        """,
        (
            content,
            content_hash,
            json.dumps(tags, ensure_ascii=False),
            conflict_key,
            proposal_digest,
            next_projection_digest,
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
    claim_evidence = _parse_evidence_refs(row["claim_evidence_json"])
    return MemoryCandidate(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        proposal_digest=row["proposal_digest"] or "",
        projection_digest=row["projection_digest"] or "",
        producer_run_key=row["producer_run_key"] or "",
        proposal_slot=int(row["proposal_slot"] or 0),
        kind=row["kind"],
        operation=row["operation"],
        target_path=row["target_path"],
        target_entry_id=row["target_entry_id"] or "",
        target_entry_hash=row["target_entry_hash"] or "",
        content=row["content"],
        content_hash=row["content_hash"],
        claim_evidence=claim_evidence or [],
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        conflict_key=row["conflict_key"] or "",
        subject_key=row["subject_key"] or "",
        assertion_kind=row["assertion_kind"] or "",
        valid_from=row["valid_from"] or "",
        valid_to=row["valid_to"] or "",
        status=row["status"],
        version=int(row["version"]),
        applied_entry_id=row["applied_entry_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        reviewed_at=row["reviewed_at"],
        review_reason=row["review_reason"] or "",
        last_error=row["last_error"] or "",
    )
