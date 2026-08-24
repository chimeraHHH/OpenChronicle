"""Deterministic remember/update/forget/reflect lifecycle evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .. import paths
from ..config import Config
from ..provenance.models import EvidenceRef, content_digest
from ..services.current_facts import list_current_facts
from ..services.memory import MemoryService
from ..store import entries as entries_store
from ..store import files as files_store
from ..store import fts

OperationType = Literal["remember", "update", "forget", "reflect"]


@dataclass(frozen=True, slots=True)
class SourceSpec:
    key: str
    path: str
    content: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperationSpec:
    id: str
    type: OperationType
    key: str = ""
    target_key: str = ""
    target_path: str = ""
    content: str = ""
    tags: tuple[str, ...] = ()
    source_keys: tuple[str, ...] = ()
    subject_key: str = ""
    assertion_kind: str = ""
    valid_from: str = ""
    valid_to: str = ""


@dataclass(frozen=True, slots=True)
class CheckpointSpec:
    id: str
    after_operation: str
    expected_current_keys: tuple[str, ...]
    forbidden_current_keys: tuple[str, ...]
    expected_history_keys: tuple[str, ...]
    forbidden_history_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TraceSpec:
    id: str
    sources: tuple[SourceSpec, ...]
    operations: tuple[OperationSpec, ...]
    checkpoints: tuple[CheckpointSpec, ...]


@dataclass(frozen=True, slots=True)
class Dataset:
    id: str
    split: str
    traces: tuple[TraceSpec, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class AppliedMemory:
    key: str
    path: str
    entry_id: str


def load_dataset(path: Path) -> Dataset:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("memory operation fixture is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "dataset_id",
        "split",
        "traces",
    }:
        raise ValueError("memory operation fixture envelope is invalid")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported memory operation fixture schema")
    raw_traces = payload["traces"]
    if not isinstance(raw_traces, list) or not raw_traces:
        raise ValueError("memory operation traces are required")
    traces = tuple(_parse_trace(value) for value in raw_traces)
    trace_ids = [trace.id for trace in traces]
    if len(set(trace_ids)) != len(trace_ids):
        raise ValueError("memory operation trace ids must be unique")
    return Dataset(
        id=_strict_text(payload["dataset_id"]),
        split=_strict_text(payload["split"]),
        traces=traces,
        digest=hashlib.sha256(raw).hexdigest(),
    )


def run_evaluation(
    *,
    dataset_path: Path,
    metric_contract_path: Path,
    repository_root: Path,
) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    contract_bytes = metric_contract_path.read_bytes()
    try:
        contract = json.loads(contract_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("memory operation metric contract is not valid JSON") from exc
    _validate_contract(contract, dataset)

    traces = [_run_trace(trace) for trace in dataset.traces]
    metrics = _metrics(traces)
    return {
        "schema_version": 1,
        "evaluation_id": "vida-memory-operations-v1",
        "repository": _repository_state(repository_root),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "dataset": {
            "id": dataset.id,
            "split": dataset.split,
            "trace_count": len(dataset.traces),
            "operation_count": sum(len(trace.operations) for trace in dataset.traces),
            "checkpoint_count": sum(len(trace.checkpoints) for trace in dataset.traces),
            "sha256": dataset.digest,
            "path": str(dataset_path.relative_to(repository_root)),
        },
        "metric_contract": {
            "sha256": hashlib.sha256(contract_bytes).hexdigest(),
            "path": str(metric_contract_path.relative_to(repository_root)),
        },
        "variant": {
            "id": "production_reviewed_memory_lifecycle",
            "metrics": metrics,
            "gate_verdict": _gate_verdict(metrics, contract["gates"]),
            "traces": traces,
        },
    }


def _run_trace(trace: TraceSpec) -> dict[str, Any]:
    with _isolated_root(), fts.cursor() as conn:
        cfg = Config()
        service = MemoryService(conn, cfg=cfg)
        sources = _seed_sources(conn, trace.sources)
        memories: dict[str, AppliedMemory] = {}
        operations: list[dict[str, Any]] = []
        checkpoints_by_operation = {
            checkpoint.after_operation: checkpoint for checkpoint in trace.checkpoints
        }
        checkpoints: list[dict[str, Any]] = []

        for operation in trace.operations:
            operation_result = _apply_operation(
                conn,
                cfg,
                service,
                operation,
                sources=sources,
                memories=memories,
            )
            operations.append(operation_result)
            checkpoint = checkpoints_by_operation.get(operation.id)
            if checkpoint is not None:
                checkpoints.append(
                    _evaluate_checkpoint(
                        conn,
                        cfg,
                        checkpoint,
                        operation_type=operation.type,
                        memories=memories,
                    )
                )

    return {
        "trace_id": trace.id,
        "operation_order": [operation.id for operation in trace.operations],
        "operations": operations,
        "checkpoints": checkpoints,
        "passed": all(item["succeeded"] for item in operations)
        and all(item["passed"] for item in checkpoints),
    }


def _seed_sources(conn, specs: tuple[SourceSpec, ...]) -> dict[str, EvidenceRef]:
    refs: dict[str, EvidenceRef] = {}
    created_paths: set[str] = set()
    for spec in specs:
        if spec.path not in created_paths:
            entries_store.create_file(
                conn,
                name=spec.path,
                description=f"Lifecycle evaluation evidence for {spec.path}",
                tags=["event", "evaluation"],
            )
            created_paths.add(spec.path)
        entry_id = (
            "ops-source-"
            + hashlib.sha256(f"{spec.key}\0{spec.path}\0{spec.content}".encode()).hexdigest()[:20]
        )
        entries_store.append_entry_once(
            conn,
            name=spec.path,
            content=spec.content,
            tags=list(spec.tags),
            entry_id=entry_id,
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )
        refs[spec.key] = EvidenceRef(
            kind="memory_entry",
            id=entry_id,
            path=spec.path,
            content_hash=content_digest(spec.content),
        )
    return refs


def _apply_operation(
    conn,
    cfg: Config,
    service: MemoryService,
    operation: OperationSpec,
    *,
    sources: dict[str, EvidenceRef],
    memories: dict[str, AppliedMemory],
) -> dict[str, Any]:
    if operation.type == "forget":
        target = memories[operation.target_key]
        fact = _required_current_fact(conn, cfg, target)
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
        return {
            "operation_id": operation.id,
            "type": operation.type,
            "target_key": operation.target_key,
            "plan_entry_count": len(preview.entries),
            "removed_entry": result.removed_entry,
            "succeeded": result.removed_entry,
            "provenance_support_valid": None,
        }

    evidence = [sources[key] for key in operation.source_keys]
    kwargs: dict[str, Any] = {
        "kind": "pattern" if operation.type == "reflect" else "fact",
        "target_path": operation.target_path,
        "content": operation.content,
        "tags": list(operation.tags),
        "evidence": evidence,
        "claim_evidence": evidence,
        "subject_key": operation.subject_key,
        "assertion_kind": operation.assertion_kind,
        "valid_from": operation.valid_from,
        "valid_to": operation.valid_to,
        "producer_run_key": f"ops-eval:{operation.id}",
    }
    if operation.type == "update":
        target = memories[operation.target_key]
        _required_current_fact(conn, cfg, target)
        kwargs.update(
            operation="supersede",
            target_entry_id=target.entry_id,
        )
    candidate = service.propose_candidate(**kwargs)
    accepted = service.approve_candidate(candidate.id, expected_version=candidate.version)
    if not accepted.applied_entry_id:
        raise RuntimeError("accepted operation did not publish an entry")
    memories[operation.key] = AppliedMemory(
        key=operation.key,
        path=operation.target_path,
        entry_id=accepted.applied_entry_id,
    )
    applied_entry = _required_entry(operation.target_path, accepted.applied_entry_id)
    exact_claim_support = tuple(accepted.claim_evidence) == tuple(evidence)
    applied_support = all(ref in applied_entry.evidence_refs for ref in evidence)
    return {
        "operation_id": operation.id,
        "type": operation.type,
        "key": operation.key,
        "target_key": operation.target_key,
        "candidate_id": accepted.id,
        "entry_id": accepted.applied_entry_id,
        "claim_source_count": len(accepted.claim_evidence),
        "provenance_support_valid": exact_claim_support and applied_support,
        "succeeded": accepted.status == "accepted",
    }


def _evaluate_checkpoint(
    conn,
    cfg: Config,
    checkpoint: CheckpointSpec,
    *,
    operation_type: OperationType,
    memories: dict[str, AppliedMemory],
) -> dict[str, Any]:
    current_ids = {fact.id for fact in list_current_facts(conn, cfg, limit=10_000)}
    current_keys = sorted(key for key, memory in memories.items() if memory.entry_id in current_ids)
    history_keys = sorted(
        key for key, memory in memories.items() if _entry_exists(memory.path, memory.entry_id)
    )
    expected_current_missing = sorted(set(checkpoint.expected_current_keys) - set(current_keys))
    forbidden_current_present = sorted(set(checkpoint.forbidden_current_keys) & set(current_keys))
    expected_history_missing = sorted(set(checkpoint.expected_history_keys) - set(history_keys))
    forbidden_history_present = sorted(set(checkpoint.forbidden_history_keys) & set(history_keys))
    passed = not (
        expected_current_missing
        or forbidden_current_present
        or expected_history_missing
        or forbidden_history_present
    )
    return {
        "checkpoint_id": checkpoint.id,
        "after_operation": checkpoint.after_operation,
        "operation_type": operation_type,
        "current_keys": current_keys,
        "history_keys": history_keys,
        "expected_current_missing": expected_current_missing,
        "forbidden_current_present": forbidden_current_present,
        "expected_history_missing": expected_history_missing,
        "forbidden_history_present": forbidden_history_present,
        "passed": passed,
    }


def _metrics(traces: list[dict[str, Any]]) -> dict[str, Any]:
    operations = [item for trace in traces for item in trace["operations"]]
    checkpoints = [item for trace in traces for item in trace["checkpoints"]]
    producers = [item for item in operations if item["type"] != "forget"]
    updates = [item for item in checkpoints if item["operation_type"] == "update"]
    forgets = [item for item in checkpoints if item["operation_type"] == "forget"]
    stale_cases = sum(bool(item["forbidden_current_present"]) for item in updates)
    leakage_cases = sum(bool(item["forbidden_history_present"]) for item in forgets)
    over_forget_cases = sum(bool(item["expected_current_missing"]) for item in forgets)
    return {
        "trace_pass_rate": _ratio(sum(bool(trace["passed"]) for trace in traces), len(traces)),
        "operation_success_rate": _ratio(
            sum(bool(item["succeeded"]) for item in operations), len(operations)
        ),
        "checkpoint_pass_rate": _ratio(
            sum(bool(item["passed"]) for item in checkpoints), len(checkpoints)
        ),
        "stale_value_rate": _ratio(stale_cases, len(updates)),
        "forget_leakage_rate": _ratio(leakage_cases, len(forgets)),
        "over_forget_rate": _ratio(over_forget_cases, len(forgets)),
        "provenance_support_rate": _ratio(
            sum(bool(item["provenance_support_valid"]) for item in producers),
            len(producers),
        ),
        "trajectory_order_accuracy": _ratio(
            sum(
                trace["operation_order"] == [item["operation_id"] for item in trace["operations"]]
                for trace in traces
            ),
            len(traces),
        ),
    }


def _gate_verdict(metrics: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "operation_success_rate": metrics["operation_success_rate"]
        >= gates["operation_success_rate_min"],
        "checkpoint_pass_rate": metrics["checkpoint_pass_rate"]
        >= gates["checkpoint_pass_rate_min"],
        "stale_value_rate": metrics["stale_value_rate"] <= gates["stale_value_rate_max"],
        "forget_leakage_rate": metrics["forget_leakage_rate"] <= gates["forget_leakage_rate_max"],
        "over_forget_rate": metrics["over_forget_rate"] <= gates["over_forget_rate_max"],
        "provenance_support_rate": metrics["provenance_support_rate"]
        >= gates["provenance_support_rate_min"],
        "trajectory_order_accuracy": metrics["trajectory_order_accuracy"]
        >= gates["trajectory_order_accuracy_min"],
    }
    return {"passed": all(checks.values()), "checks": checks}


def _parse_trace(value: object) -> TraceSpec:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "sources",
        "operations",
        "checkpoints",
    }:
        raise ValueError("memory operation trace is invalid")
    raw_sources = value["sources"]
    raw_operations = value["operations"]
    raw_checkpoints = value["checkpoints"]
    if (
        not isinstance(raw_sources, list)
        or not raw_sources
        or not isinstance(raw_operations, list)
        or not raw_operations
        or not isinstance(raw_checkpoints, list)
        or not raw_checkpoints
    ):
        raise ValueError("memory operation trace fields are required")
    sources = tuple(_parse_source(item) for item in raw_sources)
    operations = tuple(_parse_operation(item) for item in raw_operations)
    checkpoints = tuple(_parse_checkpoint(item) for item in raw_checkpoints)
    source_keys = [source.key for source in sources]
    operation_ids = [operation.id for operation in operations]
    checkpoint_ids = [checkpoint.id for checkpoint in checkpoints]
    if len(set(source_keys)) != len(source_keys):
        raise ValueError("memory operation source keys must be unique")
    if len(set(operation_ids)) != len(operation_ids):
        raise ValueError("memory operation ids must be unique")
    if len(set(checkpoint_ids)) != len(checkpoint_ids):
        raise ValueError("memory operation checkpoint ids must be unique")
    checkpoint_operations = [checkpoint.after_operation for checkpoint in checkpoints]
    if len(set(checkpoint_operations)) != len(checkpoint_operations):
        raise ValueError("memory operations may have at most one checkpoint")
    known_sources = set(source_keys)
    known_memories: set[str] = set()
    for operation in operations:
        if set(operation.source_keys) - known_sources:
            raise ValueError("memory operation references an unknown source")
        if operation.target_key and operation.target_key not in known_memories:
            raise ValueError("memory operation target must be created earlier")
        if operation.key:
            if operation.key in known_memories:
                raise ValueError("memory operation keys must be unique")
            known_memories.add(operation.key)
    if any(checkpoint.after_operation not in operation_ids for checkpoint in checkpoints):
        raise ValueError("memory checkpoint references an unknown operation")
    for checkpoint in checkpoints:
        referenced = (
            set(checkpoint.expected_current_keys)
            | set(checkpoint.forbidden_current_keys)
            | set(checkpoint.expected_history_keys)
            | set(checkpoint.forbidden_history_keys)
        )
        if referenced - known_memories:
            raise ValueError("memory checkpoint references an unknown memory key")
    return TraceSpec(
        id=_strict_text(value["id"]),
        sources=sources,
        operations=operations,
        checkpoints=checkpoints,
    )


def _parse_source(value: object) -> SourceSpec:
    if not isinstance(value, dict) or set(value) != {"key", "path", "content", "tags"}:
        raise ValueError("memory operation source is invalid")
    tags = _strict_tags(value["tags"])
    path = _strict_text(value["path"])
    if not path.startswith("event-"):
        raise ValueError("memory operation sources must use event files")
    return SourceSpec(
        key=_strict_text(value["key"]),
        path=path,
        content=_strict_text(value["content"]),
        tags=tags,
    )


def _parse_operation(value: object) -> OperationSpec:
    if not isinstance(value, dict):
        raise ValueError("memory operation is invalid")
    operation_type = value.get("type")
    if operation_type == "forget":
        if set(value) != {"id", "type", "target_key"}:
            raise ValueError("forget operation is invalid")
        return OperationSpec(
            id=_strict_text(value["id"]),
            type="forget",
            target_key=_strict_text(value["target_key"]),
        )
    expected = {
        "id",
        "type",
        "key",
        "target_path",
        "content",
        "tags",
        "source_keys",
        "subject_key",
        "assertion_kind",
        "valid_from",
        "valid_to",
    }
    if operation_type == "update":
        expected.add("target_key")
    if set(value) != expected or operation_type not in {"remember", "update", "reflect"}:
        raise ValueError("memory-producing operation is invalid")
    sources = value["source_keys"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("memory-producing operations require source keys")
    source_keys = tuple(_strict_text(item) for item in sources)
    assertion_kind = _strict_text(value["assertion_kind"])
    if operation_type == "reflect" and (assertion_kind != "inferred" or len(set(source_keys)) < 2):
        raise ValueError("reflect operations require inferred support from two sources")
    return OperationSpec(
        id=_strict_text(value["id"]),
        type=operation_type,
        key=_strict_text(value["key"]),
        target_key=_strict_text(value["target_key"]) if operation_type == "update" else "",
        target_path=_strict_text(value["target_path"]),
        content=_strict_text(value["content"]),
        tags=_strict_tags(value["tags"]),
        source_keys=source_keys,
        subject_key=_strict_text(value["subject_key"]),
        assertion_kind=assertion_kind,
        valid_from=_optional_text(value["valid_from"]),
        valid_to=_optional_text(value["valid_to"]),
    )


def _parse_checkpoint(value: object) -> CheckpointSpec:
    expected = {
        "id",
        "after_operation",
        "expected_current_keys",
        "forbidden_current_keys",
        "expected_history_keys",
        "forbidden_history_keys",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("memory operation checkpoint is invalid")
    groups = {key: _strict_text_list(value[key]) for key in expected if key.endswith("_keys")}
    if set(groups["expected_current_keys"]) & set(groups["forbidden_current_keys"]):
        raise ValueError("checkpoint current expectations overlap")
    if set(groups["expected_history_keys"]) & set(groups["forbidden_history_keys"]):
        raise ValueError("checkpoint history expectations overlap")
    return CheckpointSpec(
        id=_strict_text(value["id"]),
        after_operation=_strict_text(value["after_operation"]),
        expected_current_keys=groups["expected_current_keys"],
        forbidden_current_keys=groups["forbidden_current_keys"],
        expected_history_keys=groups["expected_history_keys"],
        forbidden_history_keys=groups["forbidden_history_keys"],
    )


def _validate_contract(contract: object, dataset: Dataset) -> None:
    if not isinstance(contract, dict):
        raise ValueError("memory operation metric contract must be an object")
    fixture = contract.get("dataset")
    required_gates = {
        "operation_success_rate_min",
        "checkpoint_pass_rate_min",
        "stale_value_rate_max",
        "forget_leakage_rate_max",
        "over_forget_rate_max",
        "provenance_support_rate_min",
        "trajectory_order_accuracy_min",
    }
    gates = contract.get("gates")
    if (
        contract.get("schema_version") != 1
        or not isinstance(fixture, dict)
        or fixture.get("id") != dataset.id
        or fixture.get("split") != dataset.split
        or contract.get("primary_metric") != "checkpoint_pass_rate"
        or not isinstance(gates, dict)
        or set(gates) != required_gates
    ):
        raise ValueError("memory operation metric contract does not match the dataset")


def _required_current_fact(conn, cfg: Config, target: AppliedMemory):
    for fact in list_current_facts(conn, cfg, limit=10_000):
        if fact.path == target.path and fact.id == target.entry_id:
            return fact
    raise RuntimeError(f"memory operation target is not current: {target.key}")


def _required_entry(path: str, entry_id: str):
    parsed = files_store.read_file(files_store.memory_path(path))
    for entry in parsed.entries:
        if entry.id == entry_id:
            return entry
    raise RuntimeError("published memory entry is missing")


def _entry_exists(path: str, entry_id: str) -> bool:
    target = files_store.memory_path(path)
    if not target.exists():
        return False
    return any(entry.id == entry_id for entry in files_store.read_file(target).entries)


def _strict_tags(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(
            isinstance(item, str)
            and item
            and "\x00" not in item
            and not any(char.isspace() for char in item)
            for item in value
        )
    ):
        raise ValueError("memory operation tags are invalid")
    return tuple(value)


def _strict_text_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("memory operation key list is invalid")
    items = tuple(_strict_text(item) for item in value)
    if len(set(items)) != len(items):
        raise ValueError("memory operation key list contains duplicates")
    return items


def _strict_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("memory operation text field is invalid")
    return value


def _optional_text(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("memory operation optional text field is invalid")
    return value


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


@contextmanager
def _isolated_root():
    previous = os.environ.get("OPENCHRONICLE_ROOT")
    with tempfile.TemporaryDirectory(prefix="oc-vida-memory-ops-") as temp_dir:
        os.environ["OPENCHRONICLE_ROOT"] = temp_dir
        paths.ensure_dirs()
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("OPENCHRONICLE_ROOT", None)
            else:
                os.environ["OPENCHRONICLE_ROOT"] = previous


def _repository_state(repository_root: Path) -> dict[str, Any]:
    commit = _git(repository_root, "rev-parse", "HEAD")
    status = _git(repository_root, "status", "--porcelain")
    return {"commit": commit, "dirty": bool(status), "status_lines": len(status.splitlines())}


def _git(repository_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError("unable to record repository identity")
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
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    baseline_root = repository_root / "benchmarks" / "vida-memory-ops-v1"
    dataset_path = args.dataset or baseline_root / "fixtures" / "traces.json"
    contract_path = args.contract or baseline_root / "json" / "metric_contract.json"
    if not dataset_path.is_absolute():
        dataset_path = repository_root / dataset_path
    if not contract_path.is_absolute():
        contract_path = repository_root / contract_path
    report = run_evaluation(
        dataset_path=dataset_path,
        metric_contract_path=contract_path,
        repository_root=repository_root,
    )
    encoded = write_report(report, args.output)
    if not args.quiet:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
