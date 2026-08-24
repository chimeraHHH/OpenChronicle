from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.provenance.models import EvidenceRef, content_digest
from openchronicle.services.current_facts import list_current_facts
from openchronicle.services.memory import MemoryService
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.store.facts import make_fact_metadata, temporal_state
from openchronicle.writer import tools as writer_tools


def _source() -> EvidenceRef:
    return EvidenceRef(
        kind="memory_entry",
        id="typed-source",
        path="event-2026-08-20.md",
        timestamp="2026-08-20T09:00:00+08:00",
        content_hash=content_digest("User explicitly prefers local-first storage."),
    )


def _seed_source(conn) -> None:
    entries_store.create_file(
        conn,
        name="event-2026-08-20.md",
        description="reviewed event source",
        tags=["event"],
    )
    entries_store.append_entry_once(
        conn,
        name="event-2026-08-20.md",
        content="User explicitly prefers local-first storage.",
        tags=["source"],
        entry_id="typed-source",
        origin=files_store.MANUAL_ENTRY_ORIGIN,
    )


def test_fact_metadata_normalizes_slot_and_valid_time() -> None:
    metadata = make_fact_metadata(
        subject_key=" User.Storage Preference ",
        assertion_kind="user-asserted",
        valid_from="2026-08-01T00:00:00+08:00",
        valid_to="2026-09-01T00:00:00+08:00",
    )
    assert metadata.subject_key == "user.storage-preference"
    assert metadata.assertion_kind == "user_asserted"
    assert temporal_state(
        metadata,
        as_of=datetime(2026, 7, 31, 0, tzinfo=UTC),
    ) == "scheduled"
    assert temporal_state(
        metadata,
        as_of=datetime(2026, 8, 15, 0, tzinfo=UTC),
    ) == "current"
    assert temporal_state(
        metadata,
        as_of=datetime(2026, 9, 1, 0, tzinfo=UTC),
    ) == "expired"

    with pytest.raises(ValueError, match="valid_to"):
        make_fact_metadata(
            subject_key="user.preference",
            assertion_kind="observed",
            valid_from="2026-09-01",
            valid_to="2026-08-01",
        )


def test_reviewed_typed_fact_round_trips_to_current_markdown_projection(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_source(conn)
        service = MemoryService(conn, cfg=cfg)
        candidate = service.propose_candidate(
            kind="preference",
            target_path="user-preferences.md",
            content="User prefers local-first storage.",
            tags=["preference", "local-first"],
            evidence=[_source()],
            subject_key="user.storage.preference",
            assertion_kind="user_asserted",
            valid_from="2026-08-20T09:00:00+08:00",
        )
        assert candidate.conflict_key == "user.storage.preference"
        accepted = service.approve_candidate(
            candidate.id,
            expected_version=candidate.version,
        )
        assert accepted.applied_entry_id is not None

        parsed = files_store.read_file(files_store.memory_path("user-preferences.md"))
        entry = next(item for item in parsed.entries if item.id == accepted.applied_entry_id)
        assert entry.fact_metadata is not None
        assert entry.fact_metadata.to_dict() == {
            "subject_key": "user.storage.preference",
            "assertion_kind": "user_asserted",
            "valid_from": "2026-08-20T09:00:00+08:00",
            "valid_to": "",
        }

        facts = list_current_facts(
            conn,
            cfg,
            as_of=datetime(2026, 8, 23, 0, tzinfo=UTC),
        )
        assert [fact.id for fact in facts] == [accepted.applied_entry_id]
        assert facts[0].subject_key == "user.storage.preference"
        assert facts[0].assertion_kind == "user_asserted"


def test_typed_supersede_replaces_same_fact_slot_without_self_conflict(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_source(conn)
        service = MemoryService(conn, cfg=cfg)
        original = service.propose_candidate(
            kind="preference",
            target_path="user-preferences.md",
            content="User prefers local-first storage.",
            tags=["preference"],
            evidence=[_source()],
            subject_key="user.storage.preference",
            assertion_kind="user_asserted",
        )
        original = service.approve_candidate(
            original.id,
            expected_version=original.version,
        )
        assert original.applied_entry_id is not None

        replacement = service.propose_candidate(
            kind="preference",
            operation="supersede",
            target_path="user-preferences.md",
            target_entry_id=original.applied_entry_id,
            content="User prefers encrypted local-first storage.",
            tags=["preference", "encryption"],
            evidence=[_source()],
            subject_key="user.storage.preference",
            assertion_kind="user_asserted",
            valid_from="2026-08-22",
        )
        assert replacement.status == "pending"
        replacement = service.approve_candidate(
            replacement.id,
            expected_version=replacement.version,
        )

        facts = list_current_facts(
            conn,
            cfg,
            as_of=datetime(2026, 8, 23, tzinfo=UTC),
        )
        assert [(fact.id, fact.subject_key, fact.content) for fact in facts] == [
            (
                replacement.applied_entry_id,
                "user.storage.preference",
                "User prefers encrypted local-first storage.",
            )
        ]

        with pytest.raises(ValueError, match="preserve the target subject_key"):
            service.propose_candidate(
                kind="preference",
                operation="supersede",
                target_path="user-preferences.md",
                target_entry_id=replacement.applied_entry_id or "",
                content="User prefers cloud storage.",
                tags=["preference"],
                evidence=[_source()],
                subject_key="user.storage.provider",
                assertion_kind="user_asserted",
            )


def test_typed_fact_slot_conflicts_across_markdown_files(ac_root: Path) -> None:
    with fts.cursor() as conn:
        _seed_source(conn)
        service = MemoryService(conn)
        first = service.propose_candidate(
            kind="preference",
            target_path="user-preferences.md",
            content="User prefers local-first storage.",
            tags=["preference"],
            evidence=[_source()],
            subject_key="user.storage.preference",
            assertion_kind="user_asserted",
        )
        second = service.propose_candidate(
            kind="profile",
            target_path="user-profile.md",
            content="User prefers cloud storage.",
            tags=["profile"],
            evidence=[_source()],
            subject_key="user.storage.preference",
            assertion_kind="inferred",
            producer_run_key="second-file",
        )

        assert first.status == "pending"
        assert second.status == "conflict"


def test_expired_typed_fact_is_excluded_from_recall_and_current_projection(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    marker = "EXPIRED_TYPED_MEMORY_MARKER"
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="topic-expired.md",
            description="expired typed fact",
            tags=["topic"],
        )
        entries_store.append_entry_once(
            conn,
            name="topic-expired.md",
            content=marker,
            tags=["expired"],
            entry_id="expired-fact",
            origin=files_store.MANUAL_ENTRY_ORIGIN,
            fact_metadata=make_fact_metadata(
                subject_key="topic.expired",
                assertion_kind="user_asserted",
                valid_to="2000-01-01T00:00:00+00:00",
            ),
        )

        assert writer_tools.tool_search_memory(
            conn,
            cfg,
            query=marker,
        )["results"] == []
        assert list_current_facts(conn, cfg) == []
