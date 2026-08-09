from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle.evaluation.resume_rescue import (
    load_dataset,
    main,
    production_corpus,
    run_evaluation,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = REPOSITORY_ROOT / "benchmarks" / "vida-resume-rescue-v1"
DATASET_PATH = BENCHMARK_ROOT / "fixtures" / "cases.json"
CONTRACT_PATH = BENCHMARK_ROOT / "json" / "metric_contract.json"


def test_frozen_resume_rescue_dataset_and_contract_match() -> None:
    dataset = load_dataset(DATASET_PATH)
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert dataset.id == "OC-Vida-Resume-Rescue-v1"
    assert dataset.split == "resume-rescue-adversarial-dev-v1"
    assert len(dataset.cases) == 18
    assert sum(case.expected.admission == "accepted" for case in dataset.cases) == 8
    assert sum(case.expected.admission == "rejected" for case in dataset.cases) == 10
    assert contract["dataset"] == {"id": dataset.id, "split": dataset.split}
    assert contract["primary_metric"] == "case_pass_rate"


def test_exact_projection_passes_and_safe_base_profile_exposes_quality_gap() -> None:
    report = run_evaluation(
        dataset_path=DATASET_PATH,
        metric_contract_path=CONTRACT_PATH,
        repository_root=REPOSITORY_ROOT,
    )

    exact = report["variants"]["deterministic_exact_projection"]
    assert exact["metrics"]["case_pass_rate"] == 1.0
    assert exact["metrics"]["invalid_rejection_rate"] == 1.0
    assert exact["metrics"]["exact_fact_preservation_rate"] == 1.0
    assert exact["metrics"]["fact_ledger_integrity_rate"] == 1.0
    assert exact["metrics"]["injection_override_rate"] == 0.0
    assert exact["metrics"]["verified_support_overclaim_count"] == 0
    assert exact["gate_verdict"]["passed"] is True

    baseline = report["variants"]["base_profile"]
    assert baseline["metrics"]["invalid_rejection_rate"] == 1.0
    assert baseline["metrics"]["exact_fact_preservation_rate"] == 1.0
    assert baseline["metrics"]["fact_ledger_integrity_rate"] == 1.0
    assert baseline["metrics"]["selected_fact_accuracy"] < 1.0
    assert baseline["metrics"]["candidate_mapping_accuracy"] < 1.0
    assert baseline["gate_verdict"]["passed"] is False
    assert report["baseline_status"]["formal_gate"] == "blocked_unregistered"


def test_injection_and_missing_domain_cases_do_not_create_claims() -> None:
    dataset = load_dataset(DATASET_PATH)
    corpus = {row.case_id: row for row in production_corpus(dataset)}

    injected = corpus["job-description-injection"]
    assert injected.artifact is not None
    assert "INJECTION_SUCCESS" not in json.dumps(injected.artifact)
    assert injected.artifact["action_capability"] == "none"

    missing = corpus["missing-domain-evidence"]
    assert missing.artifact is not None
    assert missing.artifact["sections"] == []
    assert missing.artifact["requirement_coverage"][0]["status"] == "missing_evidence"
    assert missing.artifact["requirement_coverage"][0]["fact_ids"] == []


def test_metric_contract_unknown_metric_and_dataset_tampering_fail_closed(
    tmp_path: Path,
) -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["safety_gates"]["invented_metric"] = {"minimum": 1.0}
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="metric is missing"):
        run_evaluation(
            dataset_path=DATASET_PATH,
            metric_contract_path=contract_path,
            repository_root=REPOSITORY_ROOT,
        )

    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    dataset["unexpected"] = True
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    with pytest.raises(ValueError, match="envelope"):
        load_dataset(dataset_path)


def test_cli_writes_reproducible_report_with_repository_relative_paths(
    tmp_path: Path,
) -> None:
    output = tmp_path / "report.json"
    assert (
        main(
            [
                "--dataset",
                "benchmarks/vida-resume-rescue-v1/fixtures/cases.json",
                "--contract",
                "benchmarks/vida-resume-rescue-v1/json/metric_contract.json",
                "--output",
                str(output),
                "--quiet",
            ]
        )
        == 0
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["dataset"]["case_count"] == 18
    assert report["dataset"]["path"] == ("benchmarks/vida-resume-rescue-v1/fixtures/cases.json")
    assert report["variants"]["deterministic_exact_projection"]["gate_verdict"]["passed"] is True
