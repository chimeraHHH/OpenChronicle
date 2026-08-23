from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openchronicle import config as config_mod
from openchronicle.artifact_adoptions import procedure_screen
from openchronicle.artifact_adoptions import store as adoption_store
from openchronicle.artifact_adoptions.service import (
    ArtifactAdoptionConflict,
    ArtifactAdoptionService,
)
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.prompt_rescue.service import PromptRescueService
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef
from openchronicle.reply_rescue.service import ReplyRescueService
from openchronicle.services.evidence import EvidenceResolver
from openchronicle.services.memory import MemoryService
from openchronicle.store import files as files_store
from openchronicle.store import fts


def _response(payload: dict[str, Any]):
    message = SimpleNamespace(content=json.dumps(payload), tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _cfg() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.prompt_rescue.enabled = True
    cfg.reply_rescue.enabled = True
    cfg.models["prompt_rescue"] = config_mod.ModelConfig(
        model="ollama/test-local",
        base_url="http://127.0.0.1:11434",
        timeout_seconds=1,
        num_retries=0,
    )
    cfg.models["reply_rescue"] = config_mod.ModelConfig(
        model="ollama/test-local",
        base_url="http://127.0.0.1:11434",
        timeout_seconds=1,
        num_retries=0,
    )
    return cfg


def _prompt_output(text: str = "Write a concise release note.") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow": "prompt_rescue",
        "action_capability": "none",
        "improved_prompt": text,
        "assumptions": [],
        "missing_context": [],
        "changes": ["Made the deliverable explicit."],
    }


def _reply_output(text: str = "Hi Ana, Tuesday works for me.") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow": "reply_rescue",
        "action_capability": "none",
        "reply_body": text,
        "addressed_questions": ["Confirmed Tuesday."],
        "unresolved_questions": [],
        "assumptions": [],
        "warnings": ["Verify the recipient before copying."],
        "claims": [{"text": "Tuesday works.", "support": "user_direction"}],
    }


def _qualifying_prediction() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "qualifies": True,
        "rationale": "The adopted prompt explicitly defines a reusable release-note workflow.",
        "procedure": {
            "title": "Release-note drafting checklist",
            "procedure_type": "checklist",
            "scope": "Drafting release notes",
            "trigger": "When preparing a release note from a completed change list.",
            "steps": [
                "Summarize the user-visible change.",
                "Call out migration guidance and compatibility impact.",
                "Review the text for unsupported claims.",
            ],
            "template": None,
            "action_capability": "none",
        },
    }


def _rejected_prediction() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "qualifies": False,
        "rationale": "This is a one-off reply, not a reusable procedure.",
        "procedure": None,
    }


def test_prompt_adoption_is_explicit_exact_idempotent_and_versioned(ac_root: Path) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        prompt = PromptRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: _response(_prompt_output()),
        )
        queued, _created = prompt.queue(rough_prompt="make a release note")
        ready = prompt.process_next()
        assert ready is not None and ready.output is not None
        adoption = ArtifactAdoptionService(conn, cfg)

        first, created = adoption.record_used(
            artifact_kind="prompt_rescue",
            artifact_id=ready.id,
            expected_version=ready.version,
            expected_artifact_digest=ready.output_digest,
            adopted_at=datetime(2026, 8, 24, 10, tzinfo=UTC),
        )
        replay, replay_created = adoption.record_used(
            artifact_kind="prompt_rescue",
            artifact_id=ready.id,
            expected_version=ready.version,
            expected_artifact_digest=ready.output_digest,
            adopted_at=datetime(2026, 8, 24, 11, tzinfo=UTC),
        )

        assert created is True
        assert replay_created is False
        assert replay == first
        assert first.id.startswith("aa-")
        assert first.artifact == ready.output
        assert first.artifact_digest == ready.output_digest
        assert first.artifact_version == ready.version
        assert first.output_edited is False
        assert first.adopted_at == "2026-08-24T10:00:00.000000+00:00"

        with pytest.raises(ArtifactAdoptionConflict):
            adoption.record_used(
                artifact_kind="prompt_rescue",
                artifact_id=ready.id,
                expected_version=ready.version + 1,
                expected_artifact_digest=ready.output_digest,
            )
        with pytest.raises(ArtifactAdoptionConflict):
            adoption.record_used(
                artifact_kind="prompt_rescue",
                artifact_id=ready.id,
                expected_version=ready.version,
                expected_artifact_digest="0" * 64,
            )

        edited = prompt.edit(
            ready.id,
            expected_version=ready.version,
            improved_prompt="Write a concise release note with migration guidance.",
        )
        second, second_created = adoption.record_used(
            artifact_kind="prompt_rescue",
            artifact_id=edited.id,
            expected_version=edited.version,
            expected_artifact_digest=edited.output_digest,
        )
        history = adoption_store.list_for_artifact(
            conn,
            artifact_kind="prompt_rescue",
            artifact_id=ready.id,
        )

        assert second_created is True
        assert second.id != first.id
        assert second.output_edited is True
        assert {item.id for item in history} == {first.id, second.id}
        assert first.artifact["improved_prompt"] == "Write a concise release note."

        reverted = prompt.edit(
            edited.id,
            expected_version=edited.version,
            improved_prompt="Write a concise release note.",
        )
        first_replay, first_replay_created = adoption.record_used(
            artifact_kind="prompt_rescue",
            artifact_id=reverted.id,
            expected_version=reverted.version,
            expected_artifact_digest=reverted.output_digest,
        )
        assert first_replay_created is False
        assert first_replay == first

        prompt.delete(reverted.id, expected_version=reverted.version)
        assert adoption_store.list_for_artifact(
            conn,
            artifact_kind="prompt_rescue",
            artifact_id=ready.id,
        ) == []


def test_reply_adoption_binds_reviewed_output_and_is_removed_with_source(
    ac_root: Path,
) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        reply = ReplyRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: _response(_reply_output()),
        )
        queued, _created = reply.queue_manual(
            conversation_text="Ana: Does Tuesday work?",
            intended_recipients=["Ana"],
            goal="Confirm Tuesday.",
            commitments=["Tuesday works for me."],
        )
        ready = reply.process_next()
        assert ready is not None and ready.output is not None

        recorded, created = ArtifactAdoptionService(conn, cfg).record_used(
            artifact_kind="reply_rescue",
            artifact_id=ready.id,
            expected_version=ready.version,
            expected_artifact_digest=ready.output_digest,
        )

        assert created is True
        assert recorded.artifact == _reply_output()
        assert recorded.output_edited is False
        reply.delete(ready.id, expected_version=ready.version)
        assert adoption_store.get(conn, recorded.id) is None
        assert adoption_store.list_for_artifact(
            conn,
            artifact_kind="reply_rescue",
            artifact_id=queued.id,
        ) == []


def test_adoption_store_quarantines_tampered_projection(ac_root: Path) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        prompt = PromptRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: _response(_prompt_output()),
        )
        queued, _created = prompt.queue(rough_prompt="make a release note")
        ready = prompt.process_next()
        assert ready is not None
        adoption, _created = ArtifactAdoptionService(conn, cfg).record_used(
            artifact_kind="prompt_rescue",
            artifact_id=ready.id,
            expected_version=ready.version,
            expected_artifact_digest=ready.output_digest,
        )
        conn.execute(
            "UPDATE artifact_adoptions SET output_edited=1 WHERE id=?",
            (adoption.id,),
        )

        assert adoption_store.get(conn, adoption.id) is None


def test_adopted_artifact_can_only_stage_then_explicitly_publish_procedure(
    ac_root: Path,
) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        prompt = PromptRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: _response(_prompt_output()),
        )
        _queued, _created = prompt.queue(rough_prompt="make a release note")
        ready = prompt.process_next()
        assert ready is not None
        adoption, _created = ArtifactAdoptionService(conn, cfg).record_used(
            artifact_kind="prompt_rescue",
            artifact_id=ready.id,
            expected_version=ready.version,
            expected_artifact_digest=ready.output_digest,
        )

        result = procedure_screen.stage_adoption(
            conn,
            cfg,
            adoption.id,
            llm_caller=lambda *_args, **_kwargs: _response(_qualifying_prediction()),
        )

        candidate = result.candidate
        assert result.decision.qualifies is True
        assert candidate is not None
        assert candidate.status == "pending"
        assert candidate.kind == "procedure"
        assert candidate.assertion_kind == "inferred"
        assert candidate.subject_key == f"procedure.adopted.{adoption.id.removeprefix('aa-')}"
        assert candidate.tags == [
            "procedure",
            "checklist",
            "text-only",
            "adopted-artifact",
        ]
        assert not files_store.memory_path(candidate.target_path).exists()
        source = adoption_store.evidence_ref(adoption)
        assert candidate.claim_evidence == [source]
        assert provenance_store.direct_sources(
            conn,
            EvidenceRef(kind="memory_candidate", id=candidate.id),
        ) == [source]
        resolved = EvidenceResolver(conn, cfg).resolve(source)
        assert resolved["status"] == "current"
        assert resolved["content"]["type"] == "artifact_adoption"
        assert "release note" in resolved["content"]["artifact_text"].lower()

        approved = MemoryService(conn, cfg=cfg).approve_candidate(
            candidate.id,
            expected_version=candidate.version,
        )
        assert approved.status == "accepted"
        parsed = files_store.read_file(files_store.memory_path(candidate.target_path))
        assert len(parsed.entries) == 1
        assert "**Action capability:** text-generation context only" in parsed.entries[0].body
        assert parsed.entries[0].fact_metadata is not None
        assert parsed.entries[0].fact_metadata.assertion_kind == "inferred"


def test_one_off_adoption_is_rejected_without_staging_candidate(ac_root: Path) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        reply = ReplyRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: _response(_reply_output()),
        )
        _queued, _created = reply.queue_manual(
            conversation_text="Ana: Does Tuesday work?",
            intended_recipients=["Ana"],
            goal="Confirm Tuesday.",
            commitments=["Tuesday works for me."],
        )
        ready = reply.process_next()
        assert ready is not None
        adoption, _created = ArtifactAdoptionService(conn, cfg).record_used(
            artifact_kind="reply_rescue",
            artifact_id=ready.id,
            expected_version=ready.version,
            expected_artifact_digest=ready.output_digest,
        )

        result = procedure_screen.stage_adoption(
            conn,
            cfg,
            adoption.id,
            llm_caller=lambda *_args, **_kwargs: _response(_rejected_prediction()),
        )

        assert result.decision.qualifies is False
        assert result.candidate is None
        assert candidate_store.list_candidates(conn) == []


def test_adoption_change_during_screening_cannot_stage_candidate(ac_root: Path) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        prompt = PromptRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: _response(_prompt_output()),
        )
        _queued, _created = prompt.queue(rough_prompt="make a release note")
        ready = prompt.process_next()
        assert ready is not None
        adoption, _created = ArtifactAdoptionService(conn, cfg).record_used(
            artifact_kind="prompt_rescue",
            artifact_id=ready.id,
            expected_version=ready.version,
            expected_artifact_digest=ready.output_digest,
        )

        def remove_source(*_args, **_kwargs):
            prompt.delete(ready.id, expected_version=ready.version)
            return _response(_qualifying_prediction())

        with pytest.raises(ValueError, match="changed during screening"):
            procedure_screen.stage_adoption(
                conn,
                cfg,
                adoption.id,
                llm_caller=remove_source,
            )
        assert candidate_store.list_candidates(conn) == []


def test_deleting_source_after_screening_blocks_candidate_approval(ac_root: Path) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        prompt = PromptRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: _response(_prompt_output()),
        )
        _queued, _created = prompt.queue(rough_prompt="make a release note")
        ready = prompt.process_next()
        assert ready is not None
        adoption, _created = ArtifactAdoptionService(conn, cfg).record_used(
            artifact_kind="prompt_rescue",
            artifact_id=ready.id,
            expected_version=ready.version,
            expected_artifact_digest=ready.output_digest,
        )
        result = procedure_screen.stage_adoption(
            conn,
            cfg,
            adoption.id,
            llm_caller=lambda *_args, **_kwargs: _response(_qualifying_prediction()),
        )
        candidate = result.candidate
        assert candidate is not None

        prompt.delete(ready.id, expected_version=ready.version)
        with pytest.raises(candidate_store.CandidateConflict, match="evidence is"):
            MemoryService(conn, cfg=cfg).approve_candidate(
                candidate.id,
                expected_version=candidate.version,
            )
        changed = candidate_store.get(conn, candidate.id)
        assert changed is not None and changed.status == "conflict"
