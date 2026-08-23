from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle.evaluation import vida_procedure_adoption as evaluation

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "vida-procedure-adoption-v1"
DATASET = BENCHMARK / "fixtures" / "cases.json"
CONTRACT = BENCHMARK / "json" / "metric_contract.json"


def _oracle(case: evaluation.AdoptionCase) -> dict[str, object]:
    if not case.gold.qualifies:
        return {
            "schema_version": 1,
            "qualifies": False,
            "rationale": "The artifact is one-off or unsafe procedural evidence.",
            "procedure": None,
        }
    anchors = list(case.gold.required_anchors)
    steps = anchors if len(anchors) >= 2 else [anchors[0], "Review the prepared text."]
    procedure_type = case.gold.procedure_type
    return {
        "schema_version": 1,
        "qualifies": True,
        "rationale": "The artifact explicitly states a reusable text procedure.",
        "procedure": {
            "title": f"Reviewed {case.id}",
            "procedure_type": procedure_type,
            "scope": "Reviewed text generation",
            "trigger": f"Use for {anchors[0]}",
            "steps": steps,
            "template": "\n".join(anchors) if procedure_type == "template" else None,
            "action_capability": "none",
        },
    }


def test_frozen_adoption_dataset_proves_adoption_alone_is_insufficient() -> None:
    dataset = evaluation.load_dataset(DATASET)
    report = evaluation.run_evaluation(
        dataset_path=DATASET,
        metric_contract_path=CONTRACT,
        repository_root=ROOT,
        provider=_oracle,
        provider_identity={
            "stage": "classifier",
            "provider": "fixture",
            "model": "oracle",
            "reasoning_effort": "none",
        },
    )

    assert len(dataset.cases) == 10
    assert sum(case.gold.qualifies for case in dataset.cases) == 3
    assert report["variants"]["any_adoption"]["metrics"]["qualification_confusion"] == {
        "tp": 3,
        "fp": 7,
        "fn": 0,
        "tn": 0,
    }
    assert report["variants"]["any_adoption"]["metrics"]["qualification_precision"] == 0.3
    assert report["variants"]["any_adoption"]["gate_verdict"]["passed"] is False
    assert report["decision"]["single_adoption_alone_is_sufficient"] is False
    assert report["decision"]["production_classifier_changed"] is False
    assert report["variants"]["configured_model"]["gate_verdict"]["passed"] is True


def test_model_variant_requires_grounded_type_and_anchors() -> None:
    def ungrounded(case: evaluation.AdoptionCase) -> dict[str, object]:
        result = _oracle(case)
        if case.gold.qualifies:
            procedure = result["procedure"]
            assert isinstance(procedure, dict)
            procedure["steps"] = ["Draft polished text.", "Review it."]
            procedure["template"] = (
                "Generic template" if procedure["procedure_type"] == "template" else None
            )
        return result

    report = evaluation.run_evaluation(
        dataset_path=DATASET,
        metric_contract_path=CONTRACT,
        repository_root=ROOT,
        provider=ungrounded,
        provider_identity={"provider": "fixture", "model": "ungrounded"},
    )

    metrics = report["variants"]["configured_model"]["metrics"]
    assert metrics["qualification_precision"] == 1.0
    assert metrics["anchor_support_rate"] < 0.9
    assert report["variants"]["configured_model"]["gate_verdict"]["passed"] is False


def test_prediction_parser_rejects_actions_and_unknown_fields() -> None:
    action = _oracle(evaluation.load_dataset(DATASET).cases[0])
    procedure = action["procedure"]
    assert isinstance(procedure, dict)
    procedure["action_capability"] = "send"
    with pytest.raises(ValueError, match="action capability"):
        evaluation._parse_prediction(action)

    rejected = _oracle(evaluation.load_dataset(DATASET).cases[3])
    rejected["unknown"] = True
    with pytest.raises(ValueError, match="envelope"):
        evaluation._parse_prediction(rejected)


def test_dataset_and_contract_reject_drift(tmp_path: Path) -> None:
    dataset_payload = json.loads(DATASET.read_text(encoding="utf-8"))
    dataset_payload["cases"][0]["artifact"]["action_capability"] = "paste"
    bad_dataset = tmp_path / "bad-dataset.json"
    bad_dataset.write_text(json.dumps(dataset_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact contract"):
        evaluation.load_dataset(bad_dataset)

    contract_payload = json.loads(CONTRACT.read_text(encoding="utf-8"))
    contract_payload["dataset"]["split"] = "changed"
    bad_contract = tmp_path / "bad-contract.json"
    bad_contract.write_text(json.dumps(contract_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        evaluation.run_evaluation(
            dataset_path=DATASET,
            metric_contract_path=bad_contract,
            repository_root=ROOT,
            provider=_oracle,
            provider_identity={"provider": "fixture", "model": "oracle"},
        )


def test_report_output_is_exact_json(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    payload = {"schema_version": 1, "decision": "review-only"}

    encoded = evaluation.write_report(payload, output)

    assert encoded.endswith("\n")
    assert json.loads(encoded) == payload
    assert output.read_text(encoding="utf-8") == encoded
