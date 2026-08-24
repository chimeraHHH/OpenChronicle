"""Immutable, proposal-level review versions for supervised résumé rewrites."""

from __future__ import annotations

import contextlib
import copy
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, canonical_digest
from . import rewrite_store
from . import store as source_store
from .models import ResumeSchemaError, validate_artifact
from .rewrite import rewrite_proposal_digest

VALID_DECISIONS = {"accepted", "rejected"}


@dataclass(frozen=True, slots=True)
class ResumeRewriteVersion:
    id: str
    lineage_id: str
    version: int
    parent_id: str
    action: str
    proposal_id: str
    proposal_digest: str
    restore_target_id: str
    decision: str
    base_projection_id: str
    base_artifact_digest: str
    rewrite_job_id: str
    rewrite_output_digest: str
    decisions: list[dict[str, str]]
    artifact: dict[str, Any]
    artifact_digest: str
    created_at: str
    row_digest: str

    @property
    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            kind="resume_rewrite_version",
            id=self.id,
            timestamp=self.created_at,
            content_hash=self.artifact_digest,
        )

    @property
    def profile_id(self) -> str:
        return str(self.artifact["profile_binding"]["id"])

    @property
    def profile_version(self) -> int:
        return int(self.artifact["profile_binding"]["version"])

    @property
    def profile_digest(self) -> str:
        return str(self.artifact["profile_binding"]["digest"])

    @property
    def opportunity_id(self) -> str:
        return str(self.artifact["opportunity_binding"]["id"])

    @property
    def opportunity_digest(self) -> str:
        return str(self.artifact["opportunity_binding"]["digest"])


class ResumeRewriteReviewConflict(RuntimeError):
    """A proposal, source, or reviewed projection head changed."""


def decide(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    proposal_id: str,
    expected_proposal_digest: str,
    expected_job_version: int,
    expected_head_id: str,
    expected_artifact_digest: str,
    decision: str,
    now: datetime | None = None,
) -> tuple[ResumeRewriteVersion, bool]:
    """Accept or reject exactly one proposal against the current immutable head."""

    source_store.ensure_schema(conn)
    if decision not in VALID_DECISIONS:
        raise ValueError("resume rewrite decision is invalid")
    if type(expected_job_version) is not int or expected_job_version < 1:
        raise ValueError("resume rewrite expected job version is invalid")
    _require_digest(expected_proposal_digest)
    _require_digest(expected_artifact_digest)
    if not isinstance(expected_head_id, str) or len(expected_head_id) > 128:
        raise ValueError("resume rewrite expected head is invalid")
    job, base, proposal = _ready_sources(conn, job_id, proposal_id)
    proposal_hash = rewrite_proposal_digest(proposal)
    if job.version != expected_job_version or proposal_hash != expected_proposal_digest:
        raise ResumeRewriteReviewConflict("resume rewrite proposal changed")
    lineage_id = _lineage_id(job)
    created = _aware(now or datetime.now(UTC))
    with _atomic(conn, "resume_rewrite_decide"):
        current = _head_checked(conn, lineage_id)
        _require_expected_head(
            current,
            base=base,
            expected_head_id=expected_head_id,
            expected_artifact_digest=expected_artifact_digest,
        )
        previous_decisions = current.decisions if current is not None else []
        by_proposal = {item["proposal_id"]: dict(item) for item in previous_decisions}
        existing = by_proposal.get(proposal_id)
        if existing is not None and existing["status"] == decision:
            return current, False  # type: ignore[return-value]
        by_proposal[proposal_id] = {
            "proposal_id": proposal_id,
            "proposal_digest": proposal_hash,
            "fact_id": proposal["fact_id"],
            "status": decision,
        }
        decisions = _ordered_decisions(job, by_proposal)
        version = 1 if current is None else current.version + 1
        artifact = _build_artifact(base, job, decisions=decisions, version=version)
        return _insert_version(
            conn,
            lineage_id=lineage_id,
            version=version,
            parent_id=current.id if current is not None else "",
            action="decision",
            proposal_id=proposal_id,
            proposal_digest=proposal_hash,
            restore_target_id="",
            decision=decision,
            base=base,
            job=job,
            decisions=decisions,
            artifact=artifact,
            created=created,
            expected=current,
        ), True


def restore(
    conn: sqlite3.Connection,
    *,
    target_version_id: str,
    expected_head_id: str,
    expected_artifact_digest: str,
    now: datetime | None = None,
) -> ResumeRewriteVersion:
    """Make an earlier decision ledger current as a new immutable version."""

    source_store.ensure_schema(conn)
    _require_digest(expected_artifact_digest)
    if not all(
        isinstance(value, str) and value and len(value) <= 128
        for value in (target_version_id, expected_head_id)
    ):
        raise ValueError("resume rewrite restore binding is invalid")
    created = _aware(now or datetime.now(UTC))
    with _atomic(conn, "resume_rewrite_restore"):
        target = get(conn, target_version_id)
        if target is None:
            raise ResumeRewriteReviewConflict("resume rewrite restore target changed")
        current = _head_checked(conn, target.lineage_id)
        if (
            current is None
            or current.id != expected_head_id
            or current.artifact_digest != expected_artifact_digest
        ):
            raise ResumeRewriteReviewConflict("resume rewrite head changed")
        job, base, _proposal = _ready_sources(conn, target.rewrite_job_id, None)
        if (
            target.base_projection_id != base.id
            or target.base_artifact_digest != base.artifact_digest
            or target.rewrite_output_digest != job.output_digest
        ):
            raise ResumeRewriteReviewConflict("resume rewrite restore source changed")
        version = current.version + 1
        decisions = copy.deepcopy(target.decisions)
        artifact = _build_artifact(base, job, decisions=decisions, version=version)
        return _insert_version(
            conn,
            lineage_id=target.lineage_id,
            version=version,
            parent_id=current.id,
            action="restore",
            proposal_id="",
            proposal_digest=target.artifact_digest,
            restore_target_id=target.id,
            decision="restored",
            base=base,
            job=job,
            decisions=decisions,
            artifact=artifact,
            created=created,
            expected=current,
        )


def get(conn: sqlite3.Connection, version_id: str) -> ResumeRewriteVersion | None:
    row = conn.execute("SELECT * FROM resume_rewrite_versions WHERE id=?", (version_id,)).fetchone()
    return _to_version(conn, row) if row is not None else None


def get_head(conn: sqlite3.Connection, job_id: str) -> ResumeRewriteVersion | None:
    job = rewrite_store.get(conn, job_id)
    if job is None or job.status != "ready":
        return None
    try:
        return _head_checked(conn, _lineage_id(job))
    except ResumeRewriteReviewConflict:
        return None


def list_versions(
    conn: sqlite3.Connection, *, job_id: str, limit: int = 50
) -> list[ResumeRewriteVersion]:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("resume rewrite version limit must be in [1, 200]")
    job = rewrite_store.get(conn, job_id)
    if job is None or job.status != "ready":
        return []
    rows = conn.execute(
        """
        SELECT * FROM resume_rewrite_versions
         WHERE lineage_id=? ORDER BY version DESC LIMIT ?
        """,
        (_lineage_id(job), limit),
    ).fetchall()
    return [item for row in rows if (item := _to_version(conn, row)) is not None]


def _insert_version(
    conn: sqlite3.Connection,
    *,
    lineage_id: str,
    version: int,
    parent_id: str,
    action: str,
    proposal_id: str,
    proposal_digest: str,
    restore_target_id: str,
    decision: str,
    base: source_store.ResumeProjection,
    job: rewrite_store.ResumeRewriteJob,
    decisions: list[dict[str, str]],
    artifact: dict[str, Any],
    created: datetime,
    expected: ResumeRewriteVersion | None,
) -> ResumeRewriteVersion:
    version_id = f"{lineage_id}:v{version}"
    created_at = created.isoformat(timespec="microseconds")
    artifact_hash = canonical_digest({"schema": "resume-artifact-v1", "artifact": artifact})
    values = {
        "version_id": version_id,
        "lineage_id": lineage_id,
        "version": version,
        "parent_id": parent_id,
        "action": action,
        "proposal_id": proposal_id,
        "proposal_digest": proposal_digest,
        "restore_target_id": restore_target_id,
        "decision": decision,
        "base_projection_id": base.id,
        "base_artifact_digest": base.artifact_digest,
        "rewrite_job_id": job.id,
        "rewrite_output_digest": job.output_digest,
        "decisions_digest": _decisions_digest(decisions),
        "artifact_digest": artifact_hash,
        "created_at": created_at,
        "created_at_us": _instant_us(created),
    }
    row_hash = canonical_digest({"schema": "resume-rewrite-version-row-v1", **values})
    try:
        conn.execute(
            """
            INSERT INTO resume_rewrite_versions(
                id, lineage_id, version, parent_id, action, proposal_id,
                proposal_digest, restore_target_id, decision, base_projection_id,
                base_artifact_digest, rewrite_job_id, rewrite_output_digest,
                decisions_json, artifact_json, artifact_digest, created_at,
                created_at_us, row_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                lineage_id,
                version,
                parent_id,
                action,
                proposal_id,
                proposal_digest,
                restore_target_id,
                decision,
                base.id,
                base.artifact_digest,
                job.id,
                job.output_digest,
                _json(decisions),
                _json(artifact),
                artifact_hash,
                created_at,
                _instant_us(created),
                row_hash,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ResumeRewriteReviewConflict("resume rewrite head changed") from exc
    subject = EvidenceRef(kind="resume_rewrite_version", id=version_id)
    provenance_store.replace_sources(conn, subject=subject, sources=[base.ref, job.output_ref])
    _advance_head(
        conn,
        lineage_id=lineage_id,
        version_id=version_id,
        version=version,
        artifact_digest=artifact_hash,
        updated_at=created_at,
        expected=expected,
    )
    created_version = get(conn, version_id)
    if created_version is None:
        raise RuntimeError("resume rewrite version insert produced an invalid row")
    return created_version


def _advance_head(
    conn: sqlite3.Connection,
    *,
    lineage_id: str,
    version_id: str,
    version: int,
    artifact_digest: str,
    updated_at: str,
    expected: ResumeRewriteVersion | None,
) -> None:
    digest = _head_digest(
        lineage_id=lineage_id,
        current_id=version_id,
        current_version=version,
        current_artifact_digest=artifact_digest,
        updated_at=updated_at,
    )
    if expected is None:
        try:
            conn.execute(
                """
                INSERT INTO resume_rewrite_heads(
                    lineage_id, current_id, current_version,
                    current_artifact_digest, updated_at, row_digest
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (lineage_id, version_id, version, artifact_digest, updated_at, digest),
            )
        except sqlite3.IntegrityError as exc:
            raise ResumeRewriteReviewConflict("resume rewrite head changed") from exc
        return
    result = conn.execute(
        """
        UPDATE resume_rewrite_heads
           SET current_id=?, current_version=?, current_artifact_digest=?,
               updated_at=?, row_digest=?
         WHERE lineage_id=? AND current_id=? AND current_version=?
           AND current_artifact_digest=?
        """,
        (
            version_id,
            version,
            artifact_digest,
            updated_at,
            digest,
            lineage_id,
            expected.id,
            expected.version,
            expected.artifact_digest,
        ),
    )
    if result.rowcount != 1:
        raise ResumeRewriteReviewConflict("resume rewrite head changed")


def _head_checked(conn: sqlite3.Connection, lineage_id: str) -> ResumeRewriteVersion | None:
    row = conn.execute(
        "SELECT * FROM resume_rewrite_heads WHERE lineage_id=?", (lineage_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        _aware(datetime.fromisoformat(row["updated_at"]))
        expected = _head_digest(
            lineage_id=row["lineage_id"],
            current_id=row["current_id"],
            current_version=row["current_version"],
            current_artifact_digest=row["current_artifact_digest"],
            updated_at=row["updated_at"],
        )
        current = get(conn, row["current_id"])
        if (
            row["lineage_id"] != lineage_id
            or row["row_digest"] != expected
            or current is None
            or current.lineage_id != lineage_id
            or current.version != row["current_version"]
            or current.artifact_digest != row["current_artifact_digest"]
        ):
            raise ResumeRewriteReviewConflict("resume rewrite head changed")
        return current
    except (KeyError, TypeError, ValueError):
        raise ResumeRewriteReviewConflict("resume rewrite head changed") from None


def _to_version(conn: sqlite3.Connection, row: sqlite3.Row | tuple) -> ResumeRewriteVersion | None:
    try:
        decisions = json.loads(row["decisions_json"])
        artifact = validate_artifact(json.loads(row["artifact_json"]))
        created = _aware(datetime.fromisoformat(row["created_at"]))
        base = source_store.get_projection(conn, row["base_projection_id"])
        job = rewrite_store.get(conn, row["rewrite_job_id"])
        if base is None or job is None or job.status != "ready":
            return None
        normalized_decisions = artifact["rewrite_binding"]["decisions"]
        values = {
            "version_id": row["id"],
            "lineage_id": row["lineage_id"],
            "version": row["version"],
            "parent_id": row["parent_id"],
            "action": row["action"],
            "proposal_id": row["proposal_id"],
            "proposal_digest": row["proposal_digest"],
            "restore_target_id": row["restore_target_id"],
            "decision": row["decision"],
            "base_projection_id": row["base_projection_id"],
            "base_artifact_digest": row["base_artifact_digest"],
            "rewrite_job_id": row["rewrite_job_id"],
            "rewrite_output_digest": row["rewrite_output_digest"],
            "decisions_digest": _decisions_digest(normalized_decisions),
            "artifact_digest": row["artifact_digest"],
            "created_at": row["created_at"],
            "created_at_us": row["created_at_us"],
        }
        if (
            type(decisions) is not list
            or decisions != normalized_decisions
            or type(row["version"]) is not int
            or row["version"] < 1
            or row["id"] != f"{row['lineage_id']}:v{row['version']}"
            or row["lineage_id"] != _lineage_id(job)
            or row["base_projection_id"] != base.id
            or row["base_artifact_digest"] != base.artifact_digest
            or row["rewrite_output_digest"] != job.output_digest
            or row["artifact_digest"]
            != canonical_digest({"schema": "resume-artifact-v1", "artifact": artifact})
            or row["created_at_us"] != _instant_us(created)
            or row["row_digest"]
            != canonical_digest({"schema": "resume-rewrite-version-row-v1", **values})
            or artifact
            != _build_artifact(base, job, decisions=normalized_decisions, version=row["version"])
            or (
                row["action"] == "decision"
                and (
                    row["decision"] not in VALID_DECISIONS
                    or not row["proposal_id"]
                    or row["restore_target_id"]
                    or not _decision_action_matches(row, normalized_decisions)
                )
            )
            or (
                row["action"] == "restore"
                and (
                    row["decision"] != "restored"
                    or row["proposal_id"]
                    or not _restore_action_matches(conn, row, normalized_decisions)
                )
            )
            or row["action"] not in {"decision", "restore"}
            or not _parent_binding_exists(conn, row)
        ):
            return None
        version = ResumeRewriteVersion(
            id=row["id"],
            lineage_id=row["lineage_id"],
            version=row["version"],
            parent_id=row["parent_id"],
            action=row["action"],
            proposal_id=row["proposal_id"],
            proposal_digest=row["proposal_digest"],
            restore_target_id=row["restore_target_id"],
            decision=row["decision"],
            base_projection_id=row["base_projection_id"],
            base_artifact_digest=row["base_artifact_digest"],
            rewrite_job_id=row["rewrite_job_id"],
            rewrite_output_digest=row["rewrite_output_digest"],
            decisions=normalized_decisions,
            artifact=artifact,
            artifact_digest=row["artifact_digest"],
            created_at=row["created_at"],
            row_digest=row["row_digest"],
        )
        sources = provenance_store.direct_sources_checked(
            conn, EvidenceRef(kind="resume_rewrite_version", id=version.id)
        )
        return version if sources == [base.ref, job.output_ref] else None
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        ResumeSchemaError,
    ):
        return None


def _build_artifact(
    base: source_store.ResumeProjection,
    job: rewrite_store.ResumeRewriteJob,
    *,
    decisions: list[dict[str, str]],
    version: int,
) -> dict[str, Any]:
    if job.output is None:
        raise ResumeRewriteReviewConflict("resume rewrite output changed")
    by_id = {item["proposal_id"]: item for item in job.output["proposals"]}
    accepted_by_fact: dict[str, dict[str, Any]] = {}
    for item in decisions:
        proposal = by_id.get(item["proposal_id"])
        if (
            proposal is None
            or rewrite_proposal_digest(proposal) != item["proposal_digest"]
            or proposal["fact_id"] != item["fact_id"]
        ):
            raise ResumeRewriteReviewConflict("resume rewrite decision changed")
        if item["status"] == "accepted":
            accepted_by_fact[item["fact_id"]] = proposal
    artifact = copy.deepcopy(base.artifact)
    artifact["generation_mode"] = "supervised_rewrite_projection"
    artifact["rewrite_binding"] = {
        "base_projection_id": base.id,
        "base_artifact_digest": base.artifact_digest,
        "rewrite_job_id": job.id,
        "rewrite_output_digest": job.output_digest,
        "decision_version": version,
        "decisions": copy.deepcopy(decisions),
    }
    for section in artifact["sections"]:
        for item in section["items"]:
            proposal = accepted_by_fact.get(item["fact_id"])
            if proposal is None:
                item["transformation"] = "selected_exact"
            else:
                item["text"] = proposal["proposed_text"]
                item["transformation"] = "accepted_model_rewrite"
    return validate_artifact(artifact)


def _ready_sources(
    conn: sqlite3.Connection, job_id: str, proposal_id: str | None
) -> tuple[
    rewrite_store.ResumeRewriteJob,
    source_store.ResumeProjection,
    dict[str, Any] | None,
]:
    job = rewrite_store.get(conn, job_id)
    if job is None or job.status != "ready" or job.output is None:
        raise ResumeRewriteReviewConflict("resume rewrite output changed")
    base = source_store.get_projection(conn, job.projection_id)
    if base is None or base.artifact_digest != job.projection_artifact_digest:
        raise ResumeRewriteReviewConflict("resume rewrite source changed")
    if proposal_id is None:
        return job, base, None
    proposal = next(
        (item for item in job.output["proposals"] if item["proposal_id"] == proposal_id),
        None,
    )
    if proposal is None:
        raise ResumeRewriteReviewConflict("resume rewrite proposal changed")
    return job, base, proposal


def _require_expected_head(
    current: ResumeRewriteVersion | None,
    *,
    base: source_store.ResumeProjection,
    expected_head_id: str,
    expected_artifact_digest: str,
) -> None:
    if current is None:
        if expected_head_id or expected_artifact_digest != base.artifact_digest:
            raise ResumeRewriteReviewConflict("resume rewrite head changed")
        return
    if (
        current.id != expected_head_id
        or current.artifact_digest != expected_artifact_digest
        or current.base_projection_id != base.id
        or current.base_artifact_digest != base.artifact_digest
    ):
        raise ResumeRewriteReviewConflict("resume rewrite head changed")


def _decision_action_matches(row: sqlite3.Row | tuple, decisions: list[dict[str, str]]) -> bool:
    return any(
        item["proposal_id"] == row["proposal_id"]
        and item["proposal_digest"] == row["proposal_digest"]
        and item["status"] == row["decision"]
        for item in decisions
    )


def _restore_action_matches(
    conn: sqlite3.Connection,
    row: sqlite3.Row | tuple,
    decisions: list[dict[str, str]],
) -> bool:
    target_id = row["restore_target_id"]
    if not isinstance(target_id, str) or not target_id or target_id == row["id"]:
        return False
    target = get(conn, target_id)
    return bool(
        target is not None
        and target.lineage_id == row["lineage_id"]
        and target.version < row["version"]
        and target.artifact_digest == row["proposal_digest"]
        and target.decisions == decisions
    )


def _parent_binding_exists(conn: sqlite3.Connection, row: sqlite3.Row | tuple) -> bool:
    if row["version"] == 1:
        return row["parent_id"] == ""
    expected_id = f"{row['lineage_id']}:v{row['version'] - 1}"
    if row["parent_id"] != expected_id:
        return False
    parent = conn.execute(
        "SELECT 1 FROM resume_rewrite_versions WHERE id=? AND lineage_id=? AND version=?",
        (expected_id, row["lineage_id"], row["version"] - 1),
    ).fetchone()
    return parent is not None


def _ordered_decisions(
    job: rewrite_store.ResumeRewriteJob, by_proposal: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    if job.output is None:
        raise ResumeRewriteReviewConflict("resume rewrite output changed")
    result = [
        copy.deepcopy(by_proposal[item["proposal_id"]])
        for item in job.output["proposals"]
        if item["proposal_id"] in by_proposal
    ]
    if len(result) != len(by_proposal):
        raise ResumeRewriteReviewConflict("resume rewrite decision changed")
    return result


def _lineage_id(job: rewrite_store.ResumeRewriteJob) -> str:
    digest = canonical_digest(
        {
            "schema": "resume-rewrite-lineage-v1",
            "job_id": job.id,
            "projection_id": job.projection_id,
            "projection_artifact_digest": job.projection_artifact_digest,
            "output_digest": job.output_digest,
        }
    )
    return f"resume-tailored-{digest[:32]}"


def _head_digest(**values: Any) -> str:
    return canonical_digest({"schema": "resume-rewrite-head-v1", **values})


def _decisions_digest(value: list[dict[str, str]]) -> str:
    return canonical_digest({"schema": "resume-rewrite-decisions-v1", "decisions": value})


def _require_digest(value: object) -> None:
    if not (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    ):
        raise ValueError("resume rewrite digest is invalid")


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
