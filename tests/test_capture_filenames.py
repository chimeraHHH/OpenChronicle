from __future__ import annotations

import json
from datetime import timedelta

from openchronicle.capture import filenames
from openchronicle.config import Config
from openchronicle.mcp import captures as mcp_captures


def test_v3_capture_stem_round_trips_fraction_and_positive_offset() -> None:
    stem = filenames.capture_stem("2026-08-07T12:34:56.789+08:00", "obs_0123456789abcdef")

    assert stem == "2026-08-07T12-34-56.789p08-00_obs_0123456789abcdef"
    parsed = filenames.parse_capture_stem(stem)
    assert parsed is not None
    assert parsed.isoformat(timespec="milliseconds") == "2026-08-07T12:34:56.789+08:00"


def test_capture_stem_round_trips_negative_offset() -> None:
    stem = filenames.capture_stem("2026-08-07T12:34:56.125-05:30", "obs_fedcba9876543210")

    parsed = filenames.parse_capture_stem(stem)
    assert parsed is not None
    assert parsed.utcoffset() == -timedelta(hours=5, minutes=30)
    assert parsed.microsecond == 125000


def test_legacy_capture_stem_still_parses() -> None:
    parsed = filenames.parse_capture_stem("2026-04-21T17-07-32p08-00")

    assert parsed is not None
    assert parsed.isoformat() == "2026-04-21T17:07:32+08:00"


def test_actual_legacy_negative_offset_stem_still_parses() -> None:
    parsed = filenames.parse_capture_stem("2026-04-21T17-07-32-05-30")

    assert parsed is not None
    assert parsed.utcoffset() == -timedelta(hours=5, minutes=30)


def test_mcp_reader_uses_shared_v3_parser() -> None:
    stem = "2026-08-07T12-34-56.789p08-00_obs_0123456789abcdef"

    assert mcp_captures._parse_stem(stem) == filenames.parse_capture_stem(stem)


def test_mcp_reader_returns_v3_capture(ac_root) -> None:
    capture_dir = ac_root / "capture-buffer"
    stem = "2026-08-07T12-34-56.789p08-00_obs_0123456789abcdef"
    payload = {
        "timestamp": "2026-08-07T12:34:56.789+08:00",
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "example.editor",
            "title": "Roadmap",
        },
        "visible_text": "Stage 0",
    }
    (capture_dir / f"{stem}.json").write_text(json.dumps(payload))

    result = mcp_captures.read_recent_capture(cfg=Config())

    assert result is not None
    assert result["file"] == f"{stem}.json"
    assert result["visible_text"] == "Stage 0"


def test_mcp_reader_uses_absolute_time_across_dst_fallback(ac_root) -> None:
    capture_dir = ac_root / "capture-buffer"
    older = "2026-11-01T01-59-59m04-00"
    newer = "2026-11-01T01-00-00m05-00"
    for stem, timestamp in (
        (older, "2026-11-01T01:59:59-04:00"),
        (newer, "2026-11-01T01:00:00-05:00"),
    ):
        payload = {
            "timestamp": timestamp,
            "window_meta": {
                "app_name": stem,
                "bundle_id": "example.editor",
                "title": "",
            },
        }
        (capture_dir / f"{stem}.json").write_text(json.dumps(payload))

    result = mcp_captures.read_recent_capture(cfg=Config())

    assert result is not None
    assert result["file"] == f"{newer}.json"


def test_invalid_capture_stem_is_rejected() -> None:
    assert filenames.parse_capture_stem("not-a-capture") is None
    assert filenames.parse_capture_stem("2026-08-07T12-34-56p99-99") is None
    assert filenames.parse_capture_stem("2026-99-99T99-99-99") is None


def test_atomic_capture_temp_name_is_strictly_recognized() -> None:
    stem = "2026-04-21T17-07-32.123p08-00_obs_0123456789abcdef"
    assert filenames.is_capture_temp_name(f".{stem}.json.deadbeef.tmp") is True
    assert filenames.is_capture_temp_name(f"{stem}.json.deadbeef.tmp") is False
    assert filenames.is_capture_temp_name(".notes.json.deadbeef.tmp") is False
    assert filenames.is_capture_temp_name(f".{stem}.json.bad.nonce.tmp") is False
