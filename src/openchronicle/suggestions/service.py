"""Deterministic eligibility, budget, cooldown, and evidence gates."""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from ..config import Config
from ..local_time import local_timezone
from ..privacy import policy as privacy_policy
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, canonical_digest
from ..services.context import ContextService
from . import store

SUPPORTED_WORKFLOWS = {
    "work_resumption",
}
WORK_RESUMPTION_NEXT_STEP = (
    "Review the last verified state and choose what to continue. "
    "OpenChronicle has not executed any action."
)


@dataclass(frozen=True, slots=True)
class SuggestionProposal:
    semantic_key: str
    workflow: str
    title: str
    summary: str
    artifact: dict[str, Any]
    evidence: tuple[EvidenceRef, ...]
    score: float
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SuggestionDecision:
    emitted: bool
    reason: str
    suggestion: store.Suggestion | None = None


class SuggestionKernel:
    """Publish reviewable local artifacts without owning any action capability."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg
        store.ensure_schema(conn)

    def emit(
        self,
        proposal: SuggestionProposal,
        *,
        now: datetime | None = None,
    ) -> SuggestionDecision:
        now = _aware(now or datetime.now(UTC))
        _validate_config(self.cfg)
        if not self.cfg.suggestions.enabled:
            return SuggestionDecision(False, "disabled")
        _validate_proposal(proposal, now=now)
        evidence = list(proposal.evidence)
        current_policy = privacy_policy.stored_observation_policy_digest(self.cfg.capture)
        capture_generation = store.capture_generation(self.conn)
        artifact_hash = canonical_digest(proposal.artifact)
        source_hash = store.evidence_digest(evidence)
        proposal_hash = store.proposal_digest(
            semantic_key=proposal.semantic_key,
            workflow=proposal.workflow,
            title=proposal.title,
            summary=proposal.summary,
            artifact_digest=artifact_hash,
            evidence_digest=source_hash,
            policy_digest=current_policy,
            capture_generation=capture_generation,
            score=float(proposal.score),
            expires_at=_aware(proposal.expires_at).isoformat(timespec="microseconds"),
        )
        suggestion_id = f"sg-{proposal_hash[:32]}"

        with _atomic(self.conn, "suggestion_emit"):
            store.expire_due(self.conn, now=now)
            existing = store.get_by_idempotency_key(self.conn, proposal_hash)
            if existing is not None:
                if self._suggestion_allowed(existing, expected_evidence=evidence):
                    return SuggestionDecision(False, "replay", existing)
                self._expire_if_active(existing, "evidence_invalidated")
                return SuggestionDecision(False, "evidence_not_current")

            if not self._evidence_allowed(evidence):
                return SuggestionDecision(False, "evidence_not_current")
            if _in_quiet_hours(self.cfg, now):
                return SuggestionDecision(False, "quiet_hours")
            if float(proposal.score) < float(self.cfg.suggestions.min_score):
                return SuggestionDecision(False, "below_threshold")

            day_start, day_end = _local_day_bounds(now)
            if store.count_detected_between(
                self.conn,
                start_us=_instant_us(day_start),
                end_us=_instant_us(day_end),
            ) >= int(self.cfg.suggestions.daily_budget):
                return SuggestionDecision(False, "daily_budget")

            cooldown_start = now - timedelta(minutes=int(self.cfg.suggestions.cooldown_minutes))
            if store.has_semantic_since(
                self.conn,
                proposal.semantic_key,
                since_us=_instant_us(cooldown_start),
            ):
                return SuggestionDecision(False, "cooldown")

            created, inserted = store.insert(
                self.conn,
                suggestion_id=suggestion_id,
                idempotency_key=proposal_hash,
                semantic_key=proposal.semantic_key,
                workflow=proposal.workflow,
                title=proposal.title,
                summary=proposal.summary,
                artifact=proposal.artifact,
                evidence=evidence,
                policy_digest=current_policy,
                capture_generation=capture_generation,
                score=float(proposal.score),
                detected_at=now,
                expires_at=_aware(proposal.expires_at),
            )
            if not inserted:
                if self._suggestion_allowed(created, expected_evidence=evidence):
                    return SuggestionDecision(False, "replay", created)
                raise RuntimeError("concurrent suggestion replay changed evidence")
            return SuggestionDecision(True, "emitted", created)

    def list_visible(
        self,
        *,
        statuses: list[str] | None = None,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[store.Suggestion]:
        now = _aware(now or datetime.now(UTC))
        _validate_config(self.cfg)
        if not self.cfg.suggestions.enabled:
            return []
        with _atomic(self.conn, "suggestion_list"):
            store.expire_due(self.conn, now=now)
            rows = store.list_suggestions(
                self.conn,
                statuses=statuses,
                limit=limit,
            )
            visible: list[store.Suggestion] = []
            for row in rows:
                if self._suggestion_allowed(row):
                    visible.append(row)
                    continue
                self._expire_if_active(row, "evidence_invalidated")
            return visible

    def transition(
        self,
        suggestion_id: str,
        *,
        expected_version: int,
        to_status: str,
        reason: str = "",
        now: datetime | None = None,
    ) -> store.Suggestion:
        now = _aware(now or datetime.now(UTC))
        _validate_config(self.cfg)
        if not self.cfg.suggestions.enabled:
            raise store.SuggestionConflict("suggestions are disabled")
        if to_status not in {"viewed", "accepted", "dismissed"}:
            raise ValueError("unsupported user suggestion transition")
        if not isinstance(reason, str) or len(reason) > 1_000:
            raise ValueError("invalid suggestion feedback")
        with _atomic(self.conn, "suggestion_transition"):
            current = store.get(self.conn, suggestion_id)
            if current is None:
                raise store.SuggestionConflict("suggestion is missing or changed")
            if _aware(datetime.fromisoformat(current.expires_at)) <= now:
                self._expire_if_active(current, "expired")
                raise store.SuggestionConflict("suggestion expired")
            if not self._suggestion_allowed(current):
                self._expire_if_active(current, "evidence_invalidated")
                raise store.SuggestionConflict("suggestion evidence changed")
            allowed_from = {
                "viewed": ("ready",),
                "accepted": ("ready", "viewed"),
                "dismissed": ("ready", "viewed"),
            }[to_status]
            return store.transition(
                self.conn,
                suggestion_id=suggestion_id,
                expected_version=expected_version,
                from_statuses=allowed_from,
                to_status=to_status,
                reason=reason,
            )

    def _suggestion_allowed(
        self,
        suggestion: store.Suggestion,
        *,
        expected_evidence: list[EvidenceRef] | None = None,
    ) -> bool:
        if suggestion.policy_digest != privacy_policy.stored_observation_policy_digest(
            self.cfg.capture
        ):
            return False
        if suggestion.capture_generation != store.capture_generation(self.conn):
            return False
        sources = provenance_store.direct_sources_checked(
            self.conn,
            EvidenceRef(kind="suggestion", id=suggestion.id),
        )
        if not sources or store.evidence_digest(sources) != suggestion.evidence_digest:
            return False
        if expected_evidence is not None and store.evidence_digest(
            expected_evidence
        ) != store.evidence_digest(sources):
            return False
        return self._evidence_allowed(sources)

    def _evidence_allowed(self, evidence: list[EvidenceRef]) -> bool:
        context = ContextService(self.conn, self.cfg)
        return bool(evidence) and all(context.evidence_allowed(source) for source in evidence)

    def _expire_if_active(self, suggestion: store.Suggestion, reason: str) -> None:
        if suggestion.status not in store.ACTIVE_STATUSES:
            return
        with contextlib.suppress(store.SuggestionConflict):
            store.transition(
                self.conn,
                suggestion_id=suggestion.id,
                expected_version=suggestion.version,
                from_statuses=("ready", "viewed"),
                to_status="expired",
                reason=reason,
            )


@contextlib.contextmanager
def _atomic(conn: sqlite3.Connection, name: str):
    if conn.in_transaction:
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
            conn.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _validate_config(cfg: Config) -> None:
    value = cfg.suggestions
    if type(value.enabled) is not bool or type(value.quiet_hours_enabled) is not bool:
        raise ValueError("suggestion booleans must be strict")
    integer_ranges = {
        "scan_seconds": (10, 3_600),
        "daily_budget": (1, 20),
        "cooldown_minutes": (1, 10_080),
        "quiet_hours_start": (0, 23),
        "quiet_hours_end": (0, 23),
        "expiry_minutes": (5, 1_440),
        "work_resumption_min_gap_minutes": (1, 1_440),
        "work_resumption_max_gap_hours": (1, 168),
        "work_resumption_activation_minutes": (1, 120),
        "work_resumption_settle_seconds": (1, 300),
    }
    for field_name, (minimum, maximum) in integer_ranges.items():
        field_value = getattr(value, field_name)
        if type(field_value) is not int or not minimum <= field_value <= maximum:
            raise ValueError(f"suggestions.{field_name} is invalid")
    if (
        isinstance(value.min_score, bool)
        or not isinstance(value.min_score, (int, float))
        or not math.isfinite(float(value.min_score))
        or not 0.0 <= float(value.min_score) <= 1.0
    ):
        raise ValueError("suggestions.min_score is invalid")
    if value.work_resumption_max_gap_hours * 60 <= value.work_resumption_min_gap_minutes:
        raise ValueError("work resumption maximum gap must exceed its minimum")


def _validate_proposal(proposal: SuggestionProposal, *, now: datetime) -> None:
    try:
        artifact_size = len(
            json.dumps(
                proposal.artifact,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        raise ValueError("invalid suggestion proposal") from None
    if (
        not isinstance(proposal.semantic_key, str)
        or not proposal.semantic_key.strip()
        or len(proposal.semantic_key) > 256
        or "\x00" in proposal.semantic_key
        or proposal.workflow not in SUPPORTED_WORKFLOWS
        or not isinstance(proposal.title, str)
        or not proposal.title.strip()
        or len(proposal.title) > 160
        or "\x00" in proposal.title
        or not isinstance(proposal.summary, str)
        or not proposal.summary.strip()
        or len(proposal.summary) > 1_000
        or "\x00" in proposal.summary
        or not isinstance(proposal.artifact, dict)
        or artifact_size > 64 * 1_024
        or not isinstance(proposal.evidence, tuple)
        or not 1 <= len(proposal.evidence) <= 20
        or len(set(proposal.evidence)) != len(proposal.evidence)
        or isinstance(proposal.score, bool)
        or not isinstance(proposal.score, (int, float))
        or not math.isfinite(float(proposal.score))
        or not 0.0 <= float(proposal.score) <= 1.0
    ):
        raise ValueError("invalid suggestion proposal")
    _validate_work_resumption_artifact(proposal.artifact)
    expires = _aware(proposal.expires_at)
    if expires <= now or expires - now > timedelta(days=1):
        raise ValueError("suggestion expiry is invalid")


def _validate_work_resumption_artifact(artifact: dict[str, Any]) -> None:
    if set(artifact) != {
        "schema_version",
        "workflow",
        "action_capability",
        "interruption",
        "last_verified_state",
        "resumption_signal",
        "recommended_next_step",
    }:
        raise ValueError("invalid work resumption artifact")
    if (
        type(artifact["schema_version"]) is not int
        or artifact["schema_version"] != 1
        or artifact["workflow"] != "work_resumption"
        or artifact["action_capability"] != "none"
        or artifact["recommended_next_step"] != WORK_RESUMPTION_NEXT_STEP
    ):
        raise ValueError("invalid work resumption artifact")

    interruption = artifact["interruption"]
    if not isinstance(interruption, dict) or set(interruption) != {
        "previous_end",
        "current_start",
        "gap_minutes",
    }:
        raise ValueError("invalid work resumption interruption")
    gap = interruption["gap_minutes"]
    if (
        not isinstance(interruption["previous_end"], str)
        or not isinstance(interruption["current_start"], str)
        or isinstance(gap, bool)
        or not isinstance(gap, (int, float))
        or not math.isfinite(float(gap))
        or not 0 < float(gap) <= 10_080
    ):
        raise ValueError("invalid work resumption interruption")
    _aware(datetime.fromisoformat(interruption["previous_end"]))
    _aware(datetime.fromisoformat(interruption["current_start"]))

    _validate_untrusted_activity(artifact["last_verified_state"], max_entries=5)
    _validate_untrusted_activity(artifact["resumption_signal"], max_entries=3)


def _validate_untrusted_activity(value: object, *, max_entries: int) -> None:
    if not isinstance(value, dict) or set(value) != {
        "untrusted_activity_quote",
        "entries",
        "apps",
    }:
        raise ValueError("invalid untrusted activity artifact")
    entries = value["entries"]
    apps = value["apps"]
    if (
        value["untrusted_activity_quote"] is not True
        or not isinstance(entries, list)
        or len(entries) > max_entries
        or not all(isinstance(item, str) and len(item) <= 500 for item in entries)
        or not isinstance(apps, list)
        or len(apps) > 10
        or not all(isinstance(item, str) and len(item) <= 200 for item in apps)
    ):
        raise ValueError("invalid untrusted activity artifact")


def _in_quiet_hours(cfg: Config, now: datetime) -> bool:
    if not cfg.suggestions.quiet_hours_enabled:
        return False
    hour = now.astimezone(local_timezone()).hour
    start = cfg.suggestions.quiet_hours_start
    end = cfg.suggestions.quiet_hours_end
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _local_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    zone = local_timezone()
    local_day = now.astimezone(zone).date()
    start = datetime.combine(local_day, time.min, zone).astimezone(UTC)
    end = datetime.combine(local_day + timedelta(days=1), time.min, zone).astimezone(UTC)
    return start, end


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("suggestion timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _instant_us(value: datetime) -> int:
    instant = _aware(value)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = instant - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
