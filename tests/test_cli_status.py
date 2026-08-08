"""Tests for `openchronicle status` — particularly the per-stage LLM probes."""

from __future__ import annotations

import json
import os
import signal
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openchronicle import __version__, cli, daemon, paths
from openchronicle import config as config_mod
from openchronicle.writer import llm as llm_mod


def test_status_renders_mocked_pings(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OPENCHRONICLE_LLM_MOCK=1 short-circuits each stage probe to '✓ mocked'."""
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Model (timeline)" in out
    assert "Model (reducer)" in out
    assert "Model (classifier)" in out
    assert "Model (daily_wrap)" in out
    assert "Model (compact)" in out
    # All five stages share the default model, so they all show the mocked tick.
    assert out.count("mocked") >= 5


def test_ping_stages_dedups_identical_configs(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stages with identical (model, base_url, api_key) only ping the network once.

    The default config gives every stage the same model, so a five-stage
    status call should invoke ping_stage exactly once and reuse the result
    via dataclasses.replace for the other four.
    """
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    call_count = {"n": 0}

    def counting_ping(cfg, stage, *, timeout=5.0):  # noqa: ARG001
        call_count["n"] += 1
        return llm_mod.PingResult(
            stage=stage,
            model=cfg.model_for(stage).model,
            ok=True,
            latency_ms=42,
            error=None,
        )

    monkeypatch.setattr(llm_mod, "ping_stage", counting_ping)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0, result.output
    # All five stages share the default model, so dedup collapses to one network call.
    assert call_count["n"] == 1, f"expected 1 ping_stage call, got {call_count['n']}"
    # …but every stage row still shows a tick — the result was replicated, not skipped.
    assert result.output.count("42 ms") == 5


def test_status_renders_probe_failure(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ping_stage raises, status shows ✗ <ErrorClass> and still exits 0."""
    # Make sure mock-mode is OFF so the real ping_stage path runs.
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)

    def boom(cfg, stage, *, timeout=5.0):  # noqa: ARG001
        return llm_mod.PingResult(
            stage=stage,
            model=cfg.model_for(stage).model,
            ok=False,
            latency_ms=None,
            error="AuthenticationError",
        )

    monkeypatch.setattr(llm_mod, "ping_stage", boom)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "AuthenticationError" in result.output
    assert "✗" in result.output


def test_status_probes_outside_lock_then_fences_snapshot_and_serialization(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openchronicle.services import snapshot as snapshot_mod

    cfg = config_mod.Config()
    state = {"locked": False}
    events: list[str] = []

    @contextmanager
    def tracked_privacy_fence():
        assert state["locked"] is False
        state["locked"] = True
        events.append("lock.enter")
        try:
            yield
        finally:
            events.append("lock.exit")
            state["locked"] = False

    def probe(_cfg, _stages):
        assert state["locked"] is False
        events.append("probe")
        return {}

    def snapshot(_conn, _cfg, **_limits):
        assert state["locked"] is True
        events.append("snapshot")
        return {
            "capture": {"indexed_count": 0, "last": None},
            "counts": {
                "sessions": {"total": 0, "reduced": 0, "ended": 0, "failed": 0},
                "memory": {"active_files": 0, "dormant_files": 0, "entries": 0},
                "timeline_blocks": 0,
                "candidates": {},
            },
            "daily_wrap": {"wraps": []},
        }

    def serialize(_table) -> None:
        assert state["locked"] is True
        events.append("serialize")

    monkeypatch.setattr(cli, "_init", lambda: cfg)
    monkeypatch.setattr(cli, "_ping_stages", probe)
    monkeypatch.setattr(cli, "privacy_egress_lock", tracked_privacy_fence)
    monkeypatch.setattr(snapshot_mod, "build_snapshot", snapshot)
    monkeypatch.setattr(cli.console, "print", serialize)

    result = CliRunner().invoke(cli.app, ["status"])

    assert result.exit_code == 0, result.output
    assert events == ["probe", "lock.enter", "snapshot", "serialize", "lock.exit"]

# ═══════════════════════════════════════════════════════════════════
#  Status helper unit tests
# ═══════════════════════════════════════════════════════════════════


def test_daemon_uptime_stopped_when_no_pid(ac_root: Path) -> None:
    """Returns "stopped" when the daemon is not running."""
    assert cli._daemon_uptime() == "stopped"


def test_daemon_uptime_running(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns a human-readable uptime when PID file exists."""
    paths.pid_file().write_text("99999")
    monkeypatch.setattr(cli, "_read_pid", lambda: 99999)

    uptime = cli._daemon_uptime()

    assert uptime != "stopped"
    assert "m" in uptime  # recently created PID file → minutes-level uptime


def test_pid_is_untrusted_without_daemon_lease(ac_root: Path) -> None:
    paths.pid_file().write_text(str(os.getpid()))

    assert cli._read_pid() is None


def test_pid_is_trusted_while_daemon_lease_is_held(ac_root: Path) -> None:
    lease_fd = daemon._acquire_daemon_lock()
    paths.pid_file().write_text(str(os.getpid()))
    try:
        assert cli._read_pid() == os.getpid()
    finally:
        daemon._release_daemon_lock(lease_fd)


@pytest.mark.parametrize("unsafe_pid", [-1, 0, 1])
def test_pid_rejects_process_group_and_init_values(
    ac_root: Path, unsafe_pid: int
) -> None:
    lease_fd = daemon._acquire_daemon_lock()
    paths.daemon_lock_file().write_text(str(unsafe_pid))
    paths.pid_file().write_text(str(unsafe_pid))
    try:
        assert cli._read_pid() is None
    finally:
        daemon._release_daemon_lock(lease_fd)


def test_stop_never_signals_pid_that_mismatches_held_lease(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease_fd = daemon._acquire_daemon_lock()
    paths.pid_file().write_text(str(os.getpid()))
    paths.daemon_lock_file().write_text("999999")
    signals: list[tuple[int, int]] = []

    def record_signal(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(cli, "_init", lambda: config_mod.Config())
    monkeypatch.setattr(cli.os, "kill", record_signal)
    try:
        result = CliRunner().invoke(cli.app, ["stop"])
    finally:
        daemon._release_daemon_lock(lease_fd)

    assert result.exit_code == 1
    assert signals == []


def test_stop_never_signals_live_recycled_pid_without_lease(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.pid_file().write_text(str(os.getpid()))
    signals: list[tuple[int, int]] = []

    def record_signal(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(cli, "_init", lambda: config_mod.Config())
    monkeypatch.setattr(cli.os, "kill", record_signal)
    result = CliRunner().invoke(cli.app, ["stop"])

    assert result.exit_code == 1
    assert (os.getpid(), signal.SIGTERM) not in signals


def test_health_stopped() -> None:
    """(None, None) → "stopped", "red"."""
    label, style = cli._health_status(None, None)
    assert label == "stopped"
    assert style == "red"


def test_health_running_no_captures() -> None:
    """PID exists but no last timestamp → "running (no captures yet)", "yellow"."""
    label, style = cli._health_status(9999, None)
    assert "no captures" in label
    assert style == "yellow"


def test_health_healthy() -> None:
    """Timestamp within 5 minutes → "healthy", "green"."""
    label, style = cli._health_status(9999, datetime.now().isoformat())
    assert label == "healthy"
    assert style == "green"


def test_health_stale() -> None:
    """Timestamp older than 5 minutes → "stale", "yellow"."""
    old = (datetime.now() - timedelta(minutes=10)).isoformat()
    label, style = cli._health_status(9999, old)
    assert "stale" in label
    assert style == "yellow"


def test_health_tz_aware_timestamp() -> None:
    """Offset-aware timestamps don't cause TypeError in subtraction."""
    label, style = cli._health_status(9999, "2026-04-22T14:00:00+08:00")
    assert label in ("healthy", "stale (no captures in >5m)")
    assert "red" not in style  # not stopped


def test_health_malformed_timestamp() -> None:
    """Unparseable timestamps are handled gracefully."""
    label, style = cli._health_status(9999, "not-a-timestamp")
    assert label == "running"
    assert style == "green"


def test_last_capture_none_when_dir_missing(ac_root: Path) -> None:
    """No capture-buffer dir → (None, None)."""
    ts, app = cli._last_capture_info()
    assert ts is None
    assert app is None


def test_last_capture_finds_newest(ac_root: Path) -> None:
    """Returns timestamp and app_name from the most recent buffer file."""
    buf = paths.capture_buffer_dir()
    buf.mkdir(parents=True, exist_ok=True)
    (buf / "c1.json").write_text(json.dumps({
        "timestamp": "2026-04-22T14:00:00+08:00",
        "window_meta": {
            "app_name": "Cursor",
            "bundle_id": "com.todesktop.230313mzl4w4u92",
            "title": "Project",
        },
    }))
    (buf / "c2.json").write_text(json.dumps({
        "timestamp": "2026-04-22T14:05:00+08:00",
        "window_meta": {
            "app_name": "Safari",
            "bundle_id": "com.apple.Safari",
            "title": "Documentation",
        },
    }))

    ts, app = cli._last_capture_info()
    assert ts == "2026-04-22T14:05:00+08:00", f"got ts={ts!r}"
    assert app == "Safari", f"got app={app!r}"


def test_last_capture_uses_absolute_time_across_dst_fallback(ac_root: Path) -> None:
    buf = paths.capture_buffer_dir()
    older = buf / "2026-11-01T01-59-59m04-00.json"
    newer = buf / "2026-11-01T01-00-00m05-00.json"
    older.write_text(json.dumps({
        "timestamp": "2026-11-01T01:59:59-04:00",
        "window_meta": {
            "app_name": "Older",
            "bundle_id": "com.example.older",
            "title": "Older window",
        },
    }))
    newer.write_text(json.dumps({
        "timestamp": "2026-11-01T01:00:00-05:00",
        "window_meta": {
            "app_name": "Newer",
            "bundle_id": "com.example.newer",
            "title": "Newer window",
        },
    }))

    ts, app = cli._last_capture_info()

    assert ts == "2026-11-01T01:00:00-05:00"
    assert app == "Newer"


def test_last_capture_handles_corrupted_json(ac_root: Path) -> None:
    """Corrupted JSON is never surfaced as capture metadata."""
    buf = paths.capture_buffer_dir()
    buf.mkdir(parents=True, exist_ok=True)
    (buf / "bad.json").write_text("{not valid json")

    ts, app = cli._last_capture_info()
    assert ts is None
    assert app is None


# ═══════════════════════════════════════════════════════════════════
#  Status command integration tests
# ═══════════════════════════════════════════════════════════════════


def test_status_renders_new_fields(ac_root: Path) -> None:
    """Status output includes Version, Uptime, Health, and Last Capture."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Version" in result.output
    assert "Uptime" in result.output
    assert "Health" in result.output
    assert "Last Capture" in result.output
    assert "Classifier Delivery" in result.output


def test_status_shows_version(ac_root: Path) -> None:
    """Status table includes the installed version string."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert __version__ in result.output
