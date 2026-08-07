from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

import frontmatter
import pytest

from openchronicle.daily_wrap import store as daily_wrap_store
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef, content_digest
from openchronicle.services.memory import MemoryService
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts


def _source() -> EvidenceRef:
    return EvidenceRef(
        kind="memory_entry",
        id="event-source",
        path="event-2026-04-21.md",
        timestamp="2026-04-21T10:00:00+00:00",
        content_hash=content_digest("Grounded source evidence."),
    )


def _ensure_source(conn) -> None:
    if not files_store.memory_path("event-2026-04-21.md").exists():
        entries_store.create_file(
            conn,
            name="event-2026-04-21.md",
            description="source",
            tags=["event"],
        )
    entries_store.append_entry_once(
        conn,
        name="event-2026-04-21.md",
        content="Grounded source evidence.",
        tags=["source"],
        entry_id="event-source",
    )


def _propose(service: MemoryService, *, content: str = "User prefers local tools."):
    _ensure_source(service.conn)
    return service.propose_candidate(
        kind="preference",
        target_path="user-preferences.md",
        content=content,
        tags=["preference", "local-first"],
        evidence=[_source()],
        confidence=0.9,
        conflict_key="tool-storage-preference",
    )


def test_candidate_store_migrates_pre_stage1_schema(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "legacy-candidates.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
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
        )
        """
    )
    try:
        candidate_store.ensure_schema(conn)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(memory_candidates)")
        }
        assert {"proposal_digest", "producer_run_key", "proposal_slot"} <= columns
    finally:
        conn.close()


def test_candidate_is_idempotent_review_first_and_approval_is_deterministic(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=20_000)
        first = _propose(service)
        second = _propose(service)
        assert first.id == second.id
        assert first.status == "pending"
        assert not files_store.memory_path("user-preferences.md").exists()

        accepted = service.approve_candidate(first.id, expected_version=first.version)
        assert accepted.status == "accepted"
        assert accepted.applied_entry_id
        replay = service.approve_candidate(
            first.id, expected_version=accepted.version
        )
        assert replay.applied_entry_id == accepted.applied_entry_id

        parsed = files_store.read_file(files_store.memory_path("user-preferences.md"))
        assert len(parsed.entries) == 1
        assert parsed.entries[0].body == first.content
        assert parsed.entries[0].evidence_refs == [
            EvidenceRef(kind="memory_candidate", id=first.id),
            _source(),
        ]
        indexed = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id=?",
            (accepted.applied_entry_id,),
        ).fetchone()[0]
        assert indexed == 1


def test_candidate_row_and_evidence_edges_commit_atomically(
    ac_root: Path, monkeypatch,
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        _ensure_source(conn)
        real_replace = provenance_store.replace_sources

        def fail_edges(*args, **kwargs):
            raise RuntimeError("edge write failed")

        monkeypatch.setattr(provenance_store, "replace_sources", fail_edges)
        with pytest.raises(RuntimeError, match="edge write failed"):
            _propose(service, content="Atomic proposal.")
        assert conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0] == 0

        monkeypatch.setattr(provenance_store, "replace_sources", real_replace)
        candidate = _propose(service, content="Atomic proposal.")
        assert provenance_store.direct_sources(
            conn, EvidenceRef(kind="memory_candidate", id=candidate.id)
        ) == [_source()]
        provenance_store.delete_subject(
            conn, EvidenceRef(kind="memory_candidate", id=candidate.id)
        )
        replay = _propose(service, content="Atomic proposal.")
        assert replay.id == candidate.id
        assert provenance_store.direct_sources(
            conn, EvidenceRef(kind="memory_candidate", id=candidate.id)
        ) == [_source()]


def test_candidate_edit_uses_cas_and_surfaces_conflicts(ac_root: Path) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        first = _propose(service)
        conflicting = _propose(service, content="User prefers cloud tools.")
        assert conflicting.status == "conflict"

        edited = service.edit_candidate(
            first.id,
            expected_version=first.version,
            content="User strongly prefers local tools.",
            tags=["preference"],
        )
        assert edited.version == first.version + 1
        with pytest.raises(candidate_store.CandidateConflict):
            service.edit_candidate(
                first.id,
                expected_version=first.version,
                content="stale edit",
                tags=["preference"],
            )


def test_classifier_run_slot_replay_preserves_first_visible_proposal(ac_root: Path) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        _ensure_source(conn)
        first = service.propose_candidate(
            kind="preference",
            target_path="user-preferences.md",
            content="First grounded wording.",
            tags=["preference"],
            evidence=[_source()],
            producer_run_key="stable-run",
            proposal_slot=0,
        )
        replay = service.propose_candidate(
            kind="preference",
            target_path="user-preferences.md",
            content="Provider retry changed the wording.",
            tags=["preference"],
            evidence=[_source()],
            producer_run_key="stable-run",
            proposal_slot=0,
        )
        assert replay.id == first.id
        assert replay.content == "First grounded wording."
        assert "preserved first" in replay.last_error
        assert conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0] == 1


def test_approval_replay_accepts_original_version_after_applying_transition(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        candidate = _propose(service)
        applying = candidate_store.transition(
            conn,
            candidate_id=candidate.id,
            expected_version=candidate.version,
            from_statuses=("pending",),
            to_status="applying",
        )
        assert applying.version == candidate.version + 1

        accepted = service.approve_candidate(
            candidate.id,
            expected_version=candidate.version,
        )
        assert accepted.status == "accepted"


def test_approval_fails_closed_when_evidence_is_missing(ac_root: Path) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        candidate = _propose(service)
        provenance_store.delete_subject(
            conn, EvidenceRef(kind="memory_candidate", id=candidate.id)
        )

        with pytest.raises(candidate_store.CandidateConflict, match="no durable evidence"):
            service.approve_candidate(candidate.id, expected_version=candidate.version)
        conflicted = candidate_store.get(conn, candidate.id)
        assert conflicted is not None
        assert conflicted.status == "conflict"
        assert not files_store.memory_path(candidate.target_path).exists()


def test_approval_crash_after_markdown_write_stays_applying_and_repairs(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        candidate = _propose(service)
        real_insert = fts.insert_entry

        def fail_candidate_projection(connection, **kwargs):
            if kwargs.get("id", "").startswith("candidate-"):
                raise RuntimeError("projection crash")
            return real_insert(connection, **kwargs)

        monkeypatch.setattr(fts, "insert_entry", fail_candidate_projection)
        with pytest.raises(RuntimeError, match="projection crash"):
            service.approve_candidate(candidate.id, expected_version=candidate.version)
        applying = candidate_store.get(conn, candidate.id)
        assert applying is not None and applying.status == "applying"
        assert _entry_ids(candidate.target_path) == [
            "candidate-" + hashlib.sha256(candidate.id.encode()).hexdigest()[:20]
        ]

        monkeypatch.setattr(fts, "insert_entry", real_insert)
        accepted = service.approve_candidate(
            candidate.id, expected_version=candidate.version
        )
        assert accepted.status == "accepted"
        assert conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id=?", (accepted.applied_entry_id,)
        ).fetchone()[0] == 1


def test_forget_removes_applying_orphan_after_projection_crash(
    ac_root: Path, monkeypatch,
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        candidate = _propose(service, content="ORPHAN_SECRET")
        real_insert = fts.insert_entry

        def fail_projection(*args, **kwargs):
            raise RuntimeError("projection crashed")

        monkeypatch.setattr(fts, "insert_entry", fail_projection)
        with pytest.raises(RuntimeError, match="projection crashed"):
            service.approve_candidate(candidate.id, expected_version=candidate.version)
        assert _entry_ids(candidate.target_path) == [
            "candidate-" + hashlib.sha256(candidate.id.encode()).hexdigest()[:20]
        ]

        monkeypatch.setattr(fts, "insert_entry", real_insert)
        service.purge_candidate(candidate.id)
        entries_store.rebuild_index(conn)
        assert candidate_store.get(conn, candidate.id) is None
        assert _entry_ids(candidate.target_path) == []
        assert fts.search(conn, query="ORPHAN_SECRET", top_k=5) == []


def test_approve_and_forget_share_cross_process_operation_fence(
    ac_root: Path, monkeypatch,
) -> None:
    with fts.cursor() as conn:
        candidate = _propose(MemoryService(conn), content="FENCED_SECRET")

    entered_append = threading.Event()
    release_append = threading.Event()
    purge_done = threading.Event()
    errors: list[BaseException] = []
    real_append = entries_store.append_entry_once

    def paused_append(*args, **kwargs):
        entered_append.set()
        if not release_append.wait(timeout=5):
            raise TimeoutError("test did not release approval")
        return real_append(*args, **kwargs)

    monkeypatch.setattr(entries_store, "append_entry_once", paused_append)

    def approve_worker() -> None:
        try:
            with fts.cursor() as conn:
                MemoryService(conn).approve_candidate(
                    candidate.id, expected_version=candidate.version
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def purge_worker() -> None:
        try:
            with fts.cursor() as conn:
                MemoryService(conn).purge_candidate(candidate.id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            purge_done.set()

    approve_thread = threading.Thread(target=approve_worker)
    purge_thread = threading.Thread(target=purge_worker)
    approve_thread.start()
    assert entered_append.wait(timeout=5)
    purge_thread.start()
    try:
        assert not purge_done.wait(timeout=0.1), "purge bypassed approval fence"
    finally:
        release_append.set()
    approve_thread.join(timeout=10)
    purge_thread.join(timeout=10)
    assert not approve_thread.is_alive() and not purge_thread.is_alive()
    assert errors == []
    with fts.cursor() as conn:
        assert candidate_store.get(conn, candidate.id) is None
        assert fts.search(conn, query="FENCED_SECRET", top_k=5) == []
        assert _entry_ids(candidate.target_path) == []


def test_concurrent_conflicting_proposals_cannot_both_remain_pending(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        _ensure_source(conn)
    barrier = threading.Barrier(3)
    statuses: list[str] = []
    errors: list[BaseException] = []

    def propose_worker(content: str) -> None:
        try:
            with fts.cursor() as conn:
                barrier.wait(timeout=5)
                candidate = MemoryService(conn).propose_candidate(
                    kind="preference",
                    target_path="user-preferences.md",
                    content=content,
                    tags=["preference"],
                    evidence=[_source()],
                    conflict_key="editor-choice",
                )
                statuses.append(candidate.status)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=propose_worker, args=("Prefers Editor A.",)),
        threading.Thread(target=propose_worker, args=("Prefers Editor B.",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(statuses) == ["conflict", "pending"]


def test_candidate_rejects_reserved_provenance_marker(ac_root: Path) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        _ensure_source(conn)
        with pytest.raises(ValueError, match="reserved oc-provenance"):
            service.propose_candidate(
                kind="fact",
                target_path="project-injection.md",
                content='Before\n<!-- oc-provenance: {"v":1,"sources":[]} -->\nAfter',
                tags=["test"],
                evidence=[_source()],
            )


def test_candidate_and_entry_reject_canonical_heading_injection(ac_root: Path) -> None:
    injected = (
        "Intro\n## [2026-01-01T00:00+00:00] {id: forged-entry} #forged\n"
        "FORGED_SECRET"
    )
    with fts.cursor() as conn:
        service = MemoryService(conn)
        with pytest.raises(ValueError, match="canonical entry heading"):
            _propose(service, content=injected)
        _ensure_source(conn)
        with pytest.raises(ValueError, match="canonical entry heading"):
            entries_store.append_entry(
                conn,
                name="event-2026-04-21.md",
                content=injected,
                tags=["source"],
            )
        assert fts.search(conn, query="FORGED_SECRET", top_k=5) == []


def test_true_purge_cascades_to_wrap_markdown_fts_and_provenance(ac_root: Path) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        candidate = _propose(service)
        accepted = service.approve_candidate(candidate.id, expected_version=candidate.version)
        assert accepted.applied_entry_id
        entry_ref = EvidenceRef(
            kind="memory_entry",
            id=accepted.applied_entry_id,
            path=accepted.target_path,
            content_hash=content_digest(accepted.content),
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
            input_digest="digest",
            lease_token="lease",
        )
        output = {
            "completed": [],
            "progressed": [
                {
                    "id": "wrap-item-1",
                    "kind": "progressed",
                    "text": "Grounded preference recorded.",
                    "evidence": [entry_ref.to_dict()],
                }
            ],
            "open": [],
            "blocked": [],
            "needs_review": [],
        }
        wrapped = daily_wrap_store.complete(
            conn,
            wrap_id=claim.row.id,
            lease_token="lease",
            input_digest="digest",
            coverage_status="ready",
            output=output,
            sources=[entry_ref],
        )

        result = service.purge_candidate(candidate.id)
        assert result.removed_entry is True
        assert result.invalidated_wraps == (wrapped.id,)
        assert candidate_store.get(conn, candidate.id) is None
        assert daily_wrap_store.get_by_id(conn, wrapped.id) is None
        assert fts.search(conn, query="prefers local", top_k=5) == []
        parsed = files_store.read_file(files_store.memory_path("user-preferences.md"))
        assert parsed.entries == []
        assert provenance_store.direct_sources(conn, entry_ref) == []


def test_purge_intent_fences_inflight_wrap_publication(
    ac_root: Path, monkeypatch,
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        candidate = _propose(service, content="Race source secret.")
        accepted = service.approve_candidate(candidate.id, expected_version=candidate.version)
        entry_ref = EvidenceRef(
            kind="memory_entry",
            id=accepted.applied_entry_id or "",
            path=accepted.target_path,
            content_hash=content_digest("Race source secret."),
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
            input_digest="race-digest",
            lease_token="race-lease",
        )

    plan_ready = threading.Event()
    release_plan = threading.Event()
    publish_done = threading.Event()
    errors: list[tuple[str, BaseException]] = []
    real_build = MemoryService._build_purge_plan

    def paused_build(self, root):
        plan = real_build(self, root)
        plan_ready.set()
        if not release_plan.wait(timeout=5):
            raise TimeoutError("test did not release purge plan")
        return plan

    monkeypatch.setattr(MemoryService, "_build_purge_plan", paused_build)

    def purge_worker() -> None:
        try:
            with fts.cursor() as conn:
                MemoryService(conn).purge_candidate(candidate.id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(("purge", exc))

    def publish_worker() -> None:
        try:
            with fts.cursor() as conn:
                daily_wrap_store.complete(
                    conn,
                    wrap_id=claim.row.id,
                    lease_token="race-lease",
                    input_digest="race-digest",
                    coverage_status="ready",
                    output={
                        "completed": [],
                        "progressed": [
                            {
                                "id": "race-item",
                                "text": "RACE_DERIVED_SECRET",
                                "evidence": [entry_ref.to_dict()],
                            }
                        ],
                        "open": [],
                        "blocked": [],
                        "needs_review": [],
                    },
                    sources=[entry_ref],
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(("publish", exc))
        finally:
            publish_done.set()

    purge_thread = threading.Thread(target=purge_worker)
    publish_thread = threading.Thread(target=publish_worker)
    purge_thread.start()
    assert plan_ready.wait(timeout=5)
    publish_thread.start()
    try:
        assert not publish_done.wait(timeout=0.1), "publish bypassed purge transaction"
    finally:
        release_plan.set()
    purge_thread.join(timeout=10)
    publish_thread.join(timeout=10)
    assert not purge_thread.is_alive() and not publish_thread.is_alive()
    assert [kind for kind, _exc in errors] == ["publish"]
    assert isinstance(errors[0][1], daily_wrap_store.DailyWrapLostLease)
    with fts.cursor() as conn:
        assert candidate_store.get(conn, candidate.id) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_wrap_revisions WHERE wrap_id=?",
            (claim.row.id,),
        ).fetchone()[0] == 0
        row = daily_wrap_store.get_by_id(conn, claim.row.id)
        assert row is not None and row.output is None


def test_purge_closure_fences_concurrent_provenance_entry_append(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        candidate = _propose(service, content="CLOSURE_SOURCE_SECRET")
        accepted = service.approve_candidate(
            candidate.id, expected_version=candidate.version
        )
        assert accepted.applied_entry_id
        source_ref = EvidenceRef(
            kind="memory_entry",
            id=accepted.applied_entry_id,
            path=accepted.target_path,
            content_hash=content_digest("CLOSURE_SOURCE_SECRET"),
        )
        entries_store.create_file(
            conn,
            name="project-late-derived.md",
            description="derived test target",
            tags=["derived"],
        )

    plan_ready = threading.Event()
    release_plan = threading.Event()
    append_done = threading.Event()
    errors: list[tuple[str, BaseException]] = []
    real_build = MemoryService._build_purge_plan

    def paused_build(self, root):
        plan = real_build(self, root)
        plan_ready.set()
        if not release_plan.wait(timeout=5):
            raise TimeoutError("test did not release purge closure")
        return plan

    monkeypatch.setattr(MemoryService, "_build_purge_plan", paused_build)

    def purge_worker() -> None:
        try:
            with fts.cursor() as conn:
                MemoryService(conn).purge_candidate(candidate.id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(("purge", exc))

    def append_worker() -> None:
        try:
            with fts.cursor() as conn:
                entries_store.append_entry_once(
                    conn,
                    name="project-late-derived.md",
                    content="LATE_DERIVED_SECRET",
                    tags=["derived"],
                    entry_id="late-derived-entry",
                    evidence_refs=[source_ref],
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(("append", exc))
        finally:
            append_done.set()

    purge_thread = threading.Thread(target=purge_worker)
    append_thread = threading.Thread(target=append_worker)
    purge_thread.start()
    assert plan_ready.wait(timeout=5)
    append_thread.start()
    try:
        assert not append_done.wait(timeout=0.1), "append bypassed purge closure fence"
    finally:
        release_plan.set()
    purge_thread.join(timeout=10)
    append_thread.join(timeout=10)
    assert not purge_thread.is_alive() and not append_thread.is_alive()
    assert [kind for kind, _exc in errors] == ["append"]
    assert isinstance(errors[0][1], ValueError)
    assert "dependency is missing or changed" in str(errors[0][1])

    with fts.cursor() as conn:
        assert fts.search(conn, query="LATE_DERIVED_SECRET", top_k=5) == []
        assert _entry_ids("project-late-derived.md") == []
        entries_store.rebuild_index(conn)
        assert fts.search(conn, query="LATE_DERIVED_SECRET", top_k=5) == []
        assert provenance_store.direct_sources(
            conn,
            EvidenceRef(
                kind="memory_entry",
                id="late-derived-entry",
                path="project-late-derived.md",
            ),
        ) == []


def test_purge_tombstone_prevents_rebuild_resurrection_after_crash(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        candidate = _propose(service)
        accepted = service.approve_candidate(candidate.id, expected_version=candidate.version)
        assert accepted.applied_entry_id
        real_delete = entries_store.delete_entry

        def injected_crash(*args, **kwargs):
            raise RuntimeError("crash after purge intent")

        monkeypatch.setattr(entries_store, "delete_entry", injected_crash)
        with pytest.raises(RuntimeError, match="purge intent"):
            service.purge_candidate(candidate.id)
        assert candidate_store.is_tombstoned(
            conn,
            kind="memory_entry",
            artifact_id=accepted.applied_entry_id,
            path=accepted.target_path,
        )

        entries_store.rebuild_index(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id=?",
            (accepted.applied_entry_id,),
        ).fetchone()[0] == 0

        monkeypatch.setattr(entries_store, "delete_entry", real_delete)
        resumed = service.resume_pending_purges()
        assert [result.candidate_id for result in resumed] == [candidate.id]
        assert candidate_store.list_tombstones(conn) == []
        assert candidate_store.get(conn, candidate.id) is None
        parsed = files_store.read_file(files_store.memory_path("user-preferences.md"))
        assert parsed.entries == []


def test_purge_replay_clears_fts_when_markdown_was_already_replaced(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        candidate = _propose(service, content="SECRET_AFTER_RENAME")
        accepted = service.approve_candidate(candidate.id, expected_version=candidate.version)
        assert accepted.applied_entry_id
        path = files_store.memory_path(candidate.target_path)
        post = frontmatter.load(path)
        post.content = ""
        post.metadata["entry_count"] = 0
        files_store.atomic_write_text(path, frontmatter.dumps(post) + "\n")
        assert fts.search(conn, query="SECRET_AFTER_RENAME", top_k=5)

        service.purge_candidate(candidate.id)
        assert fts.search(conn, query="SECRET_AFTER_RENAME", top_k=5) == []
        assert conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id=?", (accepted.applied_entry_id,)
        ).fetchone()[0] == 0
        assert candidate_store.list_tombstones(conn) == []


def test_purge_cascades_through_derived_candidates_and_entries(ac_root: Path) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        first = _propose(service, content="Root private fact.")
        first = service.approve_candidate(first.id, expected_version=first.version)
        assert first.applied_entry_id
        second = service.propose_candidate(
            kind="fact",
            target_path="project-derived.md",
            content="Derived private fact.",
            tags=["derived"],
            evidence=[
                EvidenceRef(
                    kind="memory_entry",
                    id=first.applied_entry_id,
                    path=first.target_path,
                    content_hash=content_digest("Root private fact."),
                )
            ],
        )
        second = service.approve_candidate(second.id, expected_version=second.version)
        assert second.applied_entry_id

        service.purge_candidate(first.id)
        assert candidate_store.get(conn, first.id) is None
        assert candidate_store.get(conn, second.id) is None
        assert fts.search(conn, query="private fact", top_k=10) == []
        assert _entry_ids(first.target_path) == []
        assert _entry_ids(second.target_path) == []


def test_rebuild_resolves_valid_memory_dependencies_independent_of_file_order(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        root = _propose(service, content="Root rebuild fact.")
        root = service.approve_candidate(root.id, expected_version=root.version)
        assert root.applied_entry_id
        derived = service.propose_candidate(
            kind="fact",
            target_path="project-alphabetically-first.md",
            content="Derived rebuild fact.",
            tags=["derived"],
            evidence=[
                EvidenceRef(
                    kind="memory_entry",
                    id=root.applied_entry_id,
                    path=root.target_path,
                    content_hash=content_digest("Root rebuild fact."),
                )
            ],
        )
        derived = service.approve_candidate(
            derived.id, expected_version=derived.version
        )
        assert derived.applied_entry_id

        files_count, entries_count = entries_store.rebuild_index(conn)
        assert files_count >= 3
        assert entries_count >= 3
        assert fts.search(conn, query="Derived rebuild fact", top_k=5)
        assert provenance_store.direct_sources(
            conn,
            EvidenceRef(
                kind="memory_entry",
                id=derived.applied_entry_id,
                path=derived.target_path,
            ),
        )[1].id == root.applied_entry_id


def test_supersede_is_provenance_linked_and_purged_with_accepted_source(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        root = _propose(service, content="SUPERSEDE_ROOT_SECRET")
        root = service.approve_candidate(root.id, expected_version=root.version)
        assert root.applied_entry_id

        replacement_id = entries_store.supersede_entry(
            conn,
            name=root.target_path,
            old_entry_id=root.applied_entry_id,
            new_content="SUPERSEDE_REPLACEMENT_SECRET",
            reason="SUPERSEDE_REASON_SECRET",
            tags=["replacement"],
        )
        replacement_ref = EvidenceRef(
            kind="memory_entry", id=replacement_id, path=root.target_path
        )
        sources = provenance_store.direct_sources(conn, replacement_ref)
        assert [(source.path, source.id) for source in sources] == [
            (root.target_path, root.applied_entry_id)
        ]
        assert "SUPERSEDE_REASON_SECRET" not in files_store.memory_path(
            root.target_path
        ).read_text(encoding="utf-8")

        service.purge_candidate(root.id)
        assert _entry_ids(root.target_path) == []
        assert fts.search(conn, query="SUPERSEDE_REPLACEMENT_SECRET", top_k=5) == []
        entries_store.rebuild_index(conn)
        assert fts.search(conn, query="SUPERSEDE_REPLACEMENT_SECRET", top_k=5) == []


def test_purge_scans_markdown_for_crash_orphan_missing_projection(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        root = _propose(service, content="ORPHAN_ROOT_SECRET")
        root = service.approve_candidate(root.id, expected_version=root.version)
        assert root.applied_entry_id
        source_ref = EvidenceRef(
            kind="memory_entry",
            id=root.applied_entry_id,
            path=root.target_path,
            content_hash=content_digest("ORPHAN_ROOT_SECRET"),
        )
        entries_store.create_file(
            conn,
            name="project-crash-orphan.md",
            description="crash orphan",
            tags=["project"],
        )
        real_insert = fts.insert_entry

        def fail_orphan_projection(connection, **kwargs):
            if kwargs.get("id") == "crash-orphan-entry":
                raise RuntimeError("projection crash")
            return real_insert(connection, **kwargs)

        monkeypatch.setattr(fts, "insert_entry", fail_orphan_projection)
        with pytest.raises(RuntimeError, match="projection crash"):
            entries_store.append_entry_once(
                conn,
                name="project-crash-orphan.md",
                content="ORPHAN_DERIVED_SECRET",
                tags=["derived"],
                entry_id="crash-orphan-entry",
                evidence_refs=[source_ref],
            )
        assert _entry_ids("project-crash-orphan.md") == ["crash-orphan-entry"]
        assert provenance_store.direct_sources(
            conn,
            EvidenceRef(
                kind="memory_entry",
                id="crash-orphan-entry",
                path="project-crash-orphan.md",
            ),
        ) == []

        monkeypatch.setattr(fts, "insert_entry", real_insert)
        service.purge_candidate(root.id)
        assert _entry_ids("project-crash-orphan.md") == []
        assert fts.search(conn, query="ORPHAN_DERIVED_SECRET", top_k=5) == []
        entries_store.rebuild_index(conn)
        assert fts.search(conn, query="ORPHAN_DERIVED_SECRET", top_k=5) == []


def test_forgotten_entry_cannot_seed_a_new_candidate_from_stale_reference(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        first = _propose(service, content="Forgotten source fact.")
        first = service.approve_candidate(first.id, expected_version=first.version)
        stale_ref = EvidenceRef(
            kind="memory_entry",
            id=first.applied_entry_id or "",
            path=first.target_path,
            content_hash=content_digest("Forgotten source fact."),
        )
        service.purge_candidate(first.id)

        with pytest.raises(ValueError, match="evidence is missing or changed"):
            service.propose_candidate(
                kind="fact",
                target_path="project-derived.md",
                content="DERIVED_FROM_FORGOTTEN_SECRET",
                tags=["derived"],
                evidence=[stale_ref],
            )
        assert candidate_store.list_candidates(conn) == []


def test_purge_uses_revision_provenance_to_delete_old_wrap_secret(ac_root: Path) -> None:
    with fts.cursor() as conn:
        service = MemoryService(conn)
        candidate = _propose(service, content="Revision source fact.")
        accepted = service.approve_candidate(candidate.id, expected_version=candidate.version)
        assert accepted.applied_entry_id
        entry_ref = EvidenceRef(
            kind="memory_entry",
            id=accepted.applied_entry_id,
            path=accepted.target_path,
            content_hash=content_digest("Revision source fact."),
        )
        first_claim = daily_wrap_store.claim(
            conn,
            local_date="2026-04-21",
            timezone="UTC",
            scope="default",
            window_start_utc="2026-04-21T00:00:00+00:00",
            window_end_utc="2026-04-22T00:00:00+00:00",
            workflow_version=1,
            coverage_status="ready",
            input_digest="digest-1",
            lease_token="lease-1",
        )
        first = daily_wrap_store.complete(
            conn,
            wrap_id=first_claim.row.id,
            lease_token="lease-1",
            input_digest="digest-1",
            coverage_status="ready",
            output={
                "completed": [],
                "progressed": [
                    {
                        "id": "old-secret-item",
                        "text": "SECRET_FROM_OLD_REVISION",
                        "evidence": [entry_ref.to_dict()],
                    }
                ],
                "open": [],
                "blocked": [],
                "needs_review": [],
            },
            sources=[entry_ref],
        )
        second_claim = daily_wrap_store.claim(
            conn,
            local_date="2026-04-21",
            timezone="UTC",
            scope="default",
            window_start_utc="2026-04-21T00:00:00+00:00",
            window_end_utc="2026-04-22T00:00:00+00:00",
            workflow_version=1,
            coverage_status="ready",
            input_digest="digest-2",
            lease_token="lease-2",
        )
        daily_wrap_store.complete(
            conn,
            wrap_id=second_claim.row.id,
            lease_token="lease-2",
            input_digest="digest-2",
            coverage_status="ready",
            output={
                "completed": [],
                "progressed": [],
                "open": [],
                "blocked": [],
                "needs_review": [],
            },
            sources=[_source()],
        )

        result = service.purge_candidate(candidate.id)
        assert result.invalidated_wraps == (first.id,)
        assert daily_wrap_store.get_by_id(conn, first.id) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_wrap_revisions WHERE wrap_id=?", (first.id,)
        ).fetchone()[0] == 0
        assert "SECRET_FROM_OLD_REVISION" not in "".join(
            str(value)
            for row in conn.execute("SELECT output_json FROM daily_wrap_revisions")
            for value in row
        )


def _entry_ids(path: str) -> list[str]:
    target = files_store.memory_path(path)
    if not target.exists():
        return []
    return [entry.id for entry in files_store.read_file(target).entries]
