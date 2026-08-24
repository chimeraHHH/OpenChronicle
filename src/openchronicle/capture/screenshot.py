"""Exact-frontmost-window screenshots through the native macOS helper.

This module intentionally has no monitor-capture or bounds-crop fallback.
CoreGraphics captures only the requested CGWindowID; both Python and the
helper verify the complete target identity before accepting any pixels.
"""

from __future__ import annotations

import base64
import io
import json
import platform
from dataclasses import dataclass

from ..logger import get
from .ax_capture import _run_bounded_process
from .window_meta import WindowMeta, parse_window_meta

logger = get("openchronicle.capture")

_HELPER_TIMEOUT_SECONDS = 15
_CAPTURE_SCHEMA_VERSION = 1
_MAX_CONFIGURED_WIDTH = 4096
_MAX_SOURCE_BOUND_SIDE = 8192
_MAX_SOURCE_BOUND_AREA = 12_000_000
_MAX_IMAGE_SIDE = 8192
_MAX_IMAGE_AREA = 12_000_000
_MAX_JPEG_BYTES = 12 * 1024 * 1024
_MAX_BASE64_CHARACTERS = 4 * ((_MAX_JPEG_BYTES + 2) // 3)
_SCREENSHOT_STDOUT_LIMIT_BYTES = 17 * 1024 * 1024 + 1  # native JSON cap plus newline


@dataclass(frozen=True, slots=True)
class Screenshot:
    image_base64: str
    mime_type: str = "image/jpeg"
    width: int = 0
    height: int = 0
    window_meta: WindowMeta = WindowMeta()


def _helper_path() -> str | None:
    from .ax_capture import _resolve_helper_path

    path = _resolve_helper_path()
    return str(path) if path is not None else None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _decode_verified_jpeg(encoded: object, *, width: int, height: int) -> str | None:
    if (
        not isinstance(encoded, str)
        or not encoded
        or len(encoded) > _MAX_BASE64_CHARACTERS
    ):
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None

    if len(raw) > _MAX_JPEG_BYTES:
        return None

    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed; exact-window screenshot disabled")
        return None

    try:
        with Image.open(io.BytesIO(raw)) as image:
            if (
                image.format != "JPEG"
                or image.size != (width, height)
                or image.width > _MAX_IMAGE_SIDE
                or image.height > _MAX_IMAGE_SIDE
                or image.width * image.height > _MAX_IMAGE_AREA
            ):
                return None
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "JPEG" or image.size != (width, height):
                return None
            image.load()
    except (
        OSError,
        ValueError,
        SyntaxError,
        OverflowError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None
    return encoded


def grab(
    *,
    target: WindowMeta | None = None,
    max_width: int = 1920,
    jpeg_quality: int = 80,
) -> Screenshot | None:
    """Capture exactly ``target`` if it is still the focused macOS window.

    The helper receives the expected identity on stdin, checks frontmost PID,
    CGWindowID, title and bounds immediately before and after the CoreGraphics
    call, and returns a JPEG plus the final identity.  Every failure is a
    ``None``; this function never falls back to a display screenshot.
    """
    if platform.system() != "Darwin":
        return None
    if target is None:
        logger.warning("screenshot skipped: no exact window target")
        return None
    request = target.to_capture_request()
    if request is None:
        logger.warning("screenshot skipped: incomplete window target")
        return None
    assert target.bounds is not None
    if (
        target.bounds.width > _MAX_SOURCE_BOUND_SIDE
        or target.bounds.height > _MAX_SOURCE_BOUND_SIDE
        or target.bounds.width * target.bounds.height > _MAX_SOURCE_BOUND_AREA
    ):
        logger.warning("screenshot skipped: source window exceeds pixel safety bounds")
        return None
    if (
        isinstance(max_width, bool)
        or not isinstance(max_width, int)
        or not 1 <= max_width <= _MAX_CONFIGURED_WIDTH
    ):
        logger.warning("screenshot skipped: invalid max width")
        return None
    if (
        isinstance(jpeg_quality, bool)
        or not isinstance(jpeg_quality, int)
        or not 1 <= jpeg_quality <= 100
    ):
        logger.warning("screenshot skipped: invalid JPEG quality")
        return None

    helper = _helper_path()
    if helper is None:
        logger.warning("mac-ax-helper unavailable for exact-window screenshot")
        return None

    try:
        proc = _run_bounded_process(
            [
                helper,
                "--capture-frontmost-window",
                "--max-width",
                str(max_width),
                "--jpeg-quality",
                str(jpeg_quality),
            ],
            input_bytes=json.dumps(
                request, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8"),
            timeout=_HELPER_TIMEOUT_SECONDS,
            stdout_limit=_SCREENSHOT_STDOUT_LIMIT_BYTES,
        )
    except OSError as exc:
        logger.warning("exact-window screenshot helper failed: %s", type(exc).__name__)
        return None

    if proc.timed_out:
        logger.warning("exact-window screenshot helper timed out")
        return None
    if proc.stdout_exceeded or proc.stderr_exceeded or proc.output_incomplete:
        logger.warning("exact-window screenshot helper exceeded a native output limit")
        return None
    if proc.returncode != 0:
        # Do not copy stderr: native errors must not leak a title or URL.
        logger.debug("exact-window screenshot helper exited with status %d", proc.returncode)
        return None

    try:
        payload = json.loads(proc.stdout, parse_constant=_reject_json_constant)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        RecursionError,
    ):
        logger.warning("exact-window screenshot helper returned invalid JSON")
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != _CAPTURE_SCHEMA_VERSION:
        return None
    if payload.get("mime_type") != "image/jpeg":
        return None

    width = _positive_int(payload.get("width"))
    height = _positive_int(payload.get("height"))
    final_meta = parse_window_meta(payload.get("window_meta"))
    if width is None or height is None or final_meta is None:
        return None
    if (
        width > max_width
        or width > _MAX_IMAGE_SIDE
        or height > _MAX_IMAGE_SIDE
        or width * height > _MAX_IMAGE_AREA
        or not target.same_capture_target(final_meta)
    ):
        logger.info("screenshot discarded: native target identity changed")
        return None

    encoded = _decode_verified_jpeg(payload.get("image_base64"), width=width, height=height)
    if encoded is None:
        logger.warning("exact-window screenshot helper returned invalid JPEG data")
        return None

    return Screenshot(
        image_base64=encoded,
        mime_type="image/jpeg",
        width=width,
        height=height,
        window_meta=final_meta,
    )
