#!/usr/bin/env python3
"""Run a public real-trajectory retrieval smoke without the full 1.20 GB dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

import requests
from huggingface_hub import hf_hub_url

from openchronicle.evaluation.longmemeval_v2 import (
    DATASET_REPOSITORY,
    DATASET_REVISION,
    UPSTREAM_COMMIT,
    UPSTREAM_REPOSITORY,
    AdapterConfig,
    LongMemEvalV2Corpus,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--query", default="How do I assign incidents to the relevant agents?")
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    raw_line, trajectory, download_ms = _first_public_trajectory()
    corpus = LongMemEvalV2Corpus(AdapterConfig(top_k=3, candidate_k=20))
    try:
        insert_started = time.perf_counter_ns()
        corpus.insert(trajectory)
        insert_ms = _elapsed_ms(insert_started)
        query_started = time.perf_counter_ns()
        contexts = corpus.query(args.query)
        query_ms = _elapsed_ms(query_started)
        trajectory_id = str(trajectory["id"])
        report = {
            "schema_version": 1,
            "evaluation_id": "longmemeval-v2-real-trajectory-retrieval-smoke",
            "repository": _repository_state(repository_root),
            "upstream": {
                "repository": UPSTREAM_REPOSITORY,
                "commit": UPSTREAM_COMMIT,
                "dataset_repository": DATASET_REPOSITORY,
                "dataset_revision": DATASET_REVISION,
                "source_file": "trajectories.jsonl",
            },
            "fixture": {
                "trajectory_id": trajectory_id,
                "line_bytes": len(raw_line),
                "line_sha256": hashlib.sha256(raw_line).hexdigest(),
                "state_count": len(trajectory.get("states", [])),
                "download_latency_ms": download_ms,
            },
            "variant": {
                **corpus.metadata(),
                "query": args.query,
                "insert_latency_ms": insert_ms,
                "query_latency_ms": query_ms,
                "returned_context_items": len(contexts),
                "text_only": all(item.get("type") == "text" for item in contexts),
                "source_identity_present": bool(
                    contexts and trajectory_id in contexts[0].get("value", "")
                ),
            },
        }
    finally:
        corpus.close()
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


def _first_public_trajectory() -> tuple[bytes, dict[str, object], float]:
    url = hf_hub_url(
        DATASET_REPOSITORY,
        "trajectories.jsonl",
        repo_type="dataset",
        revision=DATASET_REVISION,
    )
    started = time.perf_counter_ns()
    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()
        raw_line = next(line for line in response.iter_lines() if line.strip())
    payload = json.loads(raw_line)
    if not isinstance(payload, dict):
        raise RuntimeError("first public LongMemEval-V2 trajectory is not an object")
    return raw_line, payload, _elapsed_ms(started)


def _repository_state(repository_root: Path) -> dict[str, object]:
    commit = _git(repository_root, "rev-parse", "HEAD")
    status = _git(repository_root, "status", "--porcelain")
    return {
        "commit": commit,
        "dirty": bool(status),
        "status_lines": len(status.splitlines()),
    }


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


def _elapsed_ms(started_ns: int) -> float:
    return round((time.perf_counter_ns() - started_ns) / 1_000_000, 6)


if __name__ == "__main__":
    raise SystemExit(main())
