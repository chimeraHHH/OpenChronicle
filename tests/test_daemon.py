from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openchronicle import cli, daemon, paths
from openchronicle import config as config_mod
from openchronicle.provenance.models import EvidenceRef, content_digest
from openchronicle.services.memory import MemoryService
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.writer import llm as llm_mod


class _Manager:
    def __init__(self) -> None:
        self.force_end_reasons: list[str] = []
        self.force_end_callback_flags: list[bool] = []
        self.drain_calls = 0

    def on_event(self, _trigger) -> None:
        return None

    def on_persisted_capture(self, _trigger) -> None:
        return None

    def force_end(self, *, reason: str, run_end_callback: bool = True) -> None:
        self.force_end_reasons.append(reason)
        self.force_end_callback_flags.append(run_end_callback)

    def drain_end_callbacks(self) -> None:
        self.drain_calls += 1


def _install_manager(monkeypatch: pytest.MonkeyPatch) -> _Manager:
    manager = _Manager()

    def build_manager(_cfg, *, daemon_lease_held: bool, clock) -> _Manager:
        assert daemon_lease_held is True
        assert callable(clock)
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


async def _send_control_frame(path: Path, frame: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(frame)
        await writer.drain()
        writer.write_eof()
        return await asyncio.wait_for(reader.read(daemon._CONTROL_MAX_BYTES + 1), timeout=1)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


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

    def build_manager(_cfg, *, daemon_lease_held: bool, clock) -> BlockingDrainManager:
        assert daemon_lease_held is True
        assert callable(clock)
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
async def test_stop_reaps_provider_and_daily_wrap_thread_before_releasing_singleton(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openchronicle.daily_wrap import worker as daily_wrap_worker

    started: set[str] = set()
    cancelled: set[str] = set()
    _install_manager(monkeypatch)
    _patch_standard_workers(monkeypatch, started=started, cancelled=cancelled)

    monkeypatch.setattr(llm_mod, "_OUTER_TIMEOUT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(llm_mod, "_PROVIDER_TERMINATE_GRACE_SECONDS", 0.03)
    provider_code = (
        "import signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "sys.stdin.buffer.read(); time.sleep(60)"
    )
    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: (sys.executable, "-c", provider_code),
    )

    real_popen = subprocess.Popen
    provider_started = threading.Event()
    provider_processes: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        provider_processes.append(process)
        provider_started.set()
        return process

    monkeypatch.setattr(llm_mod.subprocess, "Popen", recording_popen)

    caller_finished = threading.Event()
    late_publish = threading.Event()

    async def daily_wrap_loop(cfg) -> None:
        def invoke() -> None:
            try:
                llm_mod.call_llm(
                    cfg,
                    "daily_wrap",
                    messages=[{"role": "user", "content": "private shutdown payload"}],
                )
            except llm_mod.ProviderCallError:
                pass
            else:
                late_publish.set()
            finally:
                caller_finished.set()

        thread = threading.Thread(
            target=invoke,
            name="openchronicle-daily-wrap-shutdown-test",
            daemon=True,
        )
        thread.start()
        await asyncio.Future()

    monkeypatch.setattr(daily_wrap_worker, "run_forever", daily_wrap_loop)
    released: list[tuple[bool, bool]] = []
    monkeypatch.setattr(daemon, "_acquire_daemon_lock", lambda: 73)
    monkeypatch.setattr(
        daemon,
        "_release_daemon_lock",
        lambda _fd: released.append(
            (
                caller_finished.is_set(),
                all(process.poll() is not None for process in provider_processes),
            )
        ),
    )

    cfg = config_mod.Config()
    cfg.models["default"] = config_mod.ModelConfig(
        model="test/model",
        api_key="sk-test",
        timeout_seconds=60.0,
        num_retries=0,
    )
    cfg.reducer.enabled = False
    cfg.daily_wrap.enabled = True
    cfg.mcp.auto_start = False
    stop = asyncio.Event()
    run_task = asyncio.create_task(daemon._run(cfg, stop_event=stop))

    try:
        assert await asyncio.to_thread(provider_started.wait, 2.0)
        stop.set()
        await asyncio.wait_for(run_task, timeout=2.0)

        assert caller_finished.is_set()
        assert provider_processes and all(
            process.poll() is not None for process in provider_processes
        )
        assert released == [(True, True)]
        assert not late_publish.is_set()
    finally:
        if not run_task.done():
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task
        for process in provider_processes:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        assert caller_finished.wait(timeout=2.0)


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
async def test_direct_run_resets_provider_admission_for_next_generation(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: set[str] = set()
    cancelled: set[str] = set()
    _install_manager(monkeypatch)
    _patch_standard_workers(monkeypatch, started=started, cancelled=cancelled)
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    generations: list[int] = []

    async def capture_uses_provider(_cfg, **_kwargs) -> None:
        llm_mod.call_llm(
            config_mod.Config(),
            "timeline",
            messages=[{"role": "user", "content": "generation probe"}],
        )
        runtime = llm_mod._active_provider_runtime
        assert runtime is not None
        generations.append(runtime.generation)
        await asyncio.Future()

    monkeypatch.setattr(
        daemon.capture_scheduler,
        "run_forever",
        capture_uses_provider,
    )
    cfg = config_mod.Config()
    cfg.reducer.enabled = False
    cfg.daily_wrap.enabled = False
    cfg.mcp.auto_start = False

    for expected_calls in (1, 2):
        stop = asyncio.Event()
        task = asyncio.create_task(daemon._run(cfg, stop_event=stop))
        while len(generations) < expected_calls:
            await asyncio.sleep(0)
        stop.set()
        await task
        assert llm_mod._active_provider_runtime is None

    assert len(generations) == 2
    assert generations[1] > generations[0]


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
        clock,
    ) -> _Manager:
        assert daemon_lease_held is True
        assert callable(clock)
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


@pytest.mark.asyncio
async def test_startup_rebuilds_memory_projection_before_session_recovery(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "topic-startup-reconcile.md"
    marker = "STARTUP_RECONCILE_PUBLIC_MARKER"
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name=name,
            description="startup reconcile fixture",
            tags=["topic"],
        )
        entry_id = entries_store.append_entry(
            conn,
            name=name,
            content=marker,
            tags=["runtime"],
        )
        conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        conn.execute("DELETE FROM files WHERE path=?", (name,))

    manager = _Manager()
    startup_observed: list[bool] = []

    def build_manager(_cfg, *, daemon_lease_held: bool, clock) -> _Manager:
        assert daemon_lease_held is True
        assert callable(clock)
        with fts.cursor() as conn:
            startup_observed.append(
                conn.execute(
                    "SELECT COUNT(*) FROM entries WHERE id=? AND path=?",
                    (entry_id, name),
                ).fetchone()[0]
                == 1
                and fts.get_file(conn, name) is not None
            )
        return manager

    started: set[str] = set()
    cancelled: set[str] = set()
    monkeypatch.setattr(daemon.session_tick, "build_manager", build_manager)
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

    assert startup_observed == [True]
    with fts.cursor() as conn:
        assert fts.search(conn, query=marker, top_k=5)[0].id == entry_id


@pytest.mark.asyncio
async def test_invalid_markdown_provenance_fails_readiness_before_workers(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = files_store.memory_path("project-invalid-startup.md")
    files_store.write_file(
        path,
        files_store.default_frontmatter(description="invalid fixture", tags=["project"]),
        (
            "## [2026-08-08T10:00:00+00:00] {id: invalid-startup}\n"
            "Public fixture\n"
            '<!-- oc-provenance: {"v":1,"sources":[]} -->\n'
            "trailing content\n"
        ),
    )
    monkeypatch.setattr(
        daemon.session_tick,
        "build_manager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("session recovery ran before projection validation")
        ),
    )

    cfg = config_mod.Config()
    cfg.mcp.auto_start = False
    with pytest.raises(ValueError, match="invalid provenance frame"):
        await daemon._run(cfg)

    assert not paths.pid_file().exists()


@pytest.mark.asyncio
async def test_cli_stop_uses_authenticated_control_socket_and_never_signals_pid(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    endpoint = await daemon._start_control_endpoint(stop)
    paths.pid_file().write_text("424242\n")
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(cli, "_init", lambda: config_mod.Config())
    monkeypatch.setattr(
        cli.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    try:
        directory_info = os.lstat(paths.daemon_control_dir())
        metadata_info = os.lstat(paths.daemon_control_metadata_file())
        socket_info = os.lstat(endpoint.socket_path)
        assert stat.S_ISDIR(directory_info.st_mode)
        assert stat.S_IMODE(directory_info.st_mode) == 0o700
        assert directory_info.st_uid == os.getuid()
        assert stat.S_ISREG(metadata_info.st_mode)
        assert stat.S_IMODE(metadata_info.st_mode) == 0o600
        assert metadata_info.st_uid == os.getuid()
        assert stat.S_ISSOCK(socket_info.st_mode)
        assert stat.S_IMODE(socket_info.st_mode) == 0o600
        assert socket_info.st_uid == os.getuid()
        assert len(endpoint.metadata.generation) == 64

        result = await asyncio.to_thread(CliRunner().invoke, cli.app, ["stop"])
        await asyncio.wait_for(stop.wait(), timeout=1)

        assert result.exit_code == 0, result.output
        assert "Authenticated daemon stop request accepted" in result.output
        assert signals == []
    finally:
        await daemon._close_control_endpoint(endpoint)

    assert not endpoint.socket_path.exists()
    assert not paths.daemon_control_metadata_file().exists()


@pytest.mark.asyncio
async def test_control_server_rejects_wrong_generation_malformed_oversize_and_peer(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    endpoint = await daemon._start_control_endpoint(stop)
    request_nonce = "1" * 64
    wrong_generation = "0" * 64
    assert wrong_generation != endpoint.metadata.generation

    try:
        wrong = (
            daemon._canonical_json_bytes(
                {
                    "version": daemon._CONTROL_VERSION,
                    "operation": "stop",
                    "generation": wrong_generation,
                    "request_nonce": request_nonce,
                }
            )
            + b"\n"
        )
        assert await _send_control_frame(endpoint.socket_path, wrong) == b""
        assert not stop.is_set()

        malformed = (
            daemon._canonical_json_bytes(
                {
                    "version": daemon._CONTROL_VERSION,
                    "operation": "stop",
                    "generation": endpoint.metadata.generation,
                    "request_nonce": request_nonce,
                    "unexpected": True,
                }
            )
            + b"\n"
        )
        assert await _send_control_frame(endpoint.socket_path, malformed) == b""
        assert not stop.is_set()

        oversize = b"x" * daemon._CONTROL_MAX_BYTES + b"\n"
        assert await _send_control_frame(endpoint.socket_path, oversize) == b""
        assert not stop.is_set()

        monkeypatch.setattr(daemon, "_CONTROL_IO_TIMEOUT_SECONDS", 0.02)
        timeout_reader, timeout_writer = await asyncio.open_unix_connection(
            str(endpoint.socket_path)
        )
        timeout_writer.write(b'{"version":1')
        await timeout_writer.drain()
        assert await asyncio.wait_for(timeout_reader.read(), timeout=1) == b""
        timeout_writer.close()
        with contextlib.suppress(OSError):
            await timeout_writer.wait_closed()
        assert not stop.is_set()

        valid = (
            daemon._canonical_json_bytes(
                {
                    "version": daemon._CONTROL_VERSION,
                    "operation": "stop",
                    "generation": endpoint.metadata.generation,
                    "request_nonce": request_nonce,
                }
            )
            + b"\n"
        )
        monkeypatch.setattr(daemon, "_control_peer_uid", lambda _socket: os.getuid() + 1)
        assert await _send_control_frame(endpoint.socket_path, valid) == b""
        assert not stop.is_set()
    finally:
        await daemon._close_control_endpoint(endpoint)


@pytest.mark.asyncio
async def test_new_control_generation_rejects_old_request_and_old_cleanup(
    ac_root: Path,
) -> None:
    first_stop = asyncio.Event()
    first = await daemon._start_control_endpoint(first_stop)
    await asyncio.to_thread(daemon.request_stop)
    await asyncio.wait_for(first_stop.wait(), timeout=1)
    await daemon._close_control_endpoint(first)

    second_stop = asyncio.Event()
    second = await daemon._start_control_endpoint(second_stop)
    try:
        assert first.metadata.generation != second.metadata.generation
        assert first.socket_path != second.socket_path
        assert daemon._remove_control_artifacts(first.metadata) is False
        assert second.socket_path.exists()
        current = daemon._read_control_metadata()
        assert current is not None
        assert daemon._same_control_generation(current, second.metadata)

        stale_request = (
            daemon._canonical_json_bytes(
                {
                    "version": daemon._CONTROL_VERSION,
                    "operation": "stop",
                    "generation": first.metadata.generation,
                    "request_nonce": "2" * 64,
                }
            )
            + b"\n"
        )
        assert await _send_control_frame(second.socket_path, stale_request) == b""
        assert not second_stop.is_set()

        await asyncio.to_thread(daemon.request_stop)
        await asyncio.wait_for(second_stop.wait(), timeout=1)
    finally:
        await daemon._close_control_endpoint(second)


@pytest.mark.asyncio
async def test_next_generation_recovers_sigkill_stale_endpoint_by_inode_and_nonce(
    ac_root: Path,
) -> None:
    stale_stop = asyncio.Event()
    stale = await daemon._start_control_endpoint(stale_stop)
    stale.server.close()
    await stale.server.wait_closed()
    assert stale.socket_path.exists()
    assert paths.daemon_control_metadata_file().exists()

    replacement_stop = asyncio.Event()
    replacement = await daemon._start_control_endpoint(replacement_stop)
    try:
        assert not stale.socket_path.exists()
        assert replacement.socket_path.exists()
        assert daemon._remove_control_artifacts(stale.metadata) is False
        assert replacement.socket_path.exists()
        await asyncio.to_thread(daemon.request_stop)
        await asyncio.wait_for(replacement_stop.wait(), timeout=1)
    finally:
        await daemon._close_control_endpoint(replacement)


@pytest.mark.asyncio
async def test_control_cleanup_requires_the_published_metadata_inode(ac_root: Path) -> None:
    endpoint = await daemon._start_control_endpoint(asyncio.Event())
    daemon._write_control_metadata(endpoint.metadata)
    replacement_metadata = daemon._read_control_metadata()
    assert replacement_metadata is not None
    assert replacement_metadata.metadata_ino != endpoint.metadata.metadata_ino

    assert daemon._remove_control_artifacts(endpoint.metadata) is False
    assert endpoint.socket_path.exists()
    assert paths.daemon_control_metadata_file().exists()

    endpoint.server.close()
    await endpoint.server.wait_closed()
    assert daemon._remove_control_artifacts(replacement_metadata)
    with contextlib.suppress(OSError):
        paths.daemon_control_dir().rmdir()


@pytest.mark.asyncio
async def test_control_cleanup_refuses_a_replaced_socket_inode(ac_root: Path) -> None:
    endpoint = await daemon._start_control_endpoint(asyncio.Event())
    endpoint.server.close()
    await endpoint.server.wait_closed()
    endpoint.socket_path.unlink()

    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(endpoint.socket_path))
    os.chmod(endpoint.socket_path, 0o600)
    replacement_info = os.lstat(endpoint.socket_path)
    assert replacement_info.st_ino != endpoint.metadata.socket_ino
    try:
        assert daemon._remove_control_artifacts(endpoint.metadata) is False
        assert endpoint.socket_path.exists()
        assert paths.daemon_control_metadata_file().exists()
    finally:
        replacement.close()
        endpoint.socket_path.unlink()

    assert daemon._remove_control_artifacts(endpoint.metadata)
    with contextlib.suppress(OSError):
        paths.daemon_control_dir().rmdir()


@pytest.mark.asyncio
async def test_control_files_reject_wrong_mode_and_symlinked_runtime_directory(
    ac_root: Path,
) -> None:
    stop = asyncio.Event()
    endpoint = await daemon._start_control_endpoint(stop)
    try:
        os.chmod(paths.daemon_control_metadata_file(), 0o644)
        with pytest.raises(daemon.DaemonControlError, match="0600"):
            await asyncio.to_thread(daemon.request_stop)
        assert not stop.is_set()
        os.chmod(paths.daemon_control_metadata_file(), 0o600)

        os.chmod(endpoint.socket_path, 0o666)
        with pytest.raises(daemon.DaemonControlError, match="0600"):
            await asyncio.to_thread(daemon.request_stop)
        assert not stop.is_set()
        os.chmod(endpoint.socket_path, 0o600)
    finally:
        await daemon._close_control_endpoint(endpoint)

    target = ac_root / "control-symlink-target"
    target.mkdir(mode=0o700)
    runtime_dir = paths.daemon_control_dir()
    runtime_dir.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(daemon.DaemonControlError, match="non-symlink 0700"):
            await daemon._start_control_endpoint(asyncio.Event())
    finally:
        runtime_dir.unlink()

    metadata_target = ac_root / "metadata-symlink-target"
    metadata_target.write_text("do not follow\n")
    paths.daemon_control_metadata_file().symlink_to(metadata_target)
    try:
        with pytest.raises(daemon.DaemonControlError, match="non-symlink 0600"):
            await asyncio.to_thread(daemon.request_stop)
        assert metadata_target.read_text() == "do not follow\n"
    finally:
        paths.daemon_control_metadata_file().unlink()


def test_stop_without_control_metadata_fails_closed_without_pid_fallback(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths.pid_file().write_text(f"{os.getpid()}\n")
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(cli, "_init", lambda: config_mod.Config())
    monkeypatch.setattr(
        cli.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    result = CliRunner().invoke(cli.app, ["stop"])

    assert result.exit_code == 1
    assert "refusing PID" in result.output
    assert "signal fallback" in result.output
    assert signals == []


def test_control_client_rejects_wrong_request_nonce_echo(ac_root: Path) -> None:
    directory = daemon._secure_control_dir(create=True)
    generation = "a" * 64
    socket_name = daemon._socket_name_for_generation(generation)
    socket_path = directory / socket_name
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    listener.listen(1)
    socket_info = os.lstat(socket_path)
    metadata = daemon._ControlMetadata(
        generation=generation,
        socket_name=socket_name,
        socket_dev=socket_info.st_dev,
        socket_ino=socket_info.st_ino,
    )
    daemon._write_control_metadata(metadata)
    persisted_metadata = daemon._read_control_metadata()
    assert persisted_metadata is not None
    errors: list[BaseException] = []

    def wrong_echo_server() -> None:
        try:
            connection, _address = listener.accept()
            with connection:
                request = bytearray()
                while b"\n" not in request:
                    request.extend(connection.recv(1024))
                value = json.loads(bytes(request).split(b"\n", 1)[0])
                request_nonce = value["request_nonce"]
                wrong_nonce = ("0" if request_nonce[0] != "0" else "1") + request_nonce[1:]
                connection.sendall(
                    daemon._canonical_json_bytes(
                        {
                            "version": daemon._CONTROL_VERSION,
                            "ok": True,
                            "generation": generation,
                            "request_nonce": wrong_nonce,
                        }
                    )
                    + b"\n"
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    server_thread = threading.Thread(target=wrong_echo_server)
    server_thread.start()
    try:
        with pytest.raises(daemon.DaemonControlError, match="authentication failed"):
            daemon.request_stop()
        server_thread.join(timeout=2)
        assert not server_thread.is_alive()
        assert errors == []
    finally:
        listener.close()
        server_thread.join(timeout=2)
        assert daemon._remove_control_artifacts(persisted_metadata)
        with contextlib.suppress(OSError):
            directory.rmdir()
