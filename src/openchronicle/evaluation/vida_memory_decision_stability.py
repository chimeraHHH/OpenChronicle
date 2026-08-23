"""Aggregate repeated memory-decision runs into an explicit stability report."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_METRIC_KEYS = (
    "parse_success_rate",
    "operation_precision",
    "operation_recall",
    "operation_f1",
    "target_binding_accuracy",
    "value_accuracy",
    "provenance_support_rate",
    "noop_accuracy",
    "operation_tp",
    "operation_fp",
    "operation_fn",
)


def aggregate_reports(
    *,
    report_paths: list[Path],
    contract_path: Path,
    repository_root: Path,
) -> dict[str, Any]:
    if not 3 <= len(report_paths) <= 20:
        raise ValueError("stability aggregation requires 3–20 runs")
    reports = [_load_report(path) for path in report_paths]
    contract_bytes = contract_path.read_bytes()
    try:
        contract = json.loads(contract_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("stability metric contract is not valid JSON") from exc
    _validate_contract(contract, run_count=len(reports))
    identity = _shared_identity(reports)

    metric_stats = {
        key: _stats([_number(report["variant"]["metrics"].get(key), key) for report in reports])
        for key in _METRIC_KEYS
    }
    gate_passes = [bool(report["variant"]["gate_verdict"].get("passed")) for report in reports]
    per_case_agreement = _per_case_agreement(reports)
    exact_case_agreement_rate = round(
        sum(item["exact"]["unique_signature_count"] == 1 for item in per_case_agreement)
        / len(per_case_agreement),
        6,
    )
    structural_case_agreement_rate = round(
        sum(item["structural"]["unique_signature_count"] == 1 for item in per_case_agreement)
        / len(per_case_agreement),
        6,
    )
    evidence_case_agreement_rate = round(
        sum(item["evidence"]["unique_signature_count"] == 1 for item in per_case_agreement)
        / len(per_case_agreement),
        6,
    )
    run_signatures = [
        {
            "run": index,
            "structural": _run_signature(report, _structural_signature),
            "evidence": _run_signature(report, _evidence_signature),
            "exact": _run_signature(report, _exact_signature),
        }
        for index, report in enumerate(reports, start=1)
    ]
    metrics = {
        "run_count": len(reports),
        "run_gate_pass_rate": round(sum(gate_passes) / len(gate_passes), 6),
        "all_runs_gate_passed": all(gate_passes),
        "exact_case_decision_agreement_rate": exact_case_agreement_rate,
        "structural_case_decision_agreement_rate": structural_case_agreement_rate,
        "evidence_case_decision_agreement_rate": evidence_case_agreement_rate,
        "unique_run_signature_count": len({item["exact"] for item in run_signatures}),
        "unique_structural_run_signature_count": len(
            {item["structural"] for item in run_signatures}
        ),
        "unique_evidence_run_signature_count": len(
            {item["evidence"] for item in run_signatures}
        ),
        "operation_f1_min": metric_stats["operation_f1"]["min"],
        "operation_f1_max": metric_stats["operation_f1"]["max"],
        "operation_f1_stdev": metric_stats["operation_f1"]["stdev"],
        "operation_recall_min": metric_stats["operation_recall"]["min"],
        "operation_precision_min": metric_stats["operation_precision"]["min"],
        "provenance_support_rate_min": metric_stats["provenance_support_rate"]["min"],
        "operation_fp_max": metric_stats["operation_fp"]["max"],
        "provider_or_parse_failure_count": sum(
            int(report["variant"]["metrics"]["failure_stage_counts"][stage])
            for report in reports
            for stage in ("provider_error", "parse_error")
        ),
    }
    verdict = _gate_verdict(metrics, contract["gates"])
    return {
        "schema_version": 1,
        "evaluation_id": contract["evaluation_id"],
        "repository": _repository_state(repository_root),
        "source_evaluation": identity,
        "metric_contract": {
            "path": str(contract_path.relative_to(repository_root)),
            "sha256": hashlib.sha256(contract_bytes).hexdigest(),
        },
        "run_count": len(reports),
        "run_report_sha256": [
            {"run": index, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for index, path in enumerate(report_paths, start=1)
        ],
        "metric_stats": metric_stats,
        "metrics": metrics,
        "per_case_agreement": per_case_agreement,
        "run_signatures": run_signatures,
        "gate_verdict": verdict,
    }


def write_report(report: dict[str, Any], output: Path | None) -> str:
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    return encoded


def _load_report(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_bytes())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid decision report: {path}") from exc
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 1
        or report.get("evaluation_id") != "vida-memory-decisions-v1"
        or not isinstance(report.get("repository"), dict)
        or not isinstance(report.get("dataset"), dict)
        or not isinstance(report.get("metric_contract"), dict)
        or not isinstance(report.get("variant"), dict)
        or not isinstance(report["variant"].get("metrics"), dict)
        or not isinstance(report["variant"].get("cases"), list)
    ):
        raise ValueError(f"unsupported decision report: {path}")
    if report["repository"].get("dirty") is not False:
        raise ValueError("stability inputs must bind clean repository states")
    return report


def _shared_identity(reports: list[dict[str, Any]]) -> dict[str, Any]:
    first = reports[0]
    identity = {
        "evaluation_id": first["evaluation_id"],
        "repository_commit": first["repository"].get("commit"),
        "dataset": first["dataset"],
        "metric_contract": first["metric_contract"],
        "variant_id": first["variant"].get("id"),
        "provider": first["variant"].get("provider"),
        "action_capability": first["variant"].get("action_capability"),
    }
    if identity["action_capability"] != "none":
        raise ValueError("stability inputs must be inert model-decision runs")
    for report in reports[1:]:
        candidate = {
            "evaluation_id": report["evaluation_id"],
            "repository_commit": report["repository"].get("commit"),
            "dataset": report["dataset"],
            "metric_contract": report["metric_contract"],
            "variant_id": report["variant"].get("id"),
            "provider": report["variant"].get("provider"),
            "action_capability": report["variant"].get("action_capability"),
        }
        if candidate != identity:
            raise ValueError("stability reports do not share one frozen identity")
    return identity


def _per_case_agreement(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    case_ids = [str(item.get("case_id")) for item in reports[0]["variant"]["cases"]]
    if len(case_ids) != len(set(case_ids)) or not case_ids:
        raise ValueError("decision report case identities are invalid")
    output: list[dict[str, Any]] = []
    for case_id in case_ids:
        signatures: dict[str, list[str]] = {
            "structural": [],
            "evidence": [],
            "exact": [],
        }
        for report in reports:
            matches = [
                item for item in report["variant"]["cases"] if item.get("case_id") == case_id
            ]
            if len(matches) != 1:
                raise ValueError("decision reports do not share one case set")
            signatures["structural"].append(_structural_signature(matches[0]))
            signatures["evidence"].append(_evidence_signature(matches[0]))
            signatures["exact"].append(_exact_signature(matches[0]))
        levels = {name: _agreement(values) for name, values in signatures.items()}
        output.append(
            {
                "case_id": case_id,
                **levels,
                # Backwards-compatible v1 aliases remain bound to the exact
                # complete output signature used by the original failed gate.
                "unique_signature_count": levels["exact"]["unique_signature_count"],
                "majority_fraction": levels["exact"]["majority_fraction"],
                "signature_counts": levels["exact"]["signature_counts"],
            }
        )
    return output


def _operations(case: dict[str, Any]) -> list[dict[str, Any]]:
    operations = case.get("predicted_operations")
    if not isinstance(operations, list) or not all(isinstance(item, dict) for item in operations):
        raise ValueError("decision case is missing predicted operations")
    return operations


def _structural_signature(case: dict[str, Any]) -> str:
    normalized_operations = [
        {"type": item.get("type"), "target_id": item.get("target_id")}
        for item in _operations(case)
    ]
    return _hash_json(normalized_operations)


def _evidence_signature(case: dict[str, Any]) -> str:
    normalized_operations = []
    for item in _operations(case):
        evidence = item.get("evidence_ids")
        if not isinstance(evidence, list) or not all(isinstance(value, str) for value in evidence):
            raise ValueError("decision operation evidence is invalid")
        normalized_operations.append(
            {
                "type": item.get("type"),
                "target_id": item.get("target_id"),
                "evidence_ids": sorted(set(evidence)),
            }
        )
    return _hash_json(normalized_operations)


def _exact_signature(case: dict[str, Any]) -> str:
    return _hash_json(_operations(case))


def _hash_json(value: object) -> str:
    normalized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _run_signature(report: dict[str, Any], signer) -> str:
    signatures = [
        {"case_id": item.get("case_id"), "signature": signer(item)}
        for item in report["variant"]["cases"]
    ]
    return _hash_json(signatures)


def _agreement(signatures: list[str]) -> dict[str, Any]:
    counts = Counter(signatures)
    return {
        "unique_signature_count": len(counts),
        "majority_fraction": round(max(counts.values()) / len(signatures), 6),
        "signature_counts": dict(sorted(counts.items())),
    }


def _stats(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "mean": round(statistics.fmean(values), 6),
        "stdev": round(statistics.pstdev(values), 6),
    }


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"decision metric is invalid: {label}")
    return float(value)


def _validate_contract(contract: object, *, run_count: int) -> None:
    common_gates = {
        "min_run_count",
        "min_run_gate_pass_rate",
        "min_operation_f1",
        "max_operation_f1_stdev",
        "min_operation_recall",
        "min_operation_precision",
        "min_provenance_support_rate",
        "max_operation_fp",
        "max_provider_or_parse_failure_count",
    }
    evaluation_id = contract.get("evaluation_id") if isinstance(contract, dict) else None
    agreement_gates = (
        {"min_exact_case_decision_agreement_rate"}
        if evaluation_id == "vida-memory-decision-stability-v1"
        else {
            "min_structural_case_decision_agreement_rate",
            "min_evidence_case_decision_agreement_rate",
        }
        if evaluation_id == "vida-memory-decision-stability-v2"
        else set()
    )
    if (
        not isinstance(contract, dict)
        or contract.get("schema_version") != 1
        or not agreement_gates
        or not isinstance(contract.get("gates"), dict)
        or set(contract["gates"]) != common_gates | agreement_gates
    ):
        raise ValueError("stability metric contract is invalid")
    gates = contract["gates"]
    if type(gates["min_run_count"]) is not int or not 3 <= gates["min_run_count"] <= run_count:
        raise ValueError("stability minimum run count is invalid")
    for key, value in gates.items():
        if key == "min_run_count":
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValueError("stability gate value is invalid")


def _gate_verdict(metrics: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "run_count": metrics["run_count"] >= gates["min_run_count"],
        "run_gate_pass_rate": (
            metrics["run_gate_pass_rate"] >= gates["min_run_gate_pass_rate"]
        ),
        "operation_f1": metrics["operation_f1_min"] >= gates["min_operation_f1"],
        "operation_f1_stdev": (
            metrics["operation_f1_stdev"] <= gates["max_operation_f1_stdev"]
        ),
        "operation_recall": (
            metrics["operation_recall_min"] >= gates["min_operation_recall"]
        ),
        "operation_precision": (
            metrics["operation_precision_min"] >= gates["min_operation_precision"]
        ),
        "provenance_support_rate": (
            metrics["provenance_support_rate_min"]
            >= gates["min_provenance_support_rate"]
        ),
        "operation_fp": metrics["operation_fp_max"] <= gates["max_operation_fp"],
        "provider_or_parse_failures": (
            metrics["provider_or_parse_failure_count"]
            <= gates["max_provider_or_parse_failure_count"]
        ),
    }
    if "min_exact_case_decision_agreement_rate" in gates:
        checks["exact_case_decision_agreement_rate"] = (
            metrics["exact_case_decision_agreement_rate"]
            >= gates["min_exact_case_decision_agreement_rate"]
        )
    else:
        checks["structural_case_decision_agreement_rate"] = (
            metrics["structural_case_decision_agreement_rate"]
            >= gates["min_structural_case_decision_agreement_rate"]
        )
        checks["evidence_case_decision_agreement_rate"] = (
            metrics["evidence_case_decision_agreement_rate"]
            >= gates["min_evidence_case_decision_agreement_rate"]
        )
    return {"passed": all(checks.values()), "checks": checks, "gates": gates}


def _repository_state(repository_root: Path) -> dict[str, Any]:
    commit = _git(repository_root, "rev-parse", "HEAD")
    status = _git(repository_root, "status", "--porcelain")
    return {"commit": commit, "dirty": bool(status), "status_lines": len(status.splitlines())}


def _git(repository_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("unable to record repository identity")
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    contract_path = args.contract or (
        repository_root
        / "benchmarks"
        / "vida-memory-decisions-v1"
        / "json"
        / "official_memops_stability_contract.json"
    )
    if not contract_path.is_absolute():
        contract_path = repository_root / contract_path
    report = aggregate_reports(
        report_paths=args.reports,
        contract_path=contract_path,
        repository_root=repository_root,
    )
    encoded = write_report(report, args.output)
    if not args.quiet:
        sys.stdout.write(encoded)
    return 0 if report["gate_verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
