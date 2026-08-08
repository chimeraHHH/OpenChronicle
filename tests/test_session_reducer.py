from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from openchronicle import config as config_mod
from openchronicle import paths
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import (
    EvidenceRef,
    observation_digest,
    timeline_block_digest,
)
from openchronicle.session import store as session_store
from openchronicle.store import files as files_mod
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store
from openchronicle.writer import session_reducer

_TZ = timezone(timedelta(hours=8))
_SID = "sess_test0000"


def _unrestricted_cfg(ac_root: Path) -> config_mod.Config:
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.capture.deny_unknown_windows = False
    return cfg


def _block_ref(block: timeline_store.TimelineBlock) -> EvidenceRef:
    return EvidenceRef(
        kind="timeline_block",
        id=block.id,
        timestamp=block.start_time.isoformat(),
        content_hash=timeline_block_digest(
            start=block.start_time.isoformat(),
            end=block.end_time.isoformat(),
            entries=block.entries,
            apps=block.apps_used,
        ),
    )


def _insert_live_block(
    conn,
    block: timeline_store.TimelineBlock,
) -> timeline_store.TimelineBlock:
    """Persist a block with current raw-observation ancestry."""
    capture_name = f"{block.id}.json"
    observation_id = "obs_" + hashlib.blake2s(
        block.id.encode(), digest_size=16
    ).hexdigest()
    visible_text = "\n".join(block.entries)
    app_name = block.apps_used[0] if block.apps_used else "Editor"
    capture = {
        "timestamp": block.start_time.isoformat(),
        "schema_version": 4,
        "observation_id": observation_id,
        "window_meta": {
            "app_name": app_name,
            "bundle_id": "com.example.editor",
            "title": "Reducer provenance fixture",
        },
        "focused_element": {"role": "AXTextArea", "value": visible_text},
        "visible_text": visible_text,
        "url": "",
    }
    capture_path = paths.capture_buffer_dir() / capture_name
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    observation = EvidenceRef(
        kind="observation",
        id=observation_id,
        path=capture_name,
        timestamp=block.start_time.isoformat(),
        content_hash=observation_digest(capture),
    )
    timeline_store.insert(conn, block)
    provenance_store.replace_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id=block.id),
        sources=[observation],
    )
    return block


def test_attach_drill_down_breadcrumb_unit() -> None:
    """Direct unit test of the breadcrumb post-processor."""
    f = session_reducer._attach_drill_down_breadcrumb
    assert f("[14:30-14:35, Cursor] edited main.py").endswith(
        'raw: read_recent_capture(at="14:30", app_name="Cursor")'
    )
    # Spaces in app name preserved verbatim.
    out = f("[09:00-09:05, Code - Insiders] reviewed config.toml")
    assert 'app_name="Code - Insiders"' in out
    # En-dash separator (LLMs sometimes emit it) still parses.
    out2 = f("[18:00–18:30, Google Chrome] reading docs")
    assert 'at="18:00"' in out2 and 'app_name="Google Chrome"' in out2
    # Lines without the canonical prefix pass through untouched.
    plain = "no prefix at all, just text"
    assert f(plain) == plain
    # Idempotent — already-breadcrumbed lines aren't double-appended.
    crumbed = f("[14:30-14:35, Cursor] edited")
    assert f(crumbed) == crumbed


def _seed_blocks(start: datetime) -> list[timeline_store.TimelineBlock]:
    """Create 3 contiguous 5-min blocks with one entry each."""
    bs: list[timeline_store.TimelineBlock] = []
    with fts.cursor() as conn:
        for i in range(3):
            b = timeline_store.TimelineBlock(
                start_time=start + timedelta(minutes=5 * i),
                end_time=start + timedelta(minutes=5 * (i + 1)),
                timezone="+08:00",
                entries=[f"[Cursor] edited file_{i}.py, involving nothing"],
                apps_used=["Cursor"],
                capture_count=6,
            )
            _insert_live_block(conn, b)
            bs.append(b)
    return bs


def test_reducer_happy_path_writes_event_daily(ac_root: Path, monkeypatch) -> None:
    start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    blocks = _seed_blocks(start)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(id=_SID, start_time=start, end_time=end, status="ended"),
        )

    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv(
        "OPENCHRONICLE_LLM_MOCK_JSON",
        json.dumps(
            {
                "summary": "Worked on a few Python files in Cursor.",
                "sub_tasks": [
                    "[10:00-10:15, Cursor] edited three files, involving file_0.py, file_1.py, file_2.py",
                ],
            }
        ),
    )

    cfg = _unrestricted_cfg(ac_root)
    result = session_reducer.reduce_session(
        cfg, session_id=_SID, start_time=start, end_time=end,
    )

    assert result.succeeded is True
    assert result.written is True
    assert result.entry_id
    assert result.path == "event-2026-04-21.md"
    assert len(result.sub_tasks) == 1

    md = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    assert "Session sess_test0000" in md
    assert "[10:00-10:15, Cursor]" in md
    assert "file_0.py" in md
    # Drill-down breadcrumb appended by _attach_drill_down_breadcrumb.
    assert 'raw: read_recent_capture(at="10:00", app_name="Cursor")' in md
    # The result list also carries it.
    assert any('read_recent_capture(at="10:00"' in s for s in result.sub_tasks)

    parsed = files_mod.read_file(paths.memory_dir() / result.path)
    generated = next(entry for entry in parsed.entries if entry.id == result.entry_id)
    assert generated.evidence_refs == [_block_ref(block) for block in blocks]
    assert all(ref.kind != "session" for ref in generated.evidence_refs)

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, _SID)
    assert row is not None
    assert row.status == "reduced"


def test_reducer_no_blocks_marks_reduced_no_write(ac_root: Path, monkeypatch) -> None:
    start = datetime(2026, 4, 21, 11, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(id="sess_empty", start_time=start, end_time=end, status="ended"),
        )
        timeline_store.initialize_processed_range(conn, start)
        timeline_store.advance_processed_through(conn, end, window_start=start)

    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    cfg = _unrestricted_cfg(ac_root)
    result = session_reducer.reduce_session(
        cfg, session_id="sess_empty", start_time=start, end_time=end,
    )

    assert result.succeeded is True
    assert result.written is False
    assert not (paths.memory_dir() / "event-2026-04-21.md").exists()

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_empty")
    assert row is not None
    assert row.status == "reduced"


def test_terminal_reduce_includes_block_straddling_exact_session_end(
    ac_root: Path, monkeypatch
) -> None:
    """A sub-minute terminal slice must not disappear at a bucket boundary."""
    block_start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    session_start = block_start + timedelta(seconds=10)
    session_end = block_start + timedelta(seconds=50)
    session_id = "sess_partial_terminal"
    with fts.cursor() as conn:
        _insert_live_block(
            conn,
            timeline_store.TimelineBlock(
                start_time=block_start,
                end_time=block_start + timedelta(minutes=1),
                entries=["[Editor] short but durable work"],
                apps_used=["Editor"],
                capture_count=1,
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=session_start,
                end_time=session_end,
                status="ended",
            ),
        )

    seen_blocks: list[str] = []

    def payload(_cfg, blocks, *_args, **_kwargs):
        seen_blocks.extend(block.id for block in blocks)
        return {
            "summary": "short session",
            "sub_tasks": ["[10:00-10:01, Editor] short but durable work"],
        }

    monkeypatch.setattr(session_reducer, "_call_reducer_llm", payload)
    result = session_reducer.reduce_session(
        _unrestricted_cfg(ac_root),
        session_id=session_id,
        start_time=session_start,
        end_time=session_end,
    )

    assert result.written is True
    assert len(seen_blocks) == 1
    assert "short but durable work" in (
        paths.memory_dir() / "event-2026-04-21.md"
    ).read_text()
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
    assert row is not None and row.status == "reduced"


def test_tampered_timeline_block_cannot_bridge_terminal_coverage(
    ac_root: Path,
) -> None:
    block_start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    session_start = block_start + timedelta(seconds=10)
    session_end = block_start + timedelta(seconds=50)
    session_id = "sess_tampered_terminal_bridge"
    block = timeline_store.TimelineBlock(
        id="tlb-tampered-terminal-bridge",
        start_time=block_start,
        end_time=block_start + timedelta(minutes=1),
        entries=["[Editor] canonical terminal evidence"],
        apps_used=["Editor"],
        capture_count=1,
    )
    with fts.cursor() as conn:
        _insert_live_block(conn, block)
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=session_start,
                end_time=session_end,
                status="ended",
            ),
        )
        conn.execute(
            "UPDATE timeline_blocks SET entries=? WHERE id=?",
            ('["TAMPERED_TERMINAL_BRIDGE"]', block.id),
        )

    result = session_reducer.reduce_session(
        _unrestricted_cfg(ac_root),
        session_id=session_id,
        start_time=session_start,
        end_time=session_end,
    )

    assert result.written is False
    assert result.succeeded is False
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
    assert row is not None
    assert row.status == "ended"
    assert row.classifier_terminal_noop is False


def test_terminal_waits_for_late_timeline_tail_then_pending_retry_writes(
    ac_root: Path, monkeypatch
) -> None:
    block_start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    session_start = block_start + timedelta(seconds=10)
    session_end = block_start + timedelta(seconds=50)
    session_id = "sess_late_terminal_bucket"
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=session_start,
                end_time=session_end,
                status="ended",
            ),
        )

    calls = 0

    def payload(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "summary": "late tail",
            "sub_tasks": ["[10:00-10:01, Editor] recovered late tail"],
        }

    monkeypatch.setattr(session_reducer, "_call_reducer_llm", payload)
    cfg = _unrestricted_cfg(ac_root)
    deferred = session_reducer.reduce_session(
        cfg,
        session_id=session_id,
        start_time=session_start,
        end_time=session_end,
    )

    assert deferred.written is False
    assert deferred.succeeded is False
    assert calls == 0
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
        assert row is not None and row.status == "ended"
        _insert_live_block(
            conn,
            timeline_store.TimelineBlock(
                start_time=block_start,
                end_time=block_start + timedelta(minutes=1),
                entries=["[Editor] recovered late tail"],
                apps_used=["Editor"],
                capture_count=1,
            ),
        )
        timeline_store.advance_processed_through(
            conn, block_start + timedelta(minutes=1)
        )

    retried = session_reducer.reduce_all_pending(cfg)

    assert len(retried) == 1 and retried[0].written is True
    assert calls == 1
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
    assert row is not None and row.status == "reduced"


def test_terminal_uses_one_way_watermark_then_blocks_read_order(
    ac_root: Path,
    monkeypatch,
) -> None:
    """A producer commit between the two reads must cause defer, not no-op."""
    block_start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    session_start = block_start + timedelta(seconds=10)
    session_end = block_start + timedelta(seconds=50)
    session_id = "sess_watermark_toctou"
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=session_start,
                end_time=session_end,
                status="ended",
            ),
        )
        timeline_store.initialize_processed_range(
            conn,
            block_start - timedelta(minutes=1),
        )
        timeline_store.advance_processed_through(
            conn,
            block_start,
            window_start=block_start - timedelta(minutes=1),
        )

    def producer_commits_after_watermark_read(
        conn,
        _start,
        _end,
        *,
        complete_only,
    ):
        assert complete_only is False
        timeline_store.advance_processed_through(
            conn,
            block_start + timedelta(minutes=1),
            window_start=block_start,
        )
        return []

    monkeypatch.setattr(
        session_reducer,
        "_blocks_for_session",
        producer_commits_after_watermark_read,
    )
    result = session_reducer.reduce_session(
        _unrestricted_cfg(ac_root),
        session_id=session_id,
        start_time=session_start,
        end_time=session_end,
    )

    assert result.written is False and result.succeeded is False
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
        assert row is not None and row.status == "ended"
        assert timeline_store.covers(
            conn,
            block_start + timedelta(minutes=1),
        )


def test_upper_watermark_cannot_prove_target_at_coverage_start(
    ac_root: Path,
) -> None:
    session_start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    session_end = session_start + timedelta(minutes=1)
    session_id = "sess_before_coverage"
    coverage_start = session_end
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=session_start,
                end_time=session_end,
                status="ended",
            ),
        )
        timeline_store.initialize_processed_range(conn, coverage_start)
        timeline_store.advance_processed_through(
            conn,
            coverage_start + timedelta(hours=1),
            window_start=coverage_start,
        )

    result = session_reducer.reduce_session(
        _unrestricted_cfg(ac_root),
        session_id=session_id,
        start_time=session_start,
        end_time=session_end,
    )

    assert result.written is False and result.succeeded is False
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
    assert row is not None and row.status == "ended"


def test_terminal_reducer_defers_intersecting_invalid_timeline_row(
    ac_root: Path,
) -> None:
    block_start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    block_end = block_start + timedelta(minutes=1)
    session_start = block_start + timedelta(seconds=10)
    session_end = block_start + timedelta(seconds=50)
    session_id = "sess_invalid_timeline_gap"
    with fts.cursor() as conn:
        block = timeline_store.TimelineBlock(
            id="tlb-invalid-terminal-gap",
            start_time=block_start,
            end_time=block_end,
            entries=["[Editor] valid before tamper"],
            apps_used=["Editor"],
            capture_count=1,
        )
        _insert_live_block(conn, block)
        conn.execute(
            "UPDATE timeline_blocks SET entries='[\"tampered\"]' WHERE id=?",
            (block.id,),
        )
        timeline_store.initialize_processed_range(conn, block_start)
        timeline_store.advance_processed_through(
            conn,
            block_end,
            window_start=block_start,
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=session_start,
                end_time=session_end,
                status="ended",
            ),
        )

    result = session_reducer.reduce_session(
        _unrestricted_cfg(ac_root),
        session_id=session_id,
        start_time=session_start,
        end_time=session_end,
    )

    assert result.succeeded is False and result.written is False
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
    assert row is not None
    assert row.status == "ended"
    assert row.classifier_terminal_noop is False


@pytest.mark.parametrize(
    ("stored_end", "grid_anchor", "premature_bound", "correct_bound"),
    [
        (
            "2026-03-08T03:10:00-04:00",
            "2026-03-08T00:00:00-05:00",
            "2026-03-08T04:00:00-04:00",
            "2026-03-08T05:00:00-04:00",
        ),
        (
            "2026-11-01T01:10:00-05:00",
            "2026-11-01T00:00:00-04:00",
            "2026-11-01T02:00:00-05:00",
            "2026-11-01T03:00:00-05:00",
        ),
    ],
)
def test_terminal_readiness_reuses_durable_grid_after_session_roundtrip(
    ac_root: Path,
    stored_end: str,
    grid_anchor: str,
    premature_bound: str,
    correct_bound: str,
) -> None:
    end = datetime.fromisoformat(stored_end)
    start = end - timedelta(minutes=10)
    session_id = f"sess_dst_ready_{end.month}"
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )
        persisted = session_store.get_by_id(conn, session_id)
    assert persisted is not None and not isinstance(persisted.end_time.tzinfo, ZoneInfo)

    too_early = (
        datetime.fromisoformat(grid_anchor),
        datetime.fromisoformat(premature_bound),
    )
    ready = (
        datetime.fromisoformat(grid_anchor),
        datetime.fromisoformat(correct_bound),
    )

    assert not session_reducer._terminal_timeline_ready(
        blocks=[],
        session_end=persisted.end_time,
        window_minutes=120,
        processed_range=too_early,
    )
    assert session_reducer._terminal_timeline_ready(
        blocks=[],
        session_end=persisted.end_time,
        window_minutes=120,
        processed_range=ready,
    )


def test_active_flush_waits_for_block_that_extends_past_now(
    ac_root: Path, monkeypatch
) -> None:
    block_start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    session_start = block_start + timedelta(seconds=10)
    now = block_start + timedelta(seconds=50)
    session_id = "sess_partial_flush"
    with fts.cursor() as conn:
        _insert_live_block(
            conn,
            timeline_store.TimelineBlock(
                start_time=block_start,
                end_time=block_start + timedelta(minutes=1),
                entries=["[Editor] still-open bucket"],
                apps_used=["Editor"],
                capture_count=1,
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=session_start,
                status="active",
            ),
        )

    monkeypatch.setattr(
        session_reducer,
        "_call_reducer_llm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("flush must wait for a complete timeline block")
        ),
    )
    result = session_reducer.flush_active_session(
        _unrestricted_cfg(ac_root),
        session_id=session_id,
        session_start=session_start,
        now=now,
    )

    assert result is None
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
    assert row is not None and row.flush_end is None


def test_reducer_llm_failure_schedules_retry(ac_root: Path, monkeypatch) -> None:
    start = datetime(2026, 4, 21, 12, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(id="sess_failing", start_time=start, end_time=end, status="ended"),
        )

    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    # Non-JSON output → json.JSONDecodeError in _call_reducer_llm → None → retry.
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK_JSON", "not json at all")

    cfg = _unrestricted_cfg(ac_root)
    result = session_reducer.reduce_session(
        cfg, session_id="sess_failing", start_time=start, end_time=end,
    )

    assert result.succeeded is False
    assert result.written is False

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_failing")
    assert row is not None
    assert row.status == "failed"
    assert row.retry_count == 1
    assert row.next_retry_at is not None


def test_reducer_exhausted_retries_writes_heuristic(ac_root: Path, monkeypatch) -> None:
    start = datetime(2026, 4, 21, 13, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    # Row begins with retry_count=4, meaning this is attempt 5/5.
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_last_chance",
                start_time=start,
                end_time=end,
                status="failed",
                retry_count=4,
            ),
        )

    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK_JSON", "still garbage")

    cfg = _unrestricted_cfg(ac_root)
    result = session_reducer.reduce_session(
        cfg, session_id="sess_last_chance", start_time=start, end_time=end,
    )

    assert result.succeeded is False
    assert result.written is True
    md = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    assert "Cursor" in md
    assert "heuristic" in md  # tag should be present on the heading

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_last_chance")
    assert row is not None
    assert row.status == "reduced"


def test_reducer_idempotent_on_already_reduced(ac_root: Path, monkeypatch) -> None:
    start = datetime(2026, 4, 21, 14, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_done", start_time=start, end_time=end, status="reduced",
            ),
        )

    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    cfg = _unrestricted_cfg(ac_root)
    result = session_reducer.reduce_session(
        cfg, session_id="sess_done", start_time=start, end_time=end,
    )
    assert result.succeeded is True
    assert result.written is False
    assert not (paths.memory_dir() / "event-2026-04-21.md").exists()


def test_flush_active_session_writes_partial_entry(
    ac_root: Path, monkeypatch,
) -> None:
    start = datetime(2026, 4, 21, 16, 0, tzinfo=_TZ)
    _seed_blocks(start)  # 3 blocks covering 16:00-16:15
    now = start + timedelta(minutes=15)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_flush1", start_time=start, status="active",
            ),
        )

    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv(
        "OPENCHRONICLE_LLM_MOCK_JSON",
        json.dumps(
            {
                "summary": "partial",
                "sub_tasks": [
                    "[16:00-16:15, Cursor] wip, involving files",
                ],
            }
        ),
    )

    cfg = _unrestricted_cfg(ac_root)
    result = session_reducer.flush_active_session(
        cfg, session_id="sess_flush1", session_start=start, now=now,
    )
    assert result is not None
    assert result.is_final is False
    assert result.written is True

    md = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    assert "Session sess_flush1 [flush]" in md

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_flush1")
    assert row is not None
    # Still active — flush must not mark reduced.
    assert row.status == "active"
    assert row.flush_end is not None
    assert row.flush_end >= now


def test_terminal_reduce_after_flush_covers_trailing_window(
    ac_root: Path, monkeypatch,
) -> None:
    start = datetime(2026, 4, 21, 17, 0, tzinfo=_TZ)
    # Two blocks: 17:00-17:05 (flushed) and 17:05-17:10 (trailing).
    with fts.cursor() as conn:
        for i in range(2):
            _insert_live_block(
                conn,
                timeline_store.TimelineBlock(
                    start_time=start + timedelta(minutes=5 * i),
                    end_time=start + timedelta(minutes=5 * (i + 1)),
                    timezone="+08:00",
                    entries=[f"[Cursor] step_{i}, involving nothing"],
                    apps_used=["Cursor"],
                    capture_count=3,
                ),
            )
        # Pretend a flush already consumed the first block.
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_flush2", start_time=start, status="active",
            ),
        )
        session_store.set_flush_end(
            conn, "sess_flush2", start + timedelta(minutes=5),
        )

    end = start + timedelta(minutes=10)
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv(
        "OPENCHRONICLE_LLM_MOCK_JSON",
        json.dumps(
            {
                "summary": "tail",
                "sub_tasks": [
                    "[17:05-17:10, Cursor] final slice, involving step_1",
                ],
            }
        ),
    )

    cfg = _unrestricted_cfg(ac_root)
    result = session_reducer.reduce_session(
        cfg, session_id="sess_flush2", start_time=start, end_time=end,
    )
    assert result.written is True
    assert result.is_final is True

    md = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    # The terminal entry is NOT tagged as flush.
    assert "Session sess_flush2 [flush]" not in md
    assert "Session sess_flush2" in md
    # Trailing window header shows 17:05, not 17:00 — flush_end trimmed the start.
    assert "17:05" in md

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_flush2")
    assert row is not None
    assert row.status == "reduced"


def test_flush_advances_only_to_persisted_block_boundary_and_accepts_late_block(
    ac_root: Path,
    monkeypatch,
) -> None:
    session_id = "sess_late_block"
    start = datetime(2026, 4, 21, 18, 0, tzinfo=_TZ)
    first_end = start + timedelta(minutes=1)
    first_block = timeline_store.TimelineBlock(
        start_time=start,
        end_time=first_end,
        entries=["[Editor] first minute"],
        apps_used=["Editor"],
        capture_count=1,
    )
    with fts.cursor() as conn:
        _insert_live_block(conn, first_block)
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=start,
                status="active",
            ),
        )

    windows: list[tuple[datetime, datetime]] = []

    def record_window(_cfg, _blocks, window_start, window_end, **_kwargs):
        windows.append((window_start, window_end))
        return {
            "summary": "partial",
            "sub_tasks": ["[18:00-18:05, Editor] continued work"],
        }

    monkeypatch.setattr(session_reducer, "_call_reducer_llm", record_window)
    cfg = _unrestricted_cfg(ac_root)
    first = session_reducer.flush_active_session(
        cfg,
        session_id=session_id,
        session_start=start,
        now=start + timedelta(minutes=5),
    )
    assert first is not None and first.written is True
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
    assert row is not None and row.flush_end == first_end
    assert windows == [(start, first_end)]

    late_end = start + timedelta(minutes=5)
    late_block = timeline_store.TimelineBlock(
        start_time=first_end,
        end_time=late_end,
        entries=["[Editor] late materialized block"],
        apps_used=["Editor"],
        capture_count=4,
    )
    with fts.cursor() as conn:
        _insert_live_block(conn, late_block)

    second = session_reducer.flush_active_session(
        cfg,
        session_id=session_id,
        session_start=start,
        now=start + timedelta(minutes=6),
    )
    assert second is not None and second.written is True
    assert windows[-1] == (first_end, late_end)
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
    assert row is not None and row.flush_end == late_end
    markdown = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    assert markdown.count(f"Session {session_id} [flush]") == 2
    parsed = files_mod.read_file(paths.memory_dir() / "event-2026-04-21.md")
    first_entry = next(entry for entry in parsed.entries if entry.id == first.entry_id)
    second_entry = next(entry for entry in parsed.entries if entry.id == second.entry_id)
    assert first_entry.evidence_refs == [_block_ref(first_block)]
    assert second_entry.evidence_refs[0] == _block_ref(late_block)
    assert [(ref.kind, ref.id) for ref in second_entry.evidence_refs] == [
        ("timeline_block", late_block.id),
        ("memory_entry", first.entry_id),
    ]
    assert all(ref.kind != "session" for ref in second_entry.evidence_refs)


def test_flush_crash_replay_recovers_watermark_without_second_llm(
    ac_root: Path,
    monkeypatch,
) -> None:
    session_id = "sess_flush_crash"
    start = datetime(2026, 4, 21, 19, 0, tzinfo=_TZ)
    block_end = start + timedelta(minutes=1)
    with fts.cursor() as conn:
        _insert_live_block(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=block_end,
                entries=["[Editor] durable flush"],
                apps_used=["Editor"],
                capture_count=1,
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=start,
                status="active",
            ),
        )

    calls = 0

    def payload(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"summary": "partial", "sub_tasks": ["[19:00-19:01, Editor] work"]}

    monkeypatch.setattr(session_reducer, "_call_reducer_llm", payload)
    real_set_flush_end = session_store.set_flush_end

    def crash_before_progress(*_args, **_kwargs):
        raise RuntimeError("simulated process death before flush progress")

    monkeypatch.setattr(session_store, "set_flush_end", crash_before_progress)
    cfg = _unrestricted_cfg(ac_root)
    with pytest.raises(RuntimeError, match="simulated process death"):
        session_reducer.flush_active_session(
            cfg,
            session_id=session_id,
            session_start=start,
            now=start + timedelta(minutes=5),
        )

    monkeypatch.setattr(session_store, "set_flush_end", real_set_flush_end)
    replay = session_reducer.flush_active_session(
        cfg,
        session_id=session_id,
        session_start=start,
        now=start + timedelta(minutes=6),
    )

    assert replay is None
    assert calls == 1
    markdown = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    assert markdown.count(f"Session {session_id} [flush]") == 1
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
        indexed = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE tags LIKE ?",
            (f"%sid:{session_id}%",),
        ).fetchone()[0]
    assert row is not None and row.flush_end == block_end
    assert indexed == 1


def test_retry_due_picks_up_failed_rows(ac_root: Path, monkeypatch) -> None:
    start = datetime(2026, 4, 21, 15, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    # Already-due failed row: next_retry_at in the past.
    past = datetime.now().astimezone() - timedelta(minutes=1)
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_retry",
                start_time=start,
                end_time=end,
                status="failed",
                retry_count=1,
                next_retry_at=past,
            ),
        )

    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    monkeypatch.setenv(
        "OPENCHRONICLE_LLM_MOCK_JSON",
        json.dumps({"summary": "recovered", "sub_tasks": ["[15:00-15:15, Cursor] ok, involving —"]}),
    )

    cfg = _unrestricted_cfg(ac_root)
    results = session_reducer.retry_due(cfg)
    assert len(results) == 1
    assert results[0].succeeded is True

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_retry")
    assert row is not None
    assert row.status == "reduced"


def test_retry_due_compares_mixed_offsets_by_absolute_time(ac_root: Path) -> None:
    start = datetime(2026, 11, 1, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
    now = datetime(2026, 11, 1, 1, 10, tzinfo=timezone(timedelta(hours=-5)))
    # 01:50 at -04:00 is 05:50Z and is already due at 06:10Z, even though its
    # ISO string sorts after 01:10 at -05:00 during the DST fallback hour.
    due_at = datetime(2026, 11, 1, 1, 50, tzinfo=timezone(timedelta(hours=-4)))
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_dst_retry",
                start_time=start,
                end_time=now,
                status="failed",
                retry_count=1,
                next_retry_at=due_at,
            ),
        )
        due = session_store.list_due_for_retry(conn, now=now)

    assert [row.id for row in due] == ["sess_dst_retry"]
