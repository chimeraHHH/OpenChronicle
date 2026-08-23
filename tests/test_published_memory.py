from __future__ import annotations

from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.services.current_facts import list_current_facts
from openchronicle.services.published_memory import (
    PublishedMemoryConflict,
    correct_current_fact,
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
