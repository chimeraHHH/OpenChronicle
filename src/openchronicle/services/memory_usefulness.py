"""Business-state-preserving report for exact Prompt Rescue memory revisions."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any

from ..artifact_adoptions import store as adoption_store
from ..config import Config
from ..prompt_rescue import store as prompt_store
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, content_digest
from ..store import entries as entries_store
from ..store import files as files_store
from ..store.facts import temporal_state
from .current_facts import list_current_facts
from .memory import entry_has_verified_successor

_RevisionKey = tuple[str, str, str, str]


def memory_usefulness_report(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Describe memory-conditioned Prompt Rescue outcomes without changing ranking.

    The report joins immutable Prompt Rescue provenance and exact adoption
    records. It intentionally emits no memory, prompt, or artifact text and
    treats edited adoptions as ambiguous rather than positive usefulness.
    """
    instant = as_of or datetime.now().astimezone()
    jobs, invalid_job_count = _prompt_jobs(conn)
    adoptions, invalid_adoption_count = _prompt_adoptions(conn)
    adoptions_by_artifact: dict[str, list[adoption_store.ArtifactAdoption]] = defaultdict(
        list
    )
    for adoption in adoptions:
        adoptions_by_artifact[adoption.artifact_id].append(adoption)
    for values in adoptions_by_artifact.values():
        values.sort(key=lambda item: (item.adopted_at, item.id))

    revision_jobs: dict[_RevisionKey, dict[str, prompt_store.PromptRescueJob]] = defaultdict(
        dict
    )
    conditioned_jobs: dict[str, prompt_store.PromptRescueJob] = {}
    no_memory_jobs: dict[str, prompt_store.PromptRescueJob] = {}
    quarantined_conditioned = 0
    revision_binding_count = 0
    tracked_binding_count = 0
    for job in jobs:
        if job.status != "ready" or job.output is None:
            continue
        expected_sources = [job.input_ref, *job.memory_context_refs]
        sources = provenance_store.direct_sources_checked(
            conn,
            EvidenceRef(kind="prompt_rescue", id=job.id),
        )
        if not job.memory_context_refs:
            if sources == expected_sources:
                no_memory_jobs[job.id] = job
            continue
        revision_binding_count += len(job.memory_context_refs)
        if sources != expected_sources:
            quarantined_conditioned += 1
            continue
        conditioned_jobs[job.id] = job
        tracked_binding_count += len(job.memory_context_refs)
        for ref in job.memory_context_refs:
            revision_jobs[_revision_key(ref)][job.id] = job

    revision_rows: list[dict[str, Any]] = []
    for key in sorted(revision_jobs):
        path, entry_id, timestamp, revision_hash = key
        ref = EvidenceRef(
            kind="memory_entry",
            id=entry_id,
            path=path,
            timestamp=timestamp,
            content_hash=revision_hash,
        )
        current_status, status_reason = _revision_status(
            conn,
            cfg,
            ref,
            as_of=instant,
        )
        dependent_artifacts = [
            _artifact_row(job, adoptions_by_artifact.get(job.id, []))
            for job in sorted(revision_jobs[key].values(), key=lambda value: value.id)
        ]
        linked_adoptions = [
            adoption
            for job in revision_jobs[key].values()
            for adoption in adoptions_by_artifact.get(job.id, [])
        ]
        revision_rows.append(
            {
                "memory_revision": {
                    "kind": ref.kind,
                    "path": ref.path,
                    "id": ref.id,
                    "timestamp": ref.timestamp,
                    "content_hash": ref.content_hash,
                },
                "current_status": current_status,
                "current_status_reason": status_reason,
                "conditioned_output_count": len(revision_jobs[key]),
                "unedited_adoption_count": sum(
                    not adoption.output_edited for adoption in linked_adoptions
                ),
                "edited_adoption_count": sum(
                    adoption.output_edited for adoption in linked_adoptions
                ),
                "dependent_artifacts": dependent_artifacts,
            }
        )

    conditioned_adoptions = [
        adoption
        for job_id in conditioned_jobs
        for adoption in adoptions_by_artifact.get(job_id, [])
    ]
    no_memory_adoptions = [
        adoption
        for job_id in no_memory_jobs
        for adoption in adoptions_by_artifact.get(job_id, [])
    ]
    conditioned_unedited_outputs = {
        adoption.artifact_id
        for adoption in conditioned_adoptions
        if not adoption.output_edited
    }
    no_memory_unedited_outputs = {
        adoption.artifact_id
        for adoption in no_memory_adoptions
        if not adoption.output_edited
    }
    linked_job_ids = conditioned_jobs.keys() | no_memory_jobs.keys()
    return {
        "schema_version": 1,
        "scope": "prompt_rescue_exact_memory_revision_outcomes",
        "interpretation": (
            "descriptive association only; edited adoptions are ambiguous and "
            "rates are not causal uplift"
        ),
        "adoption_record_semantics": (
            "one immutable row per distinct adopted artifact digest, not a use-event count"
        ),
        "missing_status_semantics": (
            "the exact revision cannot currently be re-resolved and authorized"
        ),
        "summary": {
            "memory_revision_count": len(revision_rows),
            "conditioned_output_count": len(conditioned_jobs),
            "conditioned_unedited_adoption_count": sum(
                not adoption.output_edited for adoption in conditioned_adoptions
            ),
            "conditioned_edited_adoption_count": sum(
                adoption.output_edited for adoption in conditioned_adoptions
            ),
            "conditioned_output_unedited_adoption_rate": _ratio(
                len(conditioned_unedited_outputs),
                len(conditioned_jobs),
            ),
            "no_memory_output_count": len(no_memory_jobs),
            "no_memory_unedited_adoption_count": sum(
                not adoption.output_edited for adoption in no_memory_adoptions
            ),
            "no_memory_edited_adoption_count": sum(
                adoption.output_edited for adoption in no_memory_adoptions
            ),
            "no_memory_output_unedited_adoption_rate": _ratio(
                len(no_memory_unedited_outputs),
                len(no_memory_jobs),
            ),
            "exact_revision_binding_count": revision_binding_count,
            "tracked_exact_revision_binding_count": tracked_binding_count,
            "exact_revision_tracking_coverage": _ratio(
                tracked_binding_count,
                revision_binding_count,
            ),
            "quarantined_conditioned_output_count": quarantined_conditioned,
            "invalid_prompt_rescue_row_count": invalid_job_count,
            "invalid_adoption_row_count": invalid_adoption_count,
            "unlinked_prompt_rescue_adoption_count": sum(
                adoption.artifact_id not in linked_job_ids for adoption in adoptions
            ),
        },
        "memory_revisions": revision_rows,
    }


def _prompt_jobs(
    conn: sqlite3.Connection,
) -> tuple[list[prompt_store.PromptRescueJob], int]:
    rows = conn.execute("SELECT id FROM prompt_rescue_jobs ORDER BY id").fetchall()
    jobs: list[prompt_store.PromptRescueJob] = []
    invalid = 0
    for row in rows:
        job = prompt_store.get(conn, row["id"])
        if job is None:
            invalid += 1
        else:
            jobs.append(job)
    return jobs, invalid


def _prompt_adoptions(
    conn: sqlite3.Connection,
) -> tuple[list[adoption_store.ArtifactAdoption], int]:
    rows = conn.execute(
        "SELECT id FROM artifact_adoptions WHERE artifact_kind='prompt_rescue' ORDER BY id"
    ).fetchall()
    adoptions: list[adoption_store.ArtifactAdoption] = []
    invalid = 0
    for row in rows:
        adoption = adoption_store.get(conn, row["id"])
        if adoption is None:
            invalid += 1
        else:
            adoptions.append(adoption)
    return adoptions, invalid


def _artifact_row(
    job: prompt_store.PromptRescueJob,
    adoptions: list[adoption_store.ArtifactAdoption],
) -> dict[str, Any]:
    return {
        "artifact_id": job.id,
        "current_output_digest": job.output_digest,
        "current_output_version": job.version,
        "current_output_edited": job.output_edited,
        "adoptions": [
            {
                "adoption_id": adoption.id,
                "artifact_digest": adoption.artifact_digest,
                "artifact_version": adoption.artifact_version,
                "output_edited": adoption.output_edited,
                "adopted_at": adoption.adopted_at,
            }
            for adoption in adoptions
        ],
    }


def _revision_status(
    conn: sqlite3.Connection,
    cfg: Config,
    ref: EvidenceRef,
    *,
    as_of: datetime,
) -> tuple[str, str]:
    try:
        path = files_store.memory_path(ref.path)
    except (TypeError, ValueError):
        return "missing", "invalid_path"
    try:
        parsed = files_store.read_file(path)
    except FileNotFoundError:
        return "missing", "file_missing"
    except (OSError, TypeError, ValueError):
        return "missing", "parse_failed"
    entries = [value for value in parsed.entries if value.id == ref.id]
    if not entries:
        return "missing", "entry_missing"
    if len(entries) != 1:
        return "missing", "duplicate_entry_id"
    entry = entries[0]
    if not entry.provenance_valid:
        return "missing", "invalid_provenance"
    if ref.timestamp != entry.timestamp:
        return "missing", "timestamp_mismatch"
    if ref.content_hash != content_digest(entries_store.entry_index_content(entry)):
        return "missing", "hash_mismatch"
    if entries_store.entry_index_superseded(entry) and not entry_has_verified_successor(
        conn,
        parsed,
        entry,
    ):
        return "missing", "invalid_supersede_chain"
    if entries_store.entry_index_superseded(entry):
        return "superseded", f"superseded_by:{entry.superseded_by or 'unknown'}"
    if entry.fact_metadata is not None and temporal_state(
        entry.fact_metadata,
        as_of=as_of,
    ) == "expired":
        return "expired", "valid_to"
    current = list_current_facts(
        conn,
        cfg,
        as_of=as_of,
        limit=10_000,
        subject_key=(entry.fact_metadata.subject_key if entry.fact_metadata else None),
    )
    if any(
        fact.path == ref.path
        and fact.id == ref.id
        and fact.recorded_at == ref.timestamp
        and content_digest(fact.content) == ref.content_hash
        for fact in current
    ):
        return "current", "current"
    return "missing", "not_current_or_authorized"


def _revision_key(ref: EvidenceRef) -> _RevisionKey:
    return ref.path, ref.id, ref.timestamp, ref.content_hash


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0
