"""capture/scheduler.py: write-through to captures_fts + delete-through on cleanup."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from openchronicle.capture import scheduler as scheduler_mod
from openchronicle.config import CaptureConfig
from openchronicle.store import fts
from openchronicle.timeline import aggregator as timeline_aggregator


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
    old_end = scheduler_mod.filenames.parse_timestamp(
        "2026-04-22T14:01:00+08:00"
    )
    assert old_end is not None
    assert timeline_aggregator.capture_paths_by_window(old_end, 1) == {}


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


def test_cleanup_uses_exact_fractional_processed_boundary(ac_root: Path) -> None:
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

    stats = scheduler_mod.cleanup_buffer(
        retention_hours=24,
        processed_before_ts="2026-04-22T14:00:00.050+08:00",
        screenshot_retention_hours=None,
        max_mb=0,
    )

    assert stats["deleted"] == 1
    assert not before_path.exists()
    assert after_path.exists()
    with fts.cursor() as conn:
        assert {row.id for row in fts.recent_captures(conn, limit=10)} == {after_path.stem}


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
