"""Evaluate production activity BM25 on frozen MemOps-50 long conversations."""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ..activity import store as activity_store
from ..store import fts
from . import memops50

Setting = Literal["adjacent_operation", "longitudinal_operation"]
_SETTINGS: tuple[Setting, ...] = (
    "adjacent_operation",
    "longitudinal_operation",
)


@dataclass(frozen=True, slots=True)
class EvidenceLocation:
    segment_index: int
    turn_index: int


@dataclass(frozen=True, slots=True)
class RetrievalUnit:
    segment_index: int
    content: str
    evidence: bool
    distractor: bool


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    source_file: str
    question_pair_id: str
    operation_type: str
    evaluation_type: str
    difficulty: str
    query: str
    adjacent_units: tuple[RetrievalUnit, ...]
    longitudinal_units: tuple[RetrievalUnit, ...]
    adjacent_gold: tuple[EvidenceLocation, ...]
    longitudinal_gold: tuple[EvidenceLocation, ...]
    gold_provenance_item_count: int


def load_cases(*, manifest_path: Path, memops_root: Path) -> tuple[RetrievalCase, ...]:
    """Load 50 digest-verified adjacent/longitudinal retrieval pairs."""
    memops50.verify_manifest(manifest_path=manifest_path, memops_root=memops_root)
    manifest = memops50._load_json(
        manifest_path.read_bytes(),
        label="MemOps-50 manifest",
    )
    if not isinstance(manifest, dict) or not isinstance(manifest.get("items"), list):
        raise ValueError("MemOps-50 manifest items are missing")
    root = memops_root.resolve()
    stage2_root = root / memops50.STAGE2_ROOT
    stage4_root = root / memops50.STAGE4_ROOT
    cases: list[RetrievalCase] = []
    for item in manifest["items"]:
        if not isinstance(item, dict):
            raise ValueError("MemOps-50 manifest item is invalid")
        source_file = str(item["source_file"])
        stage2_raw = (stage2_root / source_file).read_bytes()
        stage4_raw = (stage4_root / source_file).read_bytes()
        if memops50.sha256_bytes(stage2_raw) != item["stage2_sha256"]:
            raise ValueError(f"MemOps-50 Stage 2 digest changed: {source_file}")
        if memops50.sha256_bytes(stage4_raw) != item["stage4_sha256"]:
            raise ValueError(f"MemOps-50 Stage 4 digest changed: {source_file}")
        cases.append(
            _parse_case(
                stage2=memops50._load_json(stage2_raw, label=source_file),
                stage4=memops50._load_json(stage4_raw, label=source_file),
                source_file=source_file,
                question_pair_id=str(item["question_pair_id"]),
                operation_type=str(item["operation_type"]),
                evaluation_type=str(item["evaluation_type"]),
                difficulty=str(item["difficulty"]),
            )
        )
    if len(cases) != 50:
        raise ValueError("MemOps-50 retrieval tier must contain 50 cases")
    counts = _dataset_counts(cases)
    expected_counts = {
        "adjacent_segment_count": 150,
        "longitudinal_segment_count": 2500,
        "longitudinal_evidence_carrier_count": 150,
        "longitudinal_distractor_segment_count": 285,
        "gold_provenance_item_count": 165,
        "unique_gold_turn_count": 164,
        "gold_segment_count": 110,
    }
    if counts != expected_counts:
        raise ValueError(f"MemOps-50 retrieval structure changed: {counts}")
    return tuple(cases)


def run_evaluation(
    *,
    manifest_path: Path,
    memops_root: Path,
    metric_contract_path: Path,
    repository_root: Path,
) -> dict[str, Any]:
    cases = load_cases(manifest_path=manifest_path, memops_root=memops_root)
    contract_bytes = metric_contract_path.read_bytes()
    contract = memops50._load_json(contract_bytes, label="MemOps-50 retrieval contract")
    _validate_contract(contract)
    top_k = int(contract["retrieval"]["top_k"])
    outcomes = {
        setting: [
            _evaluate_case(case, setting=setting, top_k=top_k)
            for case in cases
        ]
        for setting in _SETTINGS
    }
    metrics = {setting: _metrics(rows) for setting, rows in outcomes.items()}
    comparisons = {
        "provenance_turn_recall_macro_drop": round(
            metrics["adjacent_operation"]["provenance_turn_recall_macro_at_k"]
            - metrics["longitudinal_operation"]["provenance_turn_recall_macro_at_k"],
            6,
        ),
        "provenance_segment_recall_macro_drop": round(
            metrics["adjacent_operation"]["provenance_segment_recall_macro_at_k"]
            - metrics["longitudinal_operation"]["provenance_segment_recall_macro_at_k"],
            6,
        ),
        "complete_case_recall_drop": round(
            metrics["adjacent_operation"]["complete_case_recall_rate"]
            - metrics["longitudinal_operation"]["complete_case_recall_rate"],
            6,
        ),
        "mean_reciprocal_rank_drop": round(
            metrics["adjacent_operation"]["mean_reciprocal_rank"]
            - metrics["longitudinal_operation"]["mean_reciprocal_rank"],
            6,
        ),
    }
    repository = _repository_state(repository_root)
    verdict = _gate_verdict(
        metrics["adjacent_operation"],
        metrics["longitudinal_operation"],
        comparisons,
        contract["gates"],
        repository_clean=not repository["dirty"],
    )
    return {
        "schema_version": 1,
        "evaluation_id": "memops50-production-activity-retrieval-v1",
        "repository": repository,
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "sqlite": sqlite3.sqlite_version,
        },
        "dataset": {
            "tier_id": "memops50-adjacent-longitudinal-v1",
            "manifest_path": str(manifest_path.relative_to(repository_root)),
            "manifest_sha256": memops50.sha256_bytes(manifest_path.read_bytes()),
            "upstream_commit": memops50.UPSTREAM_COMMIT,
            "logical_pair_count": len(cases),
            "row_count": sum(len(rows) for rows in outcomes.values()),
            **_dataset_counts(cases),
        },
        "metric_contract": {
            "path": str(metric_contract_path.relative_to(repository_root)),
            "sha256": memops50.sha256_bytes(contract_bytes),
        },
        "retrieval_contract": {
            "ranker": "production_activity_sqlite_fts5_bm25",
            "query_mode": "strict_and_then_or_only_after_zero_hits",
            "unit": "dataset_native_conversation_segment_proxy",
            "top_k": top_k,
            "adjacency_radius": 0,
            "model_calls": 0,
            "answer_generation": False,
            "corpus_scope": "one_isolated_scenario_per_query",
        },
        "variants": {
            setting: {"metrics": metrics[setting], "cases": outcomes[setting]}
            for setting in _SETTINGS
        },
        "comparisons": comparisons,
        "gate_verdict": verdict,
    }


def write_report(report: dict[str, Any], output: Path | None) -> str:
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    return encoded


def _parse_case(
    *,
    stage2: object,
    stage4: object,
    source_file: str,
    question_pair_id: str,
    operation_type: str,
    evaluation_type: str,
    difficulty: str,
) -> RetrievalCase:
    if not isinstance(stage2, dict) or not isinstance(stage4, dict):
        raise ValueError(f"MemOps retrieval payload must be an object: {source_file}")
    if stage2.get("operation_type") != operation_type or stage4.get("operation_type") != operation_type:
        raise ValueError(f"MemOps retrieval operation type changed: {source_file}")
    adjacent_answer = _question_answer(
        stage2,
        source_file=source_file,
        question_pair_id=question_pair_id,
        setting="adjacent_operation",
    )
    longitudinal_answer = _question_answer(
        stage4,
        source_file=source_file,
        question_pair_id=question_pair_id,
        setting="longitudinal_operation",
    )
    if memops50._without_setting(adjacent_answer) != memops50._without_setting(
        longitudinal_answer
    ):
        raise ValueError(f"MemOps retrieval question pair drifted: {source_file}")
    query = _required_text(adjacent_answer.get("question"), "question", source_file)
    adjacent_conversations = _conversation_map(
        stage2,
        source_file=source_file,
        expected_indices=range(1, 4),
    )
    longitudinal_conversations = _conversation_map(
        stage4,
        source_file=source_file,
        expected_indices=range(1, 51),
    )
    adjacent_gold, provenance_item_count = _gold_locations(
        adjacent_answer,
        conversations=adjacent_conversations,
        source_file=source_file,
    )
    insertion_map = _insertion_map(
        stage4,
        source_conversations=adjacent_conversations,
        carrier_conversations=longitudinal_conversations,
        source_file=source_file,
    )
    longitudinal_gold: list[EvidenceLocation] = []
    for location in adjacent_gold:
        carrier_segment, insertion_index = insertion_map[location.segment_index]
        carrier_turn = insertion_index + location.turn_index
        source_turn = adjacent_conversations[location.segment_index]["dialogue"][
            location.turn_index - 1
        ]
        carrier_turn_value = longitudinal_conversations[carrier_segment]["dialogue"][
            carrier_turn - 1
        ]
        if source_turn != carrier_turn_value:
            raise ValueError(f"MemOps injected evidence bytes changed: {source_file}")
        longitudinal_gold.append(
            EvidenceLocation(segment_index=carrier_segment, turn_index=carrier_turn)
        )
    return RetrievalCase(
        source_file=source_file,
        question_pair_id=question_pair_id,
        operation_type=operation_type,
        evaluation_type=evaluation_type,
        difficulty=difficulty,
        query=query,
        adjacent_units=_units(adjacent_conversations, longitudinal=False),
        longitudinal_units=_units(longitudinal_conversations, longitudinal=True),
        adjacent_gold=adjacent_gold,
        longitudinal_gold=tuple(longitudinal_gold),
        gold_provenance_item_count=provenance_item_count,
    )


def _evaluate_case(
    case: RetrievalCase,
    *,
    setting: Setting,
    top_k: int,
) -> dict[str, Any]:
    units = case.adjacent_units if setting == "adjacent_operation" else case.longitudinal_units
    gold = case.adjacent_gold if setting == "adjacent_operation" else case.longitudinal_gold
    with tempfile.TemporaryDirectory(prefix="openchronicle-memops50-retrieval-") as root:
        db_path = Path(root) / "retrieval.db"
        with fts.cursor(db_path) as conn:
            _seed_units(conn, units, setting=setting)
            hits = activity_store.search(conn, query=case.query, top_k=top_k)
    retrieved = [int(hit.session_id.removeprefix("segment-")) for hit in hits]
    expected_segments = sorted({location.segment_index for location in gold})
    expected_set = set(expected_segments)
    matched_segments = sorted(expected_set & set(retrieved))
    matched_turn_count = sum(location.segment_index in retrieved for location in gold)
    distractor_segments = {unit.segment_index for unit in units if unit.distractor}
    distractor_hits = [segment for segment in retrieved if segment in distractor_segments]
    first_expected_rank = next(
        (rank for rank, segment in enumerate(retrieved, start=1) if segment in expected_set),
        None,
    )
    unit_by_segment = {unit.segment_index: unit for unit in units}
    return {
        "source_file": case.source_file,
        "question_pair_id": case.question_pair_id,
        "operation_type": case.operation_type,
        "evaluation_type": case.evaluation_type,
        "difficulty": case.difficulty,
        "setting": setting,
        "query_sha256": hashlib.sha256(case.query.encode()).hexdigest(),
        "corpus_segment_count": len(units),
        "gold_provenance_turn_count": len(gold),
        "gold_provenance_segment_count": len(expected_segments),
        "gold_segments": expected_segments,
        "retrieved_segments": retrieved,
        "matched_gold_segments": matched_segments,
        "matched_gold_turn_count": matched_turn_count,
        "provenance_turn_recall_at_k": round(matched_turn_count / len(gold), 6),
        "provenance_segment_recall_at_k": round(
            len(matched_segments) / len(expected_segments),
            6,
        ),
        "complete_recall": len(matched_segments) == len(expected_segments),
        "reciprocal_rank": round(1 / first_expected_rank, 6) if first_expected_rank else 0.0,
        "distractor_segments_retrieved": distractor_hits,
        "distractor_at_1": bool(retrieved and retrieved[0] in distractor_segments),
        "non_gold_hit_count": sum(segment not in expected_set for segment in retrieved),
        "context_chars": sum(len(unit_by_segment[segment].content) for segment in retrieved),
        "query_mode": hits[0].query_mode if hits else "empty",
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    turn_count = sum(int(row["gold_provenance_turn_count"]) for row in rows)
    segment_count = sum(int(row["gold_provenance_segment_count"]) for row in rows)
    retrieved_count = sum(len(row["retrieved_segments"]) for row in rows)
    categories = sorted({str(row["evaluation_type"]) for row in rows})
    return {
        "case_count": len(rows),
        "provenance_turn_recall_macro_at_k": round(
            sum(
                int(row["matched_gold_turn_count"])
                / int(row["gold_provenance_turn_count"])
                for row in rows
            )
            / len(rows),
            6,
        ),
        "provenance_turn_recall_micro_at_k": round(
            sum(int(row["matched_gold_turn_count"]) for row in rows) / turn_count,
            6,
        ),
        "provenance_segment_recall_macro_at_k": round(
            sum(
                len(row["matched_gold_segments"])
                / int(row["gold_provenance_segment_count"])
                for row in rows
            )
            / len(rows),
            6,
        ),
        "provenance_segment_recall_micro_at_k": round(
            sum(len(row["matched_gold_segments"]) for row in rows) / segment_count,
            6,
        ),
        "complete_case_recall_rate": round(
            sum(bool(row["complete_recall"]) for row in rows) / len(rows),
            6,
        ),
        "mean_reciprocal_rank": round(
            sum(float(row["reciprocal_rank"]) for row in rows) / len(rows),
            6,
        ),
        "cases_with_injected_distractor_hit_rate": round(
            sum(bool(row["distractor_segments_retrieved"]) for row in rows) / len(rows),
            6,
        ),
        "injected_distractor_contamination_at_k": round(
            sum(len(row["distractor_segments_retrieved"]) for row in rows)
            / max(retrieved_count, 1),
            6,
        ),
        "injected_distractor_at_1_rate": round(
            sum(bool(row["distractor_at_1"]) for row in rows) / len(rows),
            6,
        ),
        "returned_non_gold_share": round(
            sum(int(row["non_gold_hit_count"]) for row in rows)
            / max(retrieved_count, 1),
            6,
        ),
        "empty_hit_rate": round(
            sum(not row["retrieved_segments"] for row in rows) / len(rows),
            6,
        ),
        "strict_and_case_rate": round(
            sum(row["query_mode"] == "strict_and" for row in rows) / len(rows),
            6,
        ),
        "strict_and_case_count": sum(row["query_mode"] == "strict_and" for row in rows),
        "relaxed_or_case_count": sum(
            row["query_mode"] == "relaxed_or_after_zero_hits" for row in rows
        ),
        "mean_context_chars": round(
            sum(int(row["context_chars"]) for row in rows) / len(rows),
            6,
        ),
        "complete_case_recall_by_evaluation_type": {
            category: round(
                sum(
                    bool(row["complete_recall"])
                    for row in rows
                    if row["evaluation_type"] == category
                )
                / sum(row["evaluation_type"] == category for row in rows),
                6,
            )
            for category in categories
        },
    }


def _seed_units(conn, units: tuple[RetrievalUnit, ...], *, setting: Setting) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    ids = [f"{setting}-segment-{unit.segment_index}" for unit in units]
    for ordinal, unit in enumerate(units):
        timestamp = (start + timedelta(minutes=ordinal)).isoformat(timespec="minutes")
        activity_store._insert_event(
            conn,
            activity_store.ActivityEvent(
                id=ids[ordinal],
                source_path="event-2026-01-01.md",
                source_entry_id=f"source-{ids[ordinal]}",
                source_entry_timestamp=timestamp,
                source_content_hash=hashlib.sha256(unit.content.encode()).hexdigest(),
                session_id=f"segment-{unit.segment_index}",
                ordinal=ordinal,
                start_time=timestamp,
                end_time=timestamp,
                app_name="",
                content=unit.content,
                summary="",
                previous_event_id=ids[ordinal - 1] if ordinal else None,
                next_event_id=ids[ordinal + 1] if ordinal + 1 < len(ids) else None,
            ),
        )


def _conversation_map(
    payload: dict[str, Any],
    *,
    source_file: str,
    expected_indices: range,
) -> dict[int, dict[str, Any]]:
    conversations = payload.get("conversations")
    if not isinstance(conversations, list) or not conversations:
        raise ValueError(f"MemOps conversations are missing: {source_file}")
    result: dict[int, dict[str, Any]] = {}
    for conversation in conversations:
        if not isinstance(conversation, dict) or not isinstance(conversation.get("dialogue"), list):
            raise ValueError(f"MemOps conversation is invalid: {source_file}")
        segment_index = conversation.get("segment_index")
        if type(segment_index) is not int or segment_index < 1 or segment_index in result:
            raise ValueError(f"MemOps segment index is invalid: {source_file}")
        dialogue = conversation["dialogue"]
        if not dialogue:
            raise ValueError(f"MemOps dialogue is empty: {source_file}")
        for turn in dialogue:
            if (
                not isinstance(turn, dict)
                or set(turn) != {"role", "content"}
                or turn.get("role") not in {"user", "assistant"}
                or not isinstance(turn.get("content"), str)
                or not turn["content"]
            ):
                raise ValueError(f"MemOps dialogue turn is invalid: {source_file}")
        result[segment_index] = conversation
    if list(result) != list(expected_indices):
        raise ValueError(f"MemOps segment sequence changed: {source_file}")
    return result


def _units(
    conversations: dict[int, dict[str, Any]],
    *,
    longitudinal: bool,
) -> tuple[RetrievalUnit, ...]:
    result: list[RetrievalUnit] = []
    for segment_index, conversation in conversations.items():
        evidence = bool(conversation.get("evidence_inserted")) if longitudinal else True
        distractor = bool(conversation.get("distractor_inserted")) if longitudinal else False
        if evidence and distractor:
            raise ValueError("MemOps carrier cannot also be a distractor")
        content = "\n".join(turn["content"] for turn in conversation["dialogue"])
        result.append(
            RetrievalUnit(
                segment_index=segment_index,
                content=content,
                evidence=evidence,
                distractor=distractor,
            )
        )
    return tuple(result)


def _question_answer(
    payload: dict[str, Any],
    *,
    source_file: str,
    question_pair_id: str,
    setting: Setting,
) -> dict[str, Any]:
    answers = payload.get("answer")
    if not isinstance(answers, list):
        raise ValueError(f"MemOps answers are missing: {source_file}")
    matched = [
        answer
        for answer in answers
        if isinstance(answer, dict)
        and answer.get("question_pair_id") == question_pair_id
        and answer.get("evaluation_setting") == setting
    ]
    if len(matched) != 1:
        raise ValueError(f"MemOps retrieval answer is ambiguous: {source_file}")
    return matched[0]


def _gold_locations(
    answer: dict[str, Any],
    *,
    conversations: dict[int, dict[str, Any]],
    source_file: str,
) -> tuple[tuple[EvidenceLocation, ...], int]:
    provenance = answer.get("gold_provenance")
    if not isinstance(provenance, list) or not provenance:
        raise ValueError(f"MemOps gold provenance is missing: {source_file}")
    result: list[EvidenceLocation] = []
    seen: set[tuple[int, int]] = set()
    for span in provenance:
        if not isinstance(span, dict):
            raise ValueError(f"MemOps gold provenance is invalid: {source_file}")
        segment_index = span.get("segment_index")
        turn_index = span.get("turn_index")
        quote = span.get("quote")
        if (
            type(segment_index) is not int
            or type(turn_index) is not int
            or segment_index not in conversations
            or not 1 <= turn_index <= len(conversations[segment_index]["dialogue"])
            or not isinstance(quote, str)
            or not quote
        ):
            raise ValueError(f"MemOps gold provenance does not resolve: {source_file}")
        content = conversations[segment_index]["dialogue"][turn_index - 1]["content"]
        if quote not in content:
            raise ValueError(f"MemOps gold provenance quote changed: {source_file}")
        key = (segment_index, turn_index)
        if key in seen:
            # MemOps may attach multiple independently required quote anchors
            # to one dialogue turn. Retrieval is scored at turn granularity,
            # so validate every quote above and count the turn once.
            continue
        seen.add(key)
        result.append(EvidenceLocation(segment_index=segment_index, turn_index=turn_index))
    return tuple(result), len(provenance)


def _insertion_map(
    payload: dict[str, Any],
    *,
    source_conversations: dict[int, dict[str, Any]],
    carrier_conversations: dict[int, dict[str, Any]],
    source_file: str,
) -> dict[int, tuple[int, int]]:
    metadata = payload.get("injection_metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("insertions"), list):
        raise ValueError(f"MemOps injection metadata is missing: {source_file}")
    result: dict[int, tuple[int, int]] = {}
    for insertion in metadata["insertions"]:
        if not isinstance(insertion, dict):
            raise ValueError(f"MemOps evidence insertion is invalid: {source_file}")
        source_segment = insertion.get("evidence_segment_index")
        carrier_segment = insertion.get("conversation_index")
        insertion_index = insertion.get("insertion_index")
        if (
            type(source_segment) is not int
            or source_segment not in source_conversations
            or source_segment in result
            or type(carrier_segment) is not int
            or carrier_segment not in carrier_conversations
            or type(insertion_index) is not int
            or insertion_index < 0
        ):
            raise ValueError(f"MemOps evidence insertion does not resolve: {source_file}")
        source_dialogue = source_conversations[source_segment]["dialogue"]
        carrier = carrier_conversations[carrier_segment]
        if (
            carrier.get("evidence_inserted") is not True
            or carrier.get("evidence_segment_index") != source_segment
            or carrier.get("insertion_index") != insertion_index
            or carrier["dialogue"][insertion_index : insertion_index + len(source_dialogue)]
            != source_dialogue
        ):
            raise ValueError(f"MemOps evidence carrier changed: {source_file}")
        result[source_segment] = (carrier_segment, insertion_index)
    if set(result) != set(source_conversations):
        raise ValueError(f"MemOps evidence insertion coverage changed: {source_file}")
    carrier_segments = [carrier for carrier, _index in result.values()]
    if len(set(carrier_segments)) != len(carrier_segments):
        raise ValueError(f"MemOps evidence carriers are not one-to-one: {source_file}")
    local_carriers = {
        segment_index
        for segment_index, conversation in carrier_conversations.items()
        if conversation.get("evidence_inserted") is True
    }
    if set(carrier_segments) != local_carriers:
        raise ValueError(f"MemOps evidence carrier coverage changed: {source_file}")
    return result


def _dataset_counts(cases: tuple[RetrievalCase, ...] | list[RetrievalCase]) -> dict[str, int]:
    return {
        "adjacent_segment_count": sum(len(case.adjacent_units) for case in cases),
        "longitudinal_segment_count": sum(len(case.longitudinal_units) for case in cases),
        "longitudinal_evidence_carrier_count": sum(
            unit.evidence for case in cases for unit in case.longitudinal_units
        ),
        "longitudinal_distractor_segment_count": sum(
            unit.distractor for case in cases for unit in case.longitudinal_units
        ),
        "gold_provenance_item_count": sum(
            case.gold_provenance_item_count for case in cases
        ),
        "unique_gold_turn_count": sum(len(case.adjacent_gold) for case in cases),
        "gold_segment_count": sum(
            len({location.segment_index for location in case.adjacent_gold})
            for case in cases
        ),
    }


def _validate_contract(contract: object) -> None:
    if not isinstance(contract, dict) or set(contract) != {
        "schema_version",
        "tier_id",
        "retrieval",
        "gates",
    }:
        raise ValueError("MemOps retrieval contract envelope is invalid")
    if contract["schema_version"] != 1 or contract["tier_id"] != "memops50-stage4-retrieval-v1":
        raise ValueError("MemOps retrieval contract identity is invalid")
    retrieval = contract["retrieval"]
    if not isinstance(retrieval, dict) or set(retrieval) != {
        "ranker",
        "unit",
        "top_k",
        "adjacency_radius",
    }:
        raise ValueError("MemOps retrieval configuration is invalid")
    if (
        retrieval["ranker"] != "production_activity_sqlite_fts5_bm25"
        or retrieval["unit"] != "conversation_segment"
        or type(retrieval["top_k"]) is not int
        or not 1 <= retrieval["top_k"] <= 20
        or retrieval["adjacency_radius"] != 0
    ):
        raise ValueError("MemOps retrieval configuration changed")
    gates = contract["gates"]
    expected = {
        "longitudinal_provenance_turn_recall_at_k_min",
        "adjacent_provenance_segment_recall_at_k_min",
        "longitudinal_provenance_segment_recall_at_k_min",
        "longitudinal_complete_case_recall_rate_min",
        "longitudinal_mean_reciprocal_rank_min",
        "provenance_segment_recall_drop_max",
        "mean_reciprocal_rank_drop_max",
        "longitudinal_injected_distractor_contamination_at_k_max",
        "longitudinal_injected_distractor_at_1_rate_max",
        "longitudinal_empty_hit_rate_max",
        "repository_clean_required",
    }
    if not isinstance(gates, dict) or set(gates) != expected:
        raise ValueError("MemOps retrieval gates are invalid")
    for key in expected - {"repository_clean_required"}:
        value = gates[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError(f"MemOps retrieval gate is invalid: {key}")
    if gates["repository_clean_required"] is not True:
        raise ValueError("MemOps retrieval clean-repository gate is required")


def _gate_verdict(
    adjacent: dict[str, Any],
    longitudinal: dict[str, Any],
    comparisons: dict[str, float],
    gates: dict[str, Any],
    *,
    repository_clean: bool,
) -> dict[str, Any]:
    checks = {
        "adjacent_provenance_segment_recall_macro_at_k": (
            adjacent["provenance_segment_recall_macro_at_k"]
            >= gates["adjacent_provenance_segment_recall_at_k_min"]
        ),
        "longitudinal_provenance_turn_recall_at_k": (
            longitudinal["provenance_turn_recall_macro_at_k"]
            >= gates["longitudinal_provenance_turn_recall_at_k_min"]
        ),
        "longitudinal_provenance_segment_recall_at_k": (
            longitudinal["provenance_segment_recall_macro_at_k"]
            >= gates["longitudinal_provenance_segment_recall_at_k_min"]
        ),
        "longitudinal_complete_case_recall_rate": (
            longitudinal["complete_case_recall_rate"]
            >= gates["longitudinal_complete_case_recall_rate_min"]
        ),
        "longitudinal_mean_reciprocal_rank": (
            longitudinal["mean_reciprocal_rank"]
            >= gates["longitudinal_mean_reciprocal_rank_min"]
        ),
        "provenance_segment_recall_drop": (
            comparisons["provenance_segment_recall_macro_drop"]
            <= gates["provenance_segment_recall_drop_max"]
        ),
        "mean_reciprocal_rank_drop": (
            comparisons["mean_reciprocal_rank_drop"]
            <= gates["mean_reciprocal_rank_drop_max"]
        ),
        "longitudinal_injected_distractor_contamination_at_k": (
            longitudinal["injected_distractor_contamination_at_k"]
            <= gates["longitudinal_injected_distractor_contamination_at_k_max"]
        ),
        "longitudinal_injected_distractor_at_1_rate": (
            longitudinal["injected_distractor_at_1_rate"]
            <= gates["longitudinal_injected_distractor_at_1_rate_max"]
        ),
        "longitudinal_empty_hit_rate": (
            longitudinal["empty_hit_rate"]
            <= gates["longitudinal_empty_hit_rate_max"]
        ),
        "repository_clean": repository_clean,
    }
    return {"passed": all(checks.values()), "checks": checks, "gates": gates}


def _required_text(value: object, field: str, source_file: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"MemOps {field} is invalid: {source_file}")
    return value


def _repository_state(root: Path) -> dict[str, Any]:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()

    status = git("status", "--short")
    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(status),
        "status_lines": len(status.splitlines()) if status else 0,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memops-root", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("benchmarks/memops50-lifecycle-v1/json/manifest.json"),
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("benchmarks/memops50-lifecycle-v1/json/retrieval_metric_contract.json"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    repository_root = Path.cwd().resolve()
    report = run_evaluation(
        manifest_path=args.manifest.resolve(),
        memops_root=args.memops_root.expanduser().resolve(),
        metric_contract_path=args.contract.resolve(),
        repository_root=repository_root,
    )
    print(write_report(report, args.output))
    return 0 if report["gate_verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
