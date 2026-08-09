from __future__ import annotations

from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.provenance import store as provenance_store
from openchronicle.resume_rescue import review_store, rewrite_store
from openchronicle.resume_rescue.render import build_document_tree
from openchronicle.resume_rescue.rewrite import rewrite_proposal_digest
from openchronicle.resume_rescue.rewrite_generation import build_rewrite_provider_input
from openchronicle.resume_rescue.service import ResumeRescueService
from openchronicle.store import fts


def _cfg() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.resume_rescue.enabled = True
    return cfg


def _ready(service: ResumeRescueService, conn):
    profile, _ = service.save_profile(
        profile_id="review-profile",
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
        profile_id=profile.profile_id,
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
    job, _ = rewrite_store.create(
        conn,
        projection=projection,
        provider_input=build_rewrite_provider_input(projection.artifact),
        template_version=1,
        template_digest="c" * 64,
        model_identity="ollama/test",
        provider_location="local",
        remote_egress_authorized=False,
    )
    claimed = rewrite_store.claim_next(conn, lease_token="review-lease", lease_seconds=60)
    assert claimed is not None
    proposal = {
        "proposal_id": "proposal-latency",
        "operation": "replace_text",
        "section": "experience",
        "fact_id": "fact-latency",
        "original_text": ("Built Python APIs at Acme Labs and reduced p95 latency by 40% in 2024."),
        "proposed_text": ("Reduced p95 latency by 40% in 2024 at Acme Labs; built Python APIs."),
        "rationale": "Emphasizes the mapped result without adding a claim.",
        "requirement_ids": ["req-reliability"],
        "evidence_fragments": ["reduced p95 latency by 40%"],
    }
    ready = rewrite_store.complete(
        conn,
        job_id=job.id,
        lease_token="review-lease",
        output={"schema_version": 1, "proposals": [proposal]},
    )
    return profile, projection, ready, proposal


def _decide(conn, projection, job, proposal, *, head=None, decision="accepted"):
    return review_store.decide(
        conn,
        job_id=job.id,
        proposal_id=proposal["proposal_id"],
        expected_proposal_digest=rewrite_proposal_digest(proposal),
        expected_job_version=job.version,
        expected_head_id=head.id if head is not None else "",
        expected_artifact_digest=(
            head.artifact_digest if head is not None else projection.artifact_digest
        ),
        decision=decision,
    )


def test_accept_creates_immutable_derived_projection_without_mutating_profile(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        profile, projection, job, proposal = _ready(service, conn)

        accepted, created = _decide(conn, projection, job, proposal)

        assert created is True
        assert accepted.version == 1
        assert accepted.parent_id == ""
        assert accepted.decision == "accepted"
        assert accepted.artifact["generation_mode"] == "supervised_rewrite_projection"
        item = accepted.artifact["sections"][0]["items"][0]
        assert item["text"] == proposal["proposed_text"]
        assert item["transformation"] == "accepted_model_rewrite"
        assert service.get_profile(profile.profile_id) == profile
        assert review_store.get_head(conn, job.id) == accepted
        assert review_store.list_versions(conn, job_id=job.id) == [accepted]
        assert provenance_store.direct_sources_checked(conn, accepted.ref) == [
            projection.ref,
            job.output_ref,
        ]
        assert provenance_store.is_current(conn, accepted.ref)
        tree = build_document_tree(profile=profile, projection=accepted)
        assert proposal["proposed_text"] in tree.plain_text()


def test_decisions_are_individual_digest_bound_and_head_fenced(ac_root: Path) -> None:
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        _profile, projection, job, proposal = _ready(service, conn)
        with pytest.raises(ValueError, match="decision is invalid"):
            _decide(conn, projection, job, proposal, decision="accept_all")
        with pytest.raises(review_store.ResumeRewriteReviewConflict, match="proposal changed"):
            review_store.decide(
                conn,
                job_id=job.id,
                proposal_id=proposal["proposal_id"],
                expected_proposal_digest="d" * 64,
                expected_job_version=job.version,
                expected_head_id="",
                expected_artifact_digest=projection.artifact_digest,
                decision="accepted",
            )

        first, _ = _decide(conn, projection, job, proposal)
        replay, created = _decide(conn, projection, job, proposal, head=first)
        assert created is False
        assert replay == first
        with pytest.raises(review_store.ResumeRewriteReviewConflict, match="head changed"):
            _decide(conn, projection, job, proposal)

        rejected, created = _decide(
            conn, projection, job, proposal, head=first, decision="rejected"
        )
        assert created is True
        assert rejected.version == 2
        assert rejected.parent_id == first.id
        item = rejected.artifact["sections"][0]["items"][0]
        assert item["text"] == proposal["original_text"]
        assert item["transformation"] == "selected_exact"


def test_restore_is_non_destructive_and_job_delete_cannot_break_history(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        _profile, projection, job, proposal = _ready(service, conn)
        accepted, _ = _decide(conn, projection, job, proposal)
        rejected, _ = _decide(conn, projection, job, proposal, head=accepted, decision="rejected")

        restored = review_store.restore(
            conn,
            target_version_id=accepted.id,
            expected_head_id=rejected.id,
            expected_artifact_digest=rejected.artifact_digest,
        )

        assert restored.version == 3
        assert restored.parent_id == rejected.id
        assert restored.action == "restore"
        assert restored.decision == "restored"
        assert restored.restore_target_id == accepted.id
        assert restored.decisions == accepted.decisions
        assert restored.artifact["sections"][0]["items"][0]["text"] == proposal["proposed_text"]
        assert [item.version for item in review_store.list_versions(conn, job_id=job.id)] == [
            3,
            2,
            1,
        ]
        with pytest.raises(rewrite_store.ResumeRewriteConflict, match="reviewed versions"):
            rewrite_store.delete(conn, job_id=job.id, expected_version=job.version)


def test_tampered_version_or_provenance_is_hidden_and_blocks_head_progress(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        _profile, projection, job, proposal = _ready(service, conn)
        accepted, _ = _decide(conn, projection, job, proposal)
        conn.execute(
            "UPDATE resume_rewrite_versions SET decisions_json='[]' WHERE id=?",
            (accepted.id,),
        )

        assert review_store.get(conn, accepted.id) is None
        assert review_store.get_head(conn, job.id) is None
        with pytest.raises(review_store.ResumeRewriteReviewConflict, match="head changed"):
            _decide(conn, projection, job, proposal, head=accepted, decision="rejected")


def test_missing_version_provenance_hides_review_history(ac_root: Path) -> None:
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        _profile, projection, job, proposal = _ready(service, conn)
        accepted, _ = _decide(conn, projection, job, proposal)
        conn.execute(
            "DELETE FROM provenance_edges WHERE subject_kind='resume_rewrite_version' AND subject_id=?",
            (accepted.id,),
        )
        assert review_store.get(conn, accepted.id) is None
        assert review_store.list_versions(conn, job_id=job.id) == []
