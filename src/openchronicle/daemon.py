"""Top-level daemon: capture scheduler + timeline aggregator + session cutter.

The writer combines session-boundary callbacks with periodic reducer flushes,
classifier passes, and a lightweight retry loop for durable pending sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import errno
import fcntl
import hmac
import json
import os
import re
import secrets
import signal
import socket
import stat
import struct
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .capture import reconcile as capture_reconcile
from .capture import scheduler as capture_scheduler
from .config import Config
from .local_time import MonotonicWallClock
from .logger import get
from .services.memory import MemoryService
from .session import tick as session_tick
from .store import entries as entries_store
from .store import files as store_files
from .store import fts
from .timeline import tick as timeline_tick
from .writer import llm as llm_mod

logger = get("openchronicle.daemon")

_CONTROL_VERSION = 1
_CONTROL_MAX_BYTES = 4096
_CONTROL_IO_TIMEOUT_SECONDS = 1.0
_CONTROL_GENERATION_BYTES = 32
_CONTROL_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_SOCKET_RE = re.compile(r"^control-[0-9a-f]{24}\.sock$")
_CONTROL_METADATA_KEYS = frozenset(
    {"version", "generation", "socket_name", "socket_dev", "socket_ino"}
)
_CONTROL_REQUEST_KEYS = frozenset({"version", "operation", "generation", "request_nonce"})
_CONTROL_RESPONSE_KEYS = frozenset({"version", "ok", "generation", "request_nonce"})


class DaemonControlError(RuntimeError):
    """The authenticated local daemon control endpoint is unavailable."""


class _ControlSocketMissing(DaemonControlError):
    """The generation metadata exists, but its bound socket is absent."""


@dataclass(frozen=True, slots=True)
class _ControlMetadata:
    generation: str
    socket_name: str
    socket_dev: int
    socket_ino: int
    metadata_dev: int = field(default=0, compare=False, repr=False)
    metadata_ino: int = field(default=0, compare=False, repr=False)

    def payload(self) -> dict[str, object]:
        return {
            "version": _CONTROL_VERSION,
            "generation": self.generation,
            "socket_name": self.socket_name,
            "socket_dev": self.socket_dev,
            "socket_ino": self.socket_ino,
        }


@dataclass(slots=True)
class _ControlEndpoint:
    metadata: _ControlMetadata
    socket_path: Path
    server: asyncio.AbstractServer
    handler_tasks: set[asyncio.Task[None]]


def _canonical_json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _load_closed_json(raw: bytes, expected_keys: frozenset[str]) -> dict[str, Any]:
    if not raw or len(raw) > _CONTROL_MAX_BYTES:
        raise ValueError("control message size is invalid")
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("control message is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("control message schema is invalid")
    return value


def _valid_nonce(value: object) -> bool:
    return isinstance(value, str) and _CONTROL_NONCE_RE.fullmatch(value) is not None


def _socket_name_for_generation(generation: str) -> str:
    return f"control-{generation[:24]}.sock"


def _secure_control_dir(*, create: bool) -> Path:
    directory = paths.daemon_control_dir()
    if create:
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise DaemonControlError("cannot create private daemon control directory") from exc
    try:
        info = os.lstat(directory)
    except FileNotFoundError as exc:
        raise DaemonControlError("private daemon control directory is unavailable") from exc
    except OSError as exc:
        raise DaemonControlError("cannot inspect daemon control directory") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise DaemonControlError(
            "daemon control directory must be a user-owned, non-symlink 0700 directory"
        )
    return directory


def _metadata_from_value(
    value: dict[str, Any],
    *,
    metadata_dev: int = 0,
    metadata_ino: int = 0,
) -> _ControlMetadata:
    if (
        type(value.get("version")) is not int
        or value["version"] != _CONTROL_VERSION
        or not _valid_nonce(value.get("generation"))
        or not isinstance(value.get("socket_name"), str)
        or _CONTROL_SOCKET_RE.fullmatch(value["socket_name"]) is None
        or value["socket_name"] != _socket_name_for_generation(value["generation"])
        or type(value.get("socket_dev")) is not int
        or value["socket_dev"] < 0
        or type(value.get("socket_ino")) is not int
        or value["socket_ino"] <= 0
    ):
        raise DaemonControlError("daemon control metadata schema is invalid")
    return _ControlMetadata(
        generation=value["generation"],
        socket_name=value["socket_name"],
        socket_dev=value["socket_dev"],
        socket_ino=value["socket_ino"],
        metadata_dev=metadata_dev,
        metadata_ino=metadata_ino,
    )


def _read_control_metadata() -> _ControlMetadata | None:
    metadata_path = paths.daemon_control_metadata_file()
    try:
        before = os.lstat(metadata_path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DaemonControlError("cannot inspect daemon control metadata") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or before.st_size <= 0
        or before.st_size > _CONTROL_MAX_BYTES
    ):
        raise DaemonControlError(
            "daemon control metadata must be a user-owned, non-symlink 0600 file"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(metadata_path, flags)
    except OSError as exc:
        raise DaemonControlError("cannot open daemon control metadata safely") from exc
    try:
        try:
            current = os.fstat(fd)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_uid != os.getuid()
                or stat.S_IMODE(current.st_mode) != 0o600
                or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or current.st_size <= 0
                or current.st_size > _CONTROL_MAX_BYTES
            ):
                raise DaemonControlError("daemon control metadata changed during validation")
            raw = os.read(fd, _CONTROL_MAX_BYTES + 1)
            if len(raw) != current.st_size:
                raise DaemonControlError("daemon control metadata changed during read")
        except OSError as exc:
            raise DaemonControlError("cannot read daemon control metadata safely") from exc
    finally:
        with suppress(OSError):
            os.close(fd)
    try:
        value = _load_closed_json(raw, _CONTROL_METADATA_KEYS)
    except ValueError as exc:
        raise DaemonControlError("daemon control metadata schema is invalid") from exc
    if raw != _canonical_json_bytes(value):
        raise DaemonControlError("daemon control metadata encoding is not canonical")
    return _metadata_from_value(
        value,
        metadata_dev=current.st_dev,
        metadata_ino=current.st_ino,
    )


def _write_control_metadata(metadata: _ControlMetadata) -> None:
    metadata_path = paths.daemon_control_metadata_file()
    payload = _canonical_json_bytes(metadata.payload())
    if len(payload) > _CONTROL_MAX_BYTES:
        raise DaemonControlError("daemon control metadata exceeds its size limit")
    fd, temporary_name = tempfile.mkstemp(
        dir=metadata_path.parent,
        prefix=f".{metadata_path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short daemon control metadata write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary_path, metadata_path)
        with suppress(OSError):
            directory_fd = os.open(metadata_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        with suppress(OSError):
            temporary_path.unlink()
        raise
    persisted = _read_control_metadata()
    if persisted is None or not _same_control_generation(persisted, metadata):
        raise DaemonControlError("daemon control metadata publication failed")


def _same_control_generation(
    current: _ControlMetadata,
    expected: _ControlMetadata,
) -> bool:
    return bool(
        hmac.compare_digest(current.generation, expected.generation)
        and current.socket_name == expected.socket_name
        and current.socket_dev == expected.socket_dev
        and current.socket_ino == expected.socket_ino
    )


def _validated_control_socket(metadata: _ControlMetadata) -> Path:
    directory = _secure_control_dir(create=False)
    if metadata.socket_name != _socket_name_for_generation(metadata.generation):
        raise DaemonControlError("daemon control socket name is not generation-bound")
    socket_path = directory / metadata.socket_name
    if len(os.fsencode(socket_path)) + 1 > 100:
        raise DaemonControlError("daemon control socket path exceeds the safe local limit")
    try:
        info = os.lstat(socket_path)
    except FileNotFoundError as exc:
        raise _ControlSocketMissing("daemon control socket is unavailable") from exc
    except OSError as exc:
        raise DaemonControlError("cannot inspect daemon control socket") from exc
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or (info.st_dev, info.st_ino) != (metadata.socket_dev, metadata.socket_ino)
    ):
        raise DaemonControlError(
            "daemon control socket must match its user-owned 0600 generation metadata"
        )
    return socket_path


def _unlink_metadata_if_unchanged(metadata: _ControlMetadata) -> bool:
    metadata_path = paths.daemon_control_metadata_file()
    try:
        current = os.lstat(metadata_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if (current.st_dev, current.st_ino) != (
        metadata.metadata_dev,
        metadata.metadata_ino,
    ):
        return False
    try:
        metadata_path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _remove_control_artifacts(expected: _ControlMetadata) -> bool:
    """Remove only the socket/metadata still owned by ``expected``."""
    try:
        current = _read_control_metadata()
    except DaemonControlError:
        return False
    if (
        current is None
        or not _same_control_generation(current, expected)
        or (current.metadata_dev, current.metadata_ino)
        != (expected.metadata_dev, expected.metadata_ino)
    ):
        return False
    try:
        socket_path = _validated_control_socket(current)
    except _ControlSocketMissing:
        pass
    except DaemonControlError:
        return False
    else:
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
    return _unlink_metadata_if_unchanged(current)


def _cleanup_stale_control_endpoint() -> None:
    stale = _read_control_metadata()
    if stale is not None and not _remove_control_artifacts(stale):
        raise DaemonControlError(
            "stale daemon control endpoint is not safely bound to its metadata"
        )


def _control_peer_uid(peer_socket: Any) -> int | None:
    """Return the AF_UNIX peer uid on supported kernels, else fail closed."""
    try:
        if sys.platform == "darwin":
            option = getattr(socket, "LOCAL_PEERCRED", None)
            if option is None:
                return None
            raw = peer_socket.getsockopt(0, option, 76)
            if len(raw) < 8:
                return None
            version, uid = struct.unpack_from("=II", raw, 0)
            return uid if version == 0 else None
        if sys.platform.startswith("linux"):
            option = getattr(socket, "SO_PEERCRED", None)
            if option is None:
                return None
            size = struct.calcsize("=iii")
            raw = peer_socket.getsockopt(socket.SOL_SOCKET, option, size)
            _pid, uid, _gid = struct.unpack("=iii", raw)
            return uid
    except (AttributeError, OSError, struct.error):
        return None
    return None


def _parse_control_request(raw: bytes) -> tuple[str, str]:
    value = _load_closed_json(raw, _CONTROL_REQUEST_KEYS)
    if (
        type(value.get("version")) is not int
        or value["version"] != _CONTROL_VERSION
        or value.get("operation") != "stop"
        or not _valid_nonce(value.get("generation"))
        or not _valid_nonce(value.get("request_nonce"))
    ):
        raise ValueError("control request fields are invalid")
    return value["generation"], value["request_nonce"]


async def _read_control_frame(reader: asyncio.StreamReader) -> bytes:
    framed = bytearray()
    while True:
        chunk = await reader.read(min(1024, _CONTROL_MAX_BYTES + 1 - len(framed)))
        if not chunk:
            break
        framed.extend(chunk)
        if len(framed) > _CONTROL_MAX_BYTES:
            raise ValueError("control request exceeds its size limit")
    if not framed or not framed.endswith(b"\n") or b"\n" in framed[:-1]:
        raise ValueError("control request framing is invalid")
    return bytes(framed[:-1])


async def _handle_control_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    generation: str,
    stop_event: asyncio.Event,
) -> None:
    try:
        peer_socket = writer.get_extra_info("socket")
        if peer_socket is None or _control_peer_uid(peer_socket) != os.getuid():
            return
        raw_request = await asyncio.wait_for(
            _read_control_frame(reader),
            timeout=_CONTROL_IO_TIMEOUT_SECONDS,
        )
        request_generation, request_nonce = _parse_control_request(raw_request)
        if not hmac.compare_digest(request_generation, generation):
            return
        response = (
            _canonical_json_bytes(
                {
                    "version": _CONTROL_VERSION,
                    "ok": True,
                    "generation": generation,
                    "request_nonce": request_nonce,
                }
            )
            + b"\n"
        )
        writer.write(response)
        await asyncio.wait_for(
            writer.drain(),
            timeout=_CONTROL_IO_TIMEOUT_SECONDS,
        )
        # The authenticated echo is in the kernel send buffer before the
        # existing graceful-shutdown path is triggered.
        stop_event.set()
    except (
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        OSError,
        TimeoutError,
        ValueError,
    ):
        return
    finally:
        writer.close()
        with suppress(OSError, TimeoutError):
            await asyncio.wait_for(
                writer.wait_closed(),
                timeout=_CONTROL_IO_TIMEOUT_SECONDS,
            )


def _unlink_unpublished_socket(
    socket_path: Path,
    *,
    socket_dev: int,
    socket_ino: int,
) -> None:
    try:
        current = os.lstat(socket_path)
    except OSError:
        return
    if (
        stat.S_ISSOCK(current.st_mode)
        and current.st_uid == os.getuid()
        and (current.st_dev, current.st_ino) == (socket_dev, socket_ino)
    ):
        with suppress(OSError):
            socket_path.unlink()


async def _start_control_endpoint(stop_event: asyncio.Event) -> _ControlEndpoint:
    directory = _secure_control_dir(create=True)
    _cleanup_stale_control_endpoint()
    generation = secrets.token_hex(_CONTROL_GENERATION_BYTES)
    socket_name = _socket_name_for_generation(generation)
    socket_path = directory / socket_name
    if len(os.fsencode(socket_path)) + 1 > 100:
        raise DaemonControlError("daemon control socket path exceeds the safe local limit")
    handler_tasks: set[asyncio.Task[None]] = set()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(
            _handle_control_client(
                reader,
                writer,
                generation=generation,
                stop_event=stop_event,
            ),
            name="daemon-control-client",
        )
        handler_tasks.add(task)
        task.add_done_callback(handler_tasks.discard)

    try:
        server = await asyncio.start_unix_server(
            accept,
            path=str(socket_path),
            limit=_CONTROL_MAX_BYTES,
        )
    except OSError as exc:
        raise DaemonControlError("cannot bind daemon control socket") from exc
    socket_dev = socket_ino = 0
    try:
        os.chmod(socket_path, 0o600)
        info = os.lstat(socket_path)
        socket_dev, socket_ino = info.st_dev, info.st_ino
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise DaemonControlError("daemon control socket publication is unsafe")
        metadata = _ControlMetadata(
            generation=generation,
            socket_name=socket_name,
            socket_dev=socket_dev,
            socket_ino=socket_ino,
        )
        _write_control_metadata(metadata)
        persisted = _read_control_metadata()
        if persisted is None:
            raise DaemonControlError("daemon control metadata disappeared after publication")
        return _ControlEndpoint(
            metadata=persisted,
            socket_path=socket_path,
            server=server,
            handler_tasks=handler_tasks,
        )
    except BaseException:
        server.close()
        await server.wait_closed()
        if socket_ino:
            _unlink_unpublished_socket(
                socket_path,
                socket_dev=socket_dev,
                socket_ino=socket_ino,
            )
        raise


async def _close_control_endpoint(endpoint: _ControlEndpoint) -> None:
    endpoint.server.close()
    with suppress(OSError):
        await endpoint.server.wait_closed()
    for task in tuple(endpoint.handler_tasks):
        if not task.done():
            task.cancel()
    if endpoint.handler_tasks:
        await asyncio.gather(*tuple(endpoint.handler_tasks), return_exceptions=True)
    if not _remove_control_artifacts(endpoint.metadata):
        logger.warning("daemon control endpoint changed generation; refusing stale cleanup")
        return
    try:
        directory = _secure_control_dir(create=False)
        directory.rmdir()
    except (DaemonControlError, OSError):
        pass


def _recv_control_response(client: socket.socket) -> bytes:
    framed = bytearray()
    while True:
        chunk = client.recv(min(1024, _CONTROL_MAX_BYTES + 1 - len(framed)))
        if not chunk:
            break
        framed.extend(chunk)
        if len(framed) > _CONTROL_MAX_BYTES:
            raise DaemonControlError("daemon control response exceeds its size limit")
    if not framed:
        raise DaemonControlError("daemon closed the control connection without an echo")
    if not framed.endswith(b"\n") or b"\n" in framed[:-1]:
        raise DaemonControlError("daemon control response framing is invalid")
    return bytes(framed[:-1])


def request_stop(*, timeout_seconds: float = _CONTROL_IO_TIMEOUT_SECONDS) -> None:
    """Request graceful shutdown without ever signaling a PID."""
    metadata = _read_control_metadata()
    if metadata is None:
        raise DaemonControlError(
            "authenticated control metadata is unavailable; refusing PID signal fallback"
        )
    socket_path = _validated_control_socket(metadata)
    request_nonce = secrets.token_hex(_CONTROL_GENERATION_BYTES)
    request = (
        _canonical_json_bytes(
            {
                "version": _CONTROL_VERSION,
                "operation": "stop",
                "generation": metadata.generation,
                "request_nonce": request_nonce,
            }
        )
        + b"\n"
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(socket_path))
            if _control_peer_uid(client) != os.getuid():
                raise DaemonControlError("daemon control peer uid could not be verified")
            client.sendall(request)
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError as exc:
                # A same-uid endpoint may have already sent and closed its
                # response. Preserve the buffered echo for authentication;
                # every other shutdown failure remains fail-closed.
                if exc.errno != errno.ENOTCONN:
                    raise
            raw_response = _recv_control_response(client)
    except DaemonControlError:
        raise
    except OSError as exc:
        raise DaemonControlError("authenticated daemon control request failed") from exc
    try:
        response = _load_closed_json(raw_response, _CONTROL_RESPONSE_KEYS)
    except ValueError as exc:
        raise DaemonControlError("daemon control response schema is invalid") from exc
    if (
        type(response.get("version")) is not int
        or response["version"] != _CONTROL_VERSION
        or response.get("ok") is not True
        or not _valid_nonce(response.get("generation"))
        or not _valid_nonce(response.get("request_nonce"))
        or not hmac.compare_digest(response["generation"], metadata.generation)
        or not hmac.compare_digest(response["request_nonce"], request_nonce)
    ):
        raise DaemonControlError("daemon control response authentication failed")


def _acquire_daemon_lock() -> int:
    """Take the singleton daemon lease and return its held descriptor."""
    lock_path = paths.daemon_lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise RuntimeError("another OpenChronicle daemon holds the instance lock") from exc
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    return fd


def _release_daemon_lock(fd: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _write_pid_file() -> None:
    fd = os.open(
        paths.pid_file(),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_owned_pid_file() -> None:
    try:
        recorded = int(paths.pid_file().read_text().strip())
    except (FileNotFoundError, OSError, ValueError):
        return
    if recorded == os.getpid():
        with suppress(FileNotFoundError):
            paths.pid_file().unlink()


async def _mcp_loop(cfg: Config) -> None:
    """Host the MCP server inside the daemon. On crash, back off and restart."""
    from .mcp import server as mcp_server

    delay = 2.0
    while True:
        try:
            logger.info("mcp server starting (%s)", cfg.mcp.transport)
            await mcp_server.run_async(cfg)
            logger.info("mcp server exited cleanly")
            return
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            logger.error(
                "mcp server failed to bind %s:%d — %s",
                cfg.mcp.host,
                cfg.mcp.port,
                exc,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp server crashed: %s (restarting in %.0fs)", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


async def _run(
    cfg: Config,
    *,
    capture_only: bool = False,
    stop_event: asyncio.Event | None = None,
    daemon_lock_fd: int | None = None,
) -> None:
    paths.ensure_dirs()
    runtime_clock = MonotonicWallClock()

    # Capture-only must not make model calls, including the reducer normally
    # spawned by a session-end callback or the daily catch-up task. Clone the
    # caller's config so this runtime override never leaks back into CLI state.
    effective_cfg = copy.deepcopy(cfg) if capture_only else cfg
    if capture_only:
        effective_cfg.reducer.enabled = False
        effective_cfg.mcp.auto_start = False

    session_manager = None
    activity_gate = None
    tasks: list[asyncio.Task] = []
    stop_task: asyncio.Task | None = None
    installed_signals: list[signal.Signals] = []
    stop = stop_event or asyncio.Event()
    control_endpoint: _ControlEndpoint | None = None
    provider_runtime: llm_mod.ProviderDaemonRuntime | None = None
    owns_provider_runtime = False
    provider_context_token = None

    def _handle_stop() -> None:
        logger.info("shutdown signal received")
        try:
            if provider_runtime is not None:
                # Close admission and kill current provider process groups in
                # the signal callback, before slower task cleanup begins.
                llm_mod.cancel_daemon_provider_runtime(provider_runtime)
        except Exception:  # noqa: BLE001
            logger.exception("provider cancellation failed during shutdown signal")
        finally:
            stop.set()

    loop = asyncio.get_running_loop()
    owns_daemon_lock = daemon_lock_fd is None
    if daemon_lock_fd is None:
        daemon_lock_fd = _acquire_daemon_lock()
    try:
        provider_runtime, owns_provider_runtime = llm_mod.ensure_daemon_provider_runtime()
        provider_context_token = llm_mod.bind_daemon_provider_runtime(provider_runtime)
        _write_pid_file()
        control_endpoint = await _start_control_endpoint(stop)
        # A live writer holds the corresponding global lock across every temp
        # file lifetime. Under those locks, any leftover temp is necessarily a
        # crash artifact and can be removed before services expose the store.
        removed_memory_temps = store_files.cleanup_orphan_memory_temps()
        capture_temp_stats = capture_scheduler.cleanup_buffer(
            effective_cfg.capture.buffer_retention_hours,
            capture_config=effective_cfg.capture,
        )
        if removed_memory_temps or capture_temp_stats["deleted"]:
            logger.info(
                "startup removed crash temps: memory=%d capture=%d",
                removed_memory_temps,
                capture_temp_stats["deleted"],
            )
        # An authorized forget may have crashed after its deny-read
        # tombstones committed but before Markdown was removed. Recovery is a
        # core privacy invariant, so it must run even when the opt-in Daily
        # Wrap worker is disabled (the default) and before MCP is exposed.
        with fts.cursor() as conn:
            MemoryService(
                conn, soft_limit_tokens=effective_cfg.writer.soft_limit_tokens
            ).resume_pending_purges()

        # Both SQLite search indexes are projections of canonical files. A
        # crash may land between a file rename and its projection commit, so
        # restore them before session recovery or any externally visible task
        # starts. Invalid Markdown provenance deliberately aborts readiness.
        capture_reconcile_stats = capture_reconcile.reconcile_capture_index()
        with fts.cursor() as conn:
            memory_file_count, memory_entry_count = entries_store.rebuild_index(conn)
        if capture_reconcile_stats.removed or capture_reconcile_stats.skipped:
            logger.info(
                "startup repaired capture projection: removed=%d skipped=%d",
                capture_reconcile_stats.removed,
                capture_reconcile_stats.skipped,
            )
        logger.info(
            "startup rebuilt memory projection: files=%d entries=%d",
            memory_file_count,
            memory_entry_count,
        )
        # SessionManager observes every capture-worthy event and fires the
        # reducer via its on_session_end callback. Built even when
        # capture_only is true so session rows still land on disk.
        session_manager = session_tick.build_manager(
            effective_cfg,
            daemon_lease_held=True,
            clock=runtime_clock,
        )

        persisted_capture_hook = session_manager.on_persisted_capture
        if not capture_only and effective_cfg.suggestions.enabled:
            from .suggestions.activity import CaptureActivityGate

            activity_gate = CaptureActivityGate()

            def persisted_capture_hook(event: dict[str, object]) -> None:
                session_manager.on_persisted_capture(event)
                activity_gate.on_persisted_capture(event)

        tasks = [
            asyncio.create_task(
                capture_scheduler.run_forever(
                    effective_cfg.capture,
                    pre_capture_hook=persisted_capture_hook,
                    timestamp_provider=runtime_clock,
                ),
                name="capture",
            ),
            asyncio.create_task(
                session_tick.run_check_cuts(effective_cfg, session_manager),
                name="session",
            ),
            asyncio.create_task(
                session_tick.run_daily_safety_net(effective_cfg, session_manager),
                name="daily-safety-net",
            ),
        ]
        if not capture_only:
            tasks.append(
                asyncio.create_task(
                    timeline_tick.run_forever(
                        effective_cfg,
                        now_provider=runtime_clock,
                    ),
                    name="timeline",
                )
            )
            if effective_cfg.daily_wrap.enabled:
                from .daily_wrap import worker as daily_wrap_worker

                tasks.append(
                    asyncio.create_task(
                        daily_wrap_worker.run_forever(effective_cfg),
                        name="daily-wrap",
                    )
                )
            if effective_cfg.suggestions.enabled:
                from .suggestions import worker as suggestion_worker

                tasks.append(
                    asyncio.create_task(
                        suggestion_worker.run_forever(
                            effective_cfg,
                            now_provider=runtime_clock,
                            activity_gate=activity_gate,
                        ),
                        name="suggestions",
                    )
                )
            if effective_cfg.prompt_rescue.enabled:
                from .prompt_rescue import worker as prompt_rescue_worker

                tasks.append(
                    asyncio.create_task(
                        prompt_rescue_worker.run_forever(effective_cfg),
                        name="prompt-rescue",
                    )
                )
            # Both loops intentionally return immediately when the reducer is
            # disabled. Do not supervise tasks that are configured not to run:
            # an early normal return is otherwise indistinguishable from a
            # crashed background worker.
            if effective_cfg.reducer.enabled:
                tasks.append(
                    asyncio.create_task(
                        session_tick.run_flush_tick(effective_cfg, session_manager),
                        name="flush",
                    )
                )
                tasks.append(
                    asyncio.create_task(
                        session_tick.run_classifier_tick(effective_cfg, session_manager),
                        name="classifier-tick",
                    )
                )
                tasks.append(
                    asyncio.create_task(
                        session_tick.run_pending_reduction_tick(effective_cfg),
                        name="pending-reducer",
                    )
                )
        # Capture-only is a strict ingestion/debugging mode: it must not expose
        # the partially-populated store over MCP.
        if (
            not capture_only
            and effective_cfg.mcp.auto_start
            and effective_cfg.mcp.transport in ("sse", "streamable-http")
        ):
            tasks.append(asyncio.create_task(_mcp_loop(effective_cfg), name="mcp"))

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_stop)
            except NotImplementedError:
                continue
            installed_signals.append(sig)

        stop_task = asyncio.create_task(stop.wait(), name="stop-signal")
        done, _pending = await asyncio.wait(
            [stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED
        )

        # A daemon worker is expected to run until cancellation. Any worker
        # that finishes first — whether by returning or raising — is a daemon
        # failure, not a clean shutdown. Check exceptions first so simultaneous
        # completions preserve the most useful cause.
        completed_workers = sorted(
            (task for task in done if task is not stop_task),
            key=lambda task: task.get_name(),
        )
        for task in completed_workers:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.error("background task %s failed: %s", task.get_name(), exc)
                raise RuntimeError(f"background task {task.get_name()!r} failed: {exc}") from exc
        if completed_workers:
            task = completed_workers[0]
            if task.cancelled():
                logger.error("background task %s was cancelled unexpectedly", task.get_name())
                raise RuntimeError(
                    f"background task {task.get_name()!r} was cancelled unexpectedly"
                )
            logger.error("background task %s exited unexpectedly", task.get_name())
            raise RuntimeError(f"background task {task.get_name()!r} exited unexpectedly")

        logger.info("stop requested; cancelling background tasks")
    finally:
        # Revoke provider admission before cancelling asyncio tasks. A task
        # blocked in a synchronous provider call then unwinds promptly instead
        # of surviving until its configured outer deadline.
        try:
            if provider_runtime is not None:
                llm_mod.cancel_daemon_provider_runtime(provider_runtime)
        finally:
            try:
                cleanup_tasks = [*tasks]
                if stop_task is not None:
                    cleanup_tasks.append(stop_task)
                for task in cleanup_tasks:
                    if not task.done():
                        task.cancel()
                if cleanup_tasks:
                    with suppress(asyncio.CancelledError):
                        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

                # Persist the currently open session without spawning an
                # untracked daemon reducer after all workers have been joined.
                # The next boot's safety-net reduces the durable ended row.
                if session_manager is not None:
                    with suppress(Exception):
                        session_manager.force_end(
                            reason="daemon-shutdown",
                            run_end_callback=False,
                        )
                    # Natural cuts may already have dispatched terminal
                    # reducers. Join their concrete thread handles before the
                    # provider lifecycle and singleton lease can close.
                    session_manager.drain_end_callbacks()
            finally:
                try:
                    # Daily Wrap and reducer one-shot threads are registered
                    # with this generation. Task cancellation above first
                    # revokes their durable claims; draining now rules out a
                    # late publish and reaps every provider leader.
                    if provider_runtime is not None:
                        if owns_provider_runtime:
                            llm_mod.finish_daemon_provider_runtime(provider_runtime)
                        else:
                            llm_mod.drain_daemon_provider_runtime(provider_runtime)
                finally:
                    if provider_context_token is not None:
                        llm_mod.reset_daemon_provider_runtime(provider_context_token)

                    try:
                        if control_endpoint is not None:
                            await _close_control_endpoint(control_endpoint)
                    finally:
                        for sig in installed_signals:
                            with suppress(NotImplementedError):
                                loop.remove_signal_handler(sig)

                        _remove_owned_pid_file()
                        if owns_daemon_lock:
                            _release_daemon_lock(daemon_lock_fd)
                        logger.info("daemon stopped")


def run(cfg: Config, *, capture_only: bool = False) -> None:
    # Keep the singleton lease outside ``asyncio.run``. Cancelling a
    # ``to_thread`` awaitable does not stop its executor function; asyncio.run
    # waits for the default executor during loop shutdown. Releasing the lease
    # inside ``_run`` would let a replacement daemon overlap those old writes.
    daemon_lock_fd = _acquire_daemon_lock()
    provider_runtime: llm_mod.ProviderDaemonRuntime | None = None
    try:
        provider_runtime = llm_mod.begin_daemon_provider_runtime()
        asyncio.run(
            _run(
                cfg,
                capture_only=capture_only,
                daemon_lock_fd=daemon_lock_fd,
            )
        )
    finally:
        try:
            # ``asyncio.run`` has now joined the default executor. Keep the
            # cancelled generation visible through that join so copied
            # contexts cannot start work under a replacement daemon.
            if provider_runtime is not None:
                llm_mod.finish_daemon_provider_runtime(provider_runtime)
        finally:
            _remove_owned_pid_file()
            _release_daemon_lock(daemon_lock_fd)
