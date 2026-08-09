from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openchronicle.evaluation.vida_suggestions import (
    load_dataset,
    main,
    run_evaluation,
    write_report,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = REPOSITORY_ROOT / "benchmarks" / "vida-suggestions-v1"
DATASET_PATH = BASELINE_ROOT / "fixtures" / "opportunities.json"
CONTRACT_PATH = BASELINE_ROOT / "json" / "metric_contract.json"


def test_versioned_opportunity_fixture_and_metric_contract_match() -> None:
    dataset = load_dataset(DATASET_PATH)
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert dataset.id == "OC-Vida-Fixtures-v1"
    assert dataset.split == "work-resumption-opportunity-v1"
    assert len(dataset.cases) == 16
    assert sum(case.expected_help for case in dataset.cases) == 2
    assert contract["dataset"]["id"] == dataset.id
    assert contract["dataset"]["split"] == dataset.split
    assert contract["primary_metric"] == "opportunity_precision"
    assert set(contract["variants"]) == {"reactive", "heuristic", "kernel"}


def test_three_baseline_comparison_uses_production_kernel_and_preserves_root(
    ac_root: Path,
) -> None:
    previous_root = os.environ["OPENCHRONICLE_ROOT"]
    report = run_evaluation(
        dataset_path=DATASET_PATH,
        metric_contract_path=CONTRACT_PATH,
        repository_root=REPOSITORY_ROOT,
        latency_repeats=1,
    )

    assert os.environ["OPENCHRONICLE_ROOT"] == previous_root
    assert Path(previous_root) == ac_root
    assert report["dataset"]["case_count"] == 16
    assert report["dataset"]["sha256"] == load_dataset(DATASET_PATH).digest

    reactive = report["variants"]["reactive"]
    assert reactive["metrics"]["opportunity_precision"] == 1.0
    assert reactive["metrics"]["opportunity_recall"] == 0.5
    assert reactive["gate_verdict"]["passed"] is True

    heuristic = report["variants"]["heuristic"]
    assert heuristic["metrics"]["confusion"] == {
        "true_positive": 2,
        "false_positive": 8,
        "false_negative": 0,
        "true_negative": 6,
    }
    assert heuristic["gate_verdict"]["passed"] is False

    kernel = report["variants"]["kernel"]
    assert kernel["metrics"]["confusion"] == {
        "true_positive": 2,
        "false_positive": 2,
        "false_negative": 0,
        "true_negative": 12,
    }
    assert kernel["metrics"]["evidence_coverage"] == 1.0
    assert kernel["metrics"]["unsupported_claim_rate"] == 0.0
    assert kernel["metrics"]["semantic_duplicate_count"] == 0
    assert kernel["gate_verdict"]["checks"]["opportunity_precision"] is False
    assert report["baseline_status"]["formal_gate"] == "blocked_unregistered"


def test_report_output_is_exact_json(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    payload = {"schema_version": 1, "value": "evidence"}

    encoded = write_report(payload, output)

    assert encoded.endswith("\n")
    assert json.loads(encoded) == payload
    assert output.read_text(encoding="utf-8") == encoded


def test_cli_resolves_repository_relative_fixture_paths(tmp_path: Path) -> None:
    output = tmp_path / "relative-path-report.json"

    assert (
        main(
            [
                "--dataset",
                "benchmarks/vida-suggestions-v1/fixtures/opportunities.json",
                "--contract",
                "benchmarks/vida-suggestions-v1/json/metric_contract.json",
                "--output",
                str(output),
                "--latency-repeats",
                "1",
                "--quiet",
            ]
        )
        == 0
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["dataset"]["path"] == (
        "benchmarks/vida-suggestions-v1/fixtures/opportunities.json"
    )


def test_fixture_parser_rejects_unknown_fields(tmp_path: Path) -> None:
    payload = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    payload["unknown"] = True
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="envelope"):
        load_dataset(malformed)
