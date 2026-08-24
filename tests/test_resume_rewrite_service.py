from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openchronicle import config as config_mod
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef
from openchronicle.resume_rescue import review_store, rewrite_store
from openchronicle.resume_rescue.review_store import ResumeRewriteReviewConflict
from openchronicle.resume_rescue.rewrite import rewrite_proposal_digest
from openchronicle.resume_rescue.rewrite_generation import ResumeRewriteEgressDenied
from openchronicle.resume_rescue.service import ResumeRescueService
from openchronicle.store import fts


class _Message:
    def __init__(self, content: str):
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, content: str):
        self.message = _Message(content)


class _Response:
    def __init__(self, payload: dict[str, Any]):
        self.choices = [_Choice(json.dumps(payload))]


def _cfg(*, remote: bool = False) -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.resume_rescue.enabled = True
    cfg.resume_rescue.rewrite_enabled = True
    cfg.models["resume_rescue"] = config_mod.ModelConfig(
        model="openai/test-remote" if remote else "ollama/test-local",
        base_url="https://api.example.test" if remote else "http://127.0.0.1:11434",
        timeout_seconds=1,
        num_retries=0,
    )
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


def _output(*, proposed_text: str | None = None) -> dict[str, Any]:
    original = "Built Python APIs at Acme Labs and reduced p95 latency by 40% in 2024."
    return {
        "schema_version": 1,
        "proposals": [
            {
                "proposal_id": "proposal-latency",
                "operation": "replace_text",
                "section": "experience",
                "fact_id": "fact-latency",
                "original_text": original,
                "proposed_text": proposed_text
                or "Reduced p95 latency by 40% in 2024 at Acme Labs; built Python APIs.",
                "rationale": "Emphasizes the mapped result without adding a claim.",
                "requirement_ids": ["req-reliability"],
                "evidence_fragments": ["reduced p95 latency by 40%"],
            }
        ],
    }


def _queue(service: ResumeRescueService, projection, *, remote_authorized: bool = False):
    provider = service.rewrite_provider_summary()
    return service.queue_rewrite(
        projection.id,
        expected_artifact_digest=projection.artifact_digest,
        expected_model_identity=provider["model"],
        expected_provider_location=provider["location"],
        remote_egress_authorized=remote_authorized,
    )


def test_rewrite_service_queues_after_disclosure_and_processes_no_tool_call(
    ac_root: Path,
) -> None:
    cfg = _cfg()
    calls: list[dict[str, Any]] = []

    def fake_llm(_cfg, stage: str, **kwargs):
        calls.append({"stage": stage, **kwargs})
        return _Response(_output())

    with fts.cursor() as conn:
        service = ResumeRescueService(conn, cfg, llm_caller=fake_llm)
        projection = _projection(service)
        queued, created = _queue(service, projection)
        replay, replay_created = _queue(service, projection)

        assert created is True
        assert replay_created is False
        assert replay == queued
        assert service.get_rewrite(queued.id) == queued
        assert calls == []

        ready = service.process_next_rewrite()

        assert ready is not None
        assert ready.status == "ready"
        assert ready.output == _output()
        assert service.list_rewrites() == [ready]
        assert len(calls) == 1
        assert calls[0]["stage"] == "resume_rescue"
        assert calls[0]["tools"] is None
        assert calls[0]["json_mode"] is True
        payload = json.loads(calls[0]["messages"][1]["content"])
        assert set(payload) == {
            "schema_version",
            "workflow",
            "action_capability",
            "data_trust",
            "facts",
        }
        assert payload["facts"][0]["text"].startswith("Built Python APIs")
        assert "Target Labs" not in json.dumps(payload)
        assert provenance_store.direct_sources_checked(
            conn, EvidenceRef(kind="resume_rewrite", id=ready.id)
        ) == [projection.ref]


def test_remote_rewrite_requires_per_job_authorization_before_queue(ac_root: Path) -> None:
    cfg = _cfg(remote=True)
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, cfg)
        projection = _projection(service)

        with pytest.raises(ResumeRewriteEgressDenied, match="not authorized"):
            _queue(service, projection)

        assert rewrite_store.list_jobs(conn) == []
        queued, created = _queue(service, projection, remote_authorized=True)
        assert created is True
        assert queued.provider_location == "remote_or_unknown"
        assert queued.remote_egress_authorized is True


def test_provider_change_after_queue_fails_without_egress(ac_root: Path) -> None:
    cfg = _cfg()
    calls = 0

    def fake_llm(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response(_output())

    with fts.cursor() as conn:
        service = ResumeRescueService(conn, cfg, llm_caller=fake_llm)
        projection = _projection(service)
        queued, _ = _queue(service, projection)
        cfg.models["resume_rescue"] = config_mod.ModelConfig(
            model="ollama/different",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            num_retries=0,
        )

        failed = service.process_next_rewrite()

        assert failed is not None
        assert failed.id == queued.id
        assert failed.status == "failed"
        assert failed.error_code == "input_changed"
        assert calls == 0
        assert service.get_rewrite(queued.id) is None


def test_source_change_after_queue_fails_without_egress(ac_root: Path) -> None:
    cfg = _cfg()
    calls = 0

    def fake_llm(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response(_output())

    with fts.cursor() as conn:
        service = ResumeRescueService(conn, cfg, llm_caller=fake_llm)
        projection = _projection(service)
        queued, _ = _queue(service, projection)
        profile = service.get_profile("rewrite-profile")
        assert profile is not None
        service.save_profile(
            profile_id="rewrite-profile",
            display_name="Ada Example",
            locale="en-US",
            facts=[
                *profile.profile["facts"],
                {
                    "id": "fact-new",
                    "section": "skill",
                    "text": "Reviewed Python skill.",
                    "confidentiality": "private",
                    "ownership_scope": "individual",
                    "provenance": [
                        {
                            "kind": "manual_reviewed",
                            "reviewed_at": "2026-08-09T10:00:00+08:00",
                        }
                    ],
                },
            ],
            expected_version=profile.version,
        )

        failed = service.process_next_rewrite()

        assert failed is not None
        assert failed.id == queued.id
        assert failed.error_code == "input_changed"
        assert calls == 0


def test_unsupported_claim_is_failed_safely_then_can_retry(ac_root: Path) -> None:
    cfg = _cfg()
    responses = [
        _Response(_output(proposed_text="Built Kubernetes at Acme Labs in 2024.")),
        _Response(_output()),
    ]

    with fts.cursor() as conn:
        service = ResumeRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: responses.pop(0),
        )
        projection = _projection(service)
        queued, _ = _queue(service, projection)

        failed = service.process_next_rewrite()

        assert failed is not None
        assert failed.id == queued.id
        assert failed.status == "failed"
        assert failed.error_code == "unsupported_claim"
        assert failed.output is None
        retried = service.retry_rewrite(failed.id, expected_version=failed.version)
        assert retried.status == "queued"
        ready = service.process_next_rewrite()
        assert ready is not None
        assert ready.status == "ready"

        service.delete_rewrite(ready.id, expected_version=ready.version)
        assert rewrite_store.get(conn, ready.id) is None


def test_provider_failure_is_sanitized_and_provenance_tamper_hides_job(
    ac_root: Path,
) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        service = ResumeRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("credential secret diagnostic")
            ),
        )
        projection = _projection(service)
        queued, _ = _queue(service, projection)

        failed = service.process_next_rewrite()
        assert failed is not None
        assert failed.error_code == "provider_failed"
        assert "secret" not in json.dumps(failed.output)

        conn.execute(
            "DELETE FROM provenance_edges WHERE subject_kind='resume_rewrite' AND subject_id=?",
            (failed.id,),
        )
        assert service.get_rewrite(failed.id) is None
        assert service.list_rewrites() == []


def test_profile_change_after_generation_blocks_review_decision(ac_root: Path) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        service = ResumeRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: _Response(_output()),
        )
        projection = _projection(service)
        _queue(service, projection)
        ready = service.process_next_rewrite()
        assert ready is not None and ready.output is not None
        proposal = ready.output["proposals"][0]
        profile = service.get_profile("rewrite-profile")
        assert profile is not None
        service.save_profile(
            profile_id=profile.profile_id,
            display_name=profile.profile["display_name"],
            locale=profile.profile["locale"],
            facts=[
                *profile.profile["facts"],
                {
                    "id": "fact-after-generation",
                    "section": "skill",
                    "text": "Reviewed Python skill.",
                    "confidentiality": "private",
                    "ownership_scope": "individual",
                    "provenance": [
                        {
                            "kind": "manual_reviewed",
                            "reviewed_at": "2026-08-09T11:00:00+08:00",
                        }
                    ],
                },
            ],
            expected_version=profile.version,
        )

        with pytest.raises(ResumeRewriteReviewConflict, match="output changed"):
            service.decide_rewrite(
                ready.id,
                proposal_id=proposal["proposal_id"],
                expected_proposal_digest=rewrite_proposal_digest(proposal),
                expected_job_version=ready.version,
                expected_head_id="",
                expected_artifact_digest=projection.artifact_digest,
                decision="accepted",
            )

        assert review_store.list_versions(conn, job_id=ready.id) == []
