from __future__ import annotations

import inspect
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openchronicle import config as config_mod
from openchronicle import paths
from openchronicle.capture import filenames as capture_filenames
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance.models import EvidenceRef, content_digest
from openchronicle.services.memory import MemoryService
from openchronicle.session import store as session_store
from openchronicle.store import entries as entries_mod
from openchronicle.store import files as files_mod
from openchronicle.store import fts
from openchronicle.writer import classifier as classifier_mod
from openchronicle.writer import classifier_delivery, classifier_jobs, session_reducer
from openchronicle.writer import llm as llm_mod


def _now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def _insert_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    start: datetime,
    end: datetime | None = None,
    status: session_store.SessionStatus = "active",
    flush_end: datetime | None = None,
    classified_end: datetime | None = None,
) -> None:
    session_store.insert(
        conn,
        session_store.SessionRow(
            id=session_id,
            start_time=start,
            end_time=end,
            status=status,
        ),
    )
    if flush_end is not None:
        session_store.set_flush_end(conn, session_id, flush_end)
    if classified_end is not None:
        session_store.set_classified_end(conn, session_id, classified_end)


def _mark_reduced(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    end: datetime,
    terminal_entry_id: str = "",
    terminal_path: str = "",
    terminal_noop: bool = False,
) -> None:
    row = session_store.get_by_id(conn, session_id)
    assert row is not None
    if row.status == "active":
        assert session_store.mark_ended(conn, session_id, end)
    session_store.mark_reduced(
        conn,
        session_id,
        terminal_entry_id=terminal_entry_id,
        terminal_path=terminal_path,
        terminal_noop=terminal_noop,
    )


def _coverage_tag(end: datetime) -> str:
    return f"oc-window-end:{capture_filenames.safe_timestamp(end.isoformat())}"


def _create_event_file(name: str) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=name,
            description="classifier delivery test source",
            tags=["event", "session", "daily"],
        )


def _append_event_entry(
    *,
    name: str,
    session_id: str,
    entry_id: str,
    body: str,
    coverage_end: datetime | None,
) -> None:
    tags = ["session", f"sid:{session_id}"]
    if coverage_end is not None:
        tags.append(_coverage_tag(coverage_end))
    with fts.cursor() as conn:
        entries_mod.append_entry_once(
            conn,
            name=name,
            content=body,
            tags=tags,
            entry_id=entry_id,
        )


def _request_periodic(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    end: datetime,
    now: datetime,
) -> classifier_jobs.ClassifierJob:
    job = classifier_jobs.request(
        conn,
        session_id=session_id,
        requested_end=end,
        include_prior_day=False,
        now=now,
    )
    assert job is not None
    return job


def _receipt(summary: str = "committed") -> dict[str, Any]:
    return {
        "committed": True,
        "summary": summary,
        "written_ids": [],
        "created_paths": [],
        "candidate_ids": [],
        "skipped_reason": "",
    }


def _bind_and_commit(
    conn: sqlite3.Connection,
    *,
    job: classifier_jobs.ClassifierJob,
    input_digest: str = "snapshot-v1",
    producer_run_key: str | None = None,
    now: datetime | None = None,
) -> classifier_jobs.ClassifierJob:
    assert job.lease_token is not None
    run_key = producer_run_key or classifier_jobs.make_producer_run_key(job.id)
    classifier_jobs.bind_input(
        conn,
        job_id=job.id,
        lease_token=job.lease_token,
        input_digest=input_digest,
        producer_run_key=run_key,
    )
    return classifier_jobs.record_commit(
        conn,
        job_id=job.id,
        lease_token=job.lease_token,
        producer_run_key=run_key,
        result=_receipt(),
        now=now,
    )


def _tool_call(name: str, args: dict[str, Any], cid: str = "call-1") -> Any:
    return SimpleNamespace(
        id=cid,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(args, ensure_ascii=False),
        ),
    )


def _response(tool_calls: list[Any]) -> Any:
    message = SimpleNamespace(content=None, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


def _commit_response(summary: str = "committed") -> Any:
    return _response([_tool_call("commit", {"summary": summary})])


def test_claim_uses_internal_tokens_and_fences_expired_workers(ac_root: Path) -> None:
    base = _now()
    start = base - timedelta(minutes=20)
    end = base - timedelta(minutes=10)

    assert "lease_token" not in inspect.signature(classifier_jobs.claim).parameters
    with fts.cursor() as conn:
        _insert_session(
            conn,
            session_id="lease-fence",
            start=start,
            flush_end=end,
        )
        job = _request_periodic(
            conn,
            session_id="lease-fence",
            end=end,
            now=base,
        )
        first = classifier_jobs.claim(
            conn,
            job_id=job.id,
            lease_seconds=30,
            now=base,
        ).row
        assert first.lease_token
        classifier_jobs.bind_input(
            conn,
            job_id=first.id,
            lease_token=first.lease_token,
            input_digest="stable-snapshot",
            producer_run_key="stable-run",
        )

        expiry = base + timedelta(seconds=30)
        with pytest.raises(classifier_jobs.ClassifierJobLostLease):
            classifier_jobs.assert_lease(
                conn,
                job_id=first.id,
                lease_token=first.lease_token,
                now=expiry,
            )
        with pytest.raises(classifier_jobs.ClassifierJobLostLease):
            classifier_jobs.record_commit(
                conn,
                job_id=first.id,
                lease_token=first.lease_token,
                producer_run_key="stable-run",
                result=_receipt(),
                now=expiry,
            )
        with pytest.raises(classifier_jobs.ClassifierJobLostLease):
            classifier_jobs.fail(
                conn,
                job_id=first.id,
                lease_token=first.lease_token,
                error="late worker",
                retry_seconds=1,
                now=expiry,
            )

        second = classifier_jobs.claim(
            conn,
            job_id=first.id,
            lease_seconds=30,
            now=expiry,
        ).row
        assert second.lease_token
        assert second.lease_token != first.lease_token
        with pytest.raises(classifier_jobs.ClassifierJobLostLease):
            classifier_jobs.record_commit(
                conn,
                job_id=first.id,
                lease_token=first.lease_token,
                producer_run_key="stable-run",
                result=_receipt(),
                now=expiry + timedelta(seconds=1),
            )


def test_concurrent_claim_has_exactly_one_live_owner(ac_root: Path) -> None:
    base = _now()
    start = base - timedelta(minutes=20)
    end = base - timedelta(minutes=10)
    with fts.cursor() as conn:
        _insert_session(
            conn,
            session_id="concurrent-claim",
            start=start,
            flush_end=end,
        )
        job = _request_periodic(
            conn,
            session_id="concurrent-claim",
            end=end,
            now=base,
        )

    barrier = threading.Barrier(2)

    def worker() -> tuple[str, str]:
        with fts.cursor() as conn:
            barrier.wait()
            try:
                claimed = classifier_jobs.claim(
                    conn,
                    job_id=job.id,
                    lease_seconds=30,
                    now=base,
                )
            except classifier_jobs.ClassifierJobBusy:
                return "busy", ""
            assert claimed.row.lease_token is not None
            return "claimed", claimed.row.lease_token

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in [pool.submit(worker), pool.submit(worker)]]

    assert sorted(status for status, _ in outcomes) == ["busy", "claimed"]
    tokens = [token for status, token in outcomes if status == "claimed"]
    assert len(tokens) == 1 and tokens[0]


def test_claim_freezes_window_and_finalize_creates_contiguous_followup(
    ac_root: Path,
) -> None:
    base = _now()
    start = base - timedelta(minutes=40)
    first_end = base - timedelta(minutes=20)
    requested_end = base - timedelta(minutes=5)
    with fts.cursor() as conn:
        _insert_session(
            conn,
            session_id="frozen-window",
            start=start,
            flush_end=requested_end,
        )
        requested = _request_periodic(
            conn,
            session_id="frozen-window",
            end=first_end,
            now=base,
        )
        first_claim = classifier_jobs.claim(
            conn,
            job_id=requested.id,
            lease_seconds=300,
            now=base,
        ).row

        extended = _request_periodic(
            conn,
            session_id="frozen-window",
            end=requested_end,
            now=base + timedelta(seconds=1),
        )
        assert extended.window_end == first_end
        assert extended.requested_end == requested_end

        reclaimed = classifier_jobs.claim(
            conn,
            job_id=first_claim.id,
            lease_seconds=300,
            now=base + timedelta(seconds=300),
        ).row
        assert reclaimed.lease_token != first_claim.lease_token
        assert reclaimed.window_start == start
        assert reclaimed.window_end == first_end
        assert reclaimed.requested_end == requested_end

        committed = _bind_and_commit(
            conn,
            job=reclaimed,
            now=base + timedelta(seconds=301),
        )
        finalized = classifier_jobs.finalize(
            conn,
            job_id=committed.id,
            now=base + timedelta(seconds=302),
        )
        session = session_store.get_by_id(conn, "frozen-window")

    assert session is not None and session.classified_end == first_end
    assert finalized.completed.status == "succeeded"
    assert finalized.followup is not None
    assert finalized.followup.status == "pending"
    assert finalized.followup.window_start == first_end
    assert finalized.followup.window_end == requested_end
    assert finalized.followup.requested_end == requested_end


def test_committed_receipt_restarts_without_provider_call(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _now()
    start = base - timedelta(minutes=20)
    end = base - timedelta(minutes=10)
    with fts.cursor() as conn:
        _insert_session(
            conn,
            session_id="receipt-restart",
            start=start,
            flush_end=end,
        )
        requested = _request_periodic(
            conn,
            session_id="receipt-restart",
            end=end,
            now=base,
        )
        claimed = classifier_jobs.claim(
            conn,
            job_id=requested.id,
            lease_seconds=300,
            now=base,
        ).row
        committed = _bind_and_commit(conn, job=claimed, now=base)
        assert committed.status == "committed"

    def unexpected_classifier(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a committed receipt must not rerun the classifier")

    def unexpected_provider(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a committed receipt must not call the provider")

    monkeypatch.setattr(classifier_mod, "classify_window", unexpected_classifier)
    monkeypatch.setattr(llm_mod, "call_llm", unexpected_provider)
    cfg = config_mod.load(ac_root / "config.toml")
    result = classifier_delivery.process_job(cfg, committed.id)

    assert result.status == "succeeded"
    with fts.cursor() as conn:
        restarted = classifier_jobs.get(conn, committed.id)
        session = session_store.get_by_id(conn, "receipt-restart")
    assert restarted is not None and restarted.status == "succeeded"
    assert session is not None and session.classified_end == end


def test_finalize_rolls_back_bookmark_when_job_transition_fails(ac_root: Path) -> None:
    base = _now()
    start = base - timedelta(minutes=20)
    end = base - timedelta(minutes=10)
    with fts.cursor() as conn:
        _insert_session(
            conn,
            session_id="atomic-finalize",
            start=start,
            flush_end=end,
        )
        requested = _request_periodic(
            conn,
            session_id="atomic-finalize",
            end=end,
            now=base,
        )
        claimed = classifier_jobs.claim(
            conn,
            job_id=requested.id,
            lease_seconds=300,
            now=base,
        ).row
        committed = _bind_and_commit(conn, job=claimed, now=base)
        conn.executescript(
            """
            CREATE TRIGGER injected_classifier_finalize_failure
            BEFORE UPDATE OF status ON classifier_jobs
            WHEN NEW.status='succeeded'
            BEGIN
                SELECT RAISE(ABORT, 'injected finalize failure');
            END;
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected finalize failure"):
            classifier_jobs.finalize(conn, job_id=committed.id, now=base)

    with fts.cursor() as conn:
        durable_job = classifier_jobs.get(conn, committed.id)
        session = session_store.get_by_id(conn, "atomic-finalize")
    assert durable_job is not None and durable_job.status == "committed"
    assert session is not None and session.classified_end is None


def test_terminal_delivery_uses_exact_entry_and_durable_flush_prefix(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _now()
    start = base - timedelta(minutes=40)
    first_flush = start + timedelta(minutes=10)
    second_flush = start + timedelta(minutes=20)
    end = start + timedelta(minutes=30)
    session_id = "terminal-prefix"
    event_name = f"event-{start.strftime('%Y-%m-%d')}.md"
    _create_event_file(event_name)
    _append_event_entry(
        name=event_name,
        session_id=session_id,
        entry_id="flush-prefix-one",
        body="DURABLE_PREFIX_ONE",
        coverage_end=first_flush,
    )
    _append_event_entry(
        name=event_name,
        session_id=session_id,
        entry_id="flush-prefix-two",
        body="DURABLE_PREFIX_TWO",
        coverage_end=second_flush,
    )
    # The exact terminal entry is selected by its persisted ID even though it
    # intentionally has no periodic coverage tag.
    _append_event_entry(
        name=event_name,
        session_id=session_id,
        entry_id="terminal-exact-entry",
        body="TERMINAL_EXACT_BODY",
        coverage_end=None,
    )

    with fts.cursor() as conn:
        _insert_session(conn, session_id=session_id, start=start)
        _mark_reduced(
            conn,
            session_id=session_id,
            end=end,
            terminal_entry_id="terminal-exact-entry",
            terminal_path=event_name,
        )
        requested = classifier_jobs.request_terminal(
            conn,
            session_id=session_id,
            now=base,
        )
        assert requested is not None and requested.kind == "terminal"

    prompts: list[str] = []

    def fake_call_llm(
        cfg: Any,
        stage: str,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
    ) -> Any:
        assert stage == "classifier"
        prompts.append(messages[1]["content"])
        return _commit_response("terminal prefix committed")

    monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)
    cfg = config_mod.load(ac_root / "config.toml")
    delivered = classifier_delivery.process_job(cfg, requested.id)

    assert delivered.status == "succeeded"
    assert len(prompts) == 1
    assert "DURABLE_PREFIX_ONE" in prompts[0]
    assert "DURABLE_PREFIX_TWO" in prompts[0]
    assert "TERMINAL_EXACT_BODY" in prompts[0]
    with fts.cursor() as conn:
        session = session_store.get_by_id(conn, session_id)
        completed = classifier_jobs.get(conn, requested.id)
    assert session is not None
    assert session.classified_end == end
    assert session.classifier_terminal_pending is False
    assert completed is not None and completed.status == "succeeded"


def test_terminal_noop_commits_without_provider_and_clears_intent(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _now()
    start = base - timedelta(minutes=20)
    end = base - timedelta(minutes=10)
    with fts.cursor() as conn:
        _insert_session(conn, session_id="terminal-noop", start=start)
        _mark_reduced(
            conn,
            session_id="terminal-noop",
            end=end,
            terminal_path=f"event-{start.strftime('%Y-%m-%d')}.md",
            terminal_noop=True,
        )
        requested = classifier_jobs.request_terminal(
            conn,
            session_id="terminal-noop",
            now=base,
        )
        assert requested is not None

    def unexpected_provider(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an empty legacy terminal delivery must not call the provider")

    monkeypatch.setattr(llm_mod, "call_llm", unexpected_provider)
    cfg = config_mod.load(ac_root / "config.toml")
    delivered = classifier_delivery.process_job(cfg, requested.id)

    assert delivered.status == "succeeded"
    assert delivered.result is not None
    assert delivered.result["skipped_reason"] == classifier_jobs.EMPTY_TERMINAL_SKIP
    with fts.cursor() as conn:
        session = session_store.get_by_id(conn, "terminal-noop")
    assert session is not None
    assert session.classified_end == end
    assert session.classifier_terminal_pending is False


def test_terminal_noop_cannot_skip_an_unclassified_flush_prefix(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _now()
    start = base - timedelta(minutes=30)
    flush_end = base - timedelta(minutes=15)
    end = base - timedelta(minutes=5)
    event_name = f"event-{start.strftime('%Y-%m-%d')}.md"
    with fts.cursor() as conn:
        _insert_session(
            conn,
            session_id="terminal-prefix-missing",
            start=start,
            flush_end=flush_end,
        )
        _mark_reduced(
            conn,
            session_id="terminal-prefix-missing",
            end=end,
            terminal_path=event_name,
            terminal_noop=True,
        )
        requested = classifier_jobs.request_terminal(
            conn,
            session_id="terminal-prefix-missing",
            now=base,
        )
        assert requested is not None
        assert requested.allow_empty is False

    def unexpected_provider(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("missing durable flush evidence must fail before provider I/O")

    monkeypatch.setattr(llm_mod, "call_llm", unexpected_provider)
    cfg = config_mod.load(ac_root / "config.toml")
    delivered = classifier_delivery.process_job(cfg, requested.id)

    assert delivered.status == "failed"
    assert "no reducer entry" in delivered.error
    with fts.cursor() as conn:
        session = session_store.get_by_id(conn, "terminal-prefix-missing")
        durable = classifier_jobs.get(conn, requested.id)
    assert session is not None and session.classified_end is None
    assert session.classifier_terminal_pending is True
    assert durable is not None and durable.result is None


def test_empty_receipt_requires_the_typed_terminal_proof(ac_root: Path) -> None:
    base = _now()
    start = base - timedelta(minutes=20)
    end = base - timedelta(minutes=10)
    with fts.cursor() as conn:
        _insert_session(conn, session_id="typed-empty-proof", start=start)
        _mark_reduced(
            conn,
            session_id="typed-empty-proof",
            end=end,
            terminal_path=f"event-{start.strftime('%Y-%m-%d')}.md",
            terminal_noop=True,
        )
        requested = classifier_jobs.request_terminal(
            conn,
            session_id="typed-empty-proof",
            now=base,
        )
        assert requested is not None and requested.allow_empty is True
        claimed = classifier_jobs.claim(
            conn,
            job_id=requested.id,
            lease_seconds=300,
            now=base,
        ).row
        assert claimed.lease_token is not None
        run_key = classifier_jobs.make_producer_run_key(claimed.id)
        classifier_jobs.bind_input(
            conn,
            job_id=claimed.id,
            lease_token=claimed.lease_token,
            input_digest="typed-empty-input",
            producer_run_key=run_key,
        )
        with pytest.raises(ValueError, match="unsupported skip proof"):
            classifier_jobs.record_commit(
                conn,
                job_id=claimed.id,
                lease_token=claimed.lease_token,
                producer_run_key=run_key,
                result={
                    "committed": False,
                    "summary": "",
                    "written_ids": [],
                    "created_paths": [],
                    "candidate_ids": [],
                    "skipped_reason": "no session entries in window",
                },
                now=base,
            )
        durable = classifier_jobs.get(conn, claimed.id)
    assert durable is not None and durable.status == "running"


def test_legacy_cross_midnight_terminal_recovers_exact_entry(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime.fromisoformat("2026-08-07T23:40:00+08:00")
    flush_end = datetime.fromisoformat("2026-08-08T00:10:00+08:00")
    end = datetime.fromisoformat("2026-08-08T00:20:00+08:00")
    session_id = "legacy-cross-midnight"
    legacy_path = "event-2026-08-08.md"
    entry_id = session_reducer._event_entry_id(
        session_id=session_id,
        start_time=flush_end,
        end_time=end,
        is_final=True,
    )
    _create_event_file(legacy_path)
    _append_event_entry(
        name=legacy_path,
        session_id=session_id,
        entry_id=entry_id,
        body="LEGACY_TERMINAL_EXACT_BODY",
        coverage_end=None,
    )
    with fts.cursor() as conn:
        _insert_session(
            conn,
            session_id=session_id,
            start=start,
            flush_end=flush_end,
            classified_end=flush_end,
        )
        _mark_reduced(
            conn,
            session_id=session_id,
            end=end,
            terminal_entry_id=entry_id,
            terminal_path=legacy_path,
        )
        conn.execute(
            """
            UPDATE sessions
               SET classifier_terminal_pending=1,
                   classifier_terminal_entry_id='',
                   classifier_terminal_path='',
                   classifier_terminal_noop=0
             WHERE id=?
            """,
            (session_id,),
        )
        placeholder_id = classifier_jobs.make_id(
            session_id,
            flush_end,
            kind="terminal",
            terminal_entry_id="",
        )
        conn.execute(
            """
            INSERT INTO classifier_jobs(
                id, session_id, kind, terminal_entry_id, event_daily_path,
                window_start, window_end, requested_end, include_prior_day,
                allow_empty, status, created_at, updated_at
            ) VALUES (?, ?, 'terminal', '', 'event-2026-08-07.md',
                      ?, ?, ?, 0, 0, 'pending', ?, ?)
            """,
            (
                placeholder_id,
                session_id,
                flush_end.isoformat(),
                end.isoformat(),
                end.isoformat(),
                end.isoformat(),
                end.isoformat(),
            ),
        )

    assert classifier_delivery.recover_terminal_requests() == 1
    with fts.cursor() as conn:
        session = session_store.get_by_id(conn, session_id)
        requested = classifier_jobs.get_active_for_session(conn, session_id)
        placeholder = classifier_jobs.get(conn, placeholder_id)
    assert session is not None
    assert session.classifier_terminal_entry_id == entry_id
    assert session.classifier_terminal_path == legacy_path
    assert session.classifier_terminal_noop is False
    assert requested is not None and requested.terminal_entry_id == entry_id
    assert requested.event_daily_path == legacy_path
    assert placeholder is None

    prompts: list[str] = []

    def fake_call_llm(
        cfg: Any,
        stage: str,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
    ) -> Any:
        prompts.append(messages[1]["content"])
        return _commit_response("legacy terminal recovered")

    monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)
    cfg = config_mod.load(ac_root / "config.toml")
    delivered = classifier_delivery.process_job(cfg, requested.id)
    assert delivered.status == "succeeded"
    assert len(prompts) == 1 and "LEGACY_TERMINAL_EXACT_BODY" in prompts[0]
    with fts.cursor() as conn:
        session = session_store.get_by_id(conn, session_id)
    assert session is not None and session.classified_end == end


def test_terminal_intent_survives_while_periodic_job_is_active(ac_root: Path) -> None:
    base = _now()
    start = base - timedelta(minutes=30)
    periodic_end = base - timedelta(minutes=15)
    terminal_end = base - timedelta(minutes=5)
    with fts.cursor() as conn:
        _insert_session(
            conn,
            session_id="terminal-blocked",
            start=start,
            flush_end=periodic_end,
        )
        periodic = _request_periodic(
            conn,
            session_id="terminal-blocked",
            end=periodic_end,
            now=base,
        )
        claimed = classifier_jobs.claim(
            conn,
            job_id=periodic.id,
            lease_seconds=300,
            now=base,
        ).row
        _mark_reduced(
            conn,
            session_id="terminal-blocked",
            end=terminal_end,
            terminal_entry_id="blocked-terminal-entry",
            terminal_path="event-terminal-blocked.md",
        )

        blocked = classifier_jobs.request_terminal(
            conn,
            session_id="terminal-blocked",
            now=base,
        )
        pending_session = session_store.get_by_id(conn, "terminal-blocked")
        assert blocked is not None and blocked.id == periodic.id
        assert blocked.kind == "periodic"
        assert pending_session is not None
        assert pending_session.classifier_terminal_pending is True

        committed = _bind_and_commit(conn, job=claimed, now=base)
        classifier_jobs.finalize(conn, job_id=committed.id, now=base)
        terminal = classifier_jobs.request_terminal(
            conn,
            session_id="terminal-blocked",
            now=base,
        )

    assert terminal is not None
    assert terminal.kind == "terminal"
    assert terminal.terminal_entry_id == "blocked-terminal-entry"
    assert terminal.window_start == periodic_end
    assert terminal.window_end == terminal_end


def test_legacy_reduced_sessions_backfill_terminal_intent() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
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
            classified_end TEXT,
            owner_pid INTEGER,
            owner_token TEXT
        );
        INSERT INTO sessions (
            id, start_time, end_time, status, retry_count, last_error,
            created_at, updated_at, classified_end
        ) VALUES
            (
                'legacy-behind',
                '2026-08-07T09:00:00+08:00',
                '2026-08-07T10:00:00+08:00',
                'reduced', 0, '',
                '2026-08-07T09:00:00+08:00',
                '2026-08-07T10:00:00+08:00',
                '2026-08-07T09:30:00+08:00'
            ),
            (
                'legacy-complete',
                '2026-08-07T09:00:00+08:00',
                '2026-08-07T10:00:00+08:00',
                'reduced', 0, '',
                '2026-08-07T09:00:00+08:00',
                '2026-08-07T10:00:00+08:00',
                '2026-08-07T10:00:00+08:00'
            );
        """
    )
    try:
        session_store.ensure_schema(conn)
        classifier_jobs.ensure_schema(conn)
        behind = session_store.get_by_id(conn, "legacy-behind")
        complete = session_store.get_by_id(conn, "legacy-complete")
        assert behind is not None and behind.classifier_terminal_pending is True
        assert complete is not None and complete.classifier_terminal_pending is False

        # Simulate a crash after ALTER TABLE committed but before the first
        # backfill pass. A later schema open must repair the obligation again.
        conn.execute(
            "UPDATE sessions SET classifier_terminal_pending=0 WHERE id='legacy-behind'"
        )
        conn.execute(
            "DELETE FROM session_schema_migrations "
            "WHERE name='classifier-terminal-obligation-v1'"
        )
        session_store.ensure_schema(conn)
        repaired = session_store.get_by_id(conn, "legacy-behind")
        assert repaired is not None and repaired.classifier_terminal_pending is True

        with pytest.raises(classifier_jobs.ClassifierJobGap):
            classifier_jobs.request_terminal(
                conn,
                session_id="legacy-behind",
                now=datetime.fromisoformat("2026-08-07T10:01:00+08:00"),
            )
    finally:
        conn.close()


def test_allow_empty_migration_quarantines_legacy_skip_receipt() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    legacy_schema = classifier_jobs.SCHEMA.replace(
        "    allow_empty INTEGER NOT NULL DEFAULT 0,\n",
        "",
    )
    conn.executescript(legacy_schema)
    now = datetime.fromisoformat("2026-08-07T10:01:00+08:00")
    receipt = {
        "committed": False,
        "summary": "",
        "written_ids": [],
        "created_paths": [],
        "candidate_ids": [],
        "skipped_reason": "no session entries in window",
    }
    conn.execute(
        """
        INSERT INTO classifier_jobs(
            id, session_id, kind, terminal_entry_id, event_daily_path,
            window_start, window_end, requested_end, include_prior_day,
            status, attempt_count, producer_run_key, input_digest,
            result_json, created_at, updated_at, committed_at
        ) VALUES (
            'legacy-skip-job', 'legacy-skip-session', 'terminal', '',
            'event-2026-08-07.md', ?, ?, ?, 0, 'committed', 1,
            'legacy-run', 'legacy-input', ?, ?, ?, ?
        )
        """,
        (
            (now - timedelta(hours=1)).isoformat(),
            now.isoformat(),
            now.isoformat(),
            json.dumps(receipt),
            now.isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    try:
        classifier_jobs.ensure_schema(conn)
        migrated = classifier_jobs.get(conn, "legacy-skip-job")
        due = classifier_jobs.list_due(conn, now=now)
        assert migrated is not None
        assert migrated.status == "failed"
        assert migrated.result is None
        assert migrated.producer_run_key == ""
        assert [job.id for job in due] == ["legacy-skip-job"]
    finally:
        conn.close()


def test_retry_rejects_changed_bound_input_without_advancing_bookmark(
    ac_root: Path,
) -> None:
    base = _now()
    start = base - timedelta(minutes=20)
    end = base - timedelta(minutes=10)
    with fts.cursor() as conn:
        _insert_session(
            conn,
            session_id="input-change",
            start=start,
            flush_end=end,
        )
        requested = _request_periodic(
            conn,
            session_id="input-change",
            end=end,
            now=base,
        )
        first = classifier_jobs.claim(
            conn,
            job_id=requested.id,
            lease_seconds=30,
            now=base,
        ).row
        assert first.lease_token is not None
        classifier_jobs.bind_input(
            conn,
            job_id=first.id,
            lease_token=first.lease_token,
            input_digest="snapshot-before-retry",
            producer_run_key="stable-run",
        )
        classifier_jobs.fail(
            conn,
            job_id=first.id,
            lease_token=first.lease_token,
            error="provider disconnected",
            retry_seconds=1,
            now=base,
        )
        retry = classifier_jobs.claim(
            conn,
            job_id=first.id,
            lease_seconds=30,
            now=base + timedelta(seconds=2),
        ).row
        assert retry.lease_token is not None
        with pytest.raises(classifier_jobs.ClassifierJobInputChanged):
            classifier_jobs.bind_input(
                conn,
                job_id=retry.id,
                lease_token=retry.lease_token,
                input_digest="snapshot-after-retry",
                producer_run_key="stable-run",
            )
        durable = classifier_jobs.get(conn, retry.id)
        session = session_store.get_by_id(conn, "input-change")

    assert durable is not None
    assert durable.input_digest == "snapshot-before-retry"
    assert session is not None and session.classified_end is None


def test_midflight_tombstone_blocks_receipt_and_bookmark(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _now()
    start = base - timedelta(minutes=20)
    end = base - timedelta(minutes=10)
    session_id = "tombstone-fence"
    event_name = f"event-{start.strftime('%Y-%m-%d')}.md"
    _create_event_file(event_name)
    _append_event_entry(
        name=event_name,
        session_id=session_id,
        entry_id="tombstone-focus-entry",
        body="INPUT_THAT_MUST_NOT_COMMIT_AFTER_PURGE",
        coverage_end=end,
    )
    with fts.cursor() as conn:
        _insert_session(
            conn,
            session_id=session_id,
            start=start,
            flush_end=end,
        )
        requested = _request_periodic(
            conn,
            session_id=session_id,
            end=end,
            now=base,
        )

    provider_calls = 0

    def tombstone_during_provider(
        cfg: Any,
        stage: str,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
    ) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        with fts.cursor() as conn:
            candidate_store.put_tombstone(
                conn,
                kind="memory_file",
                artifact_id=event_name,
            )
        return _commit_response("must be fenced")

    monkeypatch.setattr(llm_mod, "call_llm", tombstone_during_provider)
    cfg = config_mod.load(ac_root / "config.toml")
    delivered = classifier_delivery.process_job(cfg, requested.id)

    assert provider_calls == 1
    assert delivered.status == "failed"
    assert "ClassifierJobInputChanged" in delivered.error
    with fts.cursor() as conn:
        durable = classifier_jobs.get(conn, requested.id)
        session = session_store.get_by_id(conn, session_id)
    assert durable is not None and durable.status == "failed"
    assert durable.result is None
    assert session is not None and session.classified_end is None


def test_candidate_slot_replay_preserves_first_proposal_and_receipt(
    ac_root: Path,
) -> None:
    base = _now()
    start = base - timedelta(minutes=20)
    end = base - timedelta(minutes=10)
    session_id = "candidate-replay"
    event_name = f"event-{start.strftime('%Y-%m-%d')}.md"
    entry_id = "candidate-replay-source"
    source_body = "Grounded evidence for a replay-safe candidate."
    _create_event_file(event_name)
    _append_event_entry(
        name=event_name,
        session_id=session_id,
        entry_id=entry_id,
        body=source_body,
        coverage_end=end,
    )
    parsed = files_mod.read_file(paths.memory_dir() / event_name)
    source = next(entry for entry in parsed.entries if entry.id == entry_id)
    evidence = EvidenceRef(
        kind="memory_entry",
        id=entry_id,
        path=event_name,
        timestamp=source.timestamp,
        content_hash=content_digest(source.body),
    )

    with fts.cursor() as conn:
        _insert_session(
            conn,
            session_id=session_id,
            start=start,
            flush_end=end,
        )
        requested = _request_periodic(
            conn,
            session_id=session_id,
            end=end,
            now=base,
        )
        first_claim = classifier_jobs.claim(
            conn,
            job_id=requested.id,
            lease_seconds=300,
            now=base,
        ).row
        assert first_claim.lease_token is not None
        run_key = classifier_jobs.make_producer_run_key(first_claim.id)
        classifier_jobs.bind_input(
            conn,
            job_id=first_claim.id,
            lease_token=first_claim.lease_token,
            input_digest="candidate-input-v1",
            producer_run_key=run_key,
        )
        service = MemoryService(conn)
        first = service.propose_candidate(
            kind="preference",
            target_path="user-preferences.md",
            content="First grounded wording.",
            tags=["preference"],
            evidence=[evidence],
            producer_run_key=run_key,
            proposal_slot=0,
            transaction_guard=lambda guard_conn: classifier_jobs.assert_lease(
                guard_conn,
                job_id=first_claim.id,
                lease_token=first_claim.lease_token,
            ),
        )
        classifier_jobs.fail(
            conn,
            job_id=first_claim.id,
            lease_token=first_claim.lease_token,
            error="crashed after candidate commit",
            retry_seconds=1,
            now=base,
        )

        retry = classifier_jobs.claim(
            conn,
            job_id=first_claim.id,
            lease_seconds=300,
            now=base + timedelta(seconds=2),
        ).row
        assert retry.lease_token is not None
        classifier_jobs.bind_input(
            conn,
            job_id=retry.id,
            lease_token=retry.lease_token,
            input_digest="candidate-input-v1",
            producer_run_key=run_key,
        )
        replay = service.propose_candidate(
            kind="preference",
            target_path="user-preferences.md",
            content="Provider retry changed this wording.",
            tags=["preference"],
            evidence=[evidence],
            producer_run_key=run_key,
            proposal_slot=0,
            transaction_guard=lambda guard_conn: classifier_jobs.assert_lease(
                guard_conn,
                job_id=retry.id,
                lease_token=retry.lease_token,
            ),
        )
        committed = classifier_jobs.record_commit(
            conn,
            job_id=retry.id,
            lease_token=retry.lease_token,
            producer_run_key=run_key,
            result=_receipt("candidate replay committed"),
            now=base + timedelta(seconds=3),
        )
        finalized = classifier_jobs.finalize(
            conn,
            job_id=committed.id,
            now=base + timedelta(seconds=4),
        )
        candidate_count = conn.execute(
            "SELECT COUNT(*) FROM memory_candidates WHERE producer_run_key=?",
            (run_key,),
        ).fetchone()[0]

    assert replay.id == first.id
    assert replay.content == "First grounded wording."
    assert "preserved first" in replay.last_error
    assert candidate_count == 1
    assert finalized.completed.result is not None
    assert finalized.completed.result["candidate_ids"] == [first.id]
