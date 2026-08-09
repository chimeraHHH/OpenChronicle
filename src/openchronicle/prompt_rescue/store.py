"""Durable, lease-fenced Prompt Rescue input and prepared-artifact jobs."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, canonical_digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_rescue_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'leased', 'ready', 'failed')),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('manual_paste')),
    rough_prompt TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    audience TEXT NOT NULL DEFAULT '',
    constraints_json TEXT NOT NULL DEFAULT '[]',
    desired_format TEXT NOT NULL DEFAULT '',
    source_digest TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    template_version INTEGER NOT NULL,
    template_digest TEXT NOT NULL,
    model_identity TEXT NOT NULL,
    provider_location TEXT NOT NULL
        CHECK (provider_location IN ('local', 'remote_or_unknown')),
    output_json TEXT NOT NULL DEFAULT '',
    output_digest TEXT NOT NULL DEFAULT '',
    output_edited INTEGER NOT NULL DEFAULT 0 CHECK (output_edited IN (0, 1)),
    error_code TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    projection_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prompt_rescue_queue
    ON prompt_rescue_jobs(status, created_at_us, id);
CREATE INDEX IF NOT EXISTS idx_prompt_rescue_recent
    ON prompt_rescue_jobs(created_at_us DESC, id);
"""

VALID_STATUSES = {"queued", "leased", "ready", "failed"}
VALID_ERRORS = {"", "provider_failed", "invalid_output", "input_changed", "cancelled"}


@dataclass(frozen=True, slots=True)
class PromptRescueJob:
    id: str
    idempotency_key: str
    status: str
    source_kind: str
    rough_prompt: str
    target: str
    audience: str
    constraints: tuple[str, ...]
    desired_format: str
    source_digest: str
    policy_digest: str
    template_version: int
    template_digest: str
    model_identity: str
    provider_location: str
    output: dict[str, Any] | None
    output_digest: str
    output_edited: bool
    error_code: str
    attempt_count: int
    lease_token: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str
    version: int
    projection_digest: str

    @property
    def input_ref(self) -> EvidenceRef:
        return EvidenceRef(
            kind="prompt_rescue_input",
            id=self.id,
            timestamp=self.created_at,
            content_hash=self.source_digest,
        )


class PromptRescueConflict(RuntimeError):
    """A reviewed job changed or a worker lost its lease."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def source_digest(
    *,
    source_kind: str,
    rough_prompt: str,
    target: str,
    audience: str,
    constraints: tuple[str, ...],
    desired_format: str,
) -> str:
    return canonical_digest(
        {
            "schema": "prompt-rescue-input-v1",
            "source_kind": source_kind,
            "rough_prompt": rough_prompt,
            "target": target,
            "audience": audience,
            "constraints": list(constraints),
            "desired_format": desired_format,
        }
    )


def create(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    rough_prompt: str,
    target: str,
    audience: str,
    constraints: tuple[str, ...],
    desired_format: str,
    policy_digest: str,
    template_version: int,
    template_digest: str,
    model_identity: str,
    provider_location: str,
    now: datetime | None = None,
) -> tuple[PromptRescueJob, bool]:
    ensure_schema(conn)
    _validate_input(
        source_kind=source_kind,
        rough_prompt=rough_prompt,
        target=target,
        audience=audience,
        constraints=constraints,
        desired_format=desired_format,
        policy_digest=policy_digest,
        template_version=template_version,
        template_digest=template_digest,
        model_identity=model_identity,
        provider_location=provider_location,
    )
    created = _aware(now or datetime.now(UTC))
    created_at = created.isoformat(timespec="microseconds")
    created_us = _instant_us(created)
    source_hash = source_digest(
        source_kind=source_kind,
        rough_prompt=rough_prompt,
        target=target,
        audience=audience,
        constraints=constraints,
        desired_format=desired_format,
    )
    idempotency_key = canonical_digest(
        {
            "schema": "prompt-rescue-job-v1",
            "source_digest": source_hash,
            "policy_digest": policy_digest,
            "template_version": template_version,
            "template_digest": template_digest,
            "model_identity": model_identity,
            "provider_location": provider_location,
        }
    )
    job_id = f"prompt-rescue-{idempotency_key[:32]}"
    projection = _projection_digest(
        job_id=job_id,
        idempotency_key=idempotency_key,
        status="queued",
        source_kind=source_kind,
        source_digest=source_hash,
        policy_digest=policy_digest,
        template_version=template_version,
        template_digest=template_digest,
        model_identity=model_identity,
        provider_location=provider_location,
        output_digest="",
        output_edited=False,
        error_code="",
        attempt_count=0,
        lease_token=None,
        lease_expires_at=None,
        created_at=created_at,
        created_at_us=created_us,
        updated_at=created_at,
        version=1,
    )
    before = conn.total_changes
    with _atomic(conn, "prompt_rescue_create"):
        conn.execute(
            """
            INSERT OR IGNORE INTO prompt_rescue_jobs(
                id, idempotency_key, status, source_kind, rough_prompt,
                target, audience, constraints_json, desired_format,
                source_digest, policy_digest, template_version, template_digest,
                model_identity, provider_location, output_json, output_digest,
                output_edited, error_code, attempt_count, lease_token,
                lease_expires_at, created_at, created_at_us, updated_at,
                version, projection_digest
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '',
                      0, '', 0, NULL, NULL, ?, ?, ?, 1, ?)
            """,
            (
                job_id,
                idempotency_key,
                source_kind,
                rough_prompt,
                target,
                audience,
                json.dumps(list(constraints), ensure_ascii=False, separators=(",", ":")),
                desired_format,
                source_hash,
                policy_digest,
                template_version,
                template_digest,
                model_identity,
                provider_location,
                created_at,
                created_us,
                created_at,
                projection,
            ),
        )
        job = get_by_idempotency_key(conn, idempotency_key)
        if job is None:
            raise RuntimeError("prompt rescue insert did not produce a current row")
        was_created = conn.total_changes > before
        expected_sources = [job.input_ref]
        subject = EvidenceRef(kind="prompt_rescue", id=job.id)
        if was_created:
            provenance_store.replace_sources(
                conn,
                subject=subject,
                sources=expected_sources,
            )
        elif provenance_store.direct_sources_checked(conn, subject) != expected_sources:
            raise RuntimeError("prompt rescue replay provenance differs")
    return job, was_created


def get(conn: sqlite3.Connection, job_id: str) -> PromptRescueJob | None:
    row = conn.execute(
        "SELECT * FROM prompt_rescue_jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    return _to_job(row) if row is not None else None


def get_by_idempotency_key(
    conn: sqlite3.Connection,
    idempotency_key: str,
) -> PromptRescueJob | None:
    row = conn.execute(
        "SELECT * FROM prompt_rescue_jobs WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    return _to_job(row) if row is not None else None


def list_jobs(conn: sqlite3.Connection, *, limit: int = 50) -> list[PromptRescueJob]:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("prompt rescue limit must be in [1, 200]")
    rows = conn.execute(
        """
        SELECT * FROM prompt_rescue_jobs
         ORDER BY created_at_us DESC, id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [job for row in rows if (job := _to_job(row)) is not None]


def claim_next(
    conn: sqlite3.Connection,
    *,
    lease_token: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> PromptRescueJob | None:
    if not isinstance(lease_token, str) or not lease_token or len(lease_token) > 128:
        raise ValueError("prompt rescue lease token is invalid")
    if type(lease_seconds) is not int or not 30 <= lease_seconds <= 21_600:
        raise ValueError("prompt rescue lease must be in [30, 21600]")
    current_time = _aware(now or datetime.now(UTC))
    with _atomic(conn, "prompt_rescue_claim"):
        rows = conn.execute(
            """
            SELECT * FROM prompt_rescue_jobs
             WHERE status='queued'
                OR (status='leased' AND lease_expires_at<=?)
             ORDER BY created_at_us, id
             LIMIT 100
            """,
            (current_time.isoformat(timespec="microseconds"),),
        ).fetchall()
        for raw in rows:
            job = _to_job(raw)
            if job is None:
                continue
            expires_at = (current_time + timedelta(seconds=lease_seconds)).isoformat(
                timespec="microseconds"
            )
            updated_at = current_time.isoformat(timespec="microseconds")
            next_version = job.version + 1
            projection = _projection_for_job(
                job,
                status="leased",
                output_digest=job.output_digest,
                output_edited=job.output_edited,
                error_code="",
                attempt_count=job.attempt_count + 1,
                lease_token=lease_token,
                lease_expires_at=expires_at,
                updated_at=updated_at,
                version=next_version,
            )
            result = conn.execute(
                """
                UPDATE prompt_rescue_jobs
                   SET status='leased', error_code='', attempt_count=?,
                       lease_token=?, lease_expires_at=?, updated_at=?,
                       version=?, projection_digest=?
                 WHERE id=? AND version=? AND status=?
                """,
                (
                    job.attempt_count + 1,
                    lease_token,
                    expires_at,
                    updated_at,
                    next_version,
                    projection,
                    job.id,
                    job.version,
                    job.status,
                ),
            )
            if result.rowcount == 1:
                claimed = get(conn, job.id)
                if claimed is None:
                    raise RuntimeError("claimed prompt rescue projection is invalid")
                return claimed
    return None


def complete(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    output: dict[str, Any],
) -> PromptRescueJob:
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
) -> PromptRescueJob:
    if error_code not in VALID_ERRORS - {""}:
        raise ValueError("prompt rescue error code is invalid")
    return _finish(
        conn,
        job_id=job_id,
        lease_token=lease_token,
        status="failed",
        output=None,
        error_code=error_code,
    )


def retry(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    expected_version: int,
) -> PromptRescueJob:
    return _transition(
        conn,
        job_id=job_id,
        expected_version=expected_version,
        from_status="failed",
        to_status="queued",
        output=None,
        output_edited=False,
        error_code="",
    )


def release_claim(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
) -> PromptRescueJob:
    with _atomic(conn, "prompt_rescue_release"):
        current = get(conn, job_id)
        if current is None or current.status != "leased" or current.lease_token != lease_token:
            raise PromptRescueConflict("prompt rescue lease changed")
        return _update(
            conn,
            current,
            status="queued",
            output=None,
            output_edited=False,
            error_code="",
            lease_token=None,
            lease_expires_at=None,
        )


def edit_output(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    expected_version: int,
    output: dict[str, Any],
) -> PromptRescueJob:
    return _transition(
        conn,
        job_id=job_id,
        expected_version=expected_version,
        from_status="ready",
        to_status="ready",
        output=output,
        output_edited=True,
        error_code="",
    )


def delete(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    expected_version: int,
) -> None:
    with _atomic(conn, "prompt_rescue_delete"):
        row = conn.execute(
            "SELECT version FROM prompt_rescue_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None or type(row["version"]) is not int or row["version"] != expected_version:
            raise PromptRescueConflict("prompt rescue changed")
        result = conn.execute(
            "DELETE FROM prompt_rescue_jobs WHERE id=? AND version=?",
            (job_id, expected_version),
        )
        if result.rowcount != 1:
            raise PromptRescueConflict("prompt rescue changed")
        conn.execute(
            """
            DELETE FROM provenance_edges
             WHERE (subject_kind='prompt_rescue' AND subject_id=? AND subject_path='')
                OR (source_kind='prompt_rescue_input' AND source_id=? AND source_path='')
            """,
            (job_id, job_id),
        )


def _finish(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    status: str,
    output: dict[str, Any] | None,
    error_code: str,
) -> PromptRescueJob:
    with _atomic(conn, "prompt_rescue_finish"):
        current = get(conn, job_id)
        if current is None or current.status != "leased" or current.lease_token != lease_token:
            raise PromptRescueConflict("prompt rescue lease changed")
        return _update(
            conn,
            current,
            status=status,
            output=output,
            output_edited=False,
            error_code=error_code,
            lease_token=None,
            lease_expires_at=None,
        )


def _transition(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    expected_version: int,
    from_status: str,
    to_status: str,
    output: dict[str, Any] | None,
    output_edited: bool,
    error_code: str,
) -> PromptRescueJob:
    with _atomic(conn, "prompt_rescue_transition"):
        current = get(conn, job_id)
        if current is None or current.status != from_status or current.version != expected_version:
            raise PromptRescueConflict("prompt rescue changed")
        return _update(
            conn,
            current,
            status=to_status,
            output=output,
            output_edited=output_edited,
            error_code=error_code,
            lease_token=None,
            lease_expires_at=None,
        )


def _update(
    conn: sqlite3.Connection,
    current: PromptRescueJob,
    *,
    status: str,
    output: dict[str, Any] | None,
    output_edited: bool,
    error_code: str,
    lease_token: str | None,
    lease_expires_at: str | None,
) -> PromptRescueJob:
    output_json = (
        json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if output is not None
        else ""
    )
    output_hash = canonical_digest(output) if output is not None else ""
    updated_at = datetime.now(UTC).isoformat(timespec="microseconds")
    next_version = current.version + 1
    projection = _projection_for_job(
        current,
        status=status,
        output_digest=output_hash,
        output_edited=output_edited,
        error_code=error_code,
        attempt_count=current.attempt_count,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        updated_at=updated_at,
        version=next_version,
    )
    result = conn.execute(
        """
        UPDATE prompt_rescue_jobs
           SET status=?, output_json=?, output_digest=?, output_edited=?,
               error_code=?, lease_token=?, lease_expires_at=?, updated_at=?,
               version=?, projection_digest=?
         WHERE id=? AND version=?
        """,
        (
            status,
            output_json,
            output_hash,
            int(output_edited),
            error_code,
            lease_token,
            lease_expires_at,
            updated_at,
            next_version,
            projection,
            current.id,
            current.version,
        ),
    )
    if result.rowcount != 1:
        raise PromptRescueConflict("prompt rescue changed")
    updated = get(conn, current.id)
    if updated is None:
        raise RuntimeError("updated prompt rescue projection is invalid")
    return updated


def _to_job(row: sqlite3.Row | tuple) -> PromptRescueJob | None:
    try:
        constraints_value = json.loads(row["constraints_json"])
        output_value = json.loads(row["output_json"]) if row["output_json"] else None
        created = _aware(datetime.fromisoformat(row["created_at"]))
        lease_expires = (
            _aware(datetime.fromisoformat(row["lease_expires_at"]))
            if row["lease_expires_at"] is not None
            else None
        )
        if (
            not isinstance(constraints_value, list)
            or not all(isinstance(value, str) for value in constraints_value)
            or (output_value is not None and not isinstance(output_value, dict))
            or row["status"] not in VALID_STATUSES
            or row["source_kind"] != "manual_paste"
            or row["provider_location"] not in {"local", "remote_or_unknown"}
            or row["error_code"] not in VALID_ERRORS
            or type(row["template_version"]) is not int
            or row["template_version"] < 1
            or type(row["attempt_count"]) is not int
            or row["attempt_count"] < 0
            or type(row["version"]) is not int
            or row["version"] < 1
            or type(row["output_edited"]) is not int
            or row["output_edited"] not in {0, 1}
            or row["created_at_us"] != _instant_us(created)
            or (row["status"] == "leased")
            != (row["lease_token"] is not None and lease_expires is not None)
            or (output_value is None) != (row["output_digest"] == "")
            or (row["status"] == "ready") != (output_value is not None)
            or (row["status"] == "failed") != bool(row["error_code"])
            or (output_value is not None and row["output_digest"] != canonical_digest(output_value))
        ):
            return None
        constraints = tuple(constraints_value)
        expected_source = source_digest(
            source_kind=row["source_kind"],
            rough_prompt=row["rough_prompt"],
            target=row["target"],
            audience=row["audience"],
            constraints=constraints,
            desired_format=row["desired_format"],
        )
        if row["source_digest"] != expected_source:
            return None
        job = PromptRescueJob(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            source_kind=row["source_kind"],
            rough_prompt=row["rough_prompt"],
            target=row["target"],
            audience=row["audience"],
            constraints=constraints,
            desired_format=row["desired_format"],
            source_digest=row["source_digest"],
            policy_digest=row["policy_digest"],
            template_version=row["template_version"],
            template_digest=row["template_digest"],
            model_identity=row["model_identity"],
            provider_location=row["provider_location"],
            output=output_value,
            output_digest=row["output_digest"],
            output_edited=bool(row["output_edited"]),
            error_code=row["error_code"],
            attempt_count=row["attempt_count"],
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=row["version"],
            projection_digest=row["projection_digest"],
        )
        expected_projection = _projection_for_job(
            job,
            status=job.status,
            output_digest=job.output_digest,
            output_edited=job.output_edited,
            error_code=job.error_code,
            attempt_count=job.attempt_count,
            lease_token=job.lease_token,
            lease_expires_at=job.lease_expires_at,
            updated_at=job.updated_at,
            version=job.version,
        )
        return job if job.projection_digest == expected_projection else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _projection_for_job(
    job: PromptRescueJob,
    *,
    status: str,
    output_digest: str,
    output_edited: bool,
    error_code: str,
    attempt_count: int,
    lease_token: str | None,
    lease_expires_at: str | None,
    updated_at: str,
    version: int,
) -> str:
    return _projection_digest(
        job_id=job.id,
        idempotency_key=job.idempotency_key,
        status=status,
        source_kind=job.source_kind,
        source_digest=job.source_digest,
        policy_digest=job.policy_digest,
        template_version=job.template_version,
        template_digest=job.template_digest,
        model_identity=job.model_identity,
        provider_location=job.provider_location,
        output_digest=output_digest,
        output_edited=output_edited,
        error_code=error_code,
        attempt_count=attempt_count,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        created_at=job.created_at,
        created_at_us=_instant_us(_aware(datetime.fromisoformat(job.created_at))),
        updated_at=updated_at,
        version=version,
    )


def _projection_digest(
    *,
    job_id: str,
    idempotency_key: str,
    status: str,
    source_kind: str,
    source_digest: str,
    policy_digest: str,
    template_version: int,
    template_digest: str,
    model_identity: str,
    provider_location: str,
    output_digest: str,
    output_edited: bool,
    error_code: str,
    attempt_count: int,
    lease_token: str | None,
    lease_expires_at: str | None,
    created_at: str,
    created_at_us: int,
    updated_at: str,
    version: int,
) -> str:
    return canonical_digest(
        {
            "schema": "prompt-rescue-projection-v1",
            "id": job_id,
            "idempotency_key": idempotency_key,
            "status": status,
            "source_kind": source_kind,
            "source_digest": source_digest,
            "policy_digest": policy_digest,
            "template_version": template_version,
            "template_digest": template_digest,
            "model_identity": model_identity,
            "provider_location": provider_location,
            "output_digest": output_digest,
            "output_edited": output_edited,
            "error_code": error_code,
            "attempt_count": attempt_count,
            "lease_token": lease_token,
            "lease_expires_at": lease_expires_at,
            "created_at": created_at,
            "created_at_us": created_at_us,
            "updated_at": updated_at,
            "version": version,
        }
    )


def _validate_input(**values: Any) -> None:
    strings = (
        "rough_prompt",
        "target",
        "audience",
        "desired_format",
        "policy_digest",
        "template_digest",
        "model_identity",
    )
    if any(not isinstance(values[name], str) or "\x00" in values[name] for name in strings):
        raise ValueError("prompt rescue input is invalid")
    constraints = values["constraints"]
    if (
        values["source_kind"] != "manual_paste"
        or not values["rough_prompt"].strip()
        or not values["policy_digest"]
        or type(values["template_version"]) is not int
        or values["template_version"] < 1
        or not values["template_digest"]
        or not values["model_identity"]
        or values["provider_location"] not in {"local", "remote_or_unknown"}
        or not isinstance(constraints, tuple)
        or not all(isinstance(value, str) and "\x00" not in value for value in constraints)
    ):
        raise ValueError("prompt rescue input is invalid")


@contextlib.contextmanager
def _atomic(conn: sqlite3.Connection, name: str):
    if conn.in_transaction:
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
            conn.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("prompt rescue timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _instant_us(value: datetime) -> int:
    instant = _aware(value)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = instant - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
