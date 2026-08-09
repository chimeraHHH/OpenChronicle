"""Fail-closed macOS selected-text adapter for explicit Prompt Rescue import."""

from __future__ import annotations

import json
import os
import platform
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..capture.ax_capture import _BoundedProcessResult, _maybe_compile, _run_bounded_process
from ..config import Config
from ..privacy import policy as privacy_policy

_MAX_OUTPUT_BYTES = 128 * 1024 + 1
_MAX_STDERR_BYTES = 16 * 1024
_TIMEOUT_SECONDS = 3.0
_SELECTION_KEYS = frozenset(
    {
        "schema_version",
        "source_kind",
        "selected_text",
        "captured_at",
        "app_name",
        "bundle_id",
        "pid",
        "window_title",
        "element_role",
        "element_subrole",
        "selection_location",
        "selection_length",
    }
)
_HELPER_ERRORS = frozenset(
    {
        "accessibility_untrusted",
        "no_focused_application",
        "no_focused_window",
        "no_focused_element",
        "self_selection",
        "secure_field",
        "no_selection",
        "multiple_selection",
        "selection_too_large",
        "invalid_identity",
        "focus_changed",
        "output_unavailable",
    }
)


class SelectionCaptureError(RuntimeError):
    """A sanitized selection boundary failure safe for local protocol mapping."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SelectionReceipt:
    selected_text: str
    captured_at: str
    app_name: str
    bundle_id: str
    pid: int
    window_title: str
    element_role: str
    element_subrole: str
    selection_location: int
    selection_length: int

    @property
    def binding(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "captured_at": self.captured_at,
            "app_name": self.app_name,
            "bundle_id": self.bundle_id,
            "pid": self.pid,
            "window_title": self.window_title,
            "element_role": self.element_role,
            "element_subrole": self.element_subrole,
            "selection_location": self.selection_location,
            "selection_length": self.selection_length,
        }


def capture_selection(
    cfg: Config,
    *,
    helper_path: Path | None = None,
    process_runner: Callable[..., _BoundedProcessResult] = _run_bounded_process,
) -> SelectionReceipt:
    """Capture one stable selection and apply current window privacy policy."""
    helper = helper_path or _resolve_helper_path()
    if helper is None:
        raise SelectionCaptureError("helper_unavailable")
    result = process_runner(
        [str(helper)],
        timeout=_TIMEOUT_SECONDS,
        stdout_limit=_MAX_OUTPUT_BYTES,
        stderr_limit=_MAX_STDERR_BYTES,
    )
    if result.timed_out:
        raise SelectionCaptureError("helper_timeout")
    if result.stdout_exceeded or result.stderr_exceeded or result.output_incomplete:
        raise SelectionCaptureError("helper_invalid_output")
    payload = _decode_payload(result.stdout)
    if result.returncode != 0:
        code = payload.get("error_code") if isinstance(payload, dict) else None
        raise SelectionCaptureError(code if code in _HELPER_ERRORS else "helper_failed")
    receipt = _parse_receipt(payload)

    decision = privacy_policy.evaluate_window(
        cfg.capture,
        app_name=receipt.app_name,
        bundle_id=receipt.bundle_id,
        window_title=receipt.window_title,
    )
    if not decision.allowed:
        raise SelectionCaptureError("privacy_denied")
    if privacy_policy.has_url_policy(cfg.capture):
        # The exact-selection helper deliberately does not read neighboring
        # browser controls, so it cannot prove the active URL against a policy.
        raise SelectionCaptureError("url_policy_unverifiable")
    if receipt.bundle_id.casefold() == "app.openchronicle.desktop":
        raise SelectionCaptureError("self_selection")
    return receipt


def _decode_payload(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectionCaptureError("helper_invalid_output") from exc
    if not isinstance(payload, dict):
        raise SelectionCaptureError("helper_invalid_output")
    return payload


def _parse_receipt(payload: dict[str, Any]) -> SelectionReceipt:
    if set(payload) != {"ok", "selection"} or payload.get("ok") is not True:
        raise SelectionCaptureError("helper_invalid_output")
    raw = payload["selection"]
    if not isinstance(raw, dict) or set(raw) != _SELECTION_KEYS:
        raise SelectionCaptureError("helper_invalid_output")
    if raw.get("schema_version") != 1 or raw.get("source_kind") != "macos_selection":
        raise SelectionCaptureError("helper_invalid_output")

    selected_text = _text(raw.get("selected_text"), 20_000, nonempty=True)
    captured_at = _text(raw.get("captured_at"), 100, nonempty=True)
    try:
        parsed_time = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SelectionCaptureError("helper_invalid_output") from exc
    if parsed_time.tzinfo is None:
        raise SelectionCaptureError("helper_invalid_output")
    pid = _integer(raw.get("pid"), 1, 2_147_483_647)
    selection_location = _integer(raw.get("selection_location"), 0, 2_147_483_647)
    selection_length = _integer(raw.get("selection_length"), 1, 2_147_483_647)
    return SelectionReceipt(
        selected_text=selected_text,
        captured_at=captured_at,
        app_name=_text(raw.get("app_name"), 512, nonempty=False),
        bundle_id=_text(raw.get("bundle_id"), 512, nonempty=True),
        pid=pid,
        window_title=_text(raw.get("window_title"), 512, nonempty=False),
        element_role=_text(raw.get("element_role"), 128, nonempty=True),
        element_subrole=_text(raw.get("element_subrole"), 128, nonempty=False),
        selection_location=selection_location,
        selection_length=selection_length,
    )


def _text(value: object, maximum: int, *, nonempty: bool) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise SelectionCaptureError("helper_invalid_output")
    if nonempty and not value.strip():
        raise SelectionCaptureError("helper_invalid_output")
    return value


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SelectionCaptureError("helper_invalid_output")
    return value


def _resolve_helper_path() -> Path | None:
    if platform.system() != "Darwin":
        return None
    override = os.environ.get("OPENCHRONICLE_AX_SELECTION_HELPER")
    if override:
        candidate = Path(override).expanduser().resolve()
        return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None

    candidates: list[Path] = []
    try:
        from importlib.resources import files as package_files

        bundled = Path(str(package_files("openchronicle").joinpath("_bundled")))
        candidates.append(bundled / "mac-ax-selection")
    except (ModuleNotFoundError, ValueError):
        pass
    candidates.append(Path(__file__).resolve().parents[3] / "resources" / "mac-ax-selection")
    for binary in candidates:
        source = binary.with_suffix(".swift")
        if source.is_file():
            _maybe_compile(source, binary)
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
    return None
