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


def _window(
    *,
    app_name: str = "Editor",
    bundle_id: str = "com.example.editor",
    title: str = "Roadmap",
    pid: int = 101,
    window_id: int = 202,
) -> scheduler.window_meta.WindowMeta:
    return scheduler.window_meta.WindowMeta(
        app_name=app_name,
        bundle_id=bundle_id,
        title=title,
        pid=pid,
        window_id=window_id,
        bounds=scheduler.window_meta.WindowBounds(x=10, y=20, width=900, height=700),
    )


def _focused_ax(
    meta: scheduler.window_meta.WindowMeta,
    *,
    elements: list[dict] | None = None,
) -> dict:
    assert meta.bounds is not None
    return {
        "window_meta": meta.to_capture_request(),
        "apps": [
            {
                "pid": meta.pid,
                "name": meta.app_name,
                "bundle_id": meta.bundle_id,
                "is_frontmost": True,
                "windows": [
                    {
                        "title": meta.title,
                        "focused": True,
                        "elements": elements or [],
                    }
                ],
            }
        ],
    }


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


def test_url_policy_default_allows_unknown_but_rejects_present_invalid_url() -> None:
    cfg = config_mod.CaptureConfig()

    assert policy.evaluate_url(cfg, url=None).allowed is True
    assert policy.evaluate_url(cfg, url="  ").allowed is True
    for invalid in (
        "javascript:alert(1)",
        "https://",
        "https://user:secret@example.com/private",
        "https://example.com/has whitespace",
        42,
    ):
        decision = policy.evaluate_url(cfg, url=invalid)
        assert decision == policy.CaptureDecision(False, "invalid_url")


def test_url_allowlist_is_restrictive_and_unknown_fails_closed() -> None:
    cfg = config_mod.CaptureConfig(allowed_url_patterns=["example.com"])

    assert policy.evaluate_url(cfg, url=None).reason == "unknown_url"
    assert policy.evaluate_url(cfg, url="not a URL").reason == "unknown_url"
    assert policy.evaluate_url(cfg, url="https://example.com/project").allowed is True
    assert policy.evaluate_url(cfg, url="https://docs.example.com/project").allowed is True
    # Host rules use DNS-label boundaries, not an unsafe raw substring.
    decision = policy.evaluate_url(cfg, url="https://notexample.com/project")
    assert decision.reason == "url_not_allowed"


def test_exclude_only_url_policy_also_fails_closed_on_unknown_or_invalid() -> None:
    cfg = config_mod.CaptureConfig(excluded_url_patterns=["private.example"])

    assert policy.has_url_policy(cfg) is True
    assert policy.evaluate_url(cfg, url=None).reason == "unknown_url"
    assert policy.evaluate_url(cfg, url="not a URL").reason == "unknown_url"


def test_url_policy_presence_helper_cannot_hide_malformed_falsey_config() -> None:
    cfg = config_mod.CaptureConfig()
    assert policy.has_url_policy(cfg) is False

    cfg.allowed_url_patterns = None  # type: ignore[assignment]
    assert policy.has_url_policy(cfg) is True
    validation = policy.validate_url_policy(cfg)
    assert validation.reason == "invalid_privacy_policy"
    assert "allowed_url_patterns" in validation.matched_rule
    assert policy.evaluate_url(cfg, url=None).reason == "invalid_privacy_policy"


def test_url_policy_validation_does_not_require_an_observed_url() -> None:
    cfg = config_mod.CaptureConfig(
        allowed_url_patterns=["example.com"],
        excluded_url_patterns=["/private/"],
    )

    assert policy.validate_url_policy(cfg) == policy.CaptureDecision(True)


def test_url_exclusion_precedes_allowlist_and_never_echoes_observed_url(caplog) -> None:
    cfg = config_mod.CaptureConfig(
        allowed_url_patterns=["example.com"],
        excluded_url_patterns=["/private/"],
    )
    secret_url = "https://example.com/private/SECRET-URL-VALUE"

    decision = policy.evaluate_url(cfg, url=secret_url)

    assert decision == policy.CaptureDecision(
        False,
        "excluded_url",
        "excluded_url_patterns[0]",
    )
    assert secret_url not in decision.reason
    assert secret_url not in decision.matched_rule
    assert "SECRET-URL-VALUE" not in repr(decision)
    assert "SECRET-URL-VALUE" not in caplog.text


def test_url_rules_normalize_scheme_host_default_port_and_case() -> None:
    cfg = config_mod.CaptureConfig(
        allowed_url_patterns=["EXAMPLE.COM"],
        excluded_url_patterns=["HTTPS://EXAMPLE.COM:443/PRIVATE"],
    )

    allowed = policy.evaluate_url(cfg, url="HTTPS://Sub.Example.Com:443/Public")
    excluded = policy.evaluate_url(cfg, url="https://EXAMPLE.com/PRIVATE")

    assert allowed.allowed is True
    assert excluded.reason == "excluded_url"


def test_full_url_rule_preserves_path_and_query_case() -> None:
    cfg = config_mod.CaptureConfig(allowed_url_patterns=["https://EXAMPLE.com/Public?View=Full"])

    assert policy.evaluate_url(cfg, url="HTTPS://example.COM/Public?View=Full").allowed
    assert (
        policy.evaluate_url(cfg, url="https://example.com/public?View=Full").reason
        == "url_not_allowed"
    )
    assert (
        policy.evaluate_url(cfg, url="https://example.com/Public?view=full").reason
        == "url_not_allowed"
    )


def test_scheme_less_candidate_must_pass_http_and_https_interpretations() -> None:
    excluded_http = config_mod.CaptureConfig(excluded_url_patterns=["http://private.example/path"])
    https_only_allow = config_mod.CaptureConfig(
        allowed_url_patterns=["https://private.example/path"]
    )
    host_allow = config_mod.CaptureConfig(allowed_url_patterns=["private.example"])

    assert (
        policy.evaluate_url_candidate(
            excluded_http,
            url="https://private.example/path",
            scheme_known=False,
        ).reason
        == "excluded_url"
    )
    assert (
        policy.evaluate_url_candidate(
            https_only_allow,
            url="https://private.example/path",
            scheme_known=False,
        ).reason
        == "url_not_allowed"
    )
    assert policy.evaluate_url_candidate(
        host_allow,
        url="https://private.example/path",
        scheme_known=False,
    ).allowed
    assert policy.evaluate_url_candidate(
        https_only_allow,
        url="https://private.example/path",
        scheme_known=True,
    ).allowed


def test_full_url_allow_rule_cannot_be_smuggled_in_unrelated_query() -> None:
    cfg = config_mod.CaptureConfig(allowed_url_patterns=["https://allowed.example/project"])

    assert policy.evaluate_url(cfg, url="https://allowed.example/project/child?view=1").allowed
    denied = policy.evaluate_url(
        cfg,
        url="https://evil.example/?next=https://allowed.example/project",
    )
    assert denied.reason == "url_not_allowed"


def test_allow_rule_requires_host_or_full_url_instead_of_unsafe_substring() -> None:
    cfg = config_mod.CaptureConfig(allowed_url_patterns=["allowed.example/project"])

    validation = policy.validate_url_policy(cfg)
    decision = policy.evaluate_url(
        cfg,
        url="https://evil.example/?next=allowed.example/project",
    )

    assert validation.reason == "invalid_privacy_policy"
    assert decision.reason == "invalid_privacy_policy"


def test_url_rules_cannot_be_bypassed_with_encoded_unreserved_characters() -> None:
    cfg = config_mod.CaptureConfig(excluded_url_patterns=["/private/"])

    decision = policy.evaluate_url(cfg, url="https://example.com/%70rivate/report")

    assert decision.reason == "excluded_url"


def test_invalid_percent_escape_fails_closed_without_echoing_url() -> None:
    cfg = config_mod.CaptureConfig(excluded_url_patterns=["example.com/private"])
    marker = "SECRET-BAD-PERCENT"

    decision = policy.evaluate_url(cfg, url=f"https://example.com/%ZZ/{marker}")

    assert decision.reason == "unknown_url"
    assert marker not in repr(decision)


def test_url_patterns_are_bounded_literals_not_executable_regexes() -> None:
    cfg = config_mod.CaptureConfig(excluded_url_patterns=[r"(a+)+$"])
    long_url = "https://example.com/" + "a" * 4090

    # Regex metacharacters have no special meaning and oversized observed URLs
    # are rejected before any matching work.
    assert policy.evaluate_url(cfg, url="https://example.com/aaaa").allowed is True
    assert policy.evaluate_url(cfg, url=long_url).reason == "unknown_url"


def _stored_observation(*, app_name: str = "Safari") -> dict:
    return {
        "timestamp": "2026-04-22T14:05:00+08:00",
        "schema_version": 5,
        "observation_id": "obs_0123456789abcdef0123456789abcdef",
        "trigger": {
            "event_type": "heartbeat",
            "app_name": app_name,
            "bundle_id": "com.apple.Safari",
            "window_title": "",
            "pid": 101,
            "window_id": 202,
        },
        "window_meta": {
            "app_name": app_name,
            "bundle_id": "com.apple.Safari",
            "title": "",
            "pid": 101,
            "window_id": 202,
            "bounds": {"x": 10, "y": 20, "width": 900, "height": 700},
        },
        "privacy": {
            "decision": "allowed",
            "policy_version": 3,
            "content_mode": "url_metadata_only",
        },
        "url": "https://allowed.example/project",
        "visible_text": "",
    }


def test_stored_observation_is_rechecked_against_current_window_policy() -> None:
    observation = _stored_observation(app_name="SecretApp")
    cfg = config_mod.CaptureConfig(excluded_app_names=["SecretApp"])

    decision = policy.evaluate_stored_observation(cfg, observation=observation)

    assert decision.reason == "excluded_app_name"


def test_active_url_policy_accepts_only_exact_content_free_v5_projection() -> None:
    cfg = config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"])
    observation = _stored_observation()

    assert policy.evaluate_stored_observation(cfg, observation=observation).allowed

    for mutate in (
        lambda value: value.update(schema_version=4),
        lambda value: value.update(ax_tree={"private": "content"}),
        lambda value: value.update(visible_text="private body"),
        lambda value: value["window_meta"].update(title="private title"),
        lambda value: value["trigger"].update(window_id=999),
        lambda value: value["trigger"].update(event_type="PRIVATE_TRIGGER_TEXT"),
        lambda value: value["privacy"].update(policy_version=2),
        lambda value: value.update(observation_id="obs_PRIVATE_IDENTIFIER_TEXT"),
        lambda value: value.update(timestamp="PRIVATE_TIMESTAMP_TEXT"),
    ):
        candidate = _stored_observation()
        mutate(candidate)
        decision = policy.evaluate_stored_observation(cfg, observation=candidate)
        assert decision.reason == "unverifiable_url_metadata"


def test_stored_observation_rechecks_retained_url_against_new_rules() -> None:
    observation = _stored_observation()
    cfg = config_mod.CaptureConfig(excluded_url_patterns=["/project"])

    decision = policy.evaluate_stored_observation(cfg, observation=observation)

    assert decision.reason == "excluded_url"


def test_stored_observation_title_policy_requires_one_matching_ax_window() -> None:
    meta = _window(title="Public roadmap")
    tree = _focused_ax(meta, elements=[{"role": "AXStaticText", "value": "PUBLIC"}])
    observation = {
        "timestamp": "2026-08-08T12:00:00+00:00",
        "schema_version": 4,
        "observation_id": "obs_0123456789abcdef0123456789abcdef",
        "window_meta": {
            "app_name": meta.app_name,
            "bundle_id": meta.bundle_id,
            "title": meta.title,
            "pid": meta.pid,
            "window_id": meta.window_id,
            "bounds": meta.bounds.to_dict(),
        },
        "privacy": {"decision": "allowed", "policy_version": 2},
        "ax_tree": tree,
        "visible_text": "PUBLIC",
    }
    cfg = config_mod.CaptureConfig(excluded_window_title_patterns=["Secret"])

    assert policy.evaluate_stored_observation(cfg, observation=observation).allowed

    sibling = {
        "title": "Secret payroll",
        "focused": False,
        "elements": [{"role": "AXStaticText", "value": "SECRET_SIBLING_VALUE"}],
    }
    tree["apps"][0]["windows"].append(sibling)
    observation["visible_text"] = "PUBLIC\nSECRET_SIBLING_VALUE"

    decision = policy.evaluate_stored_observation(cfg, observation=observation)

    assert decision.reason == "unverifiable_window_content"


def test_url_metadata_projection_requires_a_supported_browser_bundle() -> None:
    observation = _stored_observation()
    observation["window_meta"]["bundle_id"] = "com.example.lookalike"
    observation["trigger"]["bundle_id"] = "com.example.lookalike"
    cfg = config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"])

    decision = policy.evaluate_stored_observation(cfg, observation=observation)

    assert decision.reason == "unverifiable_url_metadata"


def test_malformed_url_policy_configuration_fails_closed_without_values() -> None:
    for field, value in (
        ("allowed_url_patterns", "example.com"),
        ("excluded_url_patterns", ["example.com", 7]),
        ("allowed_url_patterns", [""]),
        ("excluded_url_patterns", ["x" * 513]),
    ):
        cfg = config_mod.CaptureConfig()
        setattr(cfg, field, value)

        decision = policy.evaluate_url(cfg, url="https://example.com/secret")

        assert decision.allowed is False
        assert decision.reason == "invalid_privacy_policy"
        assert field in decision.matched_rule
        assert "https://example.com/secret" not in repr(decision)


def test_denied_window_stops_before_ax_and_screenshot(
    ac_root,
    monkeypatch,
    caplog,
) -> None:
    calls = {"ax": 0, "screenshot": 0, "parser": 0}

    class Provider:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool, **_kwargs):
            calls["ax"] += 1
            raise AssertionError("AX must not run for a denied window")

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: _window(
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

        def capture_frontmost(self, *, focused_window_only: bool, **_kwargs):
            raise AssertionError("denied source must stop before AX")

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: _window(
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

        def capture_frontmost(self, *, focused_window_only: bool, **_kwargs):
            raise AssertionError("mismatched event must stop before AX")

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: _window(
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
    denied_meta = _window(
        app_name="Denied",
        bundle_id="com.example.denied",
        title="Secret",
        pid=303,
        window_id=404,
    )

    class Provider:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool, **_kwargs):
            raw = _focused_ax(denied_meta)
            return AXCaptureResult(raw, "", raw["apps"], {})

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: _window(
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
    active = _window(title="Allowed")
    raw = {
        "window_meta": active.to_capture_request(),
        "apps": [
            {
                "pid": active.pid,
                "name": "Editor",
                "bundle_id": "com.example.editor",
                "is_frontmost": True,
                "windows": [
                    {"title": "Allowed", "focused": True, "elements": []},
                    {
                        "title": "Private SECRET_TITLE",
                        "focused": False,
                        "elements": [{"role": "AXStaticText", "value": "SECRET_PAYLOAD"}],
                    },
                ],
            }
        ],
    }

    class Provider:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool, **_kwargs):
            return AXCaptureResult(raw, "", raw["apps"], {})

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: active,
    )
    cfg = config_mod.CaptureConfig(
        excluded_window_title_patterns=["SECRET_TITLE"],
    )

    assert scheduler._build_capture(cfg, Provider(), trigger=None) is None


def test_matching_source_active_and_ax_identity_produces_capture(ac_root, monkeypatch) -> None:
    active = _window()
    raw = _focused_ax(
        active,
        elements=[{"role": "AXStaticText", "value": "Stage 0"}],
    )

    class Provider:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool, **_kwargs):
            assert focused_window_only is True
            return AXCaptureResult(raw, "", raw["apps"], {})

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: active,
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
    assert result["schema_version"] == 4
    assert result["privacy"]["policy_version"] == 2
    assert result["window_meta"]["title"] == "Roadmap"
    assert result["window_meta"]["window_id"] == active.window_id
    assert result["trigger"] == {
        "event_type": "UserTextInput",
        "app_name": "Editor",
        "bundle_id": "com.example.editor",
        "window_title": "Roadmap",
        "pid": active.pid,
        "window_id": active.window_id,
    }
    assert "Stage 0" in result["visible_text"]


def test_malformed_watcher_frame_is_not_copied_to_logs(caplog) -> None:
    secret = "SECRET_TYPED_TEXT"
    process = SimpleNamespace(
        stdout=io.BytesIO(f'{{"event_type": "UserTextInput", "value": "{secret}"\n'.encode()),
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


def test_oversized_watcher_frame_is_drained_without_losing_alignment(caplog) -> None:
    secret = b"SECRET_OVERSIZED_WATCHER_VALUE"
    oversized = (
        b'{"event_type":"UserTextInput","value":"'
        + (secret * (watcher_mod._MAX_WATCHER_FRAME_BYTES // len(secret) + 2))
        + b'"}\n'
    )
    valid = b'{"event_type":"AXFocusedWindowChanged","pid":123}\n'
    process = SimpleNamespace(stdout=io.BytesIO(oversized + valid), wait=lambda: 0)
    observed: list[dict] = []
    watcher = watcher_mod.AXWatcherProcess.__new__(watcher_mod.AXWatcherProcess)
    watcher._process = process
    watcher._callback = observed.append
    watcher._stop_event = threading.Event()

    with caplog.at_level(logging.DEBUG):
        watcher._read_events()

    assert observed == [{"event_type": "AXFocusedWindowChanged", "pid": 123}]
    assert secret.decode() not in caplog.text
    assert "Oversized watcher frame discarded" in caplog.text


def test_watcher_accepts_exact_payload_cap_with_lf_or_crlf() -> None:
    prefix = b'{"event_type":"AXValueChanged","padding":"'
    suffix = b'"}'
    padding = b"x" * (watcher_mod._MAX_WATCHER_FRAME_BYTES - len(prefix) - len(suffix))
    payload = prefix + padding + suffix
    assert len(payload) == watcher_mod._MAX_WATCHER_FRAME_BYTES

    for delimiter in (b"\n", b"\r\n"):
        process = SimpleNamespace(stdout=io.BytesIO(payload + delimiter), wait=lambda: 0)
        observed: list[dict] = []
        watcher = watcher_mod.AXWatcherProcess.__new__(watcher_mod.AXWatcherProcess)
        watcher._process = process
        watcher._callback = observed.append
        watcher._stop_event = threading.Event()

        watcher._read_events()

        assert len(observed) == 1
        assert observed[0]["event_type"] == "AXValueChanged"
        assert len(observed[0]["padding"]) == len(padding)
