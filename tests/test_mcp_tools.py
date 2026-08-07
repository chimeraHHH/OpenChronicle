"""Test MCP tool functions directly (bypassing FastMCP wiring)."""

from pathlib import Path

from openchronicle import config as config_mod
from openchronicle.daily_wrap import store as daily_wrap_store
from openchronicle.mcp import captures as captures_mod
from openchronicle.mcp import server as mcp_server
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance.models import EvidenceRef, content_digest
from openchronicle.store import entries as entries_mod
from openchronicle.store import fts
from openchronicle.timeline import store as timeline_store
from openchronicle.writer import tools as writer_tools


def test_list_memories(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="user-profile.md", description="identity facts", tags=["identity"]
        )
        entries_mod.create_file(
            conn, name="project-foo.md", description="Foo project", tags=["project"]
        )
        out = mcp_server._list_memories(conn)
    assert out["count"] == 2
    paths = {f["path"] for f in out["files"]}
    assert paths == {"user-profile.md", "project-foo.md"}


def test_read_memory_with_tail(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="topic-x.md", description="Topic X", tags=["topic"]
        )
        for i in range(3):
            entries_mod.append_entry(
                conn, name="topic-x.md", content=f"fact {i}", tags=["x"]
            )
        out = mcp_server._read_memory(conn, path="topic-x.md", tail_n=2)
    assert len(out["entries"]) == 2


def test_search(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="tool-vim.md", description="vim", tags=["tool"]
        )
        entries_mod.append_entry(
            conn, name="tool-vim.md", content="User uses vim for editing.", tags=["editor"]
        )
        out = mcp_server._search(conn, query="vim", top_k=3)
    assert out["results"]
    assert out["results"][0]["path"] == "tool-vim.md"


def test_recent_activity(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="event-2026-04-22.md",
                                description="week", tags=["event"])
        entries_mod.append_entry(
            conn, name="event-2026-04-22.md", content="Did a thing.", tags=["x"]
        )
        out = mcp_server._recent_activity(conn, limit=5)
    assert out["count"] >= 1


def test_get_schema() -> None:
    out = mcp_server._get_schema()
    assert "Memory Organization Spec" in out["schema"]


def test_memory_reads_and_source_drawer_include_provenance(ac_root: Path) -> None:
    source = EvidenceRef(kind="observation", id="obs_expired", path="gone.json")
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="project-grounded.md", description="grounded", tags=["project"]
        )
        entries_mod.append_entry_once(
            conn,
            name="project-grounded.md",
            content="Grounded project fact.",
            tags=["fact"],
            entry_id="grounded-fact",
            evidence_refs=[source],
        )
        read = mcp_server._read_memory(conn, path="project-grounded.md")
        drawer = mcp_server._get_provenance(
            conn,
            kind="memory_entry",
            artifact_id="grounded-fact",
            path="project-grounded.md",
        )
    assert read["entries"][0]["evidence"][0]["id"] == "obs_expired"
    assert drawer["direct_sources"][0]["availability"] == "expired"


def test_memory_read_surfaces_hide_entries_with_stale_memory_dependencies(
    ac_root: Path,
) -> None:
    source_name = "user-stale-source.md"
    derived_name = "project-stale-derived.md"
    leaf_name = "topic-stale-leaf.md"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name=source_name, description="source", tags=["user"]
        )
        source_id = entries_mod.append_entry(
            conn,
            name=source_name,
            content="STALE_SOURCE_PRIVATE",
            tags=["source"],
        )
        source_ref = EvidenceRef(
            kind="memory_entry",
            id=source_id,
            path=source_name,
            content_hash=content_digest("STALE_SOURCE_PRIVATE"),
        )
        entries_mod.create_file(
            conn, name=derived_name, description="derived", tags=["project"]
        )
        entries_mod.append_entry_once(
            conn,
            name=derived_name,
            content="STALE_DERIVED_PRIVATE",
            tags=["derived"],
            entry_id="stale-derived-entry",
            evidence_refs=[source_ref],
        )
        entries_mod.create_file(
            conn, name=leaf_name, description="leaf", tags=["topic"]
        )
        entries_mod.append_entry_once(
            conn,
            name=leaf_name,
            content="STALE_LEAF_PRIVATE",
            tags=["leaf"],
            entry_id="stale-leaf-entry",
            evidence_refs=[
                EvidenceRef(
                    kind="memory_entry",
                    id="stale-derived-entry",
                    path=derived_name,
                    content_hash=content_digest("STALE_DERIVED_PRIVATE"),
                )
            ],
        )
        entries_mod.delete_entry(conn, name=source_name, entry_id=source_id)

        read = mcp_server._read_memory(conn, path=derived_name)
        leaf_read = mcp_server._read_memory(conn, path=leaf_name)
        search = mcp_server._search(conn, query="STALE_DERIVED_PRIVATE")
        leaf_search = mcp_server._search(conn, query="STALE_LEAF_PRIVATE")
        recent = mcp_server._recent_activity(conn)
        classifier_read = writer_tools.tool_read_memory(conn, path=derived_name)
        classifier_leaf_read = writer_tools.tool_read_memory(conn, path=leaf_name)
        classifier_search = writer_tools.tool_search_memory(
            conn, query="STALE_DERIVED_PRIVATE"
        )
        classifier_leaf_search = writer_tools.tool_search_memory(
            conn, query="STALE_LEAF_PRIVATE"
        )

    assert read["entries"] == []
    assert read["entry_count"] == 0
    assert leaf_read["entries"] == []
    assert leaf_read["entry_count"] == 0
    assert search["results"] == []
    assert leaf_search["results"] == []
    assert all(
        entry["id"] not in {"stale-derived-entry", "stale-leaf-entry"}
        for entry in recent["entries"]
    )
    assert classifier_read["entries"] == []
    assert classifier_leaf_read["entries"] == []
    assert classifier_search["results"] == []
    assert classifier_leaf_search["results"] == []


def test_mcp_hides_pending_candidate_subjects_and_tombstoned_entries(
    ac_root: Path,
) -> None:
    with fts.cursor() as conn:
        denied = mcp_server._get_provenance(
            conn, kind="memory_candidate", artifact_id="mc-secret"
        )
        assert "error" in denied
        entries_mod.create_file(
            conn, name="project-purging.md", description="private", tags=["project"]
        )
        entries_mod.append_entry_once(
            conn,
            name="project-purging.md",
            content="TOMBSTONED_SECRET",
            tags=["private"],
            entry_id="purging-entry",
        )
        candidate_store.put_tombstone(
            conn,
            kind="memory_entry",
            artifact_id="purging-entry",
            path="project-purging.md",
        )
        read = mcp_server._read_memory(conn, path="project-purging.md")
        search = mcp_server._search(conn, query="TOMBSTONED_SECRET")
        recent = mcp_server._recent_activity(conn)
        provenance = mcp_server._get_provenance(
            conn,
            kind="memory_entry",
            artifact_id="purging-entry",
            path="project-purging.md",
        )
        assert read["entries"] == []
        assert read["entry_count"] == 0
        assert search["results"] == []
        assert all(
            item["id"] != "purging-entry" for item in recent["entries"]
        )
        assert "error" in provenance


def test_memory_reads_reject_case_and_unicode_aliases_of_tombstoned_paths(
    ac_root: Path,
) -> None:
    name = "project-café-secret.md"
    alternate_case = "Project-café-secret.md"
    alternate_unicode = "project-cafe\u0301-secret.md"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name=name, description="private", tags=["project"]
        )
        entries_mod.append_entry_once(
            conn,
            name=name,
            content="CANONICAL_PATH_PRIVATE_MARKER",
            tags=["private"],
            entry_id="canonical-private-entry",
        )
        candidate_store.put_tombstone(
            conn, kind="memory_file", artifact_id=name
        )
        assert "error" in mcp_server._read_memory(conn, path=alternate_case)
        assert "error" in mcp_server._read_memory(conn, path=alternate_unicode)

        candidate_store.delete_tombstone(
            conn, kind="memory_file", artifact_id=name
        )
        candidate_store.put_tombstone(
            conn,
            kind="memory_entry",
            artifact_id="canonical-private-entry",
            path=name,
        )
        assert mcp_server._read_memory(conn, path=name)["entries"] == []
        assert "error" in mcp_server._read_memory(conn, path=alternate_case)
        assert "error" in mcp_server._read_memory(conn, path=alternate_unicode)


def test_daily_wrap_mcp_helpers_are_read_only(ac_root: Path) -> None:
    with fts.cursor() as conn:
        claim = daily_wrap_store.claim(
            conn,
            local_date="2026-04-21",
            timezone="UTC",
            scope="default",
            window_start_utc="2026-04-21T00:00:00+00:00",
            window_end_utc="2026-04-22T00:00:00+00:00",
            workflow_version=1,
            coverage_status="partial",
            input_digest="mcp-digest",
            lease_token="mcp-lease",
        )
        daily_wrap_store.complete(
            conn,
            wrap_id=claim.row.id,
            lease_token="mcp-lease",
            input_digest="mcp-digest",
            coverage_status="partial",
            output={
                "completed": [],
                "progressed": [],
                "open": [],
                "blocked": [],
                "needs_review": [],
            },
            sources=[],
        )
        out = mcp_server._get_daily_wrap(
            conn, local_date="2026-04-21", timezone="UTC"
        )
        listing = mcp_server._list_daily_wraps(conn)
    assert out["revision"] == 1
    assert listing["count"] == 1

    server = mcp_server.build_server(config_mod.Config())
    tool_names = set(server._tool_manager._tools)
    assert {"get_provenance", "get_daily_wrap", "list_daily_wraps"} <= tool_names
    assert not {
        "approve_candidate",
        "reject_candidate",
        "edit_candidate",
        "forget_candidate",
    }.intersection(tool_names)


# ─── search_captures + current_context ────────────────────────────────────


def _seed_capture(conn, *, id, ts, app, title, value, text, url=""):
    fts.insert_capture(
        conn, id=id, timestamp=ts, app_name=app,
        bundle_id="com.test." + app.lower(),
        window_title=title, focused_role="AXTextArea",
        focused_value=value, visible_text=text, url=url,
    )


def test_search_captures_returns_bm25_hits_with_snippet(ac_root: Path) -> None:
    with fts.cursor() as conn:
        _seed_capture(conn, id="c1", ts="2026-04-22T14:00:00+08:00",
                      app="Cursor", title="main.py", value="def foo()",
                      text="def foo(): return 1")
        _seed_capture(conn, id="c2", ts="2026-04-22T14:05:00+08:00",
                      app="Safari", title="docs", value="",
                      text="reading about rate limiter design")

    results = captures_mod.search_captures(query="rate limiter")
    assert len(results) == 1
    r = results[0]
    assert r["file_stem"] == "c2"
    assert r["app_name"] == "Safari"
    assert "[rate]" in r["snippet"] and "[limiter]" in r["snippet"]


def test_search_captures_app_and_time_filters(ac_root: Path) -> None:
    with fts.cursor() as conn:
        _seed_capture(conn, id="c1", ts="2026-04-22T13:00:00+08:00",
                      app="Cursor", title="a.py", value="", text="login flow stuff")
        _seed_capture(conn, id="c2", ts="2026-04-22T14:00:00+08:00",
                      app="Safari", title="docs", value="", text="login flow stuff")
        _seed_capture(conn, id="c3", ts="2026-04-22T15:00:00+08:00",
                      app="Cursor", title="b.py", value="", text="login flow stuff")

    cursor_only = captures_mod.search_captures(query="login flow", app_name="Cursor")
    assert {h["file_stem"] for h in cursor_only} == {"c1", "c3"}

    bounded = captures_mod.search_captures(
        query="login flow",
        since="2026-04-22T13:30:00+08:00",
        until="2026-04-22T14:30:00+08:00",
    )
    assert {h["file_stem"] for h in bounded} == {"c2"}


def test_current_context_shape(ac_root: Path) -> None:
    """Headlines newest-first, fulltext deduped by (app,window), timeline blocks ordered."""
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    with fts.cursor() as conn:
        # Five captures, two from the same (app, window) pair so dedup should drop one.
        _seed_capture(conn, id="c1", ts="2026-04-22T14:00:00+08:00",
                      app="Cursor", title="main.py", value="x=1", text="A")
        _seed_capture(conn, id="c2", ts="2026-04-22T14:01:00+08:00",
                      app="Safari", title="docs", value="", text="B")
        _seed_capture(conn, id="c3", ts="2026-04-22T14:02:00+08:00",
                      app="Cursor", title="main.py", value="x=2", text="C")
        _seed_capture(conn, id="c4", ts="2026-04-22T14:03:00+08:00",
                      app="Slack", title="#general", value="", text="D")
        _seed_capture(conn, id="c5", ts="2026-04-22T14:04:00+08:00",
                      app="Mail", title="Inbox", value="", text="E")

        # Two timeline blocks
        timeline_store.insert(conn, timeline_store.TimelineBlock(
            start_time=datetime(2026, 4, 22, 14, 0, tzinfo=tz),
            end_time=datetime(2026, 4, 22, 14, 1, tzinfo=tz),
            entries=["[Cursor] editing main.py"], apps_used=["Cursor"], capture_count=2,
        ))
        timeline_store.insert(conn, timeline_store.TimelineBlock(
            start_time=datetime(2026, 4, 22, 14, 1, tzinfo=tz),
            end_time=datetime(2026, 4, 22, 14, 2, tzinfo=tz),
            entries=["[Safari] reading docs"], apps_used=["Safari"], capture_count=1,
        ))

    ctx = captures_mod.current_context(
        headline_limit=5, fulltext_limit=3, timeline_limit=10,
    )
    # Headlines: newest-first, all 5 captures.
    assert [h["file_stem"] for h in ctx["recent_captures_headline"]] == \
        ["c5", "c4", "c3", "c2", "c1"]

    # Fulltext: top 3 distinct (app, window) — c5(Mail), c4(Slack), c3(Cursor/main.py).
    # c1 dedupes against c3 (same Cursor/main.py).
    fulltext_stems = [r["file_stem"] for r in ctx["recent_captures_fulltext"]]
    assert fulltext_stems == ["c5", "c4", "c3"]
    # Fulltext carries the actual visible_text.
    assert ctx["recent_captures_fulltext"][2]["visible_text"] == "C"

    # Timeline blocks present and ordered chronologically.
    assert len(ctx["recent_timeline_blocks"]) == 2
    assert ctx["recent_timeline_blocks"][0]["entries"] == ["[Cursor] editing main.py"]


def test_current_context_app_filter(ac_root: Path) -> None:
    with fts.cursor() as conn:
        _seed_capture(conn, id="c1", ts="2026-04-22T14:00:00+08:00",
                      app="Cursor", title="a", value="", text="A")
        _seed_capture(conn, id="c2", ts="2026-04-22T14:01:00+08:00",
                      app="Safari", title="b", value="", text="B")

    ctx = captures_mod.current_context(app_filter="Safari", headline_limit=5)
    assert [h["file_stem"] for h in ctx["recent_captures_headline"]] == ["c2"]
