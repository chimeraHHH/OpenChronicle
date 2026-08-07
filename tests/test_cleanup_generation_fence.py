from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openchronicle import cli, paths
from openchronicle import config as config_mod
from openchronicle.session import store as session_store
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store
from openchronicle.writer import session_reducer

_TZ = timezone(timedelta(hours=8))


def _seed_ended_session(session_id: str, start: datetime, end: datetime) -> None:
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=end,
                entries=["[Editor] handled private material"],
                apps_used=["Editor"],
                capture_count=1,
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )


def _payload(*_args, **_kwargs) -> dict[str, object]:
    return {
        "summary": "Handled private material.",
        "sub_tasks": ["[09:00-09:05, Editor] handled private material"],
    }


def _blocking_payload(
    entered_llm: threading.Event,
    release_llm: threading.Event,
):
    def call(_cfg, blocks, *_args, **_kwargs):
        # Reaching the provider proves the reducer already selected and rendered
        # the pre-clean timeline snapshot.
        assert len(blocks) == 1
        assert blocks[0].entries == ["[Editor] handled private material"]
        entered_llm.set()
        if not release_llm.wait(timeout=5):
            raise AssertionError("test did not release the blocked reducer LLM")
        return _payload()

    return call


def _assert_no_reducer_projection(session_id: str, end: datetime) -> int:
    assert not list(paths.memory_dir().glob("event-*.md"))
    with fts.cursor() as conn:
        generation = fts.content_generation(conn, "reducer")
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        row = session_store.get_by_id(conn, session_id)
    assert row is not None
    assert row.status == "ended"
    assert row.classified_end == end
    assert row.classifier_terminal_pending is False
    assert row.classifier_terminal_entry_id == ""
    assert row.classifier_terminal_path == ""
    return generation


def test_memory_clean_rejects_stale_reducer_publish_and_allows_fresh_generation(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "sess_memory_generation_fence"
    start = datetime(2026, 8, 8, 9, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)
    _seed_ended_session(session_id, start, end)
    cfg = config_mod.load(ac_root / "config.toml")
    entered_llm = threading.Event()
    release_llm = threading.Event()
    monkeypatch.setattr(
        session_reducer,
        "_call_reducer_llm",
        _blocking_payload(entered_llm, release_llm),
    )

    with fts.cursor() as conn:
        generation_before = fts.content_generation(conn, "reducer")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            session_reducer.reduce_session,
            cfg,
            session_id=session_id,
            start_time=start,
            end_time=end,
        )
        assert entered_llm.wait(timeout=5)
        try:
            removed_files, removed_entries = cli._clean_memory()
        finally:
            release_llm.set()
        with pytest.raises(session_reducer.ReducerInputChanged):
            future.result(timeout=5)

    assert (removed_files, removed_entries) == (0, 0)
    generation_after = _assert_no_reducer_projection(session_id, end)
    assert generation_after == generation_before + 1

    # Cleanup invalidates only the old snapshot. A reducer that starts in the
    # new generation can still publish from the timeline evidence left intact
    # by memory-only cleanup.
    monkeypatch.setattr(session_reducer, "_call_reducer_llm", _payload)
    fresh = session_reducer.reduce_session(
        cfg,
        session_id=session_id,
        start_time=start,
        end_time=end,
    )

    assert fresh.written is True
    assert (paths.memory_dir() / "event-2026-08-08.md").exists()
    with fts.cursor() as conn:
        assert fts.content_generation(conn, "reducer") == generation_after
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        row = session_store.get_by_id(conn, session_id)
    assert row is not None
    assert row.status == "reduced"
    assert row.classifier_terminal_pending is True
    assert row.classifier_terminal_entry_id == fresh.entry_id


def test_timeline_clean_rejects_stale_reducer_publish(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "sess_timeline_generation_fence"
    start = datetime(2026, 8, 8, 10, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)
    _seed_ended_session(session_id, start, end)
    cfg = config_mod.load(ac_root / "config.toml")
    entered_llm = threading.Event()
    release_llm = threading.Event()
    monkeypatch.setattr(
        session_reducer,
        "_call_reducer_llm",
        _blocking_payload(entered_llm, release_llm),
    )

    with fts.cursor() as conn:
        generation_before = fts.content_generation(conn, "reducer")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            session_reducer.reduce_session,
            cfg,
            session_id=session_id,
            start_time=start,
            end_time=end,
        )
        assert entered_llm.wait(timeout=5)
        try:
            removed_blocks = cli._clean_timeline()
        finally:
            release_llm.set()
        with pytest.raises(session_reducer.ReducerInputChanged):
            future.result(timeout=5)

    assert removed_blocks == 1
    generation_after = _assert_no_reducer_projection(session_id, end)
    assert generation_after == generation_before + 1
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM provenance_edges").fetchone()[0] == 0
