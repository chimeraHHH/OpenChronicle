"""Pinned MemOps-50 lifecycle tier manifest construction and verification."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

UPSTREAM_REPOSITORY = "https://github.com/MemTensor/MemOps.git"
UPSTREAM_COMMIT = "312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35"
STAGE2_ROOT = "generated_result/2-evidence_conversation"
STAGE4_ROOT = "generated_result/4-inject_evidence_with_distractors"
SELECTION_SEED = "memops50-v1"
PAIR_LIST_SHA256 = "f1a2bd273122b990508504650082d8c5909d2805272ee28e10eeb53589f5b9c0"
PAIR_LIST_TSV_SHA256 = (
    "688043c68a1afd051cb0e05bafb1f3ac281c5aed26bb5e2373197c251d21710e"
)
LICENSE_SHA256 = "44321bf0b4b4e0fa2ea67a619db296eab0e38889e70580ef2cfc255fbf925e82"
RUNNER_SHA256 = "557e8e64b15eaca18464201d922f0db35d2fcb723daa9f0c60395eebc85bac62"
JUDGE_SHA256 = "17c24362c174239eb7ef5e543bc651afed60ae445a4becd49acf24f6d3c646f0"
REQUIREMENTS_SHA256 = "fde18d35921adc37775be2f13e1f246a35cb669690f34481939a808c91e6afe6"
STAGE2_INDEX_SHA256 = "74b79e3fdf3795aa94d2e4cf7ccddc57b12b167d2c704447d7182947bbac2a74"
STAGE4_INDEX_SHA256 = "a94e0eeb875f2208cd34e2cf45ed1defa3ea7614d3e407a4f9dfb2c2b1fd7aeb"
SELECTED_SOURCE_INDEX_SHA256 = (
    "18dede4f089c3f632ac7be7d8f96d309ad999ee8039fa9245a16d45738da7c43"
)

_FILE_RE = re.compile(
    r"^[A-F]\d{2}_(remember|forget|update|reflect|trajectory_ops)\.json$"
)
_OPERATIONS = ("Remember", "Forget", "Update", "Reflect", "TrajectoryOps")
_OPERATION_QUOTA = {operation: 10 for operation in _OPERATIONS}
_DIFFICULTY_QUOTA = {"medium": 25, "hard": 25}
_TOPIC_QUOTA = {"A": 9, "B": 9, "C": 8, "D": 8, "E": 8, "F": 8}
_EVALUATION_QUOTA = {
    "OperationTrace": 10,
    "TargetBinding": 10,
    "StateTransition": 8,
    "CandidateDisambiguation": 9,
    "OperationApplication": 11,
    "StateTrajectory": 2,
}


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_manifest(*, selection_path: Path, memops_root: Path) -> dict[str, Any]:
    """Build the frozen manifest from one exact upstream checkout."""
    root = memops_root.resolve()
    _require_checkout(root)
    selection = _read_selection(selection_path)
    if sha256_bytes(canonical_json(selection)) != PAIR_LIST_SHA256:
        raise ValueError("selected pair list digest does not match the frozen tier")
    selection_tsv = "".join(
        f"{pair['source_file']}\t{pair['question_pair_id']}\n" for pair in selection
    ).encode()
    if sha256_bytes(selection_tsv) != PAIR_LIST_TSV_SHA256:
        raise ValueError("selected pair TSV digest does not match the frozen tier")

    stage2_root = root / STAGE2_ROOT
    stage4_root = root / STAGE4_ROOT
    stage2_index = _directory_index(stage2_root)
    stage4_index = _directory_index(stage4_root)
    _require_index(stage2_index, count=403, digest=STAGE2_INDEX_SHA256, label="stage 2")
    _require_index(stage4_index, count=403, digest=STAGE4_INDEX_SHA256, label="stage 4")

    items = [
        _build_item(
            source_file=pair["source_file"],
            question_pair_id=pair["question_pair_id"],
            stage2_root=stage2_root,
            stage4_root=stage4_root,
        )
        for pair in selection
    ]
    selected_index = sorted(
        [item["source_file"], item["stage2_sha256"], item["stage4_sha256"]]
        for item in items
    )
    if sha256_bytes(canonical_json(selected_index)) != SELECTED_SOURCE_INDEX_SHA256:
        raise ValueError("selected source index digest does not match the frozen tier")
    _validate_item_quotas(items)

    return {
        "schema_version": 1,
        "tier_id": "memops50-adjacent-longitudinal-v1",
        "upstream": {
            "repository": UPSTREAM_REPOSITORY,
            "commit": UPSTREAM_COMMIT,
            "license_spdx": "MIT",
            "license_sha256": LICENSE_SHA256,
            "runner_path": "5-test_operation_metrics.py",
            "runner_sha256": RUNNER_SHA256,
            "judge_path": "5.5-evaluate_operation_metrics.py",
            "judge_sha256": JUDGE_SHA256,
            "requirements_sha256": REQUIREMENTS_SHA256,
            "stage2_root": STAGE2_ROOT,
            "stage2_file_count": 403,
            "stage2_index_sha256": STAGE2_INDEX_SHA256,
            "stage4_root": STAGE4_ROOT,
            "stage4_file_count": 403,
            "stage4_index_sha256": STAGE4_INDEX_SHA256,
            "selected_source_index_sha256": SELECTED_SOURCE_INDEX_SHA256,
        },
        "pair_identity": ["source_file", "question_pair_id"],
        "canonicalization": {
            "id": "python-json-sorted-compact-utf8-v1",
            "question_spec_removed_fields": ["evaluation_setting"],
        },
        "selection": {
            "seed": SELECTION_SEED,
            "algorithm": "deterministic-dinic-v1",
            "pair_list_sha256": PAIR_LIST_SHA256,
            "pair_list_tsv_sha256": PAIR_LIST_TSV_SHA256,
            "max_pairs_per_source_file": 1,
            "operation_quotas": _OPERATION_QUOTA,
            "difficulty_quotas": _DIFFICULTY_QUOTA,
            "topic_family_quotas": _TOPIC_QUOTA,
            "evaluation_type_quotas": _EVALUATION_QUOTA,
        },
        "expected": {
            "logical_pairs": 50,
            "rows_per_pair": 2,
            "rows_per_method": 100,
            "distinct_source_files": 50,
            "settings": ["adjacent_operation", "longitudinal_operation"],
        },
        "items": items,
    }


def verify_manifest(*, manifest_path: Path, memops_root: Path) -> dict[str, int]:
    """Fail closed unless a manifest and checkout reproduce the frozen tier."""
    manifest = _load_json(manifest_path.read_bytes(), label="MemOps-50 manifest")
    if not isinstance(manifest, dict):
        raise ValueError("MemOps-50 manifest must be an object")
    expected = build_manifest(
        selection_path=manifest_path.with_name("selected_pairs.json"),
        memops_root=memops_root,
    )
    if manifest != expected:
        raise ValueError("MemOps-50 manifest differs from the reconstructed manifest")
    return {"logical_pairs": len(expected["items"]), "rows_per_method": 100}


def build_decision_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Project the frozen tier into the existing inert decision-eval schema."""
    items = manifest.get("items")
    if not isinstance(items, list) or len(items) != 50:
        raise ValueError("MemOps-50 manifest items are invalid")
    samples = [
        {
            "id": item["source_file"].removesuffix(".json"),
            "file": item["source_file"],
            "sha256": item["stage2_sha256"],
        }
        for item in items
        if isinstance(item, dict)
    ]
    if len(samples) != 50 or len({sample["id"] for sample in samples}) != 50:
        raise ValueError("MemOps-50 decision samples are invalid")
    return {
        "schema_version": 1,
        "dataset_id": "MemOps-50-Official-Adjacent-Decisions-v1",
        "split": "official-adjacent-balanced-50",
        "repository": UPSTREAM_REPOSITORY,
        "commit": UPSTREAM_COMMIT,
        "data_root": STAGE2_ROOT,
        "samples": samples,
    }


def _build_item(
    *,
    source_file: str,
    question_pair_id: str,
    stage2_root: Path,
    stage4_root: Path,
    selection_seed: str = SELECTION_SEED,
) -> dict[str, str]:
    match = _FILE_RE.fullmatch(source_file)
    if match is None:
        raise ValueError(f"invalid selected source file: {source_file}")
    stage2_path = stage2_root / source_file
    stage4_path = stage4_root / source_file
    stage2_bytes = stage2_path.read_bytes()
    stage4_bytes = stage4_path.read_bytes()
    stage2 = _load_json(stage2_bytes, label=source_file)
    stage4 = _load_json(stage4_bytes, label=source_file)
    if not isinstance(stage2, dict) or not isinstance(stage4, dict):
        raise ValueError(f"MemOps source must be an object: {source_file}")
    operation_type = _required_text(stage2.get("operation_type"), "operation_type")
    if operation_type not in _OPERATIONS or stage4.get("operation_type") != operation_type:
        raise ValueError(f"operation type mismatch for {source_file}")

    stage2_answers = _pair_answers(stage2, question_pair_id, source_file)
    stage4_answers = _pair_answers(stage4, question_pair_id, source_file)
    normalized = [_without_setting(answer) for answer in stage2_answers]
    if normalized[0] != normalized[1]:
        raise ValueError(f"adjacent/longitudinal question drift in stage 2: {source_file}")
    if normalized != [_without_setting(answer) for answer in stage4_answers]:
        raise ValueError(f"stage 2/stage 4 question drift: {source_file}")
    spec = normalized[0]
    evaluation_type = _required_text(spec.get("evaluation_type"), "evaluation_type")
    difficulty = _required_text(spec.get("difficulty"), "difficulty")
    subprobe = next(
        (
            value
            for key in (
                "state_transition_probe_type",
                "application_probe_type",
                "trajectory_granularity",
            )
            if isinstance((value := spec.get(key)), str) and value
        ),
        "",
    )
    rank_hash = sha256_bytes(
        f"{selection_seed}\0{source_file}\0{question_pair_id}".encode()
    )
    return {
        "source_file": source_file,
        "question_pair_id": question_pair_id,
        "operation_type": operation_type,
        "evaluation_type": evaluation_type,
        "difficulty": difficulty,
        "subprobe": subprobe,
        "topic_family": source_file[0],
        "rank_hash": rank_hash,
        "question_spec_sha256": sha256_bytes(canonical_json(spec)),
        "stage2_sha256": sha256_bytes(stage2_bytes),
        "stage4_sha256": sha256_bytes(stage4_bytes),
    }


def _pair_answers(
    payload: object,
    question_pair_id: str,
    source_file: str,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("answer"), list):
        raise ValueError(f"answer array is missing: {source_file}")
    answers = [
        answer
        for answer in payload["answer"]
        if isinstance(answer, dict) and answer.get("question_pair_id") == question_pair_id
    ]
    if len(answers) != 2 or [answer.get("evaluation_setting") for answer in answers] != [
        "adjacent_operation",
        "longitudinal_operation",
    ]:
        raise ValueError(f"selected pair is incomplete or unordered: {source_file}")
    return answers


def _without_setting(answer: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in answer.items() if key != "evaluation_setting"}


def _read_selection(path: Path) -> list[dict[str, str]]:
    payload = _load_json(path.read_bytes(), label="MemOps-50 selection")
    if not isinstance(payload, list) or len(payload) != 50:
        raise ValueError("MemOps-50 selection must contain exactly 50 pairs")
    selection: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"source_file", "question_pair_id"}:
            raise ValueError("invalid MemOps-50 selection item")
        selection.append(
            {
                "source_file": _required_text(item["source_file"], "source_file"),
                "question_pair_id": _required_text(
                    item["question_pair_id"], "question_pair_id"
                ),
            }
        )
    if len({(item["source_file"], item["question_pair_id"]) for item in selection}) != 50:
        raise ValueError("MemOps-50 pair identities must be unique")
    return selection


def _directory_index(root: Path) -> list[list[str]]:
    return [
        [path.name, sha256_bytes(path.read_bytes())]
        for path in sorted(root.glob("*.json"))
        if path.is_file()
    ]


def _require_index(
    index: list[list[str]],
    *,
    count: int,
    digest: str,
    label: str,
) -> None:
    if len(index) != count or sha256_bytes(canonical_json(index)) != digest:
        raise ValueError(f"{label} source index does not match the frozen tier")


def _validate_item_quotas(items: list[dict[str, str]]) -> None:
    if len(items) != 50 or len({item["source_file"] for item in items}) != 50:
        raise ValueError("MemOps-50 must use 50 distinct source files")
    if Counter(item["operation_type"] for item in items) != _OPERATION_QUOTA:
        raise ValueError("MemOps-50 operation quotas do not match")
    if Counter(item["difficulty"] for item in items) != _DIFFICULTY_QUOTA:
        raise ValueError("MemOps-50 difficulty quotas do not match")
    if Counter(item["topic_family"] for item in items) != _TOPIC_QUOTA:
        raise ValueError("MemOps-50 topic-family quotas do not match")
    if Counter(item["evaluation_type"] for item in items) != _EVALUATION_QUOTA:
        raise ValueError("MemOps-50 evaluation-type quotas do not match")


def _require_checkout(root: Path) -> None:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if commit != UPSTREAM_COMMIT:
        raise ValueError("MemOps checkout does not match the pinned commit")
    if sha256_bytes((root / "LICENSE").read_bytes()) != LICENSE_SHA256:
        raise ValueError("MemOps license digest does not match the pinned commit")
    for relative_path, expected_digest in (
        ("5-test_operation_metrics.py", RUNNER_SHA256),
        ("5.5-evaluate_operation_metrics.py", JUDGE_SHA256),
        ("requirements.txt", REQUIREMENTS_SHA256),
    ):
        if sha256_bytes((root / relative_path).read_bytes()) != expected_digest:
            raise ValueError(f"MemOps source digest does not match: {relative_path}")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError(f"invalid {field}")
    return value


def _load_json(raw: bytes, *, label: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in {label}: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value in {label}: {value}")

    try:
        return json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {label}") from exc
