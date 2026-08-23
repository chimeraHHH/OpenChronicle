from __future__ import annotations

from openchronicle.evaluation import memops50_retrieval as retrieval


def _payloads() -> tuple[dict, dict]:
    answer = {
        "question_pair_id": "p1",
        "evaluation_type": "OperationTrace",
        "difficulty": "hard",
        "question": "Which medicine is Mochi taking now?",
        "expected_answer": "Milbemax",
        "gold_provenance": [
            {"segment_index": 1, "turn_index": 1, "quote": "Milbemax"}
        ],
    }
    source_dialogues = [
        [
            {"role": "user", "content": "Mochi now takes Milbemax."},
            {"role": "assistant", "content": "I noted the medicine."},
        ],
        [
            {"role": "user", "content": "Mochi visits Greenfield Clinic."},
            {"role": "assistant", "content": "I noted the clinic."},
        ],
        [
            {"role": "user", "content": "Mochi likes quiet appointments."},
            {"role": "assistant", "content": "I noted that preference."},
        ],
    ]
    stage2 = {
        "operation_type": "Remember",
        "conversations": [
            {"segment_index": index, "dialogue": dialogue}
            for index, dialogue in enumerate(source_dialogues, start=1)
        ],
        "answer": [
            {**answer, "evaluation_setting": "adjacent_operation"},
            {**answer, "evaluation_setting": "longitudinal_operation"},
        ],
    }
    carrier_by_source = {1: 2, 2: 20, 3: 40}
    source_by_carrier = {carrier: source for source, carrier in carrier_by_source.items()}
    conversations = []
    for segment_index in range(1, 51):
        source_index = source_by_carrier.get(segment_index)
        if source_index is not None:
            dialogue = [
                {"role": "user", "content": f"Filler topic {segment_index}."},
                *(dict(turn) for turn in source_dialogues[source_index - 1]),
                {"role": "assistant", "content": "More filler."},
            ]
            conversations.append(
                {
                    "segment_index": segment_index,
                    "evidence_inserted": True,
                    "evidence_segment_index": source_index,
                    "insertion_index": 1,
                    "distractor_inserted": False,
                    "dialogue": dialogue,
                }
            )
        elif segment_index == 3:
            conversations.append(
                {
                    "segment_index": segment_index,
                    "evidence_inserted": False,
                    "distractor_inserted": True,
                    "dialogue": [
                        {"role": "user", "content": "Max takes Drontal medicine."},
                        {"role": "assistant", "content": "That is Max, not Mochi."},
                    ],
                }
            )
        else:
            conversations.append(
                {
                    "segment_index": segment_index,
                    "evidence_inserted": False,
                    "distractor_inserted": False,
                    "dialogue": [
                        {"role": "user", "content": f"Unrelated topic {segment_index}."},
                        {"role": "assistant", "content": "Unrelated response."},
                    ],
                }
            )
    stage4 = {
        "operation_type": "Remember",
        "conversations": conversations,
        "injection_metadata": {
            "insertions": [
                {
                    "conversation_index": carrier_by_source[source_index],
                    "evidence_segment_index": source_index,
                    "insertion_index": 1,
                }
                for source_index in range(1, 4)
            ]
        },
        "answer": [
            {**answer, "evaluation_setting": "adjacent_operation"},
            {**answer, "evaluation_setting": "longitudinal_operation"},
        ],
    }
    return stage2, stage4


def _case() -> retrieval.RetrievalCase:
    stage2, stage4 = _payloads()
    return retrieval._parse_case(
        stage2=stage2,
        stage4=stage4,
        source_file="C01_remember.json",
        question_pair_id="p1",
        operation_type="Remember",
        evaluation_type="OperationTrace",
        difficulty="hard",
    )


def test_maps_original_gold_turn_to_injected_carrier() -> None:
    case = _case()

    assert case.adjacent_gold == (retrieval.EvidenceLocation(1, 1),)
    assert case.longitudinal_gold == (retrieval.EvidenceLocation(2, 2),)
    assert case.gold_provenance_item_count == 1
    assert [unit.segment_index for unit in case.longitudinal_units if unit.distractor] == [3]


def test_production_activity_search_recovers_carrier_without_copying_text() -> None:
    outcome = retrieval._evaluate_case(
        _case(),
        setting="longitudinal_operation",
        top_k=2,
    )

    assert outcome["complete_recall"] is True
    assert outcome["provenance_turn_recall_at_k"] == 1.0
    assert 2 in outcome["retrieved_segments"]
    assert "query" not in outcome


def test_injection_mapping_fails_closed_on_changed_carrier() -> None:
    stage2, stage4 = _payloads()
    stage4["conversations"][1]["dialogue"][1]["content"] = "Mochi takes Drontal."

    try:
        retrieval._parse_case(
            stage2=stage2,
            stage4=stage4,
            source_file="C01_remember.json",
            question_pair_id="p1",
            operation_type="Remember",
            evaluation_type="OperationTrace",
            difficulty="hard",
        )
    except ValueError as exc:
        assert "carrier changed" in str(exc)
    else:
        raise AssertionError("changed injected evidence must be rejected")


def test_metrics_count_turns_and_complete_cases_separately() -> None:
    rows = [
        {
            "gold_provenance_turn_count": 2,
            "gold_provenance_segment_count": 2,
            "matched_gold_turn_count": 1,
            "matched_gold_segments": [1],
            "complete_recall": False,
            "reciprocal_rank": 0.5,
            "distractor_segments_retrieved": [3],
            "distractor_at_1": True,
            "retrieved_segments": [3, 1],
            "non_gold_hit_count": 1,
            "query_mode": "relaxed_or_after_zero_hits",
            "context_chars": 100,
            "evaluation_type": "OperationTrace",
        },
        {
            "gold_provenance_turn_count": 1,
            "gold_provenance_segment_count": 1,
            "matched_gold_turn_count": 1,
            "matched_gold_segments": [2],
            "complete_recall": True,
            "reciprocal_rank": 1.0,
            "distractor_segments_retrieved": [],
            "distractor_at_1": False,
            "retrieved_segments": [2],
            "non_gold_hit_count": 0,
            "query_mode": "strict_and",
            "context_chars": 50,
            "evaluation_type": "OperationTrace",
        },
    ]

    metrics = retrieval._metrics(rows)

    assert metrics["provenance_turn_recall_micro_at_k"] == 0.666667
    assert metrics["provenance_turn_recall_macro_at_k"] == 0.75
    assert metrics["complete_case_recall_rate"] == 0.5
    assert metrics["cases_with_injected_distractor_hit_rate"] == 0.5
    assert metrics["injected_distractor_contamination_at_k"] == 0.333333
