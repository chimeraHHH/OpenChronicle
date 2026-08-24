from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from openchronicle.evaluation import memops50
from openchronicle.evaluation import memops50_retrieval as retrieval
from openchronicle.evaluation import memops50_selector_dev_v2 as selector_dev

ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT = ROOT / "benchmarks" / "memops50-lifecycle-v1" / "json"
HELDOUT = ROOT / "benchmarks" / "memops50-heldout-v1" / "json"
VALIDATION = ROOT / "benchmarks" / "memops50-validation-v1" / "json"
SELECTOR_DEV = ROOT / "benchmarks" / "memops50-selector-dev-v2" / "json"


def _source_ids(path: Path) -> set[str]:
    return {item["source_file"] for item in json.loads(path.read_bytes())}


def test_selector_dev_v2_is_source_disjoint_balanced_and_frozen() -> None:
    selection = json.loads((SELECTOR_DEV / "selected_pairs.json").read_bytes())
    exclusions = json.loads((SELECTOR_DEV / "source_exclusions.json").read_bytes())
    manifest = json.loads((SELECTOR_DEV / "manifest.json").read_bytes())

    earlier = [
        _source_ids(DEVELOPMENT / "selected_pairs.json"),
        _source_ids(HELDOUT / "selected_pairs.json"),
        _source_ids(VALIDATION / "selected_pairs.json"),
    ]
    assert all(len(source_ids) == 50 for source_ids in earlier)
    assert len(set().union(*earlier)) == 150
    selected_sources = {pair["source_file"] for pair in selection}
    assert len(selection) == len(selected_sources) == 50
    assert not (selected_sources & set().union(*earlier))
    assert len(selected_sources | set().union(*earlier)) == 200

    assert memops50.sha256_bytes(memops50.canonical_json(selection)) == (
        selector_dev.PAIR_LIST_SHA256
    )
    assert hashlib.sha256(
        "".join(
            f"{pair['source_file']}\t{pair['question_pair_id']}\n"
            for pair in selection
        ).encode()
    ).hexdigest() == selector_dev.PAIR_LIST_TSV_SHA256
    assert memops50.sha256_bytes(memops50.canonical_json(exclusions)) == (
        selector_dev.SOURCE_EXCLUSIONS_SHA256
    )
    assert exclusions["union_source_ids_sha256"] == (
        selector_dev.EXCLUSION_UNION_SHA256
    )
    for raw, expected_sources in zip(exclusions["tiers"], earlier, strict=True):
        assert set(raw["source_files"]) == expected_sources
        assert selector_dev._source_ids_sha256(expected_sources) == (
            raw["source_ids_sha256"]
        )

    items = manifest["items"]
    assert [
        {"source_file": item["source_file"], "question_pair_id": item["question_pair_id"]}
        for item in items
    ] == selection
    assert [item["rank_hash"] for item in items] == sorted(
        item["rank_hash"] for item in items
    )
    assert sum(int(item["rank_hash"], 16) for item in items) == int(
        selector_dev.SOLVER_OBJECTIVE_INTEGER
    )
    assert int(selector_dev.SECOND_BEST_OBJECTIVE_INTEGER) > int(
        selector_dev.SOLVER_OBJECTIVE_INTEGER
    )
    assert manifest["selection"]["unique_optimum"] is True
    assert manifest["selection"]["candidate_pair_count_after_source_exclusion"] == 1256
    assert manifest["selection"]["available_source_file_count_after_exclusion"] == 253
    assert manifest["retrieval_expected"] == selector_dev.RETRIEVAL_EXPECTED
    assert manifest["development_scope"] == {
        "selector_only": True,
        "answer_prompt_tuning": False,
        "faithfulness_prompt_tuning": False,
        "correctness_prompt_tuning": False,
        "validation_content_read": False,
        "model_calls_during_selection": 0,
    }

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


def test_candidate_inventory_skips_exclusions_before_reading(
    tmp_path: Path,
    monkeypatch,
) -> None:
    blocked = tmp_path / "A01_update.json"
    blocked.write_bytes(b"not-json-and-must-not-be-read")
    allowed = tmp_path / "A02_update.json"
    allowed.write_text(
        json.dumps(
            {
                "answer": [
                    {"question_pair_id": "p1_operation_trace"},
                    {"question_pair_id": "p1_operation_trace"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(selector_dev, "AVAILABLE_SOURCE_FILE_COUNT", 1)
    monkeypatch.setattr(selector_dev, "CANDIDATE_PAIR_COUNT", 1)

    assert selector_dev._candidate_inventory(
        tmp_path,
        exclusions={blocked.name},
    ) == [
        {
            "source_file": allowed.name,
            "question_pair_id": "p1_operation_trace",
        }
    ]


def test_retrieval_routes_selector_dev_v2_through_source_only_exclusion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: dict[str, Path] = {}

    def fake_verify_manifest(**kwargs) -> None:
        called.update(kwargs)

    monkeypatch.setattr(selector_dev, "verify_manifest", fake_verify_manifest)
    manifest_path = tmp_path / "manifest.json"
    exclusion_path = tmp_path / "source_exclusions.json"
    manifest = {
        "tier_id": selector_dev.TIER_ID,
        "retrieval_expected": selector_dev.RETRIEVAL_EXPECTED,
    }

    tier_id, expected = retrieval._verify_tier_manifest(
        manifest=manifest,
        manifest_path=manifest_path,
        memops_root=tmp_path,
        exclusion_paths=(exclusion_path,),
    )

    assert tier_id == selector_dev.TIER_ID
    assert expected == selector_dev.RETRIEVAL_EXPECTED
    assert called == {
        "manifest_path": manifest_path,
        "source_exclusions_path": exclusion_path,
        "memops_root": tmp_path,
    }
