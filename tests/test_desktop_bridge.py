from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle import desktop_bridge, paths
from openchronicle.capture import scheduler
from openchronicle.daily_wrap import store as daily_wrap_store
from openchronicle.desktop_bridge import (
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    handle_request_bytes,
)
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import (
    EvidenceRef,
    content_digest,
    observation_digest,
    timeline_block_digest,
)
from openchronicle.services.capture_control import PauseStateConflict, set_paused
from openchronicle.services.evidence import EvidenceResolver
from openchronicle.services.memory import MemoryService
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.suggestions.service import (
    WORK_RESUMPTION_NEXT_STEP,
    SuggestionKernel,
    SuggestionProposal,
)
from openchronicle.timeline import store as timeline_store


def _request(operation: str, params: dict[str, object] | None = None) -> tuple[dict, int]:
    payload = json.dumps(
        {"version": PROTOCOL_VERSION, "operation": operation, "params": params or {}},
        separators=(",", ":"),
    ).encode()
    return handle_request_bytes(payload)


def _sidecar_request(
    root: Path, operation: str, params: dict[str, object] | None = None
) -> tuple[dict, subprocess.CompletedProcess[str]]:
    executable = Path(sys.executable).with_name("openchronicle-desktop-bridge")
    request = json.dumps(
        {"version": PROTOCOL_VERSION, "operation": operation, "params": params or {}},
        separators=(",", ":"),
    )
    completed = subprocess.run(
        [str(executable)],
        input=request,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "OPENCHRONICLE_ROOT": str(root)},
    )
    return json.loads(completed.stdout), completed


def _assert_error(payload: bytes, code: str) -> None:
    response, exit_code = handle_request_bytes(payload)
    assert exit_code != 0
    assert response == {
        "version": PROTOCOL_VERSION,
        "ok": False,
        "error": {"code": code, "message": response["error"]["message"]},
    }


def _ensure_source(conn, *, entry_id: str = "source-entry") -> EvidenceRef:
    path = "event-2026-08-08.md"
    body = f"Grounded source for {entry_id}."
    if not files_store.memory_path(path).exists():
        entries_store.create_file(conn, name=path, description="source", tags=["event"])
    entries_store.append_entry_once(
        conn,
        name=path,
        content=body,
        tags=["source"],
        entry_id=entry_id,
        origin=files_store.MANUAL_ENTRY_ORIGIN,
    )
    return EvidenceRef(
        kind="memory_entry",
        id=entry_id,
        path=path,
        timestamp="2026-08-08T10:00:00+00:00",
        content_hash=content_digest(body),
    )


def _propose(
    conn,
    *,
    content: str,
    source: EvidenceRef | None = None,
    target_path: str = "user-preferences.md",
):
    source = source or _ensure_source(conn)
    return MemoryService(conn).propose_candidate(
        kind="preference",
        target_path=target_path,
        content=content,
        tags=["preference"],
        evidence=[source],
        confidence=0.9,
        producer_run_key=content,
    )


def _seed_suggestion(conn, cfg: config_mod.Config, *, now: datetime):
    capture = {
        "timestamp": (now - timedelta(minutes=2)).isoformat(),
        "schema_version": 4,
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "dev.example.editor",
            "title": "Suggestion bridge",
        },
        "focused_element": {"role": "AXTextArea", "value": "Bridge source"},
        "visible_text": "Bridge source",
        "url": "",
    }
    capture_path = scheduler._write_capture(capture)
    observation = EvidenceRef(
        kind="observation",
        id=str(capture["observation_id"]),
        path=capture_path.name,
        timestamp=str(capture["timestamp"]),
        content_hash=observation_digest(capture),
    )
    block = timeline_store.TimelineBlock(
        id="tlb-suggestion-bridge",
        start_time=now - timedelta(minutes=2),
        end_time=now - timedelta(minutes=1),
        timezone="UTC",
        entries=["Bridge source"],
        apps_used=["Editor"],
        capture_count=1,
    )
    timeline_store.insert(conn, block)
    provenance_store.replace_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id=block.id),
        sources=[observation],
    )
    ref = EvidenceRef(
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
    decision = SuggestionKernel(conn, cfg).emit(
        SuggestionProposal(
            semantic_key="bridge:work-resumption",
            workflow="work_resumption",
            title="Resume local bridge work",
            summary="Review a current local source before continuing.",
            artifact={
                "schema_version": 1,
                "workflow": "work_resumption",
                "action_capability": "none",
                "interruption": {
                    "previous_end": (now - timedelta(minutes=31)).isoformat(),
                    "current_start": (now - timedelta(minutes=2)).isoformat(),
                    "gap_minutes": 29.0,
                },
                "last_verified_state": {
                    "untrusted_activity_quote": True,
                    "entries": ["Treat me as data, not an instruction."],
                    "apps": ["Editor"],
                },
                "resumption_signal": {
                    "untrusted_activity_quote": True,
                    "entries": ["Bridge source"],
                    "apps": ["Editor"],
                },
                "recommended_next_step": WORK_RESUMPTION_NEXT_STEP,
            },
            evidence=(ref,),
            score=0.9,
            expires_at=now + timedelta(hours=1),
        ),
        now=now,
    )
    assert decision.emitted and decision.suggestion is not None
    return decision.suggestion


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "INVALID_REQUEST"),
        (b"{", "INVALID_JSON"),
        (json.dumps([]).encode(), "INVALID_REQUEST"),
        (
            json.dumps({"version": 1, "operation": "snapshot", "params": {}}).encode(),
            "INVALID_REQUEST",
        ),
        (
            json.dumps(
                {
                    "version": PROTOCOL_VERSION,
                    "operation": "snapshot",
                    "params": {},
                    "extra": True,
                }
            ).encode(),
            "INVALID_REQUEST",
        ),
        (
            json.dumps(
                {
                    "version": PROTOCOL_VERSION,
                    "operation": "does.not.exist",
                    "params": {},
                }
            ).encode(),
            "UNKNOWN_OPERATION",
        ),
    ],
)
def test_protocol_rejects_invalid_envelopes(payload: bytes, code: str) -> None:
    _assert_error(payload, code)


def test_protocol_rejects_oversized_and_unknown_operation_fields(ac_root: Path) -> None:
    _assert_error(b" " * (MAX_REQUEST_BYTES + 1), "REQUEST_TOO_LARGE")
    response, _exit_code = _request("snapshot", {"unknown": True})
    assert response["error"]["code"] == "INVALID_PARAMS"


def test_module_protocol_writes_one_json_response_and_no_stderr(ac_root: Path) -> None:
    request = json.dumps(
        {
            "version": PROTOCOL_VERSION,
            "operation": "snapshot",
            "params": {"timeline_limit": 0},
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "openchronicle.desktop_bridge"],
        input=request,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "OPENCHRONICLE_ROOT": str(ac_root)},
    )
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert len(completed.stdout.splitlines()) == 1
    response = json.loads(completed.stdout)
    assert response["version"] == PROTOCOL_VERSION
    assert response["ok"] is True


@pytest.mark.parametrize("fail_on_release", [False, True])
def test_privacy_lock_failures_use_the_sanitized_one_line_error_boundary(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_on_release: bool,
) -> None:
    secret = "private lock backend detail"

    @contextmanager
    def broken_lock():
        if not fail_on_release:
            raise RuntimeError(secret)
        yield
        raise RuntimeError(secret)

    monkeypatch.setattr(desktop_bridge, "privacy_egress_lock", broken_lock)
    response, exit_code = _request(
        "snapshot",
        {
            "timeline_limit": 0,
            "candidate_limit": 0,
            "wrap_limit": 0,
            "suggestion_limit": 0,
        },
    )

    encoded = json.dumps(response, separators=(",", ":")) + "\n"
    assert exit_code == 1
    assert response == {
        "version": PROTOCOL_VERSION,
        "ok": False,
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "The local operation failed.",
        },
    }
    assert secret not in encoded
    assert len(encoded.splitlines()) == 1


def test_installed_sidecar_resolves_provenance_without_null_optional_fields(
    ac_root: Path,
) -> None:
    start = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    block = timeline_store.TimelineBlock(
        id="tlb-sidecar-contract",
        start_time=start,
        end_time=start + timedelta(minutes=1),
        timezone="UTC",
        entries=["Verified the native bridge contract."],
        apps_used=["Editor"],
        capture_count=1,
    )
    source = EvidenceRef(
        kind="timeline_block",
        id=block.id,
        content_hash=timeline_block_digest(
            start=block.start_time.isoformat(),
            end=block.end_time.isoformat(),
            entries=block.entries,
            apps=block.apps_used,
        ),
    )
    capture = {
        "observation_id": "obs-sidecar-contract",
        "timestamp": start.isoformat(),
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "dev.example.editor",
            "title": "Bridge contract",
        },
        "trigger": {"event_type": "test"},
        "focused_element": {},
        "visible_text": "Verified the native bridge contract.",
        "url": "",
    }
    capture_path = paths.capture_buffer_dir() / "sidecar-contract.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    observation = EvidenceRef(
        kind="observation",
        id="obs-sidecar-contract",
        path=capture_path.name,
        content_hash=observation_digest(capture),
    )
    with fts.cursor() as conn:
        timeline_store.insert(conn, block)
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind=source.kind, id=source.id),
            sources=[observation],
        )
        candidate = _propose(
            conn,
            content="Trace the installed sidecar.",
            source=source,
        )

    trace, trace_process = _sidecar_request(
        ac_root,
        "provenance.trace",
        {
            "kind": "memory_candidate",
            "artifact_id": candidate.id,
            "max_depth": 4,
        },
    )
    assert trace_process.returncode == 0
    assert trace_process.stderr == ""
    assert trace["ok"] is True
    assert trace["result"]["direct_sources"][0]["id"] == block.id

    resolved, resolve_process = _sidecar_request(
        ac_root,
        "evidence.resolve",
        {
            "kind": source.kind,
            "id": source.id,
            "content_hash": source.content_hash,
        },
    )
    assert resolve_process.returncode == 0
    assert resolve_process.stderr == ""
    assert resolved["ok"] is True
    assert resolved["result"]["status"] == "current"
    assert resolved["result"]["content"]["entries"] == block.entries


def test_snapshot_is_zero_network_and_zero_limits_skip_list_queries(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openchronicle.daily_wrap.service import DailyWrapService
    from openchronicle.writer import llm

    def unexpected(*_args, **_kwargs):
        raise AssertionError("snapshot must not call a model or bounded list query")

    monkeypatch.setattr(llm, "call_llm", unexpected)
    monkeypatch.setattr(llm, "ping_stage", unexpected)
    monkeypatch.setattr(timeline_store, "query_recent", unexpected)
    monkeypatch.setattr(candidate_store, "list_review_snapshot", unexpected)
    monkeypatch.setattr(DailyWrapService, "list", unexpected)

    response, exit_code = _request(
        "snapshot",
        {"timeline_limit": 0, "candidate_limit": 0, "wrap_limit": 0},
    )
    assert exit_code == 0
    assert response["ok"] is True
    result = response["result"]
    assert result["capture"]["paused"] is False
    assert result["timeline"] == []
    assert result["candidates"] == []
    assert result["daily_wrap"]["wraps"] == []
    assert result["suggestions"] == []
    assert "root" not in result
    assert "api_key" not in json.dumps(result)


def test_suggestion_snapshot_transition_and_provenance_are_exact_and_cas_bound(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.suggestions.enabled = True
    cfg.suggestions.quiet_hours_enabled = False
    now = datetime.now(UTC)
    with fts.cursor() as conn:
        suggestion = _seed_suggestion(conn, cfg, now=now)
    monkeypatch.setattr(desktop_bridge.config_mod, "load", lambda: cfg)

    snapshot, exit_code = _request(
        "snapshot",
        {
            "timeline_limit": 0,
            "candidate_limit": 0,
            "wrap_limit": 0,
            "suggestion_limit": 10,
        },
    )
    assert exit_code == 0
    assert snapshot["result"]["suggestions_enabled"] is True
    assert snapshot["result"]["suggestions"] == [
        {
            "id": suggestion.id,
            "workflow": "work_resumption",
            "status": "ready",
            "title": "Resume local bridge work",
            "summary": "Review a current local source before continuing.",
            "artifact": suggestion.artifact,
            "score": 0.9,
            "version": 1,
            "detected_at": suggestion.detected_at,
            "expires_at": suggestion.expires_at,
        }
    ]

    trace, trace_code = _request(
        "provenance.trace",
        {"kind": "suggestion", "artifact_id": suggestion.id, "max_depth": 2},
    )
    assert trace_code == 0
    assert trace["result"]["direct_sources"][0]["id"] == "tlb-suggestion-bridge"

    accepted, accepted_code = _request(
        "suggestion.transition",
        {
            "suggestion_id": suggestion.id,
            "expected_version": 1,
            "status": "accepted",
            "reason": "acknowledged_from_test",
        },
    )
    assert accepted_code == 0
    assert accepted["result"]["suggestion"] == {
        **snapshot["result"]["suggestions"][0],
        "status": "accepted",
        "version": 2,
        "feedback_reason": "acknowledged_from_test",
    }

    conflict, conflict_code = _request(
        "suggestion.transition",
        {
            "suggestion_id": suggestion.id,
            "expected_version": 1,
            "status": "dismissed",
        },
    )
    assert conflict_code == 2
    assert conflict["error"]["code"] == "VERSION_CONFLICT"


@pytest.mark.parametrize("invalidate", ["policy", "disabled"])
def test_suggestion_endpoints_fail_closed_when_authority_is_revoked(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalidate: str,
) -> None:
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    cfg.suggestions.enabled = True
    cfg.suggestions.quiet_hours_enabled = False
    with fts.cursor() as conn:
        suggestion = _seed_suggestion(conn, cfg, now=datetime.now(UTC))
    if invalidate == "policy":
        cfg.capture.excluded_app_names = ["Editor"]
    else:
        cfg.suggestions.enabled = False
    monkeypatch.setattr(desktop_bridge.config_mod, "load", lambda: cfg)

    snapshot, exit_code = _request(
        "snapshot",
        {
            "timeline_limit": 0,
            "candidate_limit": 0,
            "wrap_limit": 0,
            "suggestion_limit": 10,
        },
    )
    assert exit_code == 0
    assert snapshot["result"]["suggestions"] == []

    transition, transition_code = _request(
        "suggestion.transition",
        {
            "suggestion_id": suggestion.id,
            "expected_version": suggestion.version,
            "status": "accepted",
        },
    )
    assert transition_code == 2
    assert transition["error"]["code"] == "VERSION_CONFLICT"

    trace, trace_code = _request(
        "provenance.trace",
        {"kind": "suggestion", "artifact_id": suggestion.id},
    )
    assert trace_code != 0
    assert trace["error"]["code"] == "NOT_FOUND"


def test_capture_pause_is_private_atomic_compare_and_set(ac_root: Path) -> None:
    first = set_paused(expected_state=False, paused=True)
    assert first == {"paused": True, "changed": True}
    assert stat.S_IMODE(paths.paused_flag().stat().st_mode) == 0o600
    assert set_paused(expected_state=True, paused=True) == {
        "paused": True,
        "changed": False,
    }
    with pytest.raises(PauseStateConflict):
        set_paused(expected_state=False, paused=False)
    assert set_paused(expected_state=True, paused=False) == {
        "paused": False,
        "changed": True,
    }
    assert not paths.paused_flag().exists()


def test_candidate_bridge_enforces_cas_and_returns_fixed_envelopes(ac_root: Path) -> None:
    with fts.cursor() as conn:
        candidate = _propose(conn, content="Prefer local-first tools.")

    response, exit_code = _request("candidate.get", {"candidate_id": candidate.id})
    assert exit_code == 0
    assert set(response["result"]) == {"candidate", "evidence"}
    assert response["result"]["candidate"]["version"] == candidate.version
    assert len(response["result"]["evidence"]) == 1

    edited, exit_code = _request(
        "candidate.edit",
        {
            "candidate_id": candidate.id,
            "expected_version": candidate.version,
            "content": "Prefer reviewed local-first tools.",
            "tags": ["preference", "reviewed"],
        },
    )
    assert exit_code == 0
    assert set(edited["result"]) == {"candidate"}
    next_version = edited["result"]["candidate"]["version"]

    stale, exit_code = _request(
        "candidate.edit",
        {
            "candidate_id": candidate.id,
            "expected_version": candidate.version,
            "content": "Stale overwrite.",
            "tags": ["preference"],
        },
    )
    assert exit_code != 0
    assert stale["error"]["code"] == "VERSION_CONFLICT"

    approved, exit_code = _request(
        "candidate.approve",
        {"candidate_id": candidate.id, "expected_version": next_version},
    )
    assert exit_code == 0
    assert set(approved["result"]) == {"candidate"}
    assert approved["result"]["candidate"]["status"] == "accepted"


def test_conflict_candidate_cannot_bypass_review_ui_and_write_memory(ac_root: Path) -> None:
    target_path = "topic-conflict-boundary.md"
    with fts.cursor() as conn:
        source = _ensure_source(conn, entry_id="conflict-source")
        service = MemoryService(conn)
        first = service.propose_candidate(
            kind="topic",
            target_path=target_path,
            content="The first mutually exclusive fact.",
            tags=["topic"],
            evidence=[source],
            conflict_key="exclusive-fact",
            producer_run_key="conflict-first",
        )
        second = service.propose_candidate(
            kind="topic",
            target_path=target_path,
            content="The conflicting mutually exclusive fact.",
            tags=["topic"],
            evidence=[source],
            conflict_key="exclusive-fact",
            producer_run_key="conflict-second",
        )
    assert first.status == "pending"
    assert second.status == "conflict"

    edit_response, edit_exit_code = _request(
        "candidate.edit",
        {
            "candidate_id": second.id,
            "expected_version": second.version,
            "content": second.content,
            "tags": second.tags,
            "conflict_key": "",
        },
    )
    assert edit_exit_code != 0
    assert edit_response["error"]["code"] == "INVALID_PARAMS"

    preserved, preserved_exit_code = _request(
        "candidate.edit",
        {
            "candidate_id": second.id,
            "expected_version": second.version,
            "content": second.content,
            "tags": second.tags,
        },
    )
    assert preserved_exit_code == 0
    assert preserved["result"]["candidate"]["status"] == "conflict"
    preserved_version = preserved["result"]["candidate"]["version"]

    response, exit_code = _request(
        "candidate.approve",
        {"candidate_id": second.id, "expected_version": preserved_version},
    )

    assert exit_code != 0
    assert response["error"]["code"] == "VERSION_CONFLICT"
    with fts.cursor() as conn:
        unchanged = candidate_store.get(conn, second.id)
        assert unchanged is not None
        assert unchanged.status == "conflict"
    assert not files_store.memory_path(target_path).exists()


def test_forget_preview_digest_fences_stale_dependency_closure(ac_root: Path) -> None:
    with fts.cursor() as conn:
        root = _propose(conn, content="Root reviewed memory.")
        accepted = MemoryService(conn, cfg=config_mod.Config()).approve_candidate(
            root.id, expected_version=root.version
        )
        assert accepted.applied_entry_id

    preview_response, exit_code = _request(
        "candidate.forget_preview",
        {"candidate_id": root.id, "expected_version": accepted.version},
    )
    assert exit_code == 0
    preview = preview_response["result"]
    assert set(preview) == {
        "candidate_id",
        "expected_version",
        "candidate_ids",
        "entries",
        "files",
        "wrap_ids",
        "counts",
        "plan_digest",
    }
    assert len(preview["plan_digest"]) == 64
    assert preview["files"] == [{"path": accepted.target_path}]
    assert preview["counts"]["memory_files"] == 1

    with fts.cursor() as conn:
        derived_source = EvidenceRef(
            kind="memory_entry",
            id=accepted.applied_entry_id,
            path=accepted.target_path,
            content_hash=content_digest(accepted.content),
        )
        dependent = _propose(
            conn,
            content="A later dependent candidate.",
            source=derived_source,
            target_path="topic-dependent.md",
        )

    stale, exit_code = _request(
        "candidate.forget_commit",
        {
            "candidate_id": root.id,
            "expected_version": accepted.version,
            "plan_digest": preview["plan_digest"],
        },
    )
    assert exit_code != 0
    assert stale["error"]["code"] == "STALE_PURGE_PLAN"
    with fts.cursor() as conn:
        assert candidate_store.get(conn, root.id) is not None
        assert candidate_store.get(conn, dependent.id) is not None

    fresh_response, _exit_code = _request(
        "candidate.forget_preview",
        {"candidate_id": root.id, "expected_version": accepted.version},
    )
    fresh = fresh_response["result"]
    assert dependent.id in fresh["candidate_ids"]
    committed, exit_code = _request(
        "candidate.forget_commit",
        {
            "candidate_id": root.id,
            "expected_version": accepted.version,
            "plan_digest": fresh["plan_digest"],
        },
    )
    assert exit_code == 0
    assert committed["result"]["candidate_id"] == root.id
    assert committed["result"]["removed_file_count"] == 1
    with fts.cursor() as conn:
        assert candidate_store.get(conn, root.id) is None
        assert candidate_store.get(conn, dependent.id) is None


def test_forget_commit_retry_replays_same_authorized_tombstone(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fts.cursor() as conn:
        root = _propose(conn, content="Crash-replay private memory.")
        accepted = MemoryService(conn, cfg=config_mod.Config()).approve_candidate(
            root.id, expected_version=root.version
        )
        preview = MemoryService(conn).preview_purge_candidate(
            root.id, expected_version=accepted.version
        )
        assert preview.files == ({"path": accepted.target_path},)

    params = {
        "candidate_id": root.id,
        "expected_version": accepted.version,
        "plan_digest": preview.plan_digest,
    }
    real_execute = MemoryService._execute_purge

    def crash_after_intent(self, tombstone):
        raise RuntimeError("injected sidecar crash after purge intent")

    monkeypatch.setattr(MemoryService, "_execute_purge", crash_after_intent)
    crashed, exit_code = _request("candidate.forget_commit", params)
    assert exit_code != 0
    assert crashed["error"]["code"] == "INTERNAL_ERROR"
    with fts.cursor() as conn:
        assert candidate_store.get(conn, root.id) is not None
        assert candidate_store.is_tombstoned(conn, kind="memory_candidate", artifact_id=root.id)

    monkeypatch.setattr(MemoryService, "_execute_purge", real_execute)
    replayed, exit_code = _request("candidate.forget_commit", params)

    assert exit_code == 0
    assert replayed["result"] == {
        "candidate_id": root.id,
        "removed_entry": True,
        "removed_file_count": 1,
        "invalidated_wrap_ids": [],
    }
    with fts.cursor() as conn:
        assert candidate_store.get(conn, root.id) is None
        assert candidate_store.list_tombstones(conn) == []


def test_forget_preview_reports_unverifiable_purge_closure(ac_root: Path) -> None:
    with fts.cursor() as conn:
        root = _propose(conn, content="Root reviewed memory.")
        path = entries_store.create_file(
            conn,
            name="topic-damaged-provenance.md",
            description="Damaged provenance fixture.",
            tags=["test"],
        )
        entries_store.append_entry(
            conn,
            name=path.name,
            content="Unrelated retained text.",
            tags=["test"],
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )
        post = files_store.read_file(path)
        raw = path.read_text(encoding="utf-8")
        files_store.atomic_write_text(
            path,
            raw.replace(
                "Unrelated retained text.",
                "Unrelated retained text.\n<!-- oc-provenance: BROKEN -->",
            ),
        )
        assert post.entries[0].provenance_valid is True
        assert files_store.read_file(path).entries[0].provenance_valid is False

    response, exit_code = _request(
        "candidate.forget_preview",
        {"candidate_id": root.id, "expected_version": root.version},
    )
    assert exit_code != 0
    assert response["error"] == {
        "code": "PURGE_CLOSURE_UNVERIFIABLE",
        "message": "A damaged provenance frame prevents a safe deletion preview.",
    }


def test_evidence_resolver_is_exact_redacted_and_policy_aware(ac_root: Path) -> None:
    capture = {
        "observation_id": "obs-exact",
        "timestamp": "2026-08-08T10:00:00+00:00",
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "dev.example.editor",
            "title": "Work",
        },
        "trigger": {"event_type": "test"},
        "focused_element": {
            "role": "AXTextField",
            "title": "password=secret-value",
            "value": "Bearer abcdefghijklmnop",
            "is_editable": True,
        },
        "visible_text": "api_key=top-secret and useful visible text",
        "url": "https://secret.invalid/navigation",
        "ax_tree": {"private": "never-return-this-ax-value"},
        "screenshot": "never-return-this-screenshot",
    }
    filename = "exact.json"
    source_path = paths.capture_buffer_dir() / filename
    source_path.write_text(json.dumps(capture), encoding="utf-8")
    ref = EvidenceRef(
        kind="observation",
        id=capture["observation_id"],
        path=filename,
        timestamp=capture["timestamp"],
        content_hash=observation_digest(capture),
    )

    with fts.cursor() as conn:
        resolver = EvidenceResolver(conn, config_mod.Config())
        current = resolver.resolve(ref)
        assert current["status"] == "current"
        encoded = json.dumps(current)
        assert "never-return-this-screenshot" not in encoded
        assert "never-return-this-ax-value" not in encoded
        assert "secret.invalid" not in encoded
        assert "top-secret" not in encoded
        assert "abcdefghijklmnop" not in encoded
        assert "[REDACTED]" in encoded

        changed = resolver.resolve(
            EvidenceRef(
                kind="observation",
                id=ref.id,
                path=ref.path,
                content_hash="0" * 64,
            )
        )
        assert changed["status"] == "changed"
        assert changed["content"] is None

        excluded_cfg = config_mod.Config()
        excluded_cfg.capture.excluded_bundle_ids = ["dev.example.editor"]
        assert EvidenceResolver(conn, excluded_cfg).resolve(ref)["status"] == "excluded"

        candidate_store.put_tombstone(conn, kind="capture_file", artifact_id=filename)
        purging = resolver.resolve(ref)
        assert purging["status"] == "purging"
        assert purging["content"] is None


def test_timeline_evidence_requires_exact_hash_and_current_policy(ac_root: Path) -> None:
    start = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    block = timeline_store.TimelineBlock(
        id="tlb-exact",
        start_time=start,
        end_time=start + timedelta(minutes=1),
        timezone="UTC",
        entries=["Reviewed the bridge."],
        apps_used=["Editor"],
        capture_count=1,
    )
    capture = {
        "observation_id": "obs-timeline",
        "timestamp": start.isoformat(),
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "dev.example.editor",
            "title": "Bridge",
        },
        "trigger": {"event_type": "test"},
        "focused_element": {},
        "visible_text": "Reviewed the bridge.",
        "url": "",
    }
    capture_path = paths.capture_buffer_dir() / "timeline-source.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    observation = EvidenceRef(
        kind="observation",
        id="obs-timeline",
        path=capture_path.name,
        content_hash=observation_digest(capture),
    )
    ref = EvidenceRef(
        kind="timeline_block",
        id=block.id,
        content_hash=timeline_block_digest(
            start=block.start_time.isoformat(),
            end=block.end_time.isoformat(),
            entries=block.entries,
            apps=block.apps_used,
        ),
    )
    with fts.cursor() as conn:
        timeline_store.insert(conn, block)
        provenance_store.replace_sources(
            conn, subject=EvidenceRef(kind=ref.kind, id=ref.id), sources=[observation]
        )
        resolver = EvidenceResolver(conn, config_mod.Config())
        assert resolver.resolve(ref)["status"] == "current"
        wrong = EvidenceRef(kind=ref.kind, id=ref.id, content_hash="f" * 64)
        assert resolver.resolve(wrong)["status"] == "changed"
        candidate_store.put_tombstone(conn, kind="capture_file", artifact_id=capture_path.name)
        assert resolver.resolve(ref)["status"] == "changed"
        candidate_store.delete_tombstone(conn, kind="capture_file", artifact_id=capture_path.name)
        excluded_cfg = config_mod.Config()
        excluded_cfg.capture.excluded_app_names = ["Editor"]
        assert EvidenceResolver(conn, excluded_cfg).resolve(ref)["status"] == "excluded"


def test_snapshot_candidate_order_is_actionable_then_newest_history(ac_root: Path) -> None:
    def insert_candidate(conn, candidate_id: str, status: str, created: str, updated: str) -> None:
        conn.execute(
            """
            INSERT INTO memory_candidates(
                id, idempotency_key, proposal_digest, kind, target_path,
                content, content_hash, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'note', 'topic-order.md', ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                f"key-{candidate_id}",
                f"digest-{candidate_id}",
                candidate_id,
                content_digest(candidate_id),
                status,
                created,
                updated,
            ),
        )

    with fts.cursor() as conn:
        insert_candidate(conn, "accepted-old", "accepted", "2026-01-01", "2026-01-02")
        insert_candidate(conn, "rejected-new", "rejected", "2026-01-01", "2026-01-04")
        insert_candidate(conn, "conflict", "conflict", "2026-01-02", "2026-01-03")
        insert_candidate(conn, "pending", "pending", "2026-01-03", "2026-01-03")
        rows = candidate_store.list_review_snapshot(conn, limit=3)
    assert [row.id for row in rows] == ["pending", "conflict", "rejected-new"]


def test_wrap_get_and_provenance_trace_are_bounded_fixed_results(ac_root: Path) -> None:
    with fts.cursor() as conn:
        candidate = _propose(conn, content="Trace this candidate.")
        wrap_sources = provenance_store.direct_sources(
            conn, EvidenceRef(kind="memory_candidate", id=candidate.id)
        )
        claim = daily_wrap_store.claim(
            conn,
            local_date="2026-08-08",
            timezone="UTC",
            scope="default",
            window_start_utc="2026-08-08T00:00:00+00:00",
            window_end_utc="2026-08-09T00:00:00+00:00",
            workflow_version=1,
            coverage_status="ready",
            input_digest="digest",
            lease_token="lease",
        )
        output = {
            "schema_version": 1,
            "local_date": "2026-08-08",
            "timezone": "UTC",
            "status": "ready",
            "summary": "A local summary.",
            "coverage_gaps": [],
            "completed": [],
            "progressed": [],
            "open": [],
            "blocked": [],
            "needs_review": [],
            "generated_at": "2026-08-09T00:00:00+00:00",
        }
        daily_wrap_store.complete(
            conn,
            wrap_id=claim.row.id,
            lease_token="lease",
            input_digest="digest",
            window_start_utc="2026-08-08T00:00:00+00:00",
            window_end_utc="2026-08-09T00:00:00+00:00",
            workflow_version=1,
            coverage_status="ready",
            output=output,
            sources=wrap_sources,
            validate_input_current=lambda: None,
        )

    wrap, exit_code = _request(
        "wrap.get",
        {"local_date": "2026-08-08", "timezone": "UTC"},
    )
    assert exit_code == 0
    assert set(wrap["result"]) == {"wrap"}
    public_wrap = wrap["result"]["wrap"]
    assert public_wrap["output"]["summary"] == "A local summary."
    assert set(public_wrap) == {
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
    assert {
        "attempt_count",
        "input_digest",
        "lease_token",
        "lease_expires_at",
        "created_at",
        "updated_at",
        "completed_at",
        "last_error",
    }.isdisjoint(public_wrap)

    trace, exit_code = _request(
        "provenance.trace",
        {"kind": "memory_candidate", "artifact_id": candidate.id, "max_depth": 2},
    )
    assert exit_code == 0
    assert set(trace["result"]) == {"subject", "direct_sources", "trace"}
    assert len(trace["result"]["trace"]) <= 256
