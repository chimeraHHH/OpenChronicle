from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openchronicle import config as config_mod
from openchronicle import paths
from openchronicle.session import store as session_store
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store
from openchronicle.writer import session_reducer

_TZ = timezone(timedelta(hours=8))


def _seed_session(session_id: str, start: datetime, end: datetime) -> None:
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=end,
                entries=["[Editor] implemented Stage 0"],
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


def _payload(*_args, **_kwargs):
    return {
        "summary": "Implemented Stage 0.",
        "sub_tasks": ["[09:00-09:05, Editor] hardened the runtime, involving Stage 0"],
    }


def test_concurrent_terminal_reducers_write_session_once(
    ac_root: Path,
    monkeypatch,
) -> None:
    session_id = "sess_concurrent"
    start = datetime(2026, 8, 7, 9, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)
    _seed_session(session_id, start, end)

    calls = 0
    calls_lock = threading.Lock()

    def slow_payload(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return _payload(*args, **kwargs)

    monkeypatch.setattr(session_reducer, "_call_reducer_llm", slow_payload)
    cfg = config_mod.load(ac_root / "config.toml")
    barrier = threading.Barrier(2)

    def run_one():
        barrier.wait()
        return session_reducer.reduce_session(
            cfg,
            session_id=session_id,
            start_time=start,
            end_time=end,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: run_one(), range(2)))

    assert calls == 1
    assert sorted(result.written for result in results) == [False, True]
    markdown = (paths.memory_dir() / "event-2026-08-07.md").read_text()
    assert markdown.count(f"Session {session_id}") == 1
    with fts.cursor() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE tags LIKE ?",
            (f"%sid:{session_id}%",),
        ).fetchone()[0]
    assert count == 1


def test_terminal_reduction_replay_repairs_index_without_duplicate_markdown(
    ac_root: Path,
    monkeypatch,
) -> None:
    session_id = "sess_replay"
    start = datetime(2026, 8, 7, 10, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)
    _seed_session(session_id, start, end)
    monkeypatch.setattr(session_reducer, "_call_reducer_llm", _payload)
    cfg = config_mod.load(ac_root / "config.toml")

    first = session_reducer.reduce_session(
        cfg,
        session_id=session_id,
        start_time=start,
        end_time=end,
    )
    assert first.written is True

    # Equivalent to a crash after the atomic Markdown rename but before the
    # FTS projection/progress status became durable.
    with fts.cursor() as conn:
        conn.execute("DELETE FROM entries WHERE id=?", (first.entry_id,))
        conn.execute(
            "UPDATE files SET entry_count=0 WHERE path='event-2026-08-07.md'"
        )
        conn.execute(
            "UPDATE sessions SET status='ended', flush_end=NULL WHERE id=?",
            (session_id,),
        )

    def must_not_call_llm(*_args, **_kwargs):
        raise AssertionError("durable Markdown replay must not call the LLM")

    monkeypatch.setattr(session_reducer, "_call_reducer_llm", must_not_call_llm)
    replay = session_reducer.reduce_session(
        cfg,
        session_id=session_id,
        start_time=start,
        end_time=end,
    )

    assert replay.entry_id == first.entry_id
    # The entry is available for the downstream classifier even though replay
    # reused rather than duplicated it.
    assert replay.written is True
    markdown = (paths.memory_dir() / "event-2026-08-07.md").read_text()
    assert markdown.count(f"Session {session_id}") == 1
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
        indexed = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id=?",
            (first.entry_id,),
        ).fetchone()[0]
        file_entry_count = conn.execute(
            "SELECT entry_count FROM files WHERE path='event-2026-08-07.md'"
        ).fetchone()[0]
    assert row is not None and row.status == "reduced"
    assert indexed == 1
    assert file_entry_count == 1


def test_concurrent_failed_reducers_consume_one_retry(
    ac_root: Path,
    monkeypatch,
) -> None:
    session_id = "sess_failed_concurrent"
    start = datetime(2026, 8, 7, 11, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)
    _seed_session(session_id, start, end)

    calls = 0
    calls_lock = threading.Lock()

    def slow_failure(*_args, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return None

    monkeypatch.setattr(session_reducer, "_call_reducer_llm", slow_failure)
    cfg = config_mod.load(ac_root / "config.toml")
    barrier = threading.Barrier(3)

    def run_one():
        barrier.wait()
        return session_reducer.reduce_session(
            cfg,
            session_id=session_id,
            start_time=start,
            end_time=end,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _index: run_one(), range(3)))

    assert calls == 1
    assert all(result.written is False for result in results)
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
    assert row is not None
    assert row.status == "failed"
    assert row.retry_count == 1
    assert row.next_retry_at is not None


def test_session_reduction_locks_use_fixed_shards(ac_root: Path) -> None:
    lock_paths = {
        session_reducer._reduction_lock_path(f"sess_{index}")
        for index in range(10_000)
    }

    assert 1 < len(lock_paths) <= session_reducer._REDUCTION_LOCK_SHARDS
    assert all(path.parent == paths.root() / ".reduction-locks" for path in lock_paths)
    assert all(path.name.startswith("shard-") for path in lock_paths)
    assert session_reducer._reduction_lock_path(
        "sess_stable"
    ) == session_reducer._reduction_lock_path("sess_stable")
