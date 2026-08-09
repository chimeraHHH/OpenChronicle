"""Durable, lease-fenced proposal sets for supervised résumé rewriting."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, canonical_digest
from . import store as source_store
from .rewrite import (
    ResumeRewriteValidationError,
    rewrite_output_digest,
    validate_rewrite_model_output,
)
from .rewrite_generation import (
    VALID_PROVIDER_LOCATIONS,
    build_rewrite_provider_input,
    rewrite_provider_input_digest,
    validate_rewrite_provider_input,
)

VALID_STATUSES = {"queued", "leased", "ready", "failed"}
VALID_ERRORS = {
    "",
    "provider_failed",
    "invalid_output",
    "input_changed",
    "source_mismatch",
    "unsupported_claim",
    "secret_echo",
    "cancelled",
}


@dataclass(frozen=True, slots=True)
class ResumeRewriteJob:
    id: str
    idempotency_key: str
    status: str
    projection_id: str
    projection_artifact_digest: str
    projection_created_at: str
    provider_input: dict[str, Any]
    provider_input_digest: str
    template_version: int
    template_digest: str
    model_identity: str
    provider_location: str
    remote_egress_authorized: bool
    output: dict[str, Any] | None
    output_digest: str
    error_code: str
    attempt_count: int
    lease_token: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str
    version: int
    row_digest: str

    @property
    def projection_ref(self) -> EvidenceRef:
        return EvidenceRef(
            kind="resume_rescue",
            id=self.projection_id,
            timestamp=self.projection_created_at,
            content_hash=self.projection_artifact_digest,
        )

    @property
    def output_ref(self) -> EvidenceRef:
        if self.status != "ready" or not self.output_digest:
            raise ValueError("resume rewrite output is not ready")
        return EvidenceRef(
            kind="resume_rewrite",
            id=self.id,
            timestamp=self.updated_at,
            content_hash=self.output_digest,
        )


class ResumeRewriteConflict(RuntimeError):
    """A rewrite job changed or a worker lost its lease."""


def create(
    conn: sqlite3.Connection,
    *,
    projection: source_store.ResumeProjection,
    provider_input: dict[str, Any],
    template_version: int,
    template_digest: str,
    model_identity: str,
    provider_location: str,
    remote_egress_authorized: bool,
    now: datetime | None = None,
) -> tuple[ResumeRewriteJob, bool]:
    source_store.ensure_schema(conn)
    normalized_input = validate_rewrite_provider_input(provider_input)
    if normalized_input != build_rewrite_provider_input(projection.artifact):
        raise ValueError("resume rewrite provider input differs from projection")
    _validate_creation_fields(
        template_version=template_version,
        template_digest=template_digest,
        model_identity=model_identity,
        provider_location=provider_location,
        remote_egress_authorized=remote_egress_authorized,
    )
    created = _aware(now or datetime.now(UTC))
    created_at = created.isoformat(timespec="microseconds")
    created_us = _instant_us(created)
    input_digest = rewrite_provider_input_digest(normalized_input)
    idempotency_key = _idempotency_key(
        projection_id=projection.id,
        projection_artifact_digest=projection.artifact_digest,
        provider_input_digest=input_digest,
        template_version=template_version,
        template_digest=template_digest,
        model_identity=model_identity,
        provider_location=provider_location,
        remote_egress_authorized=remote_egress_authorized,
    )
    job_id = f"resume-rewrite-{idempotency_key[:32]}"
    values = {
        "job_id": job_id,
        "idempotency_key": idempotency_key,
        "status": "queued",
        "projection_id": projection.id,
        "projection_artifact_digest": projection.artifact_digest,
        "projection_created_at": projection.created_at,
        "provider_input_digest": input_digest,
        "template_version": template_version,
        "template_digest": template_digest,
        "model_identity": model_identity,
        "provider_location": provider_location,
        "remote_egress_authorized": remote_egress_authorized,
        "output_digest": "",
        "error_code": "",
        "attempt_count": 0,
        "lease_token": None,
        "lease_expires_at": None,
        "created_at": created_at,
        "created_at_us": created_us,
        "updated_at": created_at,
        "version": 1,
    }
    row_digest = _row_digest(**values)
    before = conn.total_changes
    with _atomic(conn, "resume_rewrite_create"):
        conn.execute(
            """
            INSERT OR IGNORE INTO resume_rewrite_jobs(
                id, idempotency_key, status, projection_id,
                projection_artifact_digest, projection_created_at,
                provider_input_json, provider_input_digest, template_version,
                template_digest, model_identity, provider_location,
                remote_egress_authorized, output_json, output_digest, error_code,
                attempt_count, lease_token, lease_expires_at, created_at,
                created_at_us, updated_at, version, row_digest
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '',
                      0, NULL, NULL, ?, ?, ?, 1, ?)
            """,
            (
                job_id,
                idempotency_key,
                projection.id,
                projection.artifact_digest,
                projection.created_at,
                _json(normalized_input),
                input_digest,
                template_version,
                template_digest,
                model_identity,
                provider_location,
                int(remote_egress_authorized),
                created_at,
                created_us,
                created_at,
                row_digest,
            ),
        )
        job = get_by_idempotency_key(conn, idempotency_key)
        if job is None:
            raise RuntimeError("resume rewrite insert did not produce a valid row")
        was_created = conn.total_changes > before
        subject = EvidenceRef(kind="resume_rewrite", id=job.id)
        expected_sources = [projection.ref]
        if was_created:
            provenance_store.replace_sources(conn, subject=subject, sources=expected_sources)
        elif provenance_store.direct_sources_checked(conn, subject) != expected_sources:
            raise RuntimeError("resume rewrite replay provenance differs")
    return job, was_created


def get(conn: sqlite3.Connection, job_id: str) -> ResumeRewriteJob | None:
    row = conn.execute("SELECT * FROM resume_rewrite_jobs WHERE id=?", (job_id,)).fetchone()
    return _to_job(conn, row) if row is not None else None


def get_by_idempotency_key(
    conn: sqlite3.Connection, idempotency_key: str
) -> ResumeRewriteJob | None:
    row = conn.execute(
        "SELECT * FROM resume_rewrite_jobs WHERE idempotency_key=?", (idempotency_key,)
    ).fetchone()
    return _to_job(conn, row) if row is not None else None


def list_jobs(conn: sqlite3.Connection, *, limit: int = 50) -> list[ResumeRewriteJob]:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("resume rewrite limit must be in [1, 200]")
    rows = conn.execute(
        """
        SELECT * FROM resume_rewrite_jobs
         ORDER BY created_at_us DESC, id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [job for row in rows if (job := _to_job(conn, row)) is not None]


def claim_next(
    conn: sqlite3.Connection,
    *,
    lease_token: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> ResumeRewriteJob | None:
    if not isinstance(lease_token, str) or not lease_token or len(lease_token) > 128:
        raise ValueError("resume rewrite lease token is invalid")
    if type(lease_seconds) is not int or not 30 <= lease_seconds <= 21_600:
        raise ValueError("resume rewrite lease must be in [30, 21600]")
    current_time = _aware(now or datetime.now(UTC))
    with _atomic(conn, "resume_rewrite_claim"):
        rows = conn.execute(
            """
            SELECT * FROM resume_rewrite_jobs
             WHERE status='queued'
                OR (status='leased' AND lease_expires_at<=?)
             ORDER BY created_at_us, id
             LIMIT 100
            """,
            (current_time.isoformat(timespec="microseconds"),),
        ).fetchall()
        for row in rows:
            job = _to_job(conn, row)
            if job is None:
                continue
            expires_at = (current_time + timedelta(seconds=lease_seconds)).isoformat(
                timespec="microseconds"
            )
            updated_at = current_time.isoformat(timespec="microseconds")
            next_version = job.version + 1
            digest = _row_for_job(
                job,
                status="leased",
                output_digest=job.output_digest,
                error_code="",
                attempt_count=job.attempt_count + 1,
                lease_token=lease_token,
                lease_expires_at=expires_at,
                updated_at=updated_at,
                version=next_version,
            )
            result = conn.execute(
                """
                UPDATE resume_rewrite_jobs
                   SET status='leased', error_code='', attempt_count=?,
                       lease_token=?, lease_expires_at=?, updated_at=?, version=?,
                       row_digest=?
                 WHERE id=? AND version=? AND status=?
                """,
                (
                    job.attempt_count + 1,
                    lease_token,
                    expires_at,
                    updated_at,
                    next_version,
                    digest,
                    job.id,
                    job.version,
                    job.status,
                ),
            )
            if result.rowcount == 1:
                claimed = get(conn, job.id)
                if claimed is None:
                    raise RuntimeError("claimed resume rewrite row is invalid")
                return claimed
    return None


def complete(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    output: dict[str, Any],
) -> ResumeRewriteJob:
    return _finish(
        conn,
        job_id=job_id,
        lease_token=lease_token,
        status="ready",
        output=output,
        error_code="",
    )


def fail(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    error_code: str,
) -> ResumeRewriteJob:
    if error_code not in VALID_ERRORS - {""}:
        raise ValueError("resume rewrite error code is invalid")
    return _finish(
        conn,
        job_id=job_id,
        lease_token=lease_token,
        status="failed",
        output=None,
        error_code=error_code,
    )


def release_claim(conn: sqlite3.Connection, *, job_id: str, lease_token: str) -> ResumeRewriteJob:
    with _atomic(conn, "resume_rewrite_release"):
        current = get(conn, job_id)
        if current is None or current.status != "leased" or current.lease_token != lease_token:
            raise ResumeRewriteConflict("resume rewrite lease changed")
        return _update(
            conn,
            current,
            status="queued",
            output=None,
            error_code="",
            lease_token=None,
            lease_expires_at=None,
        )


def retry(conn: sqlite3.Connection, *, job_id: str, expected_version: int) -> ResumeRewriteJob:
    with _atomic(conn, "resume_rewrite_retry"):
        current = get(conn, job_id)
        if current is None or current.status != "failed" or current.version != expected_version:
            raise ResumeRewriteConflict("resume rewrite changed")
        return _update(
            conn,
            current,
            status="queued",
            output=None,
            error_code="",
            lease_token=None,
            lease_expires_at=None,
        )


def delete(conn: sqlite3.Connection, *, job_id: str, expected_version: int) -> None:
    with _atomic(conn, "resume_rewrite_delete"):
        dependent = conn.execute(
            "SELECT 1 FROM resume_rewrite_versions WHERE rewrite_job_id=? LIMIT 1",
            (job_id,),
        ).fetchone()
        if dependent is not None:
            raise ResumeRewriteConflict("resume rewrite has reviewed versions")
        row = conn.execute(
            "SELECT version FROM resume_rewrite_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None or type(row["version"]) is not int or row["version"] != expected_version:
            raise ResumeRewriteConflict("resume rewrite changed")
        result = conn.execute(
            "DELETE FROM resume_rewrite_jobs WHERE id=? AND version=?",
            (job_id, expected_version),
        )
        if result.rowcount != 1:
            raise ResumeRewriteConflict("resume rewrite changed")
        conn.execute(
            """
            DELETE FROM provenance_edges
             WHERE subject_kind='resume_rewrite' AND subject_id=? AND subject_path=''
            """,
            (job_id,),
        )


def _finish(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    status: str,
    output: dict[str, Any] | None,
    error_code: str,
) -> ResumeRewriteJob:
    with _atomic(conn, "resume_rewrite_finish"):
        current = get(conn, job_id)
        if current is None or current.status != "leased" or current.lease_token != lease_token:
            raise ResumeRewriteConflict("resume rewrite lease changed")
        return _update(
            conn,
            current,
            status=status,
            output=output,
            error_code=error_code,
            lease_token=None,
            lease_expires_at=None,
        )


def _update(
    conn: sqlite3.Connection,
    current: ResumeRewriteJob,
    *,
    status: str,
    output: dict[str, Any] | None,
    error_code: str,
    lease_token: str | None,
    lease_expires_at: str | None,
) -> ResumeRewriteJob:
    projection = source_store.get_projection(conn, current.projection_id)
    if projection is None or projection.artifact_digest != current.projection_artifact_digest:
        raise ResumeRewriteConflict("resume rewrite source changed")
    if output is not None:
        output = validate_rewrite_model_output(output, artifact=projection.artifact)
    output_json = _json(output) if output is not None else ""
    output_digest = rewrite_output_digest(output) if output is not None else ""
    updated_at = datetime.now(UTC).isoformat(timespec="microseconds")
    next_version = current.version + 1
    digest = _row_for_job(
        current,
        status=status,
        output_digest=output_digest,
        error_code=error_code,
        attempt_count=current.attempt_count,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        updated_at=updated_at,
        version=next_version,
    )
    result = conn.execute(
        """
        UPDATE resume_rewrite_jobs
           SET status=?, output_json=?, output_digest=?, error_code=?,
               lease_token=?, lease_expires_at=?, updated_at=?, version=?,
               row_digest=?
         WHERE id=? AND version=?
        """,
        (
            status,
            output_json,
            output_digest,
            error_code,
            lease_token,
            lease_expires_at,
            updated_at,
            next_version,
            digest,
            current.id,
            current.version,
        ),
    )
    if result.rowcount != 1:
        raise ResumeRewriteConflict("resume rewrite changed")
    updated = get(conn, current.id)
    if updated is None:
        raise RuntimeError("updated resume rewrite row is invalid")
    return updated


def _to_job(conn: sqlite3.Connection, row: sqlite3.Row | tuple) -> ResumeRewriteJob | None:
    try:
        provider_input = validate_rewrite_provider_input(json.loads(row["provider_input_json"]))
        output = json.loads(row["output_json"]) if row["output_json"] else None
        projection = source_store.get_projection(conn, row["projection_id"])
        created = _aware(datetime.fromisoformat(row["created_at"]))
        projection_created = _aware(datetime.fromisoformat(row["projection_created_at"]))
        lease_expires = (
            _aware(datetime.fromisoformat(row["lease_expires_at"]))
            if row["lease_expires_at"] is not None
            else None
        )
        updated = _aware(datetime.fromisoformat(row["updated_at"]))
        if projection is None:
            return None
        normalized_output = (
            validate_rewrite_model_output(output, artifact=projection.artifact)
            if output is not None
            else None
        )
        if (
            row["status"] not in VALID_STATUSES
            or row["provider_location"] not in VALID_PROVIDER_LOCATIONS
            or row["error_code"] not in VALID_ERRORS
            or row["projection_artifact_digest"] != projection.artifact_digest
            or row["projection_created_at"] != projection.created_at
            or provider_input != build_rewrite_provider_input(projection.artifact)
            or row["provider_input_digest"] != rewrite_provider_input_digest(provider_input)
            or not _valid_creation_fields(
                template_version=row["template_version"],
                template_digest=row["template_digest"],
                model_identity=row["model_identity"],
                provider_location=row["provider_location"],
                remote_egress_authorized=row["remote_egress_authorized"],
                stored=True,
            )
            or (row["provider_location"] == "remote_or_unknown")
            and not bool(row["remote_egress_authorized"])
            or type(row["attempt_count"]) is not int
            or row["attempt_count"] < 0
            or type(row["version"]) is not int
            or row["version"] < 1
            or row["created_at_us"] != _instant_us(created)
            or updated < created
            or projection_created.isoformat(timespec="microseconds") != row["projection_created_at"]
            or (lease_expires is not None and lease_expires <= updated)
            or (row["status"] == "leased")
            != (row["lease_token"] is not None and lease_expires is not None)
            or (
                row["lease_token"] is not None
                and (
                    not isinstance(row["lease_token"], str)
                    or not row["lease_token"]
                    or len(row["lease_token"]) > 128
                )
            )
            or (normalized_output is None) != (row["output_digest"] == "")
            or (row["status"] == "ready") != (normalized_output is not None)
            or (row["status"] == "failed") != bool(row["error_code"])
            or (
                normalized_output is not None
                and row["output_digest"] != rewrite_output_digest(normalized_output)
            )
        ):
            return None
        job = ResumeRewriteJob(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            projection_id=row["projection_id"],
            projection_artifact_digest=row["projection_artifact_digest"],
            projection_created_at=row["projection_created_at"],
            provider_input=provider_input,
            provider_input_digest=row["provider_input_digest"],
            template_version=row["template_version"],
            template_digest=row["template_digest"],
            model_identity=row["model_identity"],
            provider_location=row["provider_location"],
            remote_egress_authorized=bool(row["remote_egress_authorized"]),
            output=normalized_output,
            output_digest=row["output_digest"],
            error_code=row["error_code"],
            attempt_count=row["attempt_count"],
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=row["version"],
            row_digest=row["row_digest"],
        )
        expected_idempotency = _idempotency_key(
            projection_id=job.projection_id,
            projection_artifact_digest=job.projection_artifact_digest,
            provider_input_digest=job.provider_input_digest,
            template_version=job.template_version,
            template_digest=job.template_digest,
            model_identity=job.model_identity,
            provider_location=job.provider_location,
            remote_egress_authorized=job.remote_egress_authorized,
        )
        if job.idempotency_key != expected_idempotency or job.id != (
            f"resume-rewrite-{expected_idempotency[:32]}"
        ):
            return None
        expected = _row_for_job(
            job,
            status=job.status,
            output_digest=job.output_digest,
            error_code=job.error_code,
            attempt_count=job.attempt_count,
            lease_token=job.lease_token,
            lease_expires_at=job.lease_expires_at,
            updated_at=job.updated_at,
            version=job.version,
        )
        return job if job.row_digest == expected else None
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        ResumeRewriteValidationError,
    ):
        return None


def _row_for_job(
    job: ResumeRewriteJob,
    *,
    status: str,
    output_digest: str,
    error_code: str,
    attempt_count: int,
    lease_token: str | None,
    lease_expires_at: str | None,
    updated_at: str,
    version: int,
) -> str:
    return _row_digest(
        job_id=job.id,
        idempotency_key=job.idempotency_key,
        status=status,
        projection_id=job.projection_id,
        projection_artifact_digest=job.projection_artifact_digest,
        projection_created_at=job.projection_created_at,
        provider_input_digest=job.provider_input_digest,
        template_version=job.template_version,
        template_digest=job.template_digest,
        model_identity=job.model_identity,
        provider_location=job.provider_location,
        remote_egress_authorized=job.remote_egress_authorized,
        output_digest=output_digest,
        error_code=error_code,
        attempt_count=attempt_count,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        created_at=job.created_at,
        created_at_us=_instant_us(_aware(datetime.fromisoformat(job.created_at))),
        updated_at=updated_at,
        version=version,
    )


def _row_digest(**values: Any) -> str:
    return canonical_digest({"schema": "resume-rewrite-row-v1", **values})


def _validate_creation_fields(**values: Any) -> None:
    if not _valid_creation_fields(**values, stored=False):
        raise ValueError("resume rewrite creation fields are invalid")


def _valid_creation_fields(
    *,
    template_version: object,
    template_digest: object,
    model_identity: object,
    provider_location: object,
    remote_egress_authorized: object,
    stored: bool,
) -> bool:
    authorization_valid = (
        type(remote_egress_authorized) is int and remote_egress_authorized in {0, 1}
        if stored
        else type(remote_egress_authorized) is bool
    )
    return bool(
        type(template_version) is int
        and template_version >= 1
        and _valid_digest(template_digest)
        and isinstance(model_identity, str)
        and bool(model_identity.strip())
        and len(model_identity) <= 256
        and "\x00" not in model_identity
        and provider_location in VALID_PROVIDER_LOCATIONS
        and authorization_valid
        and (provider_location != "remote_or_unknown" or bool(remote_egress_authorized))
    )


def _valid_digest(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _idempotency_key(
    *,
    projection_id: str,
    projection_artifact_digest: str,
    provider_input_digest: str,
    template_version: int,
    template_digest: str,
    model_identity: str,
    provider_location: str,
    remote_egress_authorized: bool,
) -> str:
    return canonical_digest(
        {
            "schema": "resume-rewrite-job-v1",
            "projection_id": projection_id,
            "projection_artifact_digest": projection_artifact_digest,
            "provider_input_digest": provider_input_digest,
            "template_version": template_version,
            "template_digest": template_digest,
            "model_identity": model_identity,
            "provider_location": provider_location,
            "remote_egress_authorized": remote_egress_authorized,
        }
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("resume rewrite timestamp must be timezone-aware")
    return value


def _instant_us(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000)


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
