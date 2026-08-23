from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle import desktop_bridge
from openchronicle.capture import scheduler
from openchronicle.daily_wrap import store as daily_wrap_store
from openchronicle.mcp import captures as mcp_captures
from openchronicle.mcp import server as mcp_server
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import (
    EvidenceRef,
    observation_digest,
    timeline_block_digest,
)
from openchronicle.services.context import ContextService
from openchronicle.services.memory import MemoryService
from openchronicle.services.snapshot import build_snapshot
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.timeline import aggregator
from openchronicle.timeline import store as timeline_store

_BUNDLE = "com.example.private"
_MARKER = "POLICY_CHANGED_PRIVATE_MARKER"
_QUERY = "POLICY CHANGED PRIVATE"


def _seed_policy_graph(conn) -> tuple[config_mod.Config, object, str, str]:
    cfg = config_mod.Config()
    timestamp = scheduler._now_iso()
    capture = {
        "timestamp": timestamp,
        "schema_version": 4,
        "window_meta": {
            "app_name": "PrivateApp",
            "bundle_id": _BUNDLE,
            "title": _MARKER,
        },
        "focused_element": {
            "role": "AXTextArea",
            "value": _MARKER,
        },
        "visible_text": _MARKER,
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
    start = datetime.fromisoformat(timestamp)
    block = timeline_store.TimelineBlock(
        id="tlb-policy-egress",
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=[_MARKER],
        apps_used=["PrivateApp"],
        capture_count=1,
    )
    timeline_store.insert(conn, block)
    block_ref = EvidenceRef(
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
    provenance_store.replace_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id=block.id),
        sources=[observation],
    )

    derived_path = "project-policy-derived.md"
    entries_store.create_file(
        conn,
        name=derived_path,
        description="derived private fixture",
        tags=["project"],
    )
    entries_store.append_entry_once(
        conn,
        name=derived_path,
        content=_MARKER,
        tags=["private"],
        entry_id="policy-derived-entry",
        evidence_refs=[block_ref],
    )
    manual_path = "user-manual-policy.md"
    entries_store.create_file(
        conn,
        name=manual_path,
        description="manual fixture",
        tags=["user"],
    )
    entries_store.append_entry_once(
        conn,
        name=manual_path,
        content="MANUAL_MEMORY_REMAINS_VISIBLE",
        tags=["manual"],
        entry_id="manual-policy-entry",
        origin=files_store.MANUAL_ENTRY_ORIGIN,
    )

    candidate = MemoryService(conn).propose_candidate(
        kind="fact",
        target_path="project-policy-approved.md",
        content=_MARKER,
        tags=["private"],
        evidence=[block_ref],
        producer_run_key="policy-egress-candidate",
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
        input_digest="policy-egress-wrap",
        lease_token="policy-egress-lease",
    )
    daily_wrap_store.complete(
        conn,
        wrap_id=claim.row.id,
        lease_token="policy-egress-lease",
        input_digest="policy-egress-wrap",
        window_start_utc="2026-08-08T00:00:00+00:00",
        window_end_utc="2026-08-09T00:00:00+00:00",
        workflow_version=1,
        coverage_status="ready",
        output={
            "schema_version": 1,
            "summary": _MARKER,
            "completed": [],
            "progressed": [],
            "open": [],
            "blocked": [],
            "needs_review": [],
        },
        sources=[block_ref],
        validate_input_current=lambda: None,
    )
    return cfg, candidate, derived_path, claim.row.id


def test_policy_change_hides_raw_memory_wrap_and_provenance_mcp_egress(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        cfg, _candidate, derived_path, _wrap_id = _seed_policy_graph(conn)

    assert mcp_captures.read_recent_capture(cfg=cfg) is not None
    assert mcp_captures.search_captures(cfg=cfg, query=_QUERY)
    cfg.capture.excluded_bundle_ids = [_BUNDLE]

    with fts.cursor() as conn:
        outputs = {
            "read_memory": mcp_server._read_memory(conn, cfg=cfg, path=derived_path),
            "search_memory": mcp_server._search(conn, cfg=cfg, query=_QUERY),
            "recent": mcp_server._recent_activity(conn, cfg=cfg),
            "list": mcp_server._list_memories(conn, cfg=cfg),
            "provenance": mcp_server._get_provenance(
                conn,
                cfg=cfg,
                kind="memory_entry",
                artifact_id="policy-derived-entry",
                path=derived_path,
            ),
            "wrap": mcp_server._get_daily_wrap(
                conn,
                cfg=cfg,
                local_date="2026-08-08",
                timezone="UTC",
            ),
            "wraps": mcp_server._list_daily_wraps(conn, cfg=cfg),
            "manual": mcp_server._read_memory(conn, cfg=cfg, path="user-manual-policy.md"),
        }

    raw_outputs = {
        "recent_capture": mcp_captures.read_recent_capture(cfg=cfg),
        "capture_search": mcp_captures.search_captures(cfg=cfg, query=_QUERY),
        "context": mcp_captures.current_context(cfg=cfg),
    }
    assert _MARKER not in json.dumps({**outputs, **raw_outputs})
    assert "error" in outputs["read_memory"]
    assert outputs["search_memory"]["results"] == []
    assert outputs["wraps"]["wraps"] == []
    assert "error" in outputs["provenance"]
    assert "MANUAL_MEMORY_REMAINS_VISIBLE" in json.dumps(outputs["manual"])
    assert raw_outputs["recent_capture"] is None
    assert raw_outputs["capture_search"] == []


def test_policy_change_hides_desktop_snapshot_and_fences_candidate_approval(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        cfg, candidate, _derived_path, _wrap_id = _seed_policy_graph(conn)
        cfg.capture.excluded_bundle_ids = [_BUNDLE]
        snapshot = build_snapshot(
            conn,
            cfg,
            timeline_limit=12,
            candidate_limit=50,
            wrap_limit=14,
        )
        encoded = json.dumps(snapshot)
        assert _MARKER not in encoded
        assert snapshot["capture"]["last"] is None
        assert snapshot["timeline"] == []
        assert snapshot["candidates"] == []
        assert snapshot["daily_wrap"]["wraps"] == []

        with pytest.raises(candidate_store.CandidateConflict):
            MemoryService(conn, cfg=cfg).approve_candidate(
                candidate.id,
                expected_version=candidate.version,
            )
        current = candidate_store.get(conn, candidate.id)
        assert current is not None and current.status == "conflict"
        assert not files_store.memory_path(candidate.target_path).exists()


def test_policy_change_hides_desktop_detail_endpoints(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with fts.cursor() as conn:
        cfg, candidate, derived_path, _wrap_id = _seed_policy_graph(conn)
    cfg.capture.excluded_bundle_ids = [_BUNDLE]
    monkeypatch.setattr(desktop_bridge.config_mod, "load", lambda: cfg)

    def request(operation: str, params: dict[str, object]) -> tuple[dict, int]:
        return desktop_bridge.handle_request_bytes(
            json.dumps(
                {
                    "version": desktop_bridge.PROTOCOL_VERSION,
                    "operation": operation,
                    "params": params,
                }
            ).encode()
        )

    responses = [
        request("candidate.get", {"candidate_id": candidate.id}),
        request(
            "wrap.get",
            {"local_date": "2026-08-08", "timezone": "UTC"},
        ),
        request(
            "provenance.trace",
            {
                "kind": "memory_entry",
                "artifact_id": "policy-derived-entry",
                "path": derived_path,
            },
        ),
        request(
            "candidate.approve",
            {
                "candidate_id": candidate.id,
                "expected_version": candidate.version,
            },
        ),
    ]
    assert all(exit_code != 0 for _response, exit_code in responses)
    assert _MARKER not in json.dumps(responses)
    assert responses[0][0]["error"]["code"] == "NOT_FOUND"
    assert responses[1][0]["error"]["code"] == "NOT_FOUND"
    assert responses[2][0]["error"]["code"] == "NOT_FOUND"
    assert responses[3][0]["error"]["code"] == "VERSION_CONFLICT"
    assert not files_store.memory_path(candidate.target_path).exists()


def test_candidate_approval_rechecks_policy_inside_publication_fence(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with fts.cursor() as conn:
        cfg, candidate, _derived_path, _wrap_id = _seed_policy_graph(conn)
        real_allowed = ContextService.evidence_allowed
        candidate_checks = 0

        def policy_changes_between_checks(
            self: ContextService,
            subject: EvidenceRef,
            *,
            embedded_sources: list[EvidenceRef] | None = None,
        ) -> bool:
            nonlocal candidate_checks
            if subject.kind == "memory_candidate":
                candidate_checks += 1
                return candidate_checks == 1
            return real_allowed(self, subject, embedded_sources=embedded_sources)

        monkeypatch.setattr(
            ContextService,
            "evidence_allowed",
            policy_changes_between_checks,
        )
        with pytest.raises(candidate_store.CandidateConflict):
            MemoryService(conn, cfg=cfg).approve_candidate(
                candidate.id,
                expected_version=candidate.version,
            )

        assert candidate_checks == 2
        current = candidate_store.get(conn, candidate.id)
        assert current is not None and current.status == "conflict"
        assert not files_store.memory_path(candidate.target_path).exists()


def test_timeline_screenshot_projection_preserves_authoritative_source_hash(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime.now().astimezone().replace(microsecond=0)
    capture = {
        "timestamp": start.isoformat(),
        "schema_version": 4,
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "title": "Hash binding",
        },
        "visible_text": "bounded prompt text",
        "url": "",
        "screenshot": {
            "mime_type": "image/jpeg",
            "image_base64": "aGVsbG8=",
        },
    }
    capture_path = scheduler._write_capture(capture)
    expected_digest = observation_digest(capture)
    parsed = aggregator._load_captures([capture_path], drop_screenshot=True)
    assert "screenshot" not in parsed[0][1]
    legacy_response = mcp_captures._format_response(capture_path, capture, include_screenshot=True)
    assert "screenshot_b64" not in legacy_response
    assert "screenshot_mime" not in legacy_response
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK_JSON", '{"entries":["bounded prompt text"]}')

    with fts.cursor() as conn:
        block = aggregator.produce_block_for_window(
            config_mod.Config(),
            conn,
            start=start,
            end=start + timedelta(minutes=1),
            parsed_captures=parsed,
        )
        assert block is not None
        sources = provenance_store.direct_sources(
            conn, EvidenceRef(kind="timeline_block", id=block.id)
        )
        assert len(sources) == 1
        assert sources[0].content_hash == expected_digest
        assert provenance_store.is_current(conn, sources[0])
