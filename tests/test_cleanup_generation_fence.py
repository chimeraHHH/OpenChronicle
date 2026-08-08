from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from openchronicle import cli, paths
from openchronicle import config as config_mod
from openchronicle.capture import filenames as capture_filenames
from openchronicle.capture import scheduler as capture_scheduler
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef, observation_digest
from openchronicle.session import store as session_store
from openchronicle.store import fts
from openchronicle.timeline import aggregator
from openchronicle.timeline import store as timeline_store
from openchronicle.timeline import tick as timeline_tick
from openchronicle.writer import session_reducer

_TZ = timezone(timedelta(hours=8))


def _unrestricted_cfg(ac_root: Path) -> config_mod.Config:
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.capture.deny_unknown_windows = False
    return cfg


def _seed_ended_session(session_id: str, start: datetime, end: datetime) -> None:
    with fts.cursor() as conn:
        block = timeline_store.TimelineBlock(
            start_time=start,
            end_time=end,
            entries=["[Editor] handled private material"],
            apps_used=["Editor"],
            capture_count=1,
        )
        timeline_store.insert(conn, block)
        observation_id = "obs_" + hashlib.blake2s(block.id.encode(), digest_size=16).hexdigest()
        capture_name = (
            capture_filenames.capture_stem(start.isoformat(), observation_id) + ".json"
        )
        capture = {
            "timestamp": start.isoformat(),
            "schema_version": 4,
            "observation_id": observation_id,
            "window_meta": {
                "app_name": "Editor",
                "bundle_id": "com.example.editor",
                "title": "Cleanup generation fence fixture",
                "pid": 101,
                "window_id": 202,
                "bounds": {"x": 10, "y": 20, "width": 900, "height": 700},
            },
            "trigger": {
                "event_type": "manual",
                "app_name": "Editor",
                "bundle_id": "com.example.editor",
                "window_title": "Cleanup generation fence fixture",
                "pid": 101,
                "window_id": 202,
            },
            "privacy": {"decision": "allowed", "policy_version": 2},
            "focused_element": {
                "role": "AXTextArea",
                "value": "handled private material",
            },
            "visible_text": "handled private material",
            "url": "",
        }
        (paths.capture_buffer_dir() / capture_name).write_text(
            json.dumps(capture), encoding="utf-8"
        )
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block.id),
            sources=[
                EvidenceRef(
                    kind="observation",
                    id=observation_id,
                    path=capture_name,
                    timestamp=start.isoformat(),
                    content_hash=observation_digest(capture),
                )
            ],
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )


def _payload(*_args, **_kwargs) -> dict[str, object]:
    return {
        "summary": "Handled private material.",
        "sub_tasks": ["[09:00-09:05, Editor] handled private material"],
    }


def _llm_response() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_payload())))]
    )


def _blocking_provider(
    entered_llm: threading.Event,
    release_llm: threading.Event,
    provider_returning: threading.Event,
):
    def call(_cfg, stage, *, messages, **_kwargs):
        # Reaching the provider proves the reducer already selected and rendered
        # a policy-authorized pre-clean timeline snapshot.
        assert stage == "reducer"
        assert "[Editor] handled private material" in messages[-1]["content"]
        entered_llm.set()
        if not release_llm.wait(timeout=5):
            raise AssertionError("test did not release the blocked reducer LLM")
        provider_returning.set()
        return _llm_response()

    return call


def _delay_post_egress_publish_until_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_returning: threading.Event,
    cleanup_finished: threading.Event,
) -> None:
    """Make the cleanup-first generation-fence branch deterministic."""
    real_publish_fence = session_reducer._publish_fence

    @contextmanager
    def delayed_publish_fence(conn, generation):
        if provider_returning.is_set() and not cleanup_finished.wait(timeout=5):
            raise AssertionError("cleanup did not finish before reducer publish")
        with real_publish_fence(conn, generation):
            yield

    monkeypatch.setattr(session_reducer, "_publish_fence", delayed_publish_fence)


def _assert_no_reducer_projection(session_id: str, end: datetime) -> int:
    assert not list(paths.memory_dir().glob("event-*.md"))
    with fts.cursor() as conn:
        generation = fts.content_generation(conn, "reducer")
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        row = session_store.get_by_id(conn, session_id)
    assert row is not None
    assert row.status == "ended"
    assert row.classified_end == end
    assert row.classifier_terminal_pending is False
    assert row.classifier_terminal_entry_id == ""
    assert row.classifier_terminal_path == ""
    return generation


def test_memory_clean_rejects_stale_reducer_publish_and_allows_fresh_generation(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "sess_memory_generation_fence"
    start = datetime(2026, 8, 8, 9, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)
    _seed_ended_session(session_id, start, end)
    cfg = _unrestricted_cfg(ac_root)
    entered_llm = threading.Event()
    release_llm = threading.Event()
    provider_returning = threading.Event()
    cleanup_started = threading.Event()
    cleanup_finished = threading.Event()
    monkeypatch.setattr(
        session_reducer.llm_mod,
        "call_llm",
        _blocking_provider(entered_llm, release_llm, provider_returning),
    )
    _delay_post_egress_publish_until_cleanup(
        monkeypatch,
        provider_returning=provider_returning,
        cleanup_finished=cleanup_finished,
    )

    with fts.cursor() as conn:
        generation_before = fts.content_generation(conn, "reducer")

    def clean_memory() -> tuple[int, int]:
        cleanup_started.set()
        try:
            return cli._clean_memory()
        finally:
            cleanup_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reducer_future = pool.submit(
            session_reducer.reduce_session,
            cfg,
            session_id=session_id,
            start_time=start,
            end_time=end,
        )
        assert entered_llm.wait(timeout=5)
        cleanup_future = pool.submit(clean_memory)
        assert cleanup_started.wait(timeout=5)
        with pytest.raises(FutureTimeoutError):
            cleanup_future.result(timeout=0.2)
        release_llm.set()
        removed_files, removed_entries = cleanup_future.result(timeout=5)
        with pytest.raises(session_reducer.ReducerInputChanged):
            reducer_future.result(timeout=5)

    assert (removed_files, removed_entries) == (0, 0)
    generation_after = _assert_no_reducer_projection(session_id, end)
    assert generation_after == generation_before + 1

    # Cleanup invalidates only the old snapshot. A reducer that starts in the
    # new generation can still publish from the timeline evidence left intact
    # by memory-only cleanup.
    monkeypatch.setattr(session_reducer.llm_mod, "call_llm", lambda *_a, **_kw: _llm_response())
    fresh = session_reducer.reduce_session(
        cfg,
        session_id=session_id,
        start_time=start,
        end_time=end,
    )

    assert fresh.written is True
    assert (paths.memory_dir() / "event-2026-08-08.md").exists()
    with fts.cursor() as conn:
        assert fts.content_generation(conn, "reducer") == generation_after
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        row = session_store.get_by_id(conn, session_id)
    assert row is not None
    assert row.status == "reduced"
    assert row.classifier_terminal_pending is True
    assert row.classifier_terminal_entry_id == fresh.entry_id


def test_timeline_clean_rejects_stale_reducer_publish(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "sess_timeline_generation_fence"
    start = datetime(2026, 8, 8, 10, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)
    _seed_ended_session(session_id, start, end)
    cfg = _unrestricted_cfg(ac_root)
    entered_llm = threading.Event()
    release_llm = threading.Event()
    provider_returning = threading.Event()
    cleanup_started = threading.Event()
    cleanup_finished = threading.Event()
    monkeypatch.setattr(
        session_reducer.llm_mod,
        "call_llm",
        _blocking_provider(entered_llm, release_llm, provider_returning),
    )
    _delay_post_egress_publish_until_cleanup(
        monkeypatch,
        provider_returning=provider_returning,
        cleanup_finished=cleanup_finished,
    )

    with fts.cursor() as conn:
        generation_before = fts.content_generation(conn, "reducer")

    def clean_timeline() -> int:
        cleanup_started.set()
        try:
            return cli._clean_timeline()
        finally:
            cleanup_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reducer_future = pool.submit(
            session_reducer.reduce_session,
            cfg,
            session_id=session_id,
            start_time=start,
            end_time=end,
        )
        assert entered_llm.wait(timeout=5)
        cleanup_future = pool.submit(clean_timeline)
        assert cleanup_started.wait(timeout=5)
        with pytest.raises(FutureTimeoutError):
            cleanup_future.result(timeout=0.2)
        release_llm.set()
        removed_blocks = cleanup_future.result(timeout=5)
        with pytest.raises(session_reducer.ReducerInputChanged):
            reducer_future.result(timeout=5)

    assert removed_blocks == 1
    generation_after = _assert_no_reducer_projection(session_id, end)
    assert generation_after == generation_before + 1
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM provenance_edges").fetchone()[0] == 0


def test_raw_retention_cannot_cross_final_reducer_revalidation_and_publish(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "sess_raw_retention_publish_fence"
    start = datetime(2026, 8, 8, 10, 30, tzinfo=_TZ)
    end = start + timedelta(minutes=5)
    _seed_ended_session(session_id, start, end)
    cfg = _unrestricted_cfg(ac_root)
    capture_path = next(paths.capture_buffer_dir().glob("*.json"))
    stale_mtime = (
        datetime.now(UTC).timestamp()
        - timedelta(hours=2).total_seconds()
    )
    os.utime(capture_path, (stale_mtime, stale_mtime))

    final_revalidated = threading.Event()
    allow_publish = threading.Event()
    cleanup_started = threading.Event()
    cleanup_finished = threading.Event()
    append_observed = threading.Event()
    policy_calls = 0
    policy_calls_lock = threading.Lock()

    real_policy_allowed_blocks = session_reducer._policy_allowed_blocks

    def gate_after_final_revalidation(conn, policy_cfg, blocks):
        nonlocal policy_calls
        allowed = real_policy_allowed_blocks(conn, policy_cfg, blocks)
        with policy_calls_lock:
            policy_calls += 1
            is_final_revalidation = policy_calls == 3
        if is_final_revalidation:
            final_revalidated.set()
            if not allow_publish.wait(timeout=5):
                raise AssertionError("test did not release final reducer publication")
        return allowed

    real_append = session_reducer._append_event_entry

    def assert_source_exists_at_append(*args, **kwargs):
        assert final_revalidated.is_set()
        assert capture_path.exists()
        assert not cleanup_finished.is_set()
        append_observed.set()
        return real_append(*args, **kwargs)

    monkeypatch.setattr(
        session_reducer,
        "_policy_allowed_blocks",
        gate_after_final_revalidation,
    )
    monkeypatch.setattr(session_reducer, "_append_event_entry", assert_source_exists_at_append)
    monkeypatch.setattr(
        session_reducer.llm_mod,
        "call_llm",
        lambda *_args, **_kwargs: _llm_response(),
    )

    def clean_raw_capture() -> dict[str, int]:
        cleanup_started.set()
        try:
            return capture_scheduler.cleanup_buffer(
                retention_hours=1,
                processed_before_ts=(end + timedelta(days=1)).isoformat(),
            )
        finally:
            cleanup_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reducer_future = pool.submit(
            session_reducer.reduce_session,
            cfg,
            session_id=session_id,
            start_time=start,
            end_time=end,
        )
        assert final_revalidated.wait(timeout=5)
        cleanup_future = pool.submit(clean_raw_capture)
        assert cleanup_started.wait(timeout=5)
        try:
            # The cleanup thread has reached the capture-store acquisition but
            # cannot delete the authorized source until append + status commit
            # leave the reducer's final publication fence.
            with pytest.raises(FutureTimeoutError):
                cleanup_future.result(timeout=0.2)
        finally:
            allow_publish.set()

        result = reducer_future.result(timeout=5)
        cleanup_stats = cleanup_future.result(timeout=5)

    assert append_observed.is_set()
    assert result.written is True
    assert cleanup_stats == {"deleted": 1, "stripped": 0, "evicted": 0}
    assert not capture_path.exists()
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
    assert row is not None
    assert row.status == "reduced"
    assert row.classifier_terminal_entry_id == result.entry_id


def test_timeline_clean_rejects_stale_timeline_provider_publish(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 8, 8, 11, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    capture = {
        "timestamp": start.isoformat(),
        "schema_version": 4,
        "observation_id": "obs-timeline-clean-generation",
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "title": "Timeline cleanup generation fixture",
            "pid": 301,
            "window_id": 302,
            "bounds": {"x": 10, "y": 20, "width": 900, "height": 700},
        },
        "trigger": {
            "event_type": "manual",
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "window_title": "Timeline cleanup generation fixture",
            "pid": 301,
            "window_id": 302,
        },
        "privacy": {"decision": "allowed", "policy_version": 2},
        "focused_element": {
            "role": "AXTextArea",
            "value": "STALE_TIMELINE_PROVIDER_INPUT",
        },
        "visible_text": "STALE_TIMELINE_PROVIDER_INPUT",
        "url": "",
    }
    capture_path = paths.capture_buffer_dir() / "timeline-clean-generation.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    cfg = _unrestricted_cfg(ac_root)
    entered_provider = threading.Event()
    release_provider = threading.Event()
    provider_returning = threading.Event()
    cleanup_started = threading.Event()
    cleanup_finished = threading.Event()

    def blocking_timeline_provider(_cfg, stage, *, messages, **_kwargs):
        assert stage == "timeline"
        assert "STALE_TIMELINE_PROVIDER_INPUT" in messages[-1]["content"]
        entered_provider.set()
        if not release_provider.wait(timeout=5):
            raise AssertionError("test did not release the timeline provider")
        provider_returning.set()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"entries":["STALE_TIMELINE_PROVIDER_OUTPUT"]}'
                    )
                )
            ]
        )

    real_block = timeline_store.TimelineBlock

    def delayed_block(*args, **kwargs):
        if provider_returning.is_set() and not cleanup_finished.wait(timeout=5):
            raise AssertionError("cleanup did not finish before timeline publication")
        return real_block(*args, **kwargs)

    monkeypatch.setattr(aggregator.llm_mod, "call_llm", blocking_timeline_provider)
    monkeypatch.setattr(aggregator.store, "TimelineBlock", delayed_block)

    with fts.cursor() as conn:
        generation_before = fts.content_generation(conn, "timeline")

    def produce():
        with fts.cursor() as conn:
            return aggregator.produce_block_for_window(
                cfg,
                conn,
                start=start,
                end=end,
                parsed_captures=[(capture_path, capture)],
            )

    def clean_timeline() -> int:
        cleanup_started.set()
        try:
            return cli._clean_timeline()
        finally:
            cleanup_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        producer_future = pool.submit(produce)
        assert entered_provider.wait(timeout=5)
        cleanup_future = pool.submit(clean_timeline)
        assert cleanup_started.wait(timeout=5)
        with pytest.raises(FutureTimeoutError):
            cleanup_future.result(timeout=0.2)
        release_provider.set()
        assert cleanup_future.result(timeout=5) == 0
        with pytest.raises(aggregator.TimelineInputChanged):
            producer_future.result(timeout=5)

    with fts.cursor() as conn:
        assert fts.content_generation(conn, "timeline") == generation_before + 1
        assert conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM timeline_state").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM provenance_edges WHERE subject_kind='timeline_block'"
            ).fetchone()[0]
            == 0
        )


def test_timeline_clean_does_not_advance_invalidated_tick_watermark(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 8, 8, 12, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    capture_scheduler._write_capture(
        {
            "timestamp": (start + timedelta(seconds=10)).isoformat(),
            "schema_version": 4,
            "trigger": {"event_type": "manual"},
            "window_meta": {
                "app_name": "Editor",
                "bundle_id": "com.example.editor",
                "title": "Timeline watermark reset fixture",
                "pid": 401,
                "window_id": 402,
                "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            },
            "focused_element": {
                "role": "AXTextArea",
                "value": "FRESH_TICK_RETRY_INPUT",
            },
            "visible_text": "FRESH_TICK_RETRY_INPUT",
            "url": "",
        }
    )
    cfg = _unrestricted_cfg(ac_root)
    cfg.timeline.window_minutes = 1
    cfg.timeline.cold_lookback_minutes = 1
    monkeypatch.setattr(timeline_tick, "_now", lambda: end)

    entered_provider = threading.Event()
    release_provider = threading.Event()
    provider_returning = threading.Event()
    cleanup_finished = threading.Event()

    def blocked_provider(_cfg, stage, *, messages, **_kwargs):
        assert stage == "timeline"
        assert "FRESH_TICK_RETRY_INPUT" in messages[-1]["content"]
        entered_provider.set()
        if not release_provider.wait(timeout=5):
            raise AssertionError("test did not release timeline tick provider")
        provider_returning.set()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"entries":["INVALIDATED_TICK_OUTPUT"]}')
                )
            ]
        )

    real_block = timeline_store.TimelineBlock

    def delayed_block(*args, **kwargs):
        if provider_returning.is_set() and not cleanup_finished.wait(timeout=5):
            raise AssertionError("timeline cleanup did not finish")
        return real_block(*args, **kwargs)

    monkeypatch.setattr(aggregator.llm_mod, "call_llm", blocked_provider)
    monkeypatch.setattr(aggregator.store, "TimelineBlock", delayed_block)

    def clean_timeline() -> int:
        try:
            return cli._clean_timeline()
        finally:
            cleanup_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tick_future = pool.submit(timeline_tick._run_once, cfg)
        assert entered_provider.wait(timeout=5)
        clean_future = pool.submit(clean_timeline)
        with pytest.raises(FutureTimeoutError):
            clean_future.result(timeout=0.2)
        release_provider.set()
        assert clean_future.result(timeout=5) == 0
        assert tick_future.result(timeout=5) == 0

    with fts.cursor() as conn:
        assert timeline_store.get_processed_range(conn) is None
        assert conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0] == 0

    monkeypatch.setattr(aggregator.store, "TimelineBlock", real_block)
    monkeypatch.setattr(
        aggregator.llm_mod,
        "call_llm",
        lambda *_args, **_kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"entries":["FRESH_TICK_RETRY_OUTPUT"]}')
                )
            ]
        ),
    )
    assert timeline_tick._run_once(cfg) == 1
    with fts.cursor() as conn:
        assert timeline_store.get_processed_range(conn) == (start, end)
        row = conn.execute("SELECT entries FROM timeline_blocks").fetchone()
        assert row is not None
        assert "FRESH_TICK_RETRY_OUTPUT" in row["entries"]


def test_blocked_timeline_provider_does_not_pause_capture_persistence(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 8, 8, 15, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    source_capture = {
        "timestamp": (start + timedelta(seconds=5)).isoformat(),
        "schema_version": 4,
        "observation_id": "obs_a501a502",
        "trigger": {
            "event_type": "manual",
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "window_title": "Provider capture liveness",
            "pid": 501,
            "window_id": 502,
        },
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "title": "Provider capture liveness",
            "pid": 501,
            "window_id": 502,
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
        },
        "privacy": {"decision": "allowed", "policy_version": 2},
        "focused_element": {
            "role": "AXTextArea",
            "value": "PROVIDER_BLOCKED_CAPTURE_SOURCE",
        },
        "visible_text": "PROVIDER_BLOCKED_CAPTURE_SOURCE",
        "url": "",
    }
    source_path = paths.capture_buffer_dir() / "provider-capture-source.json"
    source_path.write_text(json.dumps(source_capture), encoding="utf-8")
    provider_entered = threading.Event()
    release_provider = threading.Event()

    def blocked_provider(_cfg, stage, *, messages, **_kwargs):
        assert stage == "timeline"
        assert "PROVIDER_BLOCKED_CAPTURE_SOURCE" in messages[-1]["content"]
        provider_entered.set()
        if not release_provider.wait(timeout=5):
            raise AssertionError("test did not release timeline provider")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"entries":["provider complete"]}')
                )
            ]
        )

    monkeypatch.setattr(aggregator.llm_mod, "call_llm", blocked_provider)
    cfg = _unrestricted_cfg(ac_root)

    def produce():
        with fts.cursor() as conn:
            return aggregator.produce_block_for_window(
                cfg,
                conn,
                start=start,
                end=end,
                parsed_captures=[(source_path, source_capture)],
            )

    later_capture = {
        **source_capture,
        "timestamp": (start + timedelta(minutes=2)).isoformat(),
        "observation_id": "obs_b501b502",
        "visible_text": "CAPTURE_WRITTEN_DURING_PROVIDER",
        "focused_element": {
            "role": "AXTextArea",
            "value": "CAPTURE_WRITTEN_DURING_PROVIDER",
        },
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        producer_future = pool.submit(produce)
        assert provider_entered.wait(timeout=5)
        capture_future = pool.submit(capture_scheduler._write_capture, later_capture)
        # Remote model latency holds the review fence, but ordinary capture
        # persistence needs only the short capture-store fence and must remain
        # live throughout the call.
        written_path = capture_future.result(timeout=1)
        assert written_path.exists()
        release_provider.set()
        assert producer_future.result(timeout=5) is not None

    with fts.cursor() as conn:
        hits = fts.search_captures(conn, query="CAPTURE_WRITTEN_DURING_PROVIDER")
    assert [hit.id for hit in hits] == [written_path.stem]
