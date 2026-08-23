from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

from openchronicle import cli
from openchronicle import config as config_mod
from openchronicle.artifact_adoptions import procedure_screen
from openchronicle.artifact_adoptions.service import ArtifactAdoptionService
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.prompt_rescue.service import PromptRescueService
from openchronicle.store import fts


def _response(payload: dict[str, Any]):
    message = SimpleNamespace(content=json.dumps(payload), tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _cfg() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.prompt_rescue.enabled = True
    cfg.models["prompt_rescue"] = config_mod.ModelConfig(
        model="ollama/test-local",
        base_url="http://127.0.0.1:11434",
        timeout_seconds=1,
        num_retries=0,
    )
    return cfg


def _prompt_output() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow": "prompt_rescue",
        "action_capability": "none",
        "improved_prompt": "Write a concise release note.",
        "assumptions": [],
        "missing_context": [],
        "changes": ["Made the deliverable explicit."],
    }


def _qualifying_prediction() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "qualifies": True,
        "rationale": "The adopted prompt defines a reusable release-note workflow.",
        "procedure": {
            "title": "Release-note checklist",
            "procedure_type": "checklist",
            "scope": "Drafting release notes",
            "trigger": "When preparing a release note.",
            "steps": ["Summarize the change.", "Review unsupported claims."],
            "template": None,
            "action_capability": "none",
        },
    }


def _record_adoption(cfg) -> str:
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
        return adoption.id


def test_cli_lists_and_explicitly_screens_adoption(
    ac_root: Path,
    monkeypatch,
) -> None:
    cfg = _cfg()
    adoption_id = _record_adoption(cfg)
    monkeypatch.setattr(cli, "_init", lambda: cfg)
    monkeypatch.setattr(
        procedure_screen.llm_mod,
        "call_llm",
        lambda *_args, **_kwargs: _response(_qualifying_prediction()),
    )
    runner = CliRunner()

    listed = runner.invoke(cli.app, ["memory", "adoptions"], terminal_width=240)
    assert listed.exit_code == 0
    assert adoption_id[:10] in listed.stdout
    assert "prompt_resc" in listed.stdout

    screened = runner.invoke(cli.app, ["memory", "screen-adoption", adoption_id])
    assert screened.exit_code == 0
    assert "may send that text" in screened.stdout
    assert "Staged review-only candidate" in screened.stdout
    assert "approval remains" in screened.stdout
    assert "explicit." in screened.stdout
    with fts.cursor() as conn:
        candidates = candidate_store.list_candidates(conn)
    assert len(candidates) == 1
    assert candidates[0].status == "pending"


def test_cli_screen_reports_rejection_without_candidate(
    ac_root: Path,
    monkeypatch,
) -> None:
    cfg = _cfg()
    adoption_id = _record_adoption(cfg)
    monkeypatch.setattr(cli, "_init", lambda: cfg)
    rejected: dict[str, Any] = {
        "schema_version": 1,
        "qualifies": False,
        "rationale": "The output is one-off.",
        "procedure": None,
    }
    monkeypatch.setattr(
        procedure_screen.llm_mod,
        "call_llm",
        lambda *_args, **_kwargs: _response(rejected),
    )

    result = CliRunner().invoke(cli.app, ["memory", "screen-adoption", adoption_id])

    assert result.exit_code == 0
    assert "Not staged" in result.stdout
    with fts.cursor() as conn:
        assert candidate_store.list_candidates(conn) == []
