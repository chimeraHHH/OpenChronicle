from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest

from openchronicle import config as config_mod
from openchronicle.capture import scheduler
from openchronicle.capture.ax_models import AXCaptureResult


def _meta(
    *,
    app_name: str = "Editor",
    bundle_id: str = "com.example.editor",
    title: str = "Roadmap",
    pid: int = 700,
    window_id: int = 800,
) -> scheduler.window_meta.WindowMeta:
    return scheduler.window_meta.WindowMeta(
        app_name=app_name,
        bundle_id=bundle_id,
        title=title,
        pid=pid,
        window_id=window_id,
        bounds=scheduler.window_meta.WindowBounds(x=20, y=30, width=1000, height=720),
    )


def _raw(
    meta: scheduler.window_meta.WindowMeta,
    *,
    elements: list[dict] | None = None,
) -> dict:
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


def _safari_address(value: str, *, identifier: str = "smart-search-field") -> dict:
    return {
        "role": "AXTextField",
        "identifier": identifier,
        "value": value,
    }


class _Provider:
    available = True

    def __init__(self, raw: dict | None, *, metadata: dict | None = None) -> None:
        self.raw = raw
        self.metadata = {"mode": "test"} if metadata is None else metadata
        self.calls = 0
        self.complete_tree_requests: list[bool] = []

    def capture_frontmost(self, *, focused_window_only: bool, require_complete_tree: bool = False):
        assert focused_window_only is True
        self.calls += 1
        self.complete_tree_requests.append(require_complete_tree)
        if self.raw is None:
            return None
        return AXCaptureResult(
            self.raw,
            "",
            self.raw.get("apps", []),
            self.metadata,
            tree_complete_verified=require_complete_tree,
            effective_max_depth=100 if require_complete_tree else None,
        )


def test_malformed_url_policy_denies_before_window_or_ax(ac_root, monkeypatch, caplog) -> None:
    secret = "SECRET-MALFORMED-CONFIG"
    cfg = config_mod.CaptureConfig()
    cfg.allowed_url_patterns = f"https://{secret}.example"  # type: ignore[assignment]

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: (_ for _ in ()).throw(AssertionError("window helper must not run")),
    )
    provider = _Provider(_raw(_meta()))

    with caplog.at_level(logging.INFO):
        assert scheduler._build_capture(cfg, provider, trigger=None) is None

    assert provider.calls == 0
    assert secret not in caplog.text


def test_unavailable_ax_provider_cannot_create_metadata_only_capture(
    ac_root, monkeypatch, caplog
) -> None:
    active = _meta()
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    class Unavailable:
        available = False
        reason = "SECRET-HELPER-FAILURE"

        def capture_frontmost(
            self, *, focused_window_only: bool, require_complete_tree: bool = False
        ):
            raise AssertionError("unavailable provider must not be called")

    with caplog.at_level(logging.INFO):
        assert scheduler.capture_once(config_mod.CaptureConfig(), Unavailable()) is None

    assert not list(ac_root.rglob("*.json"))
    assert "SECRET-HELPER-FAILURE" not in caplog.text


def test_ax_none_cannot_create_metadata_only_capture(ac_root, monkeypatch) -> None:
    active = _meta()
    provider = _Provider(None)
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    assert scheduler.capture_once(config_mod.CaptureConfig(), provider) is None
    assert provider.calls == 1
    assert not list(ac_root.rglob("*.json"))


def test_same_title_sibling_window_id_is_not_treated_as_same_target(ac_root, monkeypatch) -> None:
    active = _meta(title="Untitled", window_id=801)
    sibling = replace(active, window_id=802)
    provider = _Provider(_raw(sibling))
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    assert scheduler._build_capture(config_mod.CaptureConfig(), provider, None) is None


@pytest.mark.parametrize(
    ("mutation",),
    [
        (lambda raw: raw["apps"][0].update(is_frontmost=False),),
        (lambda raw: raw["apps"][0]["windows"][0].update(focused=False),),
        (lambda raw: raw.pop("window_meta"),),
        (lambda raw: raw["window_meta"].update(window_id=0),),
        (lambda raw: raw["apps"][0].update(pid=True),),
    ],
)
def test_ax_payload_requires_single_exact_frontmost_focused_identity(mutation) -> None:
    raw = _raw(_meta())
    mutation(raw)

    assert scheduler._ax_identity(raw) is None


def test_malformed_ax_content_parser_failure_is_generic_and_fail_closed(
    ac_root, monkeypatch, caplog
) -> None:
    marker = "SECRET-MALFORMED-AX-VALUE"
    active = _meta()
    raw = _raw(active)
    raw["apps"][0]["windows"][0]["elements"] = [marker]
    provider = _Provider(raw)
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    with caplog.at_level(logging.WARNING):
        assert scheduler.capture_once(config_mod.CaptureConfig(), provider) is None

    assert marker not in caplog.text
    assert not list(ac_root.rglob("*.json"))


@pytest.mark.parametrize(
    "inject",
    [
        lambda raw, marker: raw.update(diagnostic=f"https://forbidden.invalid/{marker}"),
        lambda raw, marker: raw["apps"][0]["windows"][0]["elements"].append(
            {
                "role": "AXTextField",
                "opaque_address": f"forbidden.invalid/{marker}",
            }
        ),
    ],
)
def test_unknown_ax_schema_fields_cannot_bypass_url_policy(
    ac_root, monkeypatch, caplog, inject
) -> None:
    marker = "UNKNOWN-AX-SCHEMA-CANARY"
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    raw = _raw(active)
    inject(raw, marker)
    provider = _Provider(raw)
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    with caplog.at_level(logging.INFO):
        assert (
            scheduler.capture_once(
                config_mod.CaptureConfig(excluded_url_patterns=["forbidden.invalid"]),
                provider,
            )
            is None
        )

    assert marker not in caplog.text
    assert not list(ac_root.rglob("*.json"))


def test_provider_metadata_is_removed_from_url_policy_projection(ac_root, monkeypatch) -> None:
    marker = "PROVIDER-METADATA-CANARY"
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(
        _raw(
            active,
            elements=[_safari_address("https://allowed.example/safe")],
        ),
        metadata={"debug": f"https://forbidden.invalid/{marker}"},
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    result = scheduler._build_capture(
        config_mod.CaptureConfig(
            allowed_url_patterns=["allowed.example"],
            excluded_url_patterns=["forbidden.invalid"],
        ),
        provider,
        None,
    )

    assert result is not None
    assert "ax_metadata" not in result
    assert marker not in json.dumps(result)


def test_secure_provider_value_is_redacted_again_before_json_and_fts(ac_root, monkeypatch) -> None:
    marker = "SECURE-PLAINTEXT-PROVIDER-CANARY"
    active = _meta()
    provider = _Provider(
        _raw(
            active,
            elements=[
                {
                    "role": "AXTextField",
                    "subrole": "AXSecureTextField",
                    "title": "Password",
                    "value": marker,
                }
            ],
        )
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    path = scheduler.capture_once(config_mod.CaptureConfig(), provider)

    assert path is not None
    persisted = path.read_text(encoding="utf-8")
    assert marker not in persisted
    assert persisted.count("[REDACTED]") >= 2
    with scheduler.fts_store.cursor() as conn:
        row = conn.execute("SELECT focused_value, visible_text FROM captures LIMIT 1").fetchone()
    assert row is not None
    assert marker not in "\n".join(str(value or "") for value in row)
    assert "[REDACTED]" in str(row["focused_value"])


def test_excluded_browser_url_stops_before_pixels_and_all_local_sinks(
    ac_root, monkeypatch, caplog
) -> None:
    marker = "FORBIDDEN-URL-MARKER-92c3"
    active = _meta(
        app_name="Safari",
        bundle_id="com.apple.Safari",
        title="Allowed title",
    )
    raw = _raw(
        active,
        elements=[
            {
                "role": "AXGroup",
                "children": [
                    {
                        **_safari_address(f"https://private.example/{marker}"),
                    }
                ],
            }
        ],
    )
    provider = _Provider(raw)
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)
    monkeypatch.setattr(
        scheduler.screenshot,
        "grab",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("URL denial must precede screenshot capture")
        ),
    )
    cfg = config_mod.CaptureConfig(
        excluded_url_patterns=["private.example"],
        include_screenshot=True,
    )

    with caplog.at_level(logging.DEBUG):
        assert scheduler.capture_once(cfg, provider) is None

    assert marker not in caplog.text
    assert not list(ac_root.rglob("*.json"))
    for path in ac_root.rglob("*"):
        if path.is_file():
            assert marker.encode() not in path.read_bytes()


def test_every_browser_url_candidate_is_gated_before_raw_tree_persistence(
    ac_root, monkeypatch, caplog
) -> None:
    marker = "FORBIDDEN-SECOND-URL-4f7a"
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    raw = _raw(
        active,
        elements=[
            _safari_address("https://allowed.example/start"),
            {
                "role": "AXGroup",
                "children": [
                    {
                        "role": "AXTextField",
                        "value": f"https://forbidden.example/{marker}",
                    }
                ],
            },
        ],
    )
    provider = _Provider(raw)
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)
    cfg = config_mod.CaptureConfig(
        allowed_url_patterns=["allowed.example", "forbidden.example"],
        excluded_url_patterns=["forbidden.example"],
    )

    with caplog.at_level(logging.INFO):
        assert scheduler.capture_once(cfg, provider) is None

    assert marker not in caplog.text
    assert not list(ac_root.rglob("*.json"))


@pytest.mark.parametrize("role", ["AXComboBox", "AXTextArea", "AXStaticText"])
def test_excluded_url_in_other_text_bearing_role_cannot_bypass_gate(
    ac_root, monkeypatch, role
) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(
        _raw(
            active,
            elements=[
                _safari_address("https://public.example/ok"),
                {"role": role, "value": "https://forbidden.example/private"},
            ],
        )
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)
    cfg = config_mod.CaptureConfig(
        allowed_url_patterns=["public.example"],
        excluded_url_patterns=["forbidden.example"],
    )

    assert scheduler._build_capture(cfg, provider, None) is None


def test_bare_excluded_url_in_static_ax_value_is_also_gated(ac_root, monkeypatch) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(
        _raw(
            active,
            elements=[
                _safari_address("https://public.example/ok"),
                {"role": "AXStaticText", "value": "forbidden.example/private"},
            ],
        )
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)
    cfg = config_mod.CaptureConfig(
        allowed_url_patterns=["public.example"],
        excluded_url_patterns=["forbidden.example"],
    )

    assert scheduler._build_capture(cfg, provider, None) is None


def test_incomplete_browser_url_scan_fails_closed_when_policy_is_enabled(
    ac_root, monkeypatch
) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    elements = [
        {"role": "AXStaticText", "value": "padding"}
        for _ in range(scheduler.s1_parser._MAX_URL_SCAN_NODES)
    ]
    elements.append(_safari_address("https://forbidden.example/late"))
    provider = _Provider(_raw(active, elements=elements))
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    cfg = config_mod.CaptureConfig(excluded_url_patterns=["forbidden.example"])
    assert scheduler._build_capture(cfg, provider, None) is None


def test_multiple_allowed_browser_url_fields_are_still_ambiguous(ac_root, monkeypatch) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(
        _raw(
            active,
            elements=[
                _safari_address("https://allowed.example/one", identifier="address-field"),
                _safari_address("https://allowed.example/two"),
            ],
        )
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    cfg = config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"])
    assert scheduler._build_capture(cfg, provider, None) is None


def test_browser_url_must_be_stable_across_two_complete_ax_snapshots(ac_root, monkeypatch) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    snapshots = iter(
        [
            _raw(
                active,
                elements=[_safari_address("https://allowed.example/one")],
            ),
            _raw(
                active,
                elements=[_safari_address("https://allowed.example/two")],
            ),
        ]
    )

    class Provider:
        available = True

        def capture_frontmost(self, **kwargs):
            assert kwargs == {
                "focused_window_only": True,
                "require_complete_tree": True,
            }
            raw = next(snapshots)
            return AXCaptureResult(
                raw,
                "",
                raw["apps"],
                {},
                tree_complete_verified=True,
                effective_max_depth=100,
            )

    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)
    cfg = config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"])

    assert scheduler._build_capture(cfg, Provider(), None) is None


def test_url_policy_rejects_identifier_to_label_only_snapshot_downgrade(
    ac_root, monkeypatch
) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    address = "https://allowed.example/stable"
    snapshots = iter(
        [
            _raw(active, elements=[_safari_address(address)]),
            _raw(
                active,
                elements=[
                    {
                        "role": "AXTextField",
                        "title": "Smart Search Field",
                        "value": address,
                    }
                ],
            ),
        ]
    )

    class Provider:
        available = True
        calls = 0

        def capture_frontmost(self, **_kwargs):
            self.calls += 1
            raw = next(snapshots)
            return AXCaptureResult(
                raw,
                "",
                raw["apps"],
                {},
                tree_complete_verified=True,
                effective_max_depth=100,
            )

    provider = Provider()
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    assert (
        scheduler._build_capture(
            config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"]),
            provider,
            None,
        )
        is None
    )
    assert provider.calls == 2


@pytest.mark.parametrize("separator", [",", ";"])
def test_delimited_forbidden_url_cannot_merge_into_allowed_candidate(
    ac_root, monkeypatch, separator
) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(
        _raw(
            active,
            elements=[
                _safari_address("https://allowed.example/start"),
                {
                    "role": "AXStaticText",
                    "value": (
                        f"https://allowed.example/x{separator}https://forbidden.example/private"
                    ),
                },
            ],
        )
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)
    cfg = config_mod.CaptureConfig(
        allowed_url_patterns=["allowed.example", "forbidden.example"],
        excluded_url_patterns=["https://forbidden.example/private"],
    )

    assert scheduler._build_capture(cfg, provider, None) is None
    assert provider.calls == 1


def test_adjacent_double_url_fails_closed_before_second_snapshot(ac_root, monkeypatch) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(
        _raw(
            active,
            elements=[
                _safari_address("https://allowed.example/start"),
                {
                    "role": "AXStaticText",
                    "value": ("https://allowed.example/xhttps://forbidden.example/private"),
                },
            ],
        )
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    assert (
        scheduler._build_capture(
            config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"]),
            provider,
            None,
        )
        is None
    )
    assert provider.calls == 1


def test_url_policy_projects_away_non_atomic_page_content_and_titles(
    ac_root, monkeypatch
) -> None:
    title_marker = "FORBIDDEN-NONATOMIC-WINDOW-TITLE"
    active = _meta(
        app_name="Safari",
        bundle_id="com.apple.Safari",
        title=title_marker,
    )

    def snapshot(text: str) -> dict:
        return _raw(
            active,
            elements=[
                _safari_address("https://allowed.example/stable"),
                {"role": "AXStaticText", "value": text},
            ],
        )

    first_marker = "FORBIDDEN-FIRST-SNAPSHOT-BODY"
    second_marker = "FORBIDDEN-SECOND-SNAPSHOT-BODY"
    snapshots = iter((snapshot(first_marker), snapshot(second_marker)))

    class Provider:
        available = True

        def capture_frontmost(self, **_kwargs):
            raw = next(snapshots)
            return AXCaptureResult(
                raw,
                "",
                raw["apps"],
                {},
                tree_complete_verified=True,
                effective_max_depth=100,
            )

    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)
    cfg = config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"])

    result = scheduler._build_capture(cfg, Provider(), None)

    assert result is not None
    serialized = json.dumps(result)
    assert first_marker not in serialized
    assert second_marker not in serialized
    assert title_marker not in serialized
    assert "ax_tree" not in result
    assert "focused_element" not in result
    assert result["visible_text"] == ""
    assert result["url"] == "https://allowed.example/stable"
    assert result["window_meta"]["title"] == ""
    assert result["trigger"]["window_title"] == ""
    assert result["privacy"] == {
        "decision": "allowed",
        "policy_version": 3,
        "content_mode": "url_metadata_only",
    }


def test_scheme_less_allowed_address_is_not_durable_url_evidence(ac_root, monkeypatch) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(_raw(active, elements=[_safari_address("allowed.example/path")]))
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    assert (
        scheduler._build_capture(
            config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"]),
            provider,
            None,
        )
        is None
    )
    assert provider.calls == 1


@pytest.mark.parametrize(
    "address",
    [
        "https://allowed.example/path,part",
        "https://allowed.example/path;param",
        "https://allowed.example/file.",
    ],
)
def test_strict_address_is_not_retokenized_as_generic_prose(
    ac_root, monkeypatch, address
) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(_raw(active, elements=[_safari_address(address)]))
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    result = scheduler._build_capture(
        config_mod.CaptureConfig(allowed_url_patterns=[address]),
        provider,
        None,
    )

    assert result is not None
    assert result["url"] == address


def test_url_metadata_projection_is_the_only_durable_and_logged_shape(
    ac_root, monkeypatch, caplog
) -> None:
    title_marker = "FORBIDDEN-URL-RACE-TITLE"
    body_marker = "FORBIDDEN-URL-RACE-BODY"
    trigger_marker = "FORBIDDEN-QUEUED-TRIGGER-DETAIL"
    active = _meta(
        app_name="Safari",
        bundle_id="com.apple.Safari",
        title=title_marker,
    )
    provider = _Provider(
        _raw(
            active,
            elements=[
                _safari_address("https://allowed.example/stable"),
                {
                    "role": "AXStaticText",
                    "title": body_marker,
                    "description": body_marker,
                    "value": body_marker,
                },
            ],
        )
    )
    trigger = {
        "event_type": "UserTextInput",
        "app_name": active.app_name,
        "bundle_id": active.bundle_id,
        "window_title": active.title,
        "pid": active.pid,
        "details": {"value": trigger_marker},
    }
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    with caplog.at_level(logging.INFO):
        path = scheduler.capture_once(
            config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"]),
            provider,
            trigger=trigger,
        )

    assert path is not None
    capture = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(capture)
    markers = (title_marker, body_marker, trigger_marker)
    assert all(marker not in serialized for marker in markers)
    assert all(marker not in caplog.text for marker in markers)
    assert capture["schema_version"] == 5
    assert capture["url"] == "https://allowed.example/stable"
    assert "ax_tree" not in capture
    assert "focused_element" not in capture
    assert "screenshot" not in capture

    with scheduler.fts_store.cursor() as conn:
        row = conn.execute(
            "SELECT app_name, window_title, focused_value, visible_text, url "
            "FROM captures LIMIT 1"
        ).fetchone()
    assert row is not None
    indexed = "\n".join(str(value or "") for value in row)
    assert all(marker not in indexed for marker in markers)
    hook = json.dumps(scheduler._session_hook_event(capture))
    assert all(marker not in hook for marker in markers)


def test_url_projection_is_bound_to_strict_gate_evidence(ac_root, monkeypatch) -> None:
    poison = "https://forbidden.invalid/POISONED-S1-URL"
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(
        _raw(
            active,
            elements=[_safari_address("https://allowed.example/stable")],
        )
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)
    original_enrich = scheduler.s1_parser.enrich

    def poison_compatibility_fields(capture: dict) -> None:
        original_enrich(capture)
        capture["url"] = poison
        capture["visible_text"] = poison

    monkeypatch.setattr(scheduler.s1_parser, "enrich", poison_compatibility_fields)

    result = scheduler._build_capture(
        config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"]),
        provider,
        None,
    )

    assert result is not None
    assert result["url"] == "https://allowed.example/stable"
    assert poison not in json.dumps(result)


def test_browser_with_active_url_policy_and_unknown_url_fails_closed(ac_root, monkeypatch) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(_raw(active, elements=[{"role": "AXStaticText", "value": "Page"}]))
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    cfg = config_mod.CaptureConfig(allowed_url_patterns=["example.com"])
    assert scheduler._build_capture(cfg, provider, None) is None


def test_browser_body_decoy_cannot_replace_unverifiable_chrome_address(
    ac_root, monkeypatch
) -> None:
    marker = "PRIVATE-CHROME-PAGE-CONTENT"
    active = _meta(app_name="Chrome", bundle_id="com.google.Chrome")
    raw = _raw(
        active,
        elements=[
            {
                "role": "AXTextField",
                "identifier": "omnibox",
                "value": "chrome://password-manager/",
            },
            {
                "role": "AXWebArea",
                "children": [
                    {
                        "role": "AXStaticText",
                        "value": f"https://allowed.example/help {marker}",
                    }
                ],
            },
        ],
    )
    provider = _Provider(raw)
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    result = scheduler._build_capture(
        config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"]),
        provider,
        None,
    )

    assert result is None
    assert provider.calls == 1


def test_unrecognized_toolbar_field_cannot_impersonate_browser_address(
    ac_root, monkeypatch
) -> None:
    active = _meta(app_name="Chrome", bundle_id="com.google.Chrome")
    provider = _Provider(
        _raw(
            active,
            elements=[
                {
                    "role": "AXTextField",
                    "identifier": "extension-note-field",
                    "value": "https://allowed.example/help",
                },
                {"role": "AXStaticText", "value": "PRIVATE-PAGE-CONTENT"},
            ],
        )
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    assert (
        scheduler._build_capture(
            config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"]),
            provider,
            None,
        )
        is None
    )
    assert provider.calls == 1


def test_scheme_less_address_must_pass_http_and_https_policy(ac_root, monkeypatch) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(_raw(active, elements=[_safari_address("private.example/path")]))
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    cfg = config_mod.CaptureConfig(excluded_url_patterns=["http://private.example/path"])
    assert scheduler._build_capture(cfg, provider, None) is None
    assert provider.calls == 1


def test_url_policy_requires_verified_complete_tree_receipt(ac_root, monkeypatch) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    raw = _raw(active, elements=[_safari_address("https://allowed.example/page")])

    class Provider:
        available = True

        def capture_frontmost(self, **_kwargs):
            return AXCaptureResult(raw, "", raw["apps"], {})

    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    assert (
        scheduler._build_capture(
            config_mod.CaptureConfig(allowed_url_patterns=["allowed.example"]),
            Provider(),
            None,
        )
        is None
    )


def test_url_policy_denies_unknown_bundle_even_with_an_allowed_url_decoy(
    ac_root, monkeypatch
) -> None:
    active = _meta()
    provider = _Provider(
        _raw(
            active,
            elements=[{"role": "AXStaticText", "value": "https://example.com/help"}],
        )
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    cfg = config_mod.CaptureConfig(allowed_url_patterns=["example.com"])
    result = scheduler._build_capture(cfg, provider, None)

    assert result is None
    assert provider.complete_tree_requests == []


def test_unlisted_browser_bundle_cannot_bypass_url_exclusion(ac_root, monkeypatch) -> None:
    active = _meta(app_name="Vivaldi", bundle_id="com.vivaldi.Vivaldi")
    provider = _Provider(
        _raw(
            active,
            elements=[{"role": "AXComboBox", "value": "https://forbidden.example/private"}],
        )
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    cfg = config_mod.CaptureConfig(excluded_url_patterns=["forbidden.example"])
    assert scheduler.capture_once(cfg, provider) is None
    assert provider.calls == 0
    assert not list(ac_root.rglob("*.json"))


@pytest.mark.parametrize(
    ("field", "value", "rule"),
    [
        ("value", "localhost:8000/private", "localhost"),
        ("value", "internal/private", "internal"),
        ("value", "[::1]:8000/private", "[::1]"),
        ("description", "forbidden.example/private", "forbidden.example"),
        ("title", "forbidden.example/private — Vivaldi", "forbidden.example"),
        ("identifier", "forbidden.example/private", "forbidden.example"),
        ("domIdentifier", "forbidden.example/private", "forbidden.example"),
    ],
)
def test_unlisted_browser_bare_addresses_in_persisted_fields_are_gated(
    ac_root, monkeypatch, field, value, rule
) -> None:
    active = _meta(app_name="Vivaldi", bundle_id="com.vivaldi.Vivaldi")
    provider = _Provider(_raw(active, elements=[{"role": "AXTextField", field: value}]))
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    cfg = config_mod.CaptureConfig(excluded_url_patterns=[rule])
    assert scheduler._build_capture(cfg, provider, None) is None


@pytest.mark.parametrize("field", ["description", "identifier"])
def test_unlisted_browser_bare_address_in_window_metadata_is_gated(
    ac_root, monkeypatch, field
) -> None:
    active = _meta(app_name="Vivaldi", bundle_id="com.vivaldi.Vivaldi")
    raw = _raw(active)
    raw["apps"][0]["windows"][0][field] = "forbidden.example/private"
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    cfg = config_mod.CaptureConfig(excluded_url_patterns=["forbidden.example"])
    assert scheduler._build_capture(cfg, _Provider(raw), None) is None


def test_unlisted_browser_bare_address_in_window_title_is_gated(ac_root, monkeypatch) -> None:
    active = _meta(
        app_name="Vivaldi",
        bundle_id="com.vivaldi.Vivaldi",
        title="forbidden.example/private — Vivaldi",
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    cfg = config_mod.CaptureConfig(excluded_url_patterns=["forbidden.example"])
    assert scheduler._build_capture(cfg, _Provider(_raw(active)), None) is None


def test_screenshot_is_bound_to_exact_target_and_rechecked_after_pixels(
    ac_root, monkeypatch
) -> None:
    active = _meta()
    provider = _Provider(_raw(active))
    calls: list[scheduler.window_meta.WindowMeta] = []
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)

    def grab(**kwargs):
        calls.append(kwargs["target"])
        return scheduler.screenshot.Screenshot(
            image_base64="safe-jpeg",
            width=640,
            height=480,
            window_meta=active,
        )

    monkeypatch.setattr(scheduler.screenshot, "grab", grab)
    result = scheduler._build_capture(
        config_mod.CaptureConfig(include_screenshot=True), provider, None
    )

    assert result is not None
    assert calls == [active]
    assert result["screenshot"]["image_base64"] == "safe-jpeg"


def test_screenshot_helper_failure_drops_the_whole_observation(ac_root, monkeypatch) -> None:
    active = _meta()
    provider = _Provider(_raw(active))
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)
    monkeypatch.setattr(scheduler.screenshot, "grab", lambda **_kwargs: None)

    assert (
        scheduler._build_capture(config_mod.CaptureConfig(include_screenshot=True), provider, None)
        is None
    )


def test_browser_screenshot_is_disabled_when_url_policy_is_active(ac_root, monkeypatch) -> None:
    active = _meta(app_name="Safari", bundle_id="com.apple.Safari")
    provider = _Provider(
        _raw(
            active,
            elements=[_safari_address("https://allowed.example/page")],
        )
    )
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)
    monkeypatch.setattr(
        scheduler.screenshot,
        "grab",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("URL-gated browser pixels must never be collected")
        ),
    )

    cfg = config_mod.CaptureConfig(
        allowed_url_patterns=["allowed.example"],
        include_screenshot=True,
    )
    assert scheduler._build_capture(cfg, provider, None) is None


def test_focus_change_after_screenshot_discards_pixels_and_ax(ac_root, monkeypatch) -> None:
    active = _meta(title="Same title", window_id=801)
    sibling = replace(active, window_id=802)
    provider = _Provider(_raw(active))
    observed = iter((active, active, sibling))
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: next(observed))
    monkeypatch.setattr(
        scheduler.screenshot,
        "grab",
        lambda **_kwargs: scheduler.screenshot.Screenshot(
            image_base64="discard-me",
            width=640,
            height=480,
            window_meta=active,
        ),
    )

    assert (
        scheduler._build_capture(config_mod.CaptureConfig(include_screenshot=True), provider, None)
        is None
    )


def test_present_trigger_pid_must_match_exact_window() -> None:
    active = _meta(pid=700)
    trigger = {
        "event_type": "UserMouseClick",
        "bundle_id": active.bundle_id,
        "window_title": active.title,
        "pid": 701,
    }

    assert scheduler._trigger_matches_window(trigger, active) is False


def test_watcher_details_are_wakeup_only_and_never_persisted(ac_root, monkeypatch) -> None:
    marker = "FORBIDDEN-WATCHER-DETAIL-URL"
    active = _meta(title="Same title")
    provider = _Provider(_raw(active, elements=[{"role": "AXStaticText", "value": "safe"}]))
    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: active)
    trigger = {
        "event_type": "UserTextInput",
        "app_name": active.app_name,
        "bundle_id": active.bundle_id,
        "window_title": active.title,
        "pid": active.pid,
        "details": {"role": "AXTextField", "value": f"https://secret.invalid/{marker}"},
    }

    path = scheduler.capture_once(config_mod.CaptureConfig(), provider, trigger=trigger)

    assert path is not None
    raw = path.read_text(encoding="utf-8")
    assert marker not in raw
    assert "details" not in raw
    assert path.stat().st_mode & 0o777 == 0o600
