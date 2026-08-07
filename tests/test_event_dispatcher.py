from __future__ import annotations

from typing import Any

from openchronicle.capture.event_dispatcher import EventDispatcher


def test_dispatcher_preserves_complete_producer_isolated_watcher_event() -> None:
    captured: list[dict[str, Any]] = []
    dispatcher = EventDispatcher(
        captured.append,
        min_capture_gap_seconds=0,
        dedup_interval_seconds=0,
        same_window_dedup_seconds=0,
    )
    raw = {
        "event_type": "UserTextInput",
        "bundle_id": "com.example.Editor",
        "window_title": "Draft",
        "app_name": "Editor",
        "pid": 1234,
        "timestamp": "2026-08-07T12:00:00.123+08:00",
        "details": {"role": "AXTextArea", "value": "original"},
    }

    dispatcher.on_event(raw)
    raw["details"]["value"] = "mutated-after-dispatch"

    assert len(captured) == 1
    assert captured[0]["app_name"] == "Editor"
    assert captured[0]["pid"] == 1234
    assert captured[0]["timestamp"] == "2026-08-07T12:00:00.123+08:00"
    assert captured[0]["details"] == {"role": "AXTextArea", "value": "original"}


def test_dispatcher_still_deduplicates_on_normalized_identity() -> None:
    captured: list[dict[str, Any]] = []
    dispatcher = EventDispatcher(
        captured.append,
        min_capture_gap_seconds=0,
        dedup_interval_seconds=60,
        same_window_dedup_seconds=0,
    )
    event = {
        "event_type": "UserMouseClick",
        "bundle_id": None,
        "window_title": None,
        "details": {"role": "AXButton"},
    }

    dispatcher.on_event(event)
    dispatcher.on_event(event)

    assert len(captured) == 1
    assert captured[0]["bundle_id"] == ""
    assert captured[0]["window_title"] == ""


def test_dispatcher_filters_before_copy_or_enqueue() -> None:
    captured: list[dict[str, Any]] = []
    filtered: list[str] = []
    dispatcher = EventDispatcher(
        captured.append,
        event_filter=lambda event: filtered.append(event["bundle_id"]) or False,
        min_capture_gap_seconds=0,
        dedup_interval_seconds=0,
    )

    dispatcher.on_event(
        {
            "event_type": "UserTextInput",
            "bundle_id": "com.example.denied",
            "window_title": "Secret",
            "details": {"value": "never enqueue"},
        }
    )

    assert filtered == ["com.example.denied"]
    assert captured == []
