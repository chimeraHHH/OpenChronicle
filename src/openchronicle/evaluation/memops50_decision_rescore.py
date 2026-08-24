"""Deterministically rescore immutable MemOps-50 decision predictions."""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from typing import Any

from . import memops50
from . import vida_memory_decisions as decisions


def rescore_report(
    *,
    source_report_path: Path,
    decision_manifest_path: Path,
    memops_root: Path,
    metric_contract_path: Path,
    repository_root: Path,
) -> dict[str, Any]:
    """Replay stored predictions against the current deterministic scorer."""
    source_bytes = source_report_path.read_bytes()
    source = memops50._load_json(source_bytes, label="source decision report")
    if not isinstance(source, dict) or not isinstance(source.get("variant"), dict):
        raise ValueError("source decision report is invalid")
    source_dataset = source.get("dataset")
    source_cases = source["variant"].get("cases")
    source_provider = source["variant"].get("provider")
    if (
        not isinstance(source_dataset, dict)
        or not isinstance(source_cases, list)
        or not isinstance(source_provider, dict)
    ):
        raise ValueError("source decision report is incomplete")

    dataset = decisions.load_memops_manifest(
        decision_manifest_path,
        memops_root=memops_root,
    )
    expected_sources = [
        {"file": file_name, "sha256": digest}
        for file_name, digest in dataset.source_files
    ]
    if (
        source_dataset.get("id") != dataset.id
        or source_dataset.get("split") != dataset.split
        or source_dataset.get("sha256") != dataset.digest
        or source_dataset.get("source_revision") != dataset.source_revision
        or source_dataset.get("source_files") != expected_sources
    ):
        raise ValueError("source report does not match the verified MemOps-50 dataset")

    source_by_id: dict[str, dict[str, Any]] = {}
    for row in source_cases:
        if not isinstance(row, dict) or not isinstance(row.get("case_id"), str):
            raise ValueError("source report case is invalid")
        if row["case_id"] in source_by_id:
            raise ValueError("source report case ids are not unique")
        source_by_id[row["case_id"]] = row
    if set(source_by_id) != {case.id for case in dataset.cases}:
        raise ValueError("source report case coverage does not match MemOps-50")

    outcomes: list[dict[str, Any]] = []
    for case in dataset.cases:
        source_row = source_by_id[case.id]
        predictions = source_row.get("predicted_operations")
        if not isinstance(predictions, list):
            raise ValueError("source report predictions are missing")
        outcome = decisions._evaluate_case(
            case,
            lambda _case, values=predictions: {
                "schema_version": 1,
                "operations": values,
            },
        )
        for key in ("response_sha256", "response_chars", "latency_ms"):
            if key in source_row:
                outcome[key] = source_row[key]
        outcomes.append(outcome)

    contract_bytes = metric_contract_path.read_bytes()
    contract = memops50._load_json(contract_bytes, label="decision metric contract")
    decisions._validate_contract(contract, dataset)
    metrics = decisions._metrics(outcomes)
    repository = decisions._repository_state(repository_root)
    return {
        "schema_version": 1,
        "evaluation_id": "vida-memory-decisions-v1",
        "repository": repository,
        "environment": {
            **(source.get("environment") if isinstance(source.get("environment"), dict) else {}),
            "rescore_python": platform.python_version(),
        },
        "dataset": {
            **source_dataset,
            "gold_operation_count": sum(
                len(case.gold_operations) for case in dataset.cases
            ),
        },
        "metric_contract": {
            "sha256": memops50.sha256_bytes(contract_bytes),
            "path": str(metric_contract_path.relative_to(repository_root)),
        },
        "variant": {
            "id": "configured_model_memory_decision_json_trigger_provenance_rescore_v2",
            "provider": source_provider,
            "action_capability": "none",
            "rescore": {
                "model_call_count": 0,
                "source_report_path": str(source_report_path.relative_to(repository_root)),
                "source_report_sha256": memops50.sha256_bytes(source_bytes),
                "source_repository": source.get("repository"),
                "change": (
                    "non-reflect gold provenance uses the exact trigger_span; "
                    "reflect keeps its complete independent evidence_spans"
                ),
            },
            "metrics": metrics,
            "gate_verdict": decisions._gate_verdict(metrics, contract["gates"]),
            "cases": outcomes,
        },
    }


def write_report(report: dict[str, Any], output: Path | None = None) -> str:
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    return encoded


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--memops-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    benchmark_root = repository_root / "benchmarks" / "memops50-lifecycle-v1"
    manifest_path = args.manifest or benchmark_root / "json" / "decision_manifest.json"
    contract_path = args.contract or benchmark_root / "json" / "decision_metric_contract.json"
    source_report_path = args.source_report
    for name, value in (
        ("manifest", manifest_path),
        ("contract", contract_path),
        ("source report", source_report_path),
    ):
        if not value.is_absolute():
            resolved = repository_root / value
            if name == "manifest":
                manifest_path = resolved
            elif name == "contract":
                contract_path = resolved
            else:
                source_report_path = resolved
    report = rescore_report(
        source_report_path=source_report_path,
        decision_manifest_path=manifest_path,
        memops_root=args.memops_root,
        metric_contract_path=contract_path,
        repository_root=repository_root,
    )
    encoded = write_report(report, args.output)
    if not args.quiet:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
