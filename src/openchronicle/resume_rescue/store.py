"""Immutable profile versions and opportunity snapshots for Résumé Rescue."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..provenance.models import EvidenceRef, canonical_digest
from .models import opportunity_digest, profile_digest, validate_opportunity, validate_profile

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
        current = get_opportunity(conn, opportunity_id)
        if current is None or current.digest != digest:
            raise RuntimeError("resume opportunity insert did not produce a valid snapshot")
        return current, conn.total_changes > before


def get_opportunity(conn: sqlite3.Connection, opportunity_id: str) -> OpportunitySnapshot | None:
    row = conn.execute(
        "SELECT * FROM resume_opportunities WHERE id=?", (opportunity_id,)
    ).fetchone()
    return _to_opportunity(row) if row is not None else None


def list_opportunities(conn: sqlite3.Connection, *, limit: int = 50) -> list[OpportunitySnapshot]:
    _limit(limit)
    rows = conn.execute(
        "SELECT * FROM resume_opportunities ORDER BY created_at DESC, id LIMIT ?", (limit,)
    ).fetchall()
    return [item for row in rows if (item := _to_opportunity(row)) is not None]


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
