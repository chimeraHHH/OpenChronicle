"""Immutable, content-bound positive-adoption signals for prepared artifacts."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..provenance.models import EvidenceRef, canonical_digest

ARTIFACT_KINDS = frozenset({"prompt_rescue", "reply_rescue"})
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifact_adoptions (
    id TEXT PRIMARY KEY,
    artifact_kind TEXT NOT NULL
        CHECK (artifact_kind IN ('prompt_rescue', 'reply_rescue')),
    artifact_id TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    artifact_version INTEGER NOT NULL CHECK (artifact_version >= 1),
    artifact_json TEXT NOT NULL,
    output_edited INTEGER NOT NULL CHECK (output_edited IN (0, 1)),
    adopted_at TEXT NOT NULL,
    projection_digest TEXT NOT NULL,
    UNIQUE (artifact_kind, artifact_id, artifact_digest)
);
CREATE INDEX IF NOT EXISTS idx_artifact_adoptions_artifact
    ON artifact_adoptions(artifact_kind, artifact_id, adopted_at DESC, id);
"""


@dataclass(frozen=True, slots=True)
class ArtifactAdoption:
    id: str
    artifact_kind: str
    artifact_id: str
    artifact_digest: str
    artifact_version: int
    artifact: dict[str, Any]
    output_edited: bool
    adopted_at: str
    projection_digest: str


def ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def record(
    conn: sqlite3.Connection,
    *,
    artifact_kind: str,
    artifact_id: str,
    artifact_digest: str,
    artifact_version: int,
    artifact: dict[str, Any],
    output_edited: bool,
    adopted_at: datetime | None = None,
) -> tuple[ArtifactAdoption, bool]:
    ensure_schema(conn)
    clean_kind = artifact_kind.strip()
    clean_id = artifact_id.strip()
    if clean_kind not in ARTIFACT_KINDS or not clean_id or "\x00" in clean_id:
        raise ValueError("artifact adoption identity is invalid")
    if not _DIGEST_RE.fullmatch(artifact_digest):
        raise ValueError("artifact adoption digest is invalid")
    if type(artifact_version) is not int or artifact_version < 1:
        raise ValueError("artifact adoption version is invalid")
    if not isinstance(artifact, dict) or canonical_digest(artifact) != artifact_digest:
        raise ValueError("artifact adoption content does not match its digest")
    if type(output_edited) is not bool:
        raise ValueError("artifact adoption edited state is invalid")
    adopted = _aware(adopted_at or datetime.now(UTC)).isoformat(timespec="microseconds")
    adoption_id = "aa-" + canonical_digest(
        {
            "schema": "artifact-adoption-identity-v1",
            "artifact_kind": clean_kind,
            "artifact_id": clean_id,
            "artifact_digest": artifact_digest,
        }
    )[:32]
    artifact_json = json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    projection = _projection_digest(
        adoption_id=adoption_id,
        artifact_kind=clean_kind,
        artifact_id=clean_id,
        artifact_digest=artifact_digest,
        artifact_version=artifact_version,
        artifact=artifact,
        output_edited=output_edited,
        adopted_at=adopted,
    )
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO artifact_adoptions(
            id, artifact_kind, artifact_id, artifact_digest,
            artifact_version, artifact_json, output_edited,
            adopted_at, projection_digest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            adoption_id,
            clean_kind,
            clean_id,
            artifact_digest,
            artifact_version,
            artifact_json,
            int(output_edited),
            adopted,
            projection,
        ),
    )
    created = conn.total_changes > before
    current = get(conn, adoption_id)
    if current is None:
        raise RuntimeError("artifact adoption replay is invalid")
    if not created and (
        current.artifact_kind != clean_kind
        or current.artifact_id != clean_id
        or current.artifact_digest != artifact_digest
        or current.artifact != artifact
    ):
        raise RuntimeError("artifact adoption replay changed content")
    return current, created


def get(conn: sqlite3.Connection, adoption_id: str) -> ArtifactAdoption | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM artifact_adoptions WHERE id=?",
        (adoption_id,),
    ).fetchone()
    return _to_adoption(row) if row is not None else None


def list_for_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_kind: str,
    artifact_id: str,
) -> list[ArtifactAdoption]:
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM artifact_adoptions
         WHERE artifact_kind=? AND artifact_id=?
         ORDER BY adopted_at DESC, id
        """,
        (artifact_kind, artifact_id),
    ).fetchall()
    return [value for row in rows if (value := _to_adoption(row)) is not None]


def list_recent(conn: sqlite3.Connection, *, limit: int = 50) -> list[ArtifactAdoption]:
    ensure_schema(conn)
    if type(limit) is not int or not 1 <= limit <= 1_000:
        raise ValueError("artifact adoption limit must be between 1 and 1000")
    rows = conn.execute(
        """
        SELECT * FROM artifact_adoptions
         ORDER BY adopted_at DESC, id
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [value for row in rows if (value := _to_adoption(row)) is not None]


def evidence_ref(adoption: ArtifactAdoption) -> EvidenceRef:
    return EvidenceRef(
        kind="artifact_adoption",
        id=adoption.id,
        timestamp=adoption.adopted_at,
        content_hash=adoption.projection_digest,
    )


def delete_for_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_kind: str,
    artifact_id: str,
) -> None:
    ensure_schema(conn)
    conn.execute(
        "DELETE FROM artifact_adoptions WHERE artifact_kind=? AND artifact_id=?",
        (artifact_kind, artifact_id),
    )


def _to_adoption(row: sqlite3.Row | tuple) -> ArtifactAdoption | None:
    try:
        artifact = json.loads(row["artifact_json"])
        adopted_at = _aware(datetime.fromisoformat(row["adopted_at"])).isoformat(
            timespec="microseconds"
        )
        if (
            row["artifact_kind"] not in ARTIFACT_KINDS
            or not isinstance(row["artifact_id"], str)
            or not row["artifact_id"]
            or not _DIGEST_RE.fullmatch(row["artifact_digest"])
            or type(row["artifact_version"]) is not int
            or row["artifact_version"] < 1
            or not isinstance(artifact, dict)
            or canonical_digest(artifact) != row["artifact_digest"]
            or type(row["output_edited"]) is not int
            or row["output_edited"] not in {0, 1}
        ):
            return None
        expected_id = "aa-" + canonical_digest(
            {
                "schema": "artifact-adoption-identity-v1",
                "artifact_kind": row["artifact_kind"],
                "artifact_id": row["artifact_id"],
                "artifact_digest": row["artifact_digest"],
            }
        )[:32]
        if row["id"] != expected_id:
            return None
        expected_projection = _projection_digest(
            adoption_id=row["id"],
            artifact_kind=row["artifact_kind"],
            artifact_id=row["artifact_id"],
            artifact_digest=row["artifact_digest"],
            artifact_version=row["artifact_version"],
            artifact=artifact,
            output_edited=bool(row["output_edited"]),
            adopted_at=adopted_at,
        )
        if row["projection_digest"] != expected_projection:
            return None
        return ArtifactAdoption(
            id=row["id"],
            artifact_kind=row["artifact_kind"],
            artifact_id=row["artifact_id"],
            artifact_digest=row["artifact_digest"],
            artifact_version=row["artifact_version"],
            artifact=artifact,
            output_edited=bool(row["output_edited"]),
            adopted_at=adopted_at,
            projection_digest=row["projection_digest"],
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _projection_digest(
    *,
    adoption_id: str,
    artifact_kind: str,
    artifact_id: str,
    artifact_digest: str,
    artifact_version: int,
    artifact: dict[str, Any],
    output_edited: bool,
    adopted_at: str,
) -> str:
    return canonical_digest(
        {
            "schema": "artifact-adoption-projection-v1",
            "id": adoption_id,
            "artifact_kind": artifact_kind,
            "artifact_id": artifact_id,
            "artifact_digest": artifact_digest,
            "artifact_version": artifact_version,
            "artifact": artifact,
            "output_edited": output_edited,
            "adopted_at": adopted_at,
        }
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("artifact adoption time must be timezone-aware")
    return value.astimezone(UTC)
