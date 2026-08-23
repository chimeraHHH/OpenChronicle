"""Run MemOps-50 gold operations through the production reviewed-memory lifecycle."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, content_digest
from ..services.current_facts import list_current_facts
from ..services.memory import MemoryService
from ..store import entries as entries_store
from ..store import files as files_store
from ..store import fts
from . import memops50
from .vida_memory_decisions import EvidenceTurn
from .vida_memory_operations import _isolated_root


@dataclass(frozen=True, slots=True)
class Scenario:
    source_file: str
    operation_family: str
    evaluation_type: str
    difficulty: str
    evidence: tuple[EvidenceTurn, ...]
    operations: tuple[LifecycleOperation, ...]
    tentative_operation_count: int


@dataclass(frozen=True, slots=True)
class LifecycleOperation:
    id: str
    type: str
    target_id: str
    target_name: str
    new_value: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AppliedMemory:
    target_id: str
    path: str
    entry_id: str
    content: str


def load_scenarios(*, manifest_path: Path, memops_root: Path) -> tuple[Scenario, ...]:
    """Load only digest-verified Stage 2 operations from the frozen tier."""
    memops50.verify_manifest(manifest_path=manifest_path, memops_root=memops_root)
    manifest = memops50._load_json(
        manifest_path.read_bytes(),
        label="MemOps-50 manifest",
    )
    if not isinstance(manifest, dict) or not isinstance(manifest.get("items"), list):
        raise ValueError("MemOps-50 manifest items are missing")
    stage2_root = memops_root.resolve() / memops50.STAGE2_ROOT
    scenarios: list[Scenario] = []
    for item in manifest["items"]:
        if not isinstance(item, dict):
            raise ValueError("MemOps-50 manifest item is invalid")
        source_file = str(item["source_file"])
        raw = (stage2_root / source_file).read_bytes()
        if memops50.sha256_bytes(raw) != item["stage2_sha256"]:
            raise ValueError(f"MemOps-50 Stage 2 digest changed: {source_file}")
        payload = memops50._load_json(raw, label=source_file)
        evidence, operations = _parse_payload(payload, source_file=source_file)
        tentative_operation_count = sum(
            isinstance(operation, dict) and operation.get("validity") == "tentative"
            for operation in payload["operations"]
        )
        scenarios.append(
            Scenario(
                source_file=source_file,
                operation_family=str(item["operation_type"]),
                evaluation_type=str(item["evaluation_type"]),
                difficulty=str(item["difficulty"]),
                evidence=evidence,
                operations=operations,
                tentative_operation_count=tentative_operation_count,
            )
        )
    return tuple(scenarios)


def run_evaluation(
    *,
    manifest_path: Path,
    memops_root: Path,
    metric_contract_path: Path,
    repository_root: Path,
) -> dict[str, Any]:
    scenarios = load_scenarios(manifest_path=manifest_path, memops_root=memops_root)
    contract_bytes = metric_contract_path.read_bytes()
    contract = memops50._load_json(contract_bytes, label="MemOps-50 lifecycle contract")
    _validate_contract(contract)
    results = [_run_scenario(scenario) for scenario in scenarios]
    metrics = _metrics(results)
    repository = _repository_state(repository_root)
    return {
        "schema_version": 1,
        "evaluation_id": "memops50-reviewed-lifecycle-v1",
        "repository": repository,
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "dataset": {
            "tier_id": "memops50-adjacent-longitudinal-v1",
            "manifest_path": str(manifest_path.relative_to(repository_root)),
            "manifest_sha256": memops50.sha256_bytes(manifest_path.read_bytes()),
            "upstream_commit": memops50.UPSTREAM_COMMIT,
            "scenario_count": len(scenarios),
            "operation_source": "verified_adjacent_clean_stage2_gold",
            "confirmed_operation_count": sum(
                len(scenario.operations) for scenario in scenarios
            ),
            "excluded_tentative_operation_count": sum(
                scenario.tentative_operation_count for scenario in scenarios
            ),
        },
        "metric_contract": {
            "path": str(metric_contract_path.relative_to(repository_root)),
            "sha256": memops50.sha256_bytes(contract_bytes),
        },
        "variant": {
            "id": "production_reviewed_memory_gold_operation_oracle_v1",
            "oracle_boundary": (
                "MemOps confirmed operations stand in for human-approved decisions; "
                "this does not measure automatic operation inference or answer quality"
            ),
            "action_capability": "isolated_temporary_memory_only",
            "metrics": metrics,
            "gate_verdict": _gate_verdict(
                metrics,
                contract["gates"],
                repository_clean=not repository["dirty"],
            ),
            "scenarios": results,
        },
    }


def _run_scenario(scenario: Scenario) -> dict[str, Any]:
    with _isolated_root(), fts.cursor() as conn:
        cfg = Config()
        service = MemoryService(conn, cfg=cfg)
        sources = _seed_evidence(conn, scenario)
        current: dict[str, AppliedMemory] = {}
        history: dict[str, list[AppliedMemory]] = {}
        rows: list[dict[str, Any]] = []
        for operation in scenario.operations:
            rows.append(
                _apply_and_check(
                    conn,
                    cfg,
                    service,
                    scenario,
                    operation,
                    sources=sources,
                    current=current,
                    history=history,
                )
            )
        current_ids = _current_ids(conn, cfg)
        final_missing = sorted(
            target_id
            for target_id, applied in current.items()
            if applied.entry_id not in current_ids
        )
    return {
        "source_file": scenario.source_file,
        "operation_family": scenario.operation_family,
        "evaluation_type": scenario.evaluation_type,
        "difficulty": scenario.difficulty,
        "operation_count": len(rows),
        "excluded_tentative_operation_count": scenario.tentative_operation_count,
        "operation_order": [row["operation_id"] for row in rows],
        "final_expected_target_count": len(current),
        "final_expected_missing": final_missing,
        "passed": not final_missing and all(row["passed"] for row in rows),
        "operations": rows,
    }


def _seed_evidence(conn, scenario: Scenario) -> dict[str, EvidenceRef]:
    path = "event-memops50.md"
    entries_store.create_file(
        conn,
        name=path,
        description="External MemOps-50 evidence for an isolated lifecycle evaluation",
        tags=["event", "evaluation", "memops50"],
    )
    refs: dict[str, EvidenceRef] = {}
    for evidence in scenario.evidence:
        entry_id = "memops-source-" + hashlib.sha256(
            f"{scenario.source_file}\0{evidence.id}\0{evidence.content}".encode()
        ).hexdigest()[:20]
        entries_store.append_entry_once(
            conn,
            name=path,
            content=evidence.content,
            tags=["event", "evaluation", "memops50"],
            entry_id=entry_id,
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )
        refs[evidence.id] = EvidenceRef(
            kind="memory_entry",
            id=entry_id,
            path=path,
            content_hash=content_digest(evidence.content),
        )
    return refs


def _parse_payload(
    payload: object,
    *,
    source_file: str,
) -> tuple[tuple[EvidenceTurn, ...], tuple[LifecycleOperation, ...]]:
    if not isinstance(payload, dict):
        raise ValueError(f"MemOps-50 payload must be an object: {source_file}")
    conversations = payload.get("conversations")
    raw_operations = payload.get("operations")
    if not isinstance(conversations, list) or not isinstance(raw_operations, list):
        raise ValueError(f"MemOps-50 operations are missing: {source_file}")

    evidence: list[EvidenceTurn] = []
    evidence_ids: set[str] = set()
    for segment in conversations:
        if not isinstance(segment, dict) or not isinstance(segment.get("dialogue"), list):
            raise ValueError(f"MemOps-50 conversation is invalid: {source_file}")
        segment_index = segment.get("segment_index")
        if type(segment_index) is not int or segment_index < 1:
            raise ValueError(f"MemOps-50 segment index is invalid: {source_file}")
        for turn_index, turn in enumerate(segment["dialogue"], start=1):
            if (
                not isinstance(turn, dict)
                or turn.get("role") not in {"user", "assistant"}
                or not isinstance(turn.get("content"), str)
                or not turn["content"]
            ):
                raise ValueError(f"MemOps-50 dialogue turn is invalid: {source_file}")
            evidence_id = f"segment-{segment_index}-turn-{turn_index}"
            if evidence_id in evidence_ids:
                raise ValueError(f"MemOps-50 evidence id is ambiguous: {source_file}")
            evidence_ids.add(evidence_id)
            evidence.append(
                EvidenceTurn(
                    id=evidence_id,
                    session_id=f"segment-{segment_index}",
                    role=turn["role"],
                    content=turn["content"],
                )
            )

    operations: list[LifecycleOperation] = []
    live_targets: set[str] = set()
    operation_ids: set[str] = set()
    for raw_operation in raw_operations:
        if not isinstance(raw_operation, dict) or raw_operation.get("validity") != "confirmed":
            continue
        operation_id = raw_operation.get("operation_id")
        operation_type = raw_operation.get("type")
        target = raw_operation.get("target")
        spans = raw_operation.get("evidence_spans")
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or operation_id in operation_ids
            or operation_type not in {"remember", "update", "forget", "reflect"}
            or not isinstance(target, dict)
            or not isinstance(spans, list)
            or not spans
        ):
            raise ValueError(f"MemOps-50 confirmed operation is invalid: {source_file}")
        operation_ids.add(operation_id)
        target_id = target.get("target_id")
        target_name = target.get("target_name")
        if (
            not isinstance(target_id, str)
            or not target_id
            or not isinstance(target_name, str)
            or not target_name
        ):
            raise ValueError(f"MemOps-50 target is invalid: {source_file}")
        if operation_type in {"update", "forget"} and target_id not in live_targets:
            raise ValueError(f"MemOps-50 target has no confirmed predecessor: {source_file}")
        if operation_type in {"remember", "reflect"} and target_id in live_targets:
            raise ValueError(f"MemOps-50 target is already current: {source_file}")
        operation_evidence: list[str] = []
        for span in spans:
            if not isinstance(span, dict):
                raise ValueError(f"MemOps-50 evidence span is invalid: {source_file}")
            evidence_id = f"segment-{span.get('segment_index')}-turn-{span.get('turn_index')}"
            if evidence_id not in evidence_ids:
                raise ValueError(f"MemOps-50 evidence span does not resolve: {source_file}")
            if evidence_id not in operation_evidence:
                operation_evidence.append(evidence_id)
        new_value = raw_operation.get("new_value")
        if operation_type == "forget":
            if new_value is not None:
                raise ValueError(f"MemOps-50 forget has a new value: {source_file}")
            clean_value = ""
            live_targets.remove(target_id)
        else:
            if not isinstance(new_value, str) or not new_value:
                raise ValueError(f"MemOps-50 operation value is invalid: {source_file}")
            if operation_type == "reflect" and len(operation_evidence) < 2:
                raise ValueError(f"MemOps-50 reflection support is insufficient: {source_file}")
            clean_value = new_value
            live_targets.add(target_id)
        operations.append(
            LifecycleOperation(
                id=operation_id,
                type=operation_type,
                target_id=target_id,
                target_name=target_name,
                new_value=clean_value,
                evidence_ids=tuple(operation_evidence),
            )
        )
    if not operations:
        raise ValueError(f"MemOps-50 confirmed operations are missing: {source_file}")
    return tuple(evidence), tuple(operations)


def _apply_and_check(
    conn,
    cfg: Config,
    service: MemoryService,
    scenario: Scenario,
    operation: LifecycleOperation,
    *,
    sources: dict[str, EvidenceRef],
    current: dict[str, AppliedMemory],
    history: dict[str, list[AppliedMemory]],
) -> dict[str, Any]:
    prior_current = dict(current)
    if operation.type == "forget":
        target = current[operation.target_id]
        fact = _find_current_fact(conn, cfg, target)
        preview = service.preview_purge_fact(
            path=fact.path,
            entry_id=fact.id,
            expected_revision=fact.revision,
        )
        result = service.purge_fact(
            path=fact.path,
            entry_id=fact.id,
            expected_revision=fact.revision,
            expected_plan_digest=preview.plan_digest,
        )
        removed_history = history.pop(operation.target_id)
        current.pop(operation.target_id)
        current_ids = _current_ids(conn, cfg)
        forgotten_history_present = [
            value.entry_id
            for value in removed_history
            if _entry_exists(value.path, value.entry_id)
        ]
        expected_missing = _expected_current_missing(current, current_ids)
        forgotten_current = target.entry_id in current_ids
        passed = (
            result.removed_entry
            and not forgotten_current
            and not forgotten_history_present
            and not expected_missing
        )
        return {
            "operation_id": operation.id,
            "type": operation.type,
            "target_id": operation.target_id,
            "succeeded": result.removed_entry,
            "provenance_support_valid": None,
            "stale_previous_current": None,
            "forgotten_current": forgotten_current,
            "forgotten_history_entry_count": len(forgotten_history_present),
            "expected_current_missing": expected_missing,
            "passed": passed,
        }

    evidence = [sources[evidence_id] for evidence_id in operation.evidence_ids]
    content = f"{operation.target_name}: {operation.new_value}"
    previous = current.get(operation.target_id)
    candidate = service.propose_candidate(
        kind="pattern" if operation.type == "reflect" else "fact",
        operation="supersede" if operation.type == "update" else "append",
        target_path="user-memops50.md",
        target_entry_id=previous.entry_id if previous else "",
        content=content,
        tags=["memops50", "pattern" if operation.type == "reflect" else "fact"],
        evidence=evidence,
        claim_evidence=evidence,
        subject_key=_subject_key(scenario, operation.target_id),
        assertion_kind="inferred" if operation.type == "reflect" else "user_asserted",
        producer_run_key=(
            f"memops50-lifecycle:{scenario.source_file}:{operation.id}"
        ),
    )
    accepted = service.approve_candidate(candidate.id, expected_version=candidate.version)
    if not accepted.applied_entry_id:
        raise RuntimeError("accepted MemOps-50 operation did not publish memory")
    applied = AppliedMemory(
        target_id=operation.target_id,
        path="user-memops50.md",
        entry_id=accepted.applied_entry_id,
        content=content,
    )
    current[operation.target_id] = applied
    history.setdefault(operation.target_id, []).append(applied)
    entry = _required_entry(applied)
    durable_sources = provenance_store.direct_sources_checked(
        conn,
        EvidenceRef(kind="memory_entry", id=entry.id, path=applied.path),
    )
    provenance_valid = all(ref in entry.evidence_refs for ref in evidence) and (
        durable_sources == entry.evidence_refs
    )
    current_ids = _current_ids(conn, cfg)
    expected_missing = _expected_current_missing(current, current_ids)
    stale_previous = bool(previous and previous.entry_id in current_ids)
    current_value_exact = applied.entry_id in current_ids and entry.body == content
    unrelated_missing = sorted(
        target_id
        for target_id, value in prior_current.items()
        if target_id != operation.target_id and value.entry_id not in current_ids
    )
    passed = (
        accepted.status == "accepted"
        and provenance_valid
        and current_value_exact
        and not stale_previous
        and not expected_missing
        and not unrelated_missing
    )
    return {
        "operation_id": operation.id,
        "type": operation.type,
        "target_id": operation.target_id,
        "succeeded": accepted.status == "accepted",
        "provenance_support_valid": provenance_valid,
        "current_value_exact": current_value_exact,
        "stale_previous_current": stale_previous if operation.type == "update" else None,
        "forgotten_current": None,
        "forgotten_history_entry_count": None,
        "expected_current_missing": expected_missing,
        "unrelated_current_missing": unrelated_missing,
        "passed": passed,
    }


def _metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    operations = [row for result in results for row in result["operations"]]
    producers = [row for row in operations if row["type"] != "forget"]
    updates = [row for row in operations if row["type"] == "update"]
    forgets = [row for row in operations if row["type"] == "forget"]
    trajectory_rows = [
        row
        for result in results
        if result["operation_family"] == "TrajectoryOps"
        for row in result["operations"]
    ]
    return {
        "scenario_pass_rate": _ratio(sum(result["passed"] for result in results), len(results)),
        "operation_success_rate": _ratio(
            sum(row["succeeded"] for row in operations), len(operations)
        ),
        "checkpoint_accuracy": _ratio(sum(row["passed"] for row in operations), len(operations)),
        "final_state_accuracy": _ratio(
            sum(not result["final_expected_missing"] for result in results), len(results)
        ),
        "stale_value_rate": _ratio(
            sum(bool(row["stale_previous_current"]) for row in updates), len(updates)
        ),
        "forget_current_leakage_rate": _ratio(
            sum(bool(row["forgotten_current"]) for row in forgets), len(forgets)
        ),
        "forget_history_leakage_rate": _ratio(
            sum(bool(row["forgotten_history_entry_count"]) for row in forgets), len(forgets)
        ),
        "over_forget_rate": _ratio(
            sum(bool(row["expected_current_missing"]) for row in forgets), len(forgets)
        ),
        "provenance_support_rate": _ratio(
            sum(bool(row["provenance_support_valid"]) for row in producers), len(producers)
        ),
        "trajectory_checkpoint_accuracy": _ratio(
            sum(row["passed"] for row in trajectory_rows), len(trajectory_rows)
        ),
        "operation_counts": dict(sorted(Counter(row["type"] for row in operations).items())),
    }


def _gate_verdict(
    metrics: dict[str, Any],
    gates: dict[str, Any],
    *,
    repository_clean: bool,
) -> dict[str, Any]:
    checks = {
        "repository_clean": repository_clean if gates["repository_clean"] else True,
        "scenario_pass_rate": metrics["scenario_pass_rate"]
        >= gates["scenario_pass_rate_min"],
        "operation_success_rate": metrics["operation_success_rate"]
        >= gates["operation_success_rate_min"],
        "checkpoint_accuracy": metrics["checkpoint_accuracy"]
        >= gates["checkpoint_accuracy_min"],
        "final_state_accuracy": metrics["final_state_accuracy"]
        >= gates["final_state_accuracy_min"],
        "stale_value_rate": metrics["stale_value_rate"] <= gates["stale_value_rate_max"],
        "forget_current_leakage_rate": metrics["forget_current_leakage_rate"]
        <= gates["forget_current_leakage_rate_max"],
        "forget_history_leakage_rate": metrics["forget_history_leakage_rate"]
        <= gates["forget_history_leakage_rate_max"],
        "over_forget_rate": metrics["over_forget_rate"] <= gates["over_forget_rate_max"],
        "provenance_support_rate": metrics["provenance_support_rate"]
        >= gates["provenance_support_rate_min"],
        "trajectory_checkpoint_accuracy": metrics["trajectory_checkpoint_accuracy"]
        >= gates["trajectory_checkpoint_accuracy_min"],
    }
    return {"passed": all(checks.values()), "checks": checks}


def _validate_contract(contract: object) -> None:
    expected_gates = {
        "repository_clean",
        "scenario_pass_rate_min",
        "operation_success_rate_min",
        "checkpoint_accuracy_min",
        "final_state_accuracy_min",
        "stale_value_rate_max",
        "forget_current_leakage_rate_max",
        "forget_history_leakage_rate_max",
        "over_forget_rate_max",
        "provenance_support_rate_min",
        "trajectory_checkpoint_accuracy_min",
    }
    if (
        not isinstance(contract, dict)
        or contract.get("schema_version") != 1
        or contract.get("evaluation_id") != "memops50-reviewed-lifecycle-v1"
        or contract.get("dataset_tier_id") != "memops50-adjacent-longitudinal-v1"
        or contract.get("primary_metric") != "checkpoint_accuracy"
        or not isinstance(contract.get("gates"), dict)
        or set(contract["gates"]) != expected_gates
    ):
        raise ValueError("MemOps-50 lifecycle metric contract is invalid")


def _subject_key(scenario: Scenario, target_id: str) -> str:
    return f"memops50.{scenario.source_file.removesuffix('.json')}.{target_id}"


def _find_current_fact(conn, cfg: Config, target: AppliedMemory):
    for fact in list_current_facts(conn, cfg, limit=10_000):
        if fact.path == target.path and fact.id == target.entry_id:
            return fact
    raise RuntimeError(f"MemOps-50 target is not current: {target.target_id}")


def _current_ids(conn, cfg: Config) -> set[str]:
    return {fact.id for fact in list_current_facts(conn, cfg, limit=10_000)}


def _expected_current_missing(
    current: dict[str, AppliedMemory],
    current_ids: set[str],
) -> list[str]:
    return sorted(
        target_id
        for target_id, applied in current.items()
        if applied.entry_id not in current_ids
    )


def _required_entry(applied: AppliedMemory):
    parsed = files_store.read_file(files_store.memory_path(applied.path))
    for entry in parsed.entries:
        if entry.id == applied.entry_id:
            return entry
    raise RuntimeError("MemOps-50 published memory entry is missing")


def _entry_exists(path: str, entry_id: str) -> bool:
    target = files_store.memory_path(path)
    return target.exists() and any(
        entry.id == entry_id for entry in files_store.read_file(target).entries
    )


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


def _repository_state(repository_root: Path) -> dict[str, Any]:
    commit = _git(repository_root, "rev-parse", "HEAD")
    status = _git(repository_root, "status", "--porcelain")
    return {"commit": commit, "dirty": bool(status), "status_lines": len(status.splitlines())}


def _git(repository_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def write_report(report: dict[str, Any], output: Path | None = None) -> str:
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    return encoded


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memops-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    benchmark_root = repository_root / "benchmarks" / "memops50-lifecycle-v1"
    manifest_path = args.manifest or benchmark_root / "json" / "manifest.json"
    contract_path = args.contract or benchmark_root / "json" / "metric_contract.json"
    if not manifest_path.is_absolute():
        manifest_path = repository_root / manifest_path
    if not contract_path.is_absolute():
        contract_path = repository_root / contract_path
    report = run_evaluation(
        manifest_path=manifest_path,
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
