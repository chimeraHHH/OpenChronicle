#!/usr/bin/env python3
"""Rebuild or verify the frozen external MemOps-50 manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from openchronicle.evaluation.memops50 import (
    build_decision_manifest,
    build_manifest,
    verify_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memops-root", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("benchmarks/memops50-lifecycle-v1/json/manifest.json"),
    )
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    if args.write:
        manifest = build_manifest(
            selection_path=manifest_path.with_name("selected_pairs.json"),
            memops_root=args.memops_root,
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        decision_manifest_path = manifest_path.with_name("decision_manifest.json")
        decision_manifest_path.write_text(
            json.dumps(build_decision_manifest(manifest), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {manifest_path}")
    counts = verify_manifest(manifest_path=manifest_path, memops_root=args.memops_root)
    manifest = json.loads(manifest_path.read_bytes())
    decision_manifest_path = manifest_path.with_name("decision_manifest.json")
    if json.loads(decision_manifest_path.read_bytes()) != build_decision_manifest(manifest):
        raise ValueError("MemOps-50 decision manifest differs from the frozen projection")
    print(
        f"verified {counts['logical_pairs']} logical pairs / "
        f"{counts['rows_per_method']} rows per method"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
