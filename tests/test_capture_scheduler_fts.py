"""capture/scheduler.py: write-through to captures_fts + delete-through on cleanup."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openchronicle import cli
from openchronicle.capture import scheduler as scheduler_mod
from openchronicle.config import CaptureConfig, Config
from openchronicle.mcp import captures as mcp_captures
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.privacy import policy as privacy_policy
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import (
    EvidenceRef,
    timeline_block_sources_digest,
)
from openchronicle.store import fts
from openchronicle.timeline import aggregator as timeline_aggregator
from openchronicle.timeline import store as timeline_store


def _capture_dict(
    *,
    ts: str,
    app: str,
    title: str,
    value: str,
    text: str,
) -> dict:
    return {
        "timestamp": ts,
        "schema_version": 2,
        "trigger": {"event_type": "manual"},
        "window_meta": {
            "app_name": app,
            "title": title,
            "bundle_id": "com.test." + app.lower(),
        },
        "focused_element": {
            "role": "AXTextArea",
            "value": value,
            "is_editable": True,
            "value_length": len(value),
        },
        "visible_text": text,
        "url": "",
        "screenshot": {
            "image_base64": "AAAA",
            "mime_type": "image/jpeg",
            "width": 100,
            "height": 50,
        },
    }


def _receipt_captures(*capture_paths: Path) -> None:
    """Mark the current bytes as consumed by the timeline test fixture."""
    grouped: dict[tuple[datetime, datetime], list[tuple[Path, dict]]] = {}
    for path in capture_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        capture_time = datetime.fromisoformat(data["timestamp"])
        window_start = timeline_store.floor_to_window(capture_time, 1)
        window_end = timeline_store.add_elapsed(window_start, timedelta(minutes=1))
        grouped.setdefault((window_start, window_end), []).append((path, data))
    with fts.cursor() as conn:
        timeline_store.activate_capture_receipts(conn)
        for (window_start, window_end), parsed in grouped.items():
            sources = timeline_aggregator._capture_sources(parsed)
            block = timeline_store.TimelineBlock(
                start_time=window_start,
                end_time=window_end,
                entries=["test cleanup receipt"],
                capture_count=len(parsed),
                source_digest=timeline_block_sources_digest(sources),
            )
            timeline_store.insert(conn, block)
            provenance_store.replace_sources(
                conn,
                subject=EvidenceRef(kind="timeline_block", id=block.id),
                sources=sources,
            )
            bindings = timeline_aggregator.capture_receipt_bindings(parsed)
            timeline_store.record_capture_receipts(
                conn,
                bindings=bindings,
                window_start=window_start,
                window_end=window_end,
            )
            timeline_store.record_window_receipt(
                conn,
                timeline_store.make_window_receipt(
                    window_start=window_start,
                    window_end=window_end,
                    bindings=bindings,
                    policy_digest=privacy_policy.stored_observation_policy_digest(CaptureConfig()),
                    outcome="block",
                    block=block,
                ),
            )


def test_write_capture_indexes_into_fts(ac_root: Path) -> None:
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="main.py",
        value="def foo()",
        text="def foo(): return 1",
    )
    path = scheduler_mod._write_capture(out)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    with fts.cursor() as conn:
        hits = fts.search_captures(conn, query="foo")
        assert len(hits) == 1
        assert hits[0].id == path.stem
        assert hits[0].app_name == "Cursor"


def test_write_capture_never_overwrites_same_timestamp(ac_root: Path) -> None:
    first = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="first.py",
        value="first",
        text="first capture",
    )
    second = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="second.py",
        value="second",
        text="second capture",
    )

    first_path = scheduler_mod._write_capture(first)
    second_path = scheduler_mod._write_capture(second)

    assert first_path != second_path
    assert first_path.exists()
    assert second_path.exists()
    with fts.cursor() as conn:
        assert len(fts.recent_captures(conn, limit=10)) == 2


def test_built_capture_gets_authoritative_timestamp_at_locked_persistence(
    ac_root: Path,
    monkeypatch,
) -> None:
    old_timestamp = "2026-04-22T14:00:00+08:00"
    persisted_timestamp = "2026-04-22T14:02:00.125+08:00"
    out = _capture_dict(
        ts=old_timestamp,
        app="Cursor",
        title="slow-ax.py",
        value="durable",
        text="slow collection must not land in a closed bucket",
    )
    out["_timestamp_at_persist"] = True
    monkeypatch.setattr(scheduler_mod, "_now_iso", lambda: persisted_timestamp)

    path = scheduler_mod._write_capture(out)

    assert out["timestamp"] == persisted_timestamp
    assert "14-02-00.125" in path.name
    old_end = scheduler_mod.filenames.parse_timestamp("2026-04-22T14:01:00+08:00")
    assert old_end is not None
    assert timeline_aggregator.capture_paths_by_window(old_end, 1) == {}


def test_runner_uses_shared_timestamp_for_json_and_session_hook(
    ac_root: Path,
    monkeypatch,
) -> None:
    persisted = datetime(2026, 4, 22, 6, 2, 0, 125000, tzinfo=UTC)
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="shared-clock.py",
        value="durable",
        text="capture and session must share one exact instant",
    )
    out["_timestamp_at_persist"] = True
    events: list[dict] = []
    runner = scheduler_mod._CaptureRunner(
        CaptureConfig(),
        object(),
        pre_capture_hook=events.append,
        timestamp_provider=lambda: persisted,
    )
    monkeypatch.setattr(scheduler_mod, "_build_capture", lambda *_args: out)
    monkeypatch.setattr(
        scheduler_mod,
        "_now_iso",
        lambda: (_ for _ in ()).throw(AssertionError("host wall clock was used")),
    )

    runner.run(None)

    expected = "2026-04-22T06:02:00.125+00:00"
    assert out["timestamp"] == expected
    assert events[0]["timestamp"] == expected
    assert list((ac_root / "capture-buffer").glob("*.json"))


def test_failed_write_does_not_poison_content_dedup(ac_root: Path, monkeypatch) -> None:
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="retry.py",
        value="durable",
        text="retry after transient write failure",
    )
    runner = scheduler_mod._CaptureRunner(CaptureConfig(), object())
    attempts = 0

    monkeypatch.setattr(scheduler_mod, "_build_capture", lambda *_args: out)

    def flaky_write(_out):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient disk failure")
        return ac_root / "capture-buffer" / "written.json"

    monkeypatch.setattr(scheduler_mod, "_write_capture", flaky_write)

    runner.run(None)
    runner.run(None)
    runner.run(None)

    assert attempts == 2


def test_heartbeat_capture_creates_identity_event_for_session_hook(
    ac_root: Path, monkeypatch
) -> None:
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="heartbeat.py",
        value="durable",
        text="heartbeat session evidence",
    )
    out["trigger"] = {"event_type": "heartbeat"}
    events: list[dict] = []
    runner = scheduler_mod._CaptureRunner(
        CaptureConfig(),
        object(),
        pre_capture_hook=events.append,
    )
    monkeypatch.setattr(scheduler_mod, "_build_capture", lambda *_args: out)
    monkeypatch.setattr(
        scheduler_mod,
        "_write_capture",
        lambda _out: ac_root / "capture-buffer" / "written.json",
    )

    runner.run(None)
    runner.run(None)

    assert events == [
        {
            "event_type": "heartbeat",
            "app_name": "Cursor",
            "bundle_id": "com.test.cursor",
            "window_title": "heartbeat.py",
            "timestamp": "2026-04-22T14:00:00+08:00",
        }
    ]


def test_session_hook_never_receives_watcher_content_details(ac_root: Path, monkeypatch) -> None:
    marker = "SECRET-WATCHER-HOOK-VALUE"
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="safe.py",
        value="safe",
        text="safe evidence",
    )
    out["trigger"] = {
        "event_type": "UserTextInput",
        "details": {"value": marker},
    }
    events: list[dict] = []
    runner = scheduler_mod._CaptureRunner(
        CaptureConfig(),
        object(),
        pre_capture_hook=events.append,
    )
    monkeypatch.setattr(scheduler_mod, "_build_capture", lambda *_args: out)
    monkeypatch.setattr(
        scheduler_mod,
        "_write_capture",
        lambda _out: ac_root / "capture-buffer" / "written.json",
    )

    runner.run(
        {
            "event_type": "UserTextInput",
            "details": {"value": marker},
        }
    )

    assert len(events) == 1
    assert events[0]["event_type"] == "UserTextInput"
    assert "details" not in events[0]
    assert marker not in repr(events)


def test_stop_worker_discards_full_backlog_and_active_native_result(
    ac_root: Path, monkeypatch
) -> None:
    """A timed-out native build cannot write or fire hooks after shutdown."""
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="shutdown.py",
        value="durable",
        text="must be discarded after shutdown begins",
    )
    build_started = threading.Event()
    release_build = threading.Event()
    writes: list[dict] = []
    hooks: list[dict] = []

    def blocking_build(*_args):
        build_started.set()
        assert release_build.wait(timeout=5)
        return out

    runner = scheduler_mod._CaptureRunner(
        CaptureConfig(),
        object(),
        pre_capture_hook=hooks.append,
    )
    monkeypatch.setattr(scheduler_mod, "_build_capture", blocking_build)
    monkeypatch.setattr(
        scheduler_mod,
        "_write_capture",
        lambda capture: writes.append(capture),
    )

    runner.start_worker()
    runner.run_threaded({"event_type": "active"})
    assert build_started.wait(timeout=5)
    for index in range(runner._MAX_PENDING):
        runner.run_threaded({"event_type": f"queued-{index}"})

    runner.stop_worker(timeout=0.01)
    old_worker = runner._worker
    assert runner._accepting is False
    assert old_worker is not None and old_worker.is_alive()
    assert writes == []
    assert hooks == []

    # Triggers after stop are rejected, and the active native result is
    # discarded when it eventually returns.
    runner.run_threaded({"event_type": "too-late"})
    release_build.set()
    old_worker.join(timeout=5)
    assert not old_worker.is_alive()
    assert runner._worker is None
    assert writes == []
    assert hooks == []


@pytest.mark.asyncio
async def test_heartbeat_uses_bounded_worker_and_stops_on_cancellation(monkeypatch) -> None:
    queued: list[dict | None] = []
    stopped: list[bool] = []

    class Provider:
        available = True

    class FakeRunner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start_worker(self) -> None:
            return None

        def run(self, _trigger) -> None:
            raise AssertionError("heartbeat bypassed the bounded worker")

        def run_threaded(self, trigger) -> None:
            queued.append(trigger)

        def stop_worker(self) -> None:
            stopped.append(True)

    sleep_calls = 0

    async def one_heartbeat_then_cancel(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    cfg = CaptureConfig()
    cfg.event_driven = False
    cfg.heartbeat_minutes = 1
    monkeypatch.setattr(scheduler_mod.ax_capture, "create_provider", lambda **_kw: Provider())
    monkeypatch.setattr(scheduler_mod, "_CaptureRunner", FakeRunner)
    monkeypatch.setattr(scheduler_mod.asyncio, "sleep", one_heartbeat_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await scheduler_mod.run_forever(cfg)

    assert queued == [None, None]
    assert stopped == [True]


def test_cleanup_buffer_removes_fts_rows(ac_root: Path) -> None:
    """Time-based delete pass should also drop matching FTS rows."""
    captures = [
        ("2026-04-22T10:00:00+08:00", "old1"),
        ("2026-04-22T11:00:00+08:00", "old2"),
        ("2026-04-22T12:00:00+08:00", "keep"),
    ]
    written: list[Path] = []
    for ts, marker in captures:
        out = _capture_dict(
            ts=ts,
            app="Cursor",
            title=f"win-{marker}",
            value="",
            text=f"unique-text-{marker}",
        )
        written.append(scheduler_mod._write_capture(out))

    with fts.cursor() as conn:
        assert len(fts.recent_captures(conn, limit=10)) == 3

    # Backdate the two "old" files so the delete pass picks them up.
    long_ago = time.time() - 10 * 24 * 3600
    for p in written[:2]:
        os.utime(p, (long_ago, long_ago))
    _receipt_captures(*written)

    # processed_before_ts past every stem so all are considered "absorbed".
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=24,
        processed_before_ts="2099-01-01T00:00:00+00:00",
        screenshot_retention_hours=None,
        max_mb=0,
    )
    assert stats["deleted"] == 2
    assert stats["evicted"] == 0

    with fts.cursor() as conn:
        rec = fts.recent_captures(conn, limit=10)
        assert {h.id for h in rec} == {written[2].stem}


def test_cleanup_batches_fts_delete_before_unlink(
    ac_root: Path,
    monkeypatch,
) -> None:
    written: list[Path] = []
    for index in range(3):
        out = _capture_dict(
            ts=f"2026-04-22T1{index}:00:00+08:00",
            app="Cursor",
            title=f"private-{index}.py",
            value="",
            text=f"private batch {index}",
        )
        written.append(scheduler_mod._write_capture(out))

    long_ago = time.time() - 10 * 24 * 3600
    for path in written:
        os.utime(path, (long_ago, long_ago))
    _receipt_captures(*written)

    real_delete = scheduler_mod._delete_captures_from_fts
    calls: list[list[str]] = []

    def recording_delete(stems: list[str]) -> bool:
        calls.append(stems)
        assert all(path.exists() for path in written)
        return real_delete(stems)

    monkeypatch.setattr(
        scheduler_mod,
        "_delete_captures_from_fts",
        recording_delete,
    )

    stats = scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
    )

    assert stats["deleted"] == 3
    assert calls == [[path.stem for path in written]]


def test_cleanup_keeps_whole_window_at_fractional_processed_boundary(
    ac_root: Path,
) -> None:
    before = _capture_dict(
        ts="2026-04-22T14:00:00.010+08:00",
        app="Cursor",
        title="before.py",
        value="",
        text="before boundary",
    )
    after = _capture_dict(
        ts="2026-04-22T14:00:00.100+08:00",
        app="Cursor",
        title="after.py",
        value="",
        text="after boundary",
    )
    before_path = scheduler_mod._write_capture(before)
    after_path = scheduler_mod._write_capture(after)
    long_ago = time.time() - 10 * 24 * 3600
    os.utime(before_path, (long_ago, long_ago))
    os.utime(after_path, (long_ago, long_ago))
    _receipt_captures(before_path, after_path)

    stats = scheduler_mod.cleanup_buffer(
        retention_hours=24,
        processed_before_ts="2026-04-22T14:00:00.050+08:00",
        screenshot_retention_hours=None,
        max_mb=0,
    )

    assert stats["deleted"] == 0
    assert before_path.exists()
    assert after_path.exists()
    with fts.cursor() as conn:
        assert {row.id for row in fts.recent_captures(conn, limit=10)} == {
            before_path.stem,
            after_path.stem,
        }


def test_cleanup_without_valid_processed_boundary_fails_closed(ac_root: Path) -> None:
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="unprocessed.py",
        value="",
        text="must survive",
    )
    path = scheduler_mod._write_capture(out)
    long_ago = time.time() - 10 * 24 * 3600
    os.utime(path, (long_ago, long_ago))

    no_boundary = scheduler_mod.cleanup_buffer(retention_hours=1)
    invalid_boundary = scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="not-a-timestamp",
    )

    assert no_boundary == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert invalid_boundary == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert path.exists()


def test_cleanup_before_receipt_activation_fails_closed(ac_root: Path) -> None:
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="upgrade-before-replay.py",
        value="",
        text="watermark alone cannot authorize deletion",
    )
    path = scheduler_mod._write_capture(out)
    long_ago = time.time() - 10 * 24 * 3600
    os.utime(path, (long_ago, long_ago))

    stats = scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
        max_mb=1,
    )

    assert stats == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert path.exists()


def test_cleanup_rejects_receipt_for_replaced_same_name_capture(ac_root: Path) -> None:
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="original.py",
        value="",
        text="original consumed bytes",
    )
    path = scheduler_mod._write_capture(out)
    _receipt_captures(path)
    long_ago = time.time() - 10 * 24 * 3600
    os.utime(path, (long_ago, long_ago))

    replaced = json.loads(path.read_text(encoding="utf-8"))
    replaced["visible_text"] = "late replacement under the same filename"
    scheduler_mod._atomic_write_json(path, replaced, preserve_times=True)

    stats = scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
        max_mb=1,
    )

    assert stats == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert path.exists()


def test_cleanup_purges_crash_orphan_capture_temp_without_watermark(
    ac_root: Path,
) -> None:
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="private.py",
        value="secret",
        text="orphan temp secret",
    )
    path = scheduler_mod._write_capture(out)
    orphan = path.parent / f".{path.name}.deadbeef.tmp"
    orphan.write_text("SENSITIVE_CRASH_COPY", encoding="utf-8")

    stats = scheduler_mod.cleanup_buffer(retention_hours=24)

    assert stats == {"deleted": 1, "stripped": 0, "evicted": 0}
    assert not orphan.exists()
    assert path.exists()


def test_screenshot_strip_preserves_whole_capture_retention_age(
    ac_root: Path,
) -> None:
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="retention.py",
        value="",
        text="retention marker",
    )
    path = scheduler_mod._write_capture(out)
    ten_days_ago = time.time() - 10 * 24 * 3600
    os.utime(path, (ten_days_ago, ten_days_ago))
    _receipt_captures(path)

    stripped = scheduler_mod.cleanup_buffer(
        retention_hours=30 * 24,
        processed_before_ts="2099-01-01T00:00:00+00:00",
        screenshot_retention_hours=24,
    )

    assert stripped == {"deleted": 0, "stripped": 1, "evicted": 0}
    assert abs(path.stat().st_mtime - ten_days_ago) < 1

    deleted = scheduler_mod.cleanup_buffer(
        retention_hours=7 * 24,
        processed_before_ts="2099-01-01T00:00:00+00:00",
        screenshot_retention_hours=24,
    )
    assert deleted == {"deleted": 1, "stripped": 0, "evicted": 0}
    assert not path.exists()


def test_cleanup_retains_json_when_fts_delete_fails(
    ac_root: Path,
    monkeypatch,
) -> None:
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="private.py",
        value="",
        text="must not remain searchable without its source",
    )
    path = scheduler_mod._write_capture(out)
    long_ago = time.time() - 10 * 24 * 3600
    os.utime(path, (long_ago, long_ago))
    _receipt_captures(path)
    monkeypatch.setattr(
        scheduler_mod,
        "_delete_captures_from_fts",
        lambda _stems: False,
    )

    stats = scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
    )

    assert stats["deleted"] == 0
    assert path.exists()


def test_retention_unlink_failure_stays_tombstoned_and_recovers(
    ac_root: Path,
    monkeypatch,
) -> None:
    marker = "AUTOMATIC_RETENTION_UNLINK_SECRET"
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="private-retention.py",
        value="",
        text=marker,
    )
    path = scheduler_mod._write_capture(out)
    long_ago = time.time() - 10 * 24 * 3600
    os.utime(path, (long_ago, long_ago))
    _receipt_captures(path)
    real_unlink = Path.unlink

    def fail_target(target: Path, *args, **kwargs) -> None:
        if target == path:
            raise PermissionError("immutable automatic-retention capture")
        real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
    )

    assert stats == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert path.exists()
    with fts.cursor() as conn:
        assert fts.search_captures(conn, query=marker) == []
        tombstones = candidate_store.list_tombstones(conn, kind="capture_file")
    assert [row.artifact_id for row in tombstones] == [path.name]
    assert "unlink failed" in tombstones[0].last_error

    cfg = Config()
    cfg.capture.deny_unknown_windows = False
    assert mcp_captures.read_recent_capture(cfg=cfg) is None
    cli.rebuild_captures_index()
    assert mcp_captures.search_captures(cfg=cfg, query=marker) == []

    # A later pass can retry the residual file and clears the deny marker only
    # after unlink succeeds.
    monkeypatch.setattr(Path, "unlink", real_unlink)
    retry = scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
    )
    assert retry == {"deleted": 1, "stripped": 0, "evicted": 0}
    assert not path.exists()
    with fts.cursor() as conn:
        assert not candidate_store.is_tombstoned(
            conn,
            kind="capture_file",
            artifact_id=path.name,
        )


def test_size_eviction_unlink_failure_stays_tombstoned(
    ac_root: Path,
    monkeypatch,
) -> None:
    marker = "AUTOMATIC_SIZE_EVICTION_SECRET"
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="private-size.py",
        value="",
        text=marker + ("x" * 1_200_000),
    )
    path = scheduler_mod._write_capture(out)
    _receipt_captures(path)
    real_unlink = Path.unlink

    def fail_target(target: Path, *args, **kwargs) -> None:
        if target == path:
            raise PermissionError("immutable size-eviction capture")
        real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=24 * 365,
        processed_before_ts="2099-01-01T00:00:00+00:00",
        max_mb=1,
    )

    assert stats == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert path.exists()
    with fts.cursor() as conn:
        assert fts.search_captures(conn, query=marker) == []
        assert candidate_store.is_tombstoned(
            conn,
            kind="capture_file",
            artifact_id=path.name,
        )
    cfg = Config()
    cfg.capture.deny_unknown_windows = False
    assert mcp_captures.read_recent_capture(cfg=cfg) is None


def test_cleanup_eviction_also_drops_fts(ac_root: Path) -> None:
    """Size-based eviction should also drop matching FTS rows."""
    written: list[Path] = []
    for i in range(3):
        ts = f"2026-04-22T1{i}:00:00+08:00"
        out = _capture_dict(
            ts=ts,
            app="Cursor",
            title=f"w-{i}",
            value="",
            text="x" * 500_000,  # ~500 KB each → 1.5 MB total
        )
        written.append(scheduler_mod._write_capture(out))
    _receipt_captures(*written)

    # Tight 1 MB cap forces eviction of the oldest.
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=24 * 365,
        processed_before_ts="2099-01-01T00:00:00+00:00",
        screenshot_retention_hours=None,
        max_mb=1,
    )
    assert stats["evicted"] >= 1
    with fts.cursor() as conn:
        remaining = {h.id for h in fts.recent_captures(conn, limit=10)}
    assert len(remaining) == 3 - stats["evicted"]
    # Newest survives.
    assert written[-1].stem in remaining


def test_retention_never_splits_one_window_by_file_age(ac_root: Path) -> None:
    first = scheduler_mod._write_capture(
        _capture_dict(
            ts="2026-04-22T14:00:10+08:00",
            app="Cursor",
            title="same-window-old.py",
            value="",
            text="old member",
        )
    )
    second = scheduler_mod._write_capture(
        _capture_dict(
            ts="2026-04-22T14:00:40+08:00",
            app="Cursor",
            title="same-window-new.py",
            value="",
            text="new member",
        )
    )
    _receipt_captures(first, second)
    old = time.time() - 10 * 24 * 3600
    os.utime(first, (old, old))

    stats = scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
    )

    assert stats == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert first.exists() and second.exists()


def test_partial_window_unlink_resumes_from_durable_manifest_without_boundary(
    ac_root: Path,
    monkeypatch,
) -> None:
    first = scheduler_mod._write_capture(
        _capture_dict(
            ts="2026-04-22T15:00:10+08:00",
            app="Cursor",
            title="retire-first.py",
            value="",
            text="first retirement member",
        )
    )
    second = scheduler_mod._write_capture(
        _capture_dict(
            ts="2026-04-22T15:00:40+08:00",
            app="Cursor",
            title="retire-second.py",
            value="",
            text="second retirement member",
        )
    )
    _receipt_captures(first, second)
    old = time.time() - 10 * 24 * 3600
    for path in (first, second):
        os.utime(path, (old, old))
    real_unlink = Path.unlink

    def fail_second(target: Path, *args, **kwargs) -> None:
        if target == second:
            raise PermissionError("partial window unlink")
        real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second)
    first_pass = scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
    )
    assert first_pass == {"deleted": 1, "stripped": 0, "evicted": 0}
    assert not first.exists() and second.exists()
    with fts.cursor() as conn:
        receipts = timeline_store.window_receipts_in_raw_states(conn, "retiring")
        assert len(receipts) == 1
        assert len(timeline_store.capture_bindings_for_window(conn, receipts[0])) == 2
        tombstones = candidate_store.list_tombstones(conn, kind="capture_file")
        assert {row.artifact_id for row in tombstones} == {first.name, second.name}

    # Startup has no processed boundary. The frozen retiring manifest remains
    # independently resumable and finalizes only after every old member is absent.
    monkeypatch.setattr(Path, "unlink", real_unlink)
    retry = scheduler_mod.cleanup_buffer(retention_hours=1)
    assert retry == {"deleted": 1, "stripped": 0, "evicted": 0}
    assert not second.exists()
    with fts.cursor() as conn:
        retired = timeline_store.window_receipts_in_raw_states(conn, "retired")
        assert len(retired) == 1
        assert timeline_store.capture_receipt_paths(conn) == set()
        assert candidate_store.list_tombstones(conn, kind="capture_file") == []


def test_retiring_window_with_unexpected_late_path_does_not_finalize(
    ac_root: Path,
    monkeypatch,
) -> None:
    first = scheduler_mod._write_capture(
        _capture_dict(
            ts="2026-04-22T16:00:10+08:00",
            app="Cursor",
            title="unexpected-first.py",
            value="",
            text="first old member",
        )
    )
    second = scheduler_mod._write_capture(
        _capture_dict(
            ts="2026-04-22T16:00:30+08:00",
            app="Cursor",
            title="unexpected-second.py",
            value="",
            text="second old member",
        )
    )
    _receipt_captures(first, second)
    old = time.time() - 10 * 24 * 3600
    for path in (first, second):
        os.utime(path, (old, old))
    real_unlink = Path.unlink

    def fail_second(target: Path, *args, **kwargs) -> None:
        if target == second:
            raise PermissionError("leave one exact residual")
        real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second)
    scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(Path, "unlink", real_unlink)
    late = scheduler_mod._write_capture(
        _capture_dict(
            ts="2026-04-22T16:00:50+08:00",
            app="Cursor",
            title="unexpected-late.py",
            value="",
            text="late path outside frozen manifest",
        )
    )

    retry = scheduler_mod.cleanup_buffer(retention_hours=1)

    assert retry == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert second.exists() and late.exists()
    with fts.cursor() as conn:
        assert len(timeline_store.window_receipts_in_raw_states(conn, "retiring")) == 1


def test_reused_retiring_path_is_released_as_new_evidence(
    ac_root: Path,
    monkeypatch,
) -> None:
    path = scheduler_mod._write_capture(
        _capture_dict(
            ts="2026-04-22T17:00:10+08:00",
            app="Cursor",
            title="reused.py",
            value="",
            text="old authorized bytes",
        )
    )
    _receipt_captures(path)
    old = time.time() - 10 * 24 * 3600
    os.utime(path, (old, old))
    real_unlink = Path.unlink
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda target, *args, **kwargs: (
            (_ for _ in ()).throw(PermissionError("leave reused name"))
            if target == path
            else real_unlink(target, *args, **kwargs)
        ),
    )
    scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(Path, "unlink", real_unlink)
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["visible_text"] = "new evidence under the reused filename"
    scheduler_mod._atomic_write_json(path, changed, preserve_times=True)

    retry = scheduler_mod.cleanup_buffer(retention_hours=1)

    assert retry == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert path.exists()
    with fts.cursor() as conn:
        assert len(timeline_store.window_receipts_in_raw_states(conn, "retired")) == 1
        assert timeline_store.capture_receipt_paths(conn) == set()
        assert not candidate_store.is_tombstoned(
            conn,
            kind="capture_file",
            artifact_id=path.name,
        )


def test_retention_rechecks_binding_after_database_authorization(
    ac_root: Path,
    monkeypatch,
) -> None:
    path = scheduler_mod._write_capture(
        _capture_dict(
            ts="2026-04-22T17:15:10+08:00",
            app="Cursor",
            title="post-authorization-replacement.py",
            value="",
            text="old authorized bytes",
        )
    )
    _receipt_captures(path)
    old = time.time() - 10 * 24 * 3600
    os.utime(path, (old, old))
    real_authorize = scheduler_mod._delete_captures_from_fts

    def authorize_then_replace(stems: list[str]) -> bool:
        authorized = real_authorize(stems)
        assert authorized
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["visible_text"] = "NEW_CONTENT_AFTER_DB_AUTH"
        scheduler_mod._atomic_write_json(path, changed, preserve_times=True)
        return True

    monkeypatch.setattr(
        scheduler_mod,
        "_delete_captures_from_fts",
        authorize_then_replace,
    )

    stats = scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
    )

    assert stats == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert "NEW_CONTENT_AFTER_DB_AUTH" in path.read_text(encoding="utf-8")
    with fts.cursor() as conn:
        assert len(timeline_store.window_receipts_in_raw_states(conn, "retired")) == 1
        assert timeline_store.capture_receipt_paths(conn) == set()
        assert not candidate_store.is_tombstoned(
            conn,
            kind="capture_file",
            artifact_id=path.name,
        )


@pytest.mark.parametrize("replacement", ["malformed", "timestamp-mismatch", "symlink"])
def test_unclassifiable_reused_retiring_path_stays_quarantined(
    ac_root: Path,
    monkeypatch,
    replacement: str,
) -> None:
    path = scheduler_mod._write_capture(
        _capture_dict(
            ts="2026-04-22T17:30:10+08:00",
            app="Cursor",
            title="unclassifiable.py",
            value="",
            text="old authorized bytes",
        )
    )
    _receipt_captures(path)
    old = time.time() - 10 * 24 * 3600
    os.utime(path, (old, old))
    real_unlink = Path.unlink
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda target, *args, **kwargs: (
            (_ for _ in ()).throw(PermissionError("leave unclassifiable name"))
            if target == path
            else real_unlink(target, *args, **kwargs)
        ),
    )
    scheduler_mod.cleanup_buffer(
        retention_hours=1,
        processed_before_ts="2099-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(Path, "unlink", real_unlink)

    if replacement == "malformed":
        path.write_text("{not-json", encoding="utf-8")
    elif replacement == "timestamp-mismatch":
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["timestamp"] = "2026-04-22T17:30:20+08:00"
        scheduler_mod._atomic_write_json(path, changed, preserve_times=True)
    else:
        target = ac_root / "untrusted-target.json"
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.unlink()
        path.symlink_to(target)

    retry = scheduler_mod.cleanup_buffer(retention_hours=1)

    assert retry == {"deleted": 0, "stripped": 0, "evicted": 0}
    assert path.exists()
    with fts.cursor() as conn:
        assert len(timeline_store.window_receipts_in_raw_states(conn, "retiring")) == 1
        assert timeline_store.capture_receipt_paths(conn) == {path.name}
        assert candidate_store.is_tombstoned(
            conn,
            kind="capture_file",
            artifact_id=path.name,
        )
    cli.rebuild_captures_index()
    with fts.cursor() as conn:
        assert fts.recent_captures(conn, limit=10) == []
    cfg = Config()
    cfg.capture.deny_unknown_windows = False
    assert mcp_captures.read_recent_capture(cfg=cfg) is None


def test_size_eviction_removes_a_window_as_one_group(ac_root: Path) -> None:
    paths_in_window = [
        scheduler_mod._write_capture(
            _capture_dict(
                ts=f"2026-04-22T18:00:{second:02d}+08:00",
                app="Cursor",
                title=f"size-{second}.py",
                value="",
                text="x" * 600_000,
            )
        )
        for second in (10, 40)
    ]
    _receipt_captures(*paths_in_window)

    stats = scheduler_mod.cleanup_buffer(
        retention_hours=24 * 365,
        processed_before_ts="2099-01-01T00:00:00+00:00",
        max_mb=1,
    )

    assert stats == {"deleted": 0, "stripped": 0, "evicted": 2}
    assert not any(path.exists() for path in paths_in_window)
