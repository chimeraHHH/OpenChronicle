#!/usr/bin/env python3
"""Provision and verify the frozen LECS ONNX snapshot."""

from __future__ import annotations

import argparse
from pathlib import Path

from openchronicle.evaluation import lecs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("benchmarks/memops50-lecs-v1/json/model_artifact_manifest.json"),
    )
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    artifact_manifest_path = args.manifest.resolve()
    selection_contract_path = artifact_manifest_path.with_name("selection_metric_contract.json")
    manifest = lecs.load_frozen_artifact_manifest(
        artifact_manifest_path=artifact_manifest_path,
        selection_contract_path=selection_contract_path,
    )
    model_dir = args.model_dir.expanduser().resolve()
    if args.download:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id="Xenova/ms-marco-MiniLM-L-6-v2",
            revision=manifest["upstream"]["revision"],
            allow_patterns=[item["path"] for item in manifest["files"]],
            local_dir=model_dir,
        )
    digest = lecs.verify_model_artifacts(model_dir, manifest)
    lecs.verify_runtime(manifest, dependency_lock=Path("uv.lock").resolve())
    print(f"verified LECS model artifact sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
