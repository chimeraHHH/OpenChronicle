"""Hard-crash/restart recovery tests for persisted active sessions."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from openchronicle import config as config_mod
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef
from openchronicle.session import store as session_store
from openchronicle.session import tick as session_tick
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store

_TZ = timezone(timedelta(hours=8))


def _cfg(ac_root: Path) -> config_mod.Config:
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.reducer.enabled = False
    return cfg


def _insert_active(
    session_id: str,
    start: datetime,
    *,
    owner_pid: int | None = None,
    owner_token: str | None = None,
) -> None:
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=start,
                status="active",
                owner_pid=owner_pid,
                owner_token=owner_token,
            ),
        )


def _insert_block(start: datetime, end: datetime) -> None:
    with fts.cursor() as conn:
        block = timeline_store.TimelineBlock(
            start_time=start,
            end_time=end,
            entries=["[Cursor] persisted before the crash"],
            apps_used=["Cursor"],
            capture_count=1,
        )
        timeline_store.insert(conn, block)
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block.id),
            sources=[
                EvidenceRef(
                    kind="observation",
                    id=f"fixture-{block.id}",
                    path=f"fixture-{block.id}.json",
                    content_hash=f"fixture-digest-{block.id}",
                )
            ],
        )


def _row(session_id: str) -> session_store.SessionRow:
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
    assert row is not None
    return row


def test_kill_restart_equivalent_recovers_into_pending_reduction(
    ac_root: Path,
) -> None:
    """Persisted active row + a new manager is the durable SIGKILL/restart boundary."""
    restart_time = datetime.now().astimezone().replace(microsecond=0)
    start = restart_time - timedelta(minutes=3)
    block_end = start + timedelta(minutes=2)

    # PID 0 can never own a userspace daemon.  Leaving this active row behind
    # is the database state that SIGKILL produces because shutdown callbacks
    # never run.
    _insert_active("sess_killed", start, owner_pid=0)
    _insert_block(start + timedelta(minutes=1), block_end)

    cfg = _cfg(ac_root)
    manager = session_tick.build_manager(cfg)

    recovered = _row("sess_killed")
    assert recovered.status == "ended"
    assert recovered.end_time == block_end
    assert recovered.end_time >= recovered.start_time

    with fts.cursor() as conn:
        pending_ids = {row.id for row in session_store.list_pending_reduction(conn)}
    assert "sess_killed" in pending_ids

    # The restarted manager has no current session until the first new event.
    assert manager.current_snapshot() is None


def test_live_current_session_is_not_recovered(ac_root: Path) -> None:
    """A normal active row owned by this live process must remain active."""
    start = datetime.now().astimezone().replace(microsecond=0)
    _insert_active(
        "sess_live",
        start,
        owner_pid=os.getpid(),
        owner_token=session_store.current_owner_token(),
    )

    recovered = session_tick.recover_orphan_sessions(
        _cfg(ac_root),
        now=start + timedelta(minutes=1),
    )

    assert recovered == 0
    row = _row("sess_live")
    assert row.status == "active"
    assert row.end_time is None


def test_session_owned_by_another_live_pid_is_protected(ac_root: Path) -> None:
    start = datetime.now().astimezone().replace(microsecond=0)
    _insert_active(
        "sess_other_live",
        start,
        owner_pid=4242,
        owner_token="another-daemon-instance",
    )

    recovered = session_tick.recover_orphan_sessions(
        _cfg(ac_root),
        now=start + timedelta(minutes=1),
        pid_is_alive=lambda pid: pid == 4242,
    )

    assert recovered == 0
    assert _row("sess_other_live").status == "active"


def test_daemon_lease_overrides_reused_live_pid(ac_root: Path) -> None:
    """A singleton lease proves that a different live PID is only a reuse."""
    start = datetime.now().astimezone().replace(microsecond=0) - timedelta(minutes=2)
    _insert_active(
        "sess_other_reused_pid",
        start,
        owner_pid=4242,
        owner_token="token-from-crashed-daemon",
    )

    recovered = session_tick.recover_orphan_sessions(
        _cfg(ac_root),
        now=start + timedelta(minutes=2),
        pid_is_alive=lambda pid: pid == 4242,
        daemon_lease_held=True,
    )

    assert recovered == 1
    row = _row("sess_other_reused_pid")
    assert row.status == "ended"
    assert row.end_time == start + timedelta(minutes=1)


def test_manager_started_session_records_owner_and_survives_recovery(
    ac_root: Path,
) -> None:
    """Recovery called while a manager is live cannot close its current row."""
    cfg = _cfg(ac_root)
    manager = session_tick.build_manager(cfg)
    manager.on_event({"bundle_id": "com.cursor"})
    session_id = manager.current_id
    assert session_id is not None

    active = _row(session_id)
    assert active.owner_pid == os.getpid()
    assert active.owner_token == session_store.current_owner_token()

    assert session_tick.recover_orphan_sessions(cfg) == 0
    assert _row(session_id).status == "active"

    manager.force_end(reason="test-cleanup")


def test_instance_token_detects_previous_boot_when_pid_is_reused(
    ac_root: Path,
) -> None:
    """The current PID alone cannot make a previous interpreter's row live."""
    start = datetime.now().astimezone().replace(microsecond=0) - timedelta(minutes=2)
    _insert_active(
        "sess_reused_pid",
        start,
        owner_pid=os.getpid(),
        owner_token="token-from-previous-boot",
    )

    assert (
        session_tick.recover_orphan_sessions(
            _cfg(ac_root),
            now=start + timedelta(minutes=2),
        )
        == 1
    )
    assert _row("sess_reused_pid").status == "ended"


def test_fast_restart_clamps_empty_fallback_to_restart_time(ac_root: Path) -> None:
    """A restart inside one minute must not make the old session overlap new work."""
    start = datetime(2026, 8, 7, 9, 0, tzinfo=_TZ)
    restart_time = start + timedelta(seconds=10)
    _insert_active("sess_fast", start, owner_pid=1234)

    assert (
        session_tick.recover_orphan_sessions(
            _cfg(ac_root),
            now=restart_time,
            pid_is_alive=lambda _pid: False,
        )
        == 1
    )

    row = _row("sess_fast")
    assert row.end_time == restart_time
    assert row.end_time >= row.start_time


def test_clock_rollback_never_sets_end_before_start(ac_root: Path) -> None:
    """A backwards wall-clock jump yields a zero-length, still-reducible session."""
    start = datetime(2026, 8, 7, 9, 0, tzinfo=_TZ)
    # Rows written before owner identity was introduced have both fields NULL.
    _insert_active("sess_clock", start)

    assert (
        session_tick.recover_orphan_sessions(
            _cfg(ac_root),
            now=start - timedelta(minutes=5),
        )
        == 1
    )

    row = _row("sess_clock")
    assert row.status == "ended"
    assert row.end_time == start
    with fts.cursor() as conn:
        assert [pending.id for pending in session_store.list_pending_reduction(conn)] == [
            "sess_clock"
        ]


def test_consecutive_orphans_are_disjoint_and_recovery_is_idempotent(
    ac_root: Path,
) -> None:
    """The next session start bounds fallback and a second pass changes nothing."""
    first_start = datetime(2026, 8, 7, 9, 0, tzinfo=_TZ)
    second_start = first_start + timedelta(seconds=30)
    _insert_active("sess_first", first_start, owner_pid=0)
    _insert_active("sess_second", second_start, owner_pid=0)

    cfg = _cfg(ac_root)
    restart_time = first_start + timedelta(minutes=10)
    assert session_tick.recover_orphan_sessions(cfg, now=restart_time) == 2
    assert session_tick.recover_orphan_sessions(cfg, now=restart_time) == 0

    first = _row("sess_first")
    second = _row("sess_second")
    assert first.end_time == second_start
    assert second.end_time == second_start + timedelta(minutes=1)
    assert first.end_time <= second.start_time


def test_timeline_inference_is_bounded_by_restart_and_max_duration(
    ac_root: Path,
) -> None:
    """Persisted future/stray blocks cannot extend a recovered session window."""
    start = datetime(2026, 8, 7, 9, 0, tzinfo=_TZ)
    restart_time = start + timedelta(minutes=20)
    _insert_active("sess_bounded", start, owner_pid=0)
    # A block crossing the restart boundary is visible to the intersection
    # query, but its end must be clamped to the exact restart time.
    _insert_block(start + timedelta(minutes=19), start + timedelta(minutes=21))
    # This unrelated block is beyond both restart and the two-hour ceiling.
    _insert_block(start + timedelta(hours=5), start + timedelta(hours=5, minutes=1))

    assert (
        session_tick.recover_orphan_sessions(
            _cfg(ac_root),
            now=restart_time,
        )
        == 1
    )

    assert _row("sess_bounded").end_time == restart_time


def test_owner_pid_schema_migrates_legacy_active_rows() -> None:
    """An existing database gains owner identity without rewriting its orphan row."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            start_time TEXT NOT NULL,
            end_time TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            flush_end TEXT,
            classified_end TEXT
        );
        INSERT INTO sessions (
            id, start_time, status, retry_count, last_error, created_at, updated_at
        ) VALUES (
            'sess_legacy',
            '2026-08-07T09:00:00+08:00',
            'active',
            0,
            '',
            '2026-08-07T09:00:00+08:00',
            '2026-08-07T09:00:00+08:00'
        );
        """
    )

    session_store.ensure_schema(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "owner_pid" in columns
    assert "owner_token" in columns
    legacy = session_store.get_by_id(conn, "sess_legacy")
    assert legacy is not None
    assert legacy.owner_pid is None
    assert legacy.owner_token is None
    conn.close()


def test_naive_legacy_times_are_interpreted_as_local_wall_clock(
    monkeypatch,
) -> None:
    """Naive legacy values must not be relabelled as UTC during comparisons."""
    with monkeypatch.context() as patch:
        patch.setenv("TZ", "Asia/Shanghai")
        time.tzset()
        naive_local = datetime(2026, 8, 7, 9, 0)
        expected = datetime(2026, 8, 7, 1, 0, tzinfo=UTC)
        assert session_store._instant(naive_local) == expected
        assert session_tick._instant(naive_local) == expected
    # ``monkeypatch.context`` restored TZ; refresh libc's timezone cache too.
    time.tzset()


def test_naive_legacy_sql_bounds_use_local_instants(ac_root: Path, monkeypatch) -> None:
    """SQLite's UTC assumption for naive ISO text must not change recovery bounds."""
    with monkeypatch.context() as patch:
        patch.setenv("TZ", "Asia/Shanghai")
        time.tzset()
        naive_start = datetime(2026, 8, 7, 9, 0)
        aware_next = datetime(2026, 8, 7, 9, 30, tzinfo=_TZ)
        aware_block_start = datetime(2026, 8, 7, 9, 10, tzinfo=_TZ)
        aware_block_end = datetime(2026, 8, 7, 9, 20, tzinfo=_TZ)

        _insert_active("sess_naive", naive_start)
        _insert_active("sess_next", aware_next)
        _insert_block(aware_block_start, aware_block_end)

        with fts.cursor() as conn:
            assert session_store.next_session_start_after(conn, naive_start) == aware_next
            assert (
                session_store.latest_timeline_end_in_window(
                    conn,
                    start=naive_start,
                    end=datetime(2026, 8, 7, 10, 0),
                )
                == aware_block_end
            )
    time.tzset()
