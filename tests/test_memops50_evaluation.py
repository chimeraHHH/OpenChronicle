from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from openchronicle.evaluation import memops50

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "memops50-lifecycle-v1" / "json"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_frozen_memops50_manifest_is_self_consistent() -> None:
    selection = json.loads((BENCHMARK / "selected_pairs.json").read_bytes())
    manifest = json.loads((BENCHMARK / "manifest.json").read_bytes())

    assert len(selection) == 50
    assert memops50.sha256_bytes(memops50.canonical_json(selection)) == (
        memops50.PAIR_LIST_SHA256
    )
    assert hashlib.sha256(
        "".join(
            f"{pair['source_file']}\t{pair['question_pair_id']}\n"
            for pair in selection
        ).encode()
    ).hexdigest() == memops50.PAIR_LIST_TSV_SHA256

    items = manifest["items"]
    assert [
        {"source_file": item["source_file"], "question_pair_id": item["question_pair_id"]}
        for item in items
    ] == selection
    assert len({item["source_file"] for item in items}) == 50
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

    assert manifest["upstream"]["commit"] == memops50.UPSTREAM_COMMIT
    assert manifest["expected"] == {
        "logical_pairs": 50,
        "rows_per_pair": 2,
        "rows_per_method": 100,
        "distinct_source_files": 50,
        "settings": ["adjacent_operation", "longitudinal_operation"],
    }
    expected_item_keys = {
        "source_file",
        "question_pair_id",
        "operation_type",
        "evaluation_type",
        "difficulty",
        "subprobe",
        "topic_family",
        "rank_hash",
        "question_spec_sha256",
        "stage2_sha256",
        "stage4_sha256",
    }
    for item in items:
        assert set(item) == expected_item_keys
        assert item["rank_hash"] == memops50.sha256_bytes(
            (
                f"{memops50.SELECTION_SEED}\0{item['source_file']}\0"
                f"{item['question_pair_id']}"
            ).encode()
        )
        assert all(
            HEX64.fullmatch(item[key])
            for key in (
                "rank_hash",
                "question_spec_sha256",
                "stage2_sha256",
                "stage4_sha256",
            )
        )

    decision_manifest = json.loads((BENCHMARK / "decision_manifest.json").read_bytes())
    assert decision_manifest == memops50.build_decision_manifest(manifest)
    assert len(decision_manifest["samples"]) == 50


def test_memops50_json_loader_rejects_ambiguous_json() -> None:
    with pytest.raises(ValueError, match="duplicate JSON key"):
        memops50._load_json(b'{"same":1,"same":1}', label="fixture")
    with pytest.raises(ValueError, match="non-finite JSON value"):
        memops50._load_json(b'{"value":NaN}', label="fixture")
