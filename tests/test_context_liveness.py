from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from openchronicle import config as config_mod
from openchronicle.capture import scheduler
from openchronicle.mcp import captures as mcp_captures
from openchronicle.mcp import server as mcp_server
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import (
    EvidenceRef,
    content_digest,
    observation_digest,
    timeline_block_digest,
)
from openchronicle.services.context import ContextService
from openchronicle.services.evidence import EvidenceResolver
from openchronicle.services.memory import MemoryService
from openchronicle.session import store as session_store
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store


def _unrestricted_config() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    return cfg


def test_unrestricted_policy_still_requires_live_raw_ancestry(ac_root: Path) -> None:
    cfg = _unrestricted_config()
    timestamp = scheduler._now_iso()
    capture = {
        "timestamp": timestamp,
        "schema_version": 4,
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "title": "Liveness fixture",
        },
        "focused_element": {"role": "AXTextArea", "value": "LIVE_ONLY"},
        "visible_text": "LIVE_ONLY",
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
        id="tlb-unrestricted-liveness",
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=["LIVE_ONLY"],
        apps_used=["Editor"],
        capture_count=1,
    )
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

    with fts.cursor() as conn:
        timeline_store.insert(conn, block)
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block.id),
            sources=[observation],
        )
        entries_store.create_file(
            conn,
            name="project-live-ancestry.md",
            description="liveness fixture",
            tags=["project"],
        )
        entries_store.append_entry_once(
            conn,
            name="project-live-ancestry.md",
            content="LIVE_ONLY",
            tags=["derived"],
            entry_id="derived-live-ancestry",
            evidence_refs=[block_ref],
        )
        parsed = files_store.read_file(
            files_store.memory_path("project-live-ancestry.md")
        )
        context = ContextService(conn, cfg)
        assert context.memory_entry_allowed(
            path=parsed.path.name, entry=parsed.entries[0]
        )

        capture_path.unlink()

        assert not context.memory_entry_allowed(
            path=parsed.path.name, entry=parsed.entries[0]
        )


def test_unrestricted_policy_does_not_trust_a_candidate_after_edges_disappear(
    ac_root: Path,
) -> None:
    cfg = _unrestricted_config()
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="user-candidate-source.md",
            description="manual source",
            tags=["user"],
        )
        entry_id, _created = entries_store.append_entry_once(
            conn,
            name="user-candidate-source.md",
            content="USER_APPROVED_SOURCE",
            tags=["manual"],
            entry_id="manual-candidate-source",
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )
        source = EvidenceRef(
            kind="memory_entry",
            id=entry_id,
            path="user-candidate-source.md",
            content_hash=content_digest("USER_APPROVED_SOURCE"),
        )
        candidate = MemoryService(conn).propose_candidate(
            kind="fact",
            target_path="user-derived-candidate.md",
            content="DERIVED_CANDIDATE",
            tags=["derived"],
            evidence=[source],
        )
        subject = EvidenceRef(kind="memory_candidate", id=candidate.id)
        context = ContextService(conn, cfg)
        assert context.evidence_allowed(subject)

        provenance_store.delete_subject(conn, subject)

        assert not context.evidence_allowed(subject)


def test_manual_entry_tombstone_is_central_fail_closed_gate(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="user-tombstoned-manual.md",
            description="manual fixture",
            tags=["user"],
        )
        entry_id, _created = entries_store.append_entry_once(
            conn,
            name="user-tombstoned-manual.md",
            content="MANUAL_TOMBSTONE_MARKER",
            tags=["manual"],
            entry_id="manual-tombstone-entry",
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )
        parsed = files_store.read_file(
            files_store.memory_path("user-tombstoned-manual.md")
        )
        context = ContextService(conn, _unrestricted_config())
        assert context.memory_entry_allowed(
            path=parsed.path.name, entry=parsed.entries[0]
        )

        candidate_store.put_tombstone(
            conn,
            kind="memory_entry",
            artifact_id=entry_id,
            path=parsed.path.name,
        )

        assert not context.memory_entry_allowed(
            path=parsed.path.name, entry=parsed.entries[0]
        )


def test_malformed_candidate_ownership_never_becomes_manual_metadata(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="project-partial-owner.md",
            description="PROVIDER_DERIVED_METADATA",
            tags=["project"],
        )
        path = files_store.memory_path("project-partial-owner.md")
        files_store.update_frontmatter(
            path,
            {files_store.CANDIDATE_FILE_OWNER_KEY: "mc-" + "a" * 24},
        )
        parsed = files_store.read_file(path)

        assert not ContextService(
            conn, _unrestricted_config()
        ).memory_file_metadata_allowed(parsed)


def test_hidden_nonempty_file_cannot_leak_frontmatter_through_mcp(
    ac_root: Path,
) -> None:
    marker = "HIDDEN_PROVIDER_FRONTMATTER_MARKER"
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="project-hidden-container.md",
            description=marker,
            tags=[marker],
        )
        # Default automation origin is deliberately not a hand-written trust
        # root, even under an otherwise unrestricted capture policy.
        entries_store.append_entry(
            conn,
            name="project-hidden-container.md",
            content="HIDDEN_AUTOMATION_BODY",
            tags=["automation"],
        )

        result = mcp_server._read_memory(
            conn,
            cfg=_unrestricted_config(),
            path="project-hidden-container.md",
        )

        assert result == {"error": "file not found: project-hidden-container.md"}
        assert marker not in str(result)


def test_ambiguous_empty_container_cannot_leak_frontmatter_through_mcp(
    ac_root: Path,
) -> None:
    marker = "AMBIGUOUS_EMPTY_FRONTMATTER_MARKER"
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="project-empty-container.md",
            description=marker,
            tags=[marker],
        )

        read = mcp_server._read_memory(
            conn,
            cfg=_unrestricted_config(),
            path="project-empty-container.md",
        )
        listed = mcp_server._list_memories(
            conn,
            cfg=_unrestricted_config(),
        )

        assert "error" in read
        assert listed == {"count": 0, "files": []}
        assert marker not in str({"read": read, "list": listed})


def test_unprovenanced_timeline_and_session_are_quarantined_even_when_unrestricted(
    ac_root: Path,
) -> None:
    cfg = _unrestricted_config()
    start = datetime.now().astimezone().replace(microsecond=0)
    block = timeline_store.TimelineBlock(
        id="tlb-orphan-structural",
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=["MISSING_PROVENANCE_MARKER"],
        apps_used=["Editor"],
        capture_count=1,
    )
    block_ref = EvidenceRef(
        kind="timeline_block",
        id=block.id,
        content_hash=timeline_block_digest(
            start=block.start_time.isoformat(),
            end=block.end_time.isoformat(),
            entries=block.entries,
            apps=block.apps_used,
        ),
    )
    session_ref = EvidenceRef(kind="session", id="session-orphan-structural")
    with fts.cursor() as conn:
        timeline_store.insert(conn, block)
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_ref.id,
                start_time=start,
                end_time=start + timedelta(minutes=1),
                status="ended",
            ),
        )
        context = ContextService(conn, cfg)
        resolver = EvidenceResolver(conn, cfg)

        assert not context.evidence_allowed(block_ref)
        assert resolver.resolve(block_ref)["status"] == "excluded"
        assert resolver.resolve(session_ref)["status"] == "excluded"

    current = mcp_captures.current_context(cfg=cfg, timeline_limit=10)
    assert current["recent_timeline_blocks"] == []
    assert "MISSING_PROVENANCE_MARKER" not in str(current)
