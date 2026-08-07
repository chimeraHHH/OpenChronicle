from __future__ import annotations

import json
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openchronicle import config as config_mod
from openchronicle import paths
from openchronicle.memory_candidates import store as candidate_store
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


def test_classifier_tools_bound_retrieval_and_hide_event_entries(
    ac_root: Path,
) -> None:
    event_name, _ = _seed_event_daily("2026-04-24")
    with fts.cursor() as conn:
        state = writer_tools.CommitState()
        assert "error" in writer_tools.tool_read_memory(
            conn, path=event_name, state=state
        )
        assert "error" in writer_tools.tool_read_memory(
            conn, path="user-profile.md", tail_n=0, state=state
        )
        assert "error" in writer_tools.tool_read_memory(
            conn, path="user-profile.md", tail_n=21, state=state
        )
        assert "error" in writer_tools.tool_search_memory(
            conn, query="Cursor", top_k=0, state=state
        )
        assert "error" in writer_tools.tool_search_memory(
            conn, query="Cursor", top_k=21, state=state
        )
        result = writer_tools.tool_search_memory(
            conn, query="Cursor", top_k=20, state=state
        )
    assert result["results"] == []


def test_classifier_search_revalidates_index_and_tombstones(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="project-search.md", description="search", tags=["project"]
        )
        entry_id = entries_mod.append_entry(
            conn,
            name="project-search.md",
            content="STALE_SEARCH_SECRET",
            tags=["private"],
        )
        state = writer_tools.CommitState()
        assert writer_tools.tool_search_memory(
            conn, query="STALE_SEARCH_SECRET", state=state
        )["results"]
        candidate_store.put_tombstone(
            conn,
            kind="memory_entry",
            artifact_id=entry_id,
            path="project-search.md",
        )
        assert writer_tools.tool_search_memory(
            conn, query="STALE_SEARCH_SECRET", state=state
        )["results"] == []


def test_failed_candidate_proposal_does_not_consume_idempotency_slot(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="project-source.md", description="source", tags=["project"]
        )
        entries_mod.append_entry(
            conn,
            name="project-source.md",
            content="The migration is complete.",
            tags=["milestone"],
        )
        state = writer_tools.CommitState(producer_run_key="run-slot-test")
        read = writer_tools.tool_read_memory(
            conn, path="project-source.md", state=state
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
