from __future__ import annotations

import json
import os
import threading
import time

import pytest

from openchronicle import cli


def test_clean_captures_removes_matching_index_rows(ac_root) -> None:
    from openchronicle import paths
    from openchronicle.store import fts

    capture = {
        "timestamp": "2026-04-25T22:01:00+08:00",
        "window_meta": {"app_name": "Chrome", "title": "Python docs"},
        "visible_text": "urllib.parse docs",
    }
    capture_path = paths.capture_buffer_dir() / "normal.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="normal",
            timestamp=capture["timestamp"],
            app_name="Chrome",
            bundle_id="",
            window_title="Python docs",
            focused_role="",
            focused_value="",
            visible_text="urllib.parse docs",
            url="https://docs.python.org",
        )

    deleted = cli._clean_captures()

    assert deleted == 1
    assert not capture_path.exists()
    with fts.cursor() as conn:
        rows = conn.execute("SELECT id FROM captures").fetchall()
    assert rows == []


def test_clean_captures_retains_json_when_index_delete_fails(
    ac_root,
    monkeypatch,
) -> None:
    from openchronicle import paths
    from openchronicle.store import fts

    capture_path = paths.capture_buffer_dir() / "private.json"
    capture_path.write_text('{"visible_text": "keep source"}', encoding="utf-8")
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="private",
            timestamp="2026-04-25T22:01:00+08:00",
            app_name="Notes",
            bundle_id="",
            window_title="Private",
            focused_role="",
            focused_value="",
            visible_text="keep source",
            url="",
        )

    from openchronicle.memory_candidates import store as candidate_store

    def fail_tombstone(*args, **kwargs) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(candidate_store, "put_tombstone", fail_tombstone)

    with pytest.raises(RuntimeError, match="database unavailable"):
        cli._clean_captures()

    assert capture_path.exists()
    with fts.cursor() as conn:
        assert conn.execute(
            "SELECT id FROM captures WHERE id='private'"
        ).fetchone() is not None


def test_clean_captures_unlink_failure_stays_hidden_from_read_and_rebuild(
    ac_root,
    monkeypatch,
) -> None:
    from pathlib import Path

    from openchronicle.capture import scheduler
    from openchronicle.mcp import captures as mcp_captures
    from openchronicle.memory_candidates import store as candidate_store
    from openchronicle.store import fts

    monkeypatch.setattr(cli, "_init", lambda: None)
    capture = {
        "timestamp": "2026-04-25T22:01:00+08:00",
        "window_meta": {"app_name": "Notes", "title": "Private"},
        "visible_text": "CAPTURE_UNLINK_PRIVATE_MARKER",
    }
    capture_path = scheduler._write_capture(capture)
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id=capture_path.stem,
            timestamp=capture["timestamp"],
            app_name="Notes",
            bundle_id="",
            window_title="Private",
            focused_role="",
            focused_value="",
            visible_text=capture["visible_text"],
            url="",
        )

    real_unlink = Path.unlink

    def fail_target(path: Path, *args, **kwargs) -> None:
        if path == capture_path:
            raise PermissionError("immutable capture")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)
    with pytest.raises(RuntimeError, match="capture cleanup incomplete"):
        cli._clean_captures()

    assert capture_path.exists()
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0
        assert candidate_store.is_tombstoned(
            conn, kind="capture_file", artifact_id=capture_path.name
        )
    cli.rebuild_captures_index()
    assert mcp_captures.search_captures(query="CAPTURE_UNLINK_PRIVATE_MARKER") == []
    assert mcp_captures.read_recent_capture() is None


def test_capture_read_and_cleanup_are_linearized(
    ac_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from openchronicle.capture import scheduler
    from openchronicle.mcp import captures as mcp_captures
    from openchronicle.memory_candidates import store as candidate_store
    from openchronicle.store import fts

    capture = {
        "timestamp": "2026-04-25T22:01:00+08:00",
        "window_meta": {"app_name": "Notes", "title": "Race"},
        "visible_text": "CAPTURE_READ_CLEAN_RACE_SECRET",
    }
    capture_path = scheduler._write_capture(capture)
    entered_read = threading.Event()
    release_read = threading.Event()
    cleanup_done = threading.Event()
    read_results: list[dict | None] = []
    cleanup_errors: list[BaseException] = []
    real_load = mcp_captures._load_capture
    real_unlink = Path.unlink

    def paused_load(path):
        data = real_load(path)
        entered_read.set()
        assert release_read.wait(timeout=5)
        return data

    def fail_target(path: Path, *args, **kwargs) -> None:
        if path == capture_path:
            raise PermissionError("immutable capture")
        real_unlink(path, *args, **kwargs)

    def reader() -> None:
        read_results.append(mcp_captures.read_recent_capture())

    def cleaner() -> None:
        try:
            cli._clean_captures()
        except BaseException as exc:  # noqa: BLE001
            cleanup_errors.append(exc)
        finally:
            cleanup_done.set()

    monkeypatch.setattr(mcp_captures, "_load_capture", paused_load)
    monkeypatch.setattr(Path, "unlink", fail_target)
    reader_thread = threading.Thread(target=reader)
    cleaner_thread = threading.Thread(target=cleaner)
    reader_thread.start()
    assert entered_read.wait(timeout=5)
    cleaner_thread.start()
    try:
        assert not cleanup_done.wait(timeout=0.1), "cleanup bypassed capture read lock"
        with fts.cursor() as conn:
            assert not candidate_store.is_tombstoned(
                conn, kind="capture_file", artifact_id=capture_path.name
            )
    finally:
        release_read.set()

    reader_thread.join(timeout=10)
    cleaner_thread.join(timeout=10)
    assert not reader_thread.is_alive() and not cleaner_thread.is_alive()
    assert read_results[0] is not None
    assert read_results[0]["visible_text"] == "CAPTURE_READ_CLEAN_RACE_SECRET"
    assert len(cleanup_errors) == 1
    assert isinstance(cleanup_errors[0], RuntimeError)
    with fts.cursor() as conn:
        assert candidate_store.is_tombstoned(
            conn, kind="capture_file", artifact_id=capture_path.name
        )
    assert mcp_captures.read_recent_capture() is None


def test_clean_captures_clears_stale_rows_when_buffer_is_empty(ac_root) -> None:
    from openchronicle.store import fts

    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="stale-empty",
            timestamp="2026-04-25T22:01:00+08:00",
            app_name="Notes",
            bundle_id="",
            window_title="Sensitive stale title",
            focused_role="",
            focused_value="",
            visible_text="STALE_EMPTY_SECRET",
            url="",
        )

    assert cli._clean_captures() == 0

    with fts.cursor() as conn:
        assert conn.execute("SELECT id FROM captures").fetchall() == []


def test_clean_captures_removes_crash_orphan_temp(ac_root) -> None:
    from openchronicle.capture import scheduler

    capture = {
        "timestamp": "2026-04-25T22:01:00+08:00",
        "window_meta": {"app_name": "Notes", "title": "Private"},
        "visible_text": "private",
    }
    capture_path = scheduler._write_capture(capture)
    orphan = capture_path.parent / f".{capture_path.name}.deadbeef.tmp"
    orphan.write_text("SENSITIVE_CRASH_COPY", encoding="utf-8")

    assert cli._clean_captures() == 2
    assert not capture_path.exists()
    assert not orphan.exists()


def test_clean_captures_clears_stale_rows_when_buffer_is_missing(ac_root) -> None:
    from openchronicle import paths
    from openchronicle.store import fts

    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="stale-missing",
            timestamp="2026-04-25T22:01:00+08:00",
            app_name="Notes",
            bundle_id="",
            window_title="Sensitive stale title",
            focused_role="",
            focused_value="",
            visible_text="STALE_MISSING_SECRET",
            url="",
        )
    paths.capture_buffer_dir().rmdir()

    assert cli._clean_captures() == 0

    with fts.cursor() as conn:
        assert conn.execute("SELECT id FROM captures").fetchall() == []


def test_rebuild_captures_index_drops_rows_for_missing_files(ac_root, monkeypatch) -> None:
    from openchronicle import paths
    from openchronicle.store import fts

    monkeypatch.setattr(cli, "_init", lambda: None)

    kept_capture = {
        "timestamp": "2026-04-25T22:01:00+08:00",
        "window_meta": {"app_name": "Chrome", "title": "Python docs"},
        "visible_text": "urllib.parse docs",
    }
    kept_path = paths.capture_buffer_dir() / "kept.json"
    kept_path.write_text(json.dumps(kept_capture), encoding="utf-8")
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="kept",
            timestamp=kept_capture["timestamp"],
            app_name="Chrome",
            bundle_id="",
            window_title="Old title",
            focused_role="",
            focused_value="",
            visible_text="old text",
            url="",
        )
        fts.insert_capture(
            conn,
            id="stale",
            timestamp="2026-04-25T22:00:00+08:00",
            app_name="Chrome",
            bundle_id="",
            window_title="Deleted page",
            focused_role="",
            focused_value="",
            visible_text="deleted text",
            url="",
        )

    cli.rebuild_captures_index()

    with fts.cursor() as conn:
        rows = conn.execute(
            "SELECT id, window_title, visible_text FROM captures ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["window_title"], row["visible_text"]) for row in rows] == [
        ("kept", "Python docs", "urllib.parse docs")
    ]


@pytest.mark.parametrize("payload", ["{broken", "[]"])
def test_rebuild_captures_index_clears_invalid_same_stem_row(
    ac_root,
    monkeypatch,
    payload: str,
) -> None:
    from openchronicle import paths
    from openchronicle.store import fts

    monkeypatch.setattr(cli, "_init", lambda: None)
    capture_path = paths.capture_buffer_dir() / "invalid.json"
    capture_path.write_text(payload, encoding="utf-8")
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="invalid",
            timestamp="2026-04-25T22:01:00+08:00",
            app_name="Notes",
            bundle_id="",
            window_title="Stale private title",
            focused_role="",
            focused_value="",
            visible_text="STALE_SECRET",
            url="",
        )

    cli.rebuild_captures_index()

    assert capture_path.exists()
    with fts.cursor() as conn:
        assert conn.execute(
            "SELECT id FROM captures WHERE id='invalid'"
        ).fetchone() is None


def test_cleanup_and_rebuild_share_capture_store_lock(ac_root, monkeypatch) -> None:
    from openchronicle.capture import scheduler
    from openchronicle.store import fts

    monkeypatch.setattr(cli, "_init", lambda: None)
    capture = {
        "timestamp": "2026-04-25T22:01:00+08:00",
        "window_meta": {"app_name": "Notes", "title": "Race"},
        "visible_text": "NO_ORPHAN_AFTER_RACE",
    }
    capture_path = scheduler._write_capture(capture)
    old = time.time() - 10 * 24 * 3600
    os.utime(capture_path, (old, old))

    delete_entered = threading.Event()
    allow_unlink = threading.Event()
    real_delete = scheduler._delete_captures_from_fts

    def pausing_delete(stems: list[str]) -> bool:
        result = real_delete(stems)
        delete_entered.set()
        assert allow_unlink.wait(timeout=2)
        return result

    monkeypatch.setattr(scheduler, "_delete_captures_from_fts", pausing_delete)
    cleanup_thread = threading.Thread(
        target=scheduler.cleanup_buffer,
        kwargs={
            "retention_hours": 1,
            "processed_before_ts": "2099-01-01T00:00:00+00:00",
        },
    )
    rebuild_thread = threading.Thread(target=cli.rebuild_captures_index)

    cleanup_thread.start()
    assert delete_entered.wait(timeout=2)
    rebuild_thread.start()
    allow_unlink.set()
    cleanup_thread.join(timeout=2)
    rebuild_thread.join(timeout=2)

    assert not cleanup_thread.is_alive()
    assert not rebuild_thread.is_alive()
    assert not capture_path.exists()
    with fts.cursor() as conn:
        assert conn.execute(
            "SELECT id FROM captures WHERE id=?", (capture_path.stem,)
        ).fetchone() is None
