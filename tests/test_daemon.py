from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle import daemon, paths
from openchronicle.provenance.models import EvidenceRef, timeline_block_digest
from openchronicle.services.memory import MemoryService
from openchronicle.store import entries as entries_store
from openchronicle.store import fts


class _Manager:
    def __init__(self) -> None:
        self.force_end_reasons: list[str] = []

    def on_event(self, _trigger) -> None:
        return None

    def force_end(self, *, reason: str) -> None:
        self.force_end_reasons.append(reason)


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
    start = "2026-04-21T10:00:00+00:00"
    end = "2026-04-21T10:01:00+00:00"
    with fts.cursor() as conn:
        conn.execute(
            """
            INSERT INTO timeline_blocks(
                id, start_time, end_time, timezone, entries, apps_used,
                capture_count, created_at
            ) VALUES ('tlb-daemon-purge', ?, ?, 'UTC', '[]', '[]', 0, ?)
            """,
            (start, end, end),
        )
        source = EvidenceRef(
            kind="timeline_block",
            id="tlb-daemon-purge",
            content_hash=timeline_block_digest(
                start=start, end=end, entries=[], apps=[]
            ),
        )
        entries_store.create_file(
            conn,
            name=target_name,
            description="startup purge regression",
            tags=["project"],
        )
        service = MemoryService(conn)
        candidate = service.propose_candidate(
            kind="fact",
            target_path=target_name,
            content=marker,
            tags=["private"],
            evidence=[source],
        )
        accepted = service.approve_candidate(
            candidate.id, expected_version=candidate.version
        )
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
