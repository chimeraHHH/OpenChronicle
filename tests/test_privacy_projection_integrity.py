from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from openchronicle import config as config_mod
from openchronicle.capture import scheduler
from openchronicle.daily_wrap import store as daily_wrap_store
from openchronicle.daily_wrap.service import DailyWrapService
from openchronicle.mcp import captures as mcp_captures
from openchronicle.mcp import server as mcp_server
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import (
    EvidenceRef,
    content_digest,
    observation_digest,
)
from openchronicle.services.context import ContextService
from openchronicle.services.evidence import EvidenceResolver
from openchronicle.services.memory import MemoryService
from openchronicle.services.snapshot import build_snapshot
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store
from openchronicle.writer import session_reducer


def _config() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    return cfg


def _manual_source(conn) -> EvidenceRef:
    name = "user-projection-source.md"
    if not files_store.memory_path(name).exists():
        entries_store.create_file(
            conn,
            name=name,
            description="manual projection source",
            tags=["user"],
        )
    entry_id, _created = entries_store.append_entry_once(
        conn,
        name=name,
        content="Manual source for privacy projection tests.",
        tags=["manual"],
        entry_id="manual-projection-source",
        origin=files_store.MANUAL_ENTRY_ORIGIN,
    )
    return EvidenceRef(
        kind="memory_entry",
        id=entry_id,
        path=name,
        content_hash=content_digest("Manual source for privacy projection tests."),
    )


def _accepted_candidate(conn):
    cfg = _config()
    source = _manual_source(conn)
    service = MemoryService(conn, cfg=cfg)
    candidate = service.propose_candidate(
        kind="fact",
        target_path="project-owned-projection.md",
        content="CANDIDATE_OWNED_BODY_MARKER",
        tags=["derived", "projection"],
        evidence=[source],
        confidence=0.8,
        conflict_key="owned-projection",
    )
    accepted = service.approve_candidate(
        candidate.id,
        expected_version=candidate.version,
    )
    return cfg, candidate, accepted


@pytest.mark.parametrize("mutation", ["frontmatter", "path"])
def test_invalid_candidate_owned_container_hides_file_and_every_entry(
    ac_root: Path,
    mutation: str,
) -> None:
    with fts.cursor() as conn:
        cfg, candidate, accepted = _accepted_candidate(conn)
        original_path = files_store.memory_path(accepted.target_path)
        original = files_store.read_file(original_path)
        assert (
            files_store.candidate_file_owner(
                original.raw_frontmatter,
                path_name=original_path.name,
            )
            == candidate.id
        )
        entry = original.entries[0]
        original_ref = EvidenceRef(
            kind="memory_entry",
            id=entry.id,
            path=original_path.name,
            content_hash=content_digest(entry.body),
        )
        assert ContextService(conn, cfg).memory_entry_allowed(
            path=original_path.name,
            entry=entry,
        )
        assert EvidenceResolver(conn, cfg).resolve(original_ref)["status"] == "current"

        if mutation == "frontmatter":
            files_store.update_frontmatter(
                original_path,
                {"description": "TAINTED_CANDIDATE_FRONTMATTER_MARKER"},
            )
            current_path = original_path
        else:
            current_path = original_path.with_name("project-renamed-owned-projection.md")
            original_path.rename(current_path)

        tainted = files_store.read_file(current_path)
        tainted_entry = tainted.entries[0]
        tainted_ref = EvidenceRef(
            kind="memory_entry",
            id=tainted_entry.id,
            path=current_path.name,
            content_hash=content_digest(tainted_entry.body),
        )
        context = ContextService(conn, cfg)
        assert not context.memory_file_metadata_allowed(tainted)
        assert not context.memory_entry_allowed(
            path=current_path.name,
            entry=tainted_entry,
        )

        read = mcp_server._read_memory(conn, cfg=cfg, path=current_path.name)
        listed = mcp_server._list_memories(conn, cfg=cfg)
        resolved = EvidenceResolver(conn, cfg).resolve(tainted_ref)

        assert "error" in read
        assert all(row["path"] != current_path.name for row in listed["files"])
        assert resolved["status"] != "current"
        serialized = json.dumps(
            {"read": read, "listed": listed, "resolved": resolved},
            ensure_ascii=False,
        )
        assert "CANDIDATE_OWNED_BODY_MARKER" not in serialized
        assert "TAINTED_CANDIDATE_FRONTMATTER_MARKER" not in serialized


def test_mcp_list_rejects_each_stale_file_projection_field(ac_root: Path) -> None:
    name = "project-canonical-listing.md"
    cfg = _config()
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name=name,
            description="canonical description",
            tags=["canonical-tag"],
        )
        entries_store.append_entry_once(
            conn,
            name=name,
            content="Canonical manual listing body.",
            tags=["manual"],
            entry_id="canonical-listing-entry",
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )
        assert mcp_server._list_memories(conn, cfg=cfg)["count"] == 1

        mutations: tuple[tuple[str, object], ...] = (
            ("path", "project-stale-path-marker.md"),
            ("description", "STALE_DESCRIPTION_MARKER"),
            ("tags", "STALE_TAG_MARKER"),
            ("entry_count", 424242),
        )
        for column, value in mutations:
            conn.execute(f"UPDATE files SET {column}=? WHERE path=?", (value, name))

            listed = mcp_server._list_memories(conn, cfg=cfg)

            assert listed == {"count": 0, "files": []}
            serialized = json.dumps(listed, ensure_ascii=False)
            assert "project-stale-path-marker.md" not in serialized
            assert "STALE_DESCRIPTION_MARKER" not in serialized
            assert "STALE_TAG_MARKER" not in serialized
            assert "424242" not in serialized

            entries_store.rebuild_index(conn)
            restored = mcp_server._list_memories(conn, cfg=cfg)
            assert [row["path"] for row in restored["files"]] == [name]


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("kind", "tampered-kind"),
        ("operation", "replace"),
        ("target_path", "project-tampered-target.md"),
        ("content", "TAMPERED_CANDIDATE_CONTENT"),
        ("content_hash", "f" * 64),
        ("tags_json", '["TAMPERED_CANDIDATE_TAG"]'),
        ("confidence", 0.1),
        ("conflict_key", "tampered-conflict"),
    ],
)
def test_candidate_semantic_sql_tamper_is_excluded_everywhere(
    ac_root: Path,
    column: str,
    value: object,
) -> None:
    cfg = _config()
    with fts.cursor() as conn:
        source = _manual_source(conn)
        candidate = MemoryService(conn, cfg=cfg).propose_candidate(
            kind="preference",
            target_path="user-projection-integrity.md",
            content="Original candidate projection.",
            tags=["preference", "integrity"],
            evidence=[source],
            confidence=0.9,
            conflict_key="projection-integrity",
        )
        ref = EvidenceRef(kind="memory_candidate", id=candidate.id)
        context = ContextService(conn, cfg)
        resolver = EvidenceResolver(conn, cfg)
        assert context.evidence_allowed(ref)
        assert resolver.resolve(ref)["status"] == "current"

        conn.execute(
            f"UPDATE memory_candidates SET {column}=? WHERE id=?",
            (value, candidate.id),
        )

        changed = candidate_store.get(conn, candidate.id)
        assert changed is not None
        assert not candidate_store.projection_is_current(changed)
        assert not context.evidence_allowed(ref)
        resolved = resolver.resolve(ref)
        assert resolved["status"] == "changed"
        assert "TAMPERED_CANDIDATE" not in json.dumps(resolved, ensure_ascii=False)


def test_candidate_source_edge_swap_cannot_rebind_private_output(
    ac_root: Path,
) -> None:
    cfg = _config()
    marker = "CANDIDATE_SOURCE_BOUND_PRIVATE_OUTPUT"
    with fts.cursor() as conn:
        source_a = _manual_source(conn)
        replacement_path = "user-candidate-replacement-source.md"
        entries_store.create_file(
            conn,
            name=replacement_path,
            description="unrelated replacement source",
            tags=["user"],
        )
        replacement_body = "Unrelated current manual source."
        replacement_id = entries_store.append_entry(
            conn,
            name=replacement_path,
            content=replacement_body,
            tags=["manual"],
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )
        source_b = EvidenceRef(
            kind="memory_entry",
            id=replacement_id,
            path=replacement_path,
            content_hash=content_digest(replacement_body),
        )
        service = MemoryService(conn, cfg=cfg)
        candidate = service.propose_candidate(
            kind="fact",
            target_path="project-source-bound.md",
            content=marker,
            tags=["derived"],
            evidence=[source_a],
            confidence=0.9,
            conflict_key="source-bound",
        )
        ref = EvidenceRef(kind="memory_candidate", id=candidate.id)
        provenance_store.replace_sources(conn, subject=ref, sources=[source_b])

        rebound = candidate_store.get(conn, candidate.id)
        assert rebound is not None
        assert candidate_store.projection_is_current(rebound)
        assert not candidate_store.proposal_is_current(rebound, [source_b])
        assert not ContextService(conn, cfg).evidence_allowed(ref)
        resolved = EvidenceResolver(conn, cfg).resolve(ref)
        snapshot = build_snapshot(
            conn,
            cfg,
            timeline_limit=0,
            candidate_limit=100,
            wrap_limit=0,
        )

        assert resolved["status"] == "changed"
        assert marker not in json.dumps(resolved, ensure_ascii=False)
        assert marker not in json.dumps(snapshot, ensure_ascii=False)
        with pytest.raises(candidate_store.CandidateConflict):
            service.approve_candidate(
                candidate.id,
                expected_version=candidate.version,
            )


def _observation(marker: str, *, timestamp: str | None = None) -> tuple[EvidenceRef, Path]:
    timestamp = timestamp or scheduler._now_iso()
    capture = {
        "timestamp": timestamp,
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
            timestamp=timestamp,
            content_hash=observation_digest(capture),
        ),
        path,
    )


@pytest.mark.parametrize(
    ("column", "value", "active_id"),
    [
        ("id", "tlb-sql-tampered-id", "tlb-sql-tampered-id"),
        ("start_time", "2026-08-08T09:59:00+00:00", "tlb-projection-integrity"),
        ("end_time", "2026-08-08T10:02:00+00:00", "tlb-projection-integrity"),
        ("timezone", "TAMPERED_TIMELINE_TIMEZONE", "tlb-projection-integrity"),
        ("entries", '["TAMPERED_TIMELINE_ENTRY"]', "tlb-projection-integrity"),
        ("entries", "", "tlb-projection-integrity"),
        ("apps_used", '["TAMPERED_TIMELINE_APP"]', "tlb-projection-integrity"),
        ("apps_used", "", "tlb-projection-integrity"),
        ("capture_count", 31337, "tlb-projection-integrity"),
        ("capture_count", 1.9, "tlb-projection-integrity"),
        ("capture_count", "not-a-number", "tlb-projection-integrity"),
        ("created_at", "2026-08-08T12:34:56+00:00", "tlb-projection-integrity"),
        ("projection_digest", "f" * 64, "tlb-projection-integrity"),
    ],
)
def test_timeline_sql_projection_tamper_is_excluded_everywhere(
    ac_root: Path,
    column: str,
    value: object,
    active_id: str,
) -> None:
    start = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    source, _capture_path = _observation(
        "Canonical timeline source.",
        timestamp=start.isoformat(),
    )
    block = timeline_store.TimelineBlock(
        id="tlb-projection-integrity",
        start_time=start,
        end_time=start + timedelta(minutes=1),
        timezone="UTC",
        entries=["Canonical timeline entry."],
        apps_used=["Editor"],
        capture_count=1,
    )
    cfg = _config()
    with fts.cursor() as conn:
        timeline_store.insert(conn, block)
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block.id),
            sources=[source],
        )
        assert ContextService(conn, cfg).evidence_allowed(
            EvidenceRef(kind="timeline_block", id=block.id)
        )

        conn.execute(
            f"UPDATE timeline_blocks SET {column}=? WHERE id=?",
            (value, block.id),
        )

        assert timeline_store.get_by_id(conn, active_id) is None
        assert timeline_store.query_recent(conn) == []
        assert not ContextService(conn, cfg).evidence_allowed(
            EvidenceRef(kind="timeline_block", id=active_id)
        )
        resolved = EvidenceResolver(conn, cfg).resolve(
            EvidenceRef(kind="timeline_block", id=active_id)
        )
        assert resolved["status"] != "current"

    current = mcp_captures.current_context(cfg=cfg, timeline_limit=10)
    assert current["recent_timeline_blocks"] == []
    serialized = json.dumps(current, ensure_ascii=False)
    assert "TAMPERED_TIMELINE" not in serialized
    assert "31337" not in serialized


def test_timeline_source_edge_swap_cannot_rebind_private_output(
    ac_root: Path,
) -> None:
    start = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    source_a, _capture_path = _observation(
        "TIMELINE_SOURCE_BOUND_PRIVATE_INPUT",
        timestamp=start.isoformat(),
    )
    marker = "TIMELINE_SOURCE_BOUND_PRIVATE_OUTPUT"
    cfg = _config()
    with fts.cursor() as conn:
        source_b = _manual_source(conn)
        block = timeline_store.TimelineBlock(
            id="tlb-source-binding-integrity",
            start_time=start,
            end_time=start + timedelta(minutes=1),
            timezone="UTC",
            entries=[marker],
            apps_used=["Editor"],
            capture_count=1,
        )
        timeline_store.insert(conn, block)
        ref = EvidenceRef(kind="timeline_block", id=block.id)
        provenance_store.replace_sources(conn, subject=ref, sources=[source_a])
        assert timeline_store.get_by_id(conn, block.id) is not None
        assert ContextService(conn, cfg).evidence_allowed(ref)
        assert not ContextService(conn, cfg).evidence_allowed(
            ref,
            embedded_sources=[source_b],
        )

        provenance_store.replace_sources(conn, subject=ref, sources=[source_b])

        assert timeline_store.get_by_id(conn, block.id) is None
        assert not ContextService(conn, cfg).evidence_allowed(ref)
        with pytest.raises(session_reducer.TimelineProjectionInvalid):
            session_reducer._blocks_for_session(
                conn,
                start,
                start + timedelta(minutes=1),
                complete_only=False,
            )
        day_context = ContextService(conn, cfg).for_day(date(2026, 8, 8), "UTC")
        resolved = EvidenceResolver(conn, cfg).resolve(ref)
        current = mcp_captures.current_context(cfg=cfg, timeline_limit=10)
        snapshot = build_snapshot(
            conn,
            cfg,
            timeline_limit=10,
            candidate_limit=0,
            wrap_limit=0,
        )

        assert all(record.evidence.id != block.id for record in day_context.records)
        assert resolved["status"] != "current"
        serialized = json.dumps(
            {
                "day": [record.prompt_dict() for record in day_context.records],
                "resolved": resolved,
                "current": current,
                "snapshot": snapshot,
            },
            ensure_ascii=False,
        )
        assert marker not in serialized


@pytest.mark.parametrize("column", ["source_kind", "source_id"])
def test_malformed_timeline_edge_quarantines_without_crashing(
    ac_root: Path,
    column: str,
) -> None:
    start = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    source, _capture_path = _observation(
        "MALFORMED_EDGE_PRIVATE_INPUT",
        timestamp=start.isoformat(),
    )
    block = timeline_store.TimelineBlock(
        id=f"tlb-malformed-edge-{column}",
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=["MALFORMED_EDGE_PRIVATE_OUTPUT"],
        capture_count=1,
    )
    with fts.cursor() as conn:
        timeline_store.insert(conn, block)
        ref = EvidenceRef(kind="timeline_block", id=block.id)
        provenance_store.replace_sources(conn, subject=ref, sources=[source])
        conn.execute(
            f"UPDATE provenance_edges SET {column}='' "
            "WHERE subject_kind='timeline_block' AND subject_id=?",
            (block.id,),
        )

        assert timeline_store.get_by_id(conn, block.id) is None
        assert timeline_store.get_window(conn, block.start_time, block.end_time) is None
        assert timeline_store.window_state(conn, block.start_time, block.end_time) == "invalid"
        assert all(item.id != block.id for item in timeline_store.query_recent(conn))
        assert provenance_store.direct_sources(conn, ref) == []


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
        input_digest="privacy-binding-input",
        lease_token="privacy-binding-lease",
    )
    output = {
        "schema_version": 1,
        "local_date": "2026-08-08",
        "timezone": "UTC",
        "status": "ready",
        "summary": "PRIVACY_BOUND_WRAP_OUTPUT",
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
        lease_token="privacy-binding-lease",
        input_digest="privacy-binding-input",
        window_start_utc="2026-08-08T00:00:00+00:00",
        window_end_utc="2026-08-09T00:00:00+00:00",
        workflow_version=1,
        coverage_status="ready",
        output=output,
        sources=[source],
        validate_input_current=lambda: None,
    )
    return row, output


_TAMPERED_WRAP_OUTPUT = json.dumps(
    {
        "schema_version": 1,
        "local_date": "2026-08-08",
        "timezone": "UTC",
        "status": "ready",
        "summary": "TAMPERED_WRAP_OUTPUT",
        "completed": [],
        "progressed": [],
        "open": [],
        "blocked": [],
        "needs_review": [],
        "coverage_gaps": [],
        "generated_at": "2026-08-09T00:05:00+00:00",
    },
    sort_keys=True,
)


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("daily_wrap_jobs", "local_date", "2026-08-07"),
        ("daily_wrap_jobs", "timezone", "Asia/Shanghai"),
        ("daily_wrap_jobs", "scope", "tampered-scope"),
        ("daily_wrap_jobs", "window_start_utc", "2026-08-08T01:00:00+00:00"),
        ("daily_wrap_jobs", "window_end_utc", "2026-08-08T23:00:00+00:00"),
        ("daily_wrap_jobs", "workflow_version", 2),
        ("daily_wrap_jobs", "published_input_digest", "tampered-input"),
        ("daily_wrap_jobs", "output_json", _TAMPERED_WRAP_OUTPUT),
        ("daily_wrap_jobs", "coverage_status", "partial"),
        ("daily_wrap_revisions", "local_date", "2026-08-07"),
        ("daily_wrap_revisions", "timezone", "Asia/Shanghai"),
        ("daily_wrap_revisions", "scope", "tampered-scope"),
        (
            "daily_wrap_revisions",
            "window_start_utc",
            "2026-08-08T01:00:00+00:00",
        ),
        (
            "daily_wrap_revisions",
            "window_end_utc",
            "2026-08-08T23:00:00+00:00",
        ),
        ("daily_wrap_revisions", "workflow_version", 2),
        ("daily_wrap_revisions", "input_digest", "tampered-input"),
        ("daily_wrap_revisions", "output_json", _TAMPERED_WRAP_OUTPUT),
        ("daily_wrap_revisions", "coverage_status", "partial"),
        ("daily_wrap_revisions", "source_digest", "tampered-source-digest"),
    ],
)
def test_daily_wrap_job_and_revision_tamper_is_hidden(
    ac_root: Path,
    table: str,
    column: str,
    value: object,
) -> None:
    source, _capture_path = _observation("PRIVACY_WRAP_SOURCE")
    with fts.cursor() as conn:
        row, _output = _published_wrap(conn, source)
        cfg = _config()
        assert ContextService(conn, cfg).daily_wrap_allowed(
            row.id,
            expected_row=row,
        )

        predicate = "id=?" if table == "daily_wrap_jobs" else "wrap_id=? AND revision=1"
        conn.execute(
            f"UPDATE {table} SET {column}=? WHERE {predicate}",
            (value, row.id),
        )

        changed = daily_wrap_store.get_by_id(conn, row.id)
        assert changed is not None
        assert not ContextService(conn, cfg).daily_wrap_allowed(
            row.id,
            expected_row=changed,
        )
        resolved = EvidenceResolver(conn, cfg).resolve(EvidenceRef(kind="daily_wrap", id=row.id))
        read = mcp_server._get_daily_wrap(
            conn,
            cfg=cfg,
            local_date="2026-08-08",
            timezone="UTC",
        )

        assert resolved["status"] == "excluded"
        assert "error" in read
        serialized = json.dumps(
            {"resolved": resolved, "read": read},
            ensure_ascii=False,
        )
        assert "PRIVACY_BOUND_WRAP_OUTPUT" not in serialized
        assert "TAMPERED_WRAP_OUTPUT" not in serialized


def test_daily_wrap_revision_source_tamper_is_hidden(ac_root: Path) -> None:
    source_a, _capture_a = _observation("PRIVACY_WRAP_SOURCE_A")
    source_b, _capture_b = _observation("PRIVACY_WRAP_SOURCE_B")
    with fts.cursor() as conn:
        row, _output = _published_wrap(conn, source_a)
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(
                kind="daily_wrap_revision",
                id=f"{row.id}:r{row.revision}",
                path=row.id,
            ),
            sources=[source_b],
        )

        cfg = _config()
        assert not ContextService(conn, cfg).daily_wrap_allowed(row.id)
        resolved = EvidenceResolver(conn, cfg).resolve(EvidenceRef(kind="daily_wrap", id=row.id))
        assert resolved["status"] == "excluded"
        assert "PRIVACY_BOUND_WRAP_OUTPUT" not in json.dumps(resolved)


def _seed_wrap_day(conn, day: date) -> None:
    start = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=10)
    source, _capture_path = _observation(
        "Edited privacy binding tests.",
        timestamp=start.isoformat(),
    )
    block = timeline_store.TimelineBlock(
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=["Edited privacy binding tests."],
        apps_used=["Editor"],
        capture_count=1,
    )
    timeline_store.insert(conn, block)
    provenance_store.replace_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id=block.id),
        sources=[source],
    )
    day_start = datetime.combine(day, datetime.min.time(), UTC)
    conn.execute(
        """
        INSERT INTO timeline_state(id, processed_from, processed_through)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            processed_from=excluded.processed_from,
            processed_through=excluded.processed_through
        """,
        (
            (day_start - timedelta(minutes=1)).isoformat(),
            (day_start + timedelta(days=1)).isoformat(),
        ),
    )


def test_invalid_cached_wrap_binding_forces_regeneration(ac_root: Path) -> None:
    day = date(2026, 8, 8)
    calls = 0

    def fake_llm(cfg, stage, *, messages, tools=None, json_mode=False):  # noqa: ARG001
        nonlocal calls
        calls += 1
        payload = {
            "completed": [],
            "progressed": [],
            "open": [],
            "blocked": [],
            "needs_review": [],
        }
        message = SimpleNamespace(content=json.dumps(payload), tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    with fts.cursor() as conn:
        _seed_wrap_day(conn, day)
        cfg = _config()
        service = DailyWrapService(conn, cfg, llm_caller=fake_llm)
        first = service.run(day, "UTC")
        assert first.revision == 1
        assert calls == 1

        conn.execute(
            """
            UPDATE daily_wrap_revisions
               SET scope='tampered-cached-scope'
             WHERE wrap_id=? AND revision=?
            """,
            (first.id, first.revision),
        )
        assert not ContextService(conn, cfg).daily_wrap_allowed(
            first.id,
            expected_row=first,
        )

        regenerated = service.run(day, "UTC")

        assert calls == 2
        assert regenerated.id == first.id
        assert regenerated.revision == 2
        assert regenerated.attempt_count == 2
        assert ContextService(conn, cfg).daily_wrap_allowed(
            regenerated.id,
            expected_row=regenerated,
        )


def test_concurrent_invalid_cache_refresh_uses_one_cas_repair(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = date(2026, 8, 8)

    def response(cfg, stage, *, messages, tools=None, json_mode=False):  # noqa: ARG001
        payload = {
            "completed": [],
            "progressed": [],
            "open": [],
            "blocked": [],
            "needs_review": [],
        }
        message = SimpleNamespace(content=json.dumps(payload), tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    with fts.cursor() as conn:
        _seed_wrap_day(conn, day)
        cfg = _config()
        first = DailyWrapService(conn, cfg, llm_caller=response).run(day, "UTC")
        conn.execute(
            """
            UPDATE daily_wrap_revisions
               SET scope='tampered-concurrent-cache'
             WHERE wrap_id=? AND revision=?
            """,
            (first.id, first.revision),
        )

    real_claim = daily_wrap_store.claim
    real_complete = daily_wrap_store.complete
    force_barrier = threading.Barrier(2)
    repair_completed = threading.Event()
    coordination_lock = threading.Lock()
    forced_call_count = 0
    provider_call_count = 0

    def coordinated_claim(*args, **kwargs):
        nonlocal forced_call_count
        if kwargs.get("force_refresh"):
            with coordination_lock:
                forced_call_count += 1
                call_number = forced_call_count
            force_barrier.wait(timeout=5)
            if call_number == 2 and not repair_completed.wait(timeout=5):
                raise AssertionError("first invalid-cache repair did not complete")
        return real_claim(*args, **kwargs)

    def coordinated_complete(*args, **kwargs):
        result = real_complete(*args, **kwargs)
        repair_completed.set()
        return result

    def counted_response(*args, **kwargs):
        nonlocal provider_call_count
        with coordination_lock:
            provider_call_count += 1
        return response(*args, **kwargs)

    monkeypatch.setattr(daily_wrap_store, "claim", coordinated_claim)
    monkeypatch.setattr(daily_wrap_store, "complete", coordinated_complete)

    def refresh():
        with fts.cursor() as conn:
            return DailyWrapService(conn, _config(), llm_caller=counted_response).run(day, "UTC")

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = [
            future.result(timeout=10) for future in (pool.submit(refresh), pool.submit(refresh))
        ]

    assert [row.revision for row in rows] == [2, 2]
    assert forced_call_count == 2
    assert provider_call_count == 1
    with fts.cursor() as conn:
        current = daily_wrap_store.get_by_id(conn, first.id)
        assert current is not None
        assert current.status == "succeeded"
        assert current.revision == 2
        assert current.attempt_count == 2
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM daily_wrap_revisions WHERE wrap_id=?",
                (first.id,),
            ).fetchone()[0]
            == 2
        )


def test_force_refresh_cas_miss_never_returns_a_different_input_digest(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = date(2026, 8, 8)
    with fts.cursor() as conn:
        baseline = daily_wrap_store.DailyWrapRow(
            id=daily_wrap_store.make_id(day.isoformat(), "UTC"),
            local_date=day.isoformat(),
            timezone="UTC",
            scope="default",
            window_start_utc="2026-08-08T00:00:00+00:00",
            window_end_utc="2026-08-09T00:00:00+00:00",
            workflow_version=1,
            status="succeeded",
            coverage_status="partial",
            attempt_count=1,
            lease_token=None,
            lease_expires_at=None,
            input_digest="original-input",
            published_input_digest="original-input",
            output={},
            revision=1,
            created_at="2026-08-09T00:00:00+00:00",
            updated_at="2026-08-09T00:00:00+00:00",
            completed_at="2026-08-09T00:00:00+00:00",
            last_error="",
        )
        invalid = baseline
        concurrent = replace(
            baseline,
            revision=2,
            published_input_digest="different-current-input",
        )
        calls = 0

        def claim_sequence(*_args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                assert not kwargs.get("force_refresh")
                return daily_wrap_store.ClaimResult(row=invalid, claimed=False)
            if calls == 2:
                assert kwargs.get("force_refresh")
                return daily_wrap_store.ClaimResult(row=concurrent, claimed=False)
            raise RuntimeError("different digest was retried")

        monkeypatch.setattr(daily_wrap_store, "claim", claim_sequence)
        monkeypatch.setattr(
            ContextService,
            "daily_wrap_allowed",
            lambda _self, _wrap_id, *, expected_row=None, **_kwargs: bool(
                expected_row is not None and expected_row.revision == 2
            ),
        )

        with pytest.raises(RuntimeError, match="different digest was retried"):
            DailyWrapService(conn, _config()).run(day, "UTC")

    assert calls == 3
