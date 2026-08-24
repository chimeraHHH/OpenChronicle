#!/usr/bin/env python3
"""Rebuild or verify the frozen unseen MemOps-50 manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from openchronicle.evaluation.memops50_heldout import build_manifest, verify_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memops-root", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("benchmarks/memops50-heldout-v1/json/manifest.json"),
    )
    parser.add_argument(
        "--exclusion",
        type=Path,
        default=Path("benchmarks/memops50-lifecycle-v1/json/selected_pairs.json"),
    )
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    exclusion_path = args.exclusion.resolve()
    if args.write:
        manifest = build_manifest(
            selection_path=manifest_path.with_name("selected_pairs.json"),
            exclusion_path=exclusion_path,
            memops_root=args.memops_root,
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    counts = verify_manifest(
        manifest_path=manifest_path,
        exclusion_path=exclusion_path,
        memops_root=args.memops_root,
    )
    print(
        f"verified {counts['logical_pairs']} held-out logical pairs / "
        f"{counts['rows_per_method']} rows per method"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
