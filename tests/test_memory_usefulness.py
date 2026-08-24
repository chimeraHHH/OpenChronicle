from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

from openchronicle import cli
from openchronicle import config as config_mod
from openchronicle.artifact_adoptions.service import ArtifactAdoptionService
from openchronicle.prompt_rescue.service import PromptRescueService
from openchronicle.services.memory_usefulness import memory_usefulness_report
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.store.facts import make_fact_metadata


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


def _response() -> SimpleNamespace:
    output: dict[str, Any] = {
        "schema_version": 1,
        "workflow": "prompt_rescue",
        "action_capability": "none",
        "improved_prompt": "Produce the requested text artifact.",
        "assumptions": [],
        "missing_context": [],
        "changes": ["Made the output requirement explicit."],
    }
    message = SimpleNamespace(content=json.dumps(output), tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _publish_procedure(
    conn,
    *,
    path: str,
    entry_id: str,
    content: str,
    valid_to: str = "",
) -> None:
    entries_store.create_file(
        conn,
        name=path,
        description="Reviewed text-only procedure",
        tags=["procedure", "text-only"],
    )
    entries_store.append_entry_once(
        conn,
        name=path,
        content=content,
        tags=["procedure", "text-only"],
        entry_id=entry_id,
        origin=files_store.MANUAL_ENTRY_ORIGIN,
        fact_metadata=make_fact_metadata(
            subject_key=f"procedure.{entry_id}",
            assertion_kind="inferred",
            valid_to=valid_to,
        ),
    )


def _prepare(
    service: PromptRescueService,
    rough_prompt: str,
):
    service.queue(rough_prompt=rough_prompt)
    ready = service.process_next()
    assert ready is not None and ready.status == "ready"
    return ready


def test_usefulness_report_tracks_exact_revisions_without_storing_text(
    ac_root: Path,
) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        prompt = PromptRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: _response(),
        )
        adoption = ArtifactAdoptionService(conn, cfg)

        _publish_procedure(
            conn,
            path="procedure-one.md",
            entry_id="procedure-one-entry",
            content="ALPHAUNIQUETOKEN release workflow.",
        )
        first = _prepare(prompt, "ALPHAUNIQUETOKEN")
        first_adoption, created = adoption.record_used(
            artifact_kind="prompt_rescue",
            artifact_id=first.id,
            expected_version=first.version,
            expected_artifact_digest=first.output_digest,
            adopted_at=datetime(2026, 8, 24, 10, tzinfo=UTC),
        )
        replay, replay_created = adoption.record_used(
            artifact_kind="prompt_rescue",
            artifact_id=first.id,
            expected_version=first.version,
            expected_artifact_digest=first.output_digest,
            adopted_at=datetime(2026, 8, 24, 11, tzinfo=UTC),
        )
        assert created is True
        assert replay_created is False
        assert replay == first_adoption
        edited = prompt.edit(
            first.id,
            expected_version=first.version,
            improved_prompt="Produce a concise requested text artifact.",
        )
        adoption.record_used(
            artifact_kind="prompt_rescue",
            artifact_id=edited.id,
            expected_version=edited.version,
            expected_artifact_digest=edited.output_digest,
            adopted_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
        )

        initial = memory_usefulness_report(
            conn,
            cfg,
            as_of=datetime(2026, 8, 24, 13, tzinfo=UTC),
        )
        assert initial["memory_revisions"][0]["current_status"] == "current"

        entries_store.supersede_entry(
            conn,
            name="procedure-one.md",
            old_entry_id="procedure-one-entry",
            new_content="Replacement release workflow.",
            reason="reviewed correction",
            tags=["procedure", "text-only"],
            fact_metadata=make_fact_metadata(
                subject_key="procedure.procedure-one-entry",
                assertion_kind="inferred",
            ),
        )
        _publish_procedure(
            conn,
            path="procedure-two.md",
            entry_id="procedure-two-entry",
            content="BETAUNIQUETOKEN planning workflow.",
            valid_to="2027-01-01T00:00:00+00:00",
        )
        _prepare(prompt, "BETAUNIQUETOKEN")
        _publish_procedure(
            conn,
            path="procedure-three.md",
            entry_id="procedure-three-entry",
            content="GAMMAUNIQUETOKEN summary workflow.",
        )
        _prepare(prompt, "GAMMAUNIQUETOKEN")
        files_store.memory_path("procedure-three.md").unlink()

        no_memory = _prepare(prompt, "UNMATCHED-ZULU-QUERY")
        adoption.record_used(
            artifact_kind="prompt_rescue",
            artifact_id=no_memory.id,
            expected_version=no_memory.version,
            expected_artifact_digest=no_memory.output_digest,
            adopted_at=datetime(2026, 8, 24, 14, tzinfo=UTC),
        )

        report = memory_usefulness_report(
            conn,
            cfg,
            as_of=datetime(2028, 1, 1, tzinfo=UTC),
        )
        replay_report = memory_usefulness_report(
            conn,
            cfg,
            as_of=datetime(2028, 1, 1, tzinfo=UTC),
        )

    assert replay_report == report
    summary = report["summary"]
    assert summary == {
        **summary,
        "memory_revision_count": 3,
        "conditioned_output_count": 3,
        "conditioned_unedited_adoption_count": 1,
        "conditioned_edited_adoption_count": 1,
        "conditioned_output_unedited_adoption_rate": 0.333333,
        "no_memory_output_count": 1,
        "no_memory_unedited_adoption_count": 1,
        "no_memory_edited_adoption_count": 0,
        "no_memory_output_unedited_adoption_rate": 1.0,
        "exact_revision_binding_count": 3,
        "tracked_exact_revision_binding_count": 3,
        "exact_revision_tracking_coverage": 1.0,
        "quarantined_conditioned_output_count": 0,
        "invalid_prompt_rescue_row_count": 0,
        "invalid_adoption_row_count": 0,
        "unlinked_prompt_rescue_adoption_count": 0,
    }
    by_id = {
        row["memory_revision"]["id"]: row for row in report["memory_revisions"]
    }
    assert by_id["procedure-one-entry"]["current_status"] == "superseded"
    assert by_id["procedure-one-entry"]["current_status_reason"].startswith(
        "superseded_by:"
    )
    assert by_id["procedure-one-entry"]["unedited_adoption_count"] == 1
    assert by_id["procedure-one-entry"]["edited_adoption_count"] == 1
    assert len(by_id["procedure-one-entry"]["dependent_artifacts"][0]["adoptions"]) == 2
    assert by_id["procedure-two-entry"]["current_status"] == "expired"
    assert by_id["procedure-two-entry"]["current_status_reason"] == "valid_to"
    assert by_id["procedure-three-entry"]["current_status"] == "missing"
    assert by_id["procedure-three-entry"]["current_status_reason"] == "file_missing"
    encoded = json.dumps(report, sort_keys=True)
    assert "ALPHAUNIQUETOKEN" not in encoded
    assert "BETAUNIQUETOKEN" not in encoded
    assert "GAMMAUNIQUETOKEN" not in encoded
    assert "UNMATCHED-ZULU-QUERY" not in encoded
    assert "Produce the requested text artifact" not in encoded


def test_usefulness_report_quarantines_mismatched_provenance_edges(
    ac_root: Path,
) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        _publish_procedure(
            conn,
            path="procedure-one.md",
            entry_id="procedure-one-entry",
            content="ALPHAUNIQUETOKEN release workflow.",
        )
        prompt = PromptRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: _response(),
        )
        ready = _prepare(prompt, "ALPHAUNIQUETOKEN")
        conn.execute(
            """
            UPDATE provenance_edges
               SET source_hash=?
             WHERE subject_kind='prompt_rescue' AND subject_id=?
               AND source_kind='memory_entry'
            """,
            ("f" * 64, ready.id),
        )

        report = memory_usefulness_report(
            conn,
            cfg,
            as_of=datetime(2026, 8, 24, 13, tzinfo=UTC),
        )

    assert report["memory_revisions"] == []
    assert report["summary"]["conditioned_output_count"] == 0
    assert report["summary"]["quarantined_conditioned_output_count"] == 1
    assert report["summary"]["exact_revision_binding_count"] == 1
    assert report["summary"]["tracked_exact_revision_binding_count"] == 0
    assert report["summary"]["exact_revision_tracking_coverage"] == 0.0


def test_usefulness_report_rejects_forged_supersession_state(
    ac_root: Path,
) -> None:
    cfg = _cfg()
    with fts.cursor() as conn:
        prompt = PromptRescueService(
            conn,
            cfg,
            llm_caller=lambda *_args, **_kwargs: _response(),
        )
        for suffix in ("marker", "strike"):
            _publish_procedure(
                conn,
                path=f"procedure-{suffix}.md",
                entry_id=f"procedure-{suffix}-entry",
                content=f"UNIQUE-{suffix.upper()}-TOKEN workflow.",
            )
            _prepare(prompt, f"UNIQUE-{suffix.upper()}-TOKEN")

        marker_path = files_store.memory_path("procedure-marker.md")
        marker_entry = files_store.read_file(marker_path).entries[0]
        marker_path.write_text(
            marker_path.read_text().replace(
                marker_entry.heading_line,
                marker_entry.heading_line + " #superseded-by:forged",
                1,
            )
        )

        strike_path = files_store.memory_path("procedure-strike.md")
        strike_entry = files_store.read_file(strike_path).entries[0]
        strike_path.write_text(
            strike_path.read_text().replace(
                strike_entry.body,
                f"~~{strike_entry.body}~~",
                1,
            )
        )

        report = memory_usefulness_report(
            conn,
            cfg,
            as_of=datetime(2026, 8, 24, 13, tzinfo=UTC),
        )

    assert {
        (
            row["memory_revision"]["id"],
            row["current_status"],
            row["current_status_reason"],
        )
        for row in report["memory_revisions"]
    } == {
        ("procedure-marker-entry", "missing", "invalid_supersede_chain"),
        ("procedure-strike-entry", "missing", "invalid_supersede_chain"),
    }


def test_memory_usefulness_cli_emits_deterministic_empty_json(
    ac_root: Path,
    monkeypatch,
) -> None:
    cfg = _cfg()
    monkeypatch.setattr(cli, "_init", lambda: cfg)
    with fts.cursor() as conn:
        before = {
            "jobs": conn.execute("SELECT COUNT(*) FROM prompt_rescue_jobs").fetchone()[0],
            "adoptions": conn.execute(
                "SELECT COUNT(*) FROM artifact_adoptions"
            ).fetchone()[0],
            "edges": conn.execute("SELECT COUNT(*) FROM provenance_edges").fetchone()[0],
        }

    result = CliRunner().invoke(cli.app, ["memory", "usefulness", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["summary"]["conditioned_output_count"] == 0
    assert report["summary"]["exact_revision_tracking_coverage"] == 1.0
    assert report["memory_revisions"] == []
    with fts.cursor() as conn:
        after = {
            "jobs": conn.execute("SELECT COUNT(*) FROM prompt_rescue_jobs").fetchone()[0],
            "adoptions": conn.execute(
                "SELECT COUNT(*) FROM artifact_adoptions"
            ).fetchone()[0],
            "edges": conn.execute("SELECT COUNT(*) FROM provenance_edges").fetchone()[0],
        }
    assert after == before
