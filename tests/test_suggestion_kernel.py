from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.capture import scheduler
from openchronicle.privacy import policy as privacy_policy
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import (
    EvidenceRef,
    observation_digest,
    timeline_block_digest,
)
from openchronicle.resume_cues import store as resume_cue_store
from openchronicle.services.evidence import EvidenceResolver
from openchronicle.store import fts
from openchronicle.suggestions import service as suggestion_service
from openchronicle.suggestions import store as suggestion_store
from openchronicle.suggestions.activity import CaptureActivityGate
from openchronicle.suggestions.service import SuggestionKernel, SuggestionProposal
from openchronicle.suggestions.work_resumption import WorkResumptionService
from openchronicle.timeline import store as timeline_store


def _config() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.suggestions.enabled = True
    cfg.suggestions.quiet_hours_enabled = False
    return cfg


def _live_block(
    conn,
    cfg: config_mod.Config,
    *,
    block_id: str,
    start: datetime,
    text: str,
) -> tuple[timeline_store.TimelineBlock, EvidenceRef]:
    timestamp = (start + timedelta(seconds=10)).isoformat()
    capture = {
        "timestamp": timestamp,
        "schema_version": 4,
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "title": "Suggestion fixture",
        },
        "focused_element": {
            "role": "AXTextArea",
            "value": text,
        },
        "visible_text": text,
        "url": "",
    }
    capture_path = scheduler._write_capture(capture)
    observation = EvidenceRef(
        kind="observation",
        id=str(capture["observation_id"]),
        path=capture_path.name,
        timestamp=timestamp,
        content_hash=observation_digest(capture),
    )
    block = timeline_store.TimelineBlock(
        id=block_id,
        start_time=start,
        end_time=start + timedelta(minutes=1),
        timezone="UTC",
        entries=[text],
        apps_used=["Editor"],
        capture_count=1,
    )
    timeline_store.insert(conn, block)
    provenance_store.replace_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id=block.id),
        sources=[observation],
    )
    current = timeline_store.get_by_id(conn, block.id)
    assert current is not None
    binding = (
        capture_path.name,
        observation.id,
        observation.content_hash,
        observation.timestamp,
    )
    timeline_store.record_capture_receipts(
        conn,
        bindings=[binding],
        window_start=current.start_time,
        window_end=current.end_time,
    )
    timeline_store.record_window_receipt(
        conn,
        timeline_store.make_window_receipt(
            window_start=current.start_time,
            window_end=current.end_time,
            bindings=[binding],
            policy_digest=privacy_policy.stored_observation_policy_digest(cfg.capture),
            outcome="block",
            block=current,
        ),
    )
    ref = EvidenceRef(
        kind="timeline_block",
        id=current.id,
        timestamp=current.start_time.isoformat(),
        content_hash=timeline_block_digest(
            start=current.start_time.isoformat(),
            end=current.end_time.isoformat(),
            entries=current.entries,
            apps=current.apps_used,
        ),
    )
    return current, ref


def _proposal(
    ref: EvidenceRef,
    *,
    now: datetime,
    semantic_key: str = "fixture:one",
    marker: str = "one",
    score: float = 0.9,
) -> SuggestionProposal:
    return SuggestionProposal(
        semantic_key=semantic_key,
        workflow="work_resumption",
        title="Review recent work",
        summary="A local evidence boundary may be worth reviewing.",
        artifact={
            "schema_version": 1,
            "workflow": "work_resumption",
            "action_capability": "none",
            "interruption": {
                "previous_end": (now - timedelta(minutes=30)).isoformat(),
                "current_start": (now - timedelta(minutes=1)).isoformat(),
                "gap_minutes": 29.0,
            },
            "last_verified_state": {
                "untrusted_activity_quote": True,
                "entries": [f"previous-{marker}"],
                "apps": ["Editor"],
            },
            "resumption_signal": {
                "untrusted_activity_quote": True,
                "entries": [f"current-{marker}"],
                "apps": ["Editor"],
            },
            "recommended_next_step": suggestion_service.WORK_RESUMPTION_NEXT_STEP,
        },
        evidence=(ref,),
        score=score,
        expires_at=now + timedelta(hours=1),
    )


def test_work_resumption_emits_one_evidence_bound_side_effect_free_card(
    ac_root: Path,
) -> None:
    cfg = _config()
    previous_start = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    current_start = datetime(2026, 8, 9, 10, 31, tzinfo=UTC)
    now = datetime(2026, 8, 9, 10, 33, tzinfo=UTC)
    with fts.cursor() as conn:
        previous, _previous_ref = _live_block(
            conn,
            cfg,
            block_id="tlb-suggestion-before-gap",
            start=previous_start,
            text="Untrusted previous activity",
        )
        current, _current_ref = _live_block(
            conn,
            cfg,
            block_id="tlb-suggestion-after-gap",
            start=current_start,
            text="Untrusted resumed activity",
        )

        first = WorkResumptionService(conn, cfg).scan(now=now)
        assert first.emitted and first.reason == "emitted"
        assert first.suggestion is not None
        assert first.suggestion.workflow == "work_resumption"
        assert first.suggestion.artifact["action_capability"] == "none"
        assert first.suggestion.artifact["last_verified_state"] == {
            "untrusted_activity_quote": True,
            "entries": ["Untrusted previous activity"],
            "apps": ["Editor"],
        }
        assert first.suggestion.artifact["recommended_next_step"].endswith(
            "has not executed any action."
        )
        sources = provenance_store.direct_sources(
            conn,
            EvidenceRef(kind="suggestion", id=first.suggestion.id),
        )
        assert [source.id for source in sources] == [previous.id, current.id]

        replay = WorkResumptionService(conn, cfg).scan(now=now)
        assert not replay.emitted and replay.reason == "replay"
        assert replay.suggestion == first.suggestion
        assert len(suggestion_store.list_suggestions(conn)) == 1


def test_user_parked_cue_is_exact_evidence_for_the_next_return(ac_root: Path) -> None:
    cfg = _config()
    previous_start = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    current_start = datetime(2026, 8, 23, 10, 31, tzinfo=UTC)
    now = datetime(2026, 8, 23, 10, 33, tzinfo=UTC)
    with fts.cursor() as conn:
        previous, _previous_ref = _live_block(
            conn,
            cfg,
            block_id="tlb-cue-before-gap",
            start=previous_start,
            text="State before the interruption",
        )
        current, current_ref = _live_block(
            conn,
            cfg,
            block_id="tlb-cue-after-gap",
            start=current_start,
            text="New activity after returning",
        )
        cue = resume_cue_store.create(
            conn,
            task_label="Migration guide",
            next_step="Run the example against an empty database.",
            now=previous_start + timedelta(minutes=5),
        )

        decision = WorkResumptionService(conn, cfg).scan(now=now)

        assert decision.emitted
        assert decision.suggestion is not None
        assert decision.suggestion.title == "Resume: Migration guide"
        assert decision.suggestion.score == 1.0
        assert decision.suggestion.artifact == {
            "schema_version": 2,
            "workflow": "work_resumption",
            "action_capability": "none",
            "interruption": {
                "previous_end": previous.end_time.isoformat(),
                "current_start": current.start_time.isoformat(),
                "gap_minutes": 30.0,
            },
            "last_verified_state": {
                "untrusted_activity_quote": True,
                "entries": ["State before the interruption"],
                "apps": ["Editor"],
            },
            "resumption_signal": {
                "untrusted_activity_quote": True,
                "entries": ["New activity after returning"],
                "apps": ["Editor"],
            },
            "recommended_next_step": "Run the example against an empty database.",
            "parked_cue": {
                "id": cue.id,
                "task_label": "Migration guide",
                "next_step": "Run the example against an empty database.",
                "parked_at": cue.created_at,
                "user_authored": True,
            },
        }
        sources = provenance_store.direct_sources(
            conn,
            EvidenceRef(kind="suggestion", id=decision.suggestion.id),
        )
        assert [(source.kind, source.id) for source in sources] == [
            ("timeline_block", previous.id),
            ("timeline_block", current.id),
            ("resume_cue", cue.id),
        ]
        resolved = EvidenceResolver(conn, cfg).resolve(resume_cue_store.evidence_ref(cue))
        assert resolved["status"] == "current"
        assert resolved["content"]["next_step"] == cue.next_step
        assert resolved["content"]["user_authored"] is True

        with pytest.raises(ValueError, match="evidence does not match"):
            SuggestionKernel(conn, cfg).emit(
                SuggestionProposal(
                    semantic_key="invalid-unbound-cue",
                    workflow="work_resumption",
                    title=decision.suggestion.title,
                    summary=decision.suggestion.summary,
                    artifact=decision.suggestion.artifact,
                    evidence=(current_ref,),
                    score=1.0,
                    expires_at=now + timedelta(hours=1),
                ),
                now=now,
            )

        resume_cue_store.transition(
            conn,
            cue_id=cue.id,
            expected_version=cue.version,
            to_status="resumed",
            now=now,
        )
        assert SuggestionKernel(conn, cfg).list_visible(now=now) == []
        expired = suggestion_store.get(conn, decision.suggestion.id)
        assert expired is not None
        assert expired.status == "expired"
        assert expired.feedback_reason == "evidence_invalidated"


def test_cue_parked_after_return_started_is_not_retroactively_attached(
    ac_root: Path,
) -> None:
    cfg = _config()
    previous_start = datetime(2026, 8, 23, 11, 0, tzinfo=UTC)
    current_start = datetime(2026, 8, 23, 11, 31, tzinfo=UTC)
    now = datetime(2026, 8, 23, 11, 33, tzinfo=UTC)
    with fts.cursor() as conn:
        _live_block(
            conn,
            cfg,
            block_id="tlb-late-cue-before-gap",
            start=previous_start,
            text="Work before leaving",
        )
        _live_block(
            conn,
            cfg,
            block_id="tlb-late-cue-after-gap",
            start=current_start,
            text="Work after returning",
        )
        resume_cue_store.create(
            conn,
            task_label="Too late",
            next_step="Do not attach this to an earlier return.",
            now=current_start + timedelta(minutes=1),
        )

        decision = WorkResumptionService(conn, cfg).scan(now=now)

        assert decision.suggestion is not None
        assert decision.suggestion.title == "Resume your recent work"
        assert decision.suggestion.artifact["schema_version"] == 1
        assert "parked_cue" not in decision.suggestion.artifact


def test_no_work_resumption_card_is_emitted_while_user_is_still_away(
    ac_root: Path,
) -> None:
    cfg = _config()
    start = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        _live_block(
            conn,
            cfg,
            block_id="tlb-only-before-gap",
            start=start,
            text="Activity before leaving",
        )
        decision = WorkResumptionService(conn, cfg).scan(now=start + timedelta(hours=1))
        assert not decision.emitted
        assert decision.reason == "insufficient_context"
        assert suggestion_store.list_suggestions(conn) == []


def test_work_resumption_abstains_without_verified_text_on_both_sides(
    ac_root: Path,
) -> None:
    cfg = _config()
    now = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        _live_block(
            conn,
            cfg,
            block_id="tlb-empty-before-gap",
            start=now - timedelta(minutes=35),
            text="",
        )
        _live_block(
            conn,
            cfg,
            block_id="tlb-empty-after-gap",
            start=now - timedelta(minutes=5),
            text="",
        )

        decision = WorkResumptionService(conn, cfg).scan(now=now)

        assert not decision.emitted
        assert decision.reason == "insufficient_content"
        assert suggestion_store.list_suggestions(conn) == []


def test_quiet_hours_threshold_budget_and_cooldown_are_deterministic(
    ac_root: Path,
    monkeypatch,
) -> None:
    cfg = _config()
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        _block, ref = _live_block(
            conn,
            cfg,
            block_id="tlb-suggestion-controls",
            start=now - timedelta(minutes=5),
            text="Control fixture",
        )
        kernel = SuggestionKernel(conn, cfg)
        monkeypatch.setattr(suggestion_service, "local_timezone", lambda: UTC)

        cfg.suggestions.quiet_hours_enabled = True
        assert kernel.emit(_proposal(ref, now=now), now=now).reason == "quiet_hours"

        cfg.suggestions.quiet_hours_enabled = False
        assert (
            kernel.emit(
                _proposal(ref, now=now, score=0.5),
                now=now,
            ).reason
            == "below_threshold"
        )
        first = kernel.emit(_proposal(ref, now=now), now=now)
        assert first.emitted

        cooldown = kernel.emit(
            _proposal(
                ref,
                now=now + timedelta(minutes=1),
                marker="changed",
            ),
            now=now + timedelta(minutes=1),
        )
        assert cooldown.reason == "cooldown"

        cfg.suggestions.daily_budget = 1
        budget = kernel.emit(
            _proposal(
                ref,
                now=now + timedelta(minutes=2),
                semantic_key="fixture:two",
            ),
            now=now + timedelta(minutes=2),
        )
        assert budget.reason == "daily_budget"


def test_policy_change_hides_and_expires_prepared_artifact(ac_root: Path) -> None:
    cfg = _config()
    now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        _block, ref = _live_block(
            conn,
            cfg,
            block_id="tlb-suggestion-policy",
            start=now - timedelta(minutes=5),
            text="Policy-sensitive activity",
        )
        kernel = SuggestionKernel(conn, cfg)
        emitted = kernel.emit(_proposal(ref, now=now), now=now)
        assert emitted.suggestion is not None
        assert kernel.list_visible(now=now) == [emitted.suggestion]

        cfg.capture.excluded_app_names = ["Editor"]

        assert kernel.list_visible(now=now) == []
        expired = suggestion_store.get(conn, emitted.suggestion.id)
        assert expired is not None
        assert expired.status == "expired"
        assert expired.feedback_reason == "evidence_invalidated"


def test_new_capture_generation_hides_and_expires_stale_card(ac_root: Path) -> None:
    cfg = _config()
    now = datetime(2026, 8, 9, 12, 30, tzinfo=UTC)
    with fts.cursor() as conn:
        _block, ref = _live_block(
            conn,
            cfg,
            block_id="tlb-suggestion-before-new-activity",
            start=now - timedelta(minutes=5),
            text="State before the user continued",
        )
        kernel = SuggestionKernel(conn, cfg)
        emitted = kernel.emit(_proposal(ref, now=now), now=now)
        assert emitted.suggestion is not None
        assert emitted.suggestion.capture_generation == suggestion_store.capture_generation(conn)

        _live_block(
            conn,
            cfg,
            block_id="tlb-suggestion-new-activity",
            start=now + timedelta(minutes=1),
            text="The user continued after the suggestion was prepared",
        )

        assert kernel.list_visible(now=now + timedelta(minutes=2)) == []
        expired = suggestion_store.get(conn, emitted.suggestion.id)
        assert expired is not None
        assert expired.status == "expired"
        assert expired.feedback_reason == "evidence_invalidated"


def test_user_transitions_are_cas_bound_and_do_not_execute_actions(
    ac_root: Path,
) -> None:
    cfg = _config()
    now = datetime(2026, 8, 9, 13, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        _block, ref = _live_block(
            conn,
            cfg,
            block_id="tlb-suggestion-transition",
            start=now - timedelta(minutes=5),
            text="Transition fixture",
        )
        kernel = SuggestionKernel(conn, cfg)
        emitted = kernel.emit(_proposal(ref, now=now), now=now)
        assert emitted.suggestion is not None
        viewed = kernel.transition(
            emitted.suggestion.id,
            expected_version=emitted.suggestion.version,
            to_status="viewed",
            now=now,
        )
        accepted = kernel.transition(
            viewed.id,
            expected_version=viewed.version,
            to_status="accepted",
            now=now,
        )
        assert accepted.status == "accepted"
        assert accepted.artifact["action_capability"] == "none"
        with pytest.raises(suggestion_store.SuggestionConflict):
            kernel.transition(
                accepted.id,
                expected_version=viewed.version,
                to_status="dismissed",
                now=now,
            )


def test_malformed_config_and_projection_fail_closed(ac_root: Path) -> None:
    cfg = _config()
    now = datetime(2026, 8, 9, 14, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        _block, ref = _live_block(
            conn,
            cfg,
            block_id="tlb-suggestion-malformed",
            start=now - timedelta(minutes=5),
            text="Malformed fixture",
        )
        cfg.suggestions.daily_budget = True  # type: ignore[assignment]
        with pytest.raises(ValueError, match="daily_budget"):
            SuggestionKernel(conn, cfg).emit(_proposal(ref, now=now), now=now)

        cfg.suggestions.daily_budget = 3
        emitted = SuggestionKernel(conn, cfg).emit(_proposal(ref, now=now), now=now)
        assert emitted.suggestion is not None
        conn.execute(
            "UPDATE suggestions SET artifact_json='{}' WHERE id=?",
            (emitted.suggestion.id,),
        )
        assert suggestion_store.get(conn, emitted.suggestion.id) is None
        assert SuggestionKernel(conn, cfg).list_visible(now=now) == []


def test_malformed_projection_cannot_bypass_semantic_cooldown(ac_root: Path) -> None:
    cfg = _config()
    now = datetime(2026, 8, 9, 15, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        _block, ref = _live_block(
            conn,
            cfg,
            block_id="tlb-suggestion-cooldown-tamper",
            start=now - timedelta(minutes=5),
            text="Cooldown fixture",
        )
        kernel = SuggestionKernel(conn, cfg)
        emitted = kernel.emit(_proposal(ref, now=now), now=now)
        assert emitted.suggestion is not None
        conn.execute(
            "UPDATE suggestions SET projection_digest='malformed' WHERE id=?",
            (emitted.suggestion.id,),
        )

        retry = kernel.emit(
            _proposal(ref, now=now + timedelta(minutes=1), marker="changed"),
            now=now + timedelta(minutes=1),
        )

        assert not retry.emitted
        assert retry.reason == "cooldown"


def test_work_resumption_validates_config_before_detector_arithmetic(
    ac_root: Path,
) -> None:
    cfg = _config()
    cfg.suggestions.work_resumption_activation_minutes = True  # type: ignore[assignment]
    with fts.cursor() as conn, pytest.raises(ValueError, match="activation_minutes"):
        WorkResumptionService(conn, cfg).scan(now=datetime(2026, 8, 9, 16, 0, tzinfo=UTC))


def test_capture_breakpoint_gate_fails_closed_and_resets_after_activity(
    ac_root: Path,
) -> None:
    now = datetime(2026, 8, 9, 16, 30, tzinfo=UTC)
    gate = CaptureActivityGate()
    assert not gate.display_allowed(now_tick=100.0, settle_seconds=20)

    event = {
        "timestamp": now.isoformat(),
        "bundle_id": "com.example.editor",
        "_persisted_monotonic_tick": 100.0,
    }
    gate.on_persisted_capture(event)
    assert not gate.display_allowed(now_tick=119.999, settle_seconds=20)
    assert gate.display_allowed(now_tick=120.0, settle_seconds=20)

    gate.on_persisted_capture(
        {
            **event,
            "timestamp": (now + timedelta(seconds=21)).isoformat(),
            "_persisted_monotonic_tick": 121.0,
        }
    )
    assert not gate.display_allowed(now_tick=121.0, settle_seconds=20)
    assert gate.display_allowed(now_tick=141.0, settle_seconds=20)

    with pytest.raises(ValueError, match="moved backwards"):
        gate.on_persisted_capture(
            {
                **event,
                "_persisted_monotonic_tick": 120.0,
            }
        )
    assert not gate.display_allowed(now_tick=200.0, settle_seconds=20)


def test_work_resumption_requires_capture_breakpoint_when_gate_is_present(
    ac_root: Path,
) -> None:
    cfg = _config()
    previous_start = datetime(2026, 8, 9, 17, 0, tzinfo=UTC)
    current_start = datetime(2026, 8, 9, 17, 31, tzinfo=UTC)
    now = datetime(2026, 8, 9, 17, 33, tzinfo=UTC)
    gate = CaptureActivityGate()
    with fts.cursor() as conn:
        _live_block(
            conn,
            cfg,
            block_id="tlb-breakpoint-before-gap",
            start=previous_start,
            text="Work before interruption",
        )
        _live_block(
            conn,
            cfg,
            block_id="tlb-breakpoint-after-gap",
            start=current_start,
            text="Work after interruption",
        )
        gate.on_persisted_capture(
            {
                "timestamp": now.isoformat(),
                "bundle_id": "com.example.editor",
                "_persisted_monotonic_tick": 500.0,
            }
        )

        active = WorkResumptionService(conn, cfg).scan(
            now=now,
            activity_gate=gate,
            now_tick=519.0,
        )
        assert not active.emitted
        assert active.reason == "active_or_unsettled"

        settled = WorkResumptionService(conn, cfg).scan(
            now=now,
            activity_gate=gate,
            now_tick=520.0,
        )
        assert settled.emitted
        assert settled.reason == "emitted"


def test_malformed_breakpoint_config_fails_closed(ac_root: Path) -> None:
    cfg = _config()
    cfg.suggestions.work_resumption_settle_seconds = True  # type: ignore[assignment]
    with fts.cursor() as conn, pytest.raises(ValueError, match="settle_seconds"):
        WorkResumptionService(conn, cfg).scan(now=datetime(2026, 8, 9, 18, 0, tzinfo=UTC))


def test_kernel_rejects_any_action_capability_or_unknown_artifact_field(
    ac_root: Path,
) -> None:
    cfg = _config()
    now = datetime(2026, 8, 9, 17, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        _block, ref = _live_block(
            conn,
            cfg,
            block_id="tlb-suggestion-action-boundary",
            start=now - timedelta(minutes=5),
            text="Action boundary fixture",
        )
        proposal = _proposal(ref, now=now)
        proposal.artifact["action_capability"] = "execute"
        with pytest.raises(ValueError, match="work resumption artifact"):
            SuggestionKernel(conn, cfg).emit(proposal, now=now)

        proposal.artifact["action_capability"] = "none"
        proposal.artifact["tool_call"] = "send_message"
        with pytest.raises(ValueError, match="work resumption artifact"):
            SuggestionKernel(conn, cfg).emit(proposal, now=now)
