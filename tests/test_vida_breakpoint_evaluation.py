from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle.evaluation.vida_breakpoints import (
    load_activity_dataset,
    run_breakpoint_evaluation,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = (
    REPOSITORY_ROOT / "benchmarks" / "vida-suggestions-v1" / "fixtures" / "activity_traces.json"
)


def test_activity_trace_fixture_is_closed_and_balanced() -> None:
    dataset = load_activity_dataset(DATASET_PATH)

    assert dataset.id == "OC-Vida-Activity-Traces-v1"
    assert len(dataset.cases) == 12
    assert sum(case.expected_display for case in dataset.cases) == 4
    assert {case.sample_state for case in dataset.cases} == {
        "healthy",
        "no_sample",
        "malformed",
        "backward",
    }


def test_breakpoint_threshold_sweep_exposes_timing_only_tradeoff(
    ac_root: Path,
) -> None:
    report = run_breakpoint_evaluation(
        dataset_path=DATASET_PATH,
        repository_root=REPOSITORY_ROOT,
        latency_repeats=1,
    )

    assert report["dataset"]["label_status"].startswith("synthetic_")
    assert report["experiment_status"]["formal_gate"] == ("blocked_unregistered_auxiliary")
    variants = report["variants"]
    assert variants["no_gate"]["metrics"]["confusion"] == {
        "true_positive": 4,
        "false_positive": 8,
        "false_negative": 0,
        "true_negative": 0,
    }
    assert variants["settle_20s"]["metrics"]["confusion"] == {
        "true_positive": 3,
        "false_positive": 1,
        "false_negative": 1,
        "true_negative": 7,
    }
    assert variants["settle_30s"]["metrics"]["confusion"] == {
        "true_positive": 2,
        "false_positive": 0,
        "false_negative": 2,
        "true_negative": 8,
    }
    assert variants["settle_20s"]["metrics"]["display_precision"] == 0.75
    assert variants["settle_30s"]["metrics"]["display_precision"] == 1.0
    assert variants["settle_30s"]["metrics"]["display_recall_at_snapshot"] == 0.5


def test_activity_trace_parser_rejects_unknown_fields(tmp_path: Path) -> None:
    payload = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    payload["cases"][0]["unexpected"] = True
    malformed = tmp_path / "malformed-activity.json"
    malformed.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="case schema"):
        load_activity_dataset(malformed)
