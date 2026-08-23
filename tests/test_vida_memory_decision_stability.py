from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from openchronicle.evaluation import vida_memory_decision_stability as stability


def _base_report() -> dict:
    return {
        "schema_version": 1,
        "evaluation_id": "vida-memory-decisions-v1",
        "repository": {"commit": "a" * 40, "dirty": False, "status_lines": 0},
        "dataset": {"id": "fixed", "sha256": "b" * 64},
        "metric_contract": {"path": "contract.json", "sha256": "c" * 64},
        "variant": {
            "id": "configured_model_memory_decision_json_v1",
            "provider": {"provider": "codex_cli", "model": "gpt-5.6-sol"},
            "action_capability": "none",
            "gate_verdict": {"passed": True},
            "metrics": {
                "parse_success_rate": 1.0,
                "operation_precision": 0.93,
                "operation_recall": 1.0,
                "operation_f1": 0.964,
                "target_binding_accuracy": 1.0,
                "value_accuracy": 0.37,
                "provenance_support_rate": 0.96,
                "noop_accuracy": 1.0,
                "operation_tp": 27,
                "operation_fp": 2,
                "operation_fn": 0,
                "failure_stage_counts": {"provider_error": 0, "parse_error": 0},
            },
            "cases": [
                {
                    "case_id": "case-a",
                    "predicted_operations": [
                        {
                            "type": "remember",
                            "target_id": "alpha",
                            "new_value": "Alpha value",
                            "evidence_ids": ["t1"],
                        }
                    ],
                },
                {"case_id": "case-b", "predicted_operations": []},
            ],
        },
    }


def _write_reports(tmp_path: Path, reports: list[dict]) -> list[Path]:
    paths = []
    for index, report in enumerate(reports, start=1):
        path = tmp_path / f"run-{index}.json"
        path.write_text(json.dumps(report))
        paths.append(path)
    return paths


def _contract(tmp_path: Path, *, version: int = 1) -> Path:  # noqa: ARG001
    root = Path(__file__).resolve().parents[1]
    source = (
        root
        / "benchmarks"
        / "vida-memory-decisions-v1"
        / "json"
        / (
            "official_memops_stability_contract.json"
            if version == 1
            else "official_memops_stability_contract_v2.json"
        )
    )
    return source


def test_aggregate_identical_clean_runs_passes(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    reports = [_base_report(), _base_report(), _base_report()]
    result = stability.aggregate_reports(
        report_paths=_write_reports(tmp_path, reports),
        contract_path=_contract(tmp_path),
        repository_root=root,
    )

    assert result["gate_verdict"]["passed"] is True
    assert result["metrics"]["exact_case_decision_agreement_rate"] == 1.0
    assert result["metrics"]["unique_run_signature_count"] == 1
    assert result["metrics"]["operation_f1_stdev"] == 0.0


def test_aggregate_records_case_level_decision_variation(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    reports = [_base_report(), _base_report(), _base_report()]
    reports[2] = copy.deepcopy(reports[2])
    reports[2]["variant"]["cases"][0]["predicted_operations"].append(
        {"type": "update", "target_id": "alpha", "evidence_ids": ["t2"]}
    )
    result = stability.aggregate_reports(
        report_paths=_write_reports(tmp_path, reports),
        contract_path=_contract(tmp_path),
        repository_root=root,
    )

    case_a = next(item for item in result["per_case_agreement"] if item["case_id"] == "case-a")
    assert case_a["unique_signature_count"] == 2
    assert case_a["majority_fraction"] == 0.666667
    assert result["metrics"]["exact_case_decision_agreement_rate"] == 0.5


def test_v2_gates_structure_and_evidence_but_keeps_text_exactness_diagnostic(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    reports = [_base_report(), _base_report(), _base_report()]
    reports[2] = copy.deepcopy(reports[2])
    reports[2]["variant"]["cases"][0]["predicted_operations"][0]["new_value"] = (
        "Semantically equivalent alpha value"
    )
    result = stability.aggregate_reports(
        report_paths=_write_reports(tmp_path, reports),
        contract_path=_contract(tmp_path, version=2),
        repository_root=root,
    )

    assert result["evaluation_id"] == "vida-memory-decision-stability-v2"
    assert result["gate_verdict"]["passed"] is True
    assert result["metrics"]["structural_case_decision_agreement_rate"] == 1.0
    assert result["metrics"]["evidence_case_decision_agreement_rate"] == 1.0
    assert result["metrics"]["exact_case_decision_agreement_rate"] == 0.5


def test_aggregate_rejects_dirty_or_mixed_identity_runs(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    reports = [_base_report(), _base_report(), _base_report()]
    reports[1] = copy.deepcopy(reports[1])
    reports[1]["repository"]["dirty"] = True
    with pytest.raises(ValueError, match="clean repository"):
        stability.aggregate_reports(
            report_paths=_write_reports(tmp_path, reports),
            contract_path=_contract(tmp_path),
            repository_root=root,
        )

    reports = [_base_report(), _base_report(), _base_report()]
    reports[2] = copy.deepcopy(reports[2])
    reports[2]["variant"]["provider"]["model"] = "other-model"
    with pytest.raises(ValueError, match="frozen identity"):
        stability.aggregate_reports(
            report_paths=_write_reports(tmp_path, reports),
            contract_path=_contract(tmp_path),
            repository_root=root,
        )
