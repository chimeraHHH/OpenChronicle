from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.evaluation.prompt_rescue import (
    load_dataset,
    main,
    run_evaluation,
    run_provider_corpus,
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
                    "error_code": "invalid_source",
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
                "error_code": "",
                "latency_ms": 2.0,
            }
        )
    template = load_prompt("prompt_rescue.md")
    return {
        "schema_version": 2,
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
    assert report["dataset"]["path"] == ("benchmarks/vida-prompt-rescue-v1/fixtures/cases.json")


def test_provider_runner_uses_production_path_and_rejects_invalid_source_locally() -> None:
    dataset = load_dataset(DATASET_PATH)
    cfg = config_mod.Config()
    cfg.prompt_rescue.enabled = True
    cfg.models["prompt_rescue"] = config_mod.ModelConfig(
        model="ollama/test-local",
        base_url="http://127.0.0.1:11434",
    )
    calls: list[dict] = []

    def fake_llm(_cfg, stage: str, **kwargs):
        calls.append({"stage": stage, **kwargs})
        prompt = json.loads(kwargs["messages"][1]["content"])["rough_prompt"]
        payload = {
            "schema_version": 1,
            "workflow": "prompt_rescue",
            "action_capability": "none",
            "improved_prompt": f"Clarify and structure: {prompt}",
            "assumptions": [],
            "missing_context": [],
            "changes": ["Structured the request."],
        }
        return type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Message", (), {"content": json.dumps(payload)})()},
                    )()
                ]
            },
        )()

    payload = run_provider_corpus(dataset, cfg, llm_caller=fake_llm)

    assert payload["schema_version"] == 2
    assert payload["model_identity"] == "ollama/test-local"
    assert payload["provider_location"] == "local"
    assert len(calls) == 12
    assert all(call["stage"] == "prompt_rescue" for call in calls)
    assert all(call["json_mode"] is True and "tools" not in call for call in calls)
    rejected = [row for row in payload["cases"] if row["admission"] == "rejected"]
    assert len(rejected) == 5
    assert {row["error_code"] for row in rejected} == {"invalid_source"}


def test_provider_runner_records_closed_failure_codes() -> None:
    dataset = load_dataset(DATASET_PATH)
    cfg = config_mod.Config()
    cfg.prompt_rescue.enabled = True
    responses = iter(["not-json", RuntimeError("private provider detail")])

    def fake_llm(*_args, **_kwargs):
        value = next(responses, RuntimeError("private provider detail"))
        if isinstance(value, Exception):
            raise value
        return type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Message", (), {"content": value})()},
                    )()
                ]
            },
        )()

    payload = run_provider_corpus(dataset, cfg, llm_caller=fake_llm)
    accepted = [row for row in payload["cases"] if row["admission"] == "accepted"]

    assert accepted[0]["error_code"] == "invalid_output"
    assert accepted[1]["error_code"] == "provider_failed"
    assert all(row["output"] is None for row in accepted)
    assert "private provider detail" not in json.dumps(payload)


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
