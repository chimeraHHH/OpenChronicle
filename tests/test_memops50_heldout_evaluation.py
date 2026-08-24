from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from openchronicle.evaluation import memops50, memops50_heldout

ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT = ROOT / "benchmarks" / "memops50-lifecycle-v1" / "json"
HELDOUT = ROOT / "benchmarks" / "memops50-heldout-v1" / "json"


def test_frozen_memops50_heldout_manifest_is_self_consistent() -> None:
    development = json.loads((DEVELOPMENT / "selected_pairs.json").read_bytes())
    selection = json.loads((HELDOUT / "selected_pairs.json").read_bytes())
    manifest = json.loads((HELDOUT / "manifest.json").read_bytes())

    assert len(selection) == 50
    assert not (
        {pair["source_file"] for pair in development}
        & {pair["source_file"] for pair in selection}
    )
    assert memops50.sha256_bytes(memops50.canonical_json(selection)) == (
        memops50_heldout.PAIR_LIST_SHA256
    )
    assert hashlib.sha256(
        "".join(
            f"{pair['source_file']}\t{pair['question_pair_id']}\n"
            for pair in selection
        ).encode()
    ).hexdigest() == memops50_heldout.PAIR_LIST_TSV_SHA256

    items = manifest["items"]
    assert [
        {"source_file": item["source_file"], "question_pair_id": item["question_pair_id"]}
        for item in items
    ] == selection
    assert Counter(item["operation_type"] for item in items) == {
        "Remember": 10,
        "Forget": 10,
        "Update": 10,
        "Reflect": 10,
        "TrajectoryOps": 10,
    }
    assert Counter(item["difficulty"] for item in items) == {
        "medium": 25,
        "hard": 25,
    }
    assert Counter(item["topic_family"] for item in items) == {
        "A": 9,
        "B": 9,
        "C": 8,
        "D": 8,
        "E": 8,
        "F": 8,
    }
    assert Counter(item["evaluation_type"] for item in items) == {
        "OperationTrace": 10,
        "TargetBinding": 10,
        "StateTransition": 8,
        "CandidateDisambiguation": 9,
        "OperationApplication": 11,
        "StateTrajectory": 2,
    }
    assert manifest["selection"]["unique_optimum"] is True
    assert manifest["retrieval_expected"] == memops50_heldout.RETRIEVAL_EXPECTED
    for item in items:
        assert item["rank_hash"] == memops50.sha256_bytes(
            (
                f"{memops50_heldout.SELECTION_SEED}\0{item['source_file']}\0"
                f"{item['question_pair_id']}"
            ).encode()
        )
