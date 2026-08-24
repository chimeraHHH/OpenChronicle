from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openchronicle import cli, desktop_bridge
from openchronicle import config as config_mod
from openchronicle.capture import scheduler
from openchronicle.capture import store_lock as capture_store
from openchronicle.daily_wrap import store as daily_wrap_store
from openchronicle.mcp import server as mcp_server
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef, observation_digest
from openchronicle.services import snapshot as snapshot_service
from openchronicle.services.context import ContextService
from openchronicle.services.evidence import EvidenceResolver
from openchronicle.store import fts


def _config() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    return cfg


def _observation(marker: str) -> tuple[EvidenceRef, Path]:
    capture = {
        "timestamp": scheduler._now_iso(),
        "schema_version": 4,
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "title": marker,
        },
        "focused_element": {"role": "AXTextArea", "value": marker},
        "visible_text": marker,
        "url": "",
    }
    path = scheduler._write_capture(capture)
    return (
        EvidenceRef(
            kind="observation",
            id=str(capture["observation_id"]),
            path=path.name,
            timestamp=str(capture["timestamp"]),
            content_hash=observation_digest(capture),
        ),
        path,
    )


def _published_wrap(conn, source: EvidenceRef):
    claim = daily_wrap_store.claim(
        conn,
        local_date="2026-08-08",
        timezone="UTC",
        scope="default",
        window_start_utc="2026-08-08T00:00:00+00:00",
        window_end_utc="2026-08-09T00:00:00+00:00",
        workflow_version=1,
        coverage_status="ready",
        input_digest="revision-binding-input",
        lease_token="revision-binding-lease",
    )
    output = {
        "schema_version": 1,
        "local_date": "2026-08-08",
        "timezone": "UTC",
        "status": "ready",
        "summary": "REVISION_BOUND_OUTPUT",
        "completed": [],
        "progressed": [],
        "open": [],
        "blocked": [],
        "needs_review": [],
        "coverage_gaps": [],
        "generated_at": "2026-08-09T00:05:00+00:00",
    }
    row = daily_wrap_store.complete(
        conn,
        wrap_id=claim.row.id,
        lease_token="revision-binding-lease",
        input_digest="revision-binding-input",
        window_start_utc="2026-08-08T00:00:00+00:00",
        window_end_utc="2026-08-09T00:00:00+00:00",
        workflow_version=1,
        coverage_status="ready",
        output=output,
        sources=[source],
        validate_input_current=lambda: None,
    )
    return row, output


def test_wrap_job_output_must_match_immutable_revision(ac_root: Path) -> None:
    source, _capture_path = _observation("REVISION_SOURCE")
    with fts.cursor() as conn:
        row, output = _published_wrap(conn, source)
        assert "error" not in mcp_server._get_daily_wrap(
            conn,
            cfg=_config(),
            local_date=row.local_date,
            timezone=row.timezone,
        )
        tampered = {**output, "summary": "TAMPERED_WITHOUT_REVISION"}
        conn.execute(
            "UPDATE daily_wrap_jobs SET output_json=? WHERE id=?",
            (json.dumps(tampered, sort_keys=True), row.id),
        )

        result = mcp_server._get_daily_wrap(
            conn,
            cfg=_config(),
            local_date=row.local_date,
            timezone=row.timezone,
        )

    assert "error" in result
    assert "TAMPERED_WITHOUT_REVISION" not in json.dumps(result)


def test_public_wrap_projection_ignores_mutable_failed_refresh_metadata(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _capture_path = _observation("LAST_KNOWN_GOOD_SOURCE")
    marker = "UNBOUND_WRAP_JOB_SECRET"
    cfg = _config()
    with fts.cursor() as conn:
        published, _output = _published_wrap(conn, source)
        conn.execute(
            """
            UPDATE daily_wrap_jobs
               SET status=?, attempt_count=999, lease_token=?, lease_expires_at=?,
                   input_digest=?, created_at=?, updated_at=?, completed_at=?, last_error=?
             WHERE id=?
            """,
            (
                marker,
                marker,
                marker,
                marker,
                marker,
                marker,
                marker,
                marker,
                published.id,
            ),
        )
        current = daily_wrap_store.get_by_id(conn, published.id)
        assert current is not None
        assert ContextService(conn, cfg).daily_wrap_allowed(
            current.id,
            expected_row=current,
        )
        public = current.to_dict()
        outputs = {
            "mcp_get": mcp_server._get_daily_wrap(
                conn,
                cfg=cfg,
                local_date=current.local_date,
                timezone=current.timezone,
            ),
            "mcp_list": mcp_server._list_daily_wraps(conn, cfg=cfg),
            "desktop": desktop_bridge._wrap_payload(current),
            "snapshot": snapshot_service._wrap_summary(current),
            "evidence": EvidenceResolver(conn, cfg).resolve(
                EvidenceRef(kind="daily_wrap", id=current.id)
            ),
        }

    assert set(public) == {
        "id",
        "local_date",
        "timezone",
        "scope",
        "window_start_utc",
        "window_end_utc",
        "workflow_version",
        "status",
        "coverage_status",
        "published_input_digest",
        "output",
        "revision",
    }
    assert public["status"] == "succeeded"
    serialized = json.dumps({"public": public, **outputs}, ensure_ascii=False)
    assert marker not in serialized
    for forbidden_key in (
        "attempt_count",
        "input_digest",
        "last_error",
        "created_at",
        "updated_at",
        "completed_at",
        "lease_token",
        "lease_expires_at",
    ):
        assert f'"{forbidden_key}"' not in serialized

    monkeypatch.setattr(cli, "_init", lambda: cfg)
    result = CliRunner().invoke(
        cli.app,
        ["daily-wrap", "show", "--date", "2026-08-08", "--timezone", "UTC"],
    )
    assert result.exit_code == 0, result.output
    assert marker not in result.output
    assert "attempt_count" not in result.output
    assert "last_error" not in result.output


def test_refresh_keeps_last_published_identity_until_atomic_completion(
    ac_root: Path,
) -> None:
    source, _capture_path = _observation("ATOMIC_REFRESH_SOURCE")
    cfg = _config()
    with fts.cursor() as conn:
        published, _output = _published_wrap(conn, source)
        refresh = daily_wrap_store.claim(
            conn,
            local_date="2026-08-08",
            timezone="UTC",
            scope="default",
            window_start_utc="2026-08-08T01:00:00+00:00",
            window_end_utc="2026-08-09T01:00:00+00:00",
            workflow_version=2,
            coverage_status="partial",
            input_digest="revision-binding-input-v2",
            lease_token="revision-binding-refresh-fail",
        )
        assert refresh.claimed
        running = daily_wrap_store.get_by_id(conn, published.id)
        assert running is not None
        assert running.status == "running"
        assert running.window_start_utc == published.window_start_utc
        assert running.window_end_utc == published.window_end_utc
        assert running.workflow_version == published.workflow_version == 1
        assert running.coverage_status == published.coverage_status == "ready"
        assert ContextService(conn, cfg).daily_wrap_allowed(
            running.id,
            expected_row=running,
        )

        failed = daily_wrap_store.fail(
            conn,
            wrap_id=published.id,
            lease_token="revision-binding-refresh-fail",
            input_digest="revision-binding-input-v2",
            error="expected refresh failure",
        )
        assert failed is not None and failed.status == "failed"
        assert ContextService(conn, cfg).daily_wrap_allowed(
            failed.id,
            expected_row=failed,
        )

        retry = daily_wrap_store.claim(
            conn,
            local_date="2026-08-08",
            timezone="UTC",
            scope="default",
            window_start_utc="2026-08-08T00:00:00+00:00",
            window_end_utc="2026-08-09T00:00:00+00:00",
            workflow_version=2,
            coverage_status="partial",
            input_digest="revision-binding-input-v2",
            lease_token="revision-binding-refresh-success",
        )
        output_v2 = {
            "schema_version": 1,
            "local_date": "2026-08-08",
            "timezone": "UTC",
            "status": "partial",
            "summary": "REVISION_BOUND_OUTPUT_V2",
            "completed": [],
            "progressed": [],
            "open": [],
            "blocked": [],
            "needs_review": [],
            "coverage_gaps": ["day_in_progress"],
            "generated_at": "2026-08-09T00:06:00+00:00",
        }
        refreshed = daily_wrap_store.complete(
            conn,
            wrap_id=retry.row.id,
            lease_token="revision-binding-refresh-success",
            input_digest="revision-binding-input-v2",
            window_start_utc="2026-08-08T00:00:00+00:00",
            window_end_utc="2026-08-09T00:00:00+00:00",
            workflow_version=2,
            coverage_status="partial",
            output=output_v2,
            sources=[source],
            validate_input_current=lambda: None,
        )

        assert refreshed.revision == 2
        assert refreshed.workflow_version == 2
        assert refreshed.coverage_status == "partial"
        assert refreshed.output == output_v2
        assert ContextService(conn, cfg).daily_wrap_allowed(
            refreshed.id,
            expected_row=refreshed,
        )


def test_wrap_parent_edges_must_equal_published_revision_edges(
    ac_root: Path,
) -> None:
    source_a, capture_a = _observation("SECRET_SOURCE_A")
    source_b, _capture_b = _observation("UNRELATED_SOURCE_B")
    with fts.cursor() as conn:
        row, _output = _published_wrap(conn, source_a)
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="daily_wrap", id=row.id),
            sources=[source_b],
        )
        capture_a.unlink()

        result = mcp_server._get_daily_wrap(
            conn,
            cfg=_config(),
            local_date=row.local_date,
            timezone=row.timezone,
        )

    assert "error" in result
    assert "REVISION_BOUND_OUTPUT" not in json.dumps(result)


def test_wrap_parent_and_revision_edges_cannot_be_swapped_together(
    ac_root: Path,
) -> None:
    source_a, _capture_a = _observation("DOUBLE_SWAP_PRIVATE_SOURCE_A")
    source_b, _capture_b = _observation("DOUBLE_SWAP_CURRENT_SOURCE_B")
    cfg = _config()
    with fts.cursor() as conn:
        row, _output = _published_wrap(conn, source_a)
        revision_ref = EvidenceRef(
            kind="daily_wrap_revision",
            id=f"{row.id}:r{row.revision}",
            path=row.id,
        )
        assert ContextService(conn, cfg).daily_wrap_allowed(
            row.id,
            expected_row=row,
        )

        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="daily_wrap", id=row.id),
            sources=[source_b],
        )
        provenance_store.replace_sources(
            conn,
            subject=revision_ref,
            sources=[source_b],
        )

        current = daily_wrap_store.get_by_id(conn, row.id)
        assert current is not None
        assert not daily_wrap_store.revision_sources_are_current(
            conn,
            revision_ref,
        )
        assert not ContextService(conn, cfg).daily_wrap_allowed(
            current.id,
            expected_row=current,
        )
        read = mcp_server._get_daily_wrap(
            conn,
            cfg=cfg,
            local_date=current.local_date,
            timezone=current.timezone,
        )
        listed = mcp_server._list_daily_wraps(conn, cfg=cfg)
        resolved = EvidenceResolver(conn, cfg).resolve(
            EvidenceRef(kind="daily_wrap", id=current.id)
        )

    assert "error" in read
    assert listed == {"count": 0, "wraps": []}
    assert resolved["status"] != "current"
    serialized = json.dumps(
        {"read": read, "listed": listed, "resolved": resolved},
        ensure_ascii=False,
    )
    assert "REVISION_BOUND_OUTPUT" not in serialized


def test_malformed_wrap_edge_cannot_alias_an_empty_source_set(ac_root: Path) -> None:
    source, _capture_path = _observation("MALFORMED_WRAP_EDGE_SOURCE")
    cfg = _config()
    with fts.cursor() as conn:
        row, _output = _published_wrap(conn, source)
        conn.execute(
            """
            UPDATE provenance_edges
               SET source_id=?
             WHERE subject_kind IN ('daily_wrap', 'daily_wrap_revision')
               AND subject_path IN ('', ?)
            """,
            (sqlite3.Binary(b"malformed-source-id"), row.id),
        )

        current = daily_wrap_store.get_by_id(conn, row.id)
        assert current is not None
        assert not ContextService(conn, cfg).daily_wrap_allowed(
            current.id,
            expected_row=current,
        )
        result = mcp_server._get_daily_wrap(
            conn,
            cfg=cfg,
            local_date=current.local_date,
            timezone=current.timezone,
        )

    assert "error" in result
    assert "REVISION_BOUND_OUTPUT" not in json.dumps(result)


def test_wrap_read_is_linearized_with_capture_cleanup(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, capture_path = _observation("LINEARIZED_SOURCE")
    with fts.cursor() as conn:
        row, _output = _published_wrap(conn, source)

    entered_serialization = threading.Event()
    release_serialization = threading.Event()
    cleanup_started = threading.Event()
    cleanup_done = threading.Event()
    original_to_dict = daily_wrap_store.DailyWrapRow.to_dict

    def blocking_to_dict(self, *args, **kwargs):
        entered_serialization.set()
        if not release_serialization.wait(timeout=5):
            raise AssertionError("test did not release wrap serialization")
        return original_to_dict(self, *args, **kwargs)

    monkeypatch.setattr(daily_wrap_store.DailyWrapRow, "to_dict", blocking_to_dict)

    def read_wrap():
        with fts.cursor() as conn:
            return mcp_server._get_daily_wrap(
                conn,
                cfg=_config(),
                local_date=row.local_date,
                timezone=row.timezone,
            )

    def clean_capture() -> None:
        cleanup_started.set()
        with capture_store.capture_store_lock():
            capture_path.unlink()
        cleanup_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(read_wrap)
        assert entered_serialization.wait(timeout=5)
        cleanup_future = pool.submit(clean_capture)
        assert cleanup_started.wait(timeout=5)
        assert not cleanup_done.wait(timeout=0.1)
        release_serialization.set()
        result = read_future.result(timeout=5)
        cleanup_future.result(timeout=5)

    assert result["output"]["summary"] == "REVISION_BOUND_OUTPUT"
    assert cleanup_done.is_set()
    with fts.cursor() as conn:
        after = mcp_server._get_daily_wrap(
            conn,
            cfg=_config(),
            local_date=row.local_date,
            timezone=row.timezone,
        )
    assert "error" in after


def test_wrap_revalidates_leaf_after_authorization(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, capture_path = _observation("DOUBLE_CHECK_SOURCE")
    with fts.cursor() as conn:
        row, _output = _published_wrap(conn, source)
        original_allowed = ContextService._observation_ref_allowed
        checks = 0

        def delete_after_first_check(self, observation):
            nonlocal checks
            allowed = original_allowed(self, observation)
            checks += 1
            if checks == 1:
                capture_path.unlink()
            return allowed

        monkeypatch.setattr(
            ContextService,
            "_observation_ref_allowed",
            delete_after_first_check,
        )

        result = mcp_server._get_daily_wrap(
            conn,
            cfg=_config(),
            local_date=row.local_date,
            timezone=row.timezone,
        )

    assert checks >= 2
    assert "error" in result
    assert "REVISION_BOUND_OUTPUT" not in json.dumps(result)
