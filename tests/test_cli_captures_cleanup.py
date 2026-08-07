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

    def fail_delete(_capture_ids: list[str]) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(cli, "_delete_capture_rows", fail_delete)

    with pytest.raises(RuntimeError, match="database unavailable"):
        cli._clean_captures()

    assert capture_path.exists()
    with fts.cursor() as conn:
        assert conn.execute(
            "SELECT id FROM captures WHERE id='private'"
        ).fetchone() is not None


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
