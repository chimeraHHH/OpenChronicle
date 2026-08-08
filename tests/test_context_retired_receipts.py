from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.capture import scheduler
from openchronicle.privacy import policy as privacy_policy
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import (
    EvidenceRef,
    observation_digest,
    timeline_block_digest,
)
from openchronicle.services.context import ContextService
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store


def _config() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    return cfg


def _retained_block(
    conn,
    cfg: config_mod.Config,
    *,
    raw_state: str = "retired",
) -> tuple[timeline_store.TimelineBlock, EvidenceRef, EvidenceRef, Path]:
    start = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    timestamp = (start + timedelta(seconds=10)).isoformat()
    capture = {
        "timestamp": timestamp,
        "schema_version": 4,
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "title": "Retired receipt fixture",
        },
        "focused_element": {
            "role": "AXTextArea",
            "value": "RETIRED_RECEIPT_INPUT",
        },
        "visible_text": "RETIRED_RECEIPT_INPUT",
        "url": "",
    }
    capture_path = scheduler._write_capture(capture)
    observation = EvidenceRef(
        kind="observation",
        id=str(capture["observation_id"]),
        path=capture_path.name,
        timestamp=timestamp,
        content_hash=observation_digest(capture),
    )
    block = timeline_store.TimelineBlock(
        id="tlb-retired-context-attestation",
        start_time=start,
        end_time=start + timedelta(minutes=1),
        timezone="UTC",
        entries=["RETIRED_RECEIPT_OUTPUT"],
        apps_used=["Editor"],
        capture_count=1,
    )
    timeline_store.insert(conn, block)
    provenance_store.replace_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id=block.id),
        sources=[observation],
    )
    block = timeline_store.get_by_id(conn, block.id)
    assert block is not None
    block_ref = EvidenceRef(
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
    bindings = [
        (
            capture_path.name,
            observation.id,
            observation.content_hash,
            observation.timestamp,
        )
    ]
    timeline_store.activate_capture_receipts(conn)
    timeline_store.record_capture_receipts(
        conn,
        bindings=bindings,
        window_start=block.start_time,
        window_end=block.end_time,
    )
    timeline_store.record_window_receipt(
        conn,
        timeline_store.make_window_receipt(
            window_start=block.start_time,
            window_end=block.end_time,
            bindings=bindings,
            policy_digest=privacy_policy.stored_observation_policy_digest(cfg.capture),
            outcome="block",
            block=block,
            raw_state=raw_state,
        ),
    )
    capture_path.unlink()
    if raw_state == "retired":
        conn.execute(
            "DELETE FROM timeline_capture_receipts WHERE capture_path=?",
            (capture_path.name,),
        )
    return block, block_ref, observation, capture_path


def test_retired_receipt_authorizes_timeline_and_memory_descendant(
    ac_root: Path,
) -> None:
    cfg = _config()
    with fts.cursor() as conn:
        block, block_ref, observation, _capture_path = _retained_block(conn, cfg)
        entries_store.create_file(
            conn,
            name="project-retired-receipt.md",
            description="retired receipt context fixture",
            tags=["project"],
        )
        entries_store.append_entry_once(
            conn,
            name="project-retired-receipt.md",
            content="RETIRED_RECEIPT_MEMORY_DESCENDANT",
            tags=["derived"],
            entry_id="retired-receipt-memory-descendant",
            evidence_refs=[block_ref],
        )
        parsed = files_store.read_file(
            files_store.memory_path("project-retired-receipt.md")
        )
        service = ContextService(conn, cfg)

        assert not service.evidence_allowed(observation)
        assert service.evidence_allowed(block_ref)
        assert service.memory_entry_allowed(path=parsed.path.name, entry=parsed.entries[0])
        day_context = service.for_day(date(2026, 8, 8), "UTC")
        assert [record.evidence.id for record in day_context.records] == [block.id]


def test_retired_receipt_rejects_current_capture_policy_change(ac_root: Path) -> None:
    cfg = _config()
    with fts.cursor() as conn:
        _block, block_ref, _observation, _capture_path = _retained_block(conn, cfg)
        assert ContextService(conn, cfg).evidence_allowed(block_ref)

        cfg.capture.deny_unknown_windows = True

        assert not ContextService(conn, cfg).evidence_allowed(block_ref)


def test_retired_receipt_rejects_malformed_screenshot_policy(ac_root: Path) -> None:
    cfg = _config()
    with fts.cursor() as conn:
        _block, block_ref, _observation, _capture_path = _retained_block(conn, cfg)
        original_digest = privacy_policy.stored_observation_policy_digest(cfg.capture)
        assert ContextService(conn, cfg).evidence_allowed(block_ref)

        cfg.capture.include_screenshot = "malformed"  # type: ignore[assignment]

        decision = privacy_policy.evaluate_window(
            cfg.capture,
            app_name="Editor",
            bundle_id="com.example.editor",
            window_title="Retired receipt fixture",
        )
        assert not decision.allowed
        assert decision.reason == "invalid_privacy_policy"
        assert privacy_policy.stored_observation_policy_digest(cfg.capture) != original_digest
        assert not ContextService(conn, cfg).evidence_allowed(block_ref)


@pytest.mark.parametrize("mutation", ["receipt", "block", "source"])
def test_retired_receipt_fails_closed_after_binding_tamper(
    ac_root: Path,
    mutation: str,
) -> None:
    cfg = _config()
    with fts.cursor() as conn:
        block, block_ref, _observation, _capture_path = _retained_block(conn, cfg)
        assert ContextService(conn, cfg).evidence_allowed(block_ref)

        if mutation == "receipt":
            conn.execute(
                "UPDATE timeline_window_receipts SET receipt_digest=? WHERE block_id=?",
                ("f" * 64, block.id),
            )
        elif mutation == "block":
            conn.execute(
                "UPDATE timeline_blocks SET entries=? WHERE id=?",
                ('["TAMPERED_RETIRED_BLOCK"]', block.id),
            )
        else:
            conn.execute(
                """
                UPDATE provenance_edges SET source_hash=?
                 WHERE subject_kind='timeline_block' AND subject_id=?
                """,
                ("f" * 64, block.id),
            )

        assert not ContextService(conn, cfg).evidence_allowed(block_ref)


def test_retiring_receipt_does_not_replace_missing_raw_policy_root(ac_root: Path) -> None:
    cfg = _config()
    with fts.cursor() as conn:
        _block, block_ref, observation, _capture_path = _retained_block(
            conn,
            cfg,
            raw_state="retiring",
        )

        service = ContextService(conn, cfg)
        assert not service.evidence_allowed(observation)
        assert not service.evidence_allowed(block_ref)
