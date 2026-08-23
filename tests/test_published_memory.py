from __future__ import annotations

from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance.models import EvidenceRef, content_digest
from openchronicle.services.current_facts import list_current_facts
from openchronicle.services.memory import MemoryService
from openchronicle.services.published_memory import (
    PublishedMemoryConflict,
    correct_current_fact,
    list_revision_history,
)
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.store.facts import make_fact_metadata


def _seed_current_fact(conn) -> None:
    entries_store.create_file(
        conn,
        name="user-preferences.md",
        description="direct user memories",
        tags=["preference"],
    )
    entries_store.append_entry_once(
        conn,
        name="user-preferences.md",
        content="User prefers local-first tools.",
        tags=["preference", "local-first"],
        entry_id="published-original",
        origin=files_store.MANUAL_ENTRY_ORIGIN,
        fact_metadata=make_fact_metadata(
            subject_key="user.tools.storage",
            assertion_kind="user_asserted",
            valid_from="2026-08-01",
        ),
    )


def test_correction_supersedes_current_fact_and_preserves_semantic_history(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_current_fact(conn)
        original = list_current_facts(conn, cfg)[0]

        corrected = correct_current_fact(
            conn,
            cfg,
            path=original.path,
            entry_id=original.id,
            expected_revision=original.revision,
            content="User prefers encrypted local-first tools.",
            tags=["preference", "encrypted"],
        )

        assert corrected.id.startswith("me-")
        assert corrected.revision != original.revision
        assert corrected.content == "User prefers encrypted local-first tools."
        assert corrected.tags == ("preference", "encrypted")
        assert corrected.subject_key == original.subject_key
        assert corrected.assertion_kind == original.assertion_kind
        assert corrected.valid_from == original.valid_from
        assert corrected.source_count == 1

        parsed = files_store.read_file(files_store.memory_path(original.path))
        old_entry = next(entry for entry in parsed.entries if entry.id == original.id)
        new_entry = next(entry for entry in parsed.entries if entry.id == corrected.id)
        assert old_entry.superseded_by == corrected.id
        assert entries_store.entry_index_content(old_entry) == original.content
        assert new_entry.fact_metadata == old_entry.fact_metadata
        assert [fact.id for fact in list_current_facts(conn, cfg)] == [corrected.id]


def test_correction_retry_is_idempotent(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_current_fact(conn)
        original = list_current_facts(conn, cfg)[0]
        request = {
            "path": original.path,
            "entry_id": original.id,
            "expected_revision": original.revision,
            "content": "User prefers encrypted local-first tools.",
            "tags": ["preference", "encrypted"],
        }

        first = correct_current_fact(conn, cfg, **request)
        second = correct_current_fact(conn, cfg, **request)

        assert second == first
        parsed = files_store.read_file(files_store.memory_path(original.path))
        assert [entry.id for entry in parsed.entries] == [original.id, first.id]


def test_revision_history_is_newest_first_and_bound_to_the_current_revision(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_current_fact(conn)
        original = list_current_facts(conn, cfg)[0]
        first = correct_current_fact(
            conn,
            cfg,
            path=original.path,
            entry_id=original.id,
            expected_revision=original.revision,
            content="User prefers encrypted local-first tools.",
            tags=["preference", "encrypted"],
        )
        second = correct_current_fact(
            conn,
            cfg,
            path=first.path,
            entry_id=first.id,
            expected_revision=first.revision,
            content="User prefers encrypted, offline-capable local-first tools.",
            tags=["preference", "encrypted", "offline"],
        )

        history = list_revision_history(
            conn,
            cfg,
            path=second.path,
            entry_id=second.id,
            expected_revision=second.revision,
        )

        assert [version.id for version in history] == [second.id, first.id, original.id]
        assert [version.state for version in history] == ["current", "superseded", "superseded"]
        assert [version.content for version in history] == [
            "User prefers encrypted, offline-capable local-first tools.",
            "User prefers encrypted local-first tools.",
            "User prefers local-first tools.",
        ]
        assert history[1].superseded_by == second.id
        assert history[1].superseded_at == second.recorded_at
        assert history[2].superseded_by == first.id
        assert all(not tag.startswith("superseded-by:") for item in history for tag in item.tags)

        with pytest.raises(PublishedMemoryConflict, match="no longer current"):
            list_revision_history(
                conn,
                cfg,
                path=original.path,
                entry_id=original.id,
                expected_revision=original.revision,
            )


def test_correction_rejects_stale_revision_without_writing(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_current_fact(conn)
        original = list_current_facts(conn, cfg)[0]

        with pytest.raises(PublishedMemoryConflict, match="revision changed"):
            correct_current_fact(
                conn,
                cfg,
                path=original.path,
                entry_id=original.id,
                expected_revision="0" * 64,
                content="Stale overwrite must not land.",
                tags=["stale"],
            )

        assert list_current_facts(conn, cfg) == [original]


def test_correction_rejects_noop_and_reserved_tags(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_current_fact(conn)
        original = list_current_facts(conn, cfg)[0]

        with pytest.raises(ValueError, match="does not change"):
            correct_current_fact(
                conn,
                cfg,
                path=original.path,
                entry_id=original.id,
                expected_revision=original.revision,
                content=original.content,
                tags=list(original.tags),
            )
        with pytest.raises(ValueError, match="invalid published memory tag"):
            correct_current_fact(
                conn,
                cfg,
                path=original.path,
                entry_id=original.id,
                expected_revision=original.revision,
                content="Changed.",
                tags=["superseded-by:forged"],
            )

        with pytest.raises(ValueError, match="not published memory facts"):
            correct_current_fact(
                conn,
                cfg,
                path="event-2026-08-23.md",
                entry_id=original.id,
                expected_revision=original.revision,
                content="Changed.",
                tags=["event"],
            )


def test_fact_forget_removes_complete_manual_revision_chain_without_resurrection(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_current_fact(conn)
        original = list_current_facts(conn, cfg)[0]
        corrected = correct_current_fact(
            conn,
            cfg,
            path=original.path,
            entry_id=original.id,
            expected_revision=original.revision,
            content="User prefers encrypted local-first tools.",
            tags=["preference", "encrypted"],
        )
        service = MemoryService(conn, cfg=cfg)

        preview = service.preview_purge_fact(
            path=corrected.path,
            entry_id=corrected.id,
            expected_revision=corrected.revision,
        )

        assert preview.candidate_ids == ()
        assert {(entry["path"], entry["id"]) for entry in preview.entries} == {
            (original.path, original.id),
            (corrected.path, corrected.id),
        }
        assert preview.files == ()

        result = service.purge_fact(
            path=corrected.path,
            entry_id=corrected.id,
            expected_revision=corrected.revision,
            expected_plan_digest=preview.plan_digest,
        )

        assert result.removed_entry is True
        assert list_current_facts(conn, cfg) == []
        parsed = files_store.read_file(files_store.memory_path(original.path))
        assert parsed.entries == []


def test_fact_forget_includes_review_candidate_and_candidate_owned_container(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-08-23.md",
            description="review source",
            tags=["event"],
        )
        source_body = "User explicitly requested concise reporting."
        entries_store.append_entry_once(
            conn,
            name="event-2026-08-23.md",
            content=source_body,
            tags=["source"],
            entry_id="forget-source",
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )
        candidate = MemoryService(conn, cfg=cfg).propose_candidate(
            kind="preference",
            target_path="user-forget-generated.md",
            content="User prefers concise reports.",
            tags=["preference"],
            evidence=[
                EvidenceRef(
                    kind="memory_entry",
                    id="forget-source",
                    path="event-2026-08-23.md",
                    content_hash=content_digest(source_body),
                )
            ],
            subject_key="user.reporting.length",
            assertion_kind="user_asserted",
        )
        candidate = MemoryService(conn, cfg=cfg).approve_candidate(
            candidate.id,
            expected_version=candidate.version,
        )
        original = next(
            fact for fact in list_current_facts(conn, cfg) if fact.id == candidate.applied_entry_id
        )
        service = MemoryService(conn, cfg=cfg)
        replacement = service.propose_candidate(
            kind="preference",
            operation="supersede",
            target_path=original.path,
            target_entry_id=original.id,
            content="User prefers very concise reports.",
            tags=["preference", "very-concise"],
            evidence=[
                EvidenceRef(
                    kind="memory_entry",
                    id="forget-source",
                    path="event-2026-08-23.md",
                    content_hash=content_digest(source_body),
                )
            ],
            subject_key="user.reporting.length",
            assertion_kind="user_asserted",
        )
        replacement = service.approve_candidate(
            replacement.id,
            expected_version=replacement.version,
        )
        current = next(
            fact
            for fact in list_current_facts(conn, cfg)
            if fact.id == replacement.applied_entry_id
        )

        preview = service.preview_purge_fact(
            path=current.path,
            entry_id=current.id,
            expected_revision=current.revision,
        )

        assert preview.candidate_ids == tuple(sorted((candidate.id, replacement.id)))
        assert {entry["id"] for entry in preview.entries} == {
            original.id,
            current.id,
        }
        assert preview.files == ({"path": "user-forget-generated.md"},)
        service.purge_fact(
            path=current.path,
            entry_id=current.id,
            expected_revision=current.revision,
            expected_plan_digest=preview.plan_digest,
        )
        assert candidate_store.get(conn, candidate.id) is None
        assert candidate_store.get(conn, replacement.id) is None
        assert not files_store.memory_path("user-forget-generated.md").exists()


def test_fact_forget_rejects_preview_after_a_newer_correction(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_current_fact(conn)
        original = list_current_facts(conn, cfg)[0]
        first = correct_current_fact(
            conn,
            cfg,
            path=original.path,
            entry_id=original.id,
            expected_revision=original.revision,
            content="User prefers encrypted local-first tools.",
            tags=["preference", "encrypted"],
        )
        service = MemoryService(conn, cfg=cfg)
        preview = service.preview_purge_fact(
            path=first.path,
            entry_id=first.id,
            expected_revision=first.revision,
        )
        second = correct_current_fact(
            conn,
            cfg,
            path=first.path,
            entry_id=first.id,
            expected_revision=first.revision,
            content="User prefers encrypted local-only tools.",
            tags=["preference", "encrypted", "local-only"],
        )

        with pytest.raises(candidate_store.CandidateConflict, match="changed"):
            service.purge_fact(
                path=first.path,
                entry_id=first.id,
                expected_revision=first.revision,
                expected_plan_digest=preview.plan_digest,
            )

        assert [fact.id for fact in list_current_facts(conn, cfg)] == [second.id]
