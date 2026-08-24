"""Frozen third MemOps-50 tier reserved for downstream validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import memops50, memops50_heldout

TIER_ID = "memops50-validation-adjacent-longitudinal-v1"
SELECTION_SEED = "memops50-validation-v1"
PAIR_LIST_SHA256 = "86c75e6d62bb0b0ac6ef65ef2fc4c4d9b15e43584cdd27ca95363b481ced1055"
PAIR_LIST_TSV_SHA256 = "353fe18bb08cbf2862e2b113c5fea528eb2a69305366bff273a061cde051fc54"
SELECTED_SOURCE_INDEX_SHA256 = (
    "efbddc432221e55b885dd84e2c93d366f9b06360a5f0cc14dcdf15590f24090c"
)
SOLVER_OBJECTIVE_INTEGER = (
    "153577712655654468940988933271219753749196996734106996023648751857298942354716"
)
SECOND_BEST_OBJECTIVE_INTEGER = (
    "153801539145562194631388225908469948031252623439201405237300917948679515980776"
)
RETRIEVAL_EXPECTED = {
    "adjacent_segment_count": 150,
    "longitudinal_segment_count": 2500,
    "longitudinal_evidence_carrier_count": 150,
    "longitudinal_distractor_segment_count": 276,
    "gold_provenance_item_count": 171,
    "unique_gold_turn_count": 169,
    "gold_segment_count": 106,
}


def build_manifest(
    *,
    selection_path: Path,
    development_exclusion_path: Path,
    heldout_exclusion_path: Path,
    memops_root: Path,
) -> dict[str, Any]:
    root = memops_root.resolve()
    memops50._require_checkout(root)
    selection = _read_selection(selection_path, label="validation selection")
    development = _read_selection(
        development_exclusion_path,
        label="development exclusion",
    )
    heldout = _read_selection(heldout_exclusion_path, label="held-out exclusion")
    _require_digest(development, memops50.PAIR_LIST_SHA256, label="development exclusion")
    _require_digest(heldout, memops50_heldout.PAIR_LIST_SHA256, label="held-out exclusion")
    _require_digest(selection, PAIR_LIST_SHA256, label="validation selection")
    tsv = "".join(
        f"{pair['source_file']}\t{pair['question_pair_id']}\n" for pair in selection
    ).encode()
    if memops50.sha256_bytes(tsv) != PAIR_LIST_TSV_SHA256:
        raise ValueError("validation selected pair TSV digest changed")

    development_sources = {pair["source_file"] for pair in development}
    heldout_sources = {pair["source_file"] for pair in heldout}
    selected_sources = {pair["source_file"] for pair in selection}
    if development_sources & heldout_sources:
        raise ValueError("the two exclusion tiers overlap")
    if selected_sources & (development_sources | heldout_sources):
        raise ValueError("validation tier overlaps an earlier source file")

    stage2_root = root / memops50.STAGE2_ROOT
    stage4_root = root / memops50.STAGE4_ROOT
    stage2_index = memops50._directory_index(stage2_root)
    stage4_index = memops50._directory_index(stage4_root)
    memops50._require_index(
        stage2_index,
        count=403,
        digest=memops50.STAGE2_INDEX_SHA256,
        label="stage 2",
    )
    memops50._require_index(
        stage4_index,
        count=403,
        digest=memops50.STAGE4_INDEX_SHA256,
        label="stage 4",
    )
    items = [
        memops50._build_item(
            source_file=pair["source_file"],
            question_pair_id=pair["question_pair_id"],
            stage2_root=stage2_root,
            stage4_root=stage4_root,
            selection_seed=SELECTION_SEED,
        )
        for pair in selection
    ]
    selected_index = sorted(
        [item["source_file"], item["stage2_sha256"], item["stage4_sha256"]]
        for item in items
    )
    if (
        memops50.sha256_bytes(memops50.canonical_json(selected_index))
        != SELECTED_SOURCE_INDEX_SHA256
    ):
        raise ValueError("validation selected source index changed")
    memops50._validate_item_quotas(items)
    return {
        "schema_version": 1,
        "tier_id": TIER_ID,
        "upstream": {
            "repository": memops50.UPSTREAM_REPOSITORY,
            "commit": memops50.UPSTREAM_COMMIT,
            "license_spdx": "MIT",
            "license_sha256": memops50.LICENSE_SHA256,
            "runner_path": "5-test_operation_metrics.py",
            "runner_sha256": memops50.RUNNER_SHA256,
            "judge_path": "5.5-evaluate_operation_metrics.py",
            "judge_sha256": memops50.JUDGE_SHA256,
            "requirements_sha256": memops50.REQUIREMENTS_SHA256,
            "stage2_root": memops50.STAGE2_ROOT,
            "stage2_file_count": 403,
            "stage2_index_sha256": memops50.STAGE2_INDEX_SHA256,
            "stage4_root": memops50.STAGE4_ROOT,
            "stage4_file_count": 403,
            "stage4_index_sha256": memops50.STAGE4_INDEX_SHA256,
            "selected_source_index_sha256": SELECTED_SOURCE_INDEX_SHA256,
        },
        "pair_identity": ["source_file", "question_pair_id"],
        "canonicalization": {
            "id": "python-json-sorted-compact-utf8-v1",
            "question_spec_removed_fields": ["evaluation_setting"],
        },
        "exclusions": {
            "tiers": [
                {
                    "tier_id": "memops50-adjacent-longitudinal-v1",
                    "pair_list_sha256": memops50.PAIR_LIST_SHA256,
                    "source_file_count": 50,
                    "source_file_overlap": 0,
                },
                {
                    "tier_id": memops50_heldout.TIER_ID,
                    "pair_list_sha256": memops50_heldout.PAIR_LIST_SHA256,
                    "source_file_count": 50,
                    "source_file_overlap": 0,
                },
            ],
            "union_source_file_count": 100,
            "selected_source_file_overlap": 0,
        },
        "selection": {
            "seed": SELECTION_SEED,
            "algorithm": "scipy-1.17.1-highs-1.12.0-milp-exact-quota-v1",
            "candidate_construction": (
                "first_unique_question_pair_id_per_stage2_answer_then_final_item_verification"
            ),
            "candidate_pair_count_after_source_exclusion": 1506,
            "available_source_file_count_after_exclusion": 303,
            "pair_list_sha256": PAIR_LIST_SHA256,
            "pair_list_tsv_sha256": PAIR_LIST_TSV_SHA256,
            "objective_integer": SOLVER_OBJECTIVE_INTEGER,
            "second_best_objective_integer": SECOND_BEST_OBJECTIVE_INTEGER,
            "unique_optimum": True,
            "max_pairs_per_source_file": 1,
            "operation_quotas": memops50._OPERATION_QUOTA,
            "difficulty_quotas": memops50._DIFFICULTY_QUOTA,
            "topic_family_quotas": memops50._TOPIC_QUOTA,
            "evaluation_type_quotas": memops50._EVALUATION_QUOTA,
        },
        "expected": {
            "logical_pairs": 50,
            "rows_per_pair": 2,
            "rows_per_method": 100,
            "distinct_source_files": 50,
            "settings": ["adjacent_operation", "longitudinal_operation"],
        },
        "retrieval_expected": RETRIEVAL_EXPECTED,
        "items": items,
    }


def verify_manifest(
    *,
    manifest_path: Path,
    development_exclusion_path: Path,
    heldout_exclusion_path: Path,
    memops_root: Path,
) -> dict[str, int]:
    manifest = memops50._load_json(
        manifest_path.read_bytes(),
        label="validation MemOps-50 manifest",
    )
    if not isinstance(manifest, dict):
        raise ValueError("validation MemOps-50 manifest must be an object")
    expected = build_manifest(
        selection_path=manifest_path.with_name("selected_pairs.json"),
        development_exclusion_path=development_exclusion_path,
        heldout_exclusion_path=heldout_exclusion_path,
        memops_root=memops_root,
    )
    if manifest != expected:
        raise ValueError("validation MemOps-50 manifest differs from reconstruction")
    return {"logical_pairs": len(expected["items"]), "rows_per_method": 100}


def _read_selection(path: Path, *, label: str) -> list[dict[str, str]]:
    payload = memops50._load_json(path.read_bytes(), label=label)
    if not isinstance(payload, list) or len(payload) != 50:
        raise ValueError(f"{label} must contain 50 pairs")
    selection: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"source_file", "question_pair_id"}:
            raise ValueError(f"{label} item is invalid")
        selection.append(
            {
                "source_file": memops50._required_text(item["source_file"], "source_file"),
                "question_pair_id": memops50._required_text(
                    item["question_pair_id"],
                    "question_pair_id",
                ),
            }
        )
    if len({item["source_file"] for item in selection}) != 50:
        raise ValueError(f"{label} source files must be distinct")
    return selection


def _require_digest(selection: list[dict[str, str]], digest: str, *, label: str) -> None:
    if memops50.sha256_bytes(memops50.canonical_json(selection)) != digest:
        raise ValueError(f"{label} digest changed")
