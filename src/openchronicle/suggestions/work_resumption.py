"""Evidence-bound Work Resumption opportunities after a verified activity gap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import Config
from ..provenance.models import EvidenceRef, timeline_block_digest
from ..resume_cues import store as resume_cue_store
from ..timeline import store as timeline_store
from .activity import CaptureActivityGate
from .service import (
    WORK_RESUMPTION_NEXT_STEP,
    SuggestionDecision,
    SuggestionKernel,
    SuggestionProposal,
    _validate_config,
)


@dataclass(frozen=True, slots=True)
class WorkResumptionBoundary:
    previous: timeline_store.TimelineBlock
    current: timeline_store.TimelineBlock
    gap: timedelta


def assess_work_resumption(
    blocks: list[timeline_store.TimelineBlock],
    cfg: Config,
    *,
    now: datetime,
) -> tuple[WorkResumptionBoundary | None, str]:
    """Evaluate only the deterministic gap heuristic, without publishing."""
    now = _aware(now)
    _validate_config(cfg)
    if len(blocks) < 2:
        return None, "insufficient_context"

    activation = timedelta(minutes=cfg.suggestions.work_resumption_activation_minutes)
    min_gap = timedelta(minutes=cfg.suggestions.work_resumption_min_gap_minutes)
    max_gap = timedelta(hours=cfg.suggestions.work_resumption_max_gap_hours)
    for index in range(len(blocks) - 1, 0, -1):
        current = blocks[index]
        previous = blocks[index - 1]
        current_age = now - timeline_store.as_instant(current.end_time)
        if current_age < timedelta(0) or current_age > activation:
            continue
        gap = timeline_store.as_instant(current.start_time) - timeline_store.as_instant(
            previous.end_time
        )
        if min_gap <= gap <= max_gap:
            if not _has_verified_text(previous) or not _has_verified_text(current):
                return None, "insufficient_content"
            return WorkResumptionBoundary(previous, current, gap), "opportunity"
    return None, "no_recent_interruption"


class WorkResumptionService:
    def __init__(self, conn, cfg: Config):
        self.conn = conn
        self.cfg = cfg
        self.kernel = SuggestionKernel(conn, cfg)

    def scan(
        self,
        *,
        now: datetime | None = None,
        activity_gate: CaptureActivityGate | None = None,
        now_tick: float | None = None,
    ) -> SuggestionDecision:
        now = _aware(now or datetime.now(UTC))
        _validate_config(self.cfg)
        if not self.cfg.suggestions.enabled:
            return SuggestionDecision(False, "disabled")
        if activity_gate is not None and (
            now_tick is None
            or not activity_gate.display_allowed(
                now_tick=now_tick,
                settle_seconds=self.cfg.suggestions.work_resumption_settle_seconds,
            )
        ):
            return SuggestionDecision(False, "active_or_unsettled")
        blocks = timeline_store.query_recent(self.conn, limit=64)
        boundary, reason = assess_work_resumption(blocks, self.cfg, now=now)
        if boundary is None:
            return SuggestionDecision(False, reason)

        previous, current, gap = boundary.previous, boundary.current, boundary.gap
        previous_ref = _block_ref(previous)
        current_ref = _block_ref(current)
        parked_cue = resume_cue_store.parked_before(self.conn, current.start_time)
        artifact = {
            "schema_version": 2 if parked_cue is not None else 1,
            "workflow": "work_resumption",
            "action_capability": "none",
            "interruption": {
                "previous_end": previous.end_time.isoformat(),
                "current_start": current.start_time.isoformat(),
                "gap_minutes": round(gap.total_seconds() / 60, 2),
            },
            "last_verified_state": {
                "untrusted_activity_quote": True,
                "entries": [str(value)[:500] for value in previous.entries[-5:]],
                "apps": [str(value)[:200] for value in previous.apps_used[:10]],
            },
            "resumption_signal": {
                "untrusted_activity_quote": True,
                "entries": [str(value)[:500] for value in current.entries[-3:]],
                "apps": [str(value)[:200] for value in current.apps_used[:10]],
            },
            "recommended_next_step": WORK_RESUMPTION_NEXT_STEP,
        }
        if parked_cue is not None:
            artifact["parked_cue"] = {
                "id": parked_cue.id,
                "task_label": parked_cue.task_label,
                "next_step": parked_cue.next_step,
                "parked_at": parked_cue.created_at,
                "user_authored": True,
            }
            artifact["recommended_next_step"] = parked_cue.next_step
        return self.kernel.emit(
            SuggestionProposal(
                semantic_key=(
                    f"work-resumption-cue:{parked_cue.id}"
                    if parked_cue is not None
                    else f"work-resumption:{previous.id}:{current.id}"
                ),
                workflow="work_resumption",
                title=(
                    f"Resume: {parked_cue.task_label}"
                    if parked_cue is not None
                    else "Resume your recent work"
                ),
                summary=(
                    "A user-authored cue was parked before this verified return. "
                    "Review the exact next step before continuing."
                    if parked_cue is not None
                    else "A verified activity gap was followed by new local activity. "
                    "Review the evidence-backed state before continuing."
                ),
                artifact=artifact,
                evidence=(
                    (previous_ref, current_ref, resume_cue_store.evidence_ref(parked_cue))
                    if parked_cue is not None
                    else (previous_ref, current_ref)
                ),
                score=1.0 if parked_cue is not None else 0.9,
                expires_at=now + timedelta(minutes=self.cfg.suggestions.expiry_minutes),
            ),
            now=now,
        )


def _block_ref(block: timeline_store.TimelineBlock) -> EvidenceRef:
    return EvidenceRef(
        kind="timeline_block",
        id=block.id,
        timestamp=block.start_time.isoformat(),
        content_hash=timeline_block_digest(
            start=block.start_time.isoformat(),
            end=block.end_time.isoformat(),
            entries=block.entries,
            apps=block.apps_used,
        ),
    )


def _has_verified_text(block: timeline_store.TimelineBlock) -> bool:
    return any(isinstance(value, str) and value.strip() for value in block.entries)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("work resumption time must be timezone-aware")
    return value.astimezone(UTC)
