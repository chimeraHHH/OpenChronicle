"""Same-case comparison of minute, session, event, and adjacent-event recall."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..store.fts import _safe_fts_or_query, _safe_fts_query

Variant = Literal["minute", "session", "event", "event_adjacent"]
_VARIANTS: tuple[Variant, ...] = ("minute", "session", "event", "event_adjacent")


@dataclass(frozen=True, slots=True)
class Observation:
    id: str
    session_id: str
    event_id: str
    order: int
    text: str


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    id: str
    query: str
    required_anchors: tuple[str, ...]
    forbidden_anchors: tuple[str, ...]
    observations: tuple[Observation, ...]


@dataclass(frozen=True, slots=True)
class Dataset:
    id: str
    split: str
    cases: tuple[RetrievalCase, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class Unit:
    id: str
    text: str
    order: int


def load_dataset(path: Path) -> Dataset:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("event retrieval fixture is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "dataset_id",
        "split",
        "cases",
    }:
        raise ValueError("event retrieval fixture envelope is invalid")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported event retrieval fixture schema")
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= 100:
        raise ValueError("event retrieval cases are required")
    cases = tuple(_parse_case(item) for item in raw_cases)
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("event retrieval case ids must be unique")
    return Dataset(
        id=_text(payload["dataset_id"], 200),
        split=_text(payload["split"], 200),
        cases=cases,
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
        raise ValueError("event retrieval metric contract is not valid JSON") from exc
    _validate_contract(contract)

    outcomes = {
        variant: [_evaluate_case(case, variant) for case in dataset.cases]
        for variant in _VARIANTS
    }
    metrics = {variant: _metrics(results) for variant, results in outcomes.items()}
    comparisons = {
        "anchor_recall_lift_vs_minute": round(
            metrics["event_adjacent"]["anchor_recall"] - metrics["minute"]["anchor_recall"],
            6,
        ),
        "context_chars_ratio_vs_session": round(
            metrics["event_adjacent"]["mean_context_chars"]
            / max(metrics["session"]["mean_context_chars"], 1.0),
            6,
        ),
        "forbidden_anchor_rate_reduction_vs_session": round(
            metrics["session"]["forbidden_anchor_rate"]
            - metrics["event_adjacent"]["forbidden_anchor_rate"],
            6,
        ),
    }
    verdict = _gate_verdict(metrics["event_adjacent"], comparisons, contract["gates"])
    return {
        "schema_version": 1,
        "evaluation_id": "vida-event-retrieval-v1",
        "repository": _repository_state(repository_root),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "sqlite": sqlite3.sqlite_version,
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
        "retrieval_contract": {
            "ranker": "sqlite_fts5_bm25_strict_then_or_on_zero_hits",
            "tokenizer": "unicode61 remove_diacritics 2",
            "top_k": 1,
            "event_adjacency_radius": 1,
            "note": (
                "All variants use the same local ranker. This isolates retrieval-unit shape; "
                "production projection/authorization is covered by tests/test_activity_events.py."
            ),
        },
        "variants": {
            variant: {"metrics": metrics[variant], "cases": outcomes[variant]}
            for variant in _VARIANTS
        },
        "comparisons": comparisons,
        "gate_verdict": verdict,
    }


def write_report(report: dict[str, Any], output: Path | None) -> str:
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    return encoded


def _evaluate_case(case: RetrievalCase, variant: Variant) -> dict[str, Any]:
    units = _units(case, variant)
    match = _bm25_top_one(case.query, units)
    selected: list[Unit] = []
    if match is not None:
        if variant == "event_adjacent":
            index = next(index for index, unit in enumerate(units) if unit.id == match.id)
            selected = units[max(0, index - 1) : index + 2]
        else:
            selected = [match]
    context = "\n".join(unit.text for unit in selected)
    required_hits = [anchor for anchor in case.required_anchors if anchor in context]
    forbidden_hits = [anchor for anchor in case.forbidden_anchors if anchor in context]
    return {
        "case_id": case.id,
        "query": case.query,
        "matched_unit_id": match.id if match is not None else None,
        "returned_unit_ids": [unit.id for unit in selected],
        "required_anchor_count": len(case.required_anchors),
        "required_anchor_hits": required_hits,
        "forbidden_anchor_count": len(case.forbidden_anchors),
        "forbidden_anchor_hits": forbidden_hits,
        "answerable": len(required_hits) == len(case.required_anchors),
        "context_chars": len(context),
    }


def _units(case: RetrievalCase, variant: Variant) -> list[Unit]:
    if variant == "minute":
        return [Unit(id=item.id, text=item.text, order=item.order) for item in case.observations]
    key_name = "session_id" if variant == "session" else "event_id"
    grouped: dict[str, list[Observation]] = defaultdict(list)
    for observation in case.observations:
        grouped[getattr(observation, key_name)].append(observation)
    units = [
        Unit(
            id=unit_id,
            text="\n".join(item.text for item in sorted(items, key=lambda item: item.order)),
            order=min(item.order for item in items),
        )
        for unit_id, items in grouped.items()
    ]
    return sorted(units, key=lambda unit: (unit.order, unit.id))


def _bm25_top_one(query: str, units: list[Unit]) -> Unit | None:
    safe_query = _safe_fts_query(query)
    if safe_query == '\"\"' or not units:
        return None
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE corpus USING fts5(id UNINDEXED, text, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        conn.executemany(
            "INSERT INTO corpus(id, text) VALUES (?, ?)",
            [(unit.id, unit.text) for unit in units],
        )
        row = conn.execute(
            "SELECT id FROM corpus WHERE corpus MATCH ? ORDER BY bm25(corpus), rowid LIMIT 1",
            (safe_query,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM corpus WHERE corpus MATCH ? "
                "ORDER BY bm25(corpus), rowid LIMIT 1",
                (_safe_fts_or_query(query),),
            ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return next(unit for unit in units if unit.id == row["id"])


def _metrics(outcomes: list[dict[str, Any]]) -> dict[str, float]:
    case_count = len(outcomes)
    required = sum(int(item["required_anchor_count"]) for item in outcomes)
    required_hits = sum(len(item["required_anchor_hits"]) for item in outcomes)
    forbidden = sum(int(item["forbidden_anchor_count"]) for item in outcomes)
    forbidden_hits = sum(len(item["forbidden_anchor_hits"]) for item in outcomes)
    return {
        "case_count": float(case_count),
        "answerable_rate": round(
            sum(bool(item["answerable"]) for item in outcomes) / case_count,
            6,
        ),
        "anchor_recall": round(required_hits / required if required else 1.0, 6),
        "forbidden_anchor_rate": round(forbidden_hits / forbidden if forbidden else 0.0, 6),
        "empty_hit_rate": round(
            sum(item["matched_unit_id"] is None for item in outcomes) / case_count,
            6,
        ),
        "mean_context_chars": round(
            sum(int(item["context_chars"]) for item in outcomes) / case_count,
            6,
        ),
        "mean_returned_units": round(
            sum(len(item["returned_unit_ids"]) for item in outcomes) / case_count,
            6,
        ),
    }


def _gate_verdict(
    event_metrics: dict[str, float],
    comparisons: dict[str, float],
    gates: dict[str, float],
) -> dict[str, Any]:
    checks = {
        "answerable_rate": event_metrics["answerable_rate"] >= gates["min_answerable_rate"],
        "anchor_recall": event_metrics["anchor_recall"] >= gates["min_anchor_recall"],
        "forbidden_anchor_rate": (
            event_metrics["forbidden_anchor_rate"] <= gates["max_forbidden_anchor_rate"]
        ),
        "anchor_recall_lift_vs_minute": (
            comparisons["anchor_recall_lift_vs_minute"]
            >= gates["min_anchor_recall_lift_vs_minute"]
        ),
        "context_chars_ratio_vs_session": (
            comparisons["context_chars_ratio_vs_session"]
            <= gates["max_context_chars_ratio_vs_session"]
        ),
        "forbidden_anchor_rate_reduction_vs_session": (
            comparisons["forbidden_anchor_rate_reduction_vs_session"]
            >= gates["min_forbidden_anchor_rate_reduction_vs_session"]
        ),
    }
    return {"passed": all(checks.values()), "checks": checks, "gates": gates}


def _parse_case(raw: object) -> RetrievalCase:
    if not isinstance(raw, dict) or set(raw) != {
        "id",
        "query",
        "required_anchors",
        "forbidden_anchors",
        "observations",
    }:
        raise ValueError("event retrieval case is invalid")
    observations_raw = raw["observations"]
    if not isinstance(observations_raw, list) or not 1 <= len(observations_raw) <= 200:
        raise ValueError("event retrieval observations are required")
    observations = tuple(_parse_observation(item) for item in observations_raw)
    if len({item.id for item in observations}) != len(observations):
        raise ValueError("observation ids must be unique within a case")
    orders = [item.order for item in observations]
    if orders != sorted(orders) or len(set(orders)) != len(orders):
        raise ValueError("observation order must be unique and increasing")
    for event_id in {item.event_id for item in observations}:
        positions = [index for index, item in enumerate(observations) if item.event_id == event_id]
        if positions != list(range(min(positions), max(positions) + 1)):
            raise ValueError("event observations must be contiguous")
    return RetrievalCase(
        id=_identifier(raw["id"]),
        query=_text(raw["query"], 1_000),
        required_anchors=_anchors(raw["required_anchors"]),
        forbidden_anchors=_anchors(raw["forbidden_anchors"], allow_empty=True),
        observations=observations,
    )


def _parse_observation(raw: object) -> Observation:
    if not isinstance(raw, dict) or set(raw) != {
        "id",
        "session_id",
        "event_id",
        "order",
        "text",
    }:
        raise ValueError("event retrieval observation is invalid")
    order = raw["order"]
    if type(order) is not int or order < 0:
        raise ValueError("observation order is invalid")
    return Observation(
        id=_identifier(raw["id"]),
        session_id=_identifier(raw["session_id"]),
        event_id=_identifier(raw["event_id"]),
        order=order,
        text=_text(raw["text"], 10_000),
    )


def _anchors(value: object, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError("anchors are invalid")
    anchors = tuple(_text(item, 1_000) for item in value)
    if len(set(anchors)) != len(anchors):
        raise ValueError("anchors must be unique")
    return anchors


def _validate_contract(contract: object) -> None:
    if not isinstance(contract, dict) or set(contract) != {
        "schema_version",
        "evaluation_id",
        "gates",
    }:
        raise ValueError("event retrieval metric contract is invalid")
    if contract["schema_version"] != 1 or contract["evaluation_id"] != "vida-event-retrieval-v1":
        raise ValueError("event retrieval metric contract identity is invalid")
    gates = contract["gates"]
    expected = {
        "min_answerable_rate",
        "min_anchor_recall",
        "max_forbidden_anchor_rate",
        "min_anchor_recall_lift_vs_minute",
        "max_context_chars_ratio_vs_session",
        "min_forbidden_anchor_rate_reduction_vs_session",
    }
    if not isinstance(gates, dict) or set(gates) != expected:
        raise ValueError("event retrieval gates are invalid")
    if any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1
        for value in gates.values()
    ):
        raise ValueError("event retrieval gate values must be in [0, 1]")


def _identifier(value: object) -> str:
    text = _text(value, 200)
    if not text.replace("-", "").replace("_", "").isalnum():
        raise ValueError("identifier is invalid")
    return text


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError("text value is invalid")
    return value.strip()


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
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    benchmark_root = repository_root / "benchmarks" / "vida-event-retrieval-v1"
    dataset_path = args.dataset or benchmark_root / "json" / "cases.json"
    contract_path = args.contract or benchmark_root / "json" / "metric_contract.json"
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
    return 0 if report["gate_verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
