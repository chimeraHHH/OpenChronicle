"""Durable, lease-fenced delivery state for classifier windows.

The event reducer and the periodic classifier tick only *request* coverage.
This store serializes those requests per session, freezes a deterministic
window when a worker claims it, persists the classifier's explicit ``commit``
as a receipt, and advances ``sessions.classified_end`` in the same transaction
that completes the job.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ..session import store as session_store

ClassifierJobStatus = Literal[
    "pending",
    "running",
    "failed",
    "committed",
    "succeeded",
]

EMPTY_TERMINAL_SKIP = "proven_empty_terminal"

SCHEMA = """
CREATE TABLE IF NOT EXISTS classifier_jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'periodic',
    terminal_entry_id TEXT NOT NULL DEFAULT '',
    event_daily_path TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    requested_end TEXT NOT NULL,
    include_prior_day INTEGER NOT NULL DEFAULT 0,
    allow_empty INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at TEXT,
    next_retry_at TEXT,
    producer_run_key TEXT NOT NULL DEFAULT '',
    input_digest TEXT NOT NULL DEFAULT '',
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    committed_at TEXT,
    completed_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    UNIQUE(session_id, kind, window_start, terminal_entry_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_classifier_jobs_active_session
    ON classifier_jobs(session_id)
    WHERE status IN ('pending', 'running', 'failed', 'committed');
CREATE INDEX IF NOT EXISTS idx_classifier_jobs_due
    ON classifier_jobs(status, next_retry_at, updated_at);
CREATE TABLE IF NOT EXISTS classifier_job_schema_migrations (
    name TEXT PRIMARY KEY
);
"""

_TYPED_EMPTY_RECEIPT_MIGRATION = "typed-empty-receipt-v1"


@dataclass(frozen=True, slots=True)
class ClassifierJob:
    id: str
    session_id: str
    kind: str
    terminal_entry_id: str
    event_daily_path: str
    window_start: datetime
    window_end: datetime
    requested_end: datetime
    include_prior_day: bool
    allow_empty: bool
    status: ClassifierJobStatus
    attempt_count: int
    lease_token: str | None
    lease_expires_at: datetime | None
    next_retry_at: datetime | None
    producer_run_key: str
    input_digest: str
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    committed_at: datetime | None
    completed_at: datetime | None
    last_error: str


@dataclass(frozen=True, slots=True)
class ClaimResult:
    row: ClassifierJob
    claimed: bool


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    completed: ClassifierJob
    followup: ClassifierJob | None


class ClassifierJobBusy(RuntimeError):
    """Another healthy worker owns the requested classifier window."""


class ClassifierJobLostLease(RuntimeError):
    """A stale classifier worker attempted to publish a result."""


class ClassifierJobGap(RuntimeError):
    """Completing this job would skip an unclassified window."""


class ClassifierJobInputChanged(RuntimeError):
    """The evidence snapshot changed while a classifier job was running."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(classifier_jobs)")}
    if "allow_empty" not in columns:
        conn.execute(
            "ALTER TABLE classifier_jobs ADD COLUMN allow_empty INTEGER NOT NULL DEFAULT 0"
        )
    migration_done = conn.execute(
        "SELECT 1 FROM classifier_job_schema_migrations WHERE name=?",
        (_TYPED_EMPTY_RECEIPT_MIGRATION,),
    ).fetchone()
    if migration_done is None:
        _migrate_legacy_skip_receipts(conn)
        conn.execute(
            "INSERT OR IGNORE INTO classifier_job_schema_migrations(name) VALUES (?)",
            (_TYPED_EMPTY_RECEIPT_MIGRATION,),
        )


def _migrate_legacy_skip_receipts(conn: sqlite3.Connection) -> None:
    """Quarantine pre-proof skips so one old row cannot block recovery."""
    rows = conn.execute(
        """
        SELECT id, status, result_json FROM classifier_jobs
         WHERE status IN ('committed', 'succeeded') AND result_json IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        skipped_reason = payload.get("skipped_reason")
        if not skipped_reason or skipped_reason == EMPTY_TERMINAL_SKIP:
            continue
        if row["status"] == "succeeded":
            # A succeeded row is historical only: its bookmark already moved.
            # Drop the unverifiable pre-proof audit row instead of pretending
            # that its free-text skip had today's typed zero-block proof.
            conn.execute("DELETE FROM classifier_jobs WHERE id=?", (row["id"],))
            continue
        conn.execute(
            """
            UPDATE classifier_jobs
               SET status='failed', attempt_count=0,
                   lease_token=NULL, lease_expires_at=NULL, next_retry_at=NULL,
                   producer_run_key='', input_digest='', result_json=NULL,
                   committed_at=NULL, completed_at=NULL,
                   last_error='legacy skip receipt requires typed proof'
             WHERE id=? AND status='committed'
            """,
            (row["id"],),
        )


def make_id(
    session_id: str,
    window_start: datetime,
    *,
    kind: str = "periodic",
    terminal_entry_id: str = "",
) -> str:
    identity = terminal_entry_id if kind == "terminal" else _instant(window_start).isoformat()
    material = f"classifier-job-v2\0{session_id}\0{kind}\0{identity}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:24]
    return f"classifier-job-{digest}"


def make_producer_run_key(job_id: str) -> str:
    return hashlib.sha256(f"classifier-delivery-v1\0{job_id}".encode()).hexdigest()


def get(conn: sqlite3.Connection, job_id: str) -> ClassifierJob | None:
    row = conn.execute("SELECT * FROM classifier_jobs WHERE id=?", (job_id,)).fetchone()
    return _to_job(row) if row else None


def get_active_for_session(conn: sqlite3.Connection, session_id: str) -> ClassifierJob | None:
    row = conn.execute(
        """
        SELECT * FROM classifier_jobs
         WHERE session_id=?
           AND status IN ('pending', 'running', 'failed', 'committed')
         LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    return _to_job(row) if row else None


def request(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    requested_end: datetime,
    include_prior_day: bool,
    now: datetime | None = None,
) -> ClassifierJob | None:
    """Durably request coverage through ``requested_end``.

    Requests coalesce until a worker claims the window. Requests arriving
    while a worker is running extend ``requested_end`` and become a follow-up
    window only after the frozen in-flight window commits.
    """
    now = now or datetime.now().astimezone()
    now_iso = now.isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        session = session_store.get_by_id(conn, session_id)
        if session is None:
            raise ValueError(f"unknown classifier session: {session_id}")
        window_start = session.classified_end or session.start_time
        if session.flush_end is None or _instant(requested_end) > _instant(session.flush_end):
            raise ValueError("periodic classifier request lacks a durable flush proof")
        canonical_path = f"event-{session.start_time.strftime('%Y-%m-%d')}.md"
        active = get_active_for_session(conn, session_id)
        if _instant(requested_end) <= _instant(window_start):
            conn.execute("COMMIT")
            return active

        if active is None:
            job_id = make_id(session_id, window_start)
            conn.execute(
                """
                INSERT INTO classifier_jobs(
                    id, session_id, kind, terminal_entry_id, event_daily_path,
                    window_start, window_end, requested_end, include_prior_day,
                    status, created_at, updated_at
                ) VALUES (?, ?, 'periodic', '', ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    job_id,
                    session_id,
                    canonical_path,
                    window_start.isoformat(),
                    requested_end.isoformat(),
                    requested_end.isoformat(),
                    1 if include_prior_day else 0,
                    now_iso,
                    now_iso,
                ),
            )
        else:
            if active.kind != "periodic":
                conn.execute("COMMIT")
                return active
            target = _later(active.requested_end, requested_end)
            # Pending/failed work has no healthy worker and can absorb the
            # larger target before its next claim. Running/committed work keeps
            # a frozen end; finalize creates a contiguous follow-up job.
            window_end = (
                target
                if active.status == "pending" and active.attempt_count == 0
                else active.window_end
            )
            include_prior = active.include_prior_day or (
                include_prior_day and active.status == "pending" and active.attempt_count == 0
            )
            conn.execute(
                """
                UPDATE classifier_jobs
                   SET window_end=?, requested_end=?,
                       include_prior_day=?,
                       updated_at=?
                 WHERE id=?
                """,
                (
                    window_end.isoformat(),
                    target.isoformat(),
                    1 if include_prior else 0,
                    now_iso,
                    active.id,
                ),
            )
            job_id = active.id
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return get(conn, job_id)


def request_terminal(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    now: datetime | None = None,
) -> ClassifierJob | None:
    """Create the durable terminal delivery owed by ``mark_reduced``."""
    now = now or datetime.now().astimezone()
    now_iso = now.isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        session = session_store.get_by_id(conn, session_id)
        if session is None:
            raise ValueError(f"unknown classifier session: {session_id}")
        if not session.classifier_terminal_pending:
            conn.execute("COMMIT")
            return None
        if session.status != "reduced" or session.end_time is None:
            raise ValueError("terminal classifier request lacks a reduced-session proof")
        window_start = session.classified_end or session.start_time
        window_end = _later(window_start, session.end_time)
        entry_id = session.classifier_terminal_entry_id
        if entry_id and session.classifier_terminal_noop:
            raise ClassifierJobGap("terminal reducer intent has conflicting evidence proofs")
        if not entry_id and not session.classifier_terminal_noop:
            raise ClassifierJobGap("terminal reducer intent has no durable entry or empty proof")
        allow_empty = session.classifier_terminal_noop and (
            session.flush_end is None
            or _instant(window_start) >= _instant(session.flush_end)
        )
        job_id = make_id(
            session_id,
            window_start,
            kind="terminal",
            terminal_entry_id=entry_id,
        )
        event_path = session.classifier_terminal_path or (
            f"event-{session.start_time.strftime('%Y-%m-%d')}.md"
        )
        active = get_active_for_session(conn, session_id)
        if active is not None:
            if active.kind != "terminal":
                conn.execute("COMMIT")
                return active
            if _matches_terminal_proof(
                active,
                entry_id=entry_id,
                event_path=event_path,
                window_start=window_start,
                window_end=window_end,
                allow_empty=allow_empty,
            ):
                conn.execute("COMMIT")
                return active
            # Hydration may discover an exact legacy entry after an older
            # placeholder job was already created. Replacing the row in this
            # transaction fences any live old token before the new proof is
            # visible, even when both jobs share the empty-entry identity.
            conn.execute("DELETE FROM classifier_jobs WHERE id=?", (active.id,))
        existing_terminal = get(conn, job_id)
        if existing_terminal is not None and existing_terminal.status == "succeeded":
            if _matches_terminal_proof(
                existing_terminal,
                entry_id=entry_id,
                event_path=event_path,
                window_start=window_start,
                window_end=window_end,
                allow_empty=allow_empty,
            ):
                session_store.clear_classifier_terminal_pending(
                    conn,
                    session_id,
                    terminal_entry_id=entry_id,
                )
                conn.execute("COMMIT")
                return existing_terminal
            conn.execute("DELETE FROM classifier_jobs WHERE id=?", (existing_terminal.id,))
        conn.execute(
            """
            INSERT INTO classifier_jobs(
                id, session_id, kind, terminal_entry_id, event_daily_path,
                window_start, window_end, requested_end, include_prior_day,
                allow_empty, status, created_at, updated_at
            ) VALUES (?, ?, 'terminal', ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                job_id,
                session_id,
                entry_id,
                event_path,
                window_start.isoformat(),
                window_end.isoformat(),
                window_end.isoformat(),
                1 if session.classified_end is None else 0,
                1 if allow_empty else 0,
                now_iso,
                now_iso,
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return get(conn, job_id)


def _matches_terminal_proof(
    job: ClassifierJob,
    *,
    entry_id: str,
    event_path: str,
    window_start: datetime,
    window_end: datetime,
    allow_empty: bool,
) -> bool:
    return (
        job.kind == "terminal"
        and job.terminal_entry_id == entry_id
        and job.event_daily_path == event_path
        and _instant(job.window_start) == _instant(window_start)
        and _instant(job.window_end) == _instant(window_end)
        and job.allow_empty == allow_empty
    )


def list_due(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[ClassifierJob]:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be in [1, 1000]")
    now = now or datetime.now().astimezone()
    rows = conn.execute(
        """
        SELECT * FROM classifier_jobs
         WHERE status IN ('pending', 'running', 'failed', 'committed')
         ORDER BY created_at, id
        """
    ).fetchall()
    due: list[ClassifierJob] = []
    for raw in rows:
        job = _to_job(raw)
        if (
            job.status in ("pending", "committed")
            or job.status == "running"
            and not _lease_is_live(job, now)
            or job.status == "failed"
            and (job.next_retry_at is None or _instant(job.next_retry_at) <= _instant(now))
        ):
            due.append(job)
    due.sort(key=lambda job: (0 if job.status == "committed" else 1, job.created_at, job.id))
    return due[:limit]


def claim(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> ClaimResult:
    if lease_seconds < 30 or lease_seconds > 21_600:
        raise ValueError("lease_seconds must be in [30, 21600]")
    now = now or datetime.now().astimezone()
    lease_token = uuid.uuid4().hex
    now_iso = now.isoformat()
    expires = (now + timedelta(seconds=lease_seconds)).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = get(conn, job_id)
        if current is None:
            raise ValueError(f"unknown classifier job: {job_id}")
        if current.status in ("committed", "succeeded"):
            conn.execute("COMMIT")
            return ClaimResult(row=current, claimed=False)
        if current.status == "running" and _lease_is_live(current, now):
            conn.execute("ROLLBACK")
            raise ClassifierJobBusy(f"classifier job {job_id} is already running")
        if (
            current.status == "failed"
            and current.next_retry_at is not None
            and _instant(current.next_retry_at) > _instant(now)
        ):
            conn.execute("COMMIT")
            return ClaimResult(row=current, claimed=False)
        # Any expired worker is fenced by its old token. The replacement may
        # safely absorb requests that accumulated while the lease was live.
        window_end = (
            _later(current.window_end, current.requested_end)
            if current.attempt_count == 0 and current.status == "pending"
            else current.window_end
        )
        result = conn.execute(
            """
            UPDATE classifier_jobs
               SET status='running', window_end=?, attempt_count=attempt_count+1,
                   lease_token=?, lease_expires_at=?, next_retry_at=NULL,
                   updated_at=?, last_error=''
             WHERE id=? AND status IN ('pending', 'running', 'failed')
            """,
            (
                window_end.isoformat(),
                lease_token,
                expires,
                now_iso,
                job_id,
            ),
        )
        if result.rowcount != 1:
            raise ClassifierJobLostLease(f"classifier job {job_id} changed during claim")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    row = get(conn, job_id)
    assert row is not None
    return ClaimResult(row=row, claimed=True)


def assert_lease(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    now: datetime | None = None,
) -> ClassifierJob:
    """Fence a mutation inside the caller's existing SQLite transaction."""
    current = get(conn, job_id)
    now = now or datetime.now().astimezone()
    if (
        current is None
        or current.status != "running"
        or current.lease_token != lease_token
        or not _lease_is_live(current, now)
    ):
        raise ClassifierJobLostLease(f"classifier job {job_id} has no live lease")
    return current


def renew(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> ClassifierJob:
    if lease_seconds < 30 or lease_seconds > 21_600:
        raise ValueError("lease_seconds must be in [30, 21600]")
    now = now or datetime.now().astimezone()
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert_lease(
            conn,
            job_id=job_id,
            lease_token=lease_token,
            now=now,
        )
        conn.execute(
            """
            UPDATE classifier_jobs
               SET lease_expires_at=?, updated_at=?
             WHERE id=? AND status='running' AND lease_token=?
            """,
            (
                (now + timedelta(seconds=lease_seconds)).isoformat(),
                now.isoformat(),
                job_id,
                lease_token,
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    row = get(conn, job_id)
    assert row is not None
    return row


def bind_input(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    input_digest: str,
    producer_run_key: str,
) -> ClassifierJob:
    """Bind the first evidence snapshot; retries must reproduce it exactly."""
    if not input_digest:
        raise ValueError("classifier input digest is required")
    if not producer_run_key:
        raise ValueError("classifier producer run key is required")
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = assert_lease(
            conn,
            job_id=job_id,
            lease_token=lease_token,
        )
        if current.input_digest and current.input_digest != input_digest:
            raise ClassifierJobInputChanged(f"classifier input changed for delivery {job_id}")
        if current.producer_run_key and current.producer_run_key != producer_run_key:
            raise ClassifierJobInputChanged(f"classifier run key changed for delivery {job_id}")
        conn.execute(
            """
            UPDATE classifier_jobs
               SET producer_run_key=?, input_digest=?, updated_at=?
             WHERE id=? AND status='running' AND lease_token=?
            """,
            (
                producer_run_key,
                input_digest,
                datetime.now().astimezone().isoformat(),
                job_id,
                lease_token,
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    row = get(conn, job_id)
    assert row is not None
    return row


def record_commit(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    producer_run_key: str,
    result: dict[str, Any],
    now: datetime | None = None,
    transaction_guard: Callable[[sqlite3.Connection], None] | None = None,
) -> ClassifierJob:
    """Persist the explicit classifier commit before bookmark advancement."""
    now = now or datetime.now().astimezone()
    now_iso = now.isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = assert_lease(
            conn,
            job_id=job_id,
            lease_token=lease_token,
            now=now,
        )
        if transaction_guard is not None:
            transaction_guard(conn)
        expected_run_key = current.producer_run_key
        if producer_run_key != expected_run_key or not expected_run_key or not current.input_digest:
            raise ClassifierJobInputChanged(
                f"classifier commit did not match bound delivery {job_id}"
            )
        candidate_rows = conn.execute(
            """
            SELECT id FROM memory_candidates
             WHERE producer_run_key=?
             ORDER BY proposal_slot, id
            """,
            (expected_run_key,),
        ).fetchall()
        canonical_result = dict(result)
        canonical_result["candidate_ids"] = [str(row[0]) for row in candidate_rows]
        _validate_result(canonical_result, job=current)
        payload = json.dumps(canonical_result, ensure_ascii=False, sort_keys=True)
        if len(payload.encode("utf-8")) > 65_536:
            raise ValueError("classifier commit receipt exceeds 64 KiB")
        updated = conn.execute(
            """
            UPDATE classifier_jobs
               SET status='committed', producer_run_key=?, result_json=?,
                   lease_token=NULL, lease_expires_at=NULL, next_retry_at=NULL,
                   updated_at=?, committed_at=?, last_error=''
             WHERE id=? AND status='running' AND lease_token=?
            """,
            (producer_run_key, payload, now_iso, now_iso, job_id, lease_token),
        )
        if updated.rowcount != 1:
            raise ClassifierJobLostLease(f"classifier job {job_id} lost its lease before commit")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    row = get(conn, job_id)
    assert row is not None
    return row


def fail(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    error: str,
    retry_seconds: int,
    now: datetime | None = None,
) -> ClassifierJob | None:
    if retry_seconds < 1 or retry_seconds > 3600:
        raise ValueError("retry_seconds must be in [1, 3600]")
    now = now or datetime.now().astimezone()
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = assert_lease(
            conn,
            job_id=job_id,
            lease_token=lease_token,
            now=now,
        )
        attempt = current.attempt_count
        delay = min(3600, retry_seconds * (2 ** max(0, min(attempt - 1, 6))))
        conn.execute(
            """
            UPDATE classifier_jobs
               SET status='failed', lease_token=NULL, lease_expires_at=NULL,
                   next_retry_at=?, updated_at=?, last_error=?
             WHERE id=? AND status='running' AND lease_token=?
            """,
            (
                (now + timedelta(seconds=delay)).isoformat(),
                now.isoformat(),
                error[:2000],
                job_id,
                lease_token,
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return get(conn, job_id)


def finalize(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    now: datetime | None = None,
) -> FinalizeResult:
    """Atomically advance the session bookmark and complete a commit receipt."""
    now = now or datetime.now().astimezone()
    now_iso = now.isoformat()
    followup_id: str | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = get(conn, job_id)
        if current is None:
            raise ValueError(f"unknown classifier job: {job_id}")
        if current.status == "succeeded":
            conn.execute("COMMIT")
            return FinalizeResult(completed=current, followup=None)
        if current.status != "committed":
            raise ValueError(f"classifier job {job_id} has no durable commit receipt")
        session = session_store.get_by_id(conn, current.session_id)
        if session is None:
            raise ClassifierJobGap(
                f"classifier session {current.session_id} disappeared before finalize"
            )
        cursor = session.classified_end or session.start_time
        if _instant(cursor) < _instant(current.window_start):
            raise ClassifierJobGap(f"classifier job {job_id} starts after the durable bookmark")
        if _instant(cursor) < _instant(current.window_end):
            session_store.set_classified_end(conn, current.session_id, current.window_end)
        updated = conn.execute(
            """
            UPDATE classifier_jobs
               SET status='succeeded', updated_at=?, completed_at=?,
                   lease_token=NULL, lease_expires_at=NULL,
                   next_retry_at=NULL, last_error=''
             WHERE id=? AND status='committed'
            """,
            (now_iso, now_iso, job_id),
        )
        if updated.rowcount != 1:
            raise ClassifierJobLostLease(f"classifier job {job_id} changed during finalize")

        if current.kind == "terminal":
            session_store.clear_classifier_terminal_pending(
                conn,
                current.session_id,
                terminal_entry_id=current.terminal_entry_id,
            )

        if current.kind == "periodic" and _instant(current.requested_end) > _instant(
            current.window_end
        ):
            followup_start = current.window_end
            followup_id = make_id(current.session_id, followup_start)
            conn.execute(
                """
                INSERT INTO classifier_jobs(
                    id, session_id, kind, terminal_entry_id, event_daily_path,
                    window_start, window_end, requested_end, include_prior_day,
                    status, created_at, updated_at
                ) VALUES (?, ?, 'periodic', '', ?, ?, ?, ?, 0, 'pending', ?, ?)
                ON CONFLICT(session_id, kind, window_start, terminal_entry_id)
                DO NOTHING
                """,
                (
                    followup_id,
                    current.session_id,
                    current.event_daily_path,
                    followup_start.isoformat(),
                    current.requested_end.isoformat(),
                    current.requested_end.isoformat(),
                    now_iso,
                    now_iso,
                ),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    completed = get(conn, job_id)
    assert completed is not None
    return FinalizeResult(
        completed=completed,
        followup=get(conn, followup_id) if followup_id else None,
    )


def _lease_is_live(job: ClassifierJob, now: datetime) -> bool:
    return job.lease_expires_at is not None and _instant(job.lease_expires_at) > _instant(now)


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _later(left: datetime, right: datetime) -> datetime:
    return left if _instant(left) >= _instant(right) else right


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _validate_result(
    result: dict[str, Any],
    *,
    job: ClassifierJob | None = None,
) -> None:
    if not isinstance(result.get("committed"), bool):
        raise ValueError("classifier receipt committed must be a boolean")
    if not isinstance(result.get("summary"), str):
        raise ValueError("classifier receipt summary must be text")
    if not isinstance(result.get("skipped_reason"), str):
        raise ValueError("classifier receipt skipped_reason must be text")
    for name in ("written_ids", "created_paths", "candidate_ids"):
        values = result.get(name)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError(f"classifier receipt {name} must be a string list")
    committed = result["committed"]
    skipped_reason = result["skipped_reason"]
    if committed and skipped_reason:
        raise ValueError("classifier receipt cannot both commit and skip")
    if not committed and not skipped_reason:
        raise ValueError("classifier receipt must commit or carry a skip proof")
    if skipped_reason:
        if skipped_reason != EMPTY_TERMINAL_SKIP:
            raise ValueError("classifier receipt has an unsupported skip proof")
        if job is None or job.kind != "terminal" or not job.allow_empty:
            raise ValueError("classifier empty receipt is not proven by its terminal job")
        if result["summary"] or any(
            result[name] for name in ("written_ids", "created_paths", "candidate_ids")
        ):
            raise ValueError("classifier empty receipt cannot carry mutations")


def _to_job(row: sqlite3.Row) -> ClassifierJob:
    def required_datetime(name: str) -> datetime:
        parsed = _parse_datetime(row[name])
        if parsed is None:
            raise ValueError(f"invalid classifier_jobs.{name}")
        return parsed

    result: dict[str, Any] | None = None
    if row["result_json"]:
        try:
            value = json.loads(row["result_json"])
            result = value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            result = None
    job = ClassifierJob(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        kind=str(row["kind"]),
        terminal_entry_id=str(row["terminal_entry_id"] or ""),
        event_daily_path=str(row["event_daily_path"]),
        window_start=required_datetime("window_start"),
        window_end=required_datetime("window_end"),
        requested_end=required_datetime("requested_end"),
        include_prior_day=bool(row["include_prior_day"]),
        allow_empty=bool(row["allow_empty"]),
        status=row["status"],
        attempt_count=int(row["attempt_count"]),
        lease_token=str(row["lease_token"]) if row["lease_token"] else None,
        lease_expires_at=_parse_datetime(row["lease_expires_at"]),
        next_retry_at=_parse_datetime(row["next_retry_at"]),
        producer_run_key=str(row["producer_run_key"] or ""),
        input_digest=str(row["input_digest"] or ""),
        result=result,
        created_at=required_datetime("created_at"),
        updated_at=required_datetime("updated_at"),
        committed_at=_parse_datetime(row["committed_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
        last_error=str(row["last_error"] or ""),
    )
    if job.kind not in ("periodic", "terminal"):
        raise ValueError(f"invalid classifier job kind: {job.kind}")
    if job.status in ("committed", "succeeded"):
        if job.result is None or not job.producer_run_key or not job.input_digest:
            raise ValueError(f"classifier job {job.id} has an invalid commit receipt")
        _validate_result(job.result, job=job)
    return job
