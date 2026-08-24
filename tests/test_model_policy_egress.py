from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openchronicle import config as config_mod
from openchronicle import paths
from openchronicle.privacy import policy as privacy_policy
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import (
    EvidenceRef,
    observation_digest,
    timeline_block_digest,
)
from openchronicle.session import store as session_store
from openchronicle.store import entries as entries_mod
from openchronicle.store import files as files_mod
from openchronicle.store import fts
from openchronicle.timeline import aggregator
from openchronicle.timeline import store as timeline_store
from openchronicle.writer import classifier, session_reducer
from openchronicle.writer import llm as llm_mod

_ALLOWED_BUNDLE = "com.example.allowed"
_EXCLUDED_BUNDLE = "com.example.secret"


def _response(
    *,
    text: str = "",
    tool_calls: list[Any] | None = None,
) -> Any:
    message = SimpleNamespace(content=text or None, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> Any:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )


def _capture(
    ac_root: Path,
    *,
    stem: str,
    timestamp: datetime,
    bundle_id: str,
    marker: str,
) -> tuple[Path, dict[str, Any], EvidenceRef]:
    data: dict[str, Any] = {
        "timestamp": timestamp.isoformat(),
        "schema_version": 4,
        "observation_id": f"obs_{stem}",
        "window_meta": {
            "app_name": "Allowed" if bundle_id == _ALLOWED_BUNDLE else "Secret",
            "bundle_id": bundle_id,
            "title": "policy regression",
        },
        "focused_element": {"role": "AXTextArea", "value": marker},
        "visible_text": marker,
        "url": "",
    }
    path = ac_root / "capture-buffer" / f"{stem}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return (
        path,
        data,
        EvidenceRef(
            kind="observation",
            id=data["observation_id"],
            path=path.name,
            timestamp=data["timestamp"],
            content_hash=observation_digest(data),
        ),
    )


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


def _insert_block(
    conn,
    *,
    start: datetime,
    marker: str,
    source: EvidenceRef,
) -> timeline_store.TimelineBlock:
    block = timeline_store.TimelineBlock(
        start_time=start,
        end_time=start + timedelta(seconds=20),
        entries=[marker],
        apps_used=["Editor"],
        capture_count=1,
    )
    timeline_store.insert(conn, block)
    provenance_store.replace_sources(
        conn,
        subject=EvidenceRef(kind="timeline_block", id=block.id),
        sources=[source],
    )
    return block


def _exclude_secret_after_capture(
    cfg: config_mod.Config,
    observations: list[dict[str, Any]],
) -> None:
    assert all(
        privacy_policy.evaluate_stored_observation(cfg.capture, observation=observation).allowed
        for observation in observations
    )
    cfg.capture.excluded_bundle_ids = [_EXCLUDED_BUNDLE]


def test_timeline_model_egress_rechecks_retained_capture_policy(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime.now().astimezone().replace(microsecond=0)
    allowed = _capture(
        ac_root,
        stem="timeline-allowed",
        timestamp=start,
        bundle_id=_ALLOWED_BUNDLE,
        marker="ALLOWED_TIMELINE_CAPTURE",
    )
    excluded = _capture(
        ac_root,
        stem="timeline-excluded",
        timestamp=start + timedelta(seconds=10),
        bundle_id=_EXCLUDED_BUNDLE,
        marker="EXCLUDED_TIMELINE_CAPTURE",
    )
    cfg = config_mod.Config()
    _exclude_secret_after_capture(cfg, [allowed[1], excluded[1]])
    sent: list[str] = []

    def fake_call(
        _cfg: config_mod.Config,
        stage: str,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
    ) -> Any:
        assert stage == "timeline" and tools is None and json_mode is True
        sent.append(json.dumps(messages, ensure_ascii=False))
        return _response(text='{"entries":["ALLOWED_TIMELINE_OUTPUT"]}')

    monkeypatch.setattr(llm_mod, "call_llm", fake_call)
    with fts.cursor() as conn:
        block = aggregator.produce_block_for_window(
            cfg,
            conn,
            start=start,
            end=start + timedelta(minutes=1),
            parsed_captures=[(allowed[0], allowed[1]), (excluded[0], excluded[1])],
        )
        assert block is not None
        sources = provenance_store.direct_sources(
            conn,
            EvidenceRef(kind="timeline_block", id=block.id),
        )

    assert "ALLOWED_TIMELINE_CAPTURE" in sent[0]
    assert "EXCLUDED_TIMELINE_CAPTURE" not in sent[0]
    assert block.capture_count == 1
    assert [source.id for source in sources] == [allowed[2].id]


def test_legacy_sibling_window_never_reenters_timeline_prompt(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime.now().astimezone().replace(microsecond=0)
    secret = "SECRET_SIBLING_VALUE"
    capture_path = ac_root / "capture-buffer" / "legacy-sibling.json"
    capture = {
        "timestamp": start.isoformat(),
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "title": "Public roadmap",
        },
        "ax_tree": {
            "apps": [
                {
                    "name": "Editor",
                    "bundle_id": "com.example.editor",
                    "is_frontmost": True,
                    "windows": [
                        {"title": "Public roadmap", "focused": True, "elements": []},
                        {
                            "title": "Secret payroll",
                            "focused": False,
                            "elements": [{"role": "AXStaticText", "value": secret}],
                        },
                    ],
                }
            ]
        },
    }
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    cfg = config_mod.Config()
    cfg.capture.excluded_window_title_patterns = ["Secret"]
    sent: list[str] = []

    def unexpected_call(*args, **kwargs):  # noqa: ARG001
        sent.append(json.dumps(kwargs.get("messages"), ensure_ascii=False))
        return _response(text='{"entries":["unexpected"]}')

    monkeypatch.setattr(llm_mod, "call_llm", unexpected_call)
    with fts.cursor() as conn:
        block = aggregator.produce_block_for_window(
            cfg,
            conn,
            start=start,
            end=start + timedelta(minutes=1),
            parsed_captures=[(capture_path, capture)],
        )

    rendered, _apps = aggregator._format_events([(capture_path, capture)])
    assert block is None
    assert sent == []
    assert secret not in rendered


def test_reducer_model_egress_filters_blocks_and_preceding_entries(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime.now().astimezone().replace(microsecond=0) - timedelta(minutes=1)
    allowed_capture = _capture(
        ac_root,
        stem="reducer-allowed",
        timestamp=start,
        bundle_id=_ALLOWED_BUNDLE,
        marker="ALLOWED_REDUCER_RAW",
    )
    excluded_capture = _capture(
        ac_root,
        stem="reducer-excluded",
        timestamp=start + timedelta(seconds=30),
        bundle_id=_EXCLUDED_BUNDLE,
        marker="EXCLUDED_REDUCER_RAW",
    )
    cfg = config_mod.Config()
    _exclude_secret_after_capture(cfg, [allowed_capture[1], excluded_capture[1]])
    event_name = f"event-{start.strftime('%Y-%m-%d')}.md"
    session_id = "policy-reducer-session"

    with fts.cursor() as conn:
        allowed_block = _insert_block(
            conn,
            start=start,
            marker="ALLOWED_REDUCER_BLOCK",
            source=allowed_capture[2],
        )
        excluded_block = _insert_block(
            conn,
            start=start + timedelta(seconds=30),
            marker="EXCLUDED_REDUCER_BLOCK",
            source=excluded_capture[2],
        )
        entries_mod.create_file(
            conn,
            name=event_name,
            description="policy reducer context",
            tags=["event"],
        )
        entries_mod.append_entry_once(
            conn,
            name=event_name,
            content="ALLOWED_PRECEDING_ENTRY",
            tags=["session"],
            entry_id="allowed-preceding",
            evidence_refs=[_block_ref(allowed_block)],
        )
        entries_mod.append_entry_once(
            conn,
            name=event_name,
            content="EXCLUDED_PRECEDING_ENTRY",
            tags=["session"],
            entry_id="excluded-preceding",
            evidence_refs=[_block_ref(excluded_block)],
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=start,
                end_time=start + timedelta(seconds=50),
                status="ended",
            ),
        )

    sent: list[str] = []

    def fake_call(
        _cfg: config_mod.Config,
        stage: str,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
    ) -> Any:
        assert stage == "reducer" and tools is None and json_mode is True
        sent.append(json.dumps(messages, ensure_ascii=False))
        return _response(
            text=json.dumps(
                {
                    "summary": "allowed only",
                    "sub_tasks": ["[00:00-00:01, Editor] allowed only"],
                }
            )
        )

    monkeypatch.setattr(llm_mod, "call_llm", fake_call)
    result = session_reducer.reduce_session(
        cfg,
        session_id=session_id,
        start_time=start,
        end_time=start + timedelta(seconds=50),
    )

    assert result.written is True
    assert "ALLOWED_REDUCER_BLOCK" in sent[0]
    assert "ALLOWED_PRECEDING_ENTRY" in sent[0]
    assert "EXCLUDED_REDUCER_BLOCK" not in sent[0]
    assert "EXCLUDED_PRECEDING_ENTRY" not in sent[0]
    parsed = files_mod.read_file(paths.memory_dir() / event_name)
    generated = next(entry for entry in parsed.entries if entry.id == result.entry_id)
    assert [ref.id for ref in generated.evidence_refs if ref.kind == "timeline_block"] == [
        allowed_block.id
    ]


def test_classifier_and_tools_never_resend_policy_excluded_markers(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now().astimezone().replace(microsecond=0)
    start = now - timedelta(minutes=2)
    end = now + timedelta(minutes=2)
    allowed_capture = _capture(
        ac_root,
        stem="classifier-allowed",
        timestamp=start,
        bundle_id=_ALLOWED_BUNDLE,
        marker="ALLOWED_CLASSIFIER_RAW",
    )
    excluded_capture = _capture(
        ac_root,
        stem="classifier-excluded",
        timestamp=start + timedelta(seconds=30),
        bundle_id=_EXCLUDED_BUNDLE,
        marker="EXCLUDED_CLASSIFIER_RAW",
    )
    cfg = config_mod.Config()
    _exclude_secret_after_capture(cfg, [allowed_capture[1], excluded_capture[1]])
    session_id = "policy-classifier-session"
    event_name = f"event-{now.strftime('%Y-%m-%d')}.md"
    prior_name = f"event-{(start - timedelta(days=1)).strftime('%Y-%m-%d')}.md"
    memory_name = "project-policy-egress.md"

    with fts.cursor() as conn:
        allowed_block = _insert_block(
            conn,
            start=start + timedelta(seconds=10),
            marker="ALLOWED_CLASSIFIER_TIMELINE",
            source=allowed_capture[2],
        )
        excluded_block = _insert_block(
            conn,
            start=start + timedelta(seconds=40),
            marker="EXCLUDED_CLASSIFIER_TIMELINE",
            source=excluded_capture[2],
        )
        entries_mod.create_file(conn, name=event_name, description="focus", tags=["event"])
        entries_mod.append_entry_once(
            conn,
            name=event_name,
            content="ALLOWED_FOCUS_MARKER",
            tags=["session", f"sid:{session_id}"],
            entry_id="allowed-focus",
            evidence_refs=[_block_ref(allowed_block)],
        )
        entries_mod.append_entry_once(
            conn,
            name=event_name,
            content="EXCLUDED_FOCUS_MARKER",
            tags=["session", f"sid:{session_id}"],
            entry_id="excluded-focus",
            evidence_refs=[_block_ref(excluded_block)],
        )
        entries_mod.create_file(conn, name=prior_name, description="prior", tags=["event"])
        entries_mod.append_entry_once(
            conn,
            name=prior_name,
            content="ALLOWED_PRIOR_DAY_MARKER",
            tags=["session"],
            entry_id="allowed-prior",
            evidence_refs=[_block_ref(allowed_block)],
        )
        entries_mod.append_entry_once(
            conn,
            name=prior_name,
            content="EXCLUDED_PRIOR_DAY_MARKER",
            tags=["session"],
            entry_id="excluded-prior",
            evidence_refs=[_block_ref(excluded_block)],
        )
        entries_mod.create_file(conn, name=memory_name, description="tool memory", tags=["project"])
        entries_mod.append_entry_once(
            conn,
            name=memory_name,
            content="TOOL_COMMON ALLOWED_TOOL_MARKER",
            tags=["policy"],
            entry_id="allowed-tool",
            evidence_refs=[_block_ref(allowed_block)],
        )
        entries_mod.append_entry_once(
            conn,
            name=memory_name,
            content="TOOL_COMMON EXCLUDED_TOOL_MARKER",
            tags=["policy"],
            entry_id="excluded-tool",
            evidence_refs=[_block_ref(excluded_block)],
        )
        entries_mod.append_entry_once(
            conn,
            name=memory_name,
            content="TOOL_COMMON MANUAL_MEMORY_MARKER",
            tags=["manual"],
            entry_id="manual-tool",
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )

    sent: list[str] = []

    def fake_call(
        _cfg: config_mod.Config,
        stage: str,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
    ) -> Any:
        assert stage == "classifier" and tools is not None and json_mode is False
        sent.append(json.dumps(messages, ensure_ascii=False))
        if len(sent) == 1:
            return _response(
                tool_calls=[
                    _tool_call("read_memory", {"path": memory_name}, "read-1"),
                    _tool_call(
                        "search_memory",
                        {"query": "TOOL_COMMON", "top_k": 10},
                        "search-1",
                    ),
                ]
            )
        return _response(tool_calls=[_tool_call("commit", {"summary": "done"}, "commit-1")])

    monkeypatch.setattr(llm_mod, "call_llm", fake_call)
    result = classifier.classify_window(
        cfg,
        session_id=session_id,
        event_daily_path=event_name,
        start=start,
        end=end,
        include_prior_day=True,
    )

    assert result.committed is True
    payload = "\n".join(sent)
    assert "ALLOWED_FOCUS_MARKER" in payload
    assert "ALLOWED_CLASSIFIER_TIMELINE" in payload
    assert "ALLOWED_PRIOR_DAY_MARKER" in payload
    assert "ALLOWED_TOOL_MARKER" in payload
    assert "MANUAL_MEMORY_MARKER" in payload
    assert "EXCLUDED_FOCUS_MARKER" not in payload
    assert "EXCLUDED_CLASSIFIER_TIMELINE" not in payload
    assert "EXCLUDED_PRIOR_DAY_MARKER" not in payload
    assert "EXCLUDED_TOOL_MARKER" not in payload
