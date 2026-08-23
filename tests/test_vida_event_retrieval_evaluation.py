from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle.evaluation import vida_event_retrieval as evaluation


def _paths() -> tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parents[1]
    benchmark = root / "benchmarks" / "vida-event-retrieval-v1" / "json"
    return root, benchmark / "cases.json", benchmark / "metric_contract.json"


def test_fixed_event_retrieval_comparison_passes_frozen_gates() -> None:
    root, dataset, contract = _paths()
    report = evaluation.run_evaluation(
        dataset_path=dataset,
        metric_contract_path=contract,
        repository_root=root,
    )

    assert report["gate_verdict"]["passed"] is True
    assert report["variants"]["event_adjacent"]["metrics"]["answerable_rate"] == 1.0
    assert report["variants"]["event_adjacent"]["metrics"]["anchor_recall"] == 1.0
    assert report["variants"]["event_adjacent"]["metrics"]["forbidden_anchor_rate"] <= 0.3
    assert report["comparisons"]["anchor_recall_lift_vs_minute"] >= 0.25
    assert report["comparisons"]["context_chars_ratio_vs_session"] <= 0.75


def test_event_adjacency_recovers_previous_and_next_evidence() -> None:
    _root, dataset_path, _contract = _paths()
    dataset = evaluation.load_dataset(dataset_path)
    cases = {case.id: case for case in dataset.cases}

    previous = evaluation._evaluate_case(cases["previous-cause"], "event_adjacent")
    next_outcome = evaluation._evaluate_case(cases["next-outcome"], "event_adjacent")

    assert previous["returned_unit_ids"] == ["pc-e1", "pc-e2", "pc-e3"]
    assert previous["answerable"] is True
    assert next_outcome["returned_unit_ids"] == ["no-e1", "no-e2"]
    assert next_outcome["answerable"] is True


def test_minute_baseline_cannot_join_split_evidence() -> None:
    _root, dataset_path, _contract = _paths()
    dataset = evaluation.load_dataset(dataset_path)
    case = next(item for item in dataset.cases if item.id == "grouped-root-cause")

    outcome = evaluation._evaluate_case(case, "minute")

    assert outcome["matched_unit_id"] == "grc-m1"
    assert len(outcome["required_anchor_hits"]) == 1
    assert outcome["answerable"] is False


def test_dataset_rejects_noncontiguous_event(tmp_path: Path) -> None:
    _root, dataset_path, _contract = _paths()
    payload = json.loads(dataset_path.read_text())
    observations = payload["cases"][0]["observations"]
    observations[3]["event_id"] = observations[0]["event_id"]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="event observations must be contiguous"):
        evaluation.load_dataset(broken)
