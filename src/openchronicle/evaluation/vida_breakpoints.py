"""Auxiliary threshold comparison for Work Resumption display timing."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Config
from ..local_time import local_timezone
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef
from ..services.context import ContextService
from ..store import fts
from ..suggestions import store as suggestion_store
from ..suggestions.activity import CaptureActivityGate
from ..suggestions.work_resumption import WorkResumptionService
from .vida_suggestions import (
    BlockSpec,
    OpportunityCase,
    _aware,
    _isolated_case_root,
    _repository_state,
    _seed_case,
    _strict_text,
    write_report,
)

THRESHOLDS = (5, 10, 20, 30, 60)
SAMPLE_STATES = {"healthy", "no_sample", "malformed", "backward"}


@dataclass(frozen=True, slots=True)
class ActivityTrace:
    id: str
    simulated_user_day: str
    expected_display: bool
    sample_state: str
    latest_event_age_seconds: int | None


@dataclass(frozen=True, slots=True)
class ActivityDataset:
    id: str
    split: str
    reference_now: datetime
    previous: BlockSpec
    current: BlockSpec
    cases: tuple[ActivityTrace, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class ActivityOutcome:
    case_id: str
    expected_display: bool
    predicted_display: bool
    reason: str
    evidence_current: bool
    additional_delay_seconds: int
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "expected_display": self.expected_display,
            "predicted_display": self.predicted_display,
            "reason": self.reason,
            "evidence_current": self.evidence_current,
            "additional_delay_seconds": self.additional_delay_seconds,
            "latency_ms": round(self.latency_ms, 6),
        }


def load_activity_dataset(path: Path) -> ActivityDataset:
    raw_bytes = path.read_bytes()
    try:
        payload = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("activity trace fixture is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "dataset_id",
        "split",
        "reference_now",
        "context",
        "cases",
    }:
        raise ValueError("activity trace fixture envelope is invalid")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported activity trace fixture schema")
    context = payload["context"]
    if not isinstance(context, dict) or set(context) != {"previous", "current"}:
        raise ValueError("activity trace context is invalid")
    previous = _parse_block(context["previous"])
    current = _parse_block(context["current"])
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("activity trace cases are required")
    cases = tuple(_parse_trace(value) for value in raw_cases)
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("activity trace case ids must be unique")
    return ActivityDataset(
        id=_strict_text(payload["dataset_id"]),
        split=_strict_text(payload["split"]),
        reference_now=_aware(datetime.fromisoformat(_strict_text(payload["reference_now"]))),
        previous=previous,
        current=current,
        cases=cases,
        digest=hashlib.sha256(raw_bytes).hexdigest(),
    )


def run_breakpoint_evaluation(
    *,
    dataset_path: Path,
    repository_root: Path,
    latency_repeats: int = 3,
) -> dict[str, Any]:
    if type(latency_repeats) is not int or not 1 <= latency_repeats <= 20:
        raise ValueError("latency_repeats must be in [1, 20]")
    dataset = load_activity_dataset(dataset_path)
    variants: dict[str, Any] = {}
    for threshold in (None, *THRESHOLDS):
        name = "no_gate" if threshold is None else f"settle_{threshold}s"
        outcomes = [
            _evaluate_repeated(
                dataset,
                trace,
                threshold=threshold,
                repeats=latency_repeats,
            )
            for trace in dataset.cases
        ]
        variants[name] = {
            "settle_seconds": threshold,
            "metrics": _metrics(outcomes, dataset.cases),
            "cases": [outcome.to_dict() for outcome in outcomes],
        }
    return {
        "schema_version": 1,
        "evaluation_id": "vida-work-resumption-breakpoint-dev-v1",
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
            "label_status": "synthetic_engineering_hypotheses_not_independent_ground_truth",
        },
        "latency_repeats": latency_repeats,
        "variants": variants,
        "experiment_status": {
            "formal_gate": "blocked_unregistered_auxiliary",
            "claim_limit": (
                "Threshold sensitivity only; this development split cannot promote "
                "the canonical baseline or establish field interruption quality."
            ),
        },
    }


def _evaluate_repeated(
    dataset: ActivityDataset,
    trace: ActivityTrace,
    *,
    threshold: int | None,
    repeats: int,
) -> ActivityOutcome:
    runs = [_evaluate_once(dataset, trace, threshold=threshold) for _ in range(repeats)]
    signatures = {
        (
            run.predicted_display,
            run.reason,
            run.evidence_current,
            run.additional_delay_seconds,
        )
        for run in runs
    }
    if len(signatures) != 1:
        raise RuntimeError(f"non-deterministic breakpoint decision for {trace.id}")
    first = runs[0]
    return ActivityOutcome(
        case_id=trace.id,
        expected_display=trace.expected_display,
        predicted_display=first.predicted_display,
        reason=first.reason,
        evidence_current=first.evidence_current,
        additional_delay_seconds=first.additional_delay_seconds,
        latency_ms=max(run.latency_ms for run in runs),
    )


def _evaluate_once(
    dataset: ActivityDataset,
    trace: ActivityTrace,
    *,
    threshold: int | None,
) -> ActivityOutcome:
    opportunity = OpportunityCase(
        id=trace.id,
        simulated_user_day=trace.simulated_user_day,
        expected_help=trace.expected_display,
        explicit_invocation=False,
        gates=(),
        previous=dataset.previous,
        current=dataset.current,
    )
    with _isolated_case_root(trace.id), fts.cursor() as conn:
        cfg = Config()
        cfg.capture.deny_unknown_windows = False
        cfg.suggestions.enabled = True
        cfg.suggestions.quiet_hours_enabled = False
        _blocks, _refs = _seed_case(conn, opportunity, now=dataset.reference_now)
        gate = None
        now_tick = None
        if threshold is not None:
            cfg.suggestions.work_resumption_settle_seconds = threshold
            gate = CaptureActivityGate()
            now_tick = 10_000.0
            _prepare_gate(gate, trace, now=dataset.reference_now, now_tick=now_tick)

        started = time.perf_counter_ns()
        decision = WorkResumptionService(conn, cfg).scan(
            now=dataset.reference_now,
            activity_gate=gate,
            now_tick=now_tick,
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        evidence_current = True
        if decision.emitted and decision.suggestion is not None:
            sources = provenance_store.direct_sources_checked(
                conn,
                EvidenceRef(kind="suggestion", id=decision.suggestion.id),
            )
            evidence_current = bool(
                sources
                and suggestion_store.evidence_digest(sources) == decision.suggestion.evidence_digest
                and all(ContextService(conn, cfg).evidence_allowed(ref) for ref in sources)
            )
        delay = 0
        if (
            trace.expected_display
            and threshold is not None
            and trace.sample_state == "healthy"
            and trace.latest_event_age_seconds is not None
        ):
            delay = max(0, threshold - trace.latest_event_age_seconds)
        return ActivityOutcome(
            case_id=trace.id,
            expected_display=trace.expected_display,
            predicted_display=decision.emitted,
            reason=decision.reason,
            evidence_current=evidence_current,
            additional_delay_seconds=delay,
            latency_ms=elapsed_ms,
        )


def _prepare_gate(
    gate: CaptureActivityGate,
    trace: ActivityTrace,
    *,
    now: datetime,
    now_tick: float,
) -> None:
    if trace.sample_state == "no_sample":
        return
    if trace.sample_state == "malformed":
        try:
            gate.on_persisted_capture(
                {"timestamp": now.isoformat(), "bundle_id": "com.example.editor"}
            )
        except ValueError:
            return
        raise RuntimeError("malformed activity fixture was unexpectedly accepted")
    if trace.sample_state == "backward":
        gate.on_persisted_capture(
            {
                "timestamp": (now - timedelta(seconds=2)).isoformat(),
                "bundle_id": "com.example.editor",
                "_persisted_monotonic_tick": now_tick - 2,
            }
        )
        try:
            gate.on_persisted_capture(
                {
                    "timestamp": (now - timedelta(seconds=1)).isoformat(),
                    "bundle_id": "com.example.editor",
                    "_persisted_monotonic_tick": now_tick - 3,
                }
            )
        except ValueError:
            return
        raise RuntimeError("backwards activity fixture was unexpectedly accepted")
    age = trace.latest_event_age_seconds
    if age is None:
        raise ValueError("healthy activity trace requires an age")
    gate.on_persisted_capture(
        {
            "timestamp": (now - timedelta(seconds=age)).isoformat(),
            "bundle_id": "com.example.editor",
            "_persisted_monotonic_tick": now_tick - age,
        }
    )


def _metrics(
    outcomes: list[ActivityOutcome],
    cases: tuple[ActivityTrace, ...],
) -> dict[str, Any]:
    true_positive = sum(item.expected_display and item.predicted_display for item in outcomes)
    false_positive = sum(not item.expected_display and item.predicted_display for item in outcomes)
    false_negative = sum(item.expected_display and not item.predicted_display for item in outcomes)
    true_negative = sum(
        not item.expected_display and not item.predicted_display for item in outcomes
    )
    emitted = true_positive + false_positive
    positive_count = true_positive + false_negative
    latencies = sorted(item.latency_ms for item in outcomes)
    delays = sorted(item.additional_delay_seconds for item in outcomes if item.expected_display)
    return {
        "display_precision": round(true_positive / emitted if emitted else 1.0, 6),
        "display_recall_at_snapshot": round(
            true_positive / positive_count if positive_count else 1.0,
            6,
        ),
        "evidence_coverage": round(
            sum(item.predicted_display and item.evidence_current for item in outcomes) / emitted
            if emitted
            else 1.0,
            6,
        ),
        "invalid_interruptions_per_user_day": round(
            false_positive / len({case.simulated_user_day for case in cases}),
            6,
        ),
        "deferred_positive_count": false_negative,
        "additional_delay_p95_seconds": delays[max(0, math.ceil(len(delays) * 0.95) - 1)],
        "decision_latency_p95_ms": round(
            latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)],
            6,
        ),
        "confusion": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
        },
        "reason_counts": dict(sorted(Counter(item.reason for item in outcomes).items())),
    }


def _parse_trace(value: object) -> ActivityTrace:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "simulated_user_day",
        "expected_display",
        "sample_state",
        "latest_event_age_seconds",
    }:
        raise ValueError("activity trace case schema is invalid")
    state = _strict_text(value["sample_state"])
    if state not in SAMPLE_STATES or type(value["expected_display"]) is not bool:
        raise ValueError("activity trace case value is invalid")
    age = value["latest_event_age_seconds"]
    if state == "healthy":
        if type(age) is not int or not 0 <= age <= 3_600:
            raise ValueError("healthy activity trace age is invalid")
    elif age is not None:
        raise ValueError("unhealthy activity trace must not carry an age")
    return ActivityTrace(
        id=_strict_text(value["id"]),
        simulated_user_day=_strict_text(value["simulated_user_day"]),
        expected_display=value["expected_display"],
        sample_state=state,
        latest_event_age_seconds=age,
    )


def _parse_block(value: object) -> BlockSpec:
    if not isinstance(value, dict) or set(value) != {
        "start_minutes_ago",
        "end_minutes_ago",
        "entries",
        "apps",
    }:
        raise ValueError("activity trace block schema is invalid")
    start = value["start_minutes_ago"]
    end = value["end_minutes_ago"]
    entries = value["entries"]
    apps = value["apps"]
    if (
        type(start) is not int
        or type(end) is not int
        or start <= end
        or not isinstance(entries, list)
        or not all(isinstance(item, str) for item in entries)
        or not isinstance(apps, list)
        or not all(isinstance(item, str) for item in apps)
    ):
        raise ValueError("activity trace block value is invalid")
    return BlockSpec(start, end, tuple(entries), tuple(apps))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--latency-repeats", type=int, default=3)
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    dataset_path = args.dataset or (
        repository_root / "benchmarks" / "vida-suggestions-v1" / "fixtures" / "activity_traces.json"
    )
    if not dataset_path.is_absolute():
        dataset_path = repository_root / dataset_path
    report = run_breakpoint_evaluation(
        dataset_path=dataset_path,
        repository_root=repository_root,
        latency_repeats=args.latency_repeats,
    )
    encoded = write_report(report, args.output)
    if not args.quiet:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
