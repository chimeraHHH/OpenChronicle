from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle.evaluation import vida_memory_decisions


def _paths() -> tuple[Path, Path, Path]:
    repository = Path(__file__).resolve().parents[1]
    root = repository / "benchmarks" / "vida-memory-decisions-v1"
    return repository, root / "fixtures" / "cases.json", root / "json" / "metric_contract.json"


def _gold_provider(case: vida_memory_decisions.DecisionCase) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operations": [operation.to_dict() for operation in case.gold_operations],
    }


def test_model_decision_baseline_scores_exact_gold_without_mutating_memory() -> None:
    repository, dataset, contract = _paths()

    report = vida_memory_decisions.run_evaluation(
        dataset_path=dataset,
        metric_contract_path=contract,
        repository_root=repository,
        provider=_gold_provider,
        provider_identity={"provider": "test", "model": "gold"},
    )

    assert report["dataset"]["case_count"] == 8
    assert report["dataset"]["gold_operation_count"] == 6
    assert report["variant"]["action_capability"] == "none"
    assert report["variant"]["gate_verdict"]["passed"] is True
    assert report["variant"]["metrics"] == {
        "case_pass_rate": 1.0,
        "parse_success_rate": 1.0,
        "operation_tp": 6,
        "operation_fp": 0,
        "operation_fn": 0,
        "operation_precision": 1.0,
        "operation_recall": 1.0,
        "operation_f1": 1.0,
        "target_binding_accuracy": 1.0,
        "value_accuracy": 1.0,
        "provenance_support_rate": 1.0,
        "noop_accuracy": 1.0,
        "by_operation_type": {
            "forget": {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0},
            "reflect": {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0},
            "remember": {"tp": 2, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0},
            "update": {"tp": 2, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0},
        },
        "failure_stage_counts": {
            "provider_error": 0,
            "parse_error": 0,
            "detection_error": 0,
            "binding_error": 0,
            "value_error": 0,
            "provenance_error": 0,
        },
    }


def test_model_decision_baseline_separates_failure_stages() -> None:
    repository, dataset, contract = _paths()

    def provider(case: vida_memory_decisions.DecisionCase) -> object:
        if case.id == "remember-explicit-project-model":
            raise RuntimeError("provider detail must not land in the report")
        if case.id == "update-confirmed-model":
            return "not json"
        if case.id == "forget-one-target-not-neighbor":
            operation = case.gold_operations[0].to_dict()
            operation["target_id"] = "user.reporting.style"
            return {"schema_version": 1, "operations": [operation]}
        if case.id == "reflect-independent-sessions":
            operation = case.gold_operations[0].to_dict()
            operation["new_value"] = "Unsupported wording."
            return {"schema_version": 1, "operations": [operation]}
        if case.id == "multi-operation-update-and-remember":
            operations = [operation.to_dict() for operation in case.gold_operations]
            operations[0]["evidence_ids"] = ["m1-user"]
            operations[1]["evidence_ids"] = ["m1-user"]
            return {"schema_version": 1, "operations": operations}
        return _gold_provider(case)

    report = vida_memory_decisions.run_evaluation(
        dataset_path=dataset,
        metric_contract_path=contract,
        repository_root=repository,
        provider=provider,
        provider_identity={"provider": "test", "model": "failure-fixture"},
    )
    stages = {case["case_id"]: case["failure_stage"] for case in report["variant"]["cases"]}

    assert stages["remember-explicit-project-model"] == "provider_error"
    assert stages["update-confirmed-model"] == "parse_error"
    assert stages["forget-one-target-not-neighbor"] == "binding_error"
    assert stages["reflect-independent-sessions"] == "value_error"
    encoded = json.dumps(report)
    assert "provider detail must not land" not in encoded


def test_memops_adapter_maps_confirmed_operations_and_exact_spans() -> None:
    sample = {
        "conversations": [
            {
                "segment_index": 1,
                "dialogue": [
                    {"role": "user", "content": "Remember that my editor is Zed."},
                    {"role": "assistant", "content": "Saved."},
                ],
            }
        ],
        "operations": [
            {
                "type": "remember",
                "validity": "confirmed",
                "target": {"target_id": "user_editor", "target_name": "preferred editor"},
                "old_value": None,
                "new_value": "Zed",
                "evidence_spans": [{"segment_index": 1, "turn_index": 1}],
            },
            {
                "type": "update",
                "validity": "tentative",
                "target": {"target_id": "ignored", "target_name": "ignored"},
                "old_value": "a",
                "new_value": "b",
                "evidence_spans": [{"segment_index": 1, "turn_index": 1}],
            },
        ],
    }

    case = vida_memory_decisions.adapt_memops_sample(sample, case_id="memops-smoke")

    assert [turn.id for turn in case.evidence] == ["segment-1-turn-1", "segment-1-turn-2"]
    assert [operation.to_dict() for operation in case.gold_operations] == [
        {
            "type": "remember",
            "target_id": "user_editor",
            "old_value": "",
            "new_value": "Zed",
            "evidence_ids": ["segment-1-turn-1"],
        }
    ]


def test_model_decision_fixture_rejects_single_session_reflection(tmp_path: Path) -> None:
    _, dataset, _ = _paths()
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    reflection = next(
        case for case in payload["cases"] if case["id"] == "reflect-independent-sessions"
    )
    reflection["evidence"][1]["session_id"] = "p1"
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="independent sessions"):
        vida_memory_decisions.load_dataset(malformed)


def test_model_decision_report_round_trips_json(tmp_path: Path) -> None:
    repository, dataset, contract = _paths()
    report = vida_memory_decisions.run_evaluation(
        dataset_path=dataset,
        metric_contract_path=contract,
        repository_root=repository,
        provider=_gold_provider,
        provider_identity={"provider": "test", "model": "gold"},
    )
    output = tmp_path / "report.json"

    encoded = vida_memory_decisions.write_report(report, output)

    assert json.loads(encoded) == report
    assert json.loads(output.read_text(encoding="utf-8")) == report
