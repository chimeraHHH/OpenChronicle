"""Frozen deterministic evaluation for the Résumé Rescue source contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..local_time import local_timezone
from ..provenance.models import canonical_digest
from ..resume_rescue.models import (
    ResumeSchemaError,
    build_exact_artifact,
    opportunity_digest,
    profile_digest,
    validate_artifact,
    validate_opportunity,
    validate_profile,
    validate_projection_request,
)

MAX_DATASET_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Expected:
    admission: str
    selected_fact_ids: tuple[str, ...]
    missing_requirement_ids: tuple[str, ...]
    candidate_requirement_ids: tuple[str, ...]
    warning_fragments: tuple[str, ...]
    forbidden_artifact_fragments: tuple[str, ...]
    attack_success_fragments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResumeRescueCase:
    id: str
    category: str
    profile: dict[str, Any]
    opportunity: dict[str, Any]
    request: dict[str, Any]
    expected: Expected


@dataclass(frozen=True, slots=True)
class Dataset:
    id: str
    split: str
    cases: tuple[ResumeRescueCase, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class CorpusCase:
    case_id: str
    admission: str
    artifact: dict[str, Any] | None
    error_code: str
    deterministic: bool


def load_dataset(path: Path) -> Dataset:
    raw_bytes = _bounded_read(path, MAX_DATASET_BYTES, "Résumé Rescue dataset")
    payload = _json_object(raw_bytes, "Résumé Rescue dataset")
    if set(payload) != {"schema_version", "dataset_id", "split", "cases"}:
        raise ValueError("Résumé Rescue dataset envelope is invalid")
    if payload["schema_version"] != 1:
        raise ValueError("Résumé Rescue dataset schema is unsupported")
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > 200:
        raise ValueError("Résumé Rescue dataset cases are invalid")
    cases = tuple(_parse_case(value) for value in raw_cases)
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Résumé Rescue case IDs must be unique")
    return Dataset(
        id=_text(payload["dataset_id"], 200),
        split=_text(payload["split"], 200),
        cases=cases,
        digest=hashlib.sha256(raw_bytes).hexdigest(),
    )


def production_corpus(dataset: Dataset) -> tuple[CorpusCase, ...]:
    return tuple(_run_case(case, request=case.request) for case in dataset.cases)


def base_profile_corpus(dataset: Dataset) -> tuple[CorpusCase, ...]:
    rows: list[CorpusCase] = []
    for case in dataset.cases:
        admitted = _run_case(case, request=case.request)
        if admitted.admission == "rejected":
            rows.append(admitted)
            continue
        try:
            profile = validate_profile(case.profile)
            opportunity = validate_opportunity(case.opportunity)
            request = validate_projection_request(case.request)
        except ResumeSchemaError:
            rows.append(CorpusCase(case.id, "rejected", None, "invalid_input", True))
            continue
        conflict_ids = {
            fact_id for conflict in profile["conflicts"] for fact_id in conflict["fact_ids"]
        }
        by_section: dict[str, list[str]] = {}
        for fact in profile["facts"]:
            if fact["id"] not in conflict_ids:
                by_section.setdefault(fact["section"], []).append(fact["id"])
        baseline_request = {
            "schema_version": 1,
            "sections": [
                {"kind": kind, "fact_ids": fact_ids} for kind, fact_ids in by_section.items()
            ],
            "requirements": [
                {"id": item["id"], "text": item["text"], "fact_ids": []}
                for item in request["requirements"]
            ],
        }
        baseline_case = ResumeRescueCase(
            id=case.id,
            category=case.category,
            profile=profile,
            opportunity=opportunity,
            request=baseline_request,
            expected=case.expected,
        )
        rows.append(_run_case(baseline_case, request=baseline_request))
    return tuple(rows)


def run_evaluation(
    *,
    dataset_path: Path,
    metric_contract_path: Path,
    repository_root: Path,
) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    contract_bytes = _bounded_read(
        metric_contract_path, MAX_DATASET_BYTES, "Résumé Rescue metric contract"
    )
    contract = _json_object(contract_bytes, "Résumé Rescue metric contract")
    _validate_contract(contract, dataset)
    corpora = {
        "base_profile": base_profile_corpus(dataset),
        "deterministic_exact_projection": production_corpus(dataset),
    }
    variants = {}
    for name, corpus in corpora.items():
        evaluated = _evaluate_corpus(dataset, corpus)
        evaluated["gate_verdict"] = _gate_verdict(evaluated["metrics"], contract)
        variants[name] = evaluated
    return {
        "schema_version": 1,
        "evaluation_id": "vida-resume-rescue-v1",
        "generated_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        "repository": _repository_state(repository_root),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "timezone": str(local_timezone()),
        },
        "dataset": {
            "id": dataset.id,
            "split": dataset.split,
            "case_count": len(dataset.cases),
            "sha256": dataset.digest,
            "path": str(dataset_path.relative_to(repository_root)),
        },
        "metric_contract": {
            "sha256": hashlib.sha256(contract_bytes).hexdigest(),
            "path": str(metric_contract_path.relative_to(repository_root)),
        },
        "variants": variants,
        "baseline_status": {
            "comparator": "base_profile",
            "local_verification": "trusted_with_caveats",
            "formal_gate": "blocked_unregistered",
            "reason": (
                "The comparator is a deterministic safe base-profile projection, not Vida, "
                "an ATS, a hiring-outcome model, or an independently registered baseline."
            ),
        },
    }


def _run_case(case: ResumeRescueCase, *, request: dict[str, Any]) -> CorpusCase:
    try:
        profile = validate_profile(case.profile)
        opportunity = validate_opportunity(case.opportunity)
        normalized_request = validate_projection_request(request)
        first = build_exact_artifact(
            profile=profile,
            profile_version=1,
            profile_digest_value=profile_digest(profile),
            opportunity=opportunity,
            opportunity_id=f"resume-opportunity-{opportunity_digest(opportunity)[:32]}",
            opportunity_digest_value=opportunity_digest(opportunity),
            request=normalized_request,
        )
        second = build_exact_artifact(
            profile=profile,
            profile_version=1,
            profile_digest_value=profile_digest(profile),
            opportunity=opportunity,
            opportunity_id=f"resume-opportunity-{opportunity_digest(opportunity)[:32]}",
            opportunity_digest_value=opportunity_digest(opportunity),
            request=normalized_request,
        )
        return CorpusCase(case.id, "accepted", first, "", first == second)
    except ResumeSchemaError:
        return CorpusCase(case.id, "rejected", None, "invalid_input", True)


def _evaluate_corpus(dataset: Dataset, corpus: tuple[CorpusCase, ...]) -> dict[str, Any]:
    by_id = {row.case_id: row for row in corpus}
    if set(by_id) != {case.id for case in dataset.cases}:
        raise ValueError("Résumé Rescue corpus case coverage differs")
    outcomes = [_evaluate_case(case, by_id[case.id]) for case in dataset.cases]
    accepted = [row for row in outcomes if row["expected_admission"] == "accepted"]
    rejected = [row for row in outcomes if row["expected_admission"] == "rejected"]
    metrics = {
        "case_pass_rate": _rate(sum(row["passed"] for row in outcomes), len(outcomes)),
        "admission_accuracy": _rate(
            sum(row["admission_correct"] for row in outcomes), len(outcomes)
        ),
        "invalid_rejection_rate": _rate(
            sum(row["admission_correct"] for row in rejected), len(rejected)
        ),
        "schema_valid_rate": _rate(sum(row["schema_valid"] for row in accepted), len(accepted)),
        "exact_fact_preservation_rate": _rate(
            sum(row["exact_fact_preservation"] for row in accepted), len(accepted)
        ),
        "fact_ledger_integrity_rate": _rate(
            sum(row["fact_ledger_integrity"] for row in accepted), len(accepted)
        ),
        "selected_fact_accuracy": _rate(
            sum(row["selected_facts_correct"] for row in accepted), len(accepted)
        ),
        "missing_evidence_accuracy": _rate(
            sum(row["missing_evidence_correct"] for row in accepted), len(accepted)
        ),
        "candidate_mapping_accuracy": _rate(
            sum(row["candidate_mapping_correct"] for row in accepted), len(accepted)
        ),
        "warning_coverage_rate": _rate(
            sum(row["warnings_correct"] for row in accepted), len(accepted)
        ),
        "exclusion_ledger_rate": _rate(
            sum(row["exclusions_correct"] for row in accepted), len(accepted)
        ),
        "conflict_ledger_rate": _rate(
            sum(row["conflicts_correct"] for row in accepted), len(accepted)
        ),
        "determinism_rate": _rate(sum(row["deterministic"] for row in accepted), len(accepted)),
        "action_capability_violation_count": sum(
            row["action_capability_violation"] for row in accepted
        ),
        "verified_support_overclaim_count": sum(
            row["verified_support_overclaim"] for row in accepted
        ),
        "forbidden_artifact_rate": _rate(
            sum(row["forbidden_artifact"] for row in accepted), len(accepted)
        ),
        "injection_override_rate": _rate(
            sum(row["injection_override"] for row in accepted), len(accepted)
        ),
    }
    return {
        "case_count": len(outcomes),
        "accepted_case_count": len(accepted),
        "rejected_case_count": len(rejected),
        "corpus_digest": canonical_digest(
            {
                "schema": "resume-rescue-corpus-v1",
                "cases": [
                    {
                        "case_id": row.case_id,
                        "admission": row.admission,
                        "artifact": row.artifact,
                        "error_code": row.error_code,
                        "deterministic": row.deterministic,
                    }
                    for row in corpus
                ],
            }
        ),
        "metrics": metrics,
        "cases": outcomes,
    }


def _evaluate_case(case: ResumeRescueCase, row: CorpusCase) -> dict[str, Any]:
    expected = case.expected
    admission_correct = row.admission == expected.admission
    result: dict[str, Any] = {
        "case_id": case.id,
        "category": case.category,
        "expected_admission": expected.admission,
        "actual_admission": row.admission,
        "error_code": row.error_code,
        "admission_correct": admission_correct,
        "deterministic": row.deterministic,
        "schema_valid": False,
        "exact_fact_preservation": False,
        "fact_ledger_integrity": False,
        "selected_facts_correct": False,
        "missing_evidence_correct": False,
        "candidate_mapping_correct": False,
        "warnings_correct": False,
        "exclusions_correct": False,
        "conflicts_correct": False,
        "action_capability_violation": False,
        "verified_support_overclaim": False,
        "forbidden_artifact": False,
        "injection_override": False,
    }
    if expected.admission == "rejected":
        result["passed"] = bool(admission_correct and row.artifact is None)
        return result
    if row.admission != "accepted" or row.artifact is None:
        result["passed"] = False
        return result
    try:
        artifact = validate_artifact(row.artifact)
        profile = validate_profile(case.profile)
    except ResumeSchemaError:
        result["passed"] = False
        return result
    result["schema_valid"] = True
    source_facts = {item["id"]: item for item in profile["facts"]}
    output_items = [item for section in artifact["sections"] for item in section["items"]]
    selected_ids = [item["fact_id"] for item in output_items]
    result["exact_fact_preservation"] = all(
        item["fact_id"] in source_facts
        and item["text"] == source_facts[item["fact_id"]]["text"]
        and item["transformation"] == "selected_exact"
        for item in output_items
    )
    result["fact_ledger_integrity"] = all(
        item["fact_id"] in source_facts
        and item["provenance"] == source_facts[item["fact_id"]]["provenance"]
        and item["confidentiality"] == source_facts[item["fact_id"]]["confidentiality"]
        and item["ownership_scope"] == source_facts[item["fact_id"]]["ownership_scope"]
        for item in output_items
    )
    result["selected_facts_correct"] = selected_ids == list(expected.selected_fact_ids)
    coverage = artifact["requirement_coverage"]
    missing_ids = [item["id"] for item in coverage if item["status"] == "missing_evidence"]
    candidate_ids = [item["id"] for item in coverage if item["status"] == "candidate_supported"]
    result["missing_evidence_correct"] = missing_ids == list(expected.missing_requirement_ids)
    result["candidate_mapping_correct"] = candidate_ids == list(expected.candidate_requirement_ids)
    warning_text = "\n".join(artifact["warnings"]).casefold()
    result["warnings_correct"] = all(
        fragment.casefold() in warning_text for fragment in expected.warning_fragments
    )
    result["exclusions_correct"] = artifact["excluded_fact_ids"] == [
        fact["id"] for fact in profile["facts"] if fact["id"] not in set(selected_ids)
    ]
    result["conflicts_correct"] = artifact["conflicts"] == profile["conflicts"]
    result["action_capability_violation"] = artifact["action_capability"] != "none"
    result["verified_support_overclaim"] = any(
        item["support_assurance"] not in {"manual_mapping_unverified", "no_evidence"}
        or item["status"] not in {"candidate_supported", "missing_evidence"}
        for item in coverage
    )
    serialized = json.dumps(artifact, ensure_ascii=False, sort_keys=True).casefold()
    result["forbidden_artifact"] = any(
        fragment.casefold() in serialized for fragment in expected.forbidden_artifact_fragments
    )
    result["injection_override"] = any(
        fragment.casefold() in serialized for fragment in expected.attack_success_fragments
    )
    result["passed"] = bool(
        admission_correct
        and result["deterministic"]
        and result["schema_valid"]
        and result["exact_fact_preservation"]
        and result["fact_ledger_integrity"]
        and result["selected_facts_correct"]
        and result["missing_evidence_correct"]
        and result["candidate_mapping_correct"]
        and result["warnings_correct"]
        and result["exclusions_correct"]
        and result["conflicts_correct"]
        and not result["action_capability_violation"]
        and not result["verified_support_overclaim"]
        and not result["forbidden_artifact"]
        and not result["injection_override"]
    )
    return result


def _parse_case(value: object) -> ResumeRescueCase:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "category",
        "profile",
        "opportunity",
        "request",
        "expected",
    }:
        raise ValueError("Résumé Rescue case envelope is invalid")
    expected = value["expected"]
    expected_fields = {
        "admission",
        "selected_fact_ids",
        "missing_requirement_ids",
        "candidate_requirement_ids",
        "warning_fragments",
        "forbidden_artifact_fragments",
        "attack_success_fragments",
    }
    if not isinstance(expected, dict) or set(expected) != expected_fields:
        raise ValueError("Résumé Rescue expected outcome is invalid")
    admission = expected["admission"]
    if admission not in {"accepted", "rejected"}:
        raise ValueError("Résumé Rescue expected admission is invalid")
    profile = value["profile"]
    opportunity = value["opportunity"]
    request = value["request"]
    if (
        not isinstance(profile, dict)
        or not isinstance(opportunity, dict)
        or not isinstance(request, dict)
    ):
        raise ValueError("Résumé Rescue case source is invalid")
    return ResumeRescueCase(
        id=_text(value["id"], 200),
        category=_text(value["category"], 200),
        profile=profile,
        opportunity=opportunity,
        request=request,
        expected=Expected(
            admission=admission,
            selected_fact_ids=_string_tuple(expected["selected_fact_ids"]),
            missing_requirement_ids=_string_tuple(expected["missing_requirement_ids"]),
            candidate_requirement_ids=_string_tuple(expected["candidate_requirement_ids"]),
            warning_fragments=_string_tuple(expected["warning_fragments"]),
            forbidden_artifact_fragments=_string_tuple(expected["forbidden_artifact_fragments"]),
            attack_success_fragments=_string_tuple(expected["attack_success_fragments"]),
        ),
    )


def _validate_contract(contract: dict[str, Any], dataset: Dataset) -> None:
    if (
        set(contract)
        != {
            "schema_version",
            "dataset",
            "primary_metric",
            "safety_gates",
            "quality_gates",
        }
        or contract.get("schema_version") != 1
    ):
        raise ValueError("Résumé Rescue metric contract is invalid")
    expected_dataset = contract.get("dataset")
    if expected_dataset != {"id": dataset.id, "split": dataset.split}:
        raise ValueError("Résumé Rescue metric contract dataset differs")
    if contract.get("primary_metric") != "case_pass_rate":
        raise ValueError("Résumé Rescue primary metric is invalid")
    for group in ("safety_gates", "quality_gates"):
        gates = contract.get(group)
        if not isinstance(gates, dict) or not gates:
            raise ValueError("Résumé Rescue metric gates are invalid")
        for metric, rule in gates.items():
            if not isinstance(metric, str) or not isinstance(rule, dict) or len(rule) != 1:
                raise ValueError("Résumé Rescue metric gate is invalid")
            operator, threshold = next(iter(rule.items()))
            if operator not in {"minimum", "maximum"} or not isinstance(threshold, (int, float)):
                raise ValueError("Résumé Rescue metric threshold is invalid")


def _gate_verdict(metrics: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    failures = []
    for group in ("safety_gates", "quality_gates"):
        for metric, rule in contract[group].items():
            if metric not in metrics:
                raise ValueError(f"Résumé Rescue metric is missing: {metric}")
            operator, threshold = next(iter(rule.items()))
            value = metrics[metric]
            passed = value >= threshold if operator == "minimum" else value <= threshold
            if not passed:
                failures.append(
                    {
                        "group": group,
                        "metric": metric,
                        "operator": operator,
                        "threshold": threshold,
                        "actual": value,
                    }
                )
    return {"passed": not failures, "failures": failures}


def _repository_state(root: Path) -> dict[str, Any]:
    def command(*args: str) -> str:
        completed = subprocess.run(
            args,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.stdout.strip()

    try:
        commit = command("git", "rev-parse", "HEAD")
        dirty = bool(command("git", "status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"
        dirty = True
    return {"commit": commit, "dirty": dirty}


def _bounded_read(path: Path, maximum: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"{label} cannot be read") from exc
    if size < 1 or size > maximum:
        raise ValueError(f"{label} size is invalid")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} cannot be read") from exc


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError("Résumé Rescue fixture text is invalid")
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError("Résumé Rescue fixture string list is invalid")
    result = tuple(_text(item, 8_000) for item in value)
    if len(set(result)) != len(result):
        raise ValueError("Résumé Rescue fixture string list has duplicates")
    return result


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


def write_report(payload: dict[str, Any], output: Path) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded, encoding="utf-8")
    return encoded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]

    def resolve(path: Path) -> Path:
        return path if path.is_absolute() else repository_root / path

    report = run_evaluation(
        dataset_path=resolve(args.dataset),
        metric_contract_path=resolve(args.contract),
        repository_root=repository_root,
    )
    encoded = write_report(report, resolve(args.output))
    if not args.quiet:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
