from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.capture.ax_capture import _BoundedProcessResult
from openchronicle.prompt_rescue import selection as selection_mod
from openchronicle.prompt_rescue.selection import (
    SelectionCaptureError,
    capture_selection,
    prepare_selection_helper,
)

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


def _window_payload(stdout: bytes) -> bytes:
    defaults = json.loads(_payload())["selection"]
    try:
        selection = json.loads(stdout).get("selection", defaults)
    except (UnicodeDecodeError, json.JSONDecodeError):
        selection = defaults
    window = {
        "schema_version": 1,
        "app_name": selection.get("app_name", defaults["app_name"]),
        "bundle_id": selection.get("bundle_id", defaults["bundle_id"]),
        "pid": selection.get("pid", defaults["pid"]),
        "window_title": selection.get("window_title", defaults["window_title"]),
    }
    return json.dumps({"ok": True, "window": window}).encode()


def _runner(stdout: bytes, *, returncode: int = 0, calls: list[list[str]] | None = None):
    def run(args, **_kwargs):
        if calls is not None:
            calls.append(args)
        if "--frontmost-window-metadata" in args:
            return _BoundedProcessResult(returncode=0, stdout=_window_payload(stdout))
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
    calls: list[list[str]] = []
    with pytest.raises(SelectionCaptureError) as excluded:
        capture_selection(
            cfg,
            helper_path=Path("/trusted/helper"),
            process_runner=_runner(_payload(), calls=calls),
        )
    assert excluded.value.code == "privacy_denied"
    assert calls == [["/trusted/helper", "--frontmost-window-metadata"]]

    cfg.capture.excluded_bundle_ids = []
    cfg.capture.allowed_url_patterns = ["example.com"]
    calls.clear()
    with pytest.raises(SelectionCaptureError) as url_policy:
        capture_selection(
            cfg,
            helper_path=Path("/trusted/helper"),
            process_runner=_runner(_payload(), calls=calls),
        )
    assert url_policy.value.code == "url_policy_unverifiable"
    assert calls == []


def test_selection_adapter_rejects_preflight_to_selection_focus_change() -> None:
    selection = _payload(window_title="Changed before selection")

    def changed_runner(args, **_kwargs):
        if "--frontmost-window-metadata" in args:
            return _BoundedProcessResult(returncode=0, stdout=_window_payload(_payload()))
        return _BoundedProcessResult(returncode=0, stdout=selection)

    with pytest.raises(SelectionCaptureError) as changed:
        capture_selection(
            config_mod.Config(),
            helper_path=Path("/trusted/helper"),
            process_runner=changed_runner,
        )
    assert changed.value.code == "focus_changed"


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


def test_selection_helper_builds_in_writable_runtime_and_capture_never_compiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_AX_SELECTION_HELPER", raising=False)
    monkeypatch.setattr(selection_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(selection_mod.paths, "root", lambda: tmp_path)
    compiled: list[tuple[Path, Path]] = []

    def compile_helper(source: Path, binary: Path) -> None:
        compiled.append((source, binary))
        binary.write_bytes(b"helper")
        binary.chmod(0o700)

    monkeypatch.setattr(selection_mod, "_maybe_compile", compile_helper)
    helper = prepare_selection_helper()

    expected = tmp_path / "runtime" / "helpers" / "mac-ax-selection"
    assert helper == expected
    assert compiled == [(selection_mod._selection_helper_source(), expected)]
    assert expected.parent.stat().st_mode & 0o777 == 0o700
    assert expected.stat().st_mode & 0o777 == 0o700

    def reject_compile(*_args, **_kwargs) -> None:
        raise AssertionError("the desktop request path must never invoke swiftc")

    monkeypatch.setattr(selection_mod, "_maybe_compile", reject_compile)
    receipt = capture_selection(config_mod.Config(), process_runner=_runner(_payload()))
    assert receipt.selected_text == "Draft the launch prompt"
