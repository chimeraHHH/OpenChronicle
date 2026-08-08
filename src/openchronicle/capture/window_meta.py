"""Fail-closed metadata for the currently focused macOS window.

The native helper joins Accessibility's focused-window signal to the ordered
CoreGraphics window list.  That gives screenshot capture a concrete window
number and bounds instead of a title-only hint.  There is deliberately no
AppleScript or first-window fallback: an incomplete identity is not safe to
use as a pixel-capture target.
"""

from __future__ import annotations

import json
import math
import platform
from dataclasses import dataclass
from typing import Any

from ..logger import get
from .ax_capture import _run_bounded_process

logger = get("openchronicle.capture")

_HELPER_TIMEOUT_SECONDS = 5
_METADATA_STDOUT_LIMIT_BYTES = 64 * 1024 + 1  # native JSON cap plus trailing newline
_SCHEMA_VERSION = 1
_MAX_BOUND_MAGNITUDE = 10_000_000.0
_MAX_BOUND_SIDE = 12_288.0
_MAX_BOUND_AREA = 40_000_000.0
_MAX_IDENTITY_STRING_BYTES = 16 * 1024
_MAX_IDENTITY_TOTAL_BYTES = 20 * 1024


def _bounded_utf8_size(value: str, *, limit: int) -> int | None:
    # UTF-8 uses at least one byte per scalar, so the character check avoids
    # allocating an encoded copy of an already-obviously-oversized value.
    if len(value) > limit:
        return None
    size = len(value.encode("utf-8"))
    return size if size <= limit else None


@dataclass(frozen=True, slots=True)
class WindowBounds:
    """CoreGraphics global-coordinate bounds, in logical screen points."""

    x: float
    y: float
    width: float
    height: float

    @property
    def valid(self) -> bool:
        values = (self.x, self.y, self.width, self.height)
        return (
            all(math.isfinite(value) for value in values)
            and self.width > 0
            and self.height > 0
            and self.width <= _MAX_BOUND_SIDE
            and self.height <= _MAX_BOUND_SIDE
            and self.width * self.height <= _MAX_BOUND_AREA
            and all(abs(value) <= _MAX_BOUND_MAGNITUDE for value in values)
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class WindowMeta:
    app_name: str = ""
    title: str = ""
    bundle_id: str = ""
    pid: int | None = None
    window_id: int | None = None
    bounds: WindowBounds | None = None

    @property
    def capture_ready(self) -> bool:
        """Whether this value can safely identify one screenshot target."""
        identity_strings = (self.app_name, self.bundle_id, self.title)
        identity_sizes = (
            tuple(
                _bounded_utf8_size(value, limit=_MAX_IDENTITY_STRING_BYTES)
                for value in identity_strings
            )
            if all(isinstance(value, str) for value in identity_strings)
            else ()
        )
        return (
            isinstance(self.pid, int)
            and not isinstance(self.pid, bool)
            and self.pid > 0
            and isinstance(self.window_id, int)
            and not isinstance(self.window_id, bool)
            and self.window_id > 0
            and isinstance(self.bounds, WindowBounds)
            and self.bounds.valid
            and bool(self.app_name)
            and bool(self.bundle_id)
            and len(identity_sizes) == len(identity_strings)
            and all(size is not None for size in identity_sizes)
            and sum(size for size in identity_sizes if size is not None)
            <= _MAX_IDENTITY_TOTAL_BYTES
        )

    def same_capture_target(self, other: WindowMeta) -> bool:
        """Compare every field that the native capture helper fences."""
        return (
            self.capture_ready
            and other.capture_ready
            and self.pid == other.pid
            and self.window_id == other.window_id
            and self.bounds == other.bounds
            and self.app_name == other.app_name
            and self.bundle_id == other.bundle_id
            and self.title == other.title
        )

    def to_capture_request(self) -> dict[str, Any] | None:
        if not self.capture_ready or self.bounds is None:
            return None
        return {
            "schema_version": _SCHEMA_VERSION,
            "pid": self.pid,
            "window_id": self.window_id,
            "bounds": self.bounds.to_dict(),
            "app_name": self.app_name,
            "bundle_id": self.bundle_id,
            "title": self.title,
        }


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def parse_window_meta(value: object) -> WindowMeta | None:
    """Strictly parse the helper's versioned window identity object."""
    if not isinstance(value, dict) or value.get("schema_version") != _SCHEMA_VERSION:
        return None

    app_name = value.get("app_name")
    title = value.get("title")
    bundle_id = value.get("bundle_id")
    if not all(isinstance(field, str) for field in (app_name, title, bundle_id)):
        return None

    pid = _positive_int(value.get("pid"))
    window_id = _positive_int(value.get("window_id"))
    raw_bounds = value.get("bounds")
    if pid is None or window_id is None or not isinstance(raw_bounds, dict):
        return None

    coordinates = {
        key: _finite_number(raw_bounds.get(key)) for key in ("x", "y", "width", "height")
    }
    if any(coordinate is None for coordinate in coordinates.values()):
        return None

    bounds = WindowBounds(
        x=coordinates["x"],  # type: ignore[arg-type]
        y=coordinates["y"],  # type: ignore[arg-type]
        width=coordinates["width"],  # type: ignore[arg-type]
        height=coordinates["height"],  # type: ignore[arg-type]
    )
    meta = WindowMeta(
        app_name=app_name,
        title=title,
        bundle_id=bundle_id,
        pid=pid,
        window_id=window_id,
        bounds=bounds,
    )
    return meta if meta.capture_ready else None


def _helper_path() -> str | None:
    # Keep binary discovery/build behavior identical to AX capture.  Importing
    # lazily avoids making this lightweight value module part of a cycle.
    from .ax_capture import _resolve_helper_path

    path = _resolve_helper_path()
    return str(path) if path is not None else None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def active_window() -> WindowMeta:
    """Return a reviewable focused-window identity, or an empty value.

    Any helper failure, permission denial, malformed payload, ambiguous AX/CG
    match, or invalid geometry fails closed.  Callers can use ``capture_ready``
    to distinguish an identity that is safe for exact-window screenshots.
    """
    if platform.system() != "Darwin":
        return WindowMeta()

    helper = _helper_path()
    if helper is None:
        logger.warning("mac-ax-helper unavailable for active-window metadata")
        return WindowMeta()

    try:
        proc = _run_bounded_process(
            [helper, "--frontmost-window-metadata"],
            timeout=_HELPER_TIMEOUT_SECONDS,
            stdout_limit=_METADATA_STDOUT_LIMIT_BYTES,
        )
    except OSError as exc:
        logger.warning("active-window helper failed: %s", type(exc).__name__)
        return WindowMeta()

    if proc.timed_out:
        logger.warning("active-window helper timed out")
        return WindowMeta()
    if proc.stdout_exceeded or proc.stderr_exceeded or proc.output_incomplete:
        logger.warning("active-window helper exceeded a native output limit")
        return WindowMeta()
    if proc.returncode != 0:
        # stderr can contain OS/application details; never copy it into logs.
        logger.debug("active-window helper exited with status %d", proc.returncode)
        return WindowMeta()

    try:
        payload = json.loads(proc.stdout, parse_constant=_reject_json_constant)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        RecursionError,
    ):
        logger.warning("active-window helper returned invalid JSON")
        return WindowMeta()

    meta = parse_window_meta(payload)
    if meta is None:
        logger.warning("active-window helper returned an invalid or incomplete identity")
        return WindowMeta()
    return meta
