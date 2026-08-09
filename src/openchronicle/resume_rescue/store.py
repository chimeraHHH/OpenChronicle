"""Immutable profile versions and opportunity snapshots for Résumé Rescue."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, canonical_digest
from .models import (
    build_exact_artifact,
    opportunity_digest,
    profile_digest,
    validate_artifact,
    validate_opportunity,
    validate_profile,
    validate_projection_request,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS resume_profiles (
    profile_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    profile_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL,
    PRIMARY KEY(profile_id, version)
);
CREATE TABLE IF NOT EXISTS resume_profile_heads (
    profile_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    row_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resume_profiles_created
    ON resume_profiles(created_at DESC, profile_id, version);

CREATE TABLE IF NOT EXISTS resume_opportunities (
    id TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL,
    snapshot_digest TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resume_opportunities_created
    ON resume_opportunities(created_at DESC, id);
CREATE TABLE IF NOT EXISTS resume_opportunity_supersessions (
    previous_id TEXT PRIMARY KEY,
    next_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL,
    CHECK(previous_id <> next_id)
);
CREATE INDEX IF NOT EXISTS idx_resume_opportunity_next
    ON resume_opportunity_supersessions(next_id);

CREATE TABLE IF NOT EXISTS resume_rescue_projections (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    profile_id TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    profile_digest TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    opportunity_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resume_projections_profile
    ON resume_rescue_projections(profile_id, profile_version, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_resume_projections_opportunity
    ON resume_rescue_projections(opportunity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS resume_rewrite_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'leased', 'ready', 'failed')),
    projection_id TEXT NOT NULL,
    projection_artifact_digest TEXT NOT NULL,
    projection_created_at TEXT NOT NULL,
    provider_input_json TEXT NOT NULL,
    provider_input_digest TEXT NOT NULL,
    template_version INTEGER NOT NULL,
    template_digest TEXT NOT NULL,
    model_identity TEXT NOT NULL,
    provider_location TEXT NOT NULL
        CHECK (provider_location IN ('local', 'remote_or_unknown')),
    remote_egress_authorized INTEGER NOT NULL
        CHECK (remote_egress_authorized IN (0, 1)),
    output_json TEXT NOT NULL DEFAULT '',
    output_digest TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    row_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resume_rewrite_queue
    ON resume_rewrite_jobs(status, created_at_us, id);
CREATE INDEX IF NOT EXISTS idx_resume_rewrite_recent
    ON resume_rewrite_jobs(created_at_us DESC, id);

CREATE TABLE IF NOT EXISTS resume_rewrite_versions (
    id TEXT PRIMARY KEY,
    lineage_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('decision', 'restore')),
    proposal_id TEXT NOT NULL,
    proposal_digest TEXT NOT NULL,
    restore_target_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected', 'restored')),
    base_projection_id TEXT NOT NULL,
    base_artifact_digest TEXT NOT NULL,
    rewrite_job_id TEXT NOT NULL,
    rewrite_output_digest TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    row_digest TEXT NOT NULL,
    UNIQUE(lineage_id, version)
);
CREATE INDEX IF NOT EXISTS idx_resume_rewrite_versions_lineage
    ON resume_rewrite_versions(lineage_id, version DESC);
CREATE TABLE IF NOT EXISTS resume_rewrite_heads (
    lineage_id TEXT PRIMARY KEY,
    current_id TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    current_artifact_digest TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_digest TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ProfileVersion:
    profile_id: str
    version: int
    profile: dict[str, Any]
    digest: str
    created_at: str

    @property
    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            kind="resume_profile",
            id=self.profile_id,
            path=str(self.version),
            timestamp=self.created_at,
            content_hash=self.digest,
        )


@dataclass(frozen=True, slots=True)
class OpportunitySnapshot:
    id: str
    snapshot: dict[str, Any]
    digest: str
    created_at: str

    @property
    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            kind="resume_opportunity",
            id=self.id,
            timestamp=self.created_at,
            content_hash=self.digest,
        )


@dataclass(frozen=True, slots=True)
class ResumeProjection:
    id: str
    idempotency_key: str
    profile_id: str
    profile_version: int
    profile_digest: str
    opportunity_id: str
    opportunity_digest: str
    request: dict[str, Any]
    request_digest: str
    artifact: dict[str, Any]
    artifact_digest: str
    created_at: str

    @property
    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            kind="resume_rescue",
            id=self.id,
            timestamp=self.created_at,
            content_hash=self.artifact_digest,
        )


class ResumeRescueConflict(RuntimeError):
    """A source changed since the caller's last reviewed version."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def save_profile(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    *,
    expected_version: int | None,
    now: datetime | None = None,
) -> tuple[ProfileVersion, bool]:
    ensure_schema(conn)
    normalized = validate_profile(profile)
    digest = profile_digest(normalized)
    timestamp = _timestamp(now)
    profile_id = normalized["profile_id"]
    with _atomic(conn, "resume_profile_save"):
        head_exists = (
            conn.execute(
                "SELECT 1 FROM resume_profile_heads WHERE profile_id=?", (profile_id,)
            ).fetchone()
            is not None
        )
        current = get_current_profile(conn, profile_id)
        if head_exists and current is None:
            raise ResumeRescueConflict("resume profile changed")
        if current is not None and current.digest == digest:
            return current, False
        if current is None:
            if expected_version not in {None, 0}:
                raise ResumeRescueConflict("resume profile changed")
            next_version = 1
        else:
            if type(expected_version) is not int or expected_version != current.version:
                raise ResumeRescueConflict("resume profile changed")
            next_version = current.version + 1
        row_digest = _profile_row_digest(
            profile_id=profile_id,
            version=next_version,
            profile_digest_value=digest,
            created_at=timestamp,
        )
        conn.execute(
            """
            INSERT INTO resume_profiles(
                profile_id, version, profile_json, profile_digest, created_at, row_digest
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (profile_id, next_version, _json(normalized), digest, timestamp, row_digest),
        )
        head_digest = _head_digest(profile_id, next_version, timestamp)
        conn.execute(
            """
            INSERT INTO resume_profile_heads(profile_id, current_version, updated_at, row_digest)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                current_version=excluded.current_version,
                updated_at=excluded.updated_at,
                row_digest=excluded.row_digest
            """,
            (profile_id, next_version, timestamp, head_digest),
        )
        saved = get_current_profile(conn, profile_id)
        if saved is None or saved.version != next_version:
            raise RuntimeError("resume profile save did not produce a valid current version")
        return saved, True


def get_profile_version(
    conn: sqlite3.Connection, profile_id: str, version: int
) -> ProfileVersion | None:
    row = conn.execute(
        "SELECT * FROM resume_profiles WHERE profile_id=? AND version=?",
        (profile_id, version),
    ).fetchone()
    return _to_profile(row) if row is not None else None


def get_current_profile(conn: sqlite3.Connection, profile_id: str) -> ProfileVersion | None:
    head = conn.execute(
        "SELECT * FROM resume_profile_heads WHERE profile_id=?", (profile_id,)
    ).fetchone()
    if head is None or not _valid_head(head):
        return None
    return get_profile_version(conn, profile_id, head["current_version"])


def list_current_profiles(conn: sqlite3.Connection, *, limit: int = 50) -> list[ProfileVersion]:
    _limit(limit)
    rows = conn.execute(
        """
        SELECT p.* FROM resume_profile_heads h
        JOIN resume_profiles p
          ON p.profile_id=h.profile_id AND p.version=h.current_version
        ORDER BY h.updated_at DESC, h.profile_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [item for row in rows if (item := _to_profile_with_head(conn, row)) is not None]


def create_opportunity(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[OpportunitySnapshot, bool]:
    ensure_schema(conn)
    return _create_opportunity(conn, snapshot, now=now)


def _create_opportunity(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[OpportunitySnapshot, bool]:
    normalized = validate_opportunity(snapshot)
    digest = opportunity_digest(normalized)
    opportunity_id = f"resume-opportunity-{digest[:32]}"
    timestamp = _timestamp(now)
    row_digest = _opportunity_row_digest(opportunity_id, digest, timestamp)
    before = conn.total_changes
    with _atomic(conn, "resume_opportunity_create"):
        conn.execute(
            """
            INSERT OR IGNORE INTO resume_opportunities(
                id, snapshot_json, snapshot_digest, created_at, row_digest
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (opportunity_id, _json(normalized), digest, timestamp, row_digest),
        )
        current = _get_opportunity_snapshot(conn, opportunity_id)
        if current is None or current.digest != digest:
            raise RuntimeError("resume opportunity insert did not produce a valid snapshot")
        return current, conn.total_changes > before


def get_opportunity(conn: sqlite3.Connection, opportunity_id: str) -> OpportunitySnapshot | None:
    current = _get_opportunity_snapshot(conn, opportunity_id)
    if current is None:
        return None
    replaced = conn.execute(
        "SELECT 1 FROM resume_opportunity_supersessions WHERE previous_id=?",
        (opportunity_id,),
    ).fetchone()
    return current if replaced is None else None


def _get_opportunity_snapshot(
    conn: sqlite3.Connection, opportunity_id: str
) -> OpportunitySnapshot | None:
    row = conn.execute(
        "SELECT * FROM resume_opportunities WHERE id=?", (opportunity_id,)
    ).fetchone()
    return _to_opportunity(row) if row is not None else None


def list_opportunities(conn: sqlite3.Connection, *, limit: int = 50) -> list[OpportunitySnapshot]:
    _limit(limit)
    rows = conn.execute(
        """
        SELECT o.* FROM resume_opportunities o
        LEFT JOIN resume_opportunity_supersessions s ON s.previous_id=o.id
        WHERE s.previous_id IS NULL
        ORDER BY o.created_at DESC, o.id LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [item for row in rows if (item := _to_opportunity(row)) is not None]


def replace_opportunity(
    conn: sqlite3.Connection,
    *,
    opportunity_id: str,
    expected_digest: str,
    snapshot: dict[str, Any],
    now: datetime | None = None,
) -> tuple[OpportunitySnapshot, bool]:
    ensure_schema(conn)
    normalized = validate_opportunity(snapshot)
    replacement_digest = opportunity_digest(normalized)
    with _atomic(conn, "resume_opportunity_replace"):
        original = _get_opportunity_snapshot(conn, opportunity_id)
        if original is None or original.digest != expected_digest:
            raise ResumeRescueConflict("resume opportunity changed")
        existing = conn.execute(
            "SELECT * FROM resume_opportunity_supersessions WHERE previous_id=?",
            (opportunity_id,),
        ).fetchone()
        if existing is not None:
            replacement = _replayed_replacement(
                conn,
                existing,
                expected_digest=replacement_digest,
            )
            if replacement is not None:
                return replacement, False
            raise ResumeRescueConflict("resume opportunity changed")
        if replacement_digest == original.digest:
            return original, False
        replacement, _ = _create_opportunity(conn, normalized, now=now)
        created_at = _timestamp(now)
        row_digest = _supersession_row_digest(
            previous_id=original.id,
            next_id=replacement.id,
            created_at=created_at,
        )
        try:
            conn.execute(
                """
                INSERT INTO resume_opportunity_supersessions(
                    previous_id, next_id, created_at, row_digest
                ) VALUES (?, ?, ?, ?)
                """,
                (original.id, replacement.id, created_at, row_digest),
            )
        except sqlite3.IntegrityError as exc:
            raise ResumeRescueConflict("resume opportunity changed") from exc
        return replacement, True


def _replayed_replacement(
    conn: sqlite3.Connection,
    row: sqlite3.Row | tuple,
    *,
    expected_digest: str,
) -> OpportunitySnapshot | None:
    try:
        _parse_timestamp(row["created_at"])
        if row["row_digest"] != _supersession_row_digest(
            previous_id=row["previous_id"],
            next_id=row["next_id"],
            created_at=row["created_at"],
        ):
            return None
        replacement = get_opportunity(conn, row["next_id"])
        return (
            replacement
            if replacement is not None and replacement.digest == expected_digest
            else None
        )
    except (KeyError, TypeError, ValueError):
        return None


def create_projection(
    conn: sqlite3.Connection,
    *,
    profile: ProfileVersion,
    opportunity: OpportunitySnapshot,
    request: dict[str, Any],
    now: datetime | None = None,
) -> tuple[ResumeProjection, bool]:
    ensure_schema(conn)
    normalized_request = validate_projection_request(request)
    artifact = build_exact_artifact(
        profile=profile.profile,
        profile_version=profile.version,
        profile_digest_value=profile.digest,
        opportunity=opportunity.snapshot,
        opportunity_id=opportunity.id,
        opportunity_digest_value=opportunity.digest,
        request=normalized_request,
    )
    request_hash = canonical_digest(
        {"schema": "resume-projection-request-v1", "request": normalized_request}
    )
    artifact_hash = canonical_digest({"schema": "resume-artifact-v1", "artifact": artifact})
    idempotency_key = canonical_digest(
        {
            "schema": "resume-exact-projection-job-v1",
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "profile_digest": profile.digest,
            "opportunity_id": opportunity.id,
            "opportunity_digest": opportunity.digest,
            "request_digest": request_hash,
        }
    )
    projection_id = f"resume-rescue-{idempotency_key[:32]}"
    created_at = _timestamp(now)
    row_digest = _projection_row_digest(
        projection_id=projection_id,
        idempotency_key=idempotency_key,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        profile_digest_value=profile.digest,
        opportunity_id=opportunity.id,
        opportunity_digest_value=opportunity.digest,
        request_digest=request_hash,
        artifact_digest=artifact_hash,
        created_at=created_at,
    )
    before = conn.total_changes
    with _atomic(conn, "resume_projection_create"):
        conn.execute(
            """
            INSERT OR IGNORE INTO resume_rescue_projections(
                id, idempotency_key, profile_id, profile_version, profile_digest,
                opportunity_id, opportunity_digest, request_json, request_digest,
                artifact_json, artifact_digest, created_at, row_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                projection_id,
                idempotency_key,
                profile.profile_id,
                profile.version,
                profile.digest,
                opportunity.id,
                opportunity.digest,
                _json(normalized_request),
                request_hash,
                _json(artifact),
                artifact_hash,
                created_at,
                row_digest,
            ),
        )
        current = get_projection(conn, projection_id)
        if current is None or current.idempotency_key != idempotency_key:
            raise RuntimeError("resume projection insert did not produce a valid artifact")
        subject = EvidenceRef(kind="resume_rescue", id=projection_id)
        expected_sources = [profile.ref, opportunity.ref]
        if conn.total_changes > before:
            provenance_store.replace_sources(conn, subject=subject, sources=expected_sources)
            created = True
        else:
            if provenance_store.direct_sources_checked(conn, subject) != expected_sources:
                raise RuntimeError("resume projection replay provenance differs")
            created = False
        return current, created


def get_projection(conn: sqlite3.Connection, projection_id: str) -> ResumeProjection | None:
    row = conn.execute(
        "SELECT * FROM resume_rescue_projections WHERE id=?", (projection_id,)
    ).fetchone()
    return _to_projection(row) if row is not None else None


def list_projections(conn: sqlite3.Connection, *, limit: int = 50) -> list[ResumeProjection]:
    _limit(limit)
    rows = conn.execute(
        "SELECT * FROM resume_rescue_projections ORDER BY created_at DESC, id LIMIT ?",
        (limit,),
    ).fetchall()
    return [item for row in rows if (item := _to_projection(row)) is not None]


def _to_profile_with_head(conn: sqlite3.Connection, row: sqlite3.Row) -> ProfileVersion | None:
    profile = _to_profile(row)
    if profile is None:
        return None
    current = get_current_profile(conn, profile.profile_id)
    return profile if current == profile else None


def _to_profile(row: sqlite3.Row | tuple) -> ProfileVersion | None:
    try:
        raw = json.loads(row["profile_json"])
        profile = validate_profile(raw)
        digest = profile_digest(profile)
        if (
            type(row["version"]) is not int
            or row["version"] < 1
            or row["profile_id"] != profile["profile_id"]
            or row["profile_digest"] != digest
            or row["row_digest"]
            != _profile_row_digest(
                profile_id=row["profile_id"],
                version=row["version"],
                profile_digest_value=digest,
                created_at=row["created_at"],
            )
        ):
            return None
        _parse_timestamp(row["created_at"])
        return ProfileVersion(
            profile_id=row["profile_id"],
            version=row["version"],
            profile=profile,
            digest=digest,
            created_at=row["created_at"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _to_opportunity(row: sqlite3.Row | tuple) -> OpportunitySnapshot | None:
    try:
        raw = json.loads(row["snapshot_json"])
        snapshot = validate_opportunity(raw)
        digest = opportunity_digest(snapshot)
        if (
            row["id"] != f"resume-opportunity-{digest[:32]}"
            or row["snapshot_digest"] != digest
            or row["row_digest"] != _opportunity_row_digest(row["id"], digest, row["created_at"])
        ):
            return None
        _parse_timestamp(row["created_at"])
        return OpportunitySnapshot(
            id=row["id"], snapshot=snapshot, digest=digest, created_at=row["created_at"]
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _to_projection(row: sqlite3.Row | tuple) -> ResumeProjection | None:
    try:
        request = validate_projection_request(json.loads(row["request_json"]))
        artifact = validate_artifact(json.loads(row["artifact_json"]))
        request_hash = canonical_digest(
            {"schema": "resume-projection-request-v1", "request": request}
        )
        artifact_hash = canonical_digest({"schema": "resume-artifact-v1", "artifact": artifact})
        idempotency_key = canonical_digest(
            {
                "schema": "resume-exact-projection-job-v1",
                "profile_id": row["profile_id"],
                "profile_version": row["profile_version"],
                "profile_digest": row["profile_digest"],
                "opportunity_id": row["opportunity_id"],
                "opportunity_digest": row["opportunity_digest"],
                "request_digest": request_hash,
            }
        )
        if (
            type(row["profile_version"]) is not int
            or row["profile_version"] < 1
            or row["id"] != f"resume-rescue-{idempotency_key[:32]}"
            or row["idempotency_key"] != idempotency_key
            or row["request_digest"] != request_hash
            or row["artifact_digest"] != artifact_hash
            or artifact["profile_binding"]
            != {
                "id": row["profile_id"],
                "version": row["profile_version"],
                "digest": row["profile_digest"],
            }
            or artifact["opportunity_binding"]["id"] != row["opportunity_id"]
            or artifact["opportunity_binding"]["digest"] != row["opportunity_digest"]
            or row["row_digest"]
            != _projection_row_digest(
                projection_id=row["id"],
                idempotency_key=idempotency_key,
                profile_id=row["profile_id"],
                profile_version=row["profile_version"],
                profile_digest_value=row["profile_digest"],
                opportunity_id=row["opportunity_id"],
                opportunity_digest_value=row["opportunity_digest"],
                request_digest=request_hash,
                artifact_digest=artifact_hash,
                created_at=row["created_at"],
            )
        ):
            return None
        _parse_timestamp(row["created_at"])
        return ResumeProjection(
            id=row["id"],
            idempotency_key=idempotency_key,
            profile_id=row["profile_id"],
            profile_version=row["profile_version"],
            profile_digest=row["profile_digest"],
            opportunity_id=row["opportunity_id"],
            opportunity_digest=row["opportunity_digest"],
            request=request,
            request_digest=request_hash,
            artifact=artifact,
            artifact_digest=artifact_hash,
            created_at=row["created_at"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _valid_head(row: sqlite3.Row | tuple) -> bool:
    try:
        return bool(
            type(row["current_version"]) is int
            and row["current_version"] >= 1
            and _parse_timestamp(row["updated_at"])
            and row["row_digest"]
            == _head_digest(row["profile_id"], row["current_version"], row["updated_at"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def _profile_row_digest(
    *, profile_id: str, version: int, profile_digest_value: str, created_at: str
) -> str:
    return canonical_digest(
        {
            "schema": "resume-profile-row-v1",
            "profile_id": profile_id,
            "version": version,
            "profile_digest": profile_digest_value,
            "created_at": created_at,
        }
    )


def _head_digest(profile_id: str, current_version: int, updated_at: str) -> str:
    return canonical_digest(
        {
            "schema": "resume-profile-head-v1",
            "profile_id": profile_id,
            "current_version": current_version,
            "updated_at": updated_at,
        }
    )


def _opportunity_row_digest(opportunity_id: str, digest: str, created_at: str) -> str:
    return canonical_digest(
        {
            "schema": "resume-opportunity-row-v1",
            "id": opportunity_id,
            "snapshot_digest": digest,
            "created_at": created_at,
        }
    )


def _projection_row_digest(
    *,
    projection_id: str,
    idempotency_key: str,
    profile_id: str,
    profile_version: int,
    profile_digest_value: str,
    opportunity_id: str,
    opportunity_digest_value: str,
    request_digest: str,
    artifact_digest: str,
    created_at: str,
) -> str:
    return canonical_digest(
        {
            "schema": "resume-projection-row-v1",
            "id": projection_id,
            "idempotency_key": idempotency_key,
            "profile_id": profile_id,
            "profile_version": profile_version,
            "profile_digest": profile_digest_value,
            "opportunity_id": opportunity_id,
            "opportunity_digest": opportunity_digest_value,
            "request_digest": request_digest,
            "artifact_digest": artifact_digest,
            "created_at": created_at,
        }
    )


def _supersession_row_digest(*, previous_id: str, next_id: str, created_at: str) -> str:
    return canonical_digest(
        {
            "schema": "resume-opportunity-supersession-v1",
            "previous_id": previous_id,
            "next_id": next_id,
            "created_at": created_at,
        }
    )


def _timestamp(value: datetime | None) -> str:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("resume rescue timestamp must be timezone-aware")
    return timestamp.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("resume rescue timestamp is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("resume rescue timestamp must be timezone-aware")
    return parsed


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 200:
        raise ValueError("resume rescue limit must be in [1, 200]")


@contextlib.contextmanager
def _atomic(conn: sqlite3.Connection, name: str):
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")
