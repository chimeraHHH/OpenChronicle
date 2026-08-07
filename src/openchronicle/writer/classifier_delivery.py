"""Durable classifier delivery worker and recovery entry points."""

from __future__ import annotations

import math
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import Config
from ..logger import get
from ..session import store as session_store
from ..store import files as files_mod
from ..store import fts
from . import classifier as classifier_mod
from . import classifier_jobs, session_reducer
from . import llm as llm_mod

logger = get("openchronicle.session")


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    job_id: str
    session_id: str
    status: str
    result: dict[str, Any] | None = None
    error: str = ""


def request_window(
    *,
    session_id: str,
    end: datetime,
    include_prior_day: bool,
) -> classifier_jobs.ClassifierJob | None:
    with fts.cursor() as conn:
        return classifier_jobs.request(
            conn,
            session_id=session_id,
            requested_end=end,
            include_prior_day=include_prior_day,
        )


def recover_terminal_requests() -> int:
    """Enqueue terminal windows even if the reducer callback never ran."""
    requested = 0
    with fts.cursor() as conn:
        for row in session_store.list_reduced_needing_classification(conn):
            if (
                row.classifier_terminal_pending
                and not row.classifier_terminal_entry_id
                and not row.classifier_terminal_path
            ):
                intent = session_reducer.recover_legacy_terminal_intent(conn, row)
                if intent is None:
                    continue
                session_store.backfill_classifier_terminal_intent(
                    conn,
                    row.id,
                    terminal_entry_id=intent[0],
                    terminal_path=intent[1],
                    terminal_noop=intent[2],
                )
            job = classifier_jobs.request_terminal(
                conn,
                session_id=row.id,
            )
            if job is not None:
                requested += 1
    return requested


def process_job(cfg: Config, job_id: str) -> DeliveryResult:
    """Claim, execute, receipt, and finalize one classifier window."""
    lease_seconds = max(
        int(getattr(cfg.classifier, "lease_seconds", 300)),
        math.ceil(llm_mod.call_budget_seconds(cfg, "classifier")) + 60,
    )
    if lease_seconds > 21_600:
        raise ValueError("classifier provider call budget exceeds the maximum safe lease")
    lease_seconds = max(30, lease_seconds)

    with fts.cursor() as conn:
        existing = classifier_jobs.get(conn, job_id)
        if existing is None:
            raise ValueError(f"unknown classifier job: {job_id}")
        if existing.status == "committed":
            finalized = classifier_jobs.finalize(conn, job_id=job_id)
            return _delivery_result(finalized.completed)
        try:
            claim = classifier_jobs.claim(
                conn,
                job_id=job_id,
                lease_seconds=lease_seconds,
            )
        except classifier_jobs.ClassifierJobBusy:
            current = classifier_jobs.get(conn, job_id)
            assert current is not None
            return _delivery_result(current)
    if not claim.claimed:
        return _delivery_result(claim.row)
    job = claim.row
    assert job.lease_token is not None
    lease_token = job.lease_token

    try:
        classify = classifier_mod.classify_window(
            cfg,
            session_id=job.session_id,
            event_daily_path=job.event_daily_path,
            start=job.window_start,
            end=job.window_end,
            include_prior_day=job.include_prior_day,
            delivery_job_id=job.id,
            delivery_lease_token=lease_token,
            focus_entry_ids=(
                [job.terminal_entry_id]
                if job.kind == "terminal" and job.terminal_entry_id
                else []
                if job.kind == "terminal"
                else None
            ),
            allow_empty_delivery=job.allow_empty,
        )
        payload = {
            "committed": bool(classify.committed),
            "summary": classify.summary,
            "written_ids": list(classify.written_ids),
            "created_paths": list(classify.created_paths),
            "candidate_ids": list(classify.candidate_ids),
            "skipped_reason": classify.skipped_reason,
        }
        with fts.cursor() as conn:
            current = classifier_jobs.get(conn, job.id)
            if current is None:
                raise ValueError(f"classifier job disappeared: {job.id}")
            if classify.skipped_reason and current.status == "running":
                with files_mod.review_operation_lock():
                    classifier_jobs.record_commit(
                        conn,
                        job_id=job.id,
                        lease_token=lease_token,
                        producer_run_key=classify.producer_run_key,
                        result=payload,
                    )
            elif classify.committed and current.status == "running":
                # Compatibility fallback for injected/custom classifier
                # implementations. The built-in tool loop writes its receipt
                # inside the explicit commit tool before returning.
                with files_mod.review_operation_lock():
                    classifier_jobs.record_commit(
                        conn,
                        job_id=job.id,
                        lease_token=lease_token,
                        producer_run_key=classify.producer_run_key,
                        result=payload,
                    )
            elif not classify.committed:
                error = classify.error or "classifier ended without a durable commit"
                failed = classifier_jobs.fail(
                    conn,
                    job_id=job.id,
                    lease_token=lease_token,
                    error=error,
                    retry_seconds=_retry_seconds(cfg),
                )
                return DeliveryResult(
                    job_id=job.id,
                    session_id=job.session_id,
                    status=failed.status if failed is not None else "failed",
                    result=failed.result if failed is not None else None,
                    error=error,
                )
            finalized = classifier_jobs.finalize(conn, job_id=job.id)
        _log_success(finalized.completed)
        return _delivery_result(finalized.completed)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        with fts.cursor() as conn:
            current = classifier_jobs.get(conn, job.id)
            if (
                current is not None
                and current.status == "running"
                and current.lease_token == lease_token
            ):
                with suppress(classifier_jobs.ClassifierJobLostLease):
                    classifier_jobs.fail(
                        conn,
                        job_id=job.id,
                        lease_token=lease_token,
                        error=error,
                        retry_seconds=_retry_seconds(cfg),
                    )
            # A persisted commit receipt deliberately stays committed when
            # bookmark finalization fails. The next worker completes it
            # without another model call.
            current = classifier_jobs.get(conn, job.id)
        logger.error("classifier delivery %s failed: %s", job.id, error, exc_info=True)
        return DeliveryResult(
            job_id=job.id,
            session_id=job.session_id,
            status=current.status if current is not None else "failed",
            result=current.result if current is not None else None,
            error=error,
        )


def drain_due(cfg: Config, *, limit: int = 100) -> list[DeliveryResult]:
    """Process due jobs in canonical order, including generated follow-ups."""
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be in [1, 1000]")
    results: list[DeliveryResult] = []
    while len(results) < limit:
        recover_terminal_requests()
        with fts.cursor() as conn:
            due = classifier_jobs.list_due(conn, limit=1)
        if not due:
            break
        outcome = process_job(cfg, due[0].id)
        results.append(outcome)
        if outcome.status in ("running", "pending"):
            break
    return results


def run_recovery_pass(cfg: Config, *, limit: int = 100) -> list[DeliveryResult]:
    recover_terminal_requests()
    return drain_due(cfg, limit=limit)


def _delivery_result(job: classifier_jobs.ClassifierJob) -> DeliveryResult:
    return DeliveryResult(
        job_id=job.id,
        session_id=job.session_id,
        status=job.status,
        result=job.result,
        error=job.last_error,
    )


def _retry_seconds(cfg: Config) -> int:
    return max(1, min(3600, int(getattr(cfg.classifier, "retry_seconds", 60))))


def _log_success(job: classifier_jobs.ClassifierJob) -> None:
    payload = job.result or {}
    candidates = payload.get("candidate_ids")
    candidate_count = len(candidates) if isinstance(candidates, list) else 0
    if payload.get("skipped_reason"):
        logger.info(
            "classifier delivery %s: skipped (%s)",
            job.session_id,
            payload["skipped_reason"],
        )
    elif candidate_count:
        logger.info(
            "classifier delivery %s: staged %d memory candidate(s) for review",
            job.session_id,
            candidate_count,
        )
    else:
        logger.info("classifier delivery %s: committed with no writes", job.session_id)
