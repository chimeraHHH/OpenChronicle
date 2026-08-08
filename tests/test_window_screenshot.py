from __future__ import annotations

import base64
import io
import json
from dataclasses import replace
from pathlib import Path

from PIL import Image

from openchronicle.capture import ax_capture, screenshot, window_meta


def _target(*, title: str = "Roadmap") -> window_meta.WindowMeta:
    return window_meta.WindowMeta(
        app_name="Editor",
        title=title,
        bundle_id="com.example.editor",
        pid=123,
        window_id=456,
        bounds=window_meta.WindowBounds(x=-120.0, y=24.0, width=1440.0, height=900.0),
    )


def _identity(meta: window_meta.WindowMeta) -> dict[str, object]:
    request = meta.to_capture_request()
    assert request is not None
    return request


def _jpeg(width: int = 32, height: int = 20) -> str:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (20, 40, 60)).save(output, format="JPEG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _capture_payload(meta: window_meta.WindowMeta, *, width: int = 32, height: int = 20):
    return {
        "schema_version": 1,
        "mime_type": "image/jpeg",
        "image_base64": _jpeg(width, height),
        "width": width,
        "height": height,
        "window_meta": _identity(meta),
    }


def test_parse_window_meta_requires_stable_pid_window_and_valid_bounds() -> None:
    expected = _target()
    assert window_meta.parse_window_meta(_identity(expected)) == expected

    for field, value in (("pid", 0), ("pid", True), ("window_id", -1), ("bundle_id", "")):
        candidate = _identity(expected)
        candidate[field] = value
        assert window_meta.parse_window_meta(candidate) is None

    for field, value in (
        ("width", 0),
        ("height", -1),
        ("x", float("inf")),
        ("width", 32_769),
        ("height", 32_769),
    ):
        candidate = _identity(expected)
        candidate["bounds"] = dict(candidate["bounds"])
        candidate["bounds"][field] = value  # type: ignore[index]
        assert window_meta.parse_window_meta(candidate) is None

    oversized_area = _identity(expected)
    oversized_area["bounds"] = {
        "x": 0,
        "y": 0,
        "width": 20_000,
        "height": 20_000,
    }
    assert window_meta.parse_window_meta(oversized_area) is None


def test_capture_identity_includes_title_and_exact_bounds() -> None:
    expected = _target()

    assert expected.same_capture_target(_target()) is True
    assert expected.same_capture_target(_target(title="Another window")) is False
    assert expected.same_capture_target(
        replace(expected, bounds=window_meta.WindowBounds(-120.0, 24.0, 1439.0, 900.0))
    ) is False


def test_active_window_uses_native_metadata_mode_and_strict_parser(monkeypatch) -> None:
    expected = _target()
    calls: list[list[str]] = []
    monkeypatch.setattr(window_meta.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(window_meta, "_helper_path", lambda: "/safe/mac-ax-helper")

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["stdout_limit"] == window_meta._METADATA_STDOUT_LIMIT_BYTES
        return ax_capture._BoundedProcessResult(
            returncode=0, stdout=json.dumps(_identity(expected)).encode()
        )

    monkeypatch.setattr(window_meta, "_run_bounded_process", run)

    assert window_meta.active_window() == expected
    assert calls == [["/safe/mac-ax-helper", "--frontmost-window-metadata"]]


def test_active_window_fails_closed_without_native_identity(monkeypatch) -> None:
    monkeypatch.setattr(window_meta.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(window_meta, "_helper_path", lambda: "/safe/mac-ax-helper")

    for stdout in (
        "not-json",
        '{"schema_version":1,"pid":NaN}',
        json.dumps({"schema_version": 1, "app_name": "Editor", "title": "Roadmap"}),
    ):
        monkeypatch.setattr(
            window_meta,
            "_run_bounded_process",
            lambda *_args, _stdout=stdout, **_kwargs: ax_capture._BoundedProcessResult(
                returncode=0, stdout=_stdout.encode()
            ),
        )
        assert window_meta.active_window() == window_meta.WindowMeta()


def test_screenshot_requires_exact_target_before_starting_helper(monkeypatch) -> None:
    monkeypatch.setattr(screenshot.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        screenshot,
        "_helper_path",
        lambda: (_ for _ in ()).throw(AssertionError("helper must not be resolved")),
    )

    assert screenshot.grab(target=None) is None
    assert screenshot.grab(target=window_meta.WindowMeta()) is None
    assert screenshot.grab(target=_target(), max_width=4097) is None


def test_screenshot_passes_sensitive_identity_on_stdin_and_accepts_verified_jpeg(
    monkeypatch,
) -> None:
    secret_title = "Private plan https://secret.example/token"
    expected = _target(title=secret_title)
    observed: dict[str, object] = {}
    monkeypatch.setattr(screenshot.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(screenshot, "_helper_path", lambda: "/safe/mac-ax-helper")

    def run(args, **kwargs):
        observed["args"] = args
        observed["input"] = kwargs["input_bytes"]
        return ax_capture._BoundedProcessResult(
            returncode=0,
            stdout=json.dumps(_capture_payload(expected)).encode(),
        )

    monkeypatch.setattr(screenshot, "_run_bounded_process", run)

    result = screenshot.grab(target=expected, max_width=800, jpeg_quality=71)

    assert result is not None
    assert result.width == 32
    assert result.height == 20
    assert result.window_meta == expected
    assert observed["args"] == [
        "/safe/mac-ax-helper",
        "--capture-frontmost-window",
        "--max-width",
        "800",
        "--jpeg-quality",
        "71",
    ]
    assert secret_title not in " ".join(observed["args"])
    assert json.loads(observed["input"])["title"] == secret_title  # type: ignore[arg-type]


def test_screenshot_discards_native_result_if_window_identity_changed(monkeypatch) -> None:
    expected = _target()
    changed = _target(title="Different window")
    monkeypatch.setattr(screenshot.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(screenshot, "_helper_path", lambda: "/safe/mac-ax-helper")
    monkeypatch.setattr(
        screenshot,
        "_run_bounded_process",
        lambda *args, **kwargs: ax_capture._BoundedProcessResult(
            returncode=0, stdout=json.dumps(_capture_payload(changed)).encode()
        ),
    )

    assert screenshot.grab(target=expected) is None


def test_screenshot_rejects_permission_failure_invalid_jpeg_and_oversize(monkeypatch) -> None:
    expected = _target()
    monkeypatch.setattr(screenshot.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(screenshot, "_helper_path", lambda: "/safe/mac-ax-helper")

    responses = [
        ax_capture._BoundedProcessResult(returncode=3, stdout=b""),
        ax_capture._BoundedProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    **_capture_payload(expected),
                    "image_base64": base64.b64encode(b"not a jpeg").decode("ascii"),
                }
            ).encode(),
        ),
        ax_capture._BoundedProcessResult(
            returncode=0,
            stdout=json.dumps(_capture_payload(expected, width=801)).encode(),
        ),
    ]

    for response in responses:
        monkeypatch.setattr(
            screenshot,
            "_run_bounded_process",
            lambda *args, _response=response, **kwargs: _response,
        )
        assert screenshot.grab(target=expected, max_width=800) is None


def test_screenshot_implementation_has_no_monitor_capture_fallback() -> None:
    source = Path(screenshot.__file__).read_text()
    helper_source = (Path(__file__).parents[1] / "resources" / "mac-ax-helper.swift").read_text()

    assert "import mss" not in source
    assert "monitors[" not in source
    assert "--capture-frontmost-window" in source
    assert "windowListFromArrayScreenBounds" in helper_source
    assert "CGPreflightScreenCaptureAccess" in helper_source
    assert 'output["window_meta"] = after.toDict()' in helper_source
    assert "selectFocusedCGWindow" in helper_source
    assert "geometryMatches.allSatisfy" in helper_source
    assert "candidate.title == axTitle" in helper_source
    assert "CGDisplayCreateImage" not in helper_source
