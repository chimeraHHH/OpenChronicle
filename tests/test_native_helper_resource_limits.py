from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openchronicle.capture import ax_capture, screenshot, window_meta


def _target() -> window_meta.WindowMeta:
    return window_meta.WindowMeta(
        app_name="Editor",
        title="Roadmap",
        bundle_id="com.example.editor",
        pid=123,
        window_id=456,
        bounds=window_meta.WindowBounds(x=0, y=0, width=1440, height=900),
    )


def _process_writing(*, stdout_bytes: int = 0, stderr_bytes: int = 0) -> list[str]:
    script = (
        "import os;"
        f"os.write(1, b'o' * {stdout_bytes});"
        f"os.write(2, b's' * {stderr_bytes})"
    )
    return [sys.executable, "-c", script]


def test_bounded_process_accepts_exact_stdout_limit_and_rejects_limit_plus_one() -> None:
    limit = 4096
    exact = ax_capture._run_bounded_process(
        _process_writing(stdout_bytes=limit), timeout=5, stdout_limit=limit
    )
    over = ax_capture._run_bounded_process(
        _process_writing(stdout_bytes=limit + 1), timeout=5, stdout_limit=limit
    )

    assert exact.returncode == 0
    assert exact.stdout == b"o" * limit
    assert exact.stdout_exceeded is False
    assert over.returncode == 0
    assert over.stdout == b""
    assert over.stdout_exceeded is True


def test_bounded_process_drains_but_never_retains_oversized_stderr() -> None:
    exact = ax_capture._run_bounded_process(
        _process_writing(stderr_bytes=4096),
        timeout=5,
        stdout_limit=2,
        stderr_limit=4096,
    )
    over = ax_capture._run_bounded_process(
        _process_writing(stdout_bytes=2, stderr_bytes=4097),
        timeout=5,
        stdout_limit=2,
        stderr_limit=4096,
    )

    assert exact.stderr_exceeded is False
    assert over.stdout == b"oo"
    assert over.stdout_exceeded is False
    assert over.stderr_exceeded is True
    assert "ssss" not in repr(over)


def test_bounded_process_kills_on_timeout() -> None:
    result = ax_capture._run_bounded_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.02,
        stdout_limit=64,
    )

    assert result.timed_out is True
    assert result.stdout == b""


def test_bounded_process_does_not_wait_forever_for_inherited_pipe_fds() -> None:
    script = (
        "import subprocess,sys;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])"
    )
    started = time.monotonic()
    result = ax_capture._run_bounded_process(
        [sys.executable, "-c", script], timeout=5, stdout_limit=64
    )

    assert time.monotonic() - started < 2
    assert result.output_incomplete is True
    assert result.stdout == b""


@pytest.mark.parametrize(
    "field", ["stdout_exceeded", "stderr_exceeded", "output_incomplete"]
)
def test_ax_provider_rejects_any_native_output_overrun(
    monkeypatch, field: str
) -> None:
    result = ax_capture._BoundedProcessResult(
        returncode=0,
        stdout=b'{"timestamp":"now","apps":[]}',
        **{field: True},
    )
    monkeypatch.setattr(ax_capture, "_run_bounded_process", lambda *_a, **_k: result)
    provider = ax_capture.MacAXHelperProvider(
        helper_path=Path("/private/tmp/helper"), depth=8, timeout=3
    )

    assert provider.capture_frontmost() is None


@pytest.mark.parametrize(
    "field", ["stdout_exceeded", "stderr_exceeded", "timed_out", "output_incomplete"]
)
def test_metadata_and_screenshot_reject_bounded_process_failure(
    monkeypatch, field: str
) -> None:
    result = ax_capture._BoundedProcessResult(
        returncode=0,
        stdout=b"{}",
        **{field: True},
    )
    monkeypatch.setattr(window_meta.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(screenshot.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(window_meta, "_helper_path", lambda: "/safe/helper")
    monkeypatch.setattr(screenshot, "_helper_path", lambda: "/safe/helper")
    monkeypatch.setattr(window_meta, "_run_bounded_process", lambda *_a, **_k: result)
    monkeypatch.setattr(screenshot, "_run_bounded_process", lambda *_a, **_k: result)

    assert window_meta.active_window() == window_meta.WindowMeta()
    assert screenshot.grab(target=_target()) is None


def test_identity_string_and_geometry_limits_fail_closed() -> None:
    target = _target()
    too_long = window_meta.WindowMeta(
        app_name="a" * (window_meta._MAX_IDENTITY_STRING_BYTES + 1),
        title=target.title,
        bundle_id=target.bundle_id,
        pid=target.pid,
        window_id=target.window_id,
        bounds=target.bounds,
    )
    oversized_geometry = window_meta.WindowMeta(
        app_name=target.app_name,
        title=target.title,
        bundle_id=target.bundle_id,
        pid=target.pid,
        window_id=target.window_id,
        bounds=window_meta.WindowBounds(x=0, y=0, width=8000, height=5001),
    )

    assert too_long.capture_ready is False
    assert too_long.to_capture_request() is None
    assert oversized_geometry.capture_ready is False


def test_screenshot_pixel_limit_accepts_exact_area_and_rejects_plus_one(
    monkeypatch,
) -> None:
    target = _target()
    request = target.to_capture_request()
    assert request is not None
    monkeypatch.setattr(screenshot.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(screenshot, "_helper_path", lambda: "/safe/helper")
    monkeypatch.setattr(
        screenshot,
        "_decode_verified_jpeg",
        lambda encoded, **_kwargs: encoded if isinstance(encoded, str) else None,
    )

    def payload(height: int) -> ax_capture._BoundedProcessResult:
        return ax_capture._BoundedProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "mime_type": "image/jpeg",
                    "image_base64": "AA==",
                    "width": 4000,
                    "height": height,
                    "window_meta": request,
                }
            ).encode(),
        )

    responses = iter([payload(3000), payload(3001)])
    monkeypatch.setattr(
        screenshot, "_run_bounded_process", lambda *_a, **_k: next(responses)
    )

    assert screenshot.grab(target=target, max_width=4096) is not None
    assert screenshot.grab(target=target, max_width=4096) is None


def test_base64_and_jpeg_byte_limits_are_checked_before_image_decode(monkeypatch) -> None:
    decoded: list[int] = []

    def fake_decode(_encoded, *, validate):
        assert validate is True
        size = screenshot._MAX_JPEG_BYTES + len(decoded)
        decoded.append(size)
        return b"x" * size

    monkeypatch.setattr(screenshot.base64, "b64decode", fake_decode)

    assert screenshot._decode_verified_jpeg("AAAA", width=1, height=1) is None
    assert screenshot._decode_verified_jpeg("AAAA", width=1, height=1) is None
    assert decoded == [screenshot._MAX_JPEG_BYTES, screenshot._MAX_JPEG_BYTES + 1]

    base64_calls: list[str] = []

    def small_decode(encoded, *, validate):
        assert validate is True
        base64_calls.append(encoded)
        return b"not-a-jpeg"

    monkeypatch.setattr(screenshot, "_MAX_BASE64_CHARACTERS", 4)
    monkeypatch.setattr(screenshot.base64, "b64decode", small_decode)
    assert screenshot._decode_verified_jpeg("AAAA", width=1, height=1) is None
    assert screenshot._decode_verified_jpeg("AAAAA", width=1, height=1) is None
    assert base64_calls == ["AAAA"]


def test_pillow_decompression_bomb_is_a_fail_closed_screenshot(monkeypatch) -> None:
    from PIL import Image

    monkeypatch.setattr(
        screenshot.base64,
        "b64decode",
        lambda *_args, **_kwargs: b"bounded-jpeg-bytes",
    )
    monkeypatch.setattr(
        Image,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            Image.DecompressionBombError("crafted dimensions")
        ),
    )

    assert screenshot._decode_verified_jpeg("AAAA", width=1, height=1) is None


def test_swift_helper_declares_non_truncating_global_limits() -> None:
    source = (
        Path(__file__).parents[1] / "resources" / "mac-ax-helper.swift"
    ).read_text()

    assert "maximumTraversalNodes = 20_000" in source
    assert "maximumCapturedStringBytes = 4 * 1024 * 1024" in source
    assert "maximumAXJSONBytes = 8 * 1024 * 1024" in source
    assert "throw CaptureLimitError.exceeded" in source
    assert 'case "--require-complete-tree"' in source
    assert "if config.requireCompleteTree { throw CaptureLimitError.exceeded }" in source
    assert "if !config.raw, !config.requireCompleteTree," in source
    assert (
        'if !config.raw && !config.requireCompleteTree && role == "AXGroup"'
        in source
    )
    strict_return = source.index("if config.requireCompleteTree {\n        return AXNode(")
    text_role_filter = source.index("if let role = role, textBearingRoles.contains(role)")
    container_filter = source.index("if let role = role, containerRoles.contains(role)")
    assert strict_return < text_role_filter
    assert strict_return < container_filter
    assert "AX capture resource limit exceeded" in source
    assert "String(v.prefix" not in source
    assert "maximumScreenshotWidth = 4096" in source
    assert "maximumSourceImageArea = 12_000_000" in source
    assert "maximumOutputImageArea = 12_000_000" in source
    assert "maximumJPEGBytes = 12 * 1024 * 1024" in source
    assert ".nominalResolution" in source
    assert ".bestResolution" not in source
    assert "readDataToEndOfFile" not in source


def test_swift_complete_tree_rejects_non_benign_ax_read_errors() -> None:
    source = (
        Path(__file__).parents[1] / "resources" / "mac-ax-helper.swift"
    ).read_text()

    assert "private enum AXCaptureReadError: Error" in source
    assert "case .noValue, .attributeUnsupported:" in source
    assert "throw AXCaptureReadError.unexpectedAXError(error)" in source
    assert "requireCompleteTree: config.requireCompleteTree" in source
    assert "let childElements = try axCaptureChildren(element, config: config)" in source
    assert "let children = try axCaptureChildren(window, config: config)" in source
    assert "let role = try axCaptureString(" in source
    assert "if !timedOut, let childrenReadError" in source
    assert "throw childrenReadError" in source
    assert "AX capture attribute read failed." in source

    process_app = source[source.index("private func processApp(") :]
    direct_copy = "AXUIElementCopyAttributeValue(\n        appRef"
    assert direct_copy not in process_app


def test_swift_ax_tree_completeness_receipt_is_versioned_and_tree_only() -> None:
    source = (
        Path(__file__).parents[1] / "resources" / "mac-ax-helper.swift"
    ).read_text()

    assert "private let axCaptureSchemaVersion = 1" in source
    assert "private let resourceLimitsVersion = 1" in source
    assert 'output["ax_capture_schema_version"] = axCaptureSchemaVersion' in source
    assert 'output["tree_complete"] = true' in source
    assert 'output["focused_window_only"] = config.focusedWindowOnly' in source
    assert 'output["effective_max_depth"] = effectiveMaxDepth' in source
    assert 'output["resource_limits_version"] = resourceLimitsVersion' in source
    assert "if budget.treeComplete && !appDicts.isEmpty" in source
    assert "budget.markIncomplete()" in source
    assert source.count('"ax_capture_schema_version"') == 1
    assert source.count('"tree_complete"') == 1

    metadata_return = source.index("case .frontmostWindowMetadata:")
    screenshot_return = source.index("case .captureFrontmostWindow:")
    receipt = source.index('output["ax_capture_schema_version"]')
    assert metadata_return < receipt
    assert screenshot_return < receipt


@pytest.mark.skipif(sys.platform != "darwin", reason="native Swift helper is macOS-only")
def test_compiled_swift_helper_rejects_unknown_cli_argument(tmp_path: Path) -> None:
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        pytest.skip("swiftc unavailable")
    source = Path(__file__).parents[1] / "resources" / "mac-ax-helper.swift"
    helper = tmp_path / "mac-ax-helper"
    target = f"{platform.machine()}-apple-macos12.0"
    subprocess.run(
        [
            swiftc,
            str(source),
            "-o",
            str(helper),
            "-target",
            target,
            "-swift-version",
            "5",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )

    result = subprocess.run(
        [str(helper), "--not-a-real-option"],
        check=False,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert result.stdout == b""
    assert result.stderr == b"Unknown argument.\n"


def test_swift_watcher_bounds_identity_strings_and_jsonl_frames() -> None:
    source = (
        Path(__file__).parents[1] / "resources" / "mac-ax-watcher.swift"
    ).read_text()

    assert "let kMaxEventBytes = 32 * 1024" in source
    assert "let kMaxIdentityCharacters = 512" in source
    assert "data.count <= kMaxEventBytes" in source
    assert 'let role = truncate(axRole(el) ?? "", 200)' in source
    assert 'let subrole = truncate(axSubrole(el) ?? "", 200)' in source
    assert "truncate(app.localizedName" in source
    assert "truncate(app.bundleIdentifier" in source
    assert "return truncate(" in source


def test_screenshot_stdout_cap_covers_bounded_base64_json() -> None:
    assert screenshot._MAX_CONFIGURED_WIDTH == 4096
    assert screenshot._MAX_IMAGE_AREA == 12_000_000
    assert screenshot._MAX_BASE64_CHARACTERS == 4 * (
        (screenshot._MAX_JPEG_BYTES + 2) // 3
    )
    assert screenshot._SCREENSHOT_STDOUT_LIMIT_BYTES > screenshot._MAX_BASE64_CHARACTERS
