"""Reproducible local long-term-memory retrieval and lifecycle baseline."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts


@dataclass(frozen=True, slots=True)
class EntrySpec:
    key: str
    path: str
    content: str
    tags: tuple[str, ...]
    supersedes: str | None


@dataclass(frozen=True, slots=True)
class QueryCase:
    id: str
    category: str
    query: str
    expected_keys: tuple[str, ...]
    forbidden_keys: tuple[str, ...]
    top_k: int
    include_superseded: bool


@dataclass(frozen=True, slots=True)
class Dataset:
    id: str
    split: str
    entries: tuple[EntrySpec, ...]
    cases: tuple[QueryCase, ...]
    digest: str


def load_dataset(path: Path) -> Dataset:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("memory fixture is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "dataset_id",
        "split",
        "entries",
        "cases",
    }:
        raise ValueError("memory fixture envelope is invalid")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported memory fixture schema")
    raw_entries = payload["entries"]
    raw_cases = payload["cases"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("memory fixture entries are required")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("memory fixture cases are required")
    entries = tuple(_parse_entry(item) for item in raw_entries)
    cases = tuple(_parse_case(item) for item in raw_cases)
    entry_keys = [entry.key for entry in entries]
    case_ids = [case.id for case in cases]
    if len(set(entry_keys)) != len(entry_keys):
        raise ValueError("memory fixture entry keys must be unique")
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("memory fixture case ids must be unique")
    known = set(entry_keys)
    seen: set[str] = set()
    for entry in entries:
        if entry.supersedes is not None and entry.supersedes not in seen:
            raise ValueError("memory fixture supersedes must reference an earlier entry")
        seen.add(entry.key)
    for case in cases:
        if (set(case.expected_keys) | set(case.forbidden_keys)) - known:
            raise ValueError("memory fixture case references an unknown entry")
        if set(case.expected_keys) & set(case.forbidden_keys):
            raise ValueError("memory fixture expected and forbidden keys overlap")
    return Dataset(
        id=_strict_text(payload["dataset_id"]),
        split=_strict_text(payload["split"]),
        entries=entries,
        cases=cases,
        digest=hashlib.sha256(raw).hexdigest(),
    )


def run_evaluation(
    *,
    dataset_path: Path,
    metric_contract_path: Path,
    repository_root: Path,
    latency_repeats: int = 5,
) -> dict[str, Any]:
    if type(latency_repeats) is not int or not 1 <= latency_repeats <= 50:
        raise ValueError("latency_repeats must be in [1, 50]")
    dataset = load_dataset(dataset_path)
    contract_bytes = metric_contract_path.read_bytes()
    try:
        contract = json.loads(contract_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("memory metric contract is not valid JSON") from exc
    _validate_contract(contract, dataset)

    with _isolated_root(), fts.cursor() as conn:
        ids = _seed_entries(conn, dataset.entries)
        outcomes = [
            _evaluate_case(
                conn,
                case,
                ids=ids,
                repeats=latency_repeats,
            )
            for case in dataset.cases
        ]

    metrics = _metrics(outcomes)
    return {
        "schema_version": 1,
        "evaluation_id": "vida-memory-retrieval-v1",
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
            "case_count": len(dataset.cases),
            "entry_count": len(dataset.entries),
            "sha256": dataset.digest,
            "path": str(dataset_path.relative_to(repository_root)),
        },
        "metric_contract": {
            "sha256": hashlib.sha256(contract_bytes).hexdigest(),
            "path": str(metric_contract_path.relative_to(repository_root)),
        },
        "latency_repeats": latency_repeats,
        "variant": {
            "id": "production_fts5_bm25",
            "metrics": metrics,
            "gate_verdict": _gate_verdict(metrics, contract["gates"]),
            "cases": outcomes,
        },
    }


def _seed_entries(conn, specs: tuple[EntrySpec, ...]) -> dict[str, tuple[str, str]]:
    ids: dict[str, tuple[str, str]] = {}
    created_paths: set[str] = set()
    for spec in specs:
        if spec.path not in created_paths:
            entries_mod.create_file(
                conn,
                name=spec.path,
                description=f"Memory evaluation fixture for {spec.path}",
                tags=[spec.path.split("-", 1)[0], "evaluation"],
            )
            created_paths.add(spec.path)
        if spec.supersedes is None:
            entry_id = entries_mod.append_entry(
                conn,
                name=spec.path,
                content=spec.content,
                tags=list(spec.tags),
                origin=files_mod.MANUAL_ENTRY_ORIGIN,
            )
        else:
            old_id, old_path = ids[spec.supersedes]
            if old_path != spec.path:
                raise ValueError("supersede fixture entries must share a path")
            entry_id = entries_mod.supersede_entry(
                conn,
                name=spec.path,
                old_entry_id=old_id,
                new_content=spec.content,
                reason="memory evaluation knowledge update",
                tags=list(spec.tags),
            )
        ids[spec.key] = (entry_id, spec.path)
    return ids


def _evaluate_case(
    conn,
    case: QueryCase,
    *,
    ids: dict[str, tuple[str, str]],
    repeats: int,
) -> dict[str, Any]:
    runs: list[tuple[list[Any], float]] = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        hits = fts.search(
            conn,
            query=case.query,
            top_k=case.top_k,
            include_superseded=case.include_superseded,
        )
        latency_ms = (time.perf_counter_ns() - started) / 1_000_000
        runs.append((hits, latency_ms))
    signatures = [tuple((hit.id, hit.path) for hit in hits) for hits, _ in runs]
    if any(signature != signatures[0] for signature in signatures[1:]):
        raise RuntimeError(f"non-deterministic retrieval for {case.id}")

    id_to_key = {entry_id: key for key, (entry_id, _) in ids.items()}
    hits = runs[0][0]
    retrieved_keys = [id_to_key.get(hit.id, "") for hit in hits]
    expected = set(case.expected_keys)
    forbidden = set(case.forbidden_keys)
    found = expected & set(retrieved_keys)
    recall = len(found) / len(expected) if expected else 1.0
    first_expected_rank = next(
        (index for index, key in enumerate(retrieved_keys, start=1) if key in expected),
        None,
    )
    forbidden_hits = [key for key in retrieved_keys if key in forbidden]
    source_identity_valid = all(
        hit.id in id_to_key and ids[id_to_key[hit.id]][1] == hit.path for hit in hits
    )
    abstained = not retrieved_keys
    passed = (
        recall == 1.0
        and not forbidden_hits
        and (bool(expected) or abstained)
        and source_identity_valid
    )
    return {
        "case_id": case.id,
        "category": case.category,
        "query": case.query,
        "expected_keys": list(case.expected_keys),
        "forbidden_keys": list(case.forbidden_keys),
        "retrieved_keys": retrieved_keys,
        "recall_at_k": round(recall, 6),
        "reciprocal_rank": round(1 / first_expected_rank, 6) if first_expected_rank else 0.0,
        "forbidden_hits": forbidden_hits,
        "abstained": abstained,
        "source_identity_valid": source_identity_valid,
        "latency_ms": round(max(latency for _, latency in runs), 6),
        "passed": passed,
    }


def _metrics(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    retrieval = [item for item in outcomes if item["expected_keys"]]
    abstention = [item for item in outcomes if item["category"] == "abstention"]
    forbidden = [item for item in outcomes if item["forbidden_keys"]]
    semantic = [
        item for item in outcomes if item["category"] in {"semantic", "cross_language"}
    ]
    categories = sorted({str(item["category"]) for item in outcomes})
    category_recall = {
        category: round(
            sum(float(item["recall_at_k"]) for item in outcomes if item["category"] == category)
            / sum(1 for item in outcomes if item["category"] == category),
            6,
        )
        for category in categories
    }
    latencies = sorted(float(item["latency_ms"]) for item in outcomes)
    p95_index = max(0, min(len(latencies) - 1, int(0.95 * len(latencies) + 0.999999) - 1))
    total_hits = sum(len(item["retrieved_keys"]) for item in outcomes)
    valid_hits = sum(
        len(item["retrieved_keys"]) for item in outcomes if item["source_identity_valid"]
    )
    return {
        "case_pass_rate": round(sum(bool(item["passed"]) for item in outcomes) / len(outcomes), 6),
        "recall_at_k": round(
            sum(float(item["recall_at_k"]) for item in retrieval) / len(retrieval), 6
        ),
        "mean_reciprocal_rank": round(
            sum(float(item["reciprocal_rank"]) for item in retrieval) / len(retrieval), 6
        ),
        "semantic_recall_at_k": round(
            sum(float(item["recall_at_k"]) for item in semantic) / len(semantic), 6
        ),
        "forbidden_hit_rate": round(
            sum(bool(item["forbidden_hits"]) for item in forbidden) / len(forbidden), 6
        ),
        "abstention_accuracy": round(
            sum(bool(item["abstained"]) for item in abstention) / len(abstention), 6
        ),
        "source_identity_coverage": round(valid_hits / total_hits, 6) if total_hits else 1.0,
        "latency_p95_ms": round(latencies[p95_index], 6),
        "category_recall_at_k": category_recall,
    }


def _gate_verdict(metrics: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "recall_at_k": metrics["recall_at_k"] >= gates["recall_at_k_min"],
        "mean_reciprocal_rank": metrics["mean_reciprocal_rank"]
        >= gates["mean_reciprocal_rank_min"],
        "semantic_recall_at_k": metrics["semantic_recall_at_k"]
        >= gates["semantic_recall_at_k_min"],
        "forbidden_hit_rate": metrics["forbidden_hit_rate"]
        <= gates["forbidden_hit_rate_max"],
        "abstention_accuracy": metrics["abstention_accuracy"]
        >= gates["abstention_accuracy_min"],
        "source_identity_coverage": metrics["source_identity_coverage"]
        >= gates["source_identity_coverage_min"],
        "latency_p95_ms": metrics["latency_p95_ms"] <= gates["latency_p95_ms_max"],
    }
    return {"passed": all(checks.values()), "checks": checks}


def _parse_entry(value: object) -> EntrySpec:
    if not isinstance(value, dict) or set(value) != {
        "key",
        "path",
        "content",
        "tags",
        "supersedes",
    }:
        raise ValueError("memory fixture entry is invalid")
    tags = value["tags"]
    supersedes = value["supersedes"]
    if (
        not isinstance(tags, list)
        or not tags
        or not all(isinstance(tag, str) and tag and not any(ch.isspace() for ch in tag) for tag in tags)
        or (supersedes is not None and not isinstance(supersedes, str))
    ):
        raise ValueError("memory fixture entry fields are invalid")
    return EntrySpec(
        key=_strict_text(value["key"]),
        path=_strict_text(value["path"]),
        content=_strict_text(value["content"]),
        tags=tuple(tags),
        supersedes=supersedes,
    )


def _parse_case(value: object) -> QueryCase:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "category",
        "query",
        "expected_keys",
        "forbidden_keys",
        "top_k",
        "include_superseded",
    }:
        raise ValueError("memory fixture case is invalid")
    expected = value["expected_keys"]
    forbidden = value["forbidden_keys"]
    if (
        not isinstance(expected, list)
        or not all(isinstance(item, str) and item for item in expected)
        or not isinstance(forbidden, list)
        or not all(isinstance(item, str) and item for item in forbidden)
        or type(value["top_k"]) is not int
        or not 1 <= value["top_k"] <= 100
        or type(value["include_superseded"]) is not bool
    ):
        raise ValueError("memory fixture case fields are invalid")
    return QueryCase(
        id=_strict_text(value["id"]),
        category=_strict_text(value["category"]),
        query=_strict_text(value["query"]),
        expected_keys=tuple(expected),
        forbidden_keys=tuple(forbidden),
        top_k=value["top_k"],
        include_superseded=value["include_superseded"],
    )


def _validate_contract(contract: object, dataset: Dataset) -> None:
    if not isinstance(contract, dict):
        raise ValueError("memory metric contract must be an object")
    fixture = contract.get("dataset")
    if (
        contract.get("schema_version") != 1
        or not isinstance(fixture, dict)
        or fixture.get("id") != dataset.id
        or fixture.get("split") != dataset.split
        or contract.get("primary_metric") != "recall_at_k"
        or not isinstance(contract.get("gates"), dict)
    ):
        raise ValueError("memory metric contract does not match the dataset")


@contextmanager
def _isolated_root():
    previous = os.environ.get("OPENCHRONICLE_ROOT")
    with tempfile.TemporaryDirectory(prefix="oc-vida-memory-") as temp_dir:
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


def _strict_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("memory fixture text field is invalid")
    return value


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
    parser.add_argument("--latency-repeats", type=int, default=5)
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    baseline_root = repository_root / "benchmarks" / "vida-memory-v1"
    dataset_path = args.dataset or baseline_root / "fixtures" / "cases.json"
    contract_path = args.contract or baseline_root / "json" / "metric_contract.json"
    if not dataset_path.is_absolute():
        dataset_path = repository_root / dataset_path
    if not contract_path.is_absolute():
        contract_path = repository_root / contract_path
    report = run_evaluation(
        dataset_path=dataset_path,
        metric_contract_path=contract_path,
        repository_root=repository_root,
        latency_repeats=args.latency_repeats,
    )
    encoded = write_report(report, args.output)
    if not args.quiet:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
