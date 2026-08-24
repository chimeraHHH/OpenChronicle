from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from openchronicle.provenance.models import EvidenceRef
from openchronicle.store import fts
from openchronicle.suggestions import store as suggestion_store
from openchronicle.suggestions.feedback import summarize_feedback, valid_feedback_reason


def _insert_outcome(
    conn,
    *,
    index: int,
    status: str,
    reason: str,
) -> None:
    detected = datetime(2026, 8, 20, 9, index, tzinfo=UTC)
    suggestion, created = suggestion_store.insert(
        conn,
        suggestion_id=f"feedback-{index}",
        idempotency_key=f"feedback-key-{index}",
        semantic_key=f"feedback-semantic-{index}",
        workflow="work_resumption",
        title="Review recent work",
        summary="A local activity boundary may be useful.",
        artifact={"schema_version": 1, "action_capability": "none"},
        evidence=[EvidenceRef(kind="timeline_block", id=f"feedback-source-{index}")],
        policy_digest="feedback-policy",
        capture_generation=0,
        score=0.9,
        detected_at=detected,
        expires_at=detected + timedelta(hours=1),
    )
    assert created
    suggestion_store.transition(
        conn,
        suggestion_id=suggestion.id,
        expected_version=suggestion.version,
        from_statuses=("ready",),
        to_status=status,
        reason=reason,
    )


def test_feedback_summary_is_content_free_and_reason_grouped(ac_root: Path) -> None:
    with fts.cursor() as conn:
        _insert_outcome(conn, index=1, status="accepted", reason="helpful")
        _insert_outcome(conn, index=2, status="dismissed", reason="wrong_timing")
        _insert_outcome(conn, index=3, status="dismissed", reason="wrong_timing")
        _insert_outcome(conn, index=4, status="dismissed", reason="already_resolved")
        _insert_outcome(conn, index=5, status="dismissed", reason="helpful")

        summary = summarize_feedback(conn)

    assert summary == {
        "schema_version": 1,
        "sample_limit": 1_000,
        "sample_size": 5,
        "total_available": 5,
        "truncated": False,
        "accepted": 1,
        "dismissed": 4,
        "acceptance_rate": 0.2,
        "window_start": "2026-08-20T09:01:00.000000+00:00",
        "window_end": "2026-08-20T09:05:00.000000+00:00",
        "dismissal_reasons": [
            {"reason": "wrong_timing", "count": 2},
            {"reason": "already_resolved", "count": 1},
            {"reason": "legacy_or_unspecified", "count": 1},
        ],
        "action_capability": "none",
    }
    assert "Review recent work" not in str(summary)


def test_feedback_reason_contract_separates_helpful_from_dismissal() -> None:
    assert valid_feedback_reason(status="accepted", reason="helpful")
    assert not valid_feedback_reason(status="accepted", reason="wrong_timing")
    assert valid_feedback_reason(status="dismissed", reason="not_relevant")
    assert valid_feedback_reason(status="dismissed", reason="other")
    assert not valid_feedback_reason(status="dismissed", reason="helpful")
