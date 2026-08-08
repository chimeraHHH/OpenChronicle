"""Top-level daemon: capture scheduler + timeline aggregator + session cutter.

The writer combines session-boundary callbacks with periodic reducer flushes,
classifier passes, and a lightweight retry loop for durable pending sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import fcntl
import os
import signal
from contextlib import suppress

from . import paths
from .capture import scheduler as capture_scheduler
from .config import Config
from .logger import get
from .services.memory import MemoryService
from .session import tick as session_tick
from .store import files as store_files
from .store import fts
from .timeline import tick as timeline_tick

logger = get("openchronicle.daemon")


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

    # Capture-only must not make model calls, including the reducer normally
    # spawned by a session-end callback or the daily catch-up task. Clone the
    # caller's config so this runtime override never leaks back into CLI state.
    effective_cfg = copy.deepcopy(cfg) if capture_only else cfg
    if capture_only:
        effective_cfg.reducer.enabled = False
        effective_cfg.mcp.auto_start = False

    session_manager = None
    tasks: list[asyncio.Task] = []
    stop_task: asyncio.Task | None = None
    installed_signals: list[signal.Signals] = []
    stop = stop_event or asyncio.Event()

    def _handle_stop() -> None:
        logger.info("shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    owns_daemon_lock = daemon_lock_fd is None
    if daemon_lock_fd is None:
        daemon_lock_fd = _acquire_daemon_lock()
    try:
        _write_pid_file()
        # A live writer holds the corresponding global lock across every temp
        # file lifetime. Under those locks, any leftover temp is necessarily a
        # crash artifact and can be removed before services expose the store.
        removed_memory_temps = store_files.cleanup_orphan_memory_temps()
        capture_temp_stats = capture_scheduler.cleanup_buffer(
            effective_cfg.capture.buffer_retention_hours
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
        # SessionManager observes every capture-worthy event and fires the
        # reducer via its on_session_end callback. Built even when
        # capture_only is true so session rows still land on disk.
        session_manager = session_tick.build_manager(
            effective_cfg,
            daemon_lease_held=True,
        )

        tasks = [
            asyncio.create_task(
                capture_scheduler.run_forever(
                    effective_cfg.capture,
                    pre_capture_hook=session_manager.on_event,
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
                asyncio.create_task(timeline_tick.run_forever(effective_cfg), name="timeline")
            )
            if effective_cfg.daily_wrap.enabled:
                from .daily_wrap import worker as daily_wrap_worker

                tasks.append(
                    asyncio.create_task(
                        daily_wrap_worker.run_forever(effective_cfg),
                        name="daily-wrap",
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
        cleanup_tasks = [*tasks]
        if stop_task is not None:
            cleanup_tasks.append(stop_task)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        if cleanup_tasks:
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)

        # Persist the currently open session without spawning an untracked
        # daemon reducer after all workers have been joined.  The ended row is
        # durable and the next boot's safety-net performs its reduction while
        # holding the replacement daemon's singleton lease.
        if session_manager is not None:
            with suppress(Exception):
                session_manager.force_end(
                    reason="daemon-shutdown",
                    run_end_callback=False,
                )
            # Natural idle/timeout/safety-net cuts may already have dispatched
            # terminal reducers before shutdown began.  Join their concrete
            # thread handles before returning from ``_run`` so both the local
            # and outer ``run`` singleton-lease paths cover every old write.
            session_manager.drain_end_callbacks()

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
    try:
        asyncio.run(
            _run(
                cfg,
                capture_only=capture_only,
                daemon_lock_fd=daemon_lock_fd,
            )
        )
    finally:
        _remove_owned_pid_file()
        _release_daemon_lock(daemon_lock_fd)
