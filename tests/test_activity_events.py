from __future__ import annotations

from pathlib import Path

from openchronicle import config as config_mod
from openchronicle.activity import store as activity_store
from openchronicle.mcp import server as mcp_server
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.writer import tools as writer_tools


def _create_event_file(conn, day: str = "2026-08-23") -> str:
    path = f"event-{day}.md"
    entries_store.create_file(
        conn,
        name=path,
        description=f"Activity for {day}",
        tags=["event", "session", "daily"],
    )
    return path


def _append_session(
    conn,
    *,
    path: str,
    entry_id: str,
    session_id: str,
    start: str,
    end: str,
    summary: str,
    tasks: list[tuple[str, str, str, str]],
) -> str:
    body = [f"**Session {session_id}** ({start}–{end})", "", summary, ""]
    body.extend(
        f"- [{task_start}-{task_end}, {app}] {content}"
        for task_start, task_end, app, content in tasks
    )
    entry_id, _ = entries_store.append_entry_once(
        conn,
        name=path,
        content="\n".join(body),
        tags=["session", f"sid:{session_id}"],
        entry_id=entry_id,
        origin=files_store.MANUAL_ENTRY_ORIGIN,
    )
    return entry_id


def test_event_projection_splits_subtasks_and_materializes_neighbors(ac_root: Path) -> None:
    with fts.cursor() as conn:
        path = _create_event_file(conn)
        _append_session(
            conn,
            path=path,
            entry_id="session-release",
            session_id="sess_release",
            start="10:00",
            end="10:30",
            summary="Prepared the release and checked its documentation.",
            tasks=[
                ("10:00", "10:10", "Cursor", "updated the release script; involving build.py"),
                (
                    "10:10",
                    "10:15",
                    "Google Chrome",
                    "checked the API documentation; involving release API",
                ),
                ("10:15", "10:30", "Terminal", "ran the release tests; involving pytest"),
            ],
        )

        rows = conn.execute(
            "SELECT * FROM activity_events ORDER BY start_time"
        ).fetchall()
        result = mcp_server._search_activity(
            conn,
            cfg=config_mod.Config(),
            query="API documentation",
            top_k=1,
            adjacent=1,
        )

    assert len(rows) == 3
    assert rows[0]["previous_event_id"] is None
    assert rows[0]["next_event_id"] == rows[1]["id"]
    assert rows[1]["previous_event_id"] == rows[0]["id"]
    assert rows[1]["next_event_id"] == rows[2]["id"]
    assert rows[2]["previous_event_id"] == rows[1]["id"]
    assert rows[2]["next_event_id"] is None

    assert result["retrieval_mode"] == "event_bm25_strict_then_or_with_adjacency"
    assert result["adjacency_radius"] == 1
    assert result["results"][0]["app_name"] == "Google Chrome"
    assert result["results"][0]["source"] == {
        "kind": "memory_entry",
        "id": "session-release",
        "path": path,
        "timestamp": rows[1]["source_entry_timestamp"],
    }
    assert [item["relation"] for item in result["results"][0]["neighbors"]] == [
        "previous",
        "next",
    ]
    assert [item["app_name"] for item in result["results"][0]["neighbors"]] == [
        "Cursor",
        "Terminal",
    ]


def test_event_projection_preserves_multiline_content_and_midnight_window(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        path = _create_event_file(conn)
        entries_store.append_entry_once(
            conn,
            name=path,
            content=(
                "**Session sess_midnight** (23:55–00:05)\n\n"
                "Continued a late deployment.\n\n"
                "- [23:55-00:05, Terminal] monitored deployment output.\n"
                "  Exact error: connection reset by peer."
            ),
            tags=["session", "sid:sess_midnight"],
            entry_id="session-midnight",
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )
        event = conn.execute("SELECT * FROM activity_events").fetchone()

    assert event is not None
    assert event["start_time"].startswith("2026-08-23T23:55")
    assert event["end_time"].startswith("2026-08-24T00:05")
    assert "connection reset by peer" in event["content"]
    assert event["summary"] == "Continued a late deployment."


def test_activity_search_relaxes_only_after_zero_hit_across_event_boundary(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        path = _create_event_file(conn)
        _append_session(
            conn,
            path=path,
            entry_id="session-lumen",
            session_id="sess_lumen",
            start="11:00",
            end="11:20",
            summary="Completed the review.",
            tasks=[
                ("11:00", "11:10", "Cursor", "drafted Lumen schema LUMEN_SCHEMA_12"),
                ("11:10", "11:20", "Slack", "recorded Mira's approval"),
            ],
        )

        relaxed = mcp_server._search_activity(
            conn,
            cfg=config_mod.Config(),
            query="Lumen schema approval",
            top_k=1,
            adjacent=1,
        )
        strict = mcp_server._search_activity(
            conn,
            cfg=config_mod.Config(),
            query="Lumen schema",
            top_k=1,
            adjacent=0,
        )

    assert relaxed["results"][0]["app_name"] == "Cursor"
    assert relaxed["results"][0]["query_mode"] == "relaxed_or_after_zero_hits"
    assert relaxed["results"][0]["neighbors"][0]["app_name"] == "Slack"
    assert strict["results"][0]["app_name"] == "Cursor"
    assert strict["results"][0]["query_mode"] == "strict_and"
    assert strict["results"][0]["neighbors"] == []


def test_classifier_activity_search_exposes_match_and_neighbor_sources(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        path = _create_event_file(conn)
        _append_session(
            conn,
            path=path,
            entry_id="session-a",
            session_id="sess_a",
            start="09:00",
            end="09:10",
            summary="Prepared the repository.",
            tasks=[("09:00", "09:10", "Cursor", "updated repository metadata")],
        )
        _append_session(
            conn,
            path=path,
            entry_id="session-b",
            session_id="sess_b",
            start="09:10",
            end="09:20",
            summary="Published the repository.",
            tasks=[("09:10", "09:20", "Terminal", "pushed the repository release")],
        )
        state = writer_tools.CommitState()

        result = writer_tools.tool_search_activity_evidence(
            conn,
            cfg,
            query="repository release",
            top_k=1,
            adjacent=1,
            state=state,
        )

    assert result["results"][0]["id"] == "session-b"
    assert result["results"][0]["event_id"].startswith("activity-")
    assert result["results"][0]["neighbors"][0]["id"] == "session-a"
    assert result["results"][0]["neighbors"][0]["relation"] == "previous"
    assert len(state.allowed_evidence) == 2


def test_activity_projection_rebuilds_and_deletes_with_canonical_entry(ac_root: Path) -> None:
    with fts.cursor() as conn:
        path = _create_event_file(conn)
        entry_id = _append_session(
            conn,
            path=path,
            entry_id="session-rebuild",
            session_id="sess_rebuild",
            start="13:00",
            end="13:20",
            summary="Rebuilt the local index.",
            tasks=[("13:00", "13:20", "Terminal", "rebuilt the local activity index")],
        )
        assert conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 1

        activity_store.clear(conn)
        assert conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 0
        entries_store.rebuild_index(conn)
        assert conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 1

        assert entries_store.delete_entry(conn, name=path, entry_id=entry_id) is True
        assert conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 0
        assert activity_store.search(conn, query="activity index") == []


def test_activity_search_drops_tampered_projection_and_hidden_neighbor(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        path = _create_event_file(conn)
        _append_session(
            conn,
            path=path,
            entry_id="session-hidden",
            session_id="sess_hidden",
            start="14:00",
            end="14:10",
            summary="Prepared a private precursor.",
            tasks=[("14:00", "14:10", "Cursor", "prepared private precursor")],
        )
        _append_session(
            conn,
            path=path,
            entry_id="session-visible",
            session_id="sess_visible",
            start="14:10",
            end="14:20",
            summary="Ran the public target task.",
            tasks=[("14:10", "14:20", "Terminal", "ran public target task")],
        )
        candidate_store.put_tombstone(
            conn,
            kind="memory_entry",
            artifact_id="session-hidden",
            path=path,
        )
        visible = mcp_server._search_activity(
            conn,
            cfg=cfg,
            query="public target task",
            top_k=1,
            adjacent=1,
        )
        assert visible["results"][0]["neighbors"] == []

        event_id = visible["results"][0]["event_id"]
        conn.execute(
            "UPDATE activity_events SET content='tampered projection' WHERE id=?",
            (event_id,),
        )
        tampered = mcp_server._search_activity(
            conn,
            cfg=cfg,
            query="public target task",
            top_k=1,
            adjacent=0,
        )

    assert tampered["results"] == []


def test_mcp_registers_event_level_activity_search(ac_root: Path) -> None:
    server = mcp_server.build_server(config_mod.Config())
    assert "search_activity" in server._tool_manager._tools
