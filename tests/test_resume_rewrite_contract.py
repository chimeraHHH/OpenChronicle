from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "benchmarks" / "vida-resume-rescue-v1" / "rewrite"


def test_supervised_rewrite_manifest_freezes_limits_references_and_hard_gates() -> None:
    manifest = json.loads((CONTRACT / "manifest.json").read_text(encoding="utf-8"))

    assert set(manifest) == {
        "schema_version",
        "suite_id",
        "rewrite_contract_version",
        "limits",
        "hard_gates",
        "reported_metrics",
        "reference_implementations",
    }
    assert manifest["schema_version"] == 1
    assert manifest["suite_id"] == "OC-Vida-Resume-Supervised-Rewrite-v1"
    assert manifest["rewrite_contract_version"] == 1
    assert manifest["limits"] == {
        "selected_fact_maximum_count": 200,
        "selected_fact_maximum_chars": 8_000,
        "requirement_maximum_count": 200,
        "requirement_maximum_chars": 5_000,
        "provider_input_maximum_chars": 200_000,
        "provider_output_maximum_chars": 200_000,
        "proposal_maximum_count": 200,
        "proposal_text_maximum_chars": 8_000,
        "evidence_fragment_maximum_count": 32,
        "evidence_fragment_maximum_chars": 2_000,
        "provider_timeout_seconds": 120,
    }
    assert manifest["hard_gates"]["action_capability"] == "none"
    assert all(
        value is True for key, value in manifest["hard_gates"].items() if key != "action_capability"
    )
    assert {
        "source_binding_pass_rate",
        "protected_atom_rejection_recall",
        "human_factual_accuracy",
        "human_preference_over_exact_baseline",
        "provider_failure_rate",
    } <= set(manifest["reported_metrics"])

    references = manifest["reference_implementations"]
    assert len(references) == 7
    assert len({item["repository"] for item in references}) == len(references)
    assert all(len(item["commit"]) == 40 for item in references)
    assert {item["repository"] for item in references} == {
        "srbhr/Resume-Matcher",
        "AmruthPillai/Reactive-Resume",
        "phoinixi/resuml",
        "Gsync/jobsync",
        "xitanggg/open-resume",
        "akhil-dara/CVAurum",
        "ibarrajo/ApplyPilot",
    }


def test_supervised_rewrite_cases_cover_safety_quality_and_review_boundaries() -> None:
    dataset = json.loads((CONTRACT / "cases.json").read_text(encoding="utf-8"))

    assert set(dataset) == {"schema_version", "dataset_id", "split", "cases"}
    assert dataset["schema_version"] == 1
    assert dataset["dataset_id"] == "OC-Vida-Resume-Supervised-Rewrite-v1"
    assert dataset["split"] == "synthetic-supervised-rewrite-adversarial-dev-v1"
    cases = dataset["cases"]
    assert len(cases) == 40
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["expected"]["action_capability"] for case in cases} == {"none"}
    assert {case["expected"]["admission"] for case in cases} == {
        "proposal_set",
        "abstention",
        "rejected_pre_egress",
        "rejected_post_provider",
        "rejected_on_review",
    }
    assert {case["family"] for case in cases} == {
        "valid",
        "abstention",
        "egress",
        "injection",
        "schema",
        "binding",
        "protected_atom",
        "provider",
        "stale",
        "review",
        "action",
        "claim",
    }
    ids = {case["id"] for case in cases}
    assert {
        "valid-preserved-percentage",
        "valid-no-change-abstention",
        "remote-provider-without-opt-in",
        "excluded-fact-egress-attempt",
        "job-description-tool-instruction",
        "provider-tool-call",
        "unknown-output-field",
        "unselected-fact-id",
        "evidence-fragment-not-substring",
        "changed-percentage",
        "credential-echo",
        "provider-timeout",
        "stale-profile-after-generation",
        "stale-proposal-decision-digest",
        "bulk-accept-request",
        "unreviewed-auto-apply",
        "master-profile-mutation-attempt",
        "resume-upload-or-submit-attempt",
        "ats-outcome-claim",
    } <= ids
