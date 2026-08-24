"""Source-disjoint MemOps-50 development tier for local selector work."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from . import memops50

TIER_ID = "memops50-selector-dev-v2-adjacent-longitudinal-v1"
SELECTION_SEED = "memops50-selector-dev-v2"
PAIR_LIST_SHA256 = "25f1972376448bac77e05b8e23b432dd64ca63a15609b9dd0d209aa9f7f3809c"
PAIR_LIST_TSV_SHA256 = "93ea00ec5af99e0d0711126f1e5bc9a7a9afd3e1b64a15549e4a0f41a1fec073"
SOURCE_EXCLUSIONS_SHA256 = (
    "f1b35d3c52f568280923b65e5eb1926b21d0eebd2cb598c26fb810613fce6a0b"
)
EXCLUSION_UNION_SHA256 = (
    "d920f4b40d22eb7cbff86af6019099475789c732079793ac644159535701fd1c"
)
SELECTED_SOURCE_IDS_SHA256 = (
    "f31211ef1bc6b4ae59df9420b6929763898701cb72e0af76e4352d178dfe2ef3"
)
SELECTED_SOURCE_INDEX_SHA256 = (
    "ed7576561f01a3dc5ee50be9debb4672b7657a67762bf285c7087d2caf3375b3"
)
SOLVER_OBJECTIVE_INTEGER = (
    "195936203683711679311810569030144406157310332749992783216497488840492521689260"
)
SECOND_BEST_OBJECTIVE_INTEGER = (
    "196406442688359200038718775623938066558473325142265044846795081734960173666635"
)
CANDIDATE_PAIR_COUNT = 1256
AVAILABLE_SOURCE_FILE_COUNT = 253

_EXCLUSION_TIERS = (
    (
        "memops50-adjacent-longitudinal-v1",
        "5b343a2322a2af42f5ba102d94a0283bcd2edf8fe52d6a28c5cf645d2ad4026f",
    ),
    (
        "memops50-heldout-adjacent-longitudinal-v1",
        "836ea3b525fe3210b188263f063c17be49bf561006b540a9e0bbf64ac044887d",
    ),
    (
        "memops50-validation-adjacent-longitudinal-v1",
        "24fbfd50e43f629bb3326f138cffaa98cd41e1fd39773ffb308f684ec8edfea8",
    ),
)

RETRIEVAL_EXPECTED = {
    "adjacent_segment_count": 150,
    "longitudinal_segment_count": 2500,
    "longitudinal_evidence_carrier_count": 150,
    "longitudinal_distractor_segment_count": 285,
    "gold_provenance_item_count": 169,
    "unique_gold_turn_count": 167,
    "gold_segment_count": 109,
}


def build_manifest(
    *,
    selection_path: Path,
    source_exclusions_path: Path,
    memops_root: Path,
) -> dict[str, Any]:
    """Reconstruct the frozen tier without opening any excluded source file."""
    root = memops_root.resolve()
    memops50._require_checkout(root)
    selection = memops50._read_selection(selection_path)
    if memops50.sha256_bytes(memops50.canonical_json(selection)) != PAIR_LIST_SHA256:
        raise ValueError("selector-dev-v2 selected pair digest changed")
    tsv = "".join(
        f"{pair['source_file']}\t{pair['question_pair_id']}\n" for pair in selection
    ).encode()
    if memops50.sha256_bytes(tsv) != PAIR_LIST_TSV_SHA256:
        raise ValueError("selector-dev-v2 selected pair order changed")

    exclusions, exclusion_metadata = _read_source_exclusions(source_exclusions_path)
    selected_sources = {pair["source_file"] for pair in selection}
    if selected_sources & exclusions:
        raise ValueError("selector-dev-v2 overlaps a frozen earlier source file")
    if len(selected_sources) != 50:
        raise ValueError("selector-dev-v2 must use 50 distinct source files")
    if _source_ids_sha256(selected_sources) != SELECTED_SOURCE_IDS_SHA256:
        raise ValueError("selector-dev-v2 selected source IDs changed")

    stage2_root = root / memops50.STAGE2_ROOT
    stage4_root = root / memops50.STAGE4_ROOT
    stage2_names = {path.name for path in stage2_root.glob("*.json") if path.is_file()}
    stage4_names = {path.name for path in stage4_root.glob("*.json") if path.is_file()}
    if len(stage2_names) != 403 or stage2_names != stage4_names:
        raise ValueError("MemOps Stage 2/Stage 4 source filenames changed")
    if not exclusions <= stage2_names or not selected_sources <= stage2_names:
        raise ValueError("selector-dev-v2 source IDs do not resolve")

    candidates = _candidate_inventory(stage2_root, exclusions=exclusions)
    identities = {
        (candidate["source_file"], candidate["question_pair_id"])
        for candidate in candidates
    }
    if any(
        (pair["source_file"], pair["question_pair_id"]) not in identities
        for pair in selection
    ):
        raise ValueError("selector-dev-v2 selection is not in the candidate inventory")

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
    if [item["rank_hash"] for item in items] != sorted(
        item["rank_hash"] for item in items
    ):
        raise ValueError("selector-dev-v2 pair order is not rank-hash order")
    objective = sum(int(item["rank_hash"], 16) for item in items)
    if str(objective) != SOLVER_OBJECTIVE_INTEGER:
        raise ValueError("selector-dev-v2 solver objective changed")
    selected_index = sorted(
        [item["source_file"], item["stage2_sha256"], item["stage4_sha256"]]
        for item in items
    )
    if (
        memops50.sha256_bytes(memops50.canonical_json(selected_index))
        != SELECTED_SOURCE_INDEX_SHA256
    ):
        raise ValueError("selector-dev-v2 selected source bytes changed")
    memops50._validate_item_quotas(items)

    return {
        "schema_version": 1,
        "tier_id": TIER_ID,
        "upstream": {
            "repository": memops50.UPSTREAM_REPOSITORY,
            "commit": memops50.UPSTREAM_COMMIT,
            "license_spdx": "MIT",
            "license_sha256": memops50.LICENSE_SHA256,
            "stage2_root": memops50.STAGE2_ROOT,
            "stage2_file_count": 403,
            "stage4_root": memops50.STAGE4_ROOT,
            "stage4_file_count": 403,
            "selected_source_index_sha256": SELECTED_SOURCE_INDEX_SHA256,
        },
        "pair_identity": ["source_file", "question_pair_id"],
        "canonicalization": {
            "id": "python-json-sorted-compact-utf8-v1",
            "question_spec_removed_fields": ["evaluation_setting"],
        },
        "exclusions": exclusion_metadata,
        "selection": {
            "seed": SELECTION_SEED,
            "algorithm": "scipy-1.17.1-highs-1.12.0-milp-exact-quota-v1",
            "candidate_construction": (
                "first_unique_question_pair_id_per_stage2_answer_after_source_exclusion"
            ),
            "candidate_pair_count_after_source_exclusion": CANDIDATE_PAIR_COUNT,
            "available_source_file_count_after_exclusion": AVAILABLE_SOURCE_FILE_COUNT,
            "pair_list_sha256": PAIR_LIST_SHA256,
            "pair_list_tsv_sha256": PAIR_LIST_TSV_SHA256,
            "selected_source_ids_sha256": SELECTED_SOURCE_IDS_SHA256,
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
        "development_scope": {
            "selector_only": True,
            "answer_prompt_tuning": False,
            "faithfulness_prompt_tuning": False,
            "correctness_prompt_tuning": False,
            "validation_content_read": False,
            "model_calls_during_selection": 0,
        },
        "items": items,
    }


def verify_manifest(
    *,
    manifest_path: Path,
    source_exclusions_path: Path,
    memops_root: Path,
) -> dict[str, int]:
    manifest = memops50._load_json(
        manifest_path.read_bytes(),
        label="selector-dev-v2 MemOps-50 manifest",
    )
    if not isinstance(manifest, dict):
        raise ValueError("selector-dev-v2 manifest must be an object")
    expected = build_manifest(
        selection_path=manifest_path.with_name("selected_pairs.json"),
        source_exclusions_path=source_exclusions_path,
        memops_root=memops_root,
    )
    if manifest != expected:
        raise ValueError("selector-dev-v2 manifest differs from reconstruction")
    return {"logical_pairs": len(expected["items"]), "rows_per_method": 100}


def _candidate_inventory(
    stage2_root: Path,
    *,
    exclusions: set[str],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    sources: set[str] = set()
    for path in sorted(stage2_root.glob("*.json")):
        if path.name in exclusions:
            continue
        payload = memops50._load_json(path.read_bytes(), label=path.name)
        if not isinstance(payload, dict) or not isinstance(payload.get("answer"), list):
            raise ValueError(f"MemOps candidate source is invalid: {path.name}")
        sources.add(path.name)
        seen: set[str] = set()
        for answer in payload["answer"]:
            if not isinstance(answer, dict):
                continue
            question_pair_id = answer.get("question_pair_id")
            if not isinstance(question_pair_id, str) or not question_pair_id:
                continue
            if question_pair_id in seen:
                continue
            seen.add(question_pair_id)
            result.append(
                {
                    "source_file": path.name,
                    "question_pair_id": question_pair_id,
                }
            )
    if len(sources) != AVAILABLE_SOURCE_FILE_COUNT or len(result) != CANDIDATE_PAIR_COUNT:
        raise ValueError("selector-dev-v2 candidate inventory changed")
    return result


def _read_source_exclusions(path: Path) -> tuple[set[str], dict[str, Any]]:
    payload = memops50._load_json(path.read_bytes(), label="source-only exclusions")
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "canonicalization",
        "tiers",
        "union_source_file_count",
        "union_source_ids_sha256",
    }:
        raise ValueError("selector-dev-v2 source exclusions are invalid")
    if (
        payload["schema_version"] != 1
        or payload["canonicalization"] != "sorted-source-file-list-json-v1"
        or memops50.sha256_bytes(memops50.canonical_json(payload))
        != SOURCE_EXCLUSIONS_SHA256
    ):
        raise ValueError("selector-dev-v2 source exclusion contract changed")
    tiers = payload["tiers"]
    if not isinstance(tiers, list) or len(tiers) != len(_EXCLUSION_TIERS):
        raise ValueError("selector-dev-v2 exclusion tiers are invalid")
    union: set[str] = set()
    projected: list[dict[str, Any]] = []
    for raw, (expected_tier, expected_digest) in zip(
        tiers, _EXCLUSION_TIERS, strict=True
    ):
        if not isinstance(raw, dict) or set(raw) != {
            "tier_id",
            "source_file_count",
            "source_ids_sha256",
            "source_files",
        }:
            raise ValueError("selector-dev-v2 exclusion tier is invalid")
        source_files = raw["source_files"]
        if (
            raw["tier_id"] != expected_tier
            or raw["source_file_count"] != 50
            or raw["source_ids_sha256"] != expected_digest
            or not isinstance(source_files, list)
            or source_files != sorted(source_files)
            or len(source_files) != 50
            or len(set(source_files)) != 50
            or _source_ids_sha256(set(source_files)) != expected_digest
        ):
            raise ValueError("selector-dev-v2 exclusion source IDs changed")
        if union & set(source_files):
            raise ValueError("selector-dev-v2 exclusion tiers overlap")
        union.update(source_files)
        projected.append(
            {
                "tier_id": expected_tier,
                "source_file_count": 50,
                "source_ids_sha256": expected_digest,
            }
        )
    if (
        payload["union_source_file_count"] != 150
        or payload["union_source_ids_sha256"] != EXCLUSION_UNION_SHA256
        or len(union) != 150
        or _source_ids_sha256(union) != EXCLUSION_UNION_SHA256
    ):
        raise ValueError("selector-dev-v2 exclusion union changed")
    return union, {
        "tiers": projected,
        "union_source_file_count": 150,
        "union_source_ids_sha256": EXCLUSION_UNION_SHA256,
        "selected_source_file_overlap": 0,
        "content_policy": "source_ids_only",
    }


def _source_ids_sha256(source_files: set[str]) -> str:
    return hashlib.sha256(memops50.canonical_json(sorted(source_files))).hexdigest()
