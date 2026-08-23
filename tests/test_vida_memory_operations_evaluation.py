from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle.evaluation import vida_memory_operations


def _paths() -> tuple[Path, Path, Path]:
    repository = Path(__file__).resolve().parents[1]
    root = repository / "benchmarks" / "vida-memory-ops-v1"
    return repository, root / "fixtures" / "traces.json", root / "json" / "metric_contract.json"


def test_memory_operation_baseline_runs_production_lifecycle_in_isolation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, dataset, contract = _paths()
    user_root = tmp_path / "user-root"
    user_root.mkdir()
    sentinel = user_root / "must-remain.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    monkeypatch.setenv("OPENCHRONICLE_ROOT", str(user_root))

    report = vida_memory_operations.run_evaluation(
        dataset_path=dataset,
        metric_contract_path=contract,
        repository_root=repository,
    )

    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert not (user_root / "index.db").exists()
    assert report["dataset"]["trace_count"] == 3
    assert report["dataset"]["operation_count"] == 7
    assert report["dataset"]["checkpoint_count"] == 5
    assert report["variant"]["gate_verdict"]["passed"] is True
    assert report["variant"]["metrics"] == {
        "trace_pass_rate": 1.0,
        "operation_success_rate": 1.0,
        "checkpoint_pass_rate": 1.0,
        "stale_value_rate": 0.0,
        "forget_leakage_rate": 0.0,
        "over_forget_rate": 0.0,
        "provenance_support_rate": 1.0,
        "trajectory_order_accuracy": 1.0,
    }


def test_memory_operation_baseline_records_lifecycle_diagnostics() -> None:
    repository, dataset, contract = _paths()

    report = vida_memory_operations.run_evaluation(
        dataset_path=dataset,
        metric_contract_path=contract,
        repository_root=repository,
    )

    traces = {trace["trace_id"]: trace for trace in report["variant"]["traces"]}
    update_trace = traces["remember-update-preserves-history"]
    update = next(
        checkpoint
        for checkpoint in update_trace["checkpoints"]
        if checkpoint["checkpoint_id"] == "new-model-is-current-old-remains-history"
    )
    assert update["current_keys"] == ["timeline-model-current"]
    assert update["history_keys"] == ["timeline-model-current", "timeline-model-old"]
    assert update["forbidden_current_present"] == []

    lineage_forget = next(
        checkpoint
        for checkpoint in update_trace["checkpoints"]
        if checkpoint["checkpoint_id"] == "forget-current-model-removes-complete-lineage"
    )
    assert lineage_forget["current_keys"] == []
    assert lineage_forget["history_keys"] == []
    assert lineage_forget["forbidden_history_present"] == []

    forget = traces["forget-target-without-over-forget"]["checkpoints"][0]
    assert forget["current_keys"] == ["report-style"]
    assert forget["history_keys"] == ["report-style"]
    assert forget["forbidden_history_present"] == []

    reflection = traces["reflect-requires-independent-support"]["operations"][0]
    assert reflection["claim_source_count"] == 2
    assert reflection["provenance_support_valid"] is True


def test_memory_operation_fixture_rejects_reflection_with_one_source(tmp_path: Path) -> None:
    _, dataset, _ = _paths()
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    reflect = payload["traces"][2]["operations"][0]
    reflect["source_keys"] = reflect["source_keys"][:1]
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="two sources"):
        vida_memory_operations.load_dataset(malformed)


def test_memory_operation_report_round_trips_json(tmp_path: Path) -> None:
    repository, dataset, contract = _paths()
    report = vida_memory_operations.run_evaluation(
        dataset_path=dataset,
        metric_contract_path=contract,
        repository_root=repository,
    )
    output = tmp_path / "report.json"

    encoded = vida_memory_operations.write_report(report, output)

    assert json.loads(encoded) == report
    assert json.loads(output.read_text(encoding="utf-8")) == report
