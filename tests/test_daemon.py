from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle import daemon, paths
from openchronicle.provenance.models import EvidenceRef, content_digest
from openchronicle.services.memory import MemoryService
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts


class _Manager:
    def __init__(self) -> None:
        self.force_end_reasons: list[str] = []
        self.force_end_callback_flags: list[bool] = []
        self.drain_calls = 0

    def on_event(self, _trigger) -> None:
        return None

    def force_end(self, *, reason: str, run_end_callback: bool = True) -> None:
        self.force_end_reasons.append(reason)
        self.force_end_callback_flags.append(run_end_callback)

    def drain_end_callbacks(self) -> None:
        self.drain_calls += 1


def _install_manager(monkeypatch: pytest.MonkeyPatch) -> _Manager:
    manager = _Manager()

    def build_manager(_cfg, *, daemon_lease_held: bool) -> _Manager:
        assert daemon_lease_held is True
        return manager

    monkeypatch.setattr(daemon.session_tick, "build_manager", build_manager)
    return manager


def _blocking_worker(
    name: str,
    *,
    started: set[str],
    cancelled: set[str],
):
    async def _run(*_args, **_kwargs) -> None:
        started.add(name)
        try:
            await asyncio.Future()
        finally:
            cancelled.add(name)

    return _run


def _patch_standard_workers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    started: set[str],
    cancelled: set[str],
) -> None:
    monkeypatch.setattr(
        daemon.capture_scheduler,
        "run_forever",
        _blocking_worker("capture", started=started, cancelled=cancelled),
    )
    monkeypatch.setattr(
        daemon.session_tick,
        "run_check_cuts",
        _blocking_worker("session", started=started, cancelled=cancelled),
    )
    monkeypatch.setattr(
        daemon.session_tick,
        "run_daily_safety_net",
        _blocking_worker("daily-safety-net", started=started, cancelled=cancelled),
    )
    monkeypatch.setattr(
        daemon.timeline_tick,
        "run_forever",
        _blocking_worker("timeline", started=started, cancelled=cancelled),
    )
    monkeypatch.setattr(
        daemon.session_tick,
        "run_flush_tick",
        _blocking_worker("flush", started=started, cancelled=cancelled),
    )
    monkeypatch.setattr(
        daemon.session_tick,
        "run_classifier_tick",
        _blocking_worker("classifier-tick", started=started, cancelled=cancelled),
    )
    monkeypatch.setattr(
        daemon.session_tick,
        "run_pending_reduction_tick",
        _blocking_worker("pending-reducer", started=started, cancelled=cancelled),
    )


def test_sync_run_holds_singleton_until_cancelled_executor_work_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_started = threading.Event()
    release_native = threading.Event()
    released_fds: list[int] = []
    errors: list[BaseException] = []

    def blocking_native_work() -> None:
        native_started.set()
        assert release_native.wait(timeout=5)

    async def fake_run(
        _cfg,
        *,
        capture_only: bool,
        daemon_lock_fd: int,
    ) -> None:
        assert capture_only is False
        assert daemon_lock_fd == 73
        task = asyncio.create_task(asyncio.to_thread(blocking_native_work))
        while not native_started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(daemon, "_acquire_daemon_lock", lambda: 73)
    monkeypatch.setattr(daemon, "_release_daemon_lock", released_fds.append)
    monkeypatch.setattr(daemon, "_remove_owned_pid_file", lambda: None)
    monkeypatch.setattr(daemon, "_run", fake_run)

    def invoke() -> None:
        try:
            daemon.run(config_mod.Config())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    run_thread = threading.Thread(target=invoke)
    run_thread.start()
    assert native_started.wait(timeout=5)
    # ``fake_run`` has returned, but asyncio.run is still shutting down its
    # executor. The singleton lease must remain held across that interval.
    assert run_thread.is_alive()
    assert released_fds == []

    release_native.set()
    run_thread.join(timeout=5)
    assert not run_thread.is_alive()
    assert errors == []
    assert released_fds == [73]


def test_sync_run_holds_singleton_until_session_reducers_are_drained(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drain_started = threading.Event()
    release_drain = threading.Event()
    released_fds: list[int] = []
    errors: list[BaseException] = []
    started: set[str] = set()
    cancelled: set[str] = set()

    class BlockingDrainManager(_Manager):
        def drain_end_callbacks(self) -> None:
            self.drain_calls += 1
            drain_started.set()
            assert release_drain.wait(timeout=5)

    manager = BlockingDrainManager()

    def build_manager(_cfg, *, daemon_lease_held: bool) -> BlockingDrainManager:
        assert daemon_lease_held is True
        return manager

    _patch_standard_workers(monkeypatch, started=started, cancelled=cancelled)

    async def capture_exits(*_args, **_kwargs) -> None:
        started.add("capture")

    monkeypatch.setattr(daemon.capture_scheduler, "run_forever", capture_exits)
    monkeypatch.setattr(daemon.session_tick, "build_manager", build_manager)
    monkeypatch.setattr(daemon, "_acquire_daemon_lock", lambda: 73)
    monkeypatch.setattr(daemon, "_release_daemon_lock", released_fds.append)

    cfg = config_mod.Config()
    cfg.reducer.enabled = False
    cfg.daily_wrap.enabled = False
    cfg.mcp.auto_start = False

    def invoke() -> None:
        try:
            daemon.run(cfg)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    run_thread = threading.Thread(target=invoke, name="daemon-drain-test")
    run_thread.start()
    assert drain_started.wait(timeout=5)
    assert run_thread.is_alive()
    assert released_fds == []

    release_drain.set()
    run_thread.join(timeout=5)
    assert not run_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert manager.drain_calls == 1
    assert released_fds == [73]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["return", "raise"])
async def test_worker_completion_fails_daemon_and_cleans_up(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    started: set[str] = set()
    cancelled: set[str] = set()
    session_started = asyncio.Event()
    manager = _install_manager(monkeypatch)
    _patch_standard_workers(monkeypatch, started=started, cancelled=cancelled)

    async def blocking_session(*_args, **_kwargs) -> None:
        started.add("session")
        session_started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.add("session")

    async def failing_capture(*_args, **_kwargs) -> None:
        started.add("capture")
        await session_started.wait()
        if failure_mode == "raise":
            raise ValueError("capture exploded")

    monkeypatch.setattr(daemon.capture_scheduler, "run_forever", failing_capture)
    monkeypatch.setattr(daemon.session_tick, "run_check_cuts", blocking_session)

    cfg = config_mod.Config()
    cfg.mcp.auto_start = False

    expected = "failed" if failure_mode == "raise" else "exited unexpectedly"
    with pytest.raises(RuntimeError, match=expected) as exc_info:
        await daemon._run(cfg)

    if failure_mode == "raise":
        assert isinstance(exc_info.value.__cause__, ValueError)
    assert "session" in cancelled
    assert manager.force_end_reasons == ["daemon-shutdown"]
    assert not paths.pid_file().exists()


@pytest.mark.asyncio
async def test_stop_event_gracefully_cancels_workers_and_skips_disabled_loops(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: set[str] = set()
    cancelled: set[str] = set()
    manager = _install_manager(monkeypatch)
    _patch_standard_workers(monkeypatch, started=started, cancelled=cancelled)

    async def forbidden(*_args, **_kwargs) -> None:
        raise AssertionError("disabled reducer loop was started")

    monkeypatch.setattr(daemon.session_tick, "run_flush_tick", forbidden)
    monkeypatch.setattr(daemon.session_tick, "run_classifier_tick", forbidden)
    monkeypatch.setattr(daemon.session_tick, "run_pending_reduction_tick", forbidden)

    cfg = config_mod.Config()
    cfg.reducer.enabled = False
    cfg.mcp.auto_start = False
    stop = asyncio.Event()

    run_task = asyncio.create_task(daemon._run(cfg, stop_event=stop))
    while not {"capture", "session", "daily-safety-net", "timeline"}.issubset(started):
        await asyncio.sleep(0)
    stop.set()
    await run_task

    assert started == {"capture", "session", "daily-safety-net", "timeline"}
    assert cancelled == started
    assert manager.force_end_reasons == ["daemon-shutdown"]
    assert manager.force_end_callback_flags == [False]
    assert manager.drain_calls == 1
    assert not paths.pid_file().exists()


@pytest.mark.asyncio
async def test_capture_only_excludes_mcp_and_processing_pipeline(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: set[str] = set()
    cancelled: set[str] = set()
    manager = _install_manager(monkeypatch)
    _patch_standard_workers(monkeypatch, started=started, cancelled=cancelled)
    reducer_settings: list[bool] = []

    def build_capture_only_manager(
        actual_cfg: config_mod.Config,
        *,
        daemon_lease_held: bool,
    ) -> _Manager:
        assert daemon_lease_held is True
        reducer_settings.append(actual_cfg.reducer.enabled)
        return manager

    monkeypatch.setattr(daemon.session_tick, "build_manager", build_capture_only_manager)

    async def forbidden(*_args, **_kwargs) -> None:
        raise AssertionError("capture-only started a processing or MCP task")

    monkeypatch.setattr(daemon.timeline_tick, "run_forever", forbidden)
    monkeypatch.setattr(daemon.session_tick, "run_flush_tick", forbidden)
    monkeypatch.setattr(daemon.session_tick, "run_classifier_tick", forbidden)
    monkeypatch.setattr(daemon.session_tick, "run_pending_reduction_tick", forbidden)
    monkeypatch.setattr(daemon, "_mcp_loop", forbidden)

    cfg = config_mod.Config()
    cfg.mcp.auto_start = True
    cfg.mcp.transport = "streamable-http"
    stop = asyncio.Event()

    run_task = asyncio.create_task(daemon._run(cfg, capture_only=True, stop_event=stop))
    while not {"capture", "session", "daily-safety-net"}.issubset(started):
        await asyncio.sleep(0)
    stop.set()
    await run_task

    assert started == {"capture", "session", "daily-safety-net"}
    assert cancelled == started
    assert reducer_settings == [False]
    assert cfg.reducer.enabled is True
    assert manager.force_end_reasons == ["daemon-shutdown"]
    assert not paths.pid_file().exists()


@pytest.mark.asyncio
async def test_default_daemon_startup_resumes_interrupted_memory_purge(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "DAEMON_STARTUP_PURGE_PRIVATE_MARKER"
    target_name = "project-startup-purge.md"
    source_name = "topic-startup-purge-source.md"
    source_content = "User explicitly requested the startup purge regression."
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name=source_name,
            description="explicit manual approval source",
            tags=["topic"],
        )
        source_id = entries_store.append_entry(
            conn,
            name=source_name,
            content=source_content,
            tags=["manual"],
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )
        source = EvidenceRef(
            kind="memory_entry",
            id=source_id,
            path=source_name,
            content_hash=content_digest(source_content),
        )
        entries_store.create_file(
            conn,
            name=target_name,
            description="startup purge regression",
            tags=["project"],
        )
        approval_cfg = config_mod.Config()
        approval_cfg.capture.deny_unknown_windows = False
        service = MemoryService(conn, cfg=approval_cfg)
        candidate = service.propose_candidate(
            kind="fact",
            target_path=target_name,
            content=marker,
            tags=["private"],
            evidence=[source],
        )
        accepted = service.approve_candidate(candidate.id, expected_version=candidate.version)
        assert accepted.applied_entry_id

        real_delete = entries_store.delete_entry

        def interrupted_delete(*args, **kwargs):
            raise OSError("simulated crash after tombstones")

        monkeypatch.setattr(entries_store, "delete_entry", interrupted_delete)
        with pytest.raises(OSError, match="simulated crash"):
            service.purge_candidate(candidate.id)
        monkeypatch.setattr(entries_store, "delete_entry", real_delete)

    target = paths.memory_dir() / target_name
    assert marker in target.read_text(encoding="utf-8")

    started: set[str] = set()
    cancelled: set[str] = set()
    manager = _install_manager(monkeypatch)
    _patch_standard_workers(monkeypatch, started=started, cancelled=cancelled)
    cfg = config_mod.Config()
    cfg.reducer.enabled = False
    cfg.daily_wrap.enabled = False
    cfg.mcp.auto_start = False
    stop = asyncio.Event()

    run_task = asyncio.create_task(daemon._run(cfg, stop_event=stop))
    while not {"capture", "session", "daily-safety-net", "timeline"}.issubset(started):
        await asyncio.sleep(0)
    stop.set()
    await run_task

    assert marker not in target.read_text(encoding="utf-8")
    with fts.cursor() as conn:
        assert MemoryService(conn).get_candidate(candidate.id) is None
        assert conn.execute("SELECT COUNT(*) FROM purge_tombstones").fetchone()[0] == 0
    assert manager.force_end_reasons == ["daemon-shutdown"]
