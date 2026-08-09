from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.capture.ax_capture import _BoundedProcessResult
from openchronicle.prompt_rescue.selection import SelectionCaptureError, capture_selection

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _payload(**updates) -> bytes:
    selection = {
        "schema_version": 1,
        "source_kind": "macos_selection",
        "selected_text": "Draft the launch prompt",
        "captured_at": "2026-08-09T12:00:00Z",
        "app_name": "Notes",
        "bundle_id": "com.apple.Notes",
        "pid": 123,
        "window_title": "Launch notes",
        "element_role": "AXTextArea",
        "element_subrole": "",
        "selection_location": 4,
        "selection_length": 23,
    }
    selection.update(updates)
    return json.dumps({"ok": True, "selection": selection}).encode()


def _runner(stdout: bytes, *, returncode: int = 0):
    def run(*_args, **_kwargs):
        return _BoundedProcessResult(returncode=returncode, stdout=stdout)

    return run


def test_selection_adapter_returns_exact_closed_binding() -> None:
    receipt = capture_selection(
        config_mod.Config(),
        helper_path=Path("/trusted/helper"),
        process_runner=_runner(_payload()),
    )

    assert receipt.selected_text == "Draft the launch prompt"
    assert receipt.binding == {
        "schema_version": 1,
        "captured_at": "2026-08-09T12:00:00Z",
        "app_name": "Notes",
        "bundle_id": "com.apple.Notes",
        "pid": 123,
        "window_title": "Launch notes",
        "element_role": "AXTextArea",
        "element_subrole": "",
        "selection_location": 4,
        "selection_length": 23,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra="unknown"),
        lambda value: value.update(bundle_id=""),
        lambda value: value.update(selection_length=0),
        lambda value: value.update(captured_at="not-a-time"),
        lambda value: value.update(selected_text="   "),
    ],
)
def test_selection_adapter_rejects_malformed_native_receipt(mutation) -> None:
    raw = json.loads(_payload())
    mutation(raw["selection"])
    with pytest.raises(SelectionCaptureError, match="helper_invalid_output"):
        capture_selection(
            config_mod.Config(),
            helper_path=Path("/trusted/helper"),
            process_runner=_runner(json.dumps(raw).encode()),
        )


def test_selection_adapter_preserves_only_allowlisted_native_error_code() -> None:
    with pytest.raises(SelectionCaptureError) as captured:
        capture_selection(
            config_mod.Config(),
            helper_path=Path("/trusted/helper"),
            process_runner=_runner(
                json.dumps({"ok": False, "error_code": "secure_field"}).encode(),
                returncode=2,
            ),
        )
    assert captured.value.code == "secure_field"

    with pytest.raises(SelectionCaptureError) as unknown:
        capture_selection(
            config_mod.Config(),
            helper_path=Path("/trusted/helper"),
            process_runner=_runner(
                json.dumps({"ok": False, "error_code": "private detail"}).encode(),
                returncode=2,
            ),
        )
    assert unknown.value.code == "helper_failed"


def test_selection_adapter_applies_window_and_url_policy() -> None:
    cfg = config_mod.Config()
    cfg.capture.excluded_bundle_ids = ["com.apple.notes"]
    with pytest.raises(SelectionCaptureError) as excluded:
        capture_selection(
            cfg,
            helper_path=Path("/trusted/helper"),
            process_runner=_runner(_payload()),
        )
    assert excluded.value.code == "privacy_denied"

    cfg.capture.excluded_bundle_ids = []
    cfg.capture.allowed_url_patterns = ["example.com"]
    with pytest.raises(SelectionCaptureError) as url_policy:
        capture_selection(
            cfg,
            helper_path=Path("/trusted/helper"),
            process_runner=_runner(_payload()),
        )
    assert url_policy.value.code == "url_policy_unverifiable"


def test_selection_adapter_never_accepts_own_desktop_selection() -> None:
    with pytest.raises(SelectionCaptureError) as captured:
        capture_selection(
            config_mod.Config(),
            helper_path=Path("/trusted/helper"),
            process_runner=_runner(_payload(bundle_id="app.openchronicle.desktop")),
        )
    assert captured.value.code == "self_selection"


def test_native_selection_helper_has_no_clipboard_or_whole_value_fallback() -> None:
    source = (REPOSITORY_ROOT / "resources" / "mac-ax-selection.swift").read_text(encoding="utf-8")

    assert "kAXSelectedTextAttribute" in source
    assert "kAXSelectedTextRangeAttribute" in source
    assert "kAXSecureTextFieldSubrole" in source
    assert "kAXValueAttribute" not in source
    assert "NSPasteboard" not in source
    assert "AXUIElementSetAttributeValue" not in source
