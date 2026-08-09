from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle.evaluation.resume_rewrite import (
    load_dataset,
    run_evaluation,
    write_report,
)

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "benchmarks" / "vida-resume-rescue-v1" / "rewrite"
DATASET = SUITE / "cases.json"
CONTRACT = SUITE / "metric_contract.json"


def test_frozen_supervised_rewrite_suite_executes_every_production_boundary() -> None:
    dataset = load_dataset(DATASET)

    assert len(dataset.cases) == 40
    assert {case.exercise for case in dataset.cases} == {
        "model_output",
        "remote_egress",
        "provider_input",
        "provider_output",
        "service_generation",
        "service_review",
        "closed_protocol",
    }

    report = run_evaluation(
        dataset_path=DATASET,
        metric_contract_path=CONTRACT,
        repository_root=ROOT,
    )

    assert report["gate_verdict"]["passed"] is True
    assert report["metrics"] == {
        "case_pass_rate": 1.0,
        "schema_action_pass_rate": 1.0,
        "source_binding_pass_rate": 1.0,
        "protected_atom_rejection_recall": 1.0,
        "prompt_injection_rejection_rate": 1.0,
        "stale_decision_rejection_rate": 1.0,
        "verified_proposal_yield": 1.0,
        "abstention_rate": 1.0,
        "provider_failure_rate": 0.5,
    }
    assert report["human_metrics"] == {
        "human_factual_accuracy": None,
        "human_preference_over_exact_baseline": None,
        "requirement_target_usefulness": None,
        "status": "not_run",
    }
    assert all(row["passed"] for row in report["cases"])
    assert {row["action_capability"] for row in report["cases"]} == {"none"}
    by_id = {row["case_id"]: row for row in report["cases"]}
    assert by_id["provider-tool-call"]["raw_error_code"] == "invalid_output"
    assert by_id["excluded-fact-egress-attempt"]["raw_error_code"] == "source_mismatch"
    assert by_id["bulk-accept-request"]["raw_error_code"] == "unknown_operation"
    assert by_id["stale-profile-after-generation"]["raw_error_code"] == "review_conflict"


def test_report_writer_round_trips_utf8_and_does_not_claim_human_quality(
    tmp_path: Path,
) -> None:
    report = run_evaluation(
        dataset_path=DATASET,
        metric_contract_path=CONTRACT,
        repository_root=ROOT,
    )

    encoded = write_report(report, tmp_path / "report.json")
    decoded = json.loads(encoded)

    assert decoded["evaluation_id"] == "vida-resume-supervised-rewrite-v1"
    assert decoded["baseline_status"]["formal_gate"] == "development_only"
    assert decoded["human_metrics"]["status"] == "not_run"


def test_dataset_loader_rejects_unregistered_exercise(tmp_path: Path) -> None:
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    payload["cases"][0]["exercise"] = "invented_evaluator_path"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="exercise is invalid"):
        load_dataset(path)
