from __future__ import annotations

import json
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openchronicle import config as config_mod
from openchronicle import paths
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef, content_digest
from openchronicle.store import entries as entries_mod
from openchronicle.store import files as files_mod
from openchronicle.store import fts
from openchronicle.writer import classifier as classifier_mod
from openchronicle.writer import llm as llm_mod
from openchronicle.writer import tools as writer_tools

_TZ = timezone(timedelta(hours=8))


def _tool_call(name: str, args: dict[str, Any], cid: str = "c0") -> Any:
    fn = SimpleNamespace(
        name=name, arguments=json.dumps(args, ensure_ascii=False)
    )
    return SimpleNamespace(id=cid, function=fn)


def _response(tool_calls: list | None = None, text: str = "") -> Any:
    msg = SimpleNamespace(content=text or None, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def _seed_event_daily(day: str) -> tuple[str, str]:
    """Create event-YYYY-MM-DD.md with one entry; return (filename, entry_id)."""
    name = f"event-{day}.md"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name=name,
            description=f"Session log for {day}",
            tags=["event", "session", "daily"],
        )
        entry_id = entries_mod.append_entry(
            conn, name=name,
            content=(
                "**Session sess_abc** (10:00–10:45)\n\n"
                "The user spent 45 minutes in Cursor configuring a new "
                "Python project and said in a note: \"I prefer Cursor over "
                "VSCode now because the AI tab-complete is better.\"\n\n"
                "- [10:00-10:45, Cursor] edited project-root files, involving —\n"
            ),
            tags=["session", "sid:sess_abc"],
            # This fixture is intentionally authored directly rather than by
            # the reducer, so mark it as an explicit trusted manual root.
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
    return name, entry_id


def test_classifier_stages_grounded_preference_for_review(ac_root: Path, monkeypatch) -> None:
    day = "2026-04-21"
    name, entry_id = _seed_event_daily(day)

    parsed = files_mod.read_file(paths.memory_dir() / name)
    source_entry = next(entry for entry in parsed.entries if entry.id == entry_id)
    evidence_token = EvidenceRef(
        kind="memory_entry",
        id=entry_id,
        path=name,
        timestamp=source_entry.timestamp,
        content_hash=content_digest(source_entry.body),
    ).key

    # Scripted LLM: iter 1 → search, iter 2 → proposal, iter 3 → commit.
    script = [
        _response([_tool_call(
            "search_memory", {"query": "Cursor over VSCode"}, cid="c1",
        )]),
        _response([_tool_call(
            "propose_memory_candidate",
            {
                "kind": "preference",
                "path": "user-preferences.md",
                "content": "User prefers Cursor over VSCode because of its AI tab-complete.",
                "tags": ["editor", "preference"],
                "evidence_tokens": [evidence_token],
                "confidence": 0.95,
                "conflict_key": "preferred-editor",
            },
            cid="c2",
        )]),
        _response([_tool_call(
            "commit", {"summary": "recorded Cursor-over-VSCode preference"}, cid="c3",
        )]),
    ]

    def fake_call_llm(cfg, stage, *, messages, tools=None, json_mode=False):
        assert stage == "classifier"
        names = {tool["function"]["name"] for tool in tools}
        assert names == {
            "read_memory",
            "search_memory",
            "search_activity_evidence",
            "propose_memory_candidate",
            "commit",
        }
        return script.pop(0)

    monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)

    cfg = config_mod.load(ac_root / "config.toml")
    result = classifier_mod.classify_after_reduce(
        cfg, session_id="sess_abc", event_daily_path=name, just_written_entry_id=entry_id,
    )

    assert result.committed is True
    assert result.written_ids == []
    assert len(result.candidate_ids) == 1
    assert "Cursor-over-VSCode" in result.summary

    # Event-daily was NOT modified.
    evt = (paths.memory_dir() / name).read_text()
    assert evt.count("**Session sess_abc**") == 1

    # Review-first: user-preferences.md is unchanged until explicit approval.
    pref = (paths.memory_dir() / "user-preferences.md").read_text()
    assert "Cursor over VSCode" not in pref
    with fts.cursor() as conn:
        candidate = candidate_store.get(conn, result.candidate_ids[0])
        assert candidate is not None
        assert candidate.status == "pending"
        assert candidate.content.startswith("User prefers Cursor")


def test_classifier_rejects_event_write(ac_root: Path, monkeypatch) -> None:
    day = "2026-04-22"
    name, entry_id = _seed_event_daily(day)

    # LLM tries to write back to event-* — must be rejected without committing.
    script = [
        _response([_tool_call(
            "append",
            {"path": name, "content": "should be blocked", "tags": ["x"]},
            cid="c1",
        )]),
        _response([_tool_call(
            "commit", {"summary": ""}, cid="c2",
        )]),
    ]

    def fake_call_llm(cfg, stage, *, messages, tools=None, json_mode=False):
        return script.pop(0)

    monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)

    cfg = config_mod.load(ac_root / "config.toml")
    result = classifier_mod.classify_after_reduce(
        cfg, session_id="sess_reject", event_daily_path=name, just_written_entry_id=entry_id,
    )

    # Committed=True (LLM called commit), but zero writes landed.
    assert result.committed is True
    assert result.written_ids == []


def test_classifier_empty_commit_when_nothing_classifiable(
    ac_root: Path, monkeypatch,
) -> None:
    day = "2026-04-23"
    name, entry_id = _seed_event_daily(day)

    script = [_response([_tool_call("commit", {"summary": ""}, cid="c1")])]

    def fake_call_llm(cfg, stage, *, messages, tools=None, json_mode=False):
        return script.pop(0)

    monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)

    cfg = config_mod.load(ac_root / "config.toml")
    result = classifier_mod.classify_after_reduce(
        cfg, session_id="sess_noop", event_daily_path=name, just_written_entry_id=entry_id,
    )

    assert result.committed is True
    assert result.written_ids == []
    assert result.iterations == 1


def test_classifier_skips_when_event_daily_missing(ac_root: Path) -> None:
    cfg = config_mod.load(ac_root / "config.toml")
    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess_no_file",
        event_daily_path="event-9999-99-99.md",
        just_written_entry_id="fake",
    )
    assert result.committed is False
    assert "no entries" in result.skipped_reason



def test_classifier_never_promotes_legacy_heuristic_event(
    ac_root: Path, monkeypatch,
) -> None:
    name = "event-2026-04-23.md"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=name,
            description="Legacy reducer output",
            tags=["event", "session", "daily"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name=name,
            content=(
                "**Session sess_heuristic** (09:00–09:15)\n\n"
                "Used Cursor.\n\n"
                "- [09:00-09:15, Cursor] active during the session, involving —"
            ),
            tags=["session", "sid:sess_heuristic", "heuristic"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )

    def unexpected_llm(*args, **kwargs):
        raise AssertionError("heuristic event must not reach the classifier model")

    monkeypatch.setattr(llm_mod, "call_llm", unexpected_llm)
    cfg = config_mod.load(ac_root / "config.toml")
    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess_heuristic",
        event_daily_path=name,
        just_written_entry_id=entry_id,
    )

    assert result.committed is False
    assert result.candidate_ids == []
    assert "no entries" in result.skipped_reason

def test_classifier_tools_bound_retrieval_and_hide_event_entries(
    ac_root: Path,
) -> None:
    event_name, _ = _seed_event_daily("2026-04-24")
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        state = writer_tools.CommitState()
        assert "error" in writer_tools.tool_read_memory(
            conn, cfg, path=event_name, state=state
        )
        assert "error" in writer_tools.tool_read_memory(
            conn, cfg, path="user-profile.md", tail_n=0, state=state
        )
        assert "error" in writer_tools.tool_read_memory(
            conn, cfg, path="user-profile.md", tail_n=21, state=state
        )
        assert "error" in writer_tools.tool_search_memory(
            conn, cfg, query="Cursor", top_k=0, state=state
        )
        assert "error" in writer_tools.tool_search_memory(
            conn, cfg, query="Cursor", top_k=21, state=state
        )
        result = writer_tools.tool_search_memory(
            conn, cfg, query="Cursor", top_k=20, state=state
        )
    assert result["results"] == []


def test_classifier_search_revalidates_index_and_tombstones(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="project-search.md", description="search", tags=["project"]
        )
        entry_id = entries_mod.append_entry(
            conn,
            name="project-search.md",
            content="STALE_SEARCH_SECRET",
            tags=["private"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        state = writer_tools.CommitState()
        assert writer_tools.tool_search_memory(
            conn, cfg, query="STALE_SEARCH_SECRET", state=state
        )["results"]
        candidate_store.put_tombstone(
            conn,
            kind="memory_entry",
            artifact_id=entry_id,
            path="project-search.md",
        )
        assert writer_tools.tool_search_memory(
            conn, cfg, query="STALE_SEARCH_SECRET", state=state
        )["results"] == []


def test_failed_candidate_proposal_does_not_consume_idempotency_slot(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="project-source.md", description="source", tags=["project"]
        )
        entries_mod.append_entry(
            conn,
            name="project-source.md",
            content="The migration is complete.",
            tags=["milestone"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        state = writer_tools.CommitState(producer_run_key="run-slot-test")
        read = writer_tools.tool_read_memory(
            conn, cfg, path="project-source.md", state=state
        )
        token = read["entries"][0]["evidence_token"]
        rejected = writer_tools.tool_propose_memory_candidate(
            conn,
            kind="project_fact",
            path="project-target.md",
            content="<!-- oc-provenance: {} -->",
            tags=["project"],
            evidence_tokens=[token],
            confidence=0.9,
            conflict_key="",
            soft_limit_tokens=16_000,
            state=state,
        )
        accepted = writer_tools.tool_propose_memory_candidate(
            conn,
            kind="project_fact",
            path="project-target.md",
            content="The migration is complete.",
            tags=["project"],
            evidence_tokens=[token],
            confidence=0.9,
            conflict_key="",
            soft_limit_tokens=16_000,
            state=state,
        )
    assert "error" in rejected
    assert accepted["proposal_slot"] == 0


def test_classifier_supersede_requires_seen_target_and_replacement_evidence(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="user-preferences.md",
            description="preferences",
            tags=["preference"],
        )
        entries_mod.append_entry_once(
            conn,
            name="user-preferences.md",
            content="User prefers cloud tools.",
            tags=["preference"],
            entry_id="old-preference",
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        entries_mod.create_file(
            conn,
            name="project-new-signal.md",
            description="new reviewed signal",
            tags=["project"],
        )
        entries_mod.append_entry_once(
            conn,
            name="project-new-signal.md",
            content="User explicitly changed the preference to local tools.",
            tags=["decision"],
            entry_id="new-signal",
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        state = writer_tools.CommitState(producer_run_key="supersede-run")
        target_read = writer_tools.tool_read_memory(
            conn,
            cfg,
            path="user-preferences.md",
            state=state,
        )
        signal_read = writer_tools.tool_read_memory(
            conn,
            cfg,
            path="project-new-signal.md",
            state=state,
        )
        target_token = target_read["entries"][0]["evidence_token"]
        signal_token = signal_read["entries"][0]["evidence_token"]

        missing_target = writer_tools.tool_propose_memory_candidate(
            conn,
            kind="preference",
            operation="supersede",
            path="user-preferences.md",
            target_entry_id="old-preference",
            content="User prefers local tools.",
            tags=["preference"],
            evidence_tokens=[signal_token],
            confidence=0.95,
            conflict_key="tool-storage-preference",
            soft_limit_tokens=16_000,
            state=state,
        )
        assert "must be read or searched" in missing_target["error"]

        missing_replacement = writer_tools.tool_propose_memory_candidate(
            conn,
            kind="preference",
            operation="supersede",
            path="user-preferences.md",
            target_entry_id="old-preference",
            content="User prefers local tools.",
            tags=["preference"],
            evidence_tokens=[target_token],
            confidence=0.95,
            conflict_key="tool-storage-preference",
            soft_limit_tokens=16_000,
            state=state,
        )
        assert "replacement fact" in missing_replacement["error"]

        proposed = writer_tools.tool_propose_memory_candidate(
            conn,
            kind="preference",
            operation="supersede",
            path="user-preferences.md",
            target_entry_id="old-preference",
            content="User prefers local tools.",
            tags=["preference"],
            evidence_tokens=[target_token, signal_token],
            confidence=0.95,
            conflict_key="tool-storage-preference",
            soft_limit_tokens=16_000,
            state=state,
        )
        assert proposed["ok"] is True
        candidate = candidate_store.get(conn, proposed["candidate_id"])
        assert candidate is not None
        assert candidate.operation == "supersede"
        assert candidate.target_entry_id == "old-preference"
        sources = provenance_store.direct_sources(
            conn,
            EvidenceRef(kind="memory_candidate", id=candidate.id),
        )
        assert [(source.path, source.id) for source in sources] == [
            ("project-new-signal.md", "new-signal")
        ]


def test_activity_search_returns_distinct_grounded_sessions_only(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        for day, entry_id, session_id in (
            ("2026-04-19", "event-pattern-a", "sess_a"),
            ("2026-04-20", "event-pattern-b", "sess_b"),
        ):
            name = f"event-{day}.md"
            entries_mod.create_file(
                conn,
                name=name,
                description="session evidence",
                tags=["event"],
            )
            entries_mod.append_entry_once(
                conn,
                name=name,
                content="Writes commit messages in present tense.",
                tags=["session", f"sid:{session_id}"],
                entry_id=entry_id,
                origin=files_mod.MANUAL_ENTRY_ORIGIN,
            )
        entries_mod.append_entry_once(
            conn,
            name="event-2026-04-20.md",
            content="Writes commit messages in present tense.",
            tags=["session", "sid:sess_heuristic", "heuristic"],
            entry_id="event-pattern-heuristic",
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        entries_mod.create_file(
            conn,
            name="project-decoy.md",
            description="durable decoy",
            tags=["project"],
        )
        entries_mod.append_entry_once(
            conn,
            name="project-decoy.md",
            content="Writes commit messages in present tense.",
            tags=["project"],
            entry_id="durable-decoy",
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        state = writer_tools.CommitState()

        result = writer_tools.tool_search_activity_evidence(
            conn,
            cfg,
            query="commit messages present tense",
            top_k=10,
            state=state,
        )

        assert result["retrieval_mode"] == "bm25_activity_evidence"
        assert {
            (item["path"], item["id"], item["session_id"])
            for item in result["results"]
        } == {
            ("event-2026-04-19.md", "event-pattern-a", "sess_a"),
            ("event-2026-04-20.md", "event-pattern-b", "sess_b"),
        }
        assert len(state.allowed_evidence) == 2
