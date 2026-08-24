"""Durable suggestions and prepared artifacts with exact provenance bindings."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, canonical_digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    semantic_key TEXT NOT NULL,
    workflow TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('ready', 'viewed', 'accepted', 'dismissed', 'expired')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    capture_generation INTEGER NOT NULL,
    score REAL NOT NULL,
    detected_at TEXT NOT NULL,
    detected_at_us INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    expires_at_us INTEGER NOT NULL,
    feedback_reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    projection_digest TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggestions_inbox
    ON suggestions(status, detected_at_us DESC, id);
CREATE INDEX IF NOT EXISTS idx_suggestions_semantic
    ON suggestions(semantic_key, detected_at_us DESC, id);
CREATE INDEX IF NOT EXISTS idx_suggestions_budget
    ON suggestions(detected_at_us, id);
"""

VALID_STATUSES = {"ready", "viewed", "accepted", "dismissed", "expired"}
ACTIVE_STATUSES = {"ready", "viewed"}


@dataclass(frozen=True, slots=True)
class Suggestion:
    id: str
    idempotency_key: str
    semantic_key: str
    workflow: str
    status: str
    title: str
    summary: str
    artifact: dict[str, Any]
    artifact_digest: str
    evidence_digest: str
    policy_digest: str
    capture_generation: int
    score: float
    detected_at: str
    expires_at: str
    feedback_reason: str
    version: int
    projection_digest: str
    updated_at: str


class SuggestionConflict(RuntimeError):
    """Raised when a suggestion changed after the caller read it."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    # ``executescript`` implicitly commits a pending SQLite transaction.
    # Suggestion reads can be nested inside the desktop snapshot transaction,
    # so execute these simple DDL statements individually instead.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    columns = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute("PRAGMA table_info(suggestions)")
    }
    if "capture_generation" not in columns:
        # Suggestions were unreleased when this field was introduced. A zero
        # binding deliberately makes every pre-field row fail closed against
        # a non-empty capture store; its old projection digest also cannot be
        # mistaken for the new closed schema below.
        conn.execute(
            "ALTER TABLE suggestions ADD COLUMN capture_generation INTEGER NOT NULL DEFAULT 0"
        )


def capture_generation(conn: sqlite3.Connection) -> int:
    """Return the durable, non-rewinding capture insertion generation."""
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='captures'").fetchone()
    if row is None:
        return 0
    value = row["seq"] if isinstance(row, sqlite3.Row) else row[0]
    if type(value) is not int or value < 0:
        raise RuntimeError("capture generation is invalid")
    return value


def evidence_digest(evidence: list[EvidenceRef]) -> str:
    return canonical_digest(
        {
            "schema": "suggestion-evidence-v1",
            "sources": sorted(
                (source.to_dict() for source in evidence),
                key=lambda value: (
                    value["kind"],
                    value["path"],
                    value["id"],
                    value["timestamp"],
                    value["content_hash"],
                ),
            ),
        }
    )


def proposal_digest(
    *,
    semantic_key: str,
    workflow: str,
    title: str,
    summary: str,
    artifact_digest: str,
    evidence_digest: str,
    policy_digest: str,
    capture_generation: int,
    score: float,
    expires_at: str,
) -> str:
    return canonical_digest(
        {
            "schema": "suggestion-proposal-v1",
            "semantic_key": semantic_key,
            "workflow": workflow,
            "title": title,
            "summary": summary,
            "artifact_digest": artifact_digest,
            "evidence_digest": evidence_digest,
            "policy_digest": policy_digest,
            "capture_generation": capture_generation,
            "score": score,
            "expires_at": expires_at,
        }
    )


def _projection_digest(
    *,
    suggestion_id: str,
    idempotency_key: str,
    semantic_key: str,
    workflow: str,
    status: str,
    title: str,
    summary: str,
    artifact_digest: str,
    evidence_digest: str,
    policy_digest: str,
    capture_generation: int,
    score: float,
    detected_at: str,
    detected_at_us: int,
    expires_at: str,
    expires_at_us: int,
    feedback_reason: str,
    version: int,
    updated_at: str,
) -> str:
    return canonical_digest(
        {
            "schema": "suggestion-projection-v1",
            "id": suggestion_id,
            "idempotency_key": idempotency_key,
            "semantic_key": semantic_key,
            "workflow": workflow,
            "status": status,
            "title": title,
            "summary": summary,
            "artifact_digest": artifact_digest,
            "evidence_digest": evidence_digest,
            "policy_digest": policy_digest,
            "capture_generation": capture_generation,
            "score": score,
            "detected_at": detected_at,
            "detected_at_us": detected_at_us,
            "expires_at": expires_at,
            "expires_at_us": expires_at_us,
            "feedback_reason": feedback_reason,
            "version": version,
            "updated_at": updated_at,
        }
    )


def insert(
    conn: sqlite3.Connection,
    *,
    suggestion_id: str,
    idempotency_key: str,
    semantic_key: str,
    workflow: str,
    title: str,
    summary: str,
    artifact: dict[str, Any],
    evidence: list[EvidenceRef],
    policy_digest: str,
    capture_generation: int,
    score: float,
    detected_at: datetime,
    expires_at: datetime,
) -> tuple[Suggestion, bool]:
    if (
        not all(
            isinstance(value, str) and value.strip()
            for value in (
                suggestion_id,
                idempotency_key,
                semantic_key,
                workflow,
                title,
                summary,
                policy_digest,
            )
        )
        or not isinstance(artifact, dict)
        or not evidence
        or type(capture_generation) is not int
        or capture_generation < 0
        or isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(float(score))
        or not 0.0 <= float(score) <= 1.0
    ):
        raise ValueError("invalid suggestion proposal")
    detected = _aware(detected_at)
    expires = _aware(expires_at)
    if expires <= detected:
        raise ValueError("suggestion expiry must follow detection")
    artifact_json = json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    artifact_hash = canonical_digest(artifact)
    source_hash = evidence_digest(evidence)
    detected_text = detected.isoformat(timespec="microseconds")
    expires_text = expires.isoformat(timespec="microseconds")
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    detected_us = _instant_us(detected)
    expires_us = _instant_us(expires)
    projection = _projection_digest(
        suggestion_id=suggestion_id,
        idempotency_key=idempotency_key,
        semantic_key=semantic_key,
        workflow=workflow,
        status="ready",
        title=title,
        summary=summary,
        artifact_digest=artifact_hash,
        evidence_digest=source_hash,
        policy_digest=policy_digest,
        capture_generation=capture_generation,
        score=float(score),
        detected_at=detected_text,
        detected_at_us=detected_us,
        expires_at=expires_text,
        expires_at_us=expires_us,
        feedback_reason="",
        version=1,
        updated_at=now,
    )
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO suggestions(
            id, idempotency_key, semantic_key, workflow, status,
            title, summary, artifact_json, artifact_digest, evidence_digest,
            policy_digest, capture_generation, score, detected_at, detected_at_us,
            expires_at, expires_at_us, feedback_reason, version,
            projection_digest, updated_at
        ) VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 1, ?, ?)
        """,
        (
            suggestion_id,
            idempotency_key,
            semantic_key,
            workflow,
            title,
            summary,
            artifact_json,
            artifact_hash,
            source_hash,
            policy_digest,
            capture_generation,
            float(score),
            detected_text,
            detected_us,
            expires_text,
            expires_us,
            projection,
            now,
        ),
    )
    row = get_by_idempotency_key(conn, idempotency_key)
    if row is None:
        raise RuntimeError("suggestion insert did not produce a current row")
    created = conn.total_changes > before
    if created:
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="suggestion", id=row.id),
            sources=evidence,
        )
    elif (
        evidence_digest(
            provenance_store.direct_sources(conn, EvidenceRef(kind="suggestion", id=row.id))
        )
        != row.evidence_digest
    ):
        raise RuntimeError("suggestion replay evidence differs from durable proposal")
    return row, created


def get(conn: sqlite3.Connection, suggestion_id: str) -> Suggestion | None:
    row = conn.execute("SELECT * FROM suggestions WHERE id=?", (suggestion_id,)).fetchone()
    return _to_suggestion(row) if row is not None else None


def get_by_idempotency_key(
    conn: sqlite3.Connection,
    idempotency_key: str,
) -> Suggestion | None:
    row = conn.execute(
        "SELECT * FROM suggestions WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    return _to_suggestion(row) if row is not None else None


def has_semantic_since(
    conn: sqlite3.Connection,
    semantic_key: str,
    *,
    since_us: int,
) -> bool:
    """Fail closed for cooldown even when a stored projection is malformed."""
    row = conn.execute(
        """
        SELECT 1 FROM suggestions
         WHERE semantic_key=? AND detected_at_us>=?
         LIMIT 1
        """,
        (semantic_key, since_us),
    ).fetchone()
    return row is not None


def count_detected_between(
    conn: sqlite3.Connection,
    *,
    start_us: int,
    end_us: int,
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM suggestions
         WHERE detected_at_us>=? AND detected_at_us<?
        """,
        (start_us, end_us),
    ).fetchone()
    return int(row[0])


def list_suggestions(
    conn: sqlite3.Connection,
    *,
    statuses: list[str] | None = None,
    limit: int = 100,
) -> list[Suggestion]:
    if limit < 1 or limit > 1_000:
        raise ValueError("limit must be in [1, 1000]")
    args: list[object] = []
    where = ""
    if statuses:
        if set(statuses) - VALID_STATUSES:
            raise ValueError("invalid suggestion status")
        where = f"WHERE status IN ({','.join('?' for _ in statuses)})"
        args.extend(statuses)
    args.append(limit)
    rows = conn.execute(
        f"""
        SELECT * FROM suggestions {where}
         ORDER BY detected_at_us DESC, id DESC
         LIMIT ?
        """,
        tuple(args),
    ).fetchall()
    return [item for row in rows if (item := _to_suggestion(row)) is not None]


def transition(
    conn: sqlite3.Connection,
    *,
    suggestion_id: str,
    expected_version: int,
    from_statuses: tuple[str, ...],
    to_status: str,
    reason: str = "",
) -> Suggestion:
    if to_status not in VALID_STATUSES or not from_statuses:
        raise ValueError("invalid suggestion transition")
    current = get(conn, suggestion_id)
    if current is None:
        raise SuggestionConflict("suggestion is missing or its projection changed")
    if current.status not in from_statuses or current.version != expected_version:
        raise SuggestionConflict("suggestion changed")
    next_version = current.version + 1
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    detected = _aware(datetime.fromisoformat(current.detected_at))
    expires = _aware(datetime.fromisoformat(current.expires_at))
    projection = _projection_digest(
        suggestion_id=current.id,
        idempotency_key=current.idempotency_key,
        semantic_key=current.semantic_key,
        workflow=current.workflow,
        status=to_status,
        title=current.title,
        summary=current.summary,
        artifact_digest=current.artifact_digest,
        evidence_digest=current.evidence_digest,
        policy_digest=current.policy_digest,
        capture_generation=current.capture_generation,
        score=current.score,
        detected_at=current.detected_at,
        detected_at_us=_instant_us(detected),
        expires_at=current.expires_at,
        expires_at_us=_instant_us(expires),
        feedback_reason=reason,
        version=next_version,
        updated_at=now,
    )
    placeholders = ",".join("?" for _ in from_statuses)
    result = conn.execute(
        f"""
        UPDATE suggestions
           SET status=?, feedback_reason=?, version=?,
               projection_digest=?, updated_at=?
         WHERE id=? AND version=? AND status IN ({placeholders})
        """,
        (
            to_status,
            reason,
            next_version,
            projection,
            now,
            suggestion_id,
            expected_version,
            *from_statuses,
        ),
    )
    if result.rowcount != 1:
        raise SuggestionConflict("suggestion changed")
    updated = get(conn, suggestion_id)
    if updated is None:
        raise SuggestionConflict("suggestion projection changed during transition")
    return updated


def expire_due(conn: sqlite3.Connection, *, now: datetime) -> list[str]:
    now_us = _instant_us(_aware(now))
    rows = conn.execute(
        """
        SELECT id, version FROM suggestions
         WHERE status IN ('ready', 'viewed') AND expires_at_us<=?
         ORDER BY expires_at_us, id
        """,
        (now_us,),
    ).fetchall()
    expired: list[str] = []
    for row in rows:
        try:
            transition(
                conn,
                suggestion_id=str(row["id"]),
                expected_version=int(row["version"]),
                from_statuses=("ready", "viewed"),
                to_status="expired",
                reason="expired",
            )
        except SuggestionConflict:
            continue
        expired.append(str(row["id"]))
    return expired


def sources_current(conn: sqlite3.Connection, suggestion: Suggestion) -> bool:
    sources = provenance_store.direct_sources_checked(
        conn,
        EvidenceRef(kind="suggestion", id=suggestion.id),
    )
    return bool(sources and evidence_digest(sources) == suggestion.evidence_digest)


def _to_suggestion(row: sqlite3.Row | tuple) -> Suggestion | None:
    try:
        artifact = json.loads(row["artifact_json"])
        score = row["score"]
        detected = _aware(datetime.fromisoformat(row["detected_at"]))
        expires = _aware(datetime.fromisoformat(row["expires_at"]))
        if (
            not isinstance(artifact, dict)
            or row["status"] not in VALID_STATUSES
            or type(row["version"]) is not int
            or row["version"] < 1
            or type(row["capture_generation"]) is not int
            or row["capture_generation"] < 0
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 0.0 <= float(score) <= 1.0
            or row["artifact_digest"] != canonical_digest(artifact)
            or row["detected_at_us"] != _instant_us(detected)
            or row["expires_at_us"] != _instant_us(expires)
            or expires <= detected
        ):
            return None
        suggestion = Suggestion(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            semantic_key=row["semantic_key"],
            workflow=row["workflow"],
            status=row["status"],
            title=row["title"],
            summary=row["summary"],
            artifact=artifact,
            artifact_digest=row["artifact_digest"],
            evidence_digest=row["evidence_digest"],
            policy_digest=row["policy_digest"],
            capture_generation=row["capture_generation"],
            score=float(score),
            detected_at=row["detected_at"],
            expires_at=row["expires_at"],
            feedback_reason=row["feedback_reason"],
            version=row["version"],
            projection_digest=row["projection_digest"],
            updated_at=row["updated_at"],
        )
        expected = _projection_digest(
            suggestion_id=suggestion.id,
            idempotency_key=suggestion.idempotency_key,
            semantic_key=suggestion.semantic_key,
            workflow=suggestion.workflow,
            status=suggestion.status,
            title=suggestion.title,
            summary=suggestion.summary,
            artifact_digest=suggestion.artifact_digest,
            evidence_digest=suggestion.evidence_digest,
            policy_digest=suggestion.policy_digest,
            capture_generation=suggestion.capture_generation,
            score=suggestion.score,
            detected_at=suggestion.detected_at,
            detected_at_us=_instant_us(detected),
            expires_at=suggestion.expires_at,
            expires_at_us=_instant_us(expires),
            feedback_reason=suggestion.feedback_reason,
            version=suggestion.version,
            updated_at=suggestion.updated_at,
        )
        return suggestion if suggestion.projection_digest == expected else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("suggestion timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _instant_us(value: datetime) -> int:
    instant = _aware(value)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = instant - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
