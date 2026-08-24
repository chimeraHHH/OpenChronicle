from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef
from openchronicle.resume_rescue import ResumeRescueService
from openchronicle.resume_rescue import rewrite_store as store
from openchronicle.resume_rescue.rewrite import ResumeRewriteValidationError
from openchronicle.resume_rescue.rewrite_generation import build_rewrite_provider_input
from openchronicle.store import fts


def _cfg() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.resume_rescue.enabled = True
    return cfg


def _projection(service: ResumeRescueService):
    service.save_profile(
        profile_id="rewrite-profile",
        display_name="Ada Example",
        locale="en-US",
        facts=[
            {
                "id": "fact-latency",
                "section": "experience",
                "text": ("Built Python APIs at Acme Labs and reduced p95 latency by 40% in 2024."),
                "confidentiality": "private",
                "ownership_scope": "individual",
                "provenance": [
                    {
                        "kind": "manual_reviewed",
                        "reviewed_at": "2026-08-09T08:00:00+08:00",
                    }
                ],
            }
        ],
    )
    opportunity, _ = service.save_opportunity(
        employer="Target Labs",
        title="Reliability Engineer",
        source_text="Improve service reliability.",
        captured_at="2026-08-09T09:00:00+08:00",
    )
    projection, _ = service.compose_exact(
        profile_id="rewrite-profile",
        opportunity_id=opportunity.id,
        sections=[{"kind": "experience", "fact_ids": ["fact-latency"]}],
        requirements=[
            {
                "id": "req-reliability",
                "text": "Improve service reliability.",
                "fact_ids": ["fact-latency"],
            }
        ],
    )
    return projection


def _create(
    conn,
    projection,
    *,
    now: datetime | None = None,
    template_digest: str = "c" * 64,
):
    return store.create(
        conn,
        projection=projection,
        provider_input=build_rewrite_provider_input(projection.artifact),
        template_version=1,
        template_digest=template_digest,
        model_identity="ollama/test",
        provider_location="local",
        remote_egress_authorized=False,
        now=now,
    )


def _output() -> dict[str, object]:
    return {
        "schema_version": 1,
        "proposals": [
            {
                "proposal_id": "proposal-latency",
                "operation": "replace_text",
                "section": "experience",
                "fact_id": "fact-latency",
                "original_text": (
                    "Built Python APIs at Acme Labs and reduced p95 latency by 40% in 2024."
                ),
                "proposed_text": (
                    "Reduced p95 latency by 40% in 2024 at Acme Labs; built Python APIs."
                ),
                "rationale": "Emphasizes the mapped result without adding a claim.",
                "requirement_ids": ["req-reliability"],
                "evidence_fragments": ["reduced p95 latency by 40%"],
            }
        ],
    }


def test_rewrite_job_is_idempotent_projection_bound_and_private(ac_root: Path) -> None:
    with fts.cursor() as conn:
        projection = _projection(ResumeRescueService(conn, _cfg()))
        first, created = _create(conn, projection)
        replay, replay_created = _create(conn, projection)

        assert created is True
        assert replay_created is False
        assert replay == first
        assert first.status == "queued"
        assert first.remote_egress_authorized is False
        assert store.list_jobs(conn) == [first]
        assert provenance_store.direct_sources_checked(
            conn, EvidenceRef(kind="resume_rewrite", id=first.id)
        ) == [projection.ref]

        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='resume_rewrite_jobs'"
        ).fetchone()["sql"]
        assert "provider_location IN ('local', 'remote_or_unknown')" in sql
        assert "remote_egress_authorized IN (0, 1)" in sql


def test_remote_job_requires_recorded_authorization(ac_root: Path) -> None:
    with fts.cursor() as conn:
        projection = _projection(ResumeRescueService(conn, _cfg()))
        with pytest.raises(ValueError, match="creation fields"):
            store.create(
                conn,
                projection=projection,
                provider_input=build_rewrite_provider_input(projection.artifact),
                template_version=1,
                template_digest="c" * 64,
                model_identity="openai/test",
                provider_location="remote_or_unknown",
                remote_egress_authorized=False,
            )


def test_claim_complete_and_stale_lease_are_fail_closed(ac_root: Path) -> None:
    with fts.cursor() as conn:
        projection = _projection(ResumeRescueService(conn, _cfg()))
        queued, _ = _create(conn, projection)
        claimed = store.claim_next(conn, lease_token="lease-one", lease_seconds=60)
        assert claimed is not None
        assert claimed.id == queued.id
        assert claimed.status == "leased"
        assert claimed.attempt_count == 1

        ready = store.complete(
            conn,
            job_id=claimed.id,
            lease_token="lease-one",
            output=_output(),
        )
        assert ready.status == "ready"
        assert ready.output == _output()
        assert ready.output_digest
        assert ready.lease_token is None
        with pytest.raises(store.ResumeRewriteConflict, match="lease changed"):
            store.complete(
                conn,
                job_id=claimed.id,
                lease_token="lease-one",
                output=_output(),
            )


def test_invalid_output_does_not_consume_worker_lease(ac_root: Path) -> None:
    with fts.cursor() as conn:
        projection = _projection(ResumeRescueService(conn, _cfg()))
        queued, _ = _create(conn, projection)
        claimed = store.claim_next(conn, lease_token="lease-invalid", lease_seconds=60)
        assert claimed is not None
        invalid = _output()
        invalid["proposals"][0]["proposed_text"] = "Invented 99% improvement."
        with pytest.raises(ResumeRewriteValidationError):
            store.complete(
                conn,
                job_id=queued.id,
                lease_token="lease-invalid",
                output=invalid,
            )
        current = store.get(conn, queued.id)
        assert current is not None
        assert current.status == "leased"
        assert current.lease_token == "lease-invalid"


def test_expired_lease_reclaim_retry_and_delete(ac_root: Path) -> None:
    start = datetime(2026, 8, 9, 1, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        projection = _projection(ResumeRescueService(conn, _cfg()))
        queued, _ = _create(conn, projection, now=start)
        first = store.claim_next(
            conn,
            lease_token="lease-first",
            lease_seconds=30,
            now=start,
        )
        assert first is not None
        second = store.claim_next(
            conn,
            lease_token="lease-second",
            lease_seconds=30,
            now=start + timedelta(seconds=31),
        )
        assert second is not None
        assert second.attempt_count == 2
        with pytest.raises(store.ResumeRewriteConflict):
            store.fail(
                conn,
                job_id=queued.id,
                lease_token="lease-first",
                error_code="provider_failed",
            )
        failed = store.fail(
            conn,
            job_id=queued.id,
            lease_token="lease-second",
            error_code="provider_failed",
        )
        retried = store.retry(conn, job_id=failed.id, expected_version=failed.version)
        assert retried.status == "queued"
        assert retried.error_code == ""

        store.delete(conn, job_id=retried.id, expected_version=retried.version)
        assert store.get(conn, retried.id) is None
        assert (
            provenance_store.direct_sources_checked(
                conn, EvidenceRef(kind="resume_rewrite", id=retried.id)
            )
            == []
        )


def test_row_input_output_and_provenance_tampering_fail_closed(ac_root: Path) -> None:
    with fts.cursor() as conn:
        projection = _projection(ResumeRescueService(conn, _cfg()))
        queued, _ = _create(conn, projection)
        conn.execute(
            "UPDATE resume_rewrite_jobs SET provider_input_json='{}' WHERE id=?",
            (queued.id,),
        )
        assert store.get(conn, queued.id) is None
        assert store.list_jobs(conn) == []

    with fts.cursor() as conn:
        projection = _projection(ResumeRescueService(conn, _cfg()))
        queued, _ = _create(conn, projection, template_digest="d" * 64)
        conn.execute(
            "DELETE FROM provenance_edges WHERE subject_kind='resume_rewrite' AND subject_id=?",
            (queued.id,),
        )
        assert store.get(conn, queued.id) is not None
        assert (
            provenance_store.direct_sources_checked(
                conn, EvidenceRef(kind="resume_rewrite", id=queued.id)
            )
            == []
        )
