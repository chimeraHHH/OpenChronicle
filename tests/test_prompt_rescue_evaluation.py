from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle.evaluation.prompt_rescue import (
    load_dataset,
    main,
    run_evaluation,
)
from openchronicle.prompt_rescue.service import TEMPLATE_VERSION
from openchronicle.prompts import load as load_prompt
from openchronicle.provenance.models import canonical_digest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = REPOSITORY_ROOT / "benchmarks" / "vida-prompt-rescue-v1"
DATASET_PATH = BENCHMARK_ROOT / "fixtures" / "cases.json"
CONTRACT_PATH = BENCHMARK_ROOT / "json" / "metric_contract.json"


def _perfect_corpus() -> dict:
    dataset = load_dataset(DATASET_PATH)
    cases = []
    for case in dataset.cases:
        expected = case.expected
        if expected.admission == "rejected":
            cases.append(
                {
                    "case_id": case.id,
                    "admission": "rejected",
                    "output": None,
                    "latency_ms": 2.0,
                }
            )
            continue
        fragments = [
            *expected.required_improved_fragments,
            *expected.required_constraint_fragments,
        ]
        cases.append(
            {
                "case_id": case.id,
                "admission": "accepted",
                "output": {
                    "schema_version": 1,
                    "workflow": "prompt_rescue",
                    "action_capability": "none",
                    "improved_prompt": ". ".join(fragments),
                    "assumptions": [],
                    "missing_context": (
                        [expected.required_missing_context_any[0]]
                        if expected.required_missing_context_any
                        else []
                    ),
                    "changes": ["Structured the reviewed request."],
                },
                "latency_ms": 2.0,
            }
        )
    template = load_prompt("prompt_rescue.md")
    return {
        "schema_version": 1,
        "dataset_id": dataset.id,
        "variant": "perfect_fixture",
        "model_identity": "fixture/no-provider",
        "provider_location": "not_applicable",
        "template_version": TEMPLATE_VERSION,
        "template_digest": canonical_digest(
            {"schema": "prompt-rescue-template-v1", "text": template}
        ),
        "cases": cases,
    }


def test_frozen_prompt_rescue_dataset_and_contract_match() -> None:
    dataset = load_dataset(DATASET_PATH)
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert dataset.id == "OC-Vida-Prompt-Rescue-v1"
    assert dataset.split == "prompt-rescue-adversarial-dev-v1"
    assert len(dataset.cases) == 17
    assert sum(case.expected.admission == "accepted" for case in dataset.cases) == 12
    assert sum(case.expected.admission == "rejected" for case in dataset.cases) == 5
    assert contract["dataset"] == {"id": dataset.id, "split": dataset.split}
    assert contract["primary_metric"] == "case_pass_rate"


def test_raw_baseline_is_reproducible_and_fails_improvement_and_adversarial_gates() -> None:
    report = run_evaluation(
        dataset_path=DATASET_PATH,
        metric_contract_path=CONTRACT_PATH,
        repository_root=REPOSITORY_ROOT,
    )

    raw = report["variants"]["raw_input"]
    assert raw["metrics"]["admission_accuracy"] == 1.0
    assert raw["metrics"]["invalid_rejection_rate"] == 1.0
    assert raw["metrics"]["schema_valid_rate"] == 1.0
    assert raw["metrics"]["material_change_rate"] == 0.0
    assert raw["metrics"]["secret_echo_rate"] > 0
    assert raw["metrics"]["injection_override_rate"] > 0
    assert raw["gate_verdict"]["passed"] is False
    assert report["baseline_status"]["formal_gate"] == "blocked_unregistered"


def test_complete_external_corpus_can_pass_without_model_as_judge(tmp_path: Path) -> None:
    corpus_path = tmp_path / "perfect.json"
    corpus_path.write_text(json.dumps(_perfect_corpus()), encoding="utf-8")

    report = run_evaluation(
        dataset_path=DATASET_PATH,
        metric_contract_path=CONTRACT_PATH,
        repository_root=REPOSITORY_ROOT,
        corpus_paths=(corpus_path,),
    )

    fixture = report["variants"]["perfect_fixture"]
    assert fixture["metrics"]["case_pass_rate"] == 1.0
    assert fixture["metrics"]["action_capability_violation_count"] == 0
    assert fixture["metrics"]["unsupported_assumption_rate"] == 0.0
    assert fixture["gate_verdict"]["passed"] is True


def test_corpus_schema_and_action_escape_fail_closed(tmp_path: Path) -> None:
    corpus = _perfect_corpus()
    corpus["cases"][0]["output"]["action_capability"] = "send"
    corpus_path = tmp_path / "escaped.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")

    report = run_evaluation(
        dataset_path=DATASET_PATH,
        metric_contract_path=CONTRACT_PATH,
        repository_root=REPOSITORY_ROOT,
        corpus_paths=(corpus_path,),
    )
    fixture = report["variants"]["perfect_fixture"]
    assert fixture["metrics"]["schema_valid_rate"] < 1.0
    assert fixture["metrics"]["action_capability_violation_count"] == 1
    assert fixture["gate_verdict"]["passed"] is False

    corpus["cases"][0]["unknown"] = True
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
    with pytest.raises(ValueError, match="corpus case"):
        run_evaluation(
            dataset_path=DATASET_PATH,
            metric_contract_path=CONTRACT_PATH,
            repository_root=REPOSITORY_ROOT,
            corpus_paths=(corpus_path,),
        )


def test_cli_writes_exact_report_and_resolves_relative_paths(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    assert (
        main(
            [
                "--dataset",
                "benchmarks/vida-prompt-rescue-v1/fixtures/cases.json",
                "--contract",
                "benchmarks/vida-prompt-rescue-v1/json/metric_contract.json",
                "--output",
                str(output),
                "--quiet",
            ]
        )
        == 0
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["dataset"]["case_count"] == 17
    assert report["dataset"]["path"] == (
        "benchmarks/vida-prompt-rescue-v1/fixtures/cases.json"
    )


def test_dataset_parser_rejects_unknown_fields_and_unbounded_repeat(tmp_path: Path) -> None:
    payload = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    payload["unknown"] = True
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="envelope"):
        load_dataset(malformed)

    payload.pop("unknown")
    payload["cases"][0]["input"]["rough_prompt"] = {"repeat": "x", "count": 100_001}
    malformed.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="integer"):
        load_dataset(malformed)
