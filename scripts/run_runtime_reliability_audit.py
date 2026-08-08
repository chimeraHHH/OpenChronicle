#!/usr/bin/env python3
"""Run deterministic Stage 0 replay and soak validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

from runtime_reliability_lib import (
    DEFAULT_MANIFEST_PATH,
    AuditError,
    load_manifest,
    run_full_audit,
    soak_worker,
    verify_report,
    write_json_atomic,
)


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _replay_count(value: str) -> int:
    parsed = int(value)
    if not 4 <= parsed <= 100_000:
        raise argparse.ArgumentTypeError("must be between 4 and 100000")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay deterministic synthetic captures and run a sampled soak. "
            "A short smoke produces status=incomplete by design."
        )
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--replay-count", type=_replay_count, default=10_000)
    duration = parser.add_mutually_exclusive_group()
    duration.add_argument("--soak-seconds", type=_positive_float, default=30.0)
    duration.add_argument("--soak-hours", type=_positive_float)
    parser.add_argument("--sample-interval-seconds", type=_positive_float, default=5.0)
    parser.add_argument("--work-interval-seconds", type=_positive_float, default=1.0)
    return parser


def _worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("_mode")
    parser.add_argument("--duration-seconds", type=_positive_float, required=True)
    parser.add_argument("--work-interval-seconds", type=_positive_float, required=True)
    return parser


def _run_worker(argv: list[str]) -> int:
    args = _worker_parser().parse_args(argv)
    if args._mode != "_soak-worker":
        return 2
    result = soak_worker(
        duration_seconds=args.duration_seconds,
        work_interval_seconds=args.work_interval_seconds,
    )
    encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 64 * 1024:
        return 3
    sys.stdout.write(encoded)
    sys.stdout.flush()
    return 0 if result["completed_requested_duration"] else 4


def main(argv: list[str] | None = None) -> int:
    actual = list(sys.argv[1:] if argv is None else argv)
    if actual and actual[0] == "_soak-worker":
        return _run_worker(actual)
    args = _parser().parse_args(actual)
    manifest = load_manifest(args.manifest)
    duration_seconds = args.soak_hours * 3600 if args.soak_hours is not None else args.soak_seconds
    workspace = Path(tempfile.mkdtemp(prefix="openchronicle-runtime-audit-"))
    try:
        report = run_full_audit(
            workspace,
            replay_count=args.replay_count,
            soak_duration_seconds=duration_seconds,
            sample_interval_seconds=args.sample_interval_seconds,
            work_interval_seconds=args.work_interval_seconds,
            manifest=manifest,
        )
        verification = verify_report(report, manifest)
        if not verification["valid"]:
            raise AuditError("generated report failed structural verification")
        write_json_atomic(args.report, report)
        artifact_sha256 = hashlib.sha256(
            args.report.expanduser().resolve().read_bytes()
        ).hexdigest()
    finally:
        shutil.rmtree(workspace)
    summary = {
        "report": str(args.report.expanduser().resolve()),
        "status": report["status"],
        "storage_harness_complete": report["acceptance"]["storage_harness_complete"],
        "replay_meets_10000_minimum": report["acceptance"][
            "replay_meets_10000_minimum"
        ],
        "soak_meets_24h_minimum": report["acceptance"]["soak_meets_24h_minimum"],
        "production_daemon_queue_measured": report["acceptance"][
            "production_daemon_queue_measured"
        ],
        "artifact_sha256": artifact_sha256,
        "internally_consistent": verification["valid"],
        "verification_scope": "closed-schema-and-internal-consistency-only",
        "authenticity_verified": False,
    }
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0 if report["status"] in {"complete", "incomplete"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
