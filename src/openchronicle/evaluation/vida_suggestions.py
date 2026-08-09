"""Reproducible Work Resumption opportunity comparison on local fixtures."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import paths
from ..capture import scheduler
from ..config import Config
from ..local_time import local_timezone
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, observation_digest, timeline_block_digest
from ..services.context import ContextService
from ..store import fts
from ..suggestions import store as suggestion_store
from ..suggestions.service import (
    WORK_RESUMPTION_NEXT_STEP,
    SuggestionKernel,
    SuggestionProposal,
)
from ..suggestions.work_resumption import WorkResumptionService, assess_work_resumption
from ..timeline import store as timeline_store

VARIANTS = ("reactive", "heuristic", "kernel")
ALLOWED_GATES = {
    "below_threshold",
    "budget_exhausted",
    "policy_excluded",
    "prior_identical",
    "quiet_hours",
    "suggestions_disabled",
}


@dataclass(frozen=True, slots=True)
class BlockSpec:
    start_minutes_ago: int
    end_minutes_ago: int
    entries: tuple[str, ...]
    apps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpportunityCase:
    id: str
    simulated_user_day: str
    expected_help: bool
    explicit_invocation: bool
    gates: tuple[str, ...]
    previous: BlockSpec
    current: BlockSpec | None


@dataclass(frozen=True, slots=True)
class Dataset:
    id: str
    split: str
    reference_now: datetime
    cases: tuple[OpportunityCase, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case_id: str
    expected_help: bool
    predicted_help: bool
    reason: str
    evidence_current: bool
    unsupported_claim: bool
    semantic_duplicates: int
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "expected_help": self.expected_help,
            "predicted_help": self.predicted_help,
            "reason": self.reason,
            "evidence_current": self.evidence_current,
            "unsupported_claim": self.unsupported_claim,
            "semantic_duplicates": self.semantic_duplicates,
            "latency_ms": round(self.latency_ms, 6),
        }


def load_dataset(path: Path) -> Dataset:
    raw_bytes = path.read_bytes()
    try:
        payload = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("opportunity fixture is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "dataset_id",
        "split",
        "reference_now",
        "cases",
    }:
        raise ValueError("opportunity fixture envelope is invalid")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported opportunity fixture schema")
    reference_now = _aware(datetime.fromisoformat(_strict_text(payload["reference_now"])))
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("opportunity fixture cases are required")
    cases = tuple(_parse_case(value) for value in raw_cases)
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("opportunity fixture case ids must be unique")
    return Dataset(
        id=_strict_text(payload["dataset_id"]),
        split=_strict_text(payload["split"]),
        reference_now=reference_now,
        cases=cases,
        digest=hashlib.sha256(raw_bytes).hexdigest(),
    )


def run_evaluation(
    *,
    dataset_path: Path,
    metric_contract_path: Path,
    repository_root: Path,
    latency_repeats: int = 3,
) -> dict[str, Any]:
    if type(latency_repeats) is not int or not 1 <= latency_repeats <= 20:
        raise ValueError("latency_repeats must be in [1, 20]")
    dataset = load_dataset(dataset_path)
    contract_bytes = metric_contract_path.read_bytes()
    contract = json.loads(contract_bytes)
    _validate_contract(contract, dataset)

    variant_results: dict[str, Any] = {}
    for variant in VARIANTS:
        outcomes = [
            _evaluate_case_repeated(
                case,
                now=dataset.reference_now,
                variant=variant,
                repeats=latency_repeats,
            )
            for case in dataset.cases
        ]
        variant_results[variant] = {
            "metrics": _metrics(outcomes, dataset.cases),
            "cases": [outcome.to_dict() for outcome in outcomes],
        }

    gates = contract["stage2_gates"]
    for result in variant_results.values():
        result["gate_verdict"] = _gate_verdict(result["metrics"], gates)

    return {
        "schema_version": 1,
        "evaluation_id": "vida-work-resumption-v1",
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
        "latency_repeats": latency_repeats,
        "variants": variant_results,
        "baseline_status": {
            "comparator": "reactive",
            "local_verification": "trusted_with_caveats",
            "formal_gate": "blocked_unregistered",
            "reason": (
                "The exact local comparator and metrics are reproducible, but the current "
                "session has no artifact.confirm_baseline capability and a dirty result "
                "cannot be promoted."
            ),
        },
    }


def _evaluate_case_repeated(
    case: OpportunityCase,
    *,
    now: datetime,
    variant: str,
    repeats: int,
) -> CaseOutcome:
    runs = [_evaluate_once(case, now=now, variant=variant) for _ in range(repeats)]
    first = runs[0]
    signatures = {
        (
            run.predicted_help,
            run.reason,
            run.evidence_current,
            run.unsupported_claim,
            run.semantic_duplicates,
        )
        for run in runs
    }
    if len(signatures) != 1:
        raise RuntimeError(f"non-deterministic decision for {variant}/{case.id}")
    return CaseOutcome(
        case_id=case.id,
        expected_help=case.expected_help,
        predicted_help=first.predicted_help,
        reason=first.reason,
        evidence_current=first.evidence_current,
        unsupported_claim=first.unsupported_claim,
        semantic_duplicates=first.semantic_duplicates,
        latency_ms=max(run.latency_ms for run in runs),
    )


def _evaluate_once(
    case: OpportunityCase,
    *,
    now: datetime,
    variant: str,
) -> CaseOutcome:
    if variant not in VARIANTS:
        raise ValueError("unknown suggestion evaluation variant")
    with _isolated_case_root(case.id), fts.cursor() as conn:
        cfg = Config()
        cfg.capture.deny_unknown_windows = False
        cfg.suggestions.enabled = True
        cfg.suggestions.quiet_hours_enabled = False
        blocks, refs = _seed_case(conn, case, now=now)
        _apply_gates(conn, cfg, case, blocks=blocks, refs=refs, now=now)

        started = time.perf_counter_ns()
        if variant == "reactive" and not case.explicit_invocation:
            predicted = False
            reason = "reactive_not_invoked"
            suggestion = None
        elif variant == "heuristic":
            boundary, reason = assess_work_resumption(blocks, cfg, now=now)
            predicted = boundary is not None
            suggestion = None
        else:
            decision = WorkResumptionService(conn, cfg).scan(now=now)
            predicted = decision.emitted
            reason = decision.reason
            suggestion = decision.suggestion if decision.emitted else None
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000

        evidence_current = False
        unsupported_claim = False
        if predicted and variant == "heuristic":
            evidence_current = len(refs) == 2 and all(
                ContextService(conn, cfg).evidence_allowed(ref) for ref in refs
            )
        elif predicted and suggestion is not None:
            sources = provenance_store.direct_sources_checked(
                conn,
                EvidenceRef(kind="suggestion", id=suggestion.id),
            )
            evidence_current = bool(
                sources
                and len(sources) == 2
                and suggestion_store.evidence_digest(sources) == suggestion.evidence_digest
                and all(ContextService(conn, cfg).evidence_allowed(ref) for ref in sources)
            )
            unsupported_claim = not _artifact_supported(suggestion.artifact, blocks)

        duplicates = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(count_per_key - 1), 0)
                  FROM (
                    SELECT COUNT(*) AS count_per_key
                      FROM suggestions
                     GROUP BY semantic_key
                    HAVING COUNT(*) > 1
                  )
                """
            ).fetchone()[0]
        )
        return CaseOutcome(
            case_id=case.id,
            expected_help=case.expected_help,
            predicted_help=predicted,
            reason=reason,
            evidence_current=evidence_current if predicted else True,
            unsupported_claim=unsupported_claim,
            semantic_duplicates=duplicates,
            latency_ms=elapsed_ms,
        )


@contextmanager
def _isolated_case_root(case_id: str):
    previous = os.environ.get("OPENCHRONICLE_ROOT")
    with tempfile.TemporaryDirectory(prefix=f"oc-vida-{case_id[:24]}-") as temp_dir:
        os.environ["OPENCHRONICLE_ROOT"] = temp_dir
        paths.ensure_dirs()
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("OPENCHRONICLE_ROOT", None)
            else:
                os.environ["OPENCHRONICLE_ROOT"] = previous


def _seed_case(
    conn: sqlite3.Connection,
    case: OpportunityCase,
    *,
    now: datetime,
) -> tuple[list[timeline_store.TimelineBlock], list[EvidenceRef]]:
    specs = [("previous", case.previous)]
    if case.current is not None:
        specs.append(("current", case.current))
    blocks: list[timeline_store.TimelineBlock] = []
    refs: list[EvidenceRef] = []
    for role, spec in specs:
        block, ref = _seed_block(conn, case.id, role, spec, now=now)
        blocks.append(block)
        refs.append(ref)
    blocks.sort(key=lambda item: timeline_store.as_instant(item.start_time))
    refs_by_id = {ref.id: ref for ref in refs}
    return blocks, [refs_by_id[block.id] for block in blocks]


def _seed_block(
    conn: sqlite3.Connection,
    case_id: str,
    role: str,
    spec: BlockSpec,
    *,
    now: datetime,
) -> tuple[timeline_store.TimelineBlock, EvidenceRef]:
    start = now - timedelta(minutes=spec.start_minutes_ago)
    end = now - timedelta(minutes=spec.end_minutes_ago)
    app = spec.apps[0] if spec.apps else "Editor"
    visible_text = "\n".join(spec.entries)
    capture = {
        "timestamp": end.isoformat(),
        "schema_version": 4,
        "window_meta": {
            "app_name": app,
            "bundle_id": f"dev.openchronicle.fixture.{role}",
            "title": f"{case_id} {role}",
        },
        "focused_element": {"role": "AXTextArea", "value": visible_text},
        "visible_text": visible_text,
        "url": "",
    }
    capture_path = scheduler._write_capture(capture)
    observation = EvidenceRef(
        kind="observation",
        id=str(capture["observation_id"]),
        path=capture_path.name,
        timestamp=str(capture["timestamp"]),
        content_hash=observation_digest(capture),
    )
    block = timeline_store.TimelineBlock(
        id=f"tlb-{case_id}-{role}",
        start_time=start,
        end_time=end,
        timezone="UTC",
        entries=list(spec.entries),
        apps_used=list(spec.apps),
        capture_count=1,
    )
    timeline_store.insert(conn, block)
    provenance_store.replace_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id=block.id),
        sources=[observation],
    )
    return block, EvidenceRef(
        kind="timeline_block",
        id=block.id,
        timestamp=block.start_time.isoformat(),
        content_hash=timeline_block_digest(
            start=block.start_time.isoformat(),
            end=block.end_time.isoformat(),
            entries=block.entries,
            apps=block.apps_used,
        ),
    )


def _apply_gates(
    conn: sqlite3.Connection,
    cfg: Config,
    case: OpportunityCase,
    *,
    blocks: list[timeline_store.TimelineBlock],
    refs: list[EvidenceRef],
    now: datetime,
) -> None:
    gates = set(case.gates)
    if "quiet_hours" in gates:
        hour = now.astimezone(local_timezone()).hour
        cfg.suggestions.quiet_hours_enabled = True
        cfg.suggestions.quiet_hours_start = hour
        cfg.suggestions.quiet_hours_end = (hour + 1) % 24
    if "below_threshold" in gates:
        cfg.suggestions.min_score = 0.95
    if "budget_exhausted" in gates:
        if len(blocks) != 2 or len(refs) != 2:
            raise ValueError("budget fixture requires two blocks")
        for index in range(cfg.suggestions.daily_budget):
            detected_at = now - timedelta(minutes=10 - index)
            decision = SuggestionKernel(conn, cfg).emit(
                SuggestionProposal(
                    semantic_key=f"budget-seed:{case.id}:{index}",
                    workflow="work_resumption",
                    title="Budget seed",
                    summary="A deterministic prior opportunity for budget evaluation.",
                    artifact=_artifact(blocks[0], blocks[1]),
                    evidence=tuple(refs),
                    score=0.9,
                    expires_at=now + timedelta(hours=1),
                ),
                now=detected_at,
            )
            if not decision.emitted:
                raise RuntimeError("budget fixture did not seed its prior opportunity")
    if "prior_identical" in gates:
        decision = WorkResumptionService(conn, cfg).scan(now=now)
        if not decision.emitted:
            raise RuntimeError("duplicate fixture did not seed its prior opportunity")
    if "policy_excluded" in gates:
        cfg.capture.excluded_app_names = ["Editor"]
    if "suggestions_disabled" in gates:
        cfg.suggestions.enabled = False


def _artifact(
    previous: timeline_store.TimelineBlock,
    current: timeline_store.TimelineBlock,
) -> dict[str, Any]:
    gap = timeline_store.as_instant(current.start_time) - timeline_store.as_instant(
        previous.end_time
    )
    return {
        "schema_version": 1,
        "workflow": "work_resumption",
        "action_capability": "none",
        "interruption": {
            "previous_end": previous.end_time.isoformat(),
            "current_start": current.start_time.isoformat(),
            "gap_minutes": round(gap.total_seconds() / 60, 2),
        },
        "last_verified_state": {
            "untrusted_activity_quote": True,
            "entries": [str(value)[:500] for value in previous.entries[-5:]],
            "apps": [str(value)[:200] for value in previous.apps_used[:10]],
        },
        "resumption_signal": {
            "untrusted_activity_quote": True,
            "entries": [str(value)[:500] for value in current.entries[-3:]],
            "apps": [str(value)[:200] for value in current.apps_used[:10]],
        },
        "recommended_next_step": WORK_RESUMPTION_NEXT_STEP,
    }


def _artifact_supported(
    artifact: dict[str, Any],
    blocks: list[timeline_store.TimelineBlock],
) -> bool:
    if len(blocks) != 2:
        return False
    return artifact == _artifact(blocks[0], blocks[1])


def _metrics(
    outcomes: list[CaseOutcome],
    cases: tuple[OpportunityCase, ...],
) -> dict[str, Any]:
    true_positive = sum(item.expected_help and item.predicted_help for item in outcomes)
    false_positive = sum(not item.expected_help and item.predicted_help for item in outcomes)
    false_negative = sum(item.expected_help and not item.predicted_help for item in outcomes)
    true_negative = sum(not item.expected_help and not item.predicted_help for item in outcomes)
    emitted = true_positive + false_positive
    precision = true_positive / emitted if emitted else 1.0
    recall_denominator = true_positive + false_negative
    recall = true_positive / recall_denominator if recall_denominator else 1.0
    covered = sum(item.predicted_help and item.evidence_current for item in outcomes)
    unsupported = sum(item.predicted_help and item.unsupported_claim for item in outcomes)
    latencies = sorted(item.latency_ms for item in outcomes)
    p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1)
    return {
        "opportunity_precision": round(precision, 6),
        "opportunity_recall": round(recall, 6),
        "evidence_coverage": round(covered / emitted if emitted else 1.0, 6),
        "unsupported_claim_rate": round(unsupported / emitted if emitted else 0.0, 6),
        "semantic_duplicate_count": sum(item.semantic_duplicates for item in outcomes),
        "invalid_interruptions_per_user_day": round(
            false_positive / len({case.simulated_user_day for case in cases}), 6
        ),
        "decision_latency_p95_ms": round(latencies[p95_index], 6),
        "confusion": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
        },
        "reason_counts": dict(sorted(Counter(item.reason for item in outcomes).items())),
    }


def _gate_verdict(metrics: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "opportunity_precision": metrics["opportunity_precision"] >= gates["opportunity_precision"],
        "evidence_coverage": metrics["evidence_coverage"] >= gates["evidence_coverage"],
        "unsupported_claim_rate": metrics["unsupported_claim_rate"]
        <= gates["unsupported_claim_rate"],
        "semantic_duplicate_count": metrics["semantic_duplicate_count"]
        <= gates["semantic_duplicate_count"],
        "invalid_interruptions_per_user_day": metrics["invalid_interruptions_per_user_day"]
        <= gates["invalid_interruptions_per_user_day_max"],
        "decision_latency_p95_ms": metrics["decision_latency_p95_ms"]
        <= gates["decision_latency_p95_ms_max"],
    }
    return {"passed": all(checks.values()), "checks": checks}


def _parse_case(value: object) -> OpportunityCase:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "simulated_user_day",
        "expected_help",
        "explicit_invocation",
        "gates",
        "previous",
        "current",
    }:
        raise ValueError("opportunity fixture case is invalid")
    if type(value["expected_help"]) is not bool or type(value["explicit_invocation"]) is not bool:
        raise ValueError("opportunity fixture labels must be booleans")
    raw_gates = value["gates"]
    if (
        not isinstance(raw_gates, list)
        or not all(isinstance(item, str) for item in raw_gates)
        or len(set(raw_gates)) != len(raw_gates)
        or set(raw_gates) - ALLOWED_GATES
    ):
        raise ValueError("opportunity fixture gates are invalid")
    return OpportunityCase(
        id=_strict_text(value["id"]),
        simulated_user_day=_strict_text(value["simulated_user_day"]),
        expected_help=value["expected_help"],
        explicit_invocation=value["explicit_invocation"],
        gates=tuple(raw_gates),
        previous=_parse_block(value["previous"]),
        current=_parse_block(value["current"]) if value["current"] is not None else None,
    )


def _parse_block(value: object) -> BlockSpec:
    if not isinstance(value, dict) or set(value) != {
        "start_minutes_ago",
        "end_minutes_ago",
        "entries",
        "apps",
    }:
        raise ValueError("opportunity fixture block is invalid")
    start = value["start_minutes_ago"]
    end = value["end_minutes_ago"]
    entries = value["entries"]
    apps = value["apps"]
    if (
        type(start) is not int
        or type(end) is not int
        or not -60 <= end < start <= 10_080
        or not isinstance(entries, list)
        or len(entries) > 20
        or not all(isinstance(item, str) and len(item) <= 500 for item in entries)
        or not isinstance(apps, list)
        or len(apps) > 20
        or not all(isinstance(item, str) and len(item) <= 200 for item in apps)
    ):
        raise ValueError("opportunity fixture block values are invalid")
    return BlockSpec(start, end, tuple(entries), tuple(apps))


def _validate_contract(contract: object, dataset: Dataset) -> None:
    if not isinstance(contract, dict):
        raise ValueError("metric contract must be an object")
    fixture = contract.get("dataset")
    if (
        contract.get("schema_version") != 1
        or not isinstance(fixture, dict)
        or fixture.get("id") != dataset.id
        or fixture.get("split") != dataset.split
        or contract.get("primary_metric") != "opportunity_precision"
        or set(contract.get("variants", {})) != set(VARIANTS)
        or not isinstance(contract.get("stage2_gates"), dict)
    ):
        raise ValueError("metric contract does not match the opportunity dataset")


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
        raise ValueError("fixture text field is invalid")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation timestamp must be timezone-aware")
    return value.astimezone(UTC)


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
    parser.add_argument("--latency-repeats", type=int, default=3)
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    baseline_root = repository_root / "benchmarks" / "vida-suggestions-v1"
    dataset_path = args.dataset or baseline_root / "fixtures" / "opportunities.json"
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
