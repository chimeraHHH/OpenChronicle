from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef

LEGACY_CANDIDATE_SCHEMA = """
CREATE TABLE memory_candidates (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT 'append',
    target_path TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL,
    conflict_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    applied_entry_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT,
    review_reason TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT ''
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_legacy_candidate(conn: sqlite3.Connection, *, candidate_id: str = "mc-legacy") -> None:
    content = "Legacy grounded candidate."
    conn.execute(
        """
        INSERT INTO memory_candidates(
            id, idempotency_key, kind, operation, target_path, content,
            content_hash, tags_json, confidence, conflict_key, status,
            created_at, updated_at
        ) VALUES (?, ?, 'fact', 'append', 'project-legacy.md', ?, ?,
                  '["legacy", "grounded"]', 0.75, 'legacy-key', 'pending',
                  '2026-08-08T00:00:00+00:00', '2026-08-08T00:00:00+00:00')
        """,
        (
            candidate_id,
            f"idem-{candidate_id}",
            content,
            hashlib.sha256(content.encode()).hexdigest(),
        ),
    )


def _source() -> EvidenceRef:
    return EvidenceRef(
        kind="memory_entry",
        id="legacy-source",
        path="project-source.md",
        timestamp="2026-08-07T23:59:00+00:00",
        content_hash=hashlib.sha256(b"Legacy source.").hexdigest(),
    )


def _seed_legacy_database(path: Path) -> None:
    conn = _connect(path)
    try:
        conn.executescript(LEGACY_CANDIDATE_SCHEMA)
        provenance_store.ensure_schema(conn)
        _seed_legacy_candidate(conn)
        provenance_store.record_sources(
            conn,
            subject=EvidenceRef(kind="memory_candidate", id="mc-legacy"),
            sources=[_source()],
        )
    finally:
        conn.close()


def test_candidate_migration_resumes_when_columns_exist_without_marker(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "interrupted-candidate.db"
    _seed_legacy_database(db_path)
    conn = _connect(db_path)
    try:
        for name, declaration in (
            ("proposal_digest", "TEXT NOT NULL DEFAULT ''"),
            ("projection_digest", "TEXT NOT NULL DEFAULT ''"),
            ("producer_run_key", "TEXT NOT NULL DEFAULT ''"),
            ("proposal_slot", "INTEGER NOT NULL DEFAULT 0"),
        ):
            conn.execute(f"ALTER TABLE memory_candidates ADD COLUMN {name} {declaration}")
        conn.execute(
            """
            UPDATE memory_candidates
               SET proposal_digest='partial-proposal',
                   projection_digest='partial-projection'
            """
        )

        candidate_store.ensure_schema(conn)

        candidate = candidate_store.get(conn, "mc-legacy")
        assert candidate is not None
        assert candidate_store.projection_is_current(candidate)
        assert candidate_store.proposal_is_current(candidate, [_source()])
        assert (
            conn.execute("SELECT COUNT(*) FROM memory_candidate_schema_migrations").fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_candidate_migration_marker_prevents_blank_digest_self_healing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "one-time-candidate.db"
    _seed_legacy_database(db_path)
    conn = _connect(db_path)
    try:
        candidate_store.ensure_schema(conn)
        conn.execute(
            """
            UPDATE memory_candidates
               SET proposal_digest='', projection_digest=''
             WHERE id='mc-legacy'
            """
        )

        candidate_store.ensure_schema(conn)

        candidate = candidate_store.get(conn, "mc-legacy")
        assert candidate is not None
        assert candidate.proposal_digest == ""
        assert candidate.projection_digest == ""
        assert not candidate_store.projection_is_current(candidate)
        assert not candidate_store.proposal_is_current(candidate, [_source()])
    finally:
        conn.close()


def test_candidate_migration_only_binds_nonempty_valid_provenance(tmp_path: Path) -> None:
    db_path = tmp_path / "candidate-provenance.db"
    conn = _connect(db_path)
    try:
        conn.executescript(LEGACY_CANDIDATE_SCHEMA)
        provenance_store.ensure_schema(conn)
        _seed_legacy_candidate(conn, candidate_id="mc-no-sources")
        _seed_legacy_candidate(conn, candidate_id="mc-unbound-source")
        provenance_store.record_sources(
            conn,
            subject=EvidenceRef(kind="memory_candidate", id="mc-unbound-source"),
            sources=[
                EvidenceRef(
                    kind="memory_entry",
                    id="unbound-source",
                    path="project-unbound.md",
                    content_hash="",
                )
            ],
        )

        candidate_store.ensure_schema(conn)

        for candidate_id in ("mc-no-sources", "mc-unbound-source"):
            candidate = candidate_store.get(conn, candidate_id)
            assert candidate is not None
            assert candidate_store.projection_is_current(candidate)
            assert candidate.proposal_digest == ""
    finally:
        conn.close()


def test_candidate_migration_is_transactional_and_concurrent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollback_path = tmp_path / "rollback-candidate.db"
    _seed_legacy_database(rollback_path)
    conn = _connect(rollback_path)
    real_backfill = candidate_store._backfill_projection_migration

    def interrupted_backfill(transaction: sqlite3.Connection) -> None:
        real_backfill(transaction)
        raise RuntimeError("simulated migration interruption")

    try:
        monkeypatch.setattr(
            candidate_store,
            "_backfill_projection_migration",
            interrupted_backfill,
        )
        with pytest.raises(RuntimeError, match="simulated migration interruption"):
            candidate_store.ensure_schema(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_candidates)")}
        assert "projection_digest" not in columns
        assert (
            conn.execute("SELECT COUNT(*) FROM memory_candidate_schema_migrations").fetchone()[0]
            == 0
        )
    finally:
        conn.close()
        monkeypatch.setattr(candidate_store, "_backfill_projection_migration", real_backfill)

    concurrent_path = tmp_path / "concurrent-candidate.db"
    _seed_legacy_database(concurrent_path)
    barrier = Barrier(2)

    def migrate() -> None:
        worker = _connect(concurrent_path)
        try:
            barrier.wait(timeout=5)
            candidate_store.ensure_schema(worker)
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(migrate) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)

    check = _connect(concurrent_path)
    try:
        candidate = candidate_store.get(check, "mc-legacy")
        assert candidate is not None
        assert candidate_store.projection_is_current(candidate)
        assert candidate_store.proposal_is_current(candidate, [_source()])
        assert (
            check.execute("SELECT COUNT(*) FROM memory_candidate_schema_migrations").fetchone()[0]
            == 1
        )
    finally:
        check.close()
