from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openchronicle.resume_cues import store
from openchronicle.store import fts


def test_resume_cue_has_one_active_slot_and_explicit_terminal_state(ac_root: Path) -> None:
    created_at = datetime(2026, 8, 23, 9, 30, tzinfo=UTC)
    with fts.cursor() as conn:
        cue = store.create(
            conn,
            task_label="Release notes",
            next_step="Verify the migration example against a fresh database.",
            now=created_at,
        )
        with pytest.raises(store.ResumeCueConflict):
            store.create(
                conn,
                task_label="Another task",
                next_step="This must wait until the active cue is closed.",
                now=created_at + timedelta(seconds=1),
            )

        ref = store.evidence_ref(cue)
        assert store.ref_is_current(conn, ref)
        resumed = store.transition(
            conn,
            cue_id=cue.id,
            expected_version=cue.version,
            to_status="resumed",
            now=created_at + timedelta(minutes=30),
        )

        assert resumed.status == "resumed"
        assert resumed.version == 2
        assert resumed.task_label == cue.task_label
        assert resumed.next_step == cue.next_step
        assert not store.ref_is_current(conn, ref)
        assert store.get_parked(conn) is None
        replacement = store.create(
            conn,
            task_label="Another task",
            next_step="Continue from the reviewed outline.",
            now=created_at + timedelta(minutes=31),
        )
        assert replacement.status == "parked"


def test_resume_cue_transition_is_compare_and_swap(ac_root: Path) -> None:
    with fts.cursor() as conn:
        cue = store.create(
            conn,
            task_label="Cue CAS",
            next_step="Run the focused regression test.",
        )
        with pytest.raises(store.ResumeCueConflict):
            store.transition(
                conn,
                cue_id=cue.id,
                expected_version=cue.version + 1,
                to_status="dismissed",
            )
        dismissed = store.transition(
            conn,
            cue_id=cue.id,
            expected_version=cue.version,
            to_status="dismissed",
        )
        with pytest.raises(store.ResumeCueConflict):
            store.transition(
                conn,
                cue_id=cue.id,
                expected_version=dismissed.version,
                to_status="resumed",
            )


def test_resume_cue_must_precede_the_return_block(ac_root: Path) -> None:
    created_at = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        cue = store.create(
            conn,
            task_label="Timing",
            next_step="Continue at the exact suspended step.",
            now=created_at,
        )
        assert store.parked_before(conn, created_at - timedelta(microseconds=1)) is None
        assert store.parked_before(conn, created_at) == cue


def test_corrupt_resume_cue_projection_is_not_returned(ac_root: Path) -> None:
    with fts.cursor() as conn:
        cue = store.create(
            conn,
            task_label="Integrity",
            next_step="Keep the exact reviewed text.",
        )
        conn.execute(
            "UPDATE resume_cues SET next_step='tampered' WHERE id=?",
            (cue.id,),
        )

        assert store.get(conn, cue.id) is None
        assert store.get_parked(conn) is None
        assert store.list_cues(conn, statuses=["parked"]) == []
        assert not store.ref_is_current(conn, store.evidence_ref(cue))
