"""Lease-based canonical Daily Wrap job storage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..privacy.egress import privacy_egress_fenced
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, daily_wrap_sources_digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_wrap_jobs (
    id TEXT PRIMARY KEY,
    local_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'default',
    window_start_utc TEXT NOT NULL,
    window_end_utc TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    coverage_status TEXT NOT NULL DEFAULT 'partial',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at TEXT,
    input_digest TEXT NOT NULL DEFAULT '',
    published_input_digest TEXT NOT NULL DEFAULT '',
    output_json TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    UNIQUE(local_date, timezone, scope)
);
CREATE INDEX IF NOT EXISTS idx_daily_wrap_jobs_date
    ON daily_wrap_jobs(local_date, timezone);
CREATE INDEX IF NOT EXISTS idx_daily_wrap_jobs_status
    ON daily_wrap_jobs(status, updated_at);

CREATE TABLE IF NOT EXISTS daily_wrap_revisions (
    wrap_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    local_date TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    window_start_utc TEXT NOT NULL DEFAULT '',
    window_end_utc TEXT NOT NULL DEFAULT '',
    workflow_version INTEGER NOT NULL DEFAULT 0,
    input_digest TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    source_digest TEXT NOT NULL DEFAULT '',
    output_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(wrap_id, revision)
);

CREATE TABLE IF NOT EXISTS daily_wrap_schema_migrations (
    name TEXT PRIMARY KEY
);
"""

_REVISION_BINDING_MIGRATION = "revision-binding-v1"


@dataclass(frozen=True, slots=True)
class DailyWrapRow:
    id: str
    local_date: str
    timezone: str
    scope: str
    window_start_utc: str
    window_end_utc: str
    workflow_version: int
    status: str
    coverage_status: str
    attempt_count: int
    lease_token: str | None
    lease_expires_at: str | None
    input_digest: str
    published_input_digest: str
    output: dict[str, Any] | None
    revision: int
    created_at: str
    updated_at: str
    completed_at: str | None
    last_error: str

    def to_dict(self) -> dict[str, Any]:
        """Return only the immutable, published-revision projection.

        The canonical job row also carries mutable scheduler state (the active
        input digest, attempts, errors, leases, and timestamps).  Public
        readers must not confuse that state with the revision they have just
        authorized, especially while a refresh is running or after it fails.
        A policy-visible row necessarily has a validated published revision,
        so ``status`` below is a derived publication state rather than the
        mutable job status.
        """
        return {
            "id": self.id,
            "local_date": self.local_date,
            "timezone": self.timezone,
            "scope": self.scope,
            "window_start_utc": self.window_start_utc,
            "window_end_utc": self.window_end_utc,
            "workflow_version": self.workflow_version,
            "status": "succeeded",
            "coverage_status": self.coverage_status,
            "published_input_digest": self.published_input_digest,
            "output": self.output,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class ClaimResult:
    row: DailyWrapRow
    claimed: bool


class DailyWrapBusy(RuntimeError):
    """Another healthy worker currently owns this canonical day."""


class DailyWrapLostLease(RuntimeError):
    """A stale worker tried to publish after its lease was replaced."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    job_columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_wrap_jobs)")}
    revision_columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_wrap_revisions)")}
    migration_missing = (
        conn.execute(
            "SELECT 1 FROM daily_wrap_schema_migrations WHERE name=?",
            (_REVISION_BINDING_MIGRATION,),
        ).fetchone()
        is None
    )
    required_revision_columns = {
        "local_date",
        "timezone",
        "scope",
        "window_start_utc",
        "window_end_utc",
        "workflow_version",
        "source_digest",
    }
    if (
        "published_input_digest" in job_columns
        and not required_revision_columns - revision_columns
        and not migration_missing
    ):
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Repeat every schema decision after taking the writer lock. A killed
        # migration may have columns without a completed trust backfill, and
        # two upgrading processes may both have observed the legacy schema.
        job_columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_wrap_jobs)")}
        if "published_input_digest" not in job_columns:
            conn.execute(
                """
                ALTER TABLE daily_wrap_jobs
                ADD COLUMN published_input_digest TEXT NOT NULL DEFAULT ''
                """
            )

        revision_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(daily_wrap_revisions)")
        }
        for name, declaration in (
            ("local_date", "TEXT NOT NULL DEFAULT ''"),
            ("timezone", "TEXT NOT NULL DEFAULT ''"),
            ("scope", "TEXT NOT NULL DEFAULT ''"),
            ("window_start_utc", "TEXT NOT NULL DEFAULT ''"),
            ("window_end_utc", "TEXT NOT NULL DEFAULT ''"),
            ("workflow_version", "INTEGER NOT NULL DEFAULT 0"),
            ("source_digest", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in revision_columns:
                conn.execute(f"ALTER TABLE daily_wrap_revisions ADD COLUMN {name} {declaration}")

        migration_done = conn.execute(
            "SELECT 1 FROM daily_wrap_schema_migrations WHERE name=?",
            (_REVISION_BINDING_MIGRATION,),
        ).fetchone()
        if migration_done is None:
            _backfill_revision_binding_migration(conn)
            conn.execute(
                "INSERT INTO daily_wrap_schema_migrations(name) VALUES (?)",
                (_REVISION_BINDING_MIGRATION,),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _backfill_revision_binding_migration(conn: sqlite3.Connection) -> None:
    """Bind well-formed legacy revision edge sets exactly once."""
    provenance_exists = conn.execute(
        """
        SELECT 1 FROM sqlite_master
         WHERE type='table' AND name='provenance_edges'
        """
    ).fetchone()
    if provenance_exists is None:
        # Without the edge table there is no authoritative way to distinguish
        # a genuinely empty Wrap from missing legacy provenance.
        return
    rows = conn.execute(
        "SELECT wrap_id, revision FROM daily_wrap_revisions ORDER BY wrap_id, revision"
    ).fetchall()
    for row in rows:
        wrap_id = row["wrap_id"]
        revision = row["revision"]
        if not isinstance(wrap_id, str) or not wrap_id or type(revision) is not int or revision < 1:
            continue
        revision_ref = EvidenceRef(
            kind="daily_wrap_revision",
            id=f"{wrap_id}:r{revision}",
            path=wrap_id,
        )
        sources = provenance_store.direct_sources_checked(conn, revision_ref)
        if sources is None:
            # One malformed edge invalidates the complete source projection.
            # Leave the digest blank so public readers quarantine the row.
            continue
        conn.execute(
            """
            UPDATE daily_wrap_revisions
               SET source_digest=?
             WHERE wrap_id=? AND revision=?
            """,
            (daily_wrap_sources_digest(sources), wrap_id, revision),
        )


def make_id(local_date: str, timezone: str, scope: str = "default") -> str:
    digest = hashlib.sha256(f"{local_date}\0{timezone}\0{scope}".encode()).hexdigest()[:20]
    return f"daily-wrap-{digest}"


def get(
    conn: sqlite3.Connection,
    *,
    local_date: str,
    timezone: str,
    scope: str = "default",
) -> DailyWrapRow | None:
    row = conn.execute(
        """
        SELECT * FROM daily_wrap_jobs
         WHERE local_date=? AND timezone=? AND scope=?
        """,
        (local_date, timezone, scope),
    ).fetchone()
    return _to_row(row) if row else None


def get_by_id(conn: sqlite3.Connection, wrap_id: str) -> DailyWrapRow | None:
    row = conn.execute("SELECT * FROM daily_wrap_jobs WHERE id=?", (wrap_id,)).fetchone()
    return _to_row(row) if row else None


def list_wraps(
    conn: sqlite3.Connection,
    *,
    limit: int = 30,
    offset: int = 0,
) -> list[DailyWrapRow]:
    if limit < 1 or limit > 365:
        raise ValueError("limit must be in [1, 365]")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    rows = conn.execute(
        """
        SELECT * FROM daily_wrap_jobs
         ORDER BY local_date DESC, timezone, scope, id
         LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return [_to_row(row) for row in rows]


@privacy_egress_fenced
def claim(
    conn: sqlite3.Connection,
    *,
    local_date: str,
    timezone: str,
    scope: str,
    window_start_utc: str,
    window_end_utc: str,
    workflow_version: int,
    coverage_status: str,
    input_digest: str,
    lease_token: str,
    lease_seconds: int = 300,
    now: datetime | None = None,
    force_refresh: bool = False,
    expected_revision: int | None = None,
    expected_updated_at: str | None = None,
    expected_published_input_digest: str | None = None,
) -> ClaimResult:
    if lease_seconds < 30 or lease_seconds > 21_600:
        raise ValueError("lease_seconds must be in [30, 21600]")
    if force_refresh and (
        expected_revision is None
        or expected_updated_at is None
        or expected_published_input_digest is None
    ):
        raise ValueError("force_refresh requires the expected published job version")
    now = now or datetime.now().astimezone()
    now_iso = now.isoformat()
    expires = (now + timedelta(seconds=lease_seconds)).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = get(conn, local_date=local_date, timezone=timezone, scope=scope)
        if force_refresh:
            if existing is None:
                conn.execute("ROLLBACK")
                raise DailyWrapLostLease("daily wrap disappeared before forced refresh")
            if (
                existing.revision != expected_revision
                or existing.updated_at != expected_updated_at
                or existing.published_input_digest != expected_published_input_digest
            ):
                # Another caller already changed, repaired, or reclaimed the
                # stale projection.  Return its canonical row without
                # overwriting it; the service will authorize that newer row
                # and only retry if it remains invalid.
                conn.execute("COMMIT")
                return ClaimResult(row=existing, claimed=False)
        if (
            existing
            and existing.status == "succeeded"
            and (existing.published_input_digest or existing.input_digest) == input_digest
            and not force_refresh
        ):
            conn.execute("COMMIT")
            return ClaimResult(row=existing, claimed=False)
        if existing and existing.status == "running" and _lease_is_live(existing, now):
            conn.execute("ROLLBACK")
            raise DailyWrapBusy(f"daily wrap {local_date} ({timezone}) is already running")
        wrap_id = existing.id if existing else make_id(local_date, timezone, scope)
        if existing is None:
            conn.execute(
                """
                INSERT INTO daily_wrap_jobs(
                    id, local_date, timezone, scope, window_start_utc,
                    window_end_utc, workflow_version, status, coverage_status,
                    attempt_count, lease_token, lease_expires_at, input_digest,
                    published_input_digest, output_json, revision,
                    created_at, updated_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, 1, ?, ?, ?, '', NULL, 0, ?, ?, '')
                """,
                (
                    wrap_id,
                    local_date,
                    timezone,
                    scope,
                    window_start_utc,
                    window_end_utc,
                    workflow_version,
                    coverage_status,
                    lease_token,
                    expires,
                    input_digest,
                    now_iso,
                    now_iso,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE daily_wrap_jobs
                   SET status='running',
                       attempt_count=attempt_count+1, lease_token=?,
                       lease_expires_at=?, input_digest=?,
                       updated_at=?, last_error=''
                 WHERE id=?
                """,
                (
                    lease_token,
                    expires,
                    input_digest,
                    now_iso,
                    wrap_id,
                ),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    row = get_by_id(conn, wrap_id)
    assert row is not None
    return ClaimResult(row=row, claimed=True)


@privacy_egress_fenced
def complete(
    conn: sqlite3.Connection,
    *,
    wrap_id: str,
    lease_token: str,
    input_digest: str,
    window_start_utc: str,
    window_end_utc: str,
    workflow_version: int,
    coverage_status: str,
    output: dict[str, Any],
    sources: list[EvidenceRef],
    validate_input_current: Callable[[], None],
) -> DailyWrapRow:
    """Publish a revision from one authoritative SQLite snapshot.

    ``validate_input_current`` runs after ``BEGIN IMMEDIATE`` and before any
    revision write. It must rebuild the caller's full semantic input and raise
    when its digest changed. Rechecking immediately before this function would
    leave a TOCTOU window in which a new timeline/event source could arrive.
    """
    now = datetime.now().astimezone().isoformat()
    output_json = json.dumps(output, ensure_ascii=False, sort_keys=True)
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = get_by_id(conn, wrap_id)
        if (
            current is None
            or current.status != "running"
            or current.lease_token != lease_token
            or current.input_digest != input_digest
        ):
            conn.execute("ROLLBACK")
            raise DailyWrapLostLease("daily wrap lease or input changed before publish")
        validate_input_current()
        item_sources = _output_item_sources(output)
        if any(
            not provenance_store.is_current(conn, source) for source in [*sources, *item_sources]
        ):
            conn.execute("ROLLBACK")
            raise DailyWrapLostLease("daily wrap source was deleted or changed before publish")
        revision = current.revision + 1
        conn.execute(
            """
            INSERT INTO daily_wrap_revisions(
                wrap_id, revision, local_date, timezone, scope,
                window_start_utc, window_end_utc, workflow_version,
                input_digest, coverage_status, source_digest, output_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (
                wrap_id,
                revision,
                current.local_date,
                current.timezone,
                current.scope,
                window_start_utc,
                window_end_utc,
                workflow_version,
                input_digest,
                coverage_status,
                output_json,
                now,
            ),
        )
        result = conn.execute(
            """
            UPDATE daily_wrap_jobs
               SET window_start_utc=?, window_end_utc=?, workflow_version=?,
                   status='succeeded', coverage_status=?, output_json=?,
                   revision=?, published_input_digest=?,
                   lease_token=NULL, lease_expires_at=NULL,
                   updated_at=?, completed_at=?, last_error=''
             WHERE id=? AND status='running' AND lease_token=? AND input_digest=?
            """,
            (
                window_start_utc,
                window_end_utc,
                workflow_version,
                coverage_status,
                output_json,
                revision,
                input_digest,
                now,
                now,
                wrap_id,
                lease_token,
                input_digest,
            ),
        )
        if result.rowcount != 1:
            raise DailyWrapLostLease("daily wrap changed during publish")
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="daily_wrap", id=wrap_id),
            sources=sources,
        )
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(
                kind="daily_wrap_revision",
                id=f"{wrap_id}:r{revision}",
                path=wrap_id,
            ),
            sources=sources,
        )
        revision_ref = EvidenceRef(
            kind="daily_wrap_revision",
            id=f"{wrap_id}:r{revision}",
            path=wrap_id,
        )
        persisted_revision_sources = provenance_store.direct_sources(
            conn,
            revision_ref,
        )
        digest_update = conn.execute(
            """
            UPDATE daily_wrap_revisions
               SET source_digest=?
             WHERE wrap_id=? AND revision=? AND source_digest=''
            """,
            (
                daily_wrap_sources_digest(persisted_revision_sources),
                wrap_id,
                revision,
            ),
        )
        if digest_update.rowcount != 1:
            raise DailyWrapLostLease("daily wrap revision source binding changed")
        conn.execute(
            """
            DELETE FROM provenance_edges
             WHERE subject_kind='daily_wrap_item' AND subject_path=?
            """,
            (wrap_id,),
        )
        for category in ("completed", "progressed", "open", "blocked", "needs_review"):
            raw_items = output.get(category, [])
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                raw_refs = item.get("evidence", [])
                item_sources: list[EvidenceRef] = []
                if isinstance(raw_refs, list):
                    for raw_ref in raw_refs:
                        if not isinstance(raw_ref, dict):
                            continue
                        item_sources.append(EvidenceRef.from_dict(raw_ref))
                provenance_store.replace_sources(
                    conn,
                    subject=EvidenceRef(
                        kind="daily_wrap_item",
                        id=str(item["id"]),
                        path=wrap_id,
                    ),
                    # The provider saw the wrap's complete context, not only
                    # the citations it chose for this item. Conservatively
                    # inherit the same authorization closure as the wrap.
                    sources=sources,
                )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    row = get_by_id(conn, wrap_id)
    assert row is not None
    return row


def revision_sources_are_current(
    conn: sqlite3.Connection,
    revision_ref: EvidenceRef,
) -> bool:
    """Verify one immutable revision against its exact projected edge set."""
    if (
        revision_ref.kind != "daily_wrap_revision"
        or not revision_ref.path
        or not revision_ref.id.startswith(f"{revision_ref.path}:r")
    ):
        return False
    try:
        revision = int(revision_ref.id.rsplit(":r", 1)[1])
    except (IndexError, ValueError):
        return False
    if revision_ref.id != f"{revision_ref.path}:r{revision}" or revision < 1:
        return False
    row = conn.execute(
        """
        SELECT source_digest FROM daily_wrap_revisions
         WHERE wrap_id=? AND revision=?
        """,
        (revision_ref.path, revision),
    ).fetchone()
    if row is None or not isinstance(row["source_digest"], str) or not row["source_digest"]:
        return False
    try:
        sources = provenance_store.direct_sources_checked(conn, revision_ref)
        if sources is None:
            return False
        expected = daily_wrap_sources_digest(sources)
    except (TypeError, ValueError):
        return False
    return row["source_digest"] == expected


@privacy_egress_fenced
def fail(
    conn: sqlite3.Connection,
    *,
    wrap_id: str,
    lease_token: str,
    input_digest: str,
    error: str,
) -> DailyWrapRow | None:
    now = datetime.now().astimezone().isoformat()
    conn.execute(
        """
        UPDATE daily_wrap_jobs
           SET status='failed', lease_token=NULL, lease_expires_at=NULL,
               updated_at=?, last_error=?
         WHERE id=? AND status='running' AND lease_token=? AND input_digest=?
        """,
        (now, error[:2000], wrap_id, lease_token, input_digest),
    )
    return get_by_id(conn, wrap_id)


def cancel_claim(
    conn: sqlite3.Connection,
    *,
    wrap_id: str,
    lease_token: str,
) -> DailyWrapRow | None:
    """Revoke one scheduler-owned lease without waiting for its worker thread."""
    now = datetime.now().astimezone().isoformat()
    conn.execute(
        """
        UPDATE daily_wrap_jobs
           SET status='failed', lease_token=NULL, lease_expires_at=NULL,
               updated_at=?, last_error='DailyWrapCancelled: daemon shutdown'
         WHERE id=? AND status='running' AND lease_token=?
        """,
        (now, wrap_id, lease_token),
    )
    return get_by_id(conn, wrap_id)


@privacy_egress_fenced
def purge(conn: sqlite3.Connection, wrap_id: str) -> None:
    conn.execute(
        """
        DELETE FROM provenance_edges
         WHERE subject_path=?
           AND subject_kind IN ('daily_wrap_item', 'daily_wrap_revision')
        """,
        (wrap_id,),
    )
    conn.execute("DELETE FROM daily_wrap_revisions WHERE wrap_id=?", (wrap_id,))
    conn.execute("DELETE FROM daily_wrap_jobs WHERE id=?", (wrap_id,))
    provenance_store.delete_subject(conn, EvidenceRef(kind="daily_wrap", id=wrap_id))


def _lease_is_live(row: DailyWrapRow, now: datetime) -> bool:
    if not row.lease_expires_at:
        return False
    try:
        expires = datetime.fromisoformat(row.lease_expires_at)
    except ValueError:
        return False
    if expires.tzinfo is None or expires.utcoffset() is None:
        expires = expires.astimezone()
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.astimezone()
    return expires.astimezone(UTC) > now.astimezone(UTC)


def _to_row(row: sqlite3.Row) -> DailyWrapRow:
    output: dict[str, Any] | None = None
    if row["output_json"]:
        try:
            raw = json.loads(row["output_json"])
            output = raw if isinstance(raw, dict) else None
        except json.JSONDecodeError:
            output = None
    return DailyWrapRow(
        id=row["id"],
        local_date=row["local_date"],
        timezone=row["timezone"],
        scope=row["scope"],
        window_start_utc=row["window_start_utc"],
        window_end_utc=row["window_end_utc"],
        workflow_version=int(row["workflow_version"]),
        status=row["status"],
        coverage_status=row["coverage_status"],
        attempt_count=int(row["attempt_count"]),
        lease_token=row["lease_token"],
        lease_expires_at=row["lease_expires_at"],
        input_digest=row["input_digest"],
        published_input_digest=row["published_input_digest"] or "",
        output=output,
        revision=int(row["revision"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        last_error=row["last_error"] or "",
    )


def _output_item_sources(output: dict[str, Any]) -> list[EvidenceRef]:
    result: list[EvidenceRef] = []
    for category in ("completed", "progressed", "open", "blocked", "needs_review"):
        items = output.get(category, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_refs = item.get("evidence", [])
            if not isinstance(raw_refs, list):
                continue
            for raw_ref in raw_refs:
                if isinstance(raw_ref, dict):
                    result.append(EvidenceRef.from_dict(raw_ref))
    return result
