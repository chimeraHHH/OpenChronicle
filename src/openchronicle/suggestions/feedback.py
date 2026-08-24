"""Content-free local aggregation of proactive suggestion outcomes."""

from __future__ import annotations

import sqlite3
from collections import Counter

from . import store

MAX_FEEDBACK_SAMPLE = 1_000
DISMISSAL_REASON_CODES = (
    "not_relevant",
    "wrong_timing",
    "already_resolved",
    "too_vague",
    "other",
)
ACCEPTED_REASON_CODE = "helpful"


def summarize_feedback(
    conn: sqlite3.Connection,
    *,
    limit: int = MAX_FEEDBACK_SAMPLE,
) -> dict[str, object]:
    """Summarize recent valid terminal outcomes without suggestion content."""
    if limit < 1 or limit > MAX_FEEDBACK_SAMPLE:
        raise ValueError("feedback sample limit must be in [1, 1000]")
    outcomes = store.list_suggestions(
        conn,
        statuses=["accepted", "dismissed"],
        limit=limit,
    )
    total_available = int(
        conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE status IN ('accepted','dismissed')"
        ).fetchone()[0]
    )
    accepted = sum(item.status == "accepted" for item in outcomes)
    dismissed = sum(item.status == "dismissed" for item in outcomes)
    reasons = Counter(
        _reason_bucket(item.feedback_reason) for item in outcomes if item.status == "dismissed"
    )
    timestamps = sorted(item.detected_at for item in outcomes)
    sample_size = len(outcomes)
    return {
        "schema_version": 1,
        "sample_limit": limit,
        "sample_size": sample_size,
        "total_available": total_available,
        "truncated": total_available > sample_size,
        "accepted": accepted,
        "dismissed": dismissed,
        "acceptance_rate": accepted / sample_size if sample_size else 0.0,
        "window_start": timestamps[0] if timestamps else "",
        "window_end": timestamps[-1] if timestamps else "",
        "dismissal_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(
                reasons.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ],
        "action_capability": "none",
    }


def valid_feedback_reason(*, status: str, reason: str) -> bool:
    if status == "accepted":
        return reason == ACCEPTED_REASON_CODE
    if status == "dismissed":
        return reason in DISMISSAL_REASON_CODES
    return False


def _reason_bucket(reason: str) -> str:
    return reason if reason in DISMISSAL_REASON_CODES else "legacy_or_unspecified"
