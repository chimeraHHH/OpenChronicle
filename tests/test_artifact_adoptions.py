from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openchronicle import config as config_mod
from openchronicle.artifact_adoptions import store as adoption_store
from openchronicle.artifact_adoptions.service import (
    ArtifactAdoptionConflict,
    ArtifactAdoptionService,
)
from openchronicle.prompt_rescue.service import PromptRescueService
from openchronicle.reply_rescue.service import ReplyRescueService
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
