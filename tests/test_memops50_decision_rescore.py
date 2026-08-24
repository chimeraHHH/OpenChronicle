from __future__ import annotations

import json
from pathlib import Path

from openchronicle.evaluation import memops50_decision_rescore as rescore
from openchronicle.evaluation import vida_memory_decisions as decisions


def test_rescore_reuses_predictions_without_model_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    case = decisions.DecisionCase(
        id="case-one",
        targets=(decisions.Target(id="editor", description="preferred editor"),),
        current_memory=(),
        evidence=(
            decisions.EvidenceTurn(
                id="segment-1-turn-1",
                session_id="segment-1",
                role="user",
                content="My editor is Zed.",
            ),
        ),
        gold_operations=(
            decisions.Operation(
                type="remember",
                target_id="editor",
                old_value="",
                new_value="Zed",
                evidence_ids=("segment-1-turn-1",),
                new_value_anchors=("Zed",),
            ),
        ),
    )
    dataset = decisions.Dataset(
        id="MemOps-50-Official-Adjacent-Decisions-v1",
        split="official-adjacent-balanced-50",
        cases=(case,),
        digest="dataset-digest",
        source="official_memops_external",
        source_revision="a" * 40,
        source_files=(("case-one.json", "b" * 64),),
    )
    monkeypatch.setattr(decisions, "load_memops_manifest", lambda *_args, **_kwargs: dataset)
    monkeypatch.setattr(
        decisions,
        "_repository_state",
        lambda _root: {"commit": "c" * 40, "dirty": False, "status_lines": 0},
    )

    source_report = {
        "repository": {"commit": "d" * 40, "dirty": False, "status_lines": 0},
        "environment": {"python": "3.11"},
        "dataset": {
            "id": dataset.id,
            "split": dataset.split,
            "sha256": dataset.digest,
            "source_revision": dataset.source_revision,
            "source_files": [{"file": "case-one.json", "sha256": "b" * 64}],
        },
        "variant": {
            "provider": {"provider": "codex_cli", "model": "gpt-5.6-sol"},
            "cases": [
                {
                    "case_id": "case-one",
                    "predicted_operations": [
                        {
                            "type": "remember",
                            "target_id": "editor",
                            "old_value": "",
                            "new_value": "Zed",
                            "evidence_ids": ["segment-1-turn-1"],
                        }
                    ],
                    "response_sha256": "e" * 64,
                    "response_chars": 100,
                    "latency_ms": 42.0,
                }
            ],
        },
    }
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(source_report), encoding="utf-8")
    contract = {
        "schema_version": 1,
        "primary_metric": "operation_f1",
        "dataset": {"id": dataset.id, "split": dataset.split},
        "gates": {
            "parse_success_rate_min": 1.0,
            "operation_precision_min": 1.0,
            "operation_recall_min": 1.0,
            "target_binding_accuracy_min": 1.0,
            "value_accuracy_min": 1.0,
            "provenance_support_rate_min": 1.0,
            "noop_accuracy_min": 1.0,
        },
    }
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    report = rescore.rescore_report(
        source_report_path=source_path,
        decision_manifest_path=tmp_path / "manifest.json",
        memops_root=tmp_path,
        metric_contract_path=contract_path,
        repository_root=tmp_path,
    )

    assert report["variant"]["rescore"]["model_call_count"] == 0
    assert report["variant"]["metrics"]["operation_f1"] == 1.0
    assert report["variant"]["metrics"]["provenance_support_rate"] == 1.0
    assert report["variant"]["cases"][0]["response_sha256"] == "e" * 64
    assert report["variant"]["cases"][0]["latency_ms"] == 42.0
