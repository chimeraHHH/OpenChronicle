"""Fresh-interpreter worker used by the Stage 0 process acceptance harness."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import paths
from ..capture import filenames as capture_filenames
from ..capture import reconcile as capture_reconcile
from ..capture import scheduler as capture_scheduler
from ..config import Config
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, observation_digest
from ..session import store as session_store
from ..session import tick as session_tick
from ..store import entries as entries_store
from ..store import files as files_store
from ..store import fts
from ..timeline import store as timeline_store
from ..timeline import tick as timeline_tick
from ..writer import session_reducer

_CAPTURE_TIME = datetime(2026, 8, 8, 12, 0, 30, tzinfo=UTC)
_CAPTURE_OBSERVATION_ID = "obs_11111111111111111111111111111111"
_TIMELINE_NOW = datetime(2026, 8, 8, 12, 2, 0, tzinfo=UTC)
_SESSION_ID = "sess_runtime_acceptance"
_SESSION_OBSERVATION_ID = "obs_22222222222222222222222222222222"
_SESSION_START = datetime(2026, 8, 8, 13, 0, 0, tzinfo=UTC)
_SESSION_END = _SESSION_START + timedelta(minutes=1)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


def _config() -> Config:
    cfg = Config()
    cfg.capture.deny_unknown_windows = False
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 0
    cfg.mcp.auto_start = False
    cfg.daily_wrap.enabled = False
    return cfg


def _capture_payload(
    *,
    timestamp: datetime = _CAPTURE_TIME,
    observation_id: str = _CAPTURE_OBSERVATION_ID,
    visible_text: str = "PUBLIC_RUNTIME_ACCEPTANCE_TEXT",
) -> dict[str, Any]:
    title = "Runtime acceptance fixture"
    meta = {
        "app_name": "RuntimeFixture",
        "bundle_id": "org.openchronicle.runtime-fixture",
        "title": title,
        "pid": 4242,
        "window_id": 9001,
        "bounds": {"x": 10.0, "y": 20.0, "width": 800.0, "height": 600.0},
    }
    return {
        "schema_version": 4,
        "policy_version": 2,
        "capture_profile": "normal",
        "observation_id": observation_id,
        "timestamp": timestamp.isoformat(),
        "trigger": {"event_type": "manual"},
        "window_meta": meta,
        "focused_element": {"role": "AXTextArea", "value": visible_text},
        "visible_text": visible_text,
        "url": "",
        "ax_tree": {
            "timestamp": timestamp.isoformat(),
            "window_meta": meta,
            "apps": [
                {
                    "pid": 4242,
                    "name": "RuntimeFixture",
                    "bundle_id": "org.openchronicle.runtime-fixture",
                    "is_frontmost": True,
                    "windows": [
                        {
                            "title": title,
                            "focused": True,
                            "elements": [
                                {
                                    "role": "AXTextArea",
                                    "focused": True,
                                    "value": visible_text,
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    }


def _sqlite_health(conn) -> str:
    row = conn.execute("PRAGMA quick_check").fetchone()
    return str(row[0]) if row else "missing"


def capture_write() -> None:
    paths.ensure_dirs()
    path = capture_scheduler._write_capture(_capture_payload())
    _emit({"capture": path.name})


def capture_recover() -> None:
    paths.ensure_dirs()
    cleanup = capture_scheduler.cleanup_buffer(retention_hours=168)
    repaired = capture_reconcile.reconcile_capture_index()
    files = sorted(path.name for path in paths.capture_buffer_dir().glob("*.json"))
    temps = sorted(
        path.name
        for path in paths.capture_buffer_dir().iterdir()
        if capture_filenames.is_capture_temp_name(path.name)
    )
    with fts.cursor() as conn:
        rows = conn.execute(
            "SELECT id, observation_id FROM captures ORDER BY id"
        ).fetchall()
        health = _sqlite_health(conn)
    _emit(
        {
            "cleanup_deleted": cleanup["deleted"],
            "files": len(files),
            "file_digest": _digest_lines(files),
            "indexed": len(rows),
            "index_digest": _digest_lines(
                f"{row['id']}:{row['observation_id']}" for row in rows
            ),
            "reconcile": repaired.__dict__,
            "sqlite": health,
            "temps": len(temps),
        }
    )


def timeline_tick_once(*, seed: bool) -> None:
    paths.ensure_dirs()
    if seed:
        capture_scheduler._write_capture(_capture_payload())
    os.environ["OPENCHRONICLE_LLM_MOCK"] = "1"
    os.environ["OPENCHRONICLE_LLM_MOCK_JSON"] = json.dumps(
        {"entries": ["[RuntimeFixture] exercised process recovery"]}
    )
    timeline_tick._now = lambda: _TIMELINE_NOW
    produced = timeline_tick.tick_now(_config())
    _emit({"produced": produced, **_timeline_state()})


def _timeline_state() -> dict[str, Any]:
    with fts.cursor() as conn:
        rows = conn.execute(
            "SELECT id, start_time, end_time FROM timeline_blocks "
            "ORDER BY julianday(start_time), id"
        ).fetchall()
        processed = timeline_store.get_processed_range(conn)
        sources = conn.execute(
            "SELECT COUNT(*) FROM provenance_edges WHERE subject_kind='timeline_block'"
        ).fetchone()[0]
        health = _sqlite_health(conn)
    identities = [f"{row['start_time']}:{row['end_time']}" for row in rows]
    return {
        "blocks": len(rows),
        "block_windows": len(set(identities)),
        "block_digest": _digest_lines(identities),
        "processed_from": processed[0].isoformat() if processed else "",
        "processed_through": processed[1].isoformat() if processed else "",
        "sources": int(sources),
        "sqlite": health,
    }


def session_seed() -> None:
    _seed_session(status="ended")
    _emit({"seeded": True})


def session_status_seed() -> None:
    _seed_session(status="active")
    _emit({"seeded": True, **_session_state()})


def _seed_session(*, status: session_store.SessionStatus) -> None:
    paths.ensure_dirs()
    capture = _capture_payload(
        timestamp=_SESSION_START + timedelta(seconds=5),
        observation_id=_SESSION_OBSERVATION_ID,
        visible_text="PUBLIC_SESSION_RECOVERY_TEXT",
    )
    capture_path = capture_scheduler._write_capture(capture)
    block = timeline_store.TimelineBlock(
        start_time=_SESSION_START,
        end_time=_SESSION_END,
        entries=["[RuntimeFixture] exercised reducer recovery"],
        apps_used=["RuntimeFixture"],
        capture_count=1,
    )
    with fts.cursor() as conn:
        timeline_store.insert(conn, block)
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block.id),
            sources=[
                EvidenceRef(
                    kind="observation",
                    id=_SESSION_OBSERVATION_ID,
                    path=capture_path.name,
                    timestamp=capture["timestamp"],
                    content_hash=observation_digest(capture),
                )
            ],
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=_SESSION_ID,
                start_time=_SESSION_START,
                end_time=_SESSION_END if status != "active" else None,
                status=status,
                owner_pid=0 if status == "active" else None,
                owner_token="runtime-worker-crash-fixture" if status == "active" else None,
            ),
        )
        event_name = f"event-{_SESSION_START.date().isoformat()}.md"
        entries_store.create_file(
            conn,
            name=event_name,
            description="Runtime acceptance event fixture.",
            tags=["event", "session", "daily"],
        )


def session_mark_ended() -> None:
    with fts.cursor() as conn:
        changed = session_store.mark_ended(conn, _SESSION_ID, _SESSION_END)
    _emit({"changed": changed, **_session_state()})


def session_recover_ended() -> None:
    startup = _recover_memory_projection()
    recovered = session_tick.recover_orphan_sessions(
        _config(),
        now=_SESSION_END,
        pid_is_alive=lambda _pid: False,
        daemon_lease_held=True,
    )
    _emit({"recovered": recovered, **startup, **_session_state()})


def session_mark_failed() -> None:
    with fts.cursor() as conn:
        session_store.mark_ended(conn, _SESSION_ID, _SESSION_END)
    os.environ["OPENCHRONICLE_LLM_MOCK"] = "1"
    os.environ["OPENCHRONICLE_LLM_MOCK_JSON"] = "not-json"
    result = session_reducer.reduce_session(
        _config(),
        session_id=_SESSION_ID,
        start_time=_SESSION_START,
        end_time=_SESSION_END,
    )
    _emit({"succeeded": result.succeeded, "written": result.written, **_session_state()})


def session_recover_failed() -> None:
    startup = _recover_memory_projection()
    os.environ["OPENCHRONICLE_LLM_MOCK"] = "1"
    os.environ["OPENCHRONICLE_LLM_MOCK_JSON"] = "not-json"
    results = session_reducer.reduce_all_pending(_config())
    _emit(
        {
            "recovery_results": len(results),
            "written": sum(1 for result in results if result.written),
            **startup,
            **_session_state(),
        }
    )


def session_flush() -> None:
    startup = _recover_memory_projection()
    os.environ["OPENCHRONICLE_LLM_MOCK"] = "1"
    os.environ["OPENCHRONICLE_LLM_MOCK_JSON"] = json.dumps(
        {
            "summary": "Exercised active-session flush recovery.",
            "sub_tasks": [
                "[13:00-13:01, RuntimeFixture] flushed recovery fixture, involving public fixture"
            ],
        }
    )
    result = session_reducer.flush_active_session(
        _config(),
        session_id=_SESSION_ID,
        session_start=_SESSION_START,
        now=_SESSION_END,
    )
    _emit(
        {
            "written": bool(result and result.written),
            **startup,
            **_session_state(),
        }
    )


def session_reduce() -> None:
    startup = _recover_memory_projection()
    os.environ["OPENCHRONICLE_LLM_MOCK"] = "1"
    os.environ["OPENCHRONICLE_LLM_MOCK_JSON"] = json.dumps(
        {
            "summary": "Exercised runtime recovery.",
            "sub_tasks": [
                "[13:00-13:01, RuntimeFixture] exercised recovery, involving public fixture"
            ],
        }
    )
    result = session_reducer.reduce_session(
        _config(),
        session_id=_SESSION_ID,
        start_time=_SESSION_START,
        end_time=_SESSION_END,
    )
    _emit(
        {
            "succeeded": result.succeeded,
            "written": result.written,
            **startup,
            **_session_state(),
        }
    )


def session_inspect() -> None:
    _emit(_session_state())


def _recover_memory_projection() -> dict[str, int]:
    removed = files_store.cleanup_orphan_memory_temps()
    with fts.cursor() as conn:
        file_count, entry_count = entries_store.rebuild_index(conn)
    return {
        "startup_memory_temps_removed": removed,
        "startup_memory_files_rebuilt": file_count,
        "startup_memory_entries_rebuilt": entry_count,
    }


def _session_state() -> dict[str, Any]:
    event_name = f"event-{_SESSION_START.date().isoformat()}.md"
    event_path = files_store.memory_path(event_name)
    parsed = files_store.read_file(event_path)
    durable = [entry for entry in parsed.entries if f"sid:{_SESSION_ID}" in entry.tags]
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, _SESSION_ID)
        indexed = conn.execute(
            "SELECT id FROM entries WHERE tags LIKE ? ORDER BY id",
            (f"%sid:{_SESSION_ID}%",),
        ).fetchall()
        sources = conn.execute(
            "SELECT COUNT(*) FROM provenance_edges "
            "WHERE subject_kind='memory_entry' AND subject_path=?",
            (event_name,),
        ).fetchone()[0]
        file_row = conn.execute(
            "SELECT entry_count FROM files WHERE path=?", (event_name,)
        ).fetchone()
        pending_reductions = len(session_store.list_pending_reduction(conn))
        active_sessions = len(session_store.list_active(conn))
        health = _sqlite_health(conn)
    durable_ids = [entry.id for entry in durable]
    indexed_ids = [str(item["id"]) for item in indexed]
    memory_temp_count = sum(
        1
        for path in paths.memory_dir().iterdir()
        if files_store.is_memory_temp_name(path.name)
    )
    return {
        "durable_entries": len(durable_ids),
        "durable_digest": _digest_lines(durable_ids),
        "file_entry_count": int(file_row[0]) if file_row else -1,
        "indexed_entries": len(indexed_ids),
        "index_digest": _digest_lines(indexed_ids),
        "provenance_edges": int(sources),
        "memory_temp_count": memory_temp_count,
        "session_end": row.end_time.isoformat() if row and row.end_time else "",
        "flush_end": row.flush_end.isoformat() if row and row.flush_end else "",
        "retry_count": row.retry_count if row else -1,
        "next_retry_at": (
            row.next_retry_at.isoformat() if row and row.next_retry_at else ""
        ),
        "last_error": row.last_error if row else "",
        "session_status": row.status if row else "missing",
        "pending_reductions": pending_reductions,
        "active_sessions": active_sessions,
        "sqlite": health,
    }


def daemon_lock_hold(ready: Path) -> None:
    paths.ensure_dirs()
    from .. import daemon

    fd = daemon._acquire_daemon_lock()
    daemon._write_pid_file()
    files_store.atomic_write_text(ready, str(os.getpid()))
    while True:
        signal.pause()
    daemon._release_daemon_lock(fd)


def daemon_lock_try() -> None:
    from .. import daemon

    try:
        fd = daemon._acquire_daemon_lock()
    except RuntimeError:
        _emit({"acquired": False})
        return
    try:
        _emit({"acquired": True})
    finally:
        daemon._release_daemon_lock(fd)


def daemon_lock_cycles(cycles: int) -> None:
    from .. import daemon

    for _index in range(cycles):
        fd = daemon._acquire_daemon_lock()
        try:
            daemon._write_pid_file()
        finally:
            daemon._remove_owned_pid_file()
            daemon._release_daemon_lock(fd)
    _emit({"cycles": cycles, "pid_file_exists": paths.pid_file().exists()})


def daemon_run_stub(ready: Path) -> None:
    """Run the real daemon lifecycle with external workers replaced by blockers."""
    from .. import daemon

    async def blocking_worker(*_args, **_kwargs) -> None:
        if not ready.exists():
            files_store.atomic_write_text(ready, str(os.getpid()))
        await asyncio.Future()

    daemon.capture_scheduler.run_forever = blocking_worker
    daemon.session_tick.run_check_cuts = blocking_worker
    daemon.session_tick.run_daily_safety_net = blocking_worker
    daemon.timeline_tick.run_forever = blocking_worker

    cfg = _config()
    cfg.reducer.enabled = False
    daemon.run(cfg)


def pid_probe() -> None:
    from .. import cli

    _emit({"pid": cli._read_pid()})


def _digest_lines(values) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "capture-write",
            "capture-recover",
            "timeline-seed-tick",
            "timeline-recover",
            "session-seed",
            "session-status-seed",
            "session-mark-ended",
            "session-recover-ended",
            "session-mark-failed",
            "session-recover-failed",
            "session-flush",
            "session-reduce",
            "session-inspect",
            "daemon-lock-hold",
            "daemon-lock-try",
            "daemon-lock-cycles",
            "daemon-run-stub",
            "pid-probe",
        ),
    )
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--cycles", type=int, default=25)
    args = parser.parse_args()

    handlers = {
        "capture-write": capture_write,
        "capture-recover": capture_recover,
        "timeline-seed-tick": lambda: timeline_tick_once(seed=True),
        "timeline-recover": lambda: timeline_tick_once(seed=False),
        "session-seed": session_seed,
        "session-status-seed": session_status_seed,
        "session-mark-ended": session_mark_ended,
        "session-recover-ended": session_recover_ended,
        "session-mark-failed": session_mark_failed,
        "session-recover-failed": session_recover_failed,
        "session-flush": session_flush,
        "session-reduce": session_reduce,
        "session-inspect": session_inspect,
        "daemon-lock-try": daemon_lock_try,
        "daemon-lock-cycles": lambda: daemon_lock_cycles(max(1, args.cycles)),
        "pid-probe": pid_probe,
    }
    if args.mode == "daemon-lock-hold":
        if args.ready is None:
            parser.error("daemon-lock-hold requires --ready")
        daemon_lock_hold(args.ready)
        return
    if args.mode == "daemon-run-stub":
        if args.ready is None:
            parser.error("daemon-run-stub requires --ready")
        daemon_run_stub(args.ready)
        return
    handlers[args.mode]()


if __name__ == "__main__":
    main()
