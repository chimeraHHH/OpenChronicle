from __future__ import annotations

import json
from pathlib import Path

from openchronicle.evaluation import vida_memory


def _paths() -> tuple[Path, Path, Path]:
    repository = Path(__file__).resolve().parents[1]
    root = repository / "benchmarks" / "vida-memory-v1"
    return repository, root / "fixtures" / "cases.json", root / "json" / "metric_contract.json"


def test_memory_baseline_runs_production_store_without_touching_user_root(
    tmp_path: Path, monkeypatch,
) -> None:
    repository, dataset, contract = _paths()
    user_root = tmp_path / "user-root"
    user_root.mkdir()
    sentinel = user_root / "must-remain.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    monkeypatch.setenv("OPENCHRONICLE_ROOT", str(user_root))

    report = vida_memory.run_evaluation(
        dataset_path=dataset,
        metric_contract_path=contract,
        repository_root=repository,
        latency_repeats=2,
    )

    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert not (user_root / "index.db").exists()
    assert report["dataset"]["case_count"] == 7
    assert report["dataset"]["entry_count"] == 6
    metrics = report["variant"]["metrics"]
    assert metrics["recall_at_k"] == 0.5
    assert metrics["mean_reciprocal_rank"] == 0.5
    assert metrics["semantic_recall_at_k"] == 0.0
    assert metrics["forbidden_hit_rate"] == 0.0
    assert metrics["abstention_accuracy"] == 1.0
    assert metrics["source_identity_coverage"] == 1.0
    assert report["variant"]["gate_verdict"]["passed"] is False


def test_memory_baseline_freezes_expected_case_failures() -> None:
    repository, dataset, contract = _paths()

    report = vida_memory.run_evaluation(
        dataset_path=dataset,
        metric_contract_path=contract,
        repository_root=repository,
        latency_repeats=1,
    )

    cases = {case["case_id"]: case for case in report["variant"]["cases"]}
    assert cases["exact-editor-preference"]["passed"] is True
    assert cases["current-model-excludes-stale"]["passed"] is True
    assert cases["entity-isolation-atlas"]["passed"] is True
    assert cases["abstain-unrecorded-passport"]["passed"] is True
    assert cases["semantic-editor-paraphrase"]["passed"] is False
    assert cases["cross-language-local-database"]["passed"] is False
    assert cases["historical-model-opt-in"]["passed"] is False


def test_memory_baseline_report_round_trips_json(tmp_path: Path) -> None:
    repository, dataset, contract = _paths()
    report = vida_memory.run_evaluation(
        dataset_path=dataset,
        metric_contract_path=contract,
        repository_root=repository,
        latency_repeats=1,
    )
    output = tmp_path / "report.json"

    encoded = vida_memory.write_report(report, output)

    assert json.loads(encoded) == report
    assert json.loads(output.read_text(encoding="utf-8")) == report
