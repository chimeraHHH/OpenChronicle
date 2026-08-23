from __future__ import annotations

from openchronicle.evaluation import memops50_lifecycle as lifecycle
from openchronicle.evaluation.vida_memory_decisions import EvidenceTurn


def test_memops50_scenario_runs_reviewed_lifecycle_with_reaffirm_and_forget() -> None:
    evidence = tuple(
        EvidenceTurn(
            id=f"segment-1-turn-{index}",
            session_id="segment-1",
            role="user",
            content=content,
        )
        for index, content in enumerate(
            (
                "The original value is alpha.",
                "I reconfirmed alpha after considering beta.",
                "A second durable fact is gamma.",
                "Alpha and gamma reveal a stable pattern.",
                "Please forget the alpha fact.",
            ),
            start=1,
        )
    )
    scenario = lifecycle.Scenario(
        source_file="A01_trajectory_ops.json",
        operation_family="TrajectoryOps",
        evaluation_type="StateTrajectory",
        difficulty="hard",
        evidence=evidence,
        tentative_operation_count=0,
        operations=(
            lifecycle.LifecycleOperation(
                id="op1",
                type="remember",
                target_id="alpha",
                target_name="alpha fact",
                new_value="alpha",
                evidence_ids=("segment-1-turn-1",),
            ),
            lifecycle.LifecycleOperation(
                id="op2",
                type="update",
                target_id="alpha",
                target_name="alpha fact",
                new_value="alpha",
                evidence_ids=("segment-1-turn-2",),
            ),
            lifecycle.LifecycleOperation(
                id="op3",
                type="remember",
                target_id="gamma",
                target_name="gamma fact",
                new_value="gamma",
                evidence_ids=("segment-1-turn-3",),
            ),
            lifecycle.LifecycleOperation(
                id="op4",
                type="reflect",
                target_id="pattern",
                target_name="stable pattern",
                new_value="alpha and gamma recur",
                evidence_ids=("segment-1-turn-1", "segment-1-turn-4"),
            ),
            lifecycle.LifecycleOperation(
                id="op5",
                type="forget",
                target_id="alpha",
                target_name="alpha fact",
                new_value="",
                evidence_ids=("segment-1-turn-5",),
            ),
        ),
    )

    result = lifecycle._run_scenario(scenario)

    assert result["passed"] is True
    assert result["final_expected_target_count"] == 2
    assert result["final_expected_missing"] == []
    assert all(operation["passed"] for operation in result["operations"])
    forget = result["operations"][-1]
    assert forget["forgotten_current"] is False
    assert forget["forgotten_history_entry_count"] == 0


def test_memops50_parser_excludes_tentative_state_but_keeps_confirmation() -> None:
    payload = {
        "conversations": [
            {
                "segment_index": 1,
                "dialogue": [
                    {"role": "user", "content": "The setting is alpha."},
                    {"role": "user", "content": "It might become beta."},
                    {"role": "user", "content": "It remains alpha."},
                ],
            }
        ],
        "operations": [
            {
                "operation_id": "op1",
                "type": "remember",
                "validity": "confirmed",
                "target": {"target_id": "setting", "target_name": "setting"},
                "new_value": "alpha",
                "evidence_spans": [{"segment_index": 1, "turn_index": 1}],
            },
            {
                "operation_id": "op2",
                "type": "update",
                "validity": "tentative",
                "target": {"target_id": "setting", "target_name": "setting"},
                "new_value": "beta",
                "evidence_spans": [{"segment_index": 1, "turn_index": 2}],
            },
            {
                "operation_id": "op3",
                "type": "update",
                "validity": "confirmed",
                "target": {"target_id": "setting", "target_name": "setting"},
                "new_value": "alpha",
                "evidence_spans": [{"segment_index": 1, "turn_index": 3}],
            },
        ],
    }

    evidence, operations = lifecycle._parse_payload(payload, source_file="fixture.json")

    assert len(evidence) == 3
    assert [operation.id for operation in operations] == ["op1", "op3"]
    assert [operation.new_value for operation in operations] == ["alpha", "alpha"]
