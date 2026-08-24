#!/usr/bin/env python3
"""Verify a Stage 0 reliability JSON report against the versioned policy."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from runtime_reliability_lib import DEFAULT_MANIFEST_PATH, load_manifest, verify_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument(
        "--expect",
        choices=("complete", "storage-complete", "incomplete", "either"),
        default="complete",
        help=(
            "Use incomplete for smoke, storage-complete for 10k + >=24h, and "
            "complete only after a future schema verifies the production daemon queue."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = load_manifest(args.manifest)
    report_bytes = args.report.read_bytes()
    report = json.loads(report_bytes.decode("utf-8"))
    result = verify_report(report, manifest)
    evaluation = result.get("evaluation") or {}
    actual = evaluation.get("status")
    expected = (
        (args.expect == "either" and actual in {"complete", "incomplete"})
        or actual == args.expect
        or (
            args.expect == "storage-complete"
            and evaluation.get("storage_harness_complete") is True
        )
    )
    output = {
        "valid": result["valid"],
        "internally_consistent": result["valid"],
        "actual_status": actual,
        "expected_status": args.expect,
        "expectation_met": expected,
        "replay_meets_10000_minimum": evaluation.get("replay_meets_minimum", False),
        "soak_meets_24h_minimum": evaluation.get("soak_meets_24h_minimum", False),
        "storage_harness_complete": evaluation.get("storage_harness_complete", False),
        "production_daemon_queue_measured": evaluation.get(
            "production_daemon_queue_measured", False
        ),
        "artifact_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "verification_scope": "closed-schema-and-internal-consistency-only",
        "authenticity_verified": False,
    }
    print(json.dumps(output, ensure_ascii=True, sort_keys=True))
    return 0 if result["valid"] and expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
