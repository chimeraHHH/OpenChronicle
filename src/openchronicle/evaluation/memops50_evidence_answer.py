"""Evaluate recall, evidence distillation, cited answers, and faithfulness."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import inspect
import json
import os
import platform
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config as config_mod
from ..activity import store as activity_store
from ..config import Config
from ..store import fts
from ..writer import llm as llm_mod
from . import memops50
from . import memops50_retrieval as retrieval

Provider = Callable[[str, str], object]
_SETTINGS: tuple[retrieval.Setting, ...] = (
    "adjacent_operation",
    "longitudinal_operation",
)
ABSTENTION_TEXT = "Insufficient selected evidence."
_VALIDATION_LOCKS: dict[Path, int] = {}


@dataclass(frozen=True, slots=True)
class EvidenceAnswerCase:
    retrieval: retrieval.RetrievalCase
    gold_spec: dict[str, Any]
    correctness_gold: dict[str, Any]
    adjacent_turns: tuple[tuple[int, tuple[DialogueTurn, ...]], ...]
    longitudinal_turns: tuple[tuple[int, tuple[DialogueTurn, ...]], ...]


@dataclass(frozen=True, slots=True)
class DialogueTurn:
    turn_index: int
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class CandidateTurn:
    ref: str
    segment_id: int
    turn_index: int
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class Candidate:
    segment_id: int
    rank: int
    turns: tuple[CandidateTurn, ...]
    distractor: bool
    query_mode: str


@dataclass(frozen=True, slots=True)
class DistillPrediction:
    selection_status: str
    selected_evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerPart:
    part_id: str
    text: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerPrediction:
    abstained: bool
    parts: tuple[AnswerPart, ...]


@dataclass(frozen=True, slots=True)
class FaithfulnessPart:
    part_id: str
    support: str
    entailed_evidence_refs: tuple[str, ...]
    citation_complete: bool


@dataclass(frozen=True, slots=True)
class FaithfulnessPrediction:
    part_results: tuple[FaithfulnessPart, ...]
    all_parts_faithful: bool


@dataclass(frozen=True, slots=True)
class CorrectnessPrediction:
    answer_correct: bool
    harmful_extra: bool
    forget_leakage: bool | None
    over_forget: bool | None
    stale_value: bool | None
    reflection_precision: bool | None
    reflection_recall: bool | None
    trajectory_order: bool | None
    final_state: bool | None
    intermediate_state: bool | None
    reason_codes: tuple[str, ...]


def load_cases(
    *,
    manifest_path: Path,
    memops_root: Path,
    exclusion_paths: tuple[Path, ...] = (),
) -> tuple[EvidenceAnswerCase, ...]:
    recall_cases = retrieval.load_cases(
        manifest_path=manifest_path,
        memops_root=memops_root,
        exclusion_paths=exclusion_paths,
    )
    stage2_root = memops_root.resolve() / memops50.STAGE2_ROOT
    stage4_root = memops_root.resolve() / memops50.STAGE4_ROOT
    result: list[EvidenceAnswerCase] = []
    for case in recall_cases:
        stage2 = memops50._load_json(
            (stage2_root / case.source_file).read_bytes(),
            label=case.source_file,
        )
        stage4 = memops50._load_json(
            (stage4_root / case.source_file).read_bytes(),
            label=case.source_file,
        )
        if not isinstance(stage2, dict) or not isinstance(stage4, dict):
            raise ValueError(f"MemOps Stage 2 payload is invalid: {case.source_file}")
        gold_spec = retrieval._question_answer(
            stage2,
            source_file=case.source_file,
            question_pair_id=case.question_pair_id,
            setting="adjacent_operation",
        )
        if gold_spec.get("question") != case.query:
            raise ValueError(f"MemOps answer question changed: {case.source_file}")
        adjacent_conversations = retrieval._conversation_map(
            stage2,
            source_file=case.source_file,
            expected_indices=range(1, 4),
        )
        longitudinal_conversations = retrieval._conversation_map(
            stage4,
            source_file=case.source_file,
            expected_indices=range(1, 51),
        )

        def dialogue_turns(
            conversations: dict[int, dict[str, Any]],
        ) -> tuple[tuple[int, tuple[DialogueTurn, ...]], ...]:
            return tuple(
                (
                    segment_id,
                    tuple(
                        DialogueTurn(
                            turn_index=turn_index,
                            role=str(turn["role"]),
                            content=str(turn["content"]),
                        )
                        for turn_index, turn in enumerate(
                            conversation["dialogue"],
                            start=1,
                        )
                    ),
                )
                for segment_id, conversation in conversations.items()
            )

        correctness_gold = {
            "target_fact": stage2.get("target_fact"),
            "operation_type": stage2.get("operation_type"),
            "difficulty_knobs": stage2.get("difficulty_knobs"),
            "operations": stage2.get("operations"),
        }
        if (
            not isinstance(correctness_gold["target_fact"], str)
            or correctness_gold["operation_type"] != case.operation_type
            or not isinstance(correctness_gold["difficulty_knobs"], dict)
            or not isinstance(correctness_gold["operations"], list)
        ):
            raise ValueError(f"MemOps correctness gold is invalid: {case.source_file}")
        result.append(
            EvidenceAnswerCase(
                retrieval=case,
                gold_spec=dict(gold_spec),
                correctness_gold=correctness_gold,
                adjacent_turns=dialogue_turns(adjacent_conversations),
                longitudinal_turns=dialogue_turns(longitudinal_conversations),
            )
        )
    return tuple(result)


def retrieve_candidates(
    case: EvidenceAnswerCase,
    *,
    setting: retrieval.Setting,
    top_k: int,
) -> tuple[Candidate, ...]:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        raise ValueError("candidate top_k must be an integer in [1, 20]")
    if setting not in _SETTINGS:
        raise ValueError("evidence-answer setting is invalid")
    units = (
        case.retrieval.adjacent_units
        if setting == "adjacent_operation"
        else case.retrieval.longitudinal_units
    )
    turns = (
        case.adjacent_turns
        if setting == "adjacent_operation"
        else case.longitudinal_turns
    )
    with (
        tempfile.TemporaryDirectory(prefix="openchronicle-memops50-answer-") as root,
        fts.cursor(Path(root) / "retrieval.db") as conn,
    ):
        retrieval._seed_units(
            conn,
            units,
            setting=setting,
        )
        hits = activity_store.search(conn, query=case.retrieval.query, top_k=top_k)
    units_by_segment = {unit.segment_index: unit for unit in units}
    turns_by_segment = dict(turns)
    return tuple(
        Candidate(
            segment_id=int(hit.session_id.removeprefix("segment-")),
            rank=rank,
            turns=tuple(
                CandidateTurn(
                    ref=f"R{rank:02d}-T{turn.turn_index:02d}",
                    segment_id=int(hit.session_id.removeprefix("segment-")),
                    turn_index=turn.turn_index,
                    role=turn.role,
                    content=turn.content,
                )
                for turn in turns_by_segment[
                    int(hit.session_id.removeprefix("segment-"))
                ]
            ),
            distractor=units_by_segment[
                int(hit.session_id.removeprefix("segment-"))
            ].distractor,
            query_mode=hit.query_mode,
        )
        for rank, hit in enumerate(hits, start=1)
    )


DISTILL_SYSTEM_PROMPT = """You are an evidence selector, not an answer writer.
Choose the smallest set of supplied dialogue turns that is sufficient to
answer the question correctly. Return at most the stated limit. Preserve the
complete lifecycle boundary: updates may require old, tentative, retracted, and
confirmed evidence; forget questions may require both the deletion request and
separate retained facts; reflection questions require evidence for both the
supported pattern and its limits. Do not answer the question. Do not invent or
quote evidence. Return only the requested JSON object."""


ANSWER_SYSTEM_PROMPT = """Answer only from the selected evidence turns.
Every factual answer part must be supported by its cited evidence refs. If the selected
evidence is insufficient, abstain instead of guessing. Respect lifecycle state:
do not treat stale, tentative, retracted, or superseded values as current. When
the user asked to forget a raw value, do not reveal that value; name only the
safe removed target or category when needed. Return only the requested JSON
object and perform no action. If you abstain, emit exactly one part whose text is
"Insufficient selected evidence." and whose evidence_refs is empty."""


FAITHFULNESS_SYSTEM_PROMPT = """Independently grade whether each answer part is
supported by its cited evidence. You receive no expected answer, gold provenance,
lifecycle label, or hidden context. Treat only the supplied evidence turns as
evidence. Each part is paired only with the turns it cited; no other selected turn
is visible. A fixed abstention has no factual claim and is fully supported with
no entailed refs. Return only the requested JSON object."""


CORRECTNESS_SYSTEM_PROMPT = """Independently grade semantic correctness and
lifecycle safety against the supplied gold specification. Do not grade citation
support; a separate judge handles faithfulness. Correct historical mention of an
old value is not stale use. Missing a retained neighbor is not over-forget unless
the answer says it was removed or unavailable. Return only the requested JSON
object."""


def distill_prompt(
    case: EvidenceAnswerCase,
    candidates: Sequence[Candidate],
    *,
    max_selected: int,
) -> str:
    return (
        f"Question:\n{case.retrieval.query}\n\n"
        + _candidate_options(case)
        + f"Maximum selected turns: {max_selected}\n\n"
        + "Candidate segments and opaque turn refs, in retrieval order. "
        "Retrieval rank is not time order:\n"
        + _format_candidate_segments(candidates)
        + '\n\nReturn JSON exactly as: {"selection_status":"selected" or "insufficient", '
        '"selected_evidence_refs":["R01-T01", ...]}'
    )


def answer_prompt(
    case: EvidenceAnswerCase,
    selected: Sequence[CandidateTurn],
) -> str:
    return (
        f"Question:\n{case.retrieval.query}\n\n"
        + _candidate_options(case)
        + "Selected evidence turns:\n"
        + _format_turns(selected)
        + "\n\nReturn JSON exactly as: "
        '{"abstained": boolean, "answer_parts": [{"part_id":"P1", '
        '"text":"string", "evidence_refs":["R01-T01", ...]}]}. '
        "Non-abstaining answers require 1-8 parts and at least one selected ref per part. "
        f'For abstention, emit exactly one part with text "{ABSTENTION_TEXT}" and no refs.'
    )


def faithfulness_prompt(
    case: EvidenceAnswerCase,
    answer: AnswerPrediction,
    selected: Sequence[CandidateTurn],
) -> str:
    if len(answer.parts) != 1:
        raise ValueError("faithfulness prompt requires exactly one isolated answer part")
    turns_by_ref = {turn.ref: turn for turn in selected}
    isolated_parts = [
        {
            "part_id": part.part_id,
            "text": part.text,
            "cited_evidence_turns": [
                {
                    "ref": ref,
                    "role": turns_by_ref[ref].role,
                    "content": turns_by_ref[ref].content,
                }
                for ref in part.evidence_refs
            ],
        }
        for part in answer.parts
    ]
    return (
        f"Answer part ID: {answer.parts[0].part_id}\n"
        f"Question:\n{case.retrieval.query}\n\n"
        "Answer parts, each isolated with only its cited evidence turns:\n"
        + json.dumps(isolated_parts, ensure_ascii=False, sort_keys=True)
        + "\n\nReturn JSON exactly as: "
        '{"part_results":[{"part_id":"P1", '
        '"support":"fully_supported" or "partially_supported" or "unsupported" or "contradicted", '
        '"entailed_evidence_refs":["R01-T01", ...], "citation_complete":boolean}], '
        '"all_parts_faithful":boolean}'
    )


def correctness_prompt(
    case: EvidenceAnswerCase,
    answer: AnswerPrediction,
) -> str:
    spec = case.gold_spec
    grading = {
        key: spec.get(key)
        for key in (
            "evaluation_type",
            "evaluation_category",
            "state_transition_probe_type",
            "question",
            "expected_answer",
            "gold_memory_state",
            "judge_rubric",
            "diagnostic_checks",
            "candidate_options",
            "gold_reasoning_chain",
        )
        if key in spec
    }
    grading["stage2_correctness_gold"] = case.correctness_gold
    applicable = sorted(_applicable_correctness_fields(case))
    return (
        "Gold grading specification:\n"
        + json.dumps(grading, ensure_ascii=False, sort_keys=True)
        + "\n\nModel answer parts:\n"
        + json.dumps(_answer_parts_payload(answer), ensure_ascii=False, sort_keys=True)
        + "\n\nApplicable lifecycle output fields: "
        + json.dumps(applicable)
        + ". Every listed field must be boolean. Every unlisted lifecycle field "
        "must be null. answer_correct and harmful_extra are always boolean."
        + "\n\nReturn JSON exactly with these fields: "
        '{"answer_correct":boolean, "harmful_extra":boolean, '
        '"forget_leakage":boolean or null, "over_forget":boolean or null, '
        '"stale_value":boolean or null, "reflection_precision":boolean or null, '
        '"reflection_recall":boolean or null, "trajectory_order":boolean or null, '
        '"final_state":boolean or null, "intermediate_state":boolean or null, '
        '"reason_codes":["short_code", ...]}'
    )


def _candidate_options(case: EvidenceAnswerCase) -> str:
    options = case.gold_spec.get("candidate_options")
    if not isinstance(options, list) or not options:
        return ""
    return "Candidate options:\n" + json.dumps(options, ensure_ascii=False) + "\n\n"


def _format_candidate_segments(candidates: Sequence[Candidate]) -> str:
    if not candidates:
        return "(none)"
    return "\n\n".join(
        f"[Retrieval rank {item.rank}]\n{_format_turns(item.turns)}" for item in candidates
    )


def _format_turns(turns: Sequence[CandidateTurn]) -> str:
    if not turns:
        return "(none)"
    return "\n".join(f"[{turn.ref}] {turn.role}: {turn.content}" for turn in turns)


def _answer_parts_payload(answer: AnswerPrediction) -> dict[str, Any]:
    return {
        "abstained": answer.abstained,
        "answer_parts": [
            {
                "part_id": part.part_id,
                "text": part.text,
                "evidence_refs": list(part.evidence_refs),
            }
            for part in answer.parts
        ],
    }


def _user_prompt_templates_sha256() -> str:
    source = "\n".join(
        inspect.getsource(function)
        for function in (
            distill_prompt,
            answer_prompt,
            faithfulness_prompt,
            correctness_prompt,
            _candidate_options,
            _format_candidate_segments,
            _format_turns,
            _answer_parts_payload,
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def parse_distill_prediction(
    raw: object,
    *,
    candidate_refs: set[str],
    max_selected: int,
) -> DistillPrediction:
    payload = _json_object(raw, label="distiller")
    if set(payload) != {"selection_status", "selected_evidence_refs"}:
        raise ValueError("distiller response fields are invalid")
    status = payload["selection_status"]
    values = payload["selected_evidence_refs"]
    if status not in {"selected", "insufficient"}:
        raise ValueError("distiller selection_status is invalid")
    if not isinstance(values, list) or len(values) > max_selected:
        raise ValueError("distiller selected_evidence_refs are invalid")
    if any(not isinstance(value, str) or value not in candidate_refs for value in values):
        raise ValueError("distiller selected a non-candidate turn")
    if len(set(values)) != len(values):
        raise ValueError("distiller selected duplicate turns")
    if (status == "selected") != bool(values):
        raise ValueError("distiller selection status disagrees with selected refs")
    return DistillPrediction(
        selection_status=status,
        selected_evidence_refs=tuple(values),
    )


def parse_answer_prediction(
    raw: object,
    *,
    selected_refs: set[str],
) -> AnswerPrediction:
    payload = _json_object(raw, label="answerer")
    if set(payload) != {"answer_parts", "abstained"}:
        raise ValueError("answerer response fields are invalid")
    abstained = payload["abstained"]
    if type(abstained) is not bool:
        raise ValueError("answerer abstained flag is invalid")
    raw_parts = payload["answer_parts"]
    if not isinstance(raw_parts, list) or not 1 <= len(raw_parts) <= 8:
        raise ValueError("answerer answer_parts are invalid")
    parts: list[AnswerPart] = []
    for index, item in enumerate(raw_parts, start=1):
        if not isinstance(item, dict) or set(item) != {
            "part_id",
            "text",
            "evidence_refs",
        }:
            raise ValueError("answerer answer part fields are invalid")
        if item["part_id"] != f"P{index}":
            raise ValueError("answerer part IDs are invalid")
        text = item["text"]
        refs = item["evidence_refs"]
        if not isinstance(text, str) or not text.strip() or len(text) > 5_000:
            raise ValueError("answerer part text is invalid")
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) or ref not in selected_refs for ref in refs
        ):
            raise ValueError("answerer cited a non-selected turn")
        if len(set(refs)) != len(refs):
            raise ValueError("answerer part citations contain duplicates")
        if not abstained and not refs:
            raise ValueError("answerer emitted an uncited answer part")
        parts.append(
            AnswerPart(
                part_id=item["part_id"],
                text=text.strip(),
                evidence_refs=tuple(refs),
            )
        )
    if abstained and (
        len(parts) != 1
        or parts[0].evidence_refs
        or parts[0].text != ABSTENTION_TEXT
    ):
        raise ValueError("answerer abstention shape is invalid")
    return AnswerPrediction(abstained=abstained, parts=tuple(parts))


def parse_faithfulness_prediction(
    raw: object,
    *,
    answer: AnswerPrediction,
) -> FaithfulnessPrediction:
    payload = _json_object(raw, label="faithfulness judge")
    if set(payload) != {"part_results", "all_parts_faithful"}:
        raise ValueError("faithfulness judge response fields are invalid")
    if type(payload["all_parts_faithful"]) is not bool:
        raise ValueError("faithfulness all_parts_faithful is invalid")
    values = payload["part_results"]
    if not isinstance(values, list) or len(values) != len(answer.parts):
        raise ValueError("faithfulness part_results are invalid")
    results: list[FaithfulnessPart] = []
    for answer_part, item in zip(answer.parts, values, strict=True):
        if not isinstance(item, dict) or set(item) != {
            "part_id",
            "support",
            "entailed_evidence_refs",
            "citation_complete",
        }:
            raise ValueError("faithfulness part result fields are invalid")
        if item["part_id"] != answer_part.part_id or item["support"] not in {
            "fully_supported",
            "partially_supported",
            "unsupported",
            "contradicted",
        }:
            raise ValueError("faithfulness part result is invalid")
        entailed = item["entailed_evidence_refs"]
        if not isinstance(entailed, list) or any(
            not isinstance(ref, str) or ref not in answer_part.evidence_refs
            for ref in entailed
        ):
            raise ValueError("faithfulness entailed refs are invalid")
        if len(set(entailed)) != len(entailed) or type(item["citation_complete"]) is not bool:
            raise ValueError("faithfulness citation result is invalid")
        support = item["support"]
        citation_complete = item["citation_complete"]
        if answer.abstained:
            if support != "fully_supported" or entailed or citation_complete is not True:
                raise ValueError("faithfulness abstention result is inconsistent")
        elif support == "fully_supported":
            if not entailed or citation_complete is not True:
                raise ValueError("faithfulness full-support result is inconsistent")
        elif support == "partially_supported":
            if not entailed or citation_complete is not False:
                raise ValueError("faithfulness partial-support result is inconsistent")
        elif entailed or citation_complete is not False:
            raise ValueError("faithfulness unsupported result is inconsistent")
        results.append(
            FaithfulnessPart(
                part_id=answer_part.part_id,
                support=support,
                entailed_evidence_refs=tuple(entailed),
                citation_complete=citation_complete,
            )
        )
    all_faithful = all(
        item.support == "fully_supported" and item.citation_complete for item in results
    )
    if payload["all_parts_faithful"] is not all_faithful:
        raise ValueError("faithfulness aggregate disagrees with part results")
    return FaithfulnessPrediction(
        part_results=tuple(results),
        all_parts_faithful=all_faithful,
    )


def parse_correctness_prediction(raw: object) -> CorrectnessPrediction:
    payload = _json_object(raw, label="correctness judge")
    expected = {
        "answer_correct",
        "harmful_extra",
        "forget_leakage",
        "over_forget",
        "stale_value",
        "reflection_precision",
        "reflection_recall",
        "trajectory_order",
        "final_state",
        "intermediate_state",
        "reason_codes",
    }
    if set(payload) != expected:
        raise ValueError("correctness judge response fields are invalid")
    for field in ("answer_correct", "harmful_extra"):
        if type(payload[field]) is not bool:
            raise ValueError(f"correctness judge {field} is invalid")
    for field in (
        "forget_leakage",
        "over_forget",
        "stale_value",
        "reflection_precision",
        "reflection_recall",
        "trajectory_order",
        "final_state",
        "intermediate_state",
    ):
        if payload[field] is not None and type(payload[field]) is not bool:
            raise ValueError(f"correctness judge {field} is invalid")
    codes = payload["reason_codes"]
    if not isinstance(codes, list) or any(
        not isinstance(code, str) or not code.strip() or len(code) > 100 for code in codes
    ):
        raise ValueError("correctness judge reason_codes are invalid")
    return CorrectnessPrediction(
        answer_correct=payload["answer_correct"],
        harmful_extra=payload["harmful_extra"],
        forget_leakage=payload["forget_leakage"],
        over_forget=payload["over_forget"],
        stale_value=payload["stale_value"],
        reflection_precision=payload["reflection_precision"],
        reflection_recall=payload["reflection_recall"],
        trajectory_order=payload["trajectory_order"],
        final_state=payload["final_state"],
        intermediate_state=payload["intermediate_state"],
        reason_codes=tuple(code.strip() for code in codes),
    )


def _json_object(raw: object, *, label: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError(f"{label} response is not JSON") from None
    else:
        raise ValueError(f"{label} response type is invalid")
    if not isinstance(payload, dict):
        raise ValueError(f"{label} response must be an object")
    return payload


def configured_provider(cfg: Config, *, stage: str = "classifier") -> Provider:
    def call(system_prompt: str, user_prompt: str) -> object:
        response = llm_mod.call_llm(
            cfg,
            stage,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            json_mode=True,
        )
        return llm_mod.extract_text(response)

    return call


def evaluate_case(
    case: EvidenceAnswerCase,
    *,
    setting: retrieval.Setting,
    provider: Provider,
    candidate_top_k: int,
    max_selected: int,
    max_prompt_utf8_bytes: int,
) -> dict[str, Any]:
    candidates = retrieve_candidates(case, setting=setting, top_k=candidate_top_k)
    locations = (
        case.retrieval.adjacent_gold
        if setting == "adjacent_operation"
        else case.retrieval.longitudinal_gold
    )
    gold_turns = {
        (location.segment_index, location.turn_index)
        for location in locations
    }
    base = _base_outcome(
        case,
        setting=setting,
        candidates=candidates,
        gold_turns=gold_turns,
        max_selected=max_selected,
    )

    raw_distill: object | None = None
    distill_user = ""
    started = time.perf_counter_ns()
    try:
        distill_user = distill_prompt(case, candidates, max_selected=max_selected)
        _require_prompt_bound(
            DISTILL_SYSTEM_PROMPT,
            distill_user,
            max_bytes=max_prompt_utf8_bytes,
        )
        raw_distill = provider(DISTILL_SYSTEM_PROMPT, distill_user)
        distill_ms = _elapsed_ms(started)
        distill = parse_distill_prediction(
            raw_distill,
            candidate_refs={turn.ref for item in candidates for turn in item.turns},
            max_selected=max_selected,
        )
    except Exception as exc:  # noqa: BLE001 - evaluation records bounded failures.
        return _failed_outcome(
            base,
            phase="distill",
            exc=exc,
            started=started,
            prompt_bytes=(
                _prompt_bytes(DISTILL_SYSTEM_PROMPT, distill_user)
                if distill_user
                else None
            ),
            response=raw_distill,
        )

    selected_ref_set = set(distill.selected_evidence_refs)
    selected = tuple(
        turn
        for item in candidates
        for turn in item.turns
        if turn.ref in selected_ref_set
    )
    selection = _with_selection(
        base,
        selected=selected,
        gold_turns=gold_turns,
        distill=distill,
        distill_ms=distill_ms,
        prompt_bytes=_prompt_bytes(DISTILL_SYSTEM_PROMPT, distill_user),
        response=raw_distill,
    )
    raw_answer: object | None = None
    answer_user = ""
    started = time.perf_counter_ns()
    try:
        answer_user = answer_prompt(case, selected)
        _require_prompt_bound(
            ANSWER_SYSTEM_PROMPT,
            answer_user,
            max_bytes=max_prompt_utf8_bytes,
        )
        raw_answer = provider(ANSWER_SYSTEM_PROMPT, answer_user)
        answer_ms = _elapsed_ms(started)
        answer = parse_answer_prediction(raw_answer, selected_refs=selected_ref_set)
    except Exception as exc:  # noqa: BLE001 - evaluation records bounded failures.
        return _failed_outcome(
            selection,
            phase="answer",
            exc=exc,
            started=started,
            prompt_bytes=(
                _prompt_bytes(ANSWER_SYSTEM_PROMPT, answer_user) if answer_user else None
            ),
            response=raw_answer,
        )

    answered = _with_answer(
        selection,
        answer=answer,
        answer_ms=answer_ms,
        prompt_bytes=_prompt_bytes(ANSWER_SYSTEM_PROMPT, answer_user),
        response=raw_answer,
    )
    raw_faithfulness: list[object] = []
    faithfulness_user = ""
    faithfulness_prompt_sizes: list[int] = []
    started = time.perf_counter_ns()
    try:
        faithfulness_parts: list[FaithfulnessPart] = []
        for part in answer.parts:
            isolated_answer = AnswerPrediction(answer.abstained, (part,))
            faithfulness_user = faithfulness_prompt(case, isolated_answer, selected)
            _require_prompt_bound(
                FAITHFULNESS_SYSTEM_PROMPT,
                faithfulness_user,
                max_bytes=max_prompt_utf8_bytes,
            )
            response = provider(FAITHFULNESS_SYSTEM_PROMPT, faithfulness_user)
            raw_faithfulness.append(response)
            faithfulness_prompt_sizes.append(
                _prompt_bytes(FAITHFULNESS_SYSTEM_PROMPT, faithfulness_user)
            )
            prediction = parse_faithfulness_prediction(
                response,
                answer=isolated_answer,
            )
            faithfulness_parts.extend(prediction.part_results)
        faithfulness = FaithfulnessPrediction(
            part_results=tuple(faithfulness_parts),
            all_parts_faithful=all(
                item.support == "fully_supported" and item.citation_complete
                for item in faithfulness_parts
            ),
        )
        faithfulness_ms = _elapsed_ms(started)
    except Exception as exc:  # noqa: BLE001 - evaluation records bounded failures.
        return _failed_outcome(
            answered,
            phase="faithfulness_judge",
            exc=exc,
            started=started,
            prompt_bytes=(
                _prompt_bytes(FAITHFULNESS_SYSTEM_PROMPT, faithfulness_user)
                if faithfulness_user
                else None
            ),
            response=raw_faithfulness[-1] if raw_faithfulness else None,
        )

    faithful = _with_faithfulness(
        answered,
        faithfulness=faithfulness,
        latency_ms=faithfulness_ms,
        prompt_bytes=max(faithfulness_prompt_sizes),
        total_prompt_bytes=sum(faithfulness_prompt_sizes),
        response=raw_faithfulness,
    )
    raw_correctness: object | None = None
    correctness_user = ""
    started = time.perf_counter_ns()
    try:
        correctness_user = correctness_prompt(case, answer)
        _require_prompt_bound(
            CORRECTNESS_SYSTEM_PROMPT,
            correctness_user,
            max_bytes=max_prompt_utf8_bytes,
        )
        raw_correctness = provider(CORRECTNESS_SYSTEM_PROMPT, correctness_user)
        correctness_ms = _elapsed_ms(started)
        correctness = parse_correctness_prediction(raw_correctness)
        _validate_correctness_applicability(case, correctness)
    except Exception as exc:  # noqa: BLE001 - evaluation records bounded failures.
        return _failed_outcome(
            faithful,
            phase="correctness_judge",
            exc=exc,
            started=started,
            prompt_bytes=(
                _prompt_bytes(CORRECTNESS_SYSTEM_PROMPT, correctness_user)
                if correctness_user
                else None
            ),
            response=raw_correctness,
        )

    outcome = dict(faithful)
    outcome.update(
        pipeline_status="ok",
        correctness_judge={
            "answer_correct": correctness.answer_correct,
            "harmful_extra": correctness.harmful_extra,
            "forget_leakage": correctness.forget_leakage,
            "over_forget": correctness.over_forget,
            "stale_value": correctness.stale_value,
            "reflection_precision": correctness.reflection_precision,
            "reflection_recall": correctness.reflection_recall,
            "trajectory_order": correctness.trajectory_order,
            "final_state": correctness.final_state,
            "intermediate_state": correctness.intermediate_state,
            "reason_codes": list(correctness.reason_codes),
        },
        correctness_judge_latency_ms=correctness_ms,
        correctness_judge_prompt_utf8_bytes=_prompt_bytes(
            CORRECTNESS_SYSTEM_PROMPT,
            correctness_user,
        ),
        correctness_judge_response_sha256=_response_sha256(raw_correctness),
    )
    return outcome


def _base_outcome(
    case: EvidenceAnswerCase,
    *,
    setting: retrieval.Setting,
    candidates: Sequence[Candidate],
    gold_turns: set[tuple[int, int]],
    max_selected: int,
) -> dict[str, Any]:
    candidate_ids = [item.segment_id for item in candidates]
    candidate_turns = [turn for item in candidates for turn in item.turns]
    candidate_gold_refs = [
        turn.ref
        for turn in candidate_turns
        if (turn.segment_id, turn.turn_index) in gold_turns
    ]
    candidate_gold_segments = sorted(
        {
            turn.segment_id
            for turn in candidate_turns
            if (turn.segment_id, turn.turn_index) in gold_turns
        }
    )
    gold_segments = {segment_id for segment_id, _turn_index in gold_turns}
    candidate_chars = sum(len(turn.content) for turn in candidate_turns)
    first_gold_rank = next(
        (
            item.rank
            for item in candidates
            if any(
                (turn.segment_id, turn.turn_index) in gold_turns
                for turn in item.turns
            )
        ),
        None,
    )
    return {
        "source_file": case.retrieval.source_file,
        "question_pair_id": case.retrieval.question_pair_id,
        "setting": setting,
        "operation_type": case.retrieval.operation_type,
        "evaluation_type": case.retrieval.evaluation_type,
        "difficulty": case.retrieval.difficulty,
        "applicable_correctness_fields": sorted(
            _applicable_correctness_fields(case)
        ),
        "query_sha256": hashlib.sha256(case.retrieval.query.encode()).hexdigest(),
        "gold_turn_count": len(gold_turns),
        "gold_segment_ids": sorted(gold_segments),
        "candidate_segment_ids": candidate_ids,
        "candidate_turn_count": len(candidate_turns),
        "candidate_gold_turn_refs": candidate_gold_refs,
        "candidate_gold_segment_ids": candidate_gold_segments,
        "candidate_complete_recall": len(candidate_gold_refs) == len(gold_turns),
        "candidate_reciprocal_rank": (
            round(1 / first_gold_rank, 6) if first_gold_rank is not None else 0.0
        ),
        "candidate_context_chars": candidate_chars,
        "candidate_distractor_segment_ids": [
            item.segment_id for item in candidates if item.distractor
        ],
        "candidate_trace": [
            {
                "candidate_ref": f"R{item.rank:02d}",
                "rank": item.rank,
                "query_mode": item.query_mode,
                "turn_count": len(item.turns),
                "content_sha256": hashlib.sha256(
                    "\n".join(turn.content for turn in item.turns).encode()
                ).hexdigest(),
                "content_chars": sum(len(turn.content) for turn in item.turns),
            }
            for item in candidates
        ],
        "budgeted_gold_turn_target": min(max_selected, len(candidate_gold_refs)),
        "selection_status": None,
        "selected_evidence_refs": [],
        "selected_gold_turn_refs": [],
        "selected_segment_ids": [],
        "selected_gold_segment_ids": [],
        "selected_budgeted_complete": False,
        "selected_context_chars": 0,
        "selected_distractor_turn_refs": [],
        "cited_evidence_refs": [],
        "cited_gold_turn_refs": [],
        "answer_parts": [],
        "answer": None,
        "abstained": None,
        "faithfulness_judge": None,
        "correctness_judge": None,
    }


def _with_selection(
    outcome: dict[str, Any],
    *,
    selected: Sequence[CandidateTurn],
    gold_turns: set[tuple[int, int]],
    distill: DistillPrediction,
    distill_ms: int,
    prompt_bytes: int,
    response: object,
) -> dict[str, Any]:
    result = dict(outcome)
    selected_gold = [
        turn.ref
        for turn in selected
        if (turn.segment_id, turn.turn_index) in gold_turns
    ]
    selected_gold_segments = sorted(
        {
            turn.segment_id
            for turn in selected
            if (turn.segment_id, turn.turn_index) in gold_turns
        }
    )
    distractor_segments = set(outcome["candidate_distractor_segment_ids"])
    result.update(
        selection_status=distill.selection_status,
        selected_evidence_refs=[turn.ref for turn in selected],
        selected_gold_turn_refs=selected_gold,
        selected_segment_ids=sorted({turn.segment_id for turn in selected}),
        selected_gold_segment_ids=selected_gold_segments,
        selected_budgeted_complete=(
            outcome["budgeted_gold_turn_target"] > 0
            and len(selected_gold) == outcome["budgeted_gold_turn_target"]
        ),
        selected_context_chars=sum(len(turn.content) for turn in selected),
        selected_distractor_turn_refs=[
            turn.ref for turn in selected if turn.segment_id in distractor_segments
        ],
        distill_latency_ms=distill_ms,
        distill_prompt_utf8_bytes=prompt_bytes,
        distill_response_sha256=_response_sha256(response),
    )
    return result


def _with_answer(
    outcome: dict[str, Any],
    *,
    answer: AnswerPrediction,
    answer_ms: int,
    prompt_bytes: int,
    response: object,
) -> dict[str, Any]:
    result = dict(outcome)
    cited = list(
        dict.fromkeys(ref for part in answer.parts for ref in part.evidence_refs)
    )
    selected_gold_refs = set(outcome["selected_gold_turn_refs"])
    result.update(
        cited_evidence_refs=cited,
        cited_gold_turn_refs=[ref for ref in cited if ref in selected_gold_refs],
        answer_parts=[
            {
                "part_id": part.part_id,
                "text": part.text,
                "evidence_refs": list(part.evidence_refs),
            }
            for part in answer.parts
        ],
        answer=" ".join(part.text for part in answer.parts),
        abstained=answer.abstained,
        answer_latency_ms=answer_ms,
        answer_prompt_utf8_bytes=prompt_bytes,
        answer_response_sha256=_response_sha256(response),
    )
    return result


def _with_faithfulness(
    outcome: dict[str, Any],
    *,
    faithfulness: FaithfulnessPrediction,
    latency_ms: int,
    prompt_bytes: int,
    total_prompt_bytes: int,
    response: Sequence[object],
) -> dict[str, Any]:
    result = dict(outcome)
    result.update(
        faithfulness_judge={
            "part_results": [
                {
                    "part_id": part.part_id,
                    "support": part.support,
                    "entailed_evidence_refs": list(part.entailed_evidence_refs),
                    "citation_complete": part.citation_complete,
                }
                for part in faithfulness.part_results
            ],
            "all_parts_faithful": faithfulness.all_parts_faithful,
        },
        faithfulness_judge_latency_ms=latency_ms,
        faithfulness_judge_call_count=len(response),
        faithfulness_judge_prompt_utf8_bytes=prompt_bytes,
        faithfulness_judge_total_prompt_utf8_bytes=total_prompt_bytes,
        faithfulness_judge_response_sha256=_response_sha256(response),
    )
    return result


def _failed_outcome(
    outcome: dict[str, Any],
    *,
    phase: str,
    exc: Exception,
    started: int,
    prompt_bytes: int | None = None,
    response: object | None = None,
) -> dict[str, Any]:
    result = dict(outcome)
    result.update(
        pipeline_status=f"{phase}_error",
        error_type=type(exc).__name__,
        **{f"{phase}_latency_ms": _elapsed_ms(started)},
    )
    if prompt_bytes is not None:
        result[f"{phase}_prompt_utf8_bytes"] = prompt_bytes
    if response is not None:
        result[f"{phase}_response_sha256"] = _response_sha256(response)
    return result


def _elapsed_ms(started: int) -> int:
    return round((time.perf_counter_ns() - started) / 1_000_000)


def _prompt_bytes(system_prompt: str, user_prompt: str) -> int:
    return len((system_prompt + "\n" + user_prompt).encode("utf-8"))


def _require_prompt_bound(
    system_prompt: str,
    user_prompt: str,
    *,
    max_bytes: int,
) -> None:
    if _prompt_bytes(system_prompt, user_prompt) > max_bytes:
        raise ValueError("model prompt exceeds the frozen UTF-8 byte bound")


def _response_sha256(response: object) -> str:
    if isinstance(response, str):
        encoded = response.encode("utf-8")
    else:
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_correctness_applicability(
    case: EvidenceAnswerCase,
    prediction: CorrectnessPrediction,
) -> None:
    applicable_fields = _applicable_correctness_fields(case)
    values = {
        "forget_leakage": prediction.forget_leakage,
        "over_forget": prediction.over_forget,
        "stale_value": prediction.stale_value,
        "reflection_precision": prediction.reflection_precision,
        "reflection_recall": prediction.reflection_recall,
        "trajectory_order": prediction.trajectory_order,
        "final_state": prediction.final_state,
        "intermediate_state": prediction.intermediate_state,
    }
    for output_field, value in values.items():
        if (output_field in applicable_fields) != isinstance(value, bool):
            raise ValueError(
                f"correctness judge applicability is invalid: {output_field}"
            )


def _applicable_correctness_fields(case: EvidenceAnswerCase) -> set[str]:
    diagnostics = case.gold_spec.get("diagnostic_checks")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    mapping = {
        "forget_leakage": "leakage",
        "over_forget": "over_forget",
        "stale_value": "stale_value",
        "reflection_precision": "reflection_precision",
        "reflection_recall": "reflection_recall",
        "trajectory_order": "trajectory_order",
        "final_state": "final_state",
        "intermediate_state": "intermediate_state",
    }
    return {
        output_field
        for output_field, diagnostic_field in mapping.items()
        if diagnostics.get(diagnostic_field) is not None
    }


def metrics(outcomes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    count = len(outcomes)
    if not count:
        raise ValueError("evidence-answer evaluation has no outcomes")
    candidate_segment_count = sum(len(row["candidate_segment_ids"]) for row in outcomes)
    candidate_turn_count = sum(int(row["candidate_turn_count"]) for row in outcomes)
    selected_count = sum(len(row["selected_evidence_refs"]) for row in outcomes)
    citation_count = sum(len(row["cited_evidence_refs"]) for row in outcomes)
    gold_turn_count = sum(int(row["gold_turn_count"]) for row in outcomes)
    gold_segment_count = sum(len(row["gold_segment_ids"]) for row in outcomes)
    candidate_gold_turns = sum(len(row["candidate_gold_turn_refs"]) for row in outcomes)
    candidate_gold_segments = sum(
        len(row["candidate_gold_segment_ids"]) for row in outcomes
    )
    selected_gold_turns = sum(len(row["selected_gold_turn_refs"]) for row in outcomes)
    selected_gold_segments = sum(
        len(row["selected_gold_segment_ids"]) for row in outcomes
    )
    cited_gold_turns = sum(len(row["cited_gold_turn_refs"]) for row in outcomes)
    candidate_chars = sum(int(row["candidate_context_chars"]) for row in outcomes)
    selected_chars = sum(int(row["selected_context_chars"]) for row in outcomes)
    correctness = [
        row["correctness_judge"]
        for row in outcomes
        if isinstance(row.get("correctness_judge"), dict)
    ]
    faithfulness = [
        row["faithfulness_judge"]
        for row in outcomes
        if isinstance(row.get("faithfulness_judge"), dict)
    ]
    faith_parts = [
        part
        for judge in faithfulness
        for part in judge["part_results"]
        if isinstance(part, dict)
    ]
    entailed_citations = sum(len(part["entailed_evidence_refs"]) for part in faith_parts)
    part_citations = sum(
        len(part["evidence_refs"])
        for row in outcomes
        for part in row["answer_parts"]
    )
    forget_rows = [row for row in outcomes if row["operation_type"] == "Forget"]
    update_rows = [row for row in outcomes if row["operation_type"] == "Update"]
    reflect_rows = [row for row in outcomes if row["operation_type"] == "Reflect"]
    selected_counts = sorted(len(row["selected_evidence_refs"]) for row in outcomes)
    return {
        "case_count": count,
        "pipeline_valid_rate": _ratio(
            sum(row.get("pipeline_status") == "ok" for row in outcomes),
            count,
        ),
        "candidate_turn_recall_macro_at_k": round(
            sum(
                len(row["candidate_gold_turn_refs"]) / row["gold_turn_count"]
                for row in outcomes
            )
            / count,
            6,
        ),
        "candidate_turn_recall_micro_at_k": _ratio(
            candidate_gold_turns,
            gold_turn_count,
        ),
        "candidate_segment_recall_macro_at_k": round(
            sum(
                len(row["candidate_gold_segment_ids"]) / len(row["gold_segment_ids"])
                for row in outcomes
            )
            / count,
            6,
        ),
        "candidate_segment_recall_micro_at_k": _ratio(
            candidate_gold_segments,
            gold_segment_count,
        ),
        "candidate_complete_case_recall_rate": _ratio(
            sum(bool(row["candidate_complete_recall"]) for row in outcomes),
            count,
        ),
        "candidate_mean_reciprocal_rank": round(
            sum(float(row["candidate_reciprocal_rank"]) for row in outcomes) / count,
            6,
        ),
        "selected_gold_turn_recall_macro": round(
            sum(
                len(row["selected_gold_turn_refs"]) / row["gold_turn_count"]
                for row in outcomes
            )
            / count,
            6,
        ),
        "selected_gold_turn_recall_micro": _ratio(
            selected_gold_turns,
            gold_turn_count,
        ),
        "selector_oracle_efficiency_macro": round(
            sum(_selector_efficiency(row) for row in outcomes) / count,
            6,
        ),
        "selector_evaluable_count": sum(
            int(row["budgeted_gold_turn_target"]) > 0 for row in outcomes
        ),
        "selected_gold_segment_recall_macro": round(
            sum(
                len(row["selected_gold_segment_ids"]) / len(row["gold_segment_ids"])
                for row in outcomes
            )
            / count,
            6,
        ),
        "selected_gold_segment_recall_micro": _ratio(
            selected_gold_segments,
            gold_segment_count,
        ),
        "selected_evidence_precision_micro": _ratio(
            selected_gold_turns,
            selected_count,
            empty=0.0,
        ),
        "selected_budgeted_complete_rate": _ratio(
            sum(bool(row["selected_budgeted_complete"]) for row in outcomes),
            count,
        ),
        "empty_selection_rate": _ratio(
            sum(not row["selected_evidence_refs"] for row in outcomes),
            count,
        ),
        "citation_gold_turn_recall_macro": round(
            sum(
                len(row["cited_gold_turn_refs"]) / row["gold_turn_count"]
                for row in outcomes
            )
            / count,
            6,
        ),
        "citation_gold_turn_recall_micro": _ratio(cited_gold_turns, gold_turn_count),
        "citation_gold_turn_precision_micro": _ratio(
            cited_gold_turns,
            citation_count,
            empty=0.0,
        ),
        "selected_evidence_utilization_rate": _ratio(
            citation_count,
            selected_count,
            empty=0.0,
        ),
        "answer_accuracy": _judge_rate(correctness, "answer_correct", denominator=count),
        "harmful_extra_rate": _judge_rate(
            correctness,
            "harmful_extra",
            denominator=count,
        ),
        "fully_supported_answer_part_rate": _ratio(
            sum(part.get("support") == "fully_supported" for part in faith_parts),
            len(faith_parts),
            empty=0.0,
        ),
        "fully_faithful_answer_rate": _judge_rate(
            faithfulness,
            "all_parts_faithful",
            denominator=count,
        ),
        "contradicted_answer_part_rate": _ratio(
            sum(part.get("support") == "contradicted" for part in faith_parts),
            len(faith_parts),
            empty=0.0,
        ),
        "citation_entailment_precision": _ratio(
            entailed_citations,
            part_citations,
            empty=0.0,
        ),
        "citation_completeness_rate": _ratio(
            sum(part.get("citation_complete") is True for part in faith_parts),
            len(faith_parts),
            empty=0.0,
        ),
        "forget_leakage_rate": _lifecycle_error_rate(
            forget_rows,
            "forget_leakage",
        ),
        "forget_leakage_applicable_count": _lifecycle_applicable_count(
            forget_rows,
            "forget_leakage",
        ),
        "over_forget_rate": _lifecycle_error_rate(forget_rows, "over_forget"),
        "over_forget_applicable_count": _lifecycle_applicable_count(
            forget_rows,
            "over_forget",
        ),
        "update_stale_value_rate": _lifecycle_error_rate(update_rows, "stale_value"),
        "update_stale_value_applicable_count": _lifecycle_applicable_count(
            update_rows,
            "stale_value",
        ),
        "reflect_precision_rate": _lifecycle_success_rate(
            reflect_rows,
            "reflection_precision",
        ),
        "reflect_precision_applicable_count": _lifecycle_applicable_count(
            reflect_rows,
            "reflection_precision",
        ),
        "reflect_recall_rate": _lifecycle_success_rate(
            reflect_rows,
            "reflection_recall",
        ),
        "reflect_recall_applicable_count": _lifecycle_applicable_count(
            reflect_rows,
            "reflection_recall",
        ),
        "candidate_injected_distractor_share": _ratio(
            sum(len(row["candidate_distractor_segment_ids"]) for row in outcomes),
            candidate_segment_count,
            empty=0.0,
        ),
        "selected_injected_distractor_turn_share": _ratio(
            sum(len(row["selected_distractor_turn_refs"]) for row in outcomes),
            selected_count,
            empty=0.0,
        ),
        "selected_injected_distractor_case_rate": _ratio(
            sum(bool(row["selected_distractor_turn_refs"]) for row in outcomes),
            count,
        ),
        "mean_candidate_segment_count": round(candidate_segment_count / count, 6),
        "mean_candidate_turn_count": round(candidate_turn_count / count, 6),
        "mean_selected_count": round(selected_count / count, 6),
        "p95_selected_count": selected_counts[max(0, (95 * count + 99) // 100 - 1)],
        "mean_candidate_context_chars": round(candidate_chars / count, 6),
        "mean_selected_context_chars": round(selected_chars / count, 6),
        "selected_context_reduction_ratio": round(
            1 - (selected_chars / candidate_chars),
            6,
        )
        if candidate_chars
        else 0.0,
        "mean_distill_latency_ms": _mean_present(outcomes, "distill_latency_ms"),
        "mean_answer_latency_ms": _mean_present(outcomes, "answer_latency_ms"),
        "mean_faithfulness_judge_latency_ms": _mean_present(
            outcomes,
            "faithfulness_judge_latency_ms",
        ),
        "mean_correctness_judge_latency_ms": _mean_present(
            outcomes,
            "correctness_judge_latency_ms",
        ),
        "max_prompt_utf8_bytes_observed": max(
            int(row.get(field, 0))
            for row in outcomes
            for field in (
                "distill_prompt_utf8_bytes",
                "answer_prompt_utf8_bytes",
                "faithfulness_judge_prompt_utf8_bytes",
                "correctness_judge_prompt_utf8_bytes",
            )
        ),
        "answer_accuracy_by_operation": {
            operation: _operation_answer_accuracy(outcomes, operation)
            for operation in ("Remember", "Forget", "Update", "Reflect", "TrajectoryOps")
        },
        "answer_accuracy_when_candidate_complete": _conditional_answer_accuracy(
            outcomes,
            candidate_complete=True,
        ),
        "answer_accuracy_when_candidate_incomplete": _conditional_answer_accuracy(
            outcomes,
            candidate_complete=False,
        ),
    }


def _ratio(numerator: int, denominator: int, *, empty: float = 1.0) -> float:
    return round(numerator / denominator, 6) if denominator else empty


def _judge_rate(
    judges: Sequence[dict[str, Any]],
    field: str,
    *,
    denominator: int,
) -> float:
    return _ratio(sum(judge.get(field) is True for judge in judges), denominator)


def _lifecycle_error_rate(rows: Sequence[dict[str, Any]], field: str) -> float:
    applicable = [row for row in rows if field in row["applicable_correctness_fields"]]
    if not applicable:
        return 0.0
    return _ratio(
        sum(
            isinstance(row.get("correctness_judge"), dict)
            and row["correctness_judge"].get(field) is True
            for row in applicable
        ),
        len(applicable),
    )


def _lifecycle_success_rate(rows: Sequence[dict[str, Any]], field: str) -> float:
    applicable = [row for row in rows if field in row["applicable_correctness_fields"]]
    if not applicable:
        return 1.0
    return _ratio(
        sum(
            isinstance(row.get("correctness_judge"), dict)
            and row["correctness_judge"].get(field) is True
            for row in applicable
        ),
        len(applicable),
    )


def _lifecycle_applicable_count(
    rows: Sequence[dict[str, Any]],
    field: str,
) -> int:
    return sum(field in row["applicable_correctness_fields"] for row in rows)


def _selector_efficiency(row: dict[str, Any]) -> float:
    target = int(row["budgeted_gold_turn_target"])
    if not target:
        return 0.0
    return len(row["selected_gold_turn_refs"]) / target


def _mean_present(rows: Sequence[dict[str, Any]], field: str) -> float | None:
    values = [row[field] for row in rows if isinstance(row.get(field), int)]
    return round(sum(values) / len(values), 6) if values else None


def _conditional_answer_accuracy(
    rows: Sequence[dict[str, Any]],
    *,
    candidate_complete: bool,
) -> float | None:
    selected = [
        row for row in rows if bool(row["candidate_complete_recall"]) is candidate_complete
    ]
    if not selected:
        return None
    return _ratio(
        sum(
            isinstance(row.get("correctness_judge"), dict)
            and row["correctness_judge"].get("answer_correct") is True
            for row in selected
        ),
        len(selected),
    )


def _operation_answer_accuracy(rows: Sequence[dict[str, Any]], operation: str) -> float:
    selected = [row for row in rows if row["operation_type"] == operation]
    return _ratio(
        sum(
            isinstance(row.get("correctness_judge"), dict)
            and row["correctness_judge"].get("answer_correct") is True
            for row in selected
        ),
        len(selected),
        empty=0.0,
    )


def _acquire_validation_lock(path: Path) -> None:
    resolved = path.resolve()
    if resolved in _VALIDATION_LOCKS:
        raise ValueError("validation ledger is already active in this process")
    lock_path = resolved.with_name(f"{resolved.name}.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise ValueError("validation ledger is locked by another process") from None
    _VALIDATION_LOCKS[resolved] = descriptor


def _release_validation_lock(path: Path) -> None:
    descriptor = _VALIDATION_LOCKS.pop(path.resolve(), None)
    if descriptor is None:
        return
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_initializing_sqlite(path: Path) -> None:
    for candidate in (
        path,
        path.with_name(f"{path.name}-journal"),
        path.with_name(f"{path.name}-shm"),
        path.with_name(f"{path.name}-wal"),
    ):
        candidate.unlink(missing_ok=True)


class _ValidationLedger:
    """One-shot, stage-caching ledger used only by the blind validation tier."""

    _SCHEMA = """
    CREATE TABLE validation_run (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        run_id TEXT NOT NULL,
        state TEXT NOT NULL,
        frozen_json TEXT NOT NULL,
        report_sha256 TEXT,
        report_json TEXT
    );
    CREATE TABLE validation_stage (
        case_id TEXT NOT NULL,
        setting TEXT NOT NULL,
        stage TEXT NOT NULL,
        status TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        response_json TEXT,
        response_sha256 TEXT,
        error_type TEXT,
        PRIMARY KEY (case_id, setting, stage)
    );
    """

    def __init__(self, path: Path, frozen: dict[str, Any], *, prepare: bool) -> None:
        self.path = path
        self.frozen = frozen
        encoded = json.dumps(
            frozen,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.run_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if prepare:
            path.parent.mkdir(parents=True, exist_ok=True)
            initializing = path.with_name(f".{path.name}.initializing")
            created_ledger = False
            try:
                _acquire_validation_lock(path)
                if path.exists():
                    raise FileExistsError("validation ledger already exists")
                _remove_initializing_sqlite(initializing)
                with sqlite3.connect(initializing, timeout=30) as conn:
                    conn.execute("PRAGMA synchronous=FULL")
                    conn.executescript(self._SCHEMA)
                    conn.execute(
                        "INSERT INTO validation_run VALUES "
                        "(1, ?, 'AUTHORIZED', ?, NULL, NULL)",
                        (self.run_id, encoded),
                    )
                descriptor = os.open(initializing, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.link(initializing, path)
                created_ledger = True
                _fsync_directory(path.parent)
                _remove_initializing_sqlite(initializing)
            except Exception:
                _release_validation_lock(path)
                if created_ledger:
                    path.unlink(missing_ok=True)
                _remove_initializing_sqlite(initializing)
                raise
        else:
            if not path.is_file():
                raise ValueError("validation ledger does not exist")
            try:
                _acquire_validation_lock(path)
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT run_id, state, frozen_json FROM validation_run "
                        "WHERE singleton=1"
                    ).fetchone()
                    if row is None or row[0] != self.run_id or row[2] != encoded:
                        raise ValueError("validation ledger frozen inputs changed")
                    if row[1] == "INVALID":
                        raise ValueError(
                            "validation ledger is invalid and cannot be resumed"
                        )
                    dispatched = conn.execute(
                        "SELECT COUNT(*) FROM validation_stage "
                        "WHERE status='DISPATCHED'"
                    ).fetchone()[0]
                    if dispatched:
                        conn.execute(
                            "UPDATE validation_run SET state='INVALID' WHERE singleton=1"
                        )
                        conn.commit()
                        raise ValueError(
                            "validation ledger contains an ambiguous dispatched model call"
                        )
            except Exception:
                _release_validation_lock(path)
                raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def state(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM validation_run WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise ValueError("validation ledger run is missing")
        return str(row[0])

    def start(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE validation_run SET state='RUNNING' "
                "WHERE singleton=1 AND state IN ('AUTHORIZED', 'RUNNING', 'REPORT_READY')"
            )

    def completed_report(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state, report_sha256, report_json FROM validation_run "
                "WHERE singleton=1"
            ).fetchone()
        if row is None or row[0] not in {"REPORT_READY", "COMPLETE"}:
            return None
        encoded = str(row[2])
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row[1]:
            raise ValueError("validation ledger report digest changed")
        payload = json.loads(encoded)
        if not isinstance(payload, dict):
            raise ValueError("validation ledger report is invalid")
        return payload

    def provider_for(
        self,
        *,
        case_id: str,
        setting: retrieval.Setting,
        provider: Provider,
    ) -> Provider:
        stages = {
            DISTILL_SYSTEM_PROMPT: "distill",
            ANSWER_SYSTEM_PROMPT: "answer",
            FAITHFULNESS_SYSTEM_PROMPT: "faithfulness_judge",
            CORRECTNESS_SYSTEM_PROMPT: "correctness_judge",
        }

        def call(system_prompt: str, user_prompt: str) -> object:
            stage = stages.get(system_prompt)
            if stage is None:
                raise ValueError("validation provider received an unknown stage")
            if stage == "faithfulness_judge":
                first_line = user_prompt.partition("\n")[0]
                prefix = "Answer part ID: "
                part_id = first_line.removeprefix(prefix)
                if not first_line.startswith(prefix) or not part_id.startswith("P"):
                    raise ValueError("validation faithfulness part ID is missing")
                stage = f"faithfulness_judge:{part_id}"
            request_sha = hashlib.sha256(
                (system_prompt + "\n" + user_prompt).encode("utf-8")
            ).hexdigest()
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status, request_sha256, response_json, error_type "
                    "FROM validation_stage WHERE case_id=? AND setting=? AND stage=?",
                    (case_id, setting, stage),
                ).fetchone()
                if row is not None:
                    if row[1] != request_sha:
                        raise ValueError("validation stage prompt changed")
                    if row[0] == "SUCCEEDED":
                        return _decode_ledger_response(str(row[2]))
                    if row[0] == "FAILED":
                        raise RuntimeError(
                            f"recorded validation provider failure: {row[3]}"
                        )
                    raise ValueError("validation stage has an ambiguous status")
                conn.execute(
                    "INSERT INTO validation_stage "
                    "(case_id, setting, stage, status, request_sha256) "
                    "VALUES (?, ?, ?, 'DISPATCHED', ?)",
                    (case_id, setting, stage, request_sha),
                )
            try:
                response = provider(system_prompt, user_prompt)
                encoded_response = _encode_ledger_response(response)
            except Exception as exc:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE validation_stage SET status='FAILED', error_type=? "
                        "WHERE case_id=? AND setting=? AND stage=? AND status='DISPATCHED'",
                        (type(exc).__name__, case_id, setting, stage),
                    )
                raise
            with self._connect() as conn:
                changed = conn.execute(
                    "UPDATE validation_stage SET status='SUCCEEDED', response_json=?, "
                    "response_sha256=? WHERE case_id=? AND setting=? AND stage=? "
                    "AND status='DISPATCHED'",
                    (
                        encoded_response,
                        _response_sha256(response),
                        case_id,
                        setting,
                        stage,
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError("validation stage completion was not recorded")
            return response

        return call

    def report_ready(self, encoded_report: str) -> None:
        digest = hashlib.sha256(encoded_report.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            dispatched = conn.execute(
                "SELECT COUNT(*) FROM validation_stage WHERE status='DISPATCHED'"
            ).fetchone()[0]
            if dispatched:
                conn.execute("UPDATE validation_run SET state='INVALID' WHERE singleton=1")
                conn.commit()
                raise ValueError("validation report cannot include an ambiguous model call")
            changed = conn.execute(
                "UPDATE validation_run SET state='REPORT_READY', report_sha256=?, "
                "report_json=? "
                "WHERE singleton=1 AND state='RUNNING'",
                (digest, encoded_report),
            ).rowcount
            if changed != 1:
                raise ValueError("validation ledger is not running")

    def complete(self, encoded_report: str) -> None:
        digest = hashlib.sha256(encoded_report.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state, report_sha256 FROM validation_run WHERE singleton=1"
            ).fetchone()
            if row != ("REPORT_READY", digest):
                raise ValueError("validation report differs from the ledger")
            conn.execute(
                "UPDATE validation_run SET state='COMPLETE' WHERE singleton=1"
            )


def _encode_ledger_response(response: object) -> str:
    if not isinstance(response, (str, dict)):
        raise ValueError("validation provider response type cannot be recorded")
    return json.dumps(
        {"kind": "string" if isinstance(response, str) else "object", "value": response},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_ledger_response(encoded: str) -> object:
    payload = json.loads(encoded)
    if not isinstance(payload, dict) or set(payload) != {"kind", "value"}:
        raise ValueError("validation ledger response is invalid")
    if payload["kind"] == "string" and isinstance(payload["value"], str):
        return payload["value"]
    if payload["kind"] == "object" and isinstance(payload["value"], dict):
        return payload["value"]
    raise ValueError("validation ledger response type is invalid")


def run_evaluation(
    *,
    manifest_path: Path,
    memops_root: Path,
    exclusion_paths: tuple[Path, ...],
    metric_contract_path: Path,
    repository_root: Path,
    provider: Provider,
    provider_identity: dict[str, Any],
    output_path: Path | None = None,
    validation_ledger_path: Path | None = None,
    prepare_validation: bool = False,
    resume_validation: bool = False,
) -> dict[str, Any]:
    manifest_bytes = manifest_path.read_bytes()
    manifest = memops50._load_json(manifest_bytes, label="MemOps manifest")
    if not isinstance(manifest, dict):
        raise ValueError("MemOps manifest is invalid")
    validation_tier = (
        manifest.get("tier_id") == "memops50-validation-adjacent-longitudinal-v1"
    )
    ignored_validation_artifacts: tuple[Path, ...] = ()
    if validation_tier:
        if prepare_validation == resume_validation:
            raise ValueError(
                "validation requires exactly one of prepare or resume validation"
            )
        canonical_ledger, canonical_output = _canonical_validation_artifact_paths(
            repository_root
        )
        if validation_ledger_path is None or validation_ledger_path.resolve() != (
            canonical_ledger
        ):
            raise ValueError("validation ledger path is not the canonical tier path")
        if output_path is None or output_path.resolve() != canonical_output:
            raise ValueError("validation output path is not the canonical tier path")
        ignored_validation_artifacts = (canonical_ledger, canonical_output)
    repository = _repository_state(
        repository_root,
        ignored_paths=ignored_validation_artifacts,
    )
    if repository["dirty"]:
        raise ValueError("evidence-answer evaluation requires a clean repository")
    contract_bytes = metric_contract_path.read_bytes()
    contract = memops50._load_json(contract_bytes, label="evidence-answer contract")
    _validate_contract(contract)
    if provider_identity != contract["model"]:
        raise ValueError("configured provider differs from evidence-answer contract")
    ledger: _ValidationLedger | None = None
    if validation_tier:
        assert validation_ledger_path is not None
        assert output_path is not None
        if prepare_validation:
            if output_path.exists():
                raise ValueError("validation output already exists")
            _require_pushed_head(repository_root)
        frozen = _validation_frozen_inputs(
            repository=repository,
            repository_root=repository_root,
            manifest_path=manifest_path,
            manifest_bytes=manifest_bytes,
            exclusion_paths=exclusion_paths,
            metric_contract_path=metric_contract_path,
            contract_bytes=contract_bytes,
            provider_identity=provider_identity,
            validation_ledger_path=validation_ledger_path,
            output_path=output_path,
        )
        ledger = _ValidationLedger(
            validation_ledger_path,
            frozen,
            prepare=prepare_validation,
        )
        completed_report = ledger.completed_report()
        if completed_report is not None:
            return completed_report
        ledger.start()
    elif any(
        (
            prepare_validation,
            resume_validation,
            validation_ledger_path is not None,
        )
    ):
        raise ValueError("validation ledger flags are limited to the validation tier")
    cases = load_cases(
        manifest_path=manifest_path,
        memops_root=memops_root,
        exclusion_paths=exclusion_paths,
    )
    pipeline = contract["pipeline"]
    outcomes: dict[retrieval.Setting, list[dict[str, Any] | None]] = {
        setting: [None] * len(cases) for setting in _SETTINGS
    }
    with ThreadPoolExecutor(max_workers=int(pipeline["workers"])) as executor:
        pending = {
            executor.submit(
                evaluate_case,
                case,
                setting=setting,
                provider=(
                    ledger.provider_for(
                        case_id=f"{case.retrieval.source_file}:{case.retrieval.question_pair_id}",
                        setting=setting,
                        provider=provider,
                    )
                    if ledger is not None
                    else provider
                ),
                candidate_top_k=int(pipeline["candidate_top_k"]),
                max_selected=int(pipeline["max_selected_evidence"]),
                max_prompt_utf8_bytes=int(pipeline["max_prompt_utf8_bytes"]),
            ): (setting, index)
            for setting in _SETTINGS
            for index, case in enumerate(cases)
        }
        for future in as_completed(pending):
            setting, index = pending[future]
            outcomes[setting][index] = future.result()
    completed = {
        setting: [row for row in rows if row is not None]
        for setting, rows in outcomes.items()
    }
    if any(len(rows) != len(cases) for rows in completed.values()):
        raise RuntimeError("evidence-answer evaluation lost a setting-specific case")
    aggregates = {setting: metrics(rows) for setting, rows in completed.items()}
    comparisons = _setting_comparisons(aggregates)
    gate_verdict = _gate_verdict(
        aggregates["longitudinal_operation"],
        comparisons,
        contract["gates"],
        repository_clean=True,
    )
    report = {
        "schema_version": 1,
        "evaluation_id": "memops50-evidence-distill-answer-v1",
        "repository": repository,
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "sqlite": sqlite3.sqlite_version,
        },
        "dataset": {
            "tier_id": manifest.get("tier_id"),
            "manifest_path": str(manifest_path.relative_to(repository_root)),
            "manifest_sha256": memops50.sha256_bytes(manifest_bytes),
            "upstream_commit": memops50.UPSTREAM_COMMIT,
            "logical_pair_count": len(cases),
            "row_count": sum(len(rows) for rows in completed.values()),
        },
        "metric_contract": {
            "path": str(metric_contract_path.relative_to(repository_root)),
            "sha256": memops50.sha256_bytes(contract_bytes),
        },
        "pipeline_contract": dict(pipeline),
        "provider": dict(provider_identity),
        "variants": {
            setting: {
                "metrics": aggregates[setting],
                "cases": completed[setting],
            }
            for setting in _SETTINGS
        },
        "comparisons": comparisons,
        "gate_verdict": gate_verdict,
    }
    if ledger is not None:
        ledger.report_ready(_encode_report(report))
    return report


def _validation_frozen_inputs(
    *,
    repository: dict[str, Any],
    repository_root: Path,
    manifest_path: Path,
    manifest_bytes: bytes,
    exclusion_paths: tuple[Path, ...],
    metric_contract_path: Path,
    contract_bytes: bytes,
    provider_identity: dict[str, Any],
    validation_ledger_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    if len(exclusion_paths) != 2:
        raise ValueError("validation requires exactly two frozen exclusion tiers")
    return {
        "repository_commit": repository["commit"],
        "manifest_path": str(manifest_path.relative_to(repository_root)),
        "manifest_sha256": memops50.sha256_bytes(manifest_bytes),
        "exclusions": [
            {
                "path": str(path.relative_to(repository_root)),
                "sha256": memops50.sha256_bytes(path.read_bytes()),
            }
            for path in exclusion_paths
        ],
        "metric_contract_path": str(metric_contract_path.relative_to(repository_root)),
        "metric_contract_sha256": memops50.sha256_bytes(contract_bytes),
        "evaluator_sha256": memops50.sha256_bytes(Path(__file__).read_bytes()),
        "runner_sha256": memops50.sha256_bytes(
            (repository_root / "scripts/run_memops50_evidence_answer.py").read_bytes()
        ),
        "provider": provider_identity,
        "validation_ledger_path": str(
            validation_ledger_path.relative_to(repository_root)
        ),
        "output_path": str(output_path.relative_to(repository_root)),
        "settings": list(_SETTINGS),
        "row_count": 100,
    }


def _canonical_validation_artifact_paths(
    repository_root: Path,
) -> tuple[Path, Path]:
    result_root = (
        repository_root.resolve()
        / "benchmarks"
        / "memops50-validation-v1"
        / "results"
    )
    return (
        result_root / "evidence-answer-v1.ledger.sqlite",
        result_root / "evidence-answer-v1.json",
    )


def _require_pushed_head(repository_root: Path) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "@{u}"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.stdout.strip() != _repository_state(repository_root)["commit"]:
        raise ValueError("validation requires HEAD to equal its pushed upstream")


def _setting_comparisons(
    aggregates: dict[retrieval.Setting, dict[str, Any]],
) -> dict[str, float]:
    adjacent = aggregates["adjacent_operation"]
    longitudinal = aggregates["longitudinal_operation"]
    return {
        "candidate_turn_recall_macro_drop": round(
            adjacent["candidate_turn_recall_macro_at_k"]
            - longitudinal["candidate_turn_recall_macro_at_k"],
            6,
        ),
        "candidate_complete_case_recall_rate_drop": round(
            adjacent["candidate_complete_case_recall_rate"]
            - longitudinal["candidate_complete_case_recall_rate"],
            6,
        ),
        "answer_accuracy_drop": round(
            adjacent["answer_accuracy"] - longitudinal["answer_accuracy"],
            6,
        ),
        "fully_faithful_answer_rate_drop": round(
            adjacent["fully_faithful_answer_rate"]
            - longitudinal["fully_faithful_answer_rate"],
            6,
        ),
    }


def _validate_contract(contract: object) -> None:
    if not isinstance(contract, dict) or set(contract) != {
        "schema_version",
        "tier_id",
        "pipeline",
        "model",
        "prompts",
        "gates",
    }:
        raise ValueError("evidence-answer contract envelope is invalid")
    if contract["schema_version"] != 1 or contract["tier_id"] != (
        "memops50-evidence-distill-answer-v1"
    ):
        raise ValueError("evidence-answer contract identity is invalid")
    pipeline = contract["pipeline"]
    expected_pipeline = {
        "settings",
        "ranker",
        "candidate_top_k",
        "max_selected_evidence",
        "distiller_tools",
        "answerer_receives_candidate_pool",
        "judge_receives_uncited_evidence",
        "max_prompt_utf8_bytes",
        "workers",
    }
    if not isinstance(pipeline, dict) or set(pipeline) != expected_pipeline:
        raise ValueError("evidence-answer pipeline contract is invalid")
    if (
        pipeline["settings"] != list(_SETTINGS)
        or pipeline["ranker"] != "production_activity_sqlite_fts5_bm25"
        or pipeline["candidate_top_k"] != 20
        or pipeline["max_selected_evidence"] != 5
        or pipeline["distiller_tools"] is not False
        or pipeline["answerer_receives_candidate_pool"] is not False
        or pipeline["judge_receives_uncited_evidence"] is not False
        or type(pipeline["max_prompt_utf8_bytes"]) is not int
        or not 100_000 <= pipeline["max_prompt_utf8_bytes"] <= 2_000_000
        or type(pipeline["workers"]) is not int
        or not 1 <= pipeline["workers"] <= 8
    ):
        raise ValueError("evidence-answer pipeline contract changed")
    model = contract["model"]
    if not isinstance(model, dict) or set(model) != {
        "stage",
        "provider",
        "model",
        "reasoning_effort",
        "temperature",
        "seed",
        "tools",
    }:
        raise ValueError("evidence-answer model contract is invalid")
    if model["temperature"] is not None or model["seed"] is not None or model["tools"] is not False:
        raise ValueError("evidence-answer model execution contract changed")
    prompts = contract["prompts"]
    expected_prompts = {
        "distiller_system_sha256": hashlib.sha256(
            DISTILL_SYSTEM_PROMPT.encode()
        ).hexdigest(),
        "answerer_system_sha256": hashlib.sha256(
            ANSWER_SYSTEM_PROMPT.encode()
        ).hexdigest(),
        "faithfulness_judge_system_sha256": hashlib.sha256(
            FAITHFULNESS_SYSTEM_PROMPT.encode()
        ).hexdigest(),
        "correctness_judge_system_sha256": hashlib.sha256(
            CORRECTNESS_SYSTEM_PROMPT.encode()
        ).hexdigest(),
        "user_prompt_templates_sha256": _user_prompt_templates_sha256(),
    }
    if prompts != expected_prompts:
        raise ValueError("evidence-answer system prompt digests changed")
    gates = contract["gates"]
    expected_gates = {
        "pipeline_valid_rate_min",
        "candidate_turn_recall_macro_at_k_min",
        "candidate_segment_recall_macro_at_k_min",
        "candidate_complete_case_recall_rate_min",
        "selector_oracle_efficiency_macro_min",
        "selected_gold_turn_recall_macro_min",
        "selected_gold_segment_recall_macro_min",
        "selected_evidence_precision_micro_min",
        "selected_budgeted_complete_rate_min",
        "empty_selection_rate_max",
        "selected_injected_distractor_turn_share_max",
        "answer_accuracy_min",
        "answer_accuracy_each_operation_min",
        "fully_supported_answer_part_rate_min",
        "fully_faithful_answer_rate_min",
        "citation_entailment_precision_min",
        "citation_completeness_rate_min",
        "harmful_extra_rate_max",
        "forget_leakage_rate_max",
        "forget_leakage_applicable_count_min",
        "over_forget_rate_max",
        "over_forget_applicable_count_min",
        "update_stale_value_rate_max",
        "update_stale_value_applicable_count_min",
        "reflect_precision_rate_min",
        "reflect_precision_applicable_count_min",
        "reflect_recall_rate_min",
        "reflect_recall_applicable_count_min",
        "selected_context_reduction_ratio_min",
        "answer_accuracy_drop_max",
        "repository_clean_required",
    }
    if not isinstance(gates, dict) or set(gates) != expected_gates:
        raise ValueError("evidence-answer gates are invalid")
    count_gates = {key for key in expected_gates if "_applicable_count_min" in key}
    for key in count_gates:
        value = gates[key]
        if type(value) is not int or not 1 <= value <= 50:
            raise ValueError(f"evidence-answer applicability gate is invalid: {key}")
    for key in expected_gates - {"repository_clean_required", *count_gates}:
        value = gates[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError(f"evidence-answer gate is invalid: {key}")
    if gates["repository_clean_required"] is not True:
        raise ValueError("evidence-answer clean-repository gate is required")


def _gate_verdict(
    aggregate: dict[str, Any],
    comparisons: dict[str, float],
    gates: dict[str, Any],
    *,
    repository_clean: bool,
) -> dict[str, Any]:
    observed = {**aggregate, **comparisons}
    scalar_gates = {
        key: value
        for key, value in gates.items()
        if key not in {"answer_accuracy_each_operation_min", "repository_clean_required"}
    }
    checks = {
        key.removesuffix("_min"): observed[key.removesuffix("_min")] >= threshold
        for key, threshold in scalar_gates.items()
        if key.endswith("_min")
    }
    checks.update(
        {
            key.removesuffix("_max"): observed[key.removesuffix("_max")] <= threshold
            for key, threshold in scalar_gates.items()
            if key.endswith("_max")
        }
    )
    checks["answer_accuracy_each_operation"] = all(
        value >= gates["answer_accuracy_each_operation_min"]
        for value in aggregate["answer_accuracy_by_operation"].values()
    )
    checks["repository_clean"] = repository_clean
    return {"passed": all(checks.values()), "checks": checks, "gates": gates}


def _repository_state(
    root: Path,
    *,
    ignored_paths: Sequence[Path] = (),
) -> dict[str, Any]:
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

    raw_status = git("status", "--short", "--untracked-files=all")
    ignored: set[str] = set()
    for path in ignored_paths:
        relative_path = path.resolve().relative_to(root.resolve())
        relative = str(relative_path)
        ignored.update(
            {
                relative,
                f"{relative}-journal",
                f"{relative}-shm",
                f"{relative}-wal",
                f"{relative}.lock",
                str(relative_path.with_name(f".{relative_path.name}.initializing")),
                str(
                    relative_path.with_name(
                        f".{relative_path.name}.initializing-journal"
                    )
                ),
                str(
                    relative_path.with_name(f".{relative_path.name}.initializing-shm")
                ),
                str(
                    relative_path.with_name(f".{relative_path.name}.initializing-wal")
                ),
                str(relative_path.with_name(f".{relative_path.name}.publishing")),
            }
        )
    status_lines = [
        line
        for line in raw_status.splitlines()
        if len(line) < 4 or line[3:] not in ignored
    ]
    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(status_lines),
        "status_lines": len(status_lines),
        "ignored_artifact_status_lines": len(raw_status.splitlines())
        - len(status_lines),
    }


def _encode_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_report(
    report: dict[str, Any],
    output: Path | None,
    *,
    exclusive: bool = False,
) -> str:
    encoded = _encode_report(report)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        if exclusive:
            _publish_validation_report(output, encoded)
        else:
            output.write_text(encoded, encoding="utf-8")
    return encoded


def _publish_validation_report(output: Path, encoded: str) -> None:
    if output.exists():
        if output.read_text(encoding="utf-8") != encoded:
            raise ValueError("validation output already exists with other bytes")
        return
    publishing = output.with_name(f".{output.name}.publishing")
    descriptor = os.open(
        publishing,
        os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(publishing, output)
        except FileExistsError:
            if output.read_text(encoding="utf-8") != encoded:
                raise ValueError(
                    "validation output appeared with other bytes"
                ) from None
        _fsync_directory(output.parent)
    finally:
        publishing.unlink(missing_ok=True)


def _complete_validation_ledger(path: Path, encoded_report: str) -> None:
    digest = hashlib.sha256(encoded_report.encode("utf-8")).hexdigest()
    try:
        with sqlite3.connect(path, timeout=30) as conn:
            row = conn.execute(
                "SELECT state, report_sha256, report_json FROM validation_run "
                "WHERE singleton=1"
            ).fetchone()
            if row is None or row[1] != digest or row[2] != encoded_report:
                raise ValueError("validation report differs from its ledger")
            if row[0] == "REPORT_READY":
                conn.execute("UPDATE validation_run SET state='COMPLETE' WHERE singleton=1")
            elif row[0] != "COMPLETE":
                raise ValueError("validation ledger is not ready to complete")
    finally:
        _release_validation_lock(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memops-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--exclusion", type=Path, action="append", default=[])
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path(
            "benchmarks/memops50-lifecycle-v1/json/evidence_answer_metric_contract.json"
        ),
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validation-ledger", type=Path)
    validation_mode = parser.add_mutually_exclusive_group()
    validation_mode.add_argument(
        "--authorize-validation-once",
        action="store_true",
        help="create a new one-shot blind-validation ledger",
    )
    validation_mode.add_argument(
        "--resume-validation",
        action="store_true",
        help="resume only stages not already recorded in the validation ledger",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    repository_root = Path.cwd().resolve()
    cfg = config_mod.load(args.config) if args.config else config_mod.load()
    model = cfg.model_for("classifier")
    identity = {
        "stage": "classifier",
        "provider": model.provider,
        "model": model.model,
        "reasoning_effort": model.reasoning_effort,
        "temperature": None,
        "seed": None,
        "tools": False,
    }
    report = run_evaluation(
        manifest_path=args.manifest.resolve(),
        memops_root=args.memops_root.expanduser().resolve(),
        exclusion_paths=tuple(path.resolve() for path in args.exclusion),
        metric_contract_path=args.contract.resolve(),
        repository_root=repository_root,
        provider=configured_provider(cfg),
        provider_identity=identity,
        output_path=args.output.resolve() if args.output else None,
        validation_ledger_path=(
            args.validation_ledger.resolve() if args.validation_ledger else None
        ),
        prepare_validation=args.authorize_validation_once,
        resume_validation=args.resume_validation,
    )
    validation_requested = args.authorize_validation_once or args.resume_validation
    encoded = write_report(report, args.output, exclusive=validation_requested)
    if validation_requested:
        if args.validation_ledger is None:
            raise ValueError("validation ledger path is required")
        _complete_validation_ledger(args.validation_ledger.resolve(), encoded)
    if not args.quiet:
        print(encoded, end="")
    return 0 if report["gate_verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
