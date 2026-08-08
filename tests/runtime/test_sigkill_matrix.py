from __future__ import annotations

import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openchronicle.testing import failpoints

_WORKER = [sys.executable, "-m", "openchronicle.testing.runtime_worker"]


def _environment(root: Path, *, failpoint: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for name in (failpoints.FAILPOINT_ENV, failpoints.ACTION_ENV, failpoints.TOKEN_ENV):
        env.pop(name, None)
    env["OPENCHRONICLE_ROOT"] = str(root)
    env["PYTHONUNBUFFERED"] = "1"
    if failpoint is not None:
        token = secrets.token_hex(32)
        root.mkdir(parents=True, exist_ok=True)
        auth = root / failpoints.AUTHORIZATION_FILE
        auth.write_text(token, encoding="utf-8")
        auth.chmod(0o600)
        env[failpoints.FAILPOINT_ENV] = failpoint
        env[failpoints.ACTION_ENV] = failpoints.ACTION_SIGKILL_V1
        env[failpoints.TOKEN_ENV] = token
    return env


def _run(root: Path, mode: str, *, timeout: float = 15.0) -> dict:
    completed = subprocess.run(
        [*_WORKER, mode],
        env=_environment(root),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, completed.stderr
    return json.loads(lines[-1])


def _crash(root: Path, mode: str, failpoint_name: str) -> None:
    env = _environment(root, failpoint=failpoint_name)
    completed = subprocess.run(
        [*_WORKER, mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == -signal.SIGKILL, completed.stderr
    marker = root / failpoints.HIT_DIRECTORY / f"{failpoint_name}.hit"
    marker_lines = marker.read_text(encoding="utf-8").splitlines()
    assert marker_lines[0] == failpoint_name
    assert int(marker_lines[1]) > 1
    assert env[failpoints.TOKEN_ENV] not in marker.read_text(encoding="utf-8")


def _capture_db_count(root: Path) -> int:
    database = root / "index.db"
    if not database.exists():
        return 0
    conn = sqlite3.connect(database)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='captures'"
        ).fetchone()
        return int(conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0]) if exists else 0
    finally:
        conn.close()


def _run_daemon_startup_cycle(root: Path) -> None:
    ready = root / "daemon-startup.ready"
    process = subprocess.Popen(
        [*_WORKER, "daemon-run-stub", "--ready", str(ready)],
        env=_environment(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not ready.exists():
            assert process.poll() is None, process.stderr.read() if process.stderr else ""
            time.sleep(0.01)
        assert ready.exists()
        process.send_signal(signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, stderr
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


@pytest.mark.parametrize(
    ("failpoint_name", "files_before", "rows_before"),
    [
        ("capture.json.before_rename", 0, 0),
        ("capture.json.after_rename", 1, 0),
        ("capture.fts.before_write", 1, 0),
        ("capture.fts.after_write", 1, 1),
    ],
)
def test_capture_sigkill_converges_after_fresh_restart(
    tmp_path: Path,
    failpoint_name: str,
    files_before: int,
    rows_before: int,
) -> None:
    root = tmp_path / failpoint_name.replace(".", "-")
    _crash(root, "capture-write", failpoint_name)
    assert len(list((root / "capture-buffer").glob("*.json"))) == files_before
    assert _capture_db_count(root) == rows_before

    first = _run(root, "capture-recover")
    second = _run(root, "capture-recover")

    assert first["sqlite"] == second["sqlite"] == "ok"
    assert first["files"] == second["files"] == (0 if files_before == 0 else 1)
    assert first["indexed"] == second["indexed"] == first["files"]
    assert first["file_digest"] == second["file_digest"]
    assert first["index_digest"] == second["index_digest"]
    assert first["temps"] == second["temps"] == 0


@pytest.mark.parametrize(
    "failpoint_name",
    [
        "timeline.block.before_commit",
        "timeline.block.after_commit",
        "timeline.watermark.before_write",
        "timeline.watermark.after_write",
    ],
)
def test_timeline_sigkill_converges_block_and_watermark(
    tmp_path: Path,
    failpoint_name: str,
) -> None:
    root = tmp_path / failpoint_name.replace(".", "-")
    _crash(root, "timeline-seed-tick", failpoint_name)

    first = _run(root, "timeline-recover")
    second = _run(root, "timeline-recover")

    assert first["sqlite"] == second["sqlite"] == "ok"
    assert first["blocks"] == second["blocks"] == 1
    assert first["block_windows"] == second["block_windows"] == 1
    assert first["block_digest"] == second["block_digest"]
    assert first["sources"] == second["sources"] == 1
    assert first["processed_from"] == "2026-08-08T12:00:00+00:00"
    assert first["processed_through"] == "2026-08-08T12:02:00+00:00"
    assert second["processed_through"] == first["processed_through"]


@pytest.mark.parametrize(
    ("failpoint_name", "memory_temps_at_boundary"),
    [
        ("memory.markdown.before_rename", 1),
        ("memory.markdown.after_rename", 0),
        ("memory.fts.before_write", 0),
        ("memory.fts.after_write", 0),
        ("session.status.before_reduced", 0),
        ("session.status.after_reduced", 0),
    ],
)
def test_reducer_sigkill_converges_markdown_projection_and_status(
    tmp_path: Path,
    failpoint_name: str,
    memory_temps_at_boundary: int,
) -> None:
    root = tmp_path / failpoint_name.replace(".", "-")
    assert _run(root, "session-seed")["seeded"] is True
    _crash(root, "session-reduce", failpoint_name)
    boundary = _run(root, "session-inspect")
    assert boundary["memory_temp_count"] == memory_temps_at_boundary

    first = _run(root, "session-reduce")
    second = _run(root, "session-reduce")
    inspected = _run(root, "session-inspect")

    for state in (first, second, inspected):
        assert state["sqlite"] == "ok"
        assert state["durable_entries"] == 1
        assert state["indexed_entries"] == 1
        assert state["file_entry_count"] == 1
        assert state["provenance_edges"] >= 1
        assert state["session_status"] == "reduced"
        assert state["memory_temp_count"] == 0
    assert first["startup_memory_temps_removed"] == memory_temps_at_boundary
    assert second["startup_memory_temps_removed"] == 0
    assert first["durable_digest"] == second["durable_digest"] == inspected["durable_digest"]
    assert first["index_digest"] == second["index_digest"] == inspected["index_digest"]


def test_real_daemon_startup_removes_markdown_crash_temp_before_replay(
    tmp_path: Path,
) -> None:
    root = tmp_path / "daemon-markdown-startup"
    assert _run(root, "session-seed")["seeded"] is True
    _crash(root, "session-reduce", "memory.markdown.before_rename")
    assert _run(root, "session-inspect")["memory_temp_count"] == 1

    _run_daemon_startup_cycle(root)

    after_startup = _run(root, "session-inspect")
    assert after_startup["sqlite"] == "ok"
    assert after_startup["memory_temp_count"] == 0
    assert after_startup["durable_entries"] == after_startup["indexed_entries"] == 0
    recovered = _run(root, "session-reduce")
    assert recovered["session_status"] == "reduced"
    assert recovered["durable_entries"] == recovered["indexed_entries"] == 1


@pytest.mark.parametrize(
    ("failpoint_name", "status_at_boundary", "recovered_on_restart"),
    [
        ("session.status.before_ended", "active", 1),
        ("session.status.after_ended", "ended", 0),
    ],
)
def test_session_end_sigkill_recovers_orphan_once(
    tmp_path: Path,
    failpoint_name: str,
    status_at_boundary: str,
    recovered_on_restart: int,
) -> None:
    root = tmp_path / failpoint_name.replace(".", "-")
    assert _run(root, "session-status-seed")["session_status"] == "active"
    _crash(root, "session-mark-ended", failpoint_name)

    boundary = _run(root, "session-inspect")
    assert boundary["sqlite"] == "ok"
    assert boundary["session_status"] == status_at_boundary
    assert boundary["session_end"] == (
        "" if status_at_boundary == "active" else "2026-08-08T13:01:00+00:00"
    )

    first = _run(root, "session-recover-ended")
    second = _run(root, "session-recover-ended")

    assert first["recovered"] == recovered_on_restart
    assert second["recovered"] == 0
    for state in (first, second):
        assert state["sqlite"] == "ok"
        assert state["session_status"] == "ended"
        assert state["session_end"] == "2026-08-08T13:01:00+00:00"
        assert state["active_sessions"] == 0
        assert state["pending_reductions"] == 1
        assert state["durable_entries"] == state["indexed_entries"] == 0
        assert state["memory_temp_count"] == 0


@pytest.mark.parametrize(
    ("failpoint_name", "status_at_boundary", "retries_at_boundary"),
    [
        ("session.status.before_failed", "ended", 0),
        ("session.status.after_failed", "failed", 1),
    ],
)
def test_session_failed_sigkill_respects_durable_backoff_on_recovery(
    tmp_path: Path,
    failpoint_name: str,
    status_at_boundary: str,
    retries_at_boundary: int,
) -> None:
    root = tmp_path / failpoint_name.replace(".", "-")
    assert _run(root, "session-status-seed")["session_status"] == "active"
    _crash(root, "session-mark-failed", failpoint_name)

    boundary = _run(root, "session-inspect")
    assert boundary["sqlite"] == "ok"
    assert boundary["session_status"] == status_at_boundary
    assert boundary["retry_count"] == retries_at_boundary
    assert bool(boundary["next_retry_at"]) is (status_at_boundary == "failed")

    first = _run(root, "session-recover-failed")
    second = _run(root, "session-recover-failed")

    for state in (first, second):
        assert state["sqlite"] == "ok"
        assert state["session_status"] == "failed"
        assert state["session_end"] == "2026-08-08T13:01:00+00:00"
        assert state["retry_count"] == 1
        assert state["next_retry_at"]
        assert state["last_error"] == (
            "reducer LLM call failed or returned unparseable JSON"
        )
        assert state["active_sessions"] == 0
        assert state["pending_reductions"] == 1
        assert state["durable_entries"] == state["indexed_entries"] == 0
        assert state["written"] == 0
        assert state["memory_temp_count"] == 0
    assert second["next_retry_at"] == first["next_retry_at"]
    assert second["retry_count"] == first["retry_count"]


@pytest.mark.parametrize(
    ("failpoint_name", "flush_at_boundary"),
    [
        ("session.status.before_flush", ""),
        ("session.status.after_flush", "2026-08-08T13:01:00+00:00"),
    ],
)
def test_session_flush_sigkill_repairs_progress_without_duplicate_entry(
    tmp_path: Path,
    failpoint_name: str,
    flush_at_boundary: str,
) -> None:
    root = tmp_path / failpoint_name.replace(".", "-")
    assert _run(root, "session-status-seed")["session_status"] == "active"
    _crash(root, "session-flush", failpoint_name)

    boundary = _run(root, "session-inspect")
    assert boundary["sqlite"] == "ok"
    assert boundary["session_status"] == "active"
    assert boundary["flush_end"] == flush_at_boundary
    assert boundary["durable_entries"] == boundary["indexed_entries"] == 1

    first = _run(root, "session-flush")
    second = _run(root, "session-flush")
    inspected = _run(root, "session-inspect")

    for state in (first, second, inspected):
        assert state["sqlite"] == "ok"
        assert state["session_status"] == "active"
        assert state["session_end"] == ""
        assert state["flush_end"] == "2026-08-08T13:01:00+00:00"
        assert state["active_sessions"] == 1
        assert state["pending_reductions"] == 0
        assert state["durable_entries"] == 1
        assert state["indexed_entries"] == 1
        assert state["file_entry_count"] == 1
        assert state["provenance_edges"] == 1
        assert state["memory_temp_count"] == 0
    assert first["written"] is False
    assert second["written"] is False
    assert first["durable_digest"] == second["durable_digest"]
    assert second["durable_digest"] == inspected["durable_digest"]
    assert first["index_digest"] == second["index_digest"]
    assert second["index_digest"] == inspected["index_digest"]
