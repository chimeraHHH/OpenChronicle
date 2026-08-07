from __future__ import annotations

import io
import logging
import threading
from types import SimpleNamespace

from openchronicle import config as config_mod
from openchronicle.capture import scheduler
from openchronicle.capture import watcher as watcher_mod
from openchronicle.capture.ax_models import AXCaptureResult
from openchronicle.privacy import policy


def test_exclusions_take_precedence_over_allowlist() -> None:
    cfg = config_mod.CaptureConfig(
        allowed_bundle_ids=["com.example.editor"],
        excluded_app_names=["Sensitive Editor"],
    )

    decision = policy.evaluate_window(
        cfg,
        app_name="sensitive editor",
        bundle_id="COM.EXAMPLE.EDITOR",
        window_title="Draft",
    )

    assert decision.allowed is False
    assert decision.reason == "excluded_app_name"


def test_nonempty_allowlist_denies_unknown_or_missing_bundle() -> None:
    cfg = config_mod.CaptureConfig(allowed_bundle_ids=["com.example.allowed"])

    denied = policy.evaluate_window(cfg, app_name="Unknown", bundle_id="", window_title="Untitled")
    allowed = policy.evaluate_window(
        cfg,
        app_name="Allowed",
        bundle_id="COM.EXAMPLE.ALLOWED",
        window_title="Untitled",
    )

    assert denied == policy.CaptureDecision(False, "unknown_bundle_id", "")
    assert allowed.allowed is True


def test_unknown_window_is_denied_by_default() -> None:
    decision = policy.evaluate_window(
        config_mod.CaptureConfig(), app_name="", bundle_id="", window_title=""
    )

    assert decision.reason == "unknown_bundle_id"


def test_malformed_privacy_list_fails_closed() -> None:
    cfg = config_mod.CaptureConfig()
    cfg.excluded_bundle_ids = "com.example.secret"  # type: ignore[assignment]

    decision = policy.evaluate_window(
        cfg,
        app_name="Secret",
        bundle_id="com.example.secret",
        window_title="Vault",
    )

    assert decision.allowed is False
    assert decision.reason == "invalid_privacy_policy"
    assert "excluded_bundle_ids" in decision.matched_rule


def test_malformed_security_boolean_fails_closed() -> None:
    for field, value in (
        ("include_screenshot", "false"),
        ("deny_unknown_windows", 0),
    ):
        cfg = config_mod.CaptureConfig()
        setattr(cfg, field, value)

        decision = policy.evaluate_window(
            cfg,
            app_name="Editor",
            bundle_id="com.example.editor",
            window_title="Draft",
        )

        assert decision.allowed is False
        assert decision.reason == "invalid_privacy_policy"
        assert field in decision.matched_rule


def test_title_exclusion_is_case_insensitive_substring() -> None:
    cfg = config_mod.CaptureConfig(excluded_window_title_patterns=["Private Browsing"])

    decision = policy.evaluate_window(
        cfg,
        app_name="Safari",
        bundle_id="com.apple.Safari",
        window_title="Start Page — PRIVATE BROWSING",
    )

    assert decision.reason == "excluded_window_title"


def test_denied_window_stops_before_ax_and_screenshot(
    ac_root,
    monkeypatch,
    caplog,
) -> None:
    calls = {"ax": 0, "screenshot": 0, "parser": 0}

    class Provider:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool):
            calls["ax"] += 1
            raise AssertionError("AX must not run for a denied window")

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: SimpleNamespace(
            app_name="Passwords SECRET_APP_MARKER",
            bundle_id="com.example.passwords",
            title="Vault SECRET_TITLE_MARKER",
        ),
    )
    monkeypatch.setattr(
        scheduler.screenshot,
        "grab",
        lambda **_kwargs: calls.__setitem__("screenshot", calls["screenshot"] + 1),
    )
    monkeypatch.setattr(
        scheduler.s1_parser,
        "enrich",
        lambda _out: calls.__setitem__("parser", calls["parser"] + 1),
    )
    cfg = config_mod.CaptureConfig(
        excluded_bundle_ids=["COM.EXAMPLE.PASSWORDS"],
        include_screenshot=True,
    )

    result = scheduler._build_capture(cfg, Provider(), trigger=None)

    assert result is None
    assert calls == {"ax": 0, "screenshot": 0, "parser": 0}
    assert "SECRET_APP_MARKER" not in caplog.text
    assert "SECRET_TITLE_MARKER" not in caplog.text


def test_denied_source_event_cannot_leak_into_later_allowed_window(
    ac_root, monkeypatch, caplog
) -> None:
    class Provider:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool):
            raise AssertionError("denied source must stop before AX")

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: SimpleNamespace(
            app_name="Allowed",
            bundle_id="com.example.allowed",
            title="Allowed window",
        ),
    )
    cfg = config_mod.CaptureConfig(excluded_bundle_ids=["com.example.denied"])
    trigger = {
        "event_type": "UserTextInput",
        "app_name": "Denied",
        "bundle_id": "com.example.denied",
        "window_title": "Secret window",
        "details": {"value": "SECRET_TRIGGER_VALUE"},
    }

    assert scheduler._build_capture(cfg, Provider(), trigger) is None
    assert "SECRET_TRIGGER_VALUE" not in caplog.text


def test_queued_event_must_match_current_window_before_ax(ac_root, monkeypatch) -> None:
    class Provider:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool):
            raise AssertionError("mismatched event must stop before AX")

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: SimpleNamespace(
            app_name="Other",
            bundle_id="com.example.other",
            title="Other window",
        ),
    )
    trigger = {
        "event_type": "UserMouseClick",
        "app_name": "Source",
        "bundle_id": "com.example.source",
        "window_title": "Source window",
    }

    assert scheduler._build_capture(config_mod.CaptureConfig(), Provider(), trigger) is None


def test_queued_event_with_unknown_title_fails_closed() -> None:
    trigger = {
        "event_type": "UserMouseClick",
        "bundle_id": "com.example.editor",
        "window_title": "",
        "details": {"value": "must not cross windows"},
    }
    current = {
        "bundle_id": "com.example.editor",
        "title": "Different window",
    }

    assert scheduler._trigger_matches_window(trigger, current) is False


def test_ax_identity_change_is_dropped_before_screenshot(ac_root, monkeypatch) -> None:
    class Provider:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool):
            raw = {
                "apps": [
                    {
                        "name": "Denied",
                        "bundle_id": "com.example.denied",
                        "windows": [{"title": "Secret", "focused": True}],
                    }
                ]
            }
            return AXCaptureResult(raw, "", raw["apps"], {})

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: SimpleNamespace(
            app_name="Allowed",
            bundle_id="com.example.allowed",
            title="Allowed",
        ),
    )
    monkeypatch.setattr(
        scheduler.screenshot,
        "grab",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity mismatch must stop before screenshot")
        ),
    )
    cfg = config_mod.CaptureConfig(
        excluded_bundle_ids=["com.example.denied"],
        include_screenshot=True,
    )

    assert scheduler._build_capture(cfg, Provider(), trigger=None) is None


def test_multiwindow_ax_payload_fails_closed_before_persistence(ac_root, monkeypatch) -> None:
    raw = {
        "apps": [
            {
                "name": "Editor",
                "bundle_id": "com.example.editor",
                "windows": [
                    {"title": "Allowed", "focused": True, "elements": []},
                    {
                        "title": "Private SECRET_TITLE",
                        "focused": False,
                        "elements": [{"role": "AXStaticText", "value": "SECRET_PAYLOAD"}],
                    },
                ],
            }
        ]
    }

    class Provider:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool):
            return AXCaptureResult(raw, "", raw["apps"], {})

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: SimpleNamespace(
            app_name="Editor",
            bundle_id="com.example.editor",
            title="Allowed",
        ),
    )
    cfg = config_mod.CaptureConfig(
        excluded_window_title_patterns=["SECRET_TITLE"],
    )

    assert scheduler._build_capture(cfg, Provider(), trigger=None) is None


def test_matching_source_active_and_ax_identity_produces_capture(ac_root, monkeypatch) -> None:
    raw = {
        "apps": [
            {
                "name": "Editor",
                "bundle_id": "com.example.editor",
                "windows": [
                    {
                        "title": "Roadmap",
                        "focused": True,
                        "elements": [{"role": "AXStaticText", "value": "Stage 0"}],
                    }
                ],
            }
        ]
    }

    class Provider:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool):
            assert focused_window_only is True
            return AXCaptureResult(raw, "", raw["apps"], {})

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: SimpleNamespace(
            app_name="Editor",
            bundle_id="com.example.editor",
            title="Roadmap",
        ),
    )
    monkeypatch.setattr(
        scheduler.screenshot,
        "grab",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("screenshots must remain off by default")
        ),
    )
    trigger = {
        "event_type": "UserTextInput",
        "app_name": "Editor",
        "bundle_id": "com.example.editor",
        "window_title": "Roadmap",
        "details": {"value": "Stage 0"},
    }

    result = scheduler._build_capture(config_mod.CaptureConfig(), Provider(), trigger)

    assert result is not None
    assert result["window_meta"]["title"] == "Roadmap"
    assert result["trigger"]["details"]["value"] == "Stage 0"
    assert "Stage 0" in result["visible_text"]


def test_malformed_watcher_frame_is_not_copied_to_logs(caplog) -> None:
    secret = "SECRET_TYPED_TEXT"
    process = SimpleNamespace(
        stdout=io.StringIO(f'{{"event_type": "UserTextInput", "value": "{secret}"\n'),
        wait=lambda: 0,
    )
    watcher = watcher_mod.AXWatcherProcess.__new__(watcher_mod.AXWatcherProcess)
    watcher._process = process
    watcher._callback = None
    watcher._stop_event = threading.Event()

    with caplog.at_level(logging.DEBUG):
        watcher._read_events()

    assert secret not in caplog.text
    assert "Invalid JSON frame" in caplog.text
