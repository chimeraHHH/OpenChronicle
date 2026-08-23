from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle.evaluation import memops50_evidence_answer as evaluation
from openchronicle.evaluation import memops50_retrieval as retrieval

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = (
    ROOT
    / "benchmarks"
    / "memops50-lifecycle-v1"
    / "json"
    / "evidence_answer_metric_contract.json"
)


def _case() -> evaluation.EvidenceAnswerCase:
    adjacent = (
        retrieval.RetrievalUnit(1, "The old value was red.", True, False),
        retrieval.RetrievalUnit(2, "The current value is blue.", True, False),
        retrieval.RetrievalUnit(3, "Unrelated local detail.", True, False),
    )
    longitudinal = (
        retrieval.RetrievalUnit(1, "A convincing but unrelated secret.", False, True),
        retrieval.RetrievalUnit(2, "The current value is blue.", True, False),
    )
    recall = retrieval.RetrievalCase(
        source_file="A01_update.json",
        question_pair_id="p1_operation_trace",
        operation_type="Update",
        evaluation_type="OperationTrace",
        difficulty="medium",
        query="What is the current value?",
        adjacent_units=adjacent,
        longitudinal_units=longitudinal,
        adjacent_gold=(retrieval.EvidenceLocation(2, 1),),
        longitudinal_gold=(retrieval.EvidenceLocation(2, 1),),
        gold_provenance_item_count=1,
    )
    return evaluation.EvidenceAnswerCase(
        retrieval=recall,
        gold_spec={
            "evaluation_type": "OperationTrace",
            "question": recall.query,
            "expected_answer": "The current value is blue.",
            "gold_memory_state": "blue is current; red is stale",
            "judge_rubric": {"must_include": ["blue"]},
            "diagnostic_checks": {"stale_value": "red is stale"},
        },
        correctness_gold={
            "target_fact": "GOLD_TARGET_CANARY",
            "operation_type": "Update",
            "difficulty_knobs": {"gold_knob": "GOLD_KNOB_CANARY"},
            "operations": [{"operation_id": "GOLD_OPERATION_CANARY"}],
        },
        adjacent_turns=tuple(
            (unit.segment_index, (evaluation.DialogueTurn(1, "user", unit.content),))
            for unit in adjacent
        ),
        longitudinal_turns=(
            (1, (evaluation.DialogueTurn(1, "user", longitudinal[0].content),)),
            (2, (evaluation.DialogueTurn(1, "user", longitudinal[1].content),)),
        ),
    )


def test_prediction_parsers_enforce_exact_evidence_boundaries() -> None:
    assert evaluation.parse_distill_prediction(
        '{"selection_status":"selected","selected_evidence_refs":["R02-T01"]}',
        candidate_refs={"R01-T01", "R02-T01"},
        max_selected=1,
    ).selected_evidence_refs == ("R02-T01",)
    with pytest.raises(ValueError, match="non-candidate turn"):
        evaluation.parse_distill_prediction(
            {"selection_status": "selected", "selected_evidence_refs": ["R03-T01"]},
            candidate_refs={"R01-T01", "R02-T01"},
            max_selected=1,
        )
    with pytest.raises(ValueError, match="non-selected"):
        evaluation.parse_answer_prediction(
            {
                "abstained": False,
                "answer_parts": [
                    {"part_id": "P1", "text": "blue", "evidence_refs": ["R01-T01"]}
                ],
            },
            selected_refs={"R02-T01"},
        )


def test_answer_prompt_contains_only_distilled_evidence() -> None:
    candidates = (
        evaluation.Candidate(
            1,
            1,
            (evaluation.CandidateTurn("R01-T01", 1, 1, "user", "unselected secret"),),
            True,
            "strict_and",
        ),
        evaluation.Candidate(
            2,
            2,
            (
                evaluation.CandidateTurn(
                    "R02-T01", 2, 1, "user", "selected current value"
                ),
            ),
            False,
            "strict_and",
        ),
    )

    prompt = evaluation.answer_prompt(_case(), candidates[1].turns)

    assert "selected current value" in prompt
    assert "unselected secret" not in prompt


def test_prompt_roles_isolate_gold_and_uncited_evidence() -> None:
    case = _case()
    cited = evaluation.CandidateTurn(
        "R01-T01", 1, 1, "user", "CITED_EVIDENCE_CANARY"
    )
    uncited = evaluation.CandidateTurn(
        "R01-T02", 1, 2, "assistant", "UNCITED_EVIDENCE_CANARY"
    )
    candidates = (
        evaluation.Candidate(1, 1, (cited, uncited), False, "strict_and"),
    )
    answer = evaluation.AnswerPrediction(
        False,
        (evaluation.AnswerPart("P1", "blue", (cited.ref,)),),
    )

    distill = evaluation.distill_prompt(case, candidates, max_selected=5)
    answerer = evaluation.answer_prompt(case, (cited, uncited))
    faithful = evaluation.faithfulness_prompt(case, answer, (cited, uncited))
    correctness = evaluation.correctness_prompt(case, answer)

    for prompt in (distill, answerer, faithful):
        assert "GOLD_TARGET_CANARY" not in prompt
        assert "GOLD_OPERATION_CANARY" not in prompt
        assert "GOLD_KNOB_CANARY" not in prompt
    assert "CITED_EVIDENCE_CANARY" in faithful
    assert "UNCITED_EVIDENCE_CANARY" not in faithful
    assert "GOLD_TARGET_CANARY" in correctness
    assert "GOLD_OPERATION_CANARY" in correctness
    assert "GOLD_KNOB_CANARY" in correctness
    assert "CITED_EVIDENCE_CANARY" not in correctness


def test_abstention_and_faithfulness_consistency_are_strict() -> None:
    with pytest.raises(ValueError, match="abstention shape"):
        evaluation.parse_answer_prediction(
            {
                "abstained": True,
                "answer_parts": [
                    {"part_id": "P1", "text": "blue", "evidence_refs": []}
                ],
            },
            selected_refs=set(),
        )
    answer = evaluation.AnswerPrediction(
        False,
        (evaluation.AnswerPart("P1", "blue", ("R01-T01",)),),
    )
    with pytest.raises(ValueError, match="full-support result"):
        evaluation.parse_faithfulness_prediction(
            {
                "part_results": [
                    {
                        "part_id": "P1",
                        "support": "fully_supported",
                        "entailed_evidence_refs": [],
                        "citation_complete": True,
                    }
                ],
                "all_parts_faithful": True,
            },
            answer=answer,
        )


def test_zero_candidate_gold_is_not_selector_success() -> None:
    outcome = evaluation._base_outcome(
        _case(),
        setting="longitudinal_operation",
        candidates=(),
        gold_turns={(2, 1)},
        max_selected=5,
    )
    selected = evaluation._with_selection(
        outcome,
        selected=(),
        gold_turns={(2, 1)},
        distill=evaluation.DistillPrediction("insufficient", ()),
        distill_ms=1,
        prompt_bytes=1,
        response={"selection_status": "insufficient", "selected_evidence_refs": []},
    )

    assert selected["budgeted_gold_turn_target"] == 0
    assert selected["selected_budgeted_complete"] is False
    assert evaluation._selector_efficiency(selected) == 0.0


def test_lifecycle_rates_use_only_applicable_rows() -> None:
    applicable = {
        "applicable_correctness_fields": ["forget_leakage", "reflection_recall"],
        "correctness_judge": {"forget_leakage": True, "reflection_recall": True},
    }
    not_applicable = {
        "applicable_correctness_fields": [],
        "correctness_judge": {"forget_leakage": None, "reflection_recall": None},
    }

    assert evaluation._lifecycle_error_rate(
        [applicable, not_applicable], "forget_leakage"
    ) == 1.0
    assert evaluation._lifecycle_success_rate(
        [applicable, not_applicable], "reflection_recall"
    ) == 1.0
    assert evaluation._lifecycle_applicable_count(
        [applicable, not_applicable], "forget_leakage"
    ) == 1


def test_validation_ledger_replays_success_without_second_call(tmp_path: Path) -> None:
    path = tmp_path / "validation.sqlite"
    stale_journal = tmp_path / ".validation.sqlite.initializing-journal"
    stale_journal.write_text("partial", encoding="utf-8")
    frozen = {"contract": "fixed", "rows": 100}
    ledger = evaluation._ValidationLedger(path, frozen, prepare=True)
    assert not stale_journal.exists()
    calls = 0

    def provider(_system: str, _prompt: str) -> object:
        nonlocal calls
        calls += 1
        return {"selection_status": "insufficient", "selected_evidence_refs": []}

    wrapped = ledger.provider_for(
        case_id="A01_update.json:p1_operation_trace",
        setting="adjacent_operation",
        provider=provider,
    )
    first = wrapped(evaluation.DISTILL_SYSTEM_PROMPT, "fixed request")
    second = wrapped(evaluation.DISTILL_SYSTEM_PROMPT, "fixed request")

    assert first == second
    assert calls == 1
    with pytest.raises(ValueError, match="already active"):
        evaluation._ValidationLedger(path, frozen, prepare=False)
    evaluation._release_validation_lock(path)


def test_validation_ledger_rejects_ambiguous_dispatched_call(tmp_path: Path) -> None:
    path = tmp_path / "validation.sqlite"
    frozen = {"contract": "fixed", "rows": 100}
    evaluation._ValidationLedger(path, frozen, prepare=True)
    with evaluation.sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO validation_stage "
            "(case_id, setting, stage, status, request_sha256) "
            "VALUES ('case', 'adjacent_operation', 'distill', 'DISPATCHED', 'sha')"
        )
    evaluation._release_validation_lock(path)

    with pytest.raises(ValueError, match="ambiguous dispatched"):
        evaluation._ValidationLedger(path, frozen, prepare=False)
    with evaluation.sqlite3.connect(path) as conn:
        state = conn.execute("SELECT state FROM validation_run").fetchone()[0]
    assert state == "INVALID"


def test_dirty_preflight_happens_before_provider_or_dataset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0

    def provider(_system: str, _prompt: str) -> object:
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(
        evaluation,
        "_repository_state",
        lambda _root, **_kwargs: {
            "commit": "abc",
            "dirty": True,
            "status_lines": 1,
            "ignored_artifact_status_lines": 0,
        },
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="clean repository"):
        evaluation.run_evaluation(
            manifest_path=manifest,
            memops_root=tmp_path,
            exclusion_paths=(),
            metric_contract_path=CONTRACT,
            repository_root=ROOT,
            provider=provider,
            provider_identity={},
        )
    assert calls == 0


def test_each_answer_part_gets_a_separate_faithfulness_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = (
        evaluation.Candidate(
            2,
            1,
            (
                evaluation.CandidateTurn("R01-T01", 2, 1, "user", "PART_ONE_CANARY"),
                evaluation.CandidateTurn("R01-T02", 2, 2, "user", "PART_TWO_CANARY"),
            ),
            False,
            "strict_and",
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "retrieve_candidates",
        lambda case, setting, top_k: candidates,
    )
    faith_prompts: list[str] = []

    def provider(system: str, prompt: str) -> object:
        if system == evaluation.DISTILL_SYSTEM_PROMPT:
            return {
                "selection_status": "selected",
                "selected_evidence_refs": ["R01-T01", "R01-T02"],
            }
        if system == evaluation.ANSWER_SYSTEM_PROMPT:
            return {
                "abstained": False,
                "answer_parts": [
                    {
                        "part_id": "P1",
                        "text": "one",
                        "evidence_refs": ["R01-T01"],
                    },
                    {
                        "part_id": "P2",
                        "text": "two",
                        "evidence_refs": ["R01-T02"],
                    },
                ],
            }
        if system == evaluation.FAITHFULNESS_SYSTEM_PROMPT:
            faith_prompts.append(prompt)
            part_id = prompt.partition("\n")[0].removeprefix("Answer part ID: ")
            ref = "R01-T01" if part_id == "P1" else "R01-T02"
            return {
                "part_results": [
                    {
                        "part_id": part_id,
                        "support": "fully_supported",
                        "entailed_evidence_refs": [ref],
                        "citation_complete": True,
                    }
                ],
                "all_parts_faithful": True,
            }
        return {
            "answer_correct": True,
            "harmful_extra": False,
            "forget_leakage": None,
            "over_forget": None,
            "stale_value": False,
            "reflection_precision": None,
            "reflection_recall": None,
            "trajectory_order": None,
            "final_state": None,
            "intermediate_state": None,
            "reason_codes": [],
        }

    outcome = evaluation.evaluate_case(
        _case(),
        setting="longitudinal_operation",
        provider=provider,
        candidate_top_k=20,
        max_selected=5,
        max_prompt_utf8_bytes=1_000_000,
    )

    assert outcome["pipeline_status"] == "ok"
    assert outcome["faithfulness_judge_call_count"] == 2
    assert len(faith_prompts) == 2
    assert "PART_ONE_CANARY" in faith_prompts[0]
    assert "PART_TWO_CANARY" not in faith_prompts[0]
    assert "PART_TWO_CANARY" in faith_prompts[1]
    assert "PART_ONE_CANARY" not in faith_prompts[1]


def test_lifecycle_applicability_counts_are_gated() -> None:
    gates = json.loads(CONTRACT.read_bytes())["gates"]
    observed: dict[str, object] = {}
    for key in gates:
        if key.endswith("_min") and key != "answer_accuracy_each_operation_min":
            observed[key.removesuffix("_min")] = 1.0
        elif key.endswith("_max"):
            observed[key.removesuffix("_max")] = 0.0
    observed["forget_leakage_applicable_count"] = 0
    observed["answer_accuracy_by_operation"] = {
        operation: 1.0
        for operation in ("Remember", "Forget", "Update", "Reflect", "TrajectoryOps")
    }

    verdict = evaluation._gate_verdict(
        observed,
        {},
        gates,
        repository_clean=True,
    )

    assert verdict["checks"]["forget_leakage_applicable_count"] is False
    assert verdict["passed"] is False


def test_validation_rejects_noncanonical_artifact_paths_before_provider(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"tier_id": "memops50-validation-adjacent-longitudinal-v1"}),
        encoding="utf-8",
    )
    calls = 0

    def provider(_system: str, _prompt: str) -> object:
        nonlocal calls
        calls += 1
        return {}

    with pytest.raises(ValueError, match="canonical tier path"):
        evaluation.run_evaluation(
            manifest_path=manifest,
            memops_root=tmp_path,
            exclusion_paths=(),
            metric_contract_path=CONTRACT,
            repository_root=ROOT,
            provider=provider,
            provider_identity={},
            output_path=tmp_path / "other-output.json",
            validation_ledger_path=tmp_path / "other-ledger.sqlite",
            prepare_validation=True,
        )
    assert calls == 0


def test_validation_report_publish_is_atomic_and_non_overwriting(
    tmp_path: Path,
) -> None:
    output = tmp_path / "report.json"
    publishing = tmp_path / ".report.json.publishing"
    publishing.write_text("partial", encoding="utf-8")

    evaluation._publish_validation_report(output, '{"ok":true}\n')

    assert output.read_text(encoding="utf-8") == '{"ok":true}\n'
    assert not publishing.exists()
    evaluation._publish_validation_report(output, '{"ok":true}\n')
    with pytest.raises(ValueError, match="other bytes"):
        evaluation._publish_validation_report(output, '{"ok":false}\n')


def test_evaluate_case_separates_distill_answer_and_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = (
        evaluation.Candidate(
            1,
            1,
            (evaluation.CandidateTurn("R01-T01", 1, 1, "user", "unselected secret"),),
            True,
            "strict_and",
        ),
        evaluation.Candidate(
            2,
            2,
            (
                evaluation.CandidateTurn(
                    "R02-T01", 2, 1, "user", "The current value is blue."
                ),
            ),
            False,
            "strict_and",
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "retrieve_candidates",
        lambda case, setting, top_k: candidates,
    )
    calls: list[tuple[str, str]] = []

    def provider(system: str, prompt: str) -> object:
        calls.append((system, prompt))
        if system == evaluation.DISTILL_SYSTEM_PROMPT:
            return {
                "selection_status": "selected",
                "selected_evidence_refs": ["R02-T01"],
            }
        if system == evaluation.ANSWER_SYSTEM_PROMPT:
            assert "unselected secret" not in prompt
            return {
                "abstained": False,
                "answer_parts": [
                    {
                        "part_id": "P1",
                        "text": "The current value is blue.",
                        "evidence_refs": ["R02-T01"],
                    }
                ],
            }
        if system == evaluation.FAITHFULNESS_SYSTEM_PROMPT:
            assert "expected_answer" not in prompt
            return {
                "part_results": [
                    {
                        "part_id": "P1",
                        "support": "fully_supported",
                        "entailed_evidence_refs": ["R02-T01"],
                        "citation_complete": True,
                    }
                ],
                "all_parts_faithful": True,
            }
        return {
            "answer_correct": True,
            "harmful_extra": False,
            "forget_leakage": None,
            "over_forget": None,
            "stale_value": False,
            "reflection_precision": None,
            "reflection_recall": None,
            "trajectory_order": None,
            "final_state": None,
            "intermediate_state": None,
            "reason_codes": [],
        }

    outcome = evaluation.evaluate_case(
        _case(),
        setting="longitudinal_operation",
        provider=provider,
        candidate_top_k=20,
        max_selected=5,
        max_prompt_utf8_bytes=1_000_000,
    )

    assert len(calls) == 4
    assert outcome["pipeline_status"] == "ok"
    assert outcome["candidate_segment_ids"] == [1, 2]
    assert outcome["selected_evidence_refs"] == ["R02-T01"]
    assert outcome["cited_evidence_refs"] == ["R02-T01"]
    assert outcome["selected_budgeted_complete"] is True
    assert outcome["correctness_judge"]["answer_correct"] is True
    assert all("unselected secret" not in call[1] for call in calls[1:])


def test_metrics_do_not_hide_pipeline_failures() -> None:
    ok = {
        "operation_type": "Update",
        "setting": "longitudinal_operation",
        "applicable_correctness_fields": ["stale_value"],
        "pipeline_status": "ok",
        "gold_turn_count": 1,
        "gold_segment_ids": [2],
        "candidate_segment_ids": [1, 2],
        "candidate_turn_count": 2,
        "candidate_gold_turn_refs": ["R02-T01"],
        "candidate_gold_segment_ids": [2],
        "candidate_complete_recall": True,
        "candidate_reciprocal_rank": 0.5,
        "candidate_context_chars": 100,
        "candidate_distractor_segment_ids": [1],
        "budgeted_gold_turn_target": 1,
        "selected_evidence_refs": ["R02-T01"],
        "selected_gold_turn_refs": ["R02-T01"],
        "selected_segment_ids": [2],
        "selected_gold_segment_ids": [2],
        "selected_budgeted_complete": True,
        "selected_context_chars": 40,
        "selected_distractor_turn_refs": [],
        "cited_evidence_refs": ["R02-T01"],
        "cited_gold_turn_refs": ["R02-T01"],
        "answer_parts": [
            {"part_id": "P1", "text": "blue", "evidence_refs": ["R02-T01"]}
        ],
        "faithfulness_judge": {
            "part_results": [
                {
                    "part_id": "P1",
                    "support": "fully_supported",
                    "entailed_evidence_refs": ["R02-T01"],
                    "citation_complete": True,
                }
            ],
            "all_parts_faithful": True,
        },
        "correctness_judge": {
            "answer_correct": True,
            "harmful_extra": False,
            "stale_value": False,
        },
        "distill_prompt_utf8_bytes": 100,
        "answer_prompt_utf8_bytes": 100,
        "faithfulness_judge_prompt_utf8_bytes": 100,
        "correctness_judge_prompt_utf8_bytes": 100,
    }
    failed = dict(ok)
    failed.update(
        pipeline_status="answer_error",
        selected_evidence_refs=[],
        selected_gold_turn_refs=[],
        selected_segment_ids=[],
        selected_gold_segment_ids=[],
        selected_budgeted_complete=False,
        selected_context_chars=0,
        cited_evidence_refs=[],
        cited_gold_turn_refs=[],
        answer_parts=[],
        faithfulness_judge=None,
        correctness_judge=None,
    )

    result = evaluation.metrics([ok, failed])

    assert result["pipeline_valid_rate"] == 0.5
    assert result["answer_accuracy"] == 0.5
    assert result["fully_faithful_answer_rate"] == 0.5
    assert result["selected_gold_turn_recall_macro"] == 0.5


def test_frozen_evidence_answer_contract_is_valid() -> None:
    contract = json.loads(CONTRACT.read_bytes())

    evaluation._validate_contract(contract)

    assert contract["pipeline"]["candidate_top_k"] == 20
    assert contract["pipeline"]["settings"] == [
        "adjacent_operation",
        "longitudinal_operation",
    ]
    assert contract["pipeline"]["max_selected_evidence"] == 5
    assert contract["pipeline"]["answerer_receives_candidate_pool"] is False
    assert contract["model"] == {
        "stage": "classifier",
        "provider": "codex_cli",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "none",
        "temperature": None,
        "seed": None,
        "tools": False,
    }
