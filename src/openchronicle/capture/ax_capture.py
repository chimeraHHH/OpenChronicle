"""Cross-platform-stub AX Tree capture (macOS only in v1).

Wraps the vendored `mac-ax-helper` Swift binary. Ported from Einsia-Partner's
backend/core/capture/ax_capture_service.py with Windows branch removed and
resource resolution adapted for a uv/pip-installable package.
"""

from __future__ import annotations

import json
import os
import platform
import selectors
import signal
import subprocess
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..logger import get
from .ax_models import AXCaptureResult

logger = get("openchronicle.capture")

_SUBPROCESS_TIMEOUT = 10  # seconds (covers --timeout 3 + overhead)
_AX_STDOUT_LIMIT_BYTES = 8 * 1024 * 1024 + 1  # native JSON cap plus its trailing newline
_HELPER_STDERR_LIMIT_BYTES = 64 * 1024
_PIPE_EOF_GRACE_SECONDS = 0.25
_AX_CAPTURE_SCHEMA_VERSION = 1
_AX_RESOURCE_LIMITS_VERSION = 1
_AX_HARD_MAX_DEPTH = 128
_AX_HELPER_DEFAULT_DEPTH = 100
_AX_RECEIPT_KEYS = frozenset(
    {
        "ax_capture_schema_version",
        "tree_complete",
        "focused_window_only",
        "effective_max_depth",
        "resource_limits_version",
    }
)


def _expected_effective_depth(configured_depth: object) -> int | None:
    if isinstance(configured_depth, bool) or not isinstance(configured_depth, int):
        return None
    return min(
        configured_depth if configured_depth > 0 else _AX_HELPER_DEFAULT_DEPTH,
        _AX_HARD_MAX_DEPTH,
    )


@dataclass(frozen=True, slots=True)
class _BoundedProcessResult:
    """A native-process result that never retains unbounded pipe contents."""

    returncode: int
    stdout: bytes
    stdout_exceeded: bool = False
    stderr_exceeded: bool = False
    timed_out: bool = False
    output_incomplete: bool = False


@dataclass(slots=True)
class _BoundedOutput:
    limit: int
    retain: bool
    data: bytearray = field(default_factory=bytearray)
    seen: int = 0
    exceeded: bool = False

    def consume(self, chunk: bytes) -> None:
        if self.exceeded:
            return
        remaining = self.limit - self.seen
        if len(chunk) > remaining:
            self.exceeded = True
            self.data.clear()
            return
        self.seen += len(chunk)
        if self.retain:
            self.data.extend(chunk)


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort kill for the helper and children inheriting its pipe FDs."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with suppress(ProcessLookupError):
            proc.kill()


def _run_bounded_process(
    args: Sequence[str],
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int = _HELPER_STDERR_LIMIT_BYTES,
    input_bytes: bytes | None = None,
) -> _BoundedProcessResult:
    """Run a helper with nonblocking, strictly bounded output pipes.

    A selector drains stdout and stderr concurrently without reader threads.
    This also avoids an unbounded ``join`` if a compromised helper forks a
    child that inherits the pipe descriptors.  Such a pipe is closed after a
    short EOF grace period and the complete result is rejected.
    """
    if stdout_limit < 0 or stderr_limit < 0:
        raise ValueError("process output limits must be non-negative")

    proc = subprocess.Popen(
        list(args),
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    selector = selectors.DefaultSelector()
    stdout = _BoundedOutput(limit=stdout_limit, retain=True)
    stderr = _BoundedOutput(limit=stderr_limit, retain=False)
    input_offset = 0
    pipe_error = False
    for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    if input_bytes is not None:
        assert proc.stdin is not None
        os.set_blocking(proc.stdin.fileno(), False)
        selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")

    def close_stream(stream: Any) -> None:
        with suppress(KeyError, ValueError):
            selector.unregister(stream)
        with suppress(OSError):
            stream.close()

    deadline = time.monotonic() + timeout
    exit_seen_at: float | None = None
    timed_out = False
    while proc.poll() is None or selector.get_map():
        now = time.monotonic()
        if proc.poll() is None and now >= deadline:
            timed_out = True
            _kill_process_group(proc)
            proc.wait()
        if proc.poll() is not None and exit_seen_at is None:
            exit_seen_at = now
        if exit_seen_at is not None and now - exit_seen_at >= _PIPE_EOF_GRACE_SECONDS:
            pipe_error = bool(selector.get_map())
            if pipe_error:
                _kill_process_group(proc)
            break

        wait = 0.05
        if proc.poll() is None:
            wait = max(0.0, min(wait, deadline - now))
        elif exit_seen_at is not None:
            wait = max(0.0, min(wait, _PIPE_EOF_GRACE_SECONDS - (now - exit_seen_at)))
        if selector.get_map():
            events = selector.select(wait)
        else:
            time.sleep(wait)
            events = []
        for key, _mask in events:
            stream = key.fileobj
            try:
                if key.data == "stdin":
                    assert input_bytes is not None
                    written = os.write(stream.fileno(), input_bytes[input_offset:])
                    input_offset += written
                    if input_offset == len(input_bytes):
                        close_stream(stream)
                    continue

                chunk = os.read(stream.fileno(), 64 * 1024)
            except (BlockingIOError, InterruptedError):
                continue
            except (BrokenPipeError, OSError):
                pipe_error = True
                close_stream(stream)
                continue
            if not chunk:
                close_stream(stream)
            elif key.data == "stdout":
                stdout.consume(chunk)
            else:
                stderr.consume(chunk)

    for key in list(selector.get_map().values()):
        close_stream(key.fileobj)
    selector.close()
    if proc.poll() is None:
        _kill_process_group(proc)
    returncode = proc.wait()
    return _BoundedProcessResult(
        returncode=returncode,
        stdout=bytes(stdout.data) if not pipe_error else b"",
        stdout_exceeded=stdout.exceeded,
        stderr_exceeded=stderr.exceeded,
        timed_out=timed_out,
        output_incomplete=pipe_error,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _strip_frame_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_frame_fields(v) for k, v in value.items() if k != "frame"}
    if isinstance(value, list):
        return [_strip_frame_fields(item) for item in value]
    return value


def _consume_tree_receipt(
    data: dict[str, Any],
    *,
    focused_window_only: bool,
    configured_depth: int,
    required: bool,
) -> tuple[dict[str, Any], bool, int | None] | None:
    """Validate and remove the native AX completeness receipt.

    Old helpers are tolerated only for non-strict callers. Once URL policy
    requests a complete tree, absence, partial presence, mismatched invocation
    fields, or an unknown protocol version rejects the response before its AX
    content reaches the scheduler.
    """
    present = _AX_RECEIPT_KEYS.intersection(data)
    if not present:
        if required:
            return None
        return data, False, None
    if present != _AX_RECEIPT_KEYS:
        return None

    expected_depth = _expected_effective_depth(configured_depth)
    depth = data.get("effective_max_depth")
    schema_version = data.get("ax_capture_schema_version")
    limits_version = data.get("resource_limits_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != _AX_CAPTURE_SCHEMA_VERSION
        or data.get("tree_complete") is not True
        or data.get("focused_window_only") is not focused_window_only
        or isinstance(depth, bool)
        or not isinstance(depth, int)
        or expected_depth is None
        or depth != expected_depth
        or isinstance(limits_version, bool)
        or not isinstance(limits_version, int)
        or limits_version != _AX_RESOURCE_LIMITS_VERSION
    ):
        return None

    cleaned = {key: value for key, value in data.items() if key not in _AX_RECEIPT_KEYS}
    return cleaned, True, depth


def _maybe_compile(swift_path: Path, binary_path: Path) -> None:
    """Dev/first-run: compile the helper if missing or stale."""
    if not swift_path.is_file():
        return
    if binary_path.is_file():
        if binary_path.stat().st_mtime >= swift_path.stat().st_mtime:
            return
        logger.info("mac-ax-helper: source newer than binary, recompiling")
    else:
        logger.info("mac-ax-helper: binary missing, compiling from source")

    cache = Path("/tmp/clang-module-cache")
    cache.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CLANG_MODULE_CACHE_PATH"] = str(cache)
    arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "x86_64"
    target = f"{arch}-apple-macos12.0"
    try:
        result = subprocess.run(
            [
                "swiftc",
                str(swift_path),
                "-o",
                str(binary_path),
                "-O",
                "-target",
                target,
                "-swift-version",
                "5",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("mac-ax-helper compile failed: %s (install Xcode CLT?)", exc)
        return
    if result.returncode != 0:
        logger.warning(
            "mac-ax-helper compile failed (%d): %s",
            result.returncode,
            result.stderr.strip()[:300],
        )


def _resolve_helper_path() -> Path | None:
    """Find or build the mac-ax-helper binary.

    Search order:
      1. OPENCHRONICLE_AX_HELPER env var (absolute path)
      2. Packaged resource shipped with the wheel (_bundled/)
      3. Dev source tree (../../../resources/ relative to this file)
    """
    if platform.system() != "Darwin":
        return None

    override = os.environ.get("OPENCHRONICLE_AX_HELPER")
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p
        logger.warning("OPENCHRONICLE_AX_HELPER set but not executable: %s", p)

    candidates: list[Path] = []

    # 1. Bundled inside the installed package (wheel ships .swift; binary built on demand)
    try:
        from importlib.resources import files as _pkg_files

        bundled_dir = Path(str(_pkg_files("openchronicle").joinpath("_bundled")))
        candidates.append(bundled_dir / "mac-ax-helper")
    except (ModuleNotFoundError, ValueError):
        pass

    # 2. Dev source tree
    dev_root = Path(__file__).resolve().parents[3]  # .../OpenChronicle/
    candidates.append(dev_root / "resources" / "mac-ax-helper")

    for binary_path in candidates:
        swift_path = binary_path.with_suffix(".swift")
        if swift_path.is_file():
            _maybe_compile(swift_path, binary_path)
        if binary_path.is_file() and os.access(binary_path, os.X_OK):
            return binary_path

    return None


class AXProvider(Protocol):
    @property
    def available(self) -> bool: ...

    def capture_frontmost(
        self,
        *,
        focused_window_only: bool = True,
        require_complete_tree: bool = False,
    ) -> AXCaptureResult | None: ...

    def capture_all_visible(self) -> AXCaptureResult | None: ...

    def capture_app(
        self, app_name: str, *, focused_window_only: bool = True
    ) -> AXCaptureResult | None: ...


class UnavailableAXProvider:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    def capture_frontmost(
        self,
        *,
        focused_window_only: bool = True,
        require_complete_tree: bool = False,
    ) -> AXCaptureResult | None:
        return None

    def capture_all_visible(self) -> AXCaptureResult | None:
        return None

    def capture_app(
        self, app_name: str, *, focused_window_only: bool = True
    ) -> AXCaptureResult | None:
        return None


class MacAXHelperProvider:
    """Subprocess wrapper around the vendored mac-ax-helper Swift binary."""

    def __init__(self, *, helper_path: Path, depth: int, timeout: int, raw: bool = False) -> None:
        self._helper_path = str(helper_path)
        self._depth = depth
        self._timeout = timeout
        self._raw = raw

    @property
    def available(self) -> bool:
        return True

    def capture_frontmost(
        self,
        *,
        focused_window_only: bool = True,
        require_complete_tree: bool = False,
    ) -> AXCaptureResult | None:
        return self._run(
            all_visible=False,
            focused_window_only=focused_window_only,
            require_complete_tree=require_complete_tree,
        )

    def capture_all_visible(self) -> AXCaptureResult | None:
        return self._run(all_visible=True)

    def capture_app(
        self, app_name: str, *, focused_window_only: bool = True
    ) -> AXCaptureResult | None:
        return self._run(
            all_visible=False, app_name=app_name, focused_window_only=focused_window_only
        )

    def _run(
        self,
        *,
        all_visible: bool,
        app_name: str | None = None,
        focused_window_only: bool = False,
        require_complete_tree: bool = False,
    ) -> AXCaptureResult | None:
        args: list[str] = [self._helper_path]
        if app_name:
            args.extend(["--app-name", app_name])
        elif all_visible:
            args.append("--all-visible")
        if focused_window_only:
            args.append("--focused-window-only")
        if require_complete_tree:
            args.append("--require-complete-tree")
        if self._raw:
            args.append("--raw")
        if self._depth > 0:
            args.extend(["--depth", str(self._depth)])
        args.extend(["--timeout", str(self._timeout)])

        try:
            proc = _run_bounded_process(
                args,
                timeout=_SUBPROCESS_TIMEOUT,
                stdout_limit=_AX_STDOUT_LIMIT_BYTES,
            )
        except OSError as exc:
            logger.error("Failed to run mac-ax-helper: %s", type(exc).__name__)
            return None

        if proc.timed_out:
            logger.warning("mac-ax-helper timed out after %ds", _SUBPROCESS_TIMEOUT)
            return None
        if proc.stdout_exceeded or proc.stderr_exceeded or proc.output_incomplete:
            logger.warning("mac-ax-helper exceeded a native output limit")
            return None

        if proc.returncode == 2:
            logger.warning(
                "Accessibility permission not granted. "
                "Grant access to your terminal in System Settings → Privacy & Security → Accessibility."
            )
            return None
        if proc.returncode != 0:
            # A failed native helper must not turn arbitrary stderr (which may
            # contain a window title or AX value) into a secondary data sink.
            logger.warning("mac-ax-helper exited with status %d", proc.returncode)
            return None

        try:
            data = json.loads(proc.stdout, parse_constant=_reject_json_constant)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValueError,
            TypeError,
            RecursionError,
        ):
            logger.warning("mac-ax-helper returned invalid JSON")
            return None

        if not isinstance(data, dict):
            logger.warning("mac-ax-helper returned an invalid payload")
            return None

        receipt = _consume_tree_receipt(
            data,
            focused_window_only=focused_window_only,
            configured_depth=self._depth,
            required=require_complete_tree,
        )
        if receipt is None:
            logger.warning("mac-ax-helper returned an invalid completeness receipt")
            return None
        data, tree_complete_verified, effective_max_depth = receipt

        try:
            data = _strip_frame_fields(data)
        except RecursionError:
            logger.warning("mac-ax-helper returned an invalid payload")
            return None
        mode = "all-visible" if all_visible else "frontmost"
        return AXCaptureResult(
            raw_json=data,
            timestamp=data.get("timestamp", ""),
            apps=data.get("apps", []),
            metadata={"mode": mode, "depth": self._depth, "platform": "macos", "raw": self._raw},
            tree_complete_verified=tree_complete_verified,
            effective_max_depth=effective_max_depth,
        )


def create_provider(*, depth: int = 100, timeout: int = 3, raw: bool = False) -> AXProvider:
    if platform.system() != "Darwin":
        return UnavailableAXProvider(f"unsupported platform: {platform.system()}")
    helper = _resolve_helper_path()
    if helper is None:
        return UnavailableAXProvider(
            "mac-ax-helper not found. Build it: bash resources/build-mac-ax-helper.sh"
        )
    logger.info("AX capture initialized: %s", helper)
    return MacAXHelperProvider(helper_path=helper, depth=depth, timeout=timeout, raw=raw)
