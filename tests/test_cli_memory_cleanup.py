from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openchronicle import cli
from openchronicle.config import Config
from openchronicle.daily_wrap import store as daily_wrap_store
from openchronicle.mcp import server as mcp_server
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import (
    EvidenceRef,
    timeline_block_digest,
)
from openchronicle.services.memory import MemoryService
from openchronicle.store import entries as entries_mod
from openchronicle.store import files as files_mod
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store


def _seed_timeline_source(conn, block_id: str) -> EvidenceRef:
    start = "2026-04-21T10:00:00+00:00"
    end = "2026-04-21T10:01:00+00:00"
    block = timeline_store.TimelineBlock(
        id=block_id,
        start_time=datetime.fromisoformat(start),
        end_time=datetime.fromisoformat(end),
        timezone="UTC",
        created_at=datetime.fromisoformat(end),
    )
    timeline_store.insert(conn, block)
    provenance_store.replace_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id=block_id),
        sources=[
            EvidenceRef(
                kind="observation",
                id=f"source-{block_id}",
                path=f"source-{block_id}.json",
                timestamp=datetime(2026, 4, 21, 10, tzinfo=UTC).isoformat(),
                content_hash=f"digest-{block_id}",
            )
        ],
    )
    return EvidenceRef(
        kind="timeline_block",
        id=block_id,
        content_hash=timeline_block_digest(start=start, end=end, entries=[], apps=[]),
    )


def test_clean_memory_serializes_with_concurrent_create(ac_root: Path, monkeypatch) -> None:
    """Clean completes as one store mutation before a waiting writer proceeds."""
    before_name = "topic-before-clean.md"
    after_name = "topic-after-clean.md"
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name=before_name, description="before clean", tags=["topic"])
        entries_mod.append_entry(conn, name=before_name, content="removed by clean", tags=["topic"])

    real_review_lock = files_mod.review_operation_lock
    clean_inside_lock = threading.Event()
    create_lock_attempted = threading.Event()
    release_clean = threading.Event()
    create_done = threading.Event()
    clean_results: list[tuple[int, int]] = []
    errors: list[BaseException] = []

    @contextmanager
    def paused_review_lock():
        is_cleaner = threading.current_thread().name == "clean-memory"
        if not is_cleaner:
            create_lock_attempted.set()
        with real_review_lock():
            if is_cleaner:
                clean_inside_lock.set()
                if not release_clean.wait(timeout=5):
                    raise TimeoutError("test did not release memory cleanup")
            yield

    monkeypatch.setattr(files_mod, "review_operation_lock", paused_review_lock)

    def clean_worker() -> None:
        try:
            clean_results.append(cli._clean_memory())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def create_worker() -> None:
        try:
            with fts.cursor() as conn:
                entries_mod.create_file(
                    conn,
                    name=after_name,
                    description="created after clean",
                    tags=["topic"],
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            create_done.set()

    clean_thread = threading.Thread(target=clean_worker, name="clean-memory")
    create_thread = threading.Thread(target=create_worker, name="create-after-clean")
    clean_thread.start()
    assert clean_inside_lock.wait(timeout=5)
    create_thread.start()
    assert create_lock_attempted.wait(timeout=5)
    try:
        assert not create_done.wait(timeout=0.1), "create bypassed memory cleanup lock"
    finally:
        release_clean.set()

    clean_thread.join(timeout=10)
    create_thread.join(timeout=10)
    assert not clean_thread.is_alive()
    assert not create_thread.is_alive()
    assert errors == []
    assert clean_results == [(1, 1)]

    assert not files_mod.memory_path(before_name).exists()
    assert files_mod.memory_path(after_name).exists()
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        rows = conn.execute("SELECT path FROM files").fetchall()
    assert [row["path"] for row in rows] == [after_name]


def test_clean_memory_keeps_markdown_if_index_clear_cannot_start(
    ac_root: Path, monkeypatch
) -> None:
    name = "topic-private-clean.md"
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name=name, description="private", tags=["topic"])
        entry_id = entries_mod.append_entry(
            conn, name=name, content="SEARCHABLE_PRIVATE_MARKER", tags=["topic"]
        )
    path = files_mod.memory_path(name)
    real_cursor = fts.cursor

    @contextmanager
    def unavailable_index():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(fts, "cursor", unavailable_index)
    with pytest.raises(RuntimeError, match="database unavailable"):
        cli._clean_memory()
    monkeypatch.setattr(fts, "cursor", real_cursor)

    assert path.exists()
    with fts.cursor() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM entries WHERE id=?", (entry_id,)).fetchone()[0] == 1
        )


def test_clean_memory_unlink_failure_stays_hidden_from_read_and_rebuild(
    ac_root: Path, monkeypatch
) -> None:
    name = "topic-private-unlink.md"
    marker = "MEMORY_UNLINK_PRIVATE_MARKER"
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name=name, description="private", tags=["topic"])
        entries_mod.append_entry(conn, name=name, content=marker, tags=["topic"])
    path = files_mod.memory_path(name)
    real_unlink = Path.unlink

    def fail_target(target: Path, *args, **kwargs) -> None:
        if target == path:
            raise PermissionError("immutable memory")
        real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)
    with pytest.raises(RuntimeError, match="memory cleanup incomplete"):
        cli._clean_memory()

    assert path.exists()
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=name)
        assert mcp_server._read_memory(conn, cfg=Config(), path=name) == {
            "error": f"file not found: {name}"
        }
        entries_mod.rebuild_index(conn)
        assert mcp_server._search(conn, cfg=Config(), query=marker)["results"] == []


def test_clean_memory_removes_crash_orphan_temp(ac_root: Path) -> None:
    name = "topic-orphan-temp.md"
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name=name, description="private", tags=["topic"])
        entries_mod.append_entry(conn, name=name, content="PRIVATE_MEMORY_MARKER", tags=["topic"])
    path = files_mod.memory_path(name)
    orphan = path.parent / f".{path.name}.deadbeef.tmp"
    orphan.write_text("SENSITIVE_CRASH_COPY", encoding="utf-8")

    assert files_mod.is_memory_temp_name(orphan.name) is True
    assert cli._clean_memory() == (2, 1)
    assert not path.exists()
    assert not orphan.exists()


def test_clean_memory_clears_candidates_wraps_and_provenance(ac_root: Path) -> None:
    with fts.cursor() as conn:
        source = _seed_timeline_source(conn, "tlb-clean")
        entries_mod.create_file(
            conn, name="project-clean.md", description="private", tags=["project"]
        )
        candidate = MemoryService(conn).propose_candidate(
            kind="fact",
            target_path="project-clean.md",
            content="PRIVATE_CANDIDATE_PAYLOAD",
            tags=["private"],
            evidence=[source],
        )
        claim = daily_wrap_store.claim(
            conn,
            local_date="2026-04-21",
            timezone="UTC",
            scope="default",
            window_start_utc="2026-04-21T00:00:00+00:00",
            window_end_utc="2026-04-22T00:00:00+00:00",
            workflow_version=1,
            coverage_status="partial",
            input_digest="clean-digest",
            lease_token="clean-lease",
        )
        daily_wrap_store.complete(
            conn,
            wrap_id=claim.row.id,
            lease_token="clean-lease",
            input_digest="clean-digest",
            window_start_utc="2026-04-21T00:00:00+00:00",
            window_end_utc="2026-04-22T00:00:00+00:00",
            workflow_version=1,
            coverage_status="partial",
            output={
                "completed": [],
                "progressed": [],
                "open": [],
                "blocked": [],
                "needs_review": [],
            },
            sources=[source],
            validate_input_current=lambda: None,
        )
        assert candidate.id

    cli._clean_memory()
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM daily_wrap_jobs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM daily_wrap_revisions").fetchone()[0] == 0
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM provenance_edges
                 WHERE subject_kind != 'timeline_block'
                    OR source_kind != 'observation'
                """
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM provenance_edges").fetchone()[0] == 1


def test_clean_timeline_invalidates_wraps_and_conflicts_pending_candidates(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        source = _seed_timeline_source(conn, "tlb-delete")
        candidate = MemoryService(conn).propose_candidate(
            kind="fact",
            target_path="project-clean.md",
            content="Timeline-derived candidate.",
            tags=["project"],
            evidence=[source],
        )
        claim = daily_wrap_store.claim(
            conn,
            local_date="2026-04-21",
            timezone="UTC",
            scope="default",
            window_start_utc="2026-04-21T00:00:00+00:00",
            window_end_utc="2026-04-22T00:00:00+00:00",
            workflow_version=1,
            coverage_status="ready",
            input_digest="timeline-clean",
            lease_token="timeline-clean",
        )
        daily_wrap_store.complete(
            conn,
            wrap_id=claim.row.id,
            lease_token="timeline-clean",
            input_digest="timeline-clean",
            window_start_utc="2026-04-21T00:00:00+00:00",
            window_end_utc="2026-04-22T00:00:00+00:00",
            workflow_version=1,
            coverage_status="ready",
            output={
                "completed": [],
                "progressed": [],
                "open": [],
                "blocked": [],
                "needs_review": [],
            },
            sources=[source],
            validate_input_current=lambda: None,
        )

    result = CliRunner().invoke(cli.app, ["clean", "timeline", "--yes"])
    assert result.exit_code == 0, result.output
    assert "1 timeline block(s)" in result.output
    assert "1 Daily Wrap(s)" in result.output
    assert "1 wrap revision(s)" in result.output
    assert "1 dependent pending/conflict candidate(s)" in result.output
    with fts.cursor() as conn:
        updated = MemoryService(conn).get_candidate(candidate.id)
        assert updated is not None
        assert updated.status == "conflict"
        assert "explicitly deleted" in updated.last_error
        assert conn.execute("SELECT COUNT(*) FROM daily_wrap_jobs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0] == 0
        assert (
            conn.execute(
                """
            SELECT COUNT(*) FROM provenance_edges
             WHERE subject_kind='timeline_block' OR source_kind='timeline_block'
            """
            ).fetchone()[0]
            == 0
        )
