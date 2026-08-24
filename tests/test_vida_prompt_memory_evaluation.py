from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openchronicle import config as config_mod
from openchronicle.evaluation import vida_prompt_memory as evaluation

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "vida-prompt-memory-v1"
DATASET = BENCHMARK / "fixtures" / "cases.json"
CONTRACT = BENCHMARK / "json" / "metric_contract.json"


def _output(improved_prompt: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow": "prompt_rescue",
        "action_capability": "none",
        "improved_prompt": improved_prompt,
        "assumptions": [],
        "missing_context": [],
        "changes": ["Made the deliverable and constraints explicit."],
    }


def _oracle(case: evaluation.PromptMemoryCase, with_memory: bool) -> dict[str, Any]:
    anchors = list(case.gold.required_current_anchors)
    if with_memory and case.gold.memory_should_apply:
        anchors.extend(case.gold.required_memory_anchors)
    return _output("; ".join(anchors))


def test_reviewed_memory_variant_adds_relevant_anchors_without_leakage() -> None:
    dataset = evaluation.load_dataset(DATASET)
    report = evaluation.run_evaluation(
        dataset_path=DATASET,
        metric_contract_path=CONTRACT,
        provider=_oracle,
        provider_identity={"provider": "fixture", "model": "oracle"},
    )

    assert len(dataset.cases) == 6
    baseline = report["variants"]["no_memory"]["metrics"]
    conditioned = report["variants"]["reviewed_memory"]["metrics"]
    assert baseline["current_anchor_coverage"] == 1.0
    assert baseline["applicable_memory_anchor_coverage"] == 0.0
    assert conditioned == {
        "parse_success_rate": 1.0,
        "current_anchor_coverage": 1.0,
        "applicable_memory_anchor_coverage": 1.0,
        "forbidden_hit_rate": 0.0,
        "action_boundary_rate": 1.0,
    }
    assert report["comparison"]["applicable_memory_anchor_coverage_delta"] == 1.0
    assert report["variants"]["reviewed_memory"]["gate_verdict"]["passed"] is True
    assert report["decision"]["reply_rescue_scope_changed"] is False
    assert report["decision"]["computer_use_added"] is False


def test_stale_or_injected_memory_text_fails_the_gate() -> None:
    def leaky(case: evaluation.PromptMemoryCase, with_memory: bool) -> dict[str, Any]:
        text = "; ".join(case.gold.required_current_anchors)
        if with_memory and case.memory_items:
            text += "; " + "; ".join(item.content for item in case.memory_items)
        return _output(text)

    report = evaluation.run_evaluation(
        dataset_path=DATASET,
        metric_contract_path=CONTRACT,
        provider=leaky,
        provider_identity={"provider": "fixture", "model": "leaky"},
    )

    conditioned = report["variants"]["reviewed_memory"]
    assert conditioned["metrics"]["forbidden_hit_rate"] > 0
    assert conditioned["gate_verdict"]["passed"] is False


def test_configured_provider_uses_exact_empty_or_reviewed_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = evaluation.load_dataset(DATASET)
    case = dataset.cases[0]
    calls: list[dict[str, object]] = []

    def fake_generate(_cfg, **kwargs):
        calls.append(kwargs)
        return _output("fixture")

    monkeypatch.setattr(evaluation, "generate_output", fake_generate)
    provider = evaluation.configured_provider(config_mod.Config())

    provider(case, False)
    provider(case, True)

    assert calls[0]["reviewed_memory_context"] == {"items": []}
    items = calls[1]["reviewed_memory_context"]
    assert isinstance(items, dict)
    assert items["items"][0] == {
        "memory_id": "release-checklist",
        "path": "procedure-release-note.md",
        "content": (
            "For release notes, include Migration and Risks sections and ask "
            "for missing facts before drafting."
        ),
        "truncated": False,
    }


def test_prompt_memory_dataset_and_contract_reject_drift(tmp_path: Path) -> None:
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    payload["cases"][0]["memory_items"][0]["path"] = "project-release.md"
    bad_dataset = tmp_path / "bad-dataset.json"
    bad_dataset.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="path"):
        evaluation.load_dataset(bad_dataset)

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    contract["dataset"]["split"] = "changed"
    bad_contract = tmp_path / "bad-contract.json"
    bad_contract.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        evaluation.run_evaluation(
            dataset_path=DATASET,
            metric_contract_path=bad_contract,
            provider=_oracle,
            provider_identity={"provider": "fixture", "model": "oracle"},
        )
