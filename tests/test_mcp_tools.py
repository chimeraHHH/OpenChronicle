"""Test MCP tool functions directly (bypassing FastMCP wiring)."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from openchronicle import config as config_mod
from openchronicle import paths
from openchronicle.capture import scheduler
from openchronicle.daily_wrap import store as daily_wrap_store
from openchronicle.mcp import captures as captures_mod
from openchronicle.mcp import server as mcp_server
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef, content_digest, observation_digest
from openchronicle.store import entries as entries_mod
from openchronicle.store import files as files_mod
from openchronicle.store import fts
from openchronicle.store.facts import make_fact_metadata
from openchronicle.timeline import store as timeline_store
from openchronicle.writer import tools as writer_tools


def test_list_memories(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="user-profile.md", description="identity facts", tags=["identity"]
        )
        entries_mod.create_file(
            conn, name="project-foo.md", description="Foo project", tags=["project"]
        )
        entries_mod.append_entry(
            conn,
            name="user-profile.md",
            content="User-authored identity fact.",
            tags=["identity"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        entries_mod.append_entry(
            conn,
            name="project-foo.md",
            content="User-authored Foo project note.",
            tags=["project"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        out = mcp_server._list_memories(conn, cfg=cfg)
    assert out["count"] == 2
    paths = {f["path"] for f in out["files"]}
    assert paths == {"user-profile.md", "project-foo.md"}


def test_read_memory_with_tail(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="topic-x.md", description="Topic X", tags=["topic"])
        for i in range(3):
            entries_mod.append_entry(
                conn,
                name="topic-x.md",
                content=f"fact {i}",
                tags=["x"],
                origin=files_mod.MANUAL_ENTRY_ORIGIN,
            )
        out = mcp_server._read_memory(conn, cfg=cfg, path="topic-x.md", tail_n=2)
    assert len(out["entries"]) == 2


def test_search(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="tool-vim.md", description="vim", tags=["tool"])
        entries_mod.append_entry(
            conn,
            name="tool-vim.md",
            content="User uses vim for editing.",
            tags=["editor"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        out = mcp_server._search(conn, cfg=cfg, query="vim", top_k=3)
    assert out["results"]
    assert out["results"][0]["path"] == "tool-vim.md"


def test_search_as_of_returns_only_the_revision_active_at_that_time(
    ac_root: Path,
    monkeypatch,
) -> None:
    cfg = config_mod.Config()
    clock = ["2026-08-20T12:00"]
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: clock[0])
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="project-openchronicle.md",
            description="OpenChronicle project facts",
            tags=["project"],
        )
        old_id, _ = entries_mod.append_entry_once(
            conn,
            name="project-openchronicle.md",
            content="The timeline model is gpt-5.4-nano.",
            tags=["project", "timeline", "model"],
            entry_id="timeline-model-old",
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
            fact_metadata=make_fact_metadata(
                subject_key="project.openchronicle.timeline-model",
                assertion_kind="user_asserted",
                valid_from="2026-08-20",
            ),
        )
        clock[0] = "2026-08-23T12:00"
        new_id = entries_mod.supersede_entry(
            conn,
            name="project-openchronicle.md",
            old_entry_id=old_id,
            new_entry_id="timeline-model-current",
            new_content="The timeline model is gpt-5.6-luna.",
            reason="explicit model update",
            tags=["project", "timeline", "model"],
            fact_metadata=make_fact_metadata(
                subject_key="project.openchronicle.timeline-model",
                assertion_kind="user_asserted",
                valid_from="2026-08-23",
            ),
        )

        before = mcp_server._search(
            conn,
            cfg=cfg,
            query="timeline model",
            top_k=5,
            as_of="2026-08-21T12:00:00+08:00",
        )
        after = mcp_server._search(
            conn,
            cfg=cfg,
            query="timeline model",
            top_k=5,
            as_of="2026-08-23T13:00:00+08:00",
        )
        current = mcp_server._search(conn, cfg=cfg, query="timeline model", top_k=5)

    assert [item["id"] for item in before["results"]] == [old_id]
    assert before["results"][0]["content"] == "The timeline model is gpt-5.4-nano."
    assert [item["id"] for item in after["results"]] == [new_id]
    assert [item["id"] for item in current["results"]] == [new_id]


def test_search_as_of_rejects_invalid_time_without_recall(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        out = mcp_server._search(
            conn,
            cfg=cfg,
            query="anything",
            as_of="not-a-time",
        )

    assert out == {
        "query": "anything",
        "as_of": "not-a-time",
        "retrieval_mode": "invalid_as_of",
        "error": "as_of must be an ISO 8601 date or timestamp",
        "results": [],
    }


def test_recent_activity(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="event-2026-04-22.md", description="week", tags=["event"]
        )
        entries_mod.append_entry(
            conn,
            name="event-2026-04-22.md",
            content="Did a thing.",
            tags=["x"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        out = mcp_server._recent_activity(conn, cfg=cfg, limit=5)
    assert out["count"] >= 1


def test_get_schema() -> None:
    out = mcp_server._get_schema()
    assert "Memory Organization Spec" in out["schema"]


def test_memory_reads_and_source_drawer_include_provenance(ac_root: Path) -> None:
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    capture = {
        "timestamp": scheduler._now_iso(),
        "schema_version": 4,
        "window_meta": {
            "app_name": "Editor",
            "bundle_id": "com.example.editor",
            "title": "Grounded fixture",
        },
        "focused_element": {"role": "AXTextArea", "value": "Grounded"},
        "visible_text": "Grounded project fact.",
        "url": "",
    }
    capture_path = scheduler._write_capture(capture)
    source = EvidenceRef(
        kind="observation",
        id=str(capture["observation_id"]),
        path=capture_path.name,
        content_hash=observation_digest(capture),
    )
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
        read = mcp_server._read_memory(conn, cfg=cfg, path="project-grounded.md")
        drawer = mcp_server._get_provenance(
            conn,
            cfg=cfg,
            kind="memory_entry",
            artifact_id="grounded-fact",
            path="project-grounded.md",
        )
    assert read["entries"][0]["evidence"][0]["id"] == source.id
    assert drawer["direct_sources"][0]["availability"] == "available"


def test_memory_read_surfaces_hide_entries_with_stale_memory_dependencies(
    ac_root: Path,
) -> None:
    source_name = "user-stale-source.md"
    derived_name = "project-stale-derived.md"
    leaf_name = "topic-stale-leaf.md"
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name=source_name, description="source", tags=["user"])
        source_id = entries_mod.append_entry(
            conn,
            name=source_name,
            content="STALE_SOURCE_PRIVATE",
            tags=["source"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        source_ref = EvidenceRef(
            kind="memory_entry",
            id=source_id,
            path=source_name,
            content_hash=content_digest("STALE_SOURCE_PRIVATE"),
        )
        entries_mod.create_file(conn, name=derived_name, description="derived", tags=["project"])
        entries_mod.append_entry_once(
            conn,
            name=derived_name,
            content="STALE_DERIVED_PRIVATE",
            tags=["derived"],
            entry_id="stale-derived-entry",
            evidence_refs=[source_ref],
        )
        entries_mod.create_file(conn, name=leaf_name, description="leaf", tags=["topic"])
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

        read = mcp_server._read_memory(conn, cfg=cfg, path=derived_name)
        leaf_read = mcp_server._read_memory(conn, cfg=cfg, path=leaf_name)
        search = mcp_server._search(conn, cfg=cfg, query="STALE_DERIVED_PRIVATE")
        leaf_search = mcp_server._search(conn, cfg=cfg, query="STALE_LEAF_PRIVATE")
        recent = mcp_server._recent_activity(conn, cfg=cfg)
        classifier_read = writer_tools.tool_read_memory(conn, cfg, path=derived_name)
        classifier_leaf_read = writer_tools.tool_read_memory(conn, cfg, path=leaf_name)
        classifier_search = writer_tools.tool_search_memory(
            conn, cfg, query="STALE_DERIVED_PRIVATE"
        )
        classifier_leaf_search = writer_tools.tool_search_memory(
            conn, cfg, query="STALE_LEAF_PRIVATE"
        )

    assert "error" in read
    assert "error" in leaf_read
    assert search["results"] == []
    assert leaf_search["results"] == []
    assert all(
        entry["id"] not in {"stale-derived-entry", "stale-leaf-entry"}
        for entry in recent["entries"]
    )
    assert "error" in classifier_read
    assert "error" in classifier_leaf_read
    assert classifier_search["results"] == []
    assert classifier_leaf_search["results"] == []


def test_mcp_hides_pending_candidate_subjects_and_tombstoned_entries(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        denied = mcp_server._get_provenance(
            conn,
            cfg=cfg,
            kind="memory_candidate",
            artifact_id="mc-secret",
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
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        candidate_store.put_tombstone(
            conn,
            kind="memory_entry",
            artifact_id="purging-entry",
            path="project-purging.md",
        )
        read = mcp_server._read_memory(conn, cfg=cfg, path="project-purging.md")
        search = mcp_server._search(conn, cfg=cfg, query="TOMBSTONED_SECRET")
        recent = mcp_server._recent_activity(conn, cfg=cfg)
        provenance = mcp_server._get_provenance(
            conn,
            cfg=cfg,
            kind="memory_entry",
            artifact_id="purging-entry",
            path="project-purging.md",
        )
        assert "error" in read
        assert search["results"] == []
        assert all(item["id"] != "purging-entry" for item in recent["entries"])
        assert "error" in provenance


def test_memory_reads_reject_case_and_unicode_aliases_of_tombstoned_paths(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    name = "project-café-secret.md"
    alternate_case = "Project-café-secret.md"
    alternate_unicode = "project-cafe\u0301-secret.md"
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name=name, description="private", tags=["project"])
        entries_mod.append_entry_once(
            conn,
            name=name,
            content="CANONICAL_PATH_PRIVATE_MARKER",
            tags=["private"],
            entry_id="canonical-private-entry",
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        candidate_store.put_tombstone(conn, kind="memory_file", artifact_id=name)
        assert "error" in mcp_server._read_memory(conn, cfg=cfg, path=alternate_case)
        assert "error" in mcp_server._read_memory(conn, cfg=cfg, path=alternate_unicode)

        candidate_store.delete_tombstone(conn, kind="memory_file", artifact_id=name)
        candidate_store.put_tombstone(
            conn,
            kind="memory_entry",
            artifact_id="canonical-private-entry",
            path=name,
        )
        assert "error" in mcp_server._read_memory(conn, cfg=cfg, path=name)
        assert "error" in mcp_server._read_memory(conn, cfg=cfg, path=alternate_case)
        assert "error" in mcp_server._read_memory(conn, cfg=cfg, path=alternate_unicode)


def test_daily_wrap_mcp_helpers_are_read_only(ac_root: Path) -> None:
    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
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
            window_start_utc="2026-04-21T00:00:00+00:00",
            window_end_utc="2026-04-22T00:00:00+00:00",
            workflow_version=1,
            coverage_status="partial",
            output={
                "schema_version": 1,
                "local_date": "2026-04-21",
                "timezone": "UTC",
                "status": "partial",
                "summary": "No grounded activity items were available.",
                "completed": [],
                "progressed": [],
                "open": [],
                "blocked": [],
                "needs_review": [],
                "coverage_gaps": [],
                "generated_at": "2026-04-22T00:00:00+00:00",
            },
            sources=[],
            validate_input_current=lambda: None,
        )
        out = mcp_server._get_daily_wrap(
            conn,
            cfg=cfg,
            local_date="2026-04-21",
            timezone="UTC",
        )
        listing = mcp_server._list_daily_wraps(conn, cfg=cfg)
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


def test_list_daily_wraps_pages_past_hidden_rows(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        SimpleNamespace(
            id=f"hidden-wrap-{index:03d}",
            to_dict=lambda index=index: {"id": f"hidden-wrap-{index:03d}"},
        )
        for index in range(100)
    ]
    rows.append(
        SimpleNamespace(
            id="visible-wrap-101",
            to_dict=lambda: {"id": "visible-wrap-101"},
        )
    )
    offsets: list[int] = []

    def fake_list_wraps(conn, *, limit: int, offset: int = 0):  # noqa: ARG001
        offsets.append(offset)
        return rows[offset : offset + limit]

    monkeypatch.setattr(daily_wrap_store, "list_wraps", fake_list_wraps)
    monkeypatch.setattr(
        mcp_server.ContextService,
        "daily_wrap_allowed",
        lambda self, wrap_id, **kwargs: wrap_id == "visible-wrap-101",  # noqa: ARG005
    )

    with fts.cursor() as conn:
        result = mcp_server._list_daily_wraps(
            conn,
            cfg=config_mod.Config(),
            limit=1,
        )

    assert offsets == [0, 100]
    assert result == {"count": 1, "wraps": [{"id": "visible-wrap-101"}]}


# ─── search_captures + current_context ────────────────────────────────────


def _seed_capture(
    conn,
    *,
    id,
    ts,
    app,
    title,
    value,
    text,
    url="",
    bundle_id="",
    focused_role="AXTextArea",
) -> EvidenceRef:
    bundle_id = bundle_id or "com.test." + app.lower()
    capture_path = paths.capture_buffer_dir() / f"{id}.json"
    if not capture_path.exists():
        observation_id = "obs_" + hashlib.blake2s(str(id).encode(), digest_size=16).hexdigest()
        capture_path.write_text(
            json.dumps(
                {
                    "timestamp": ts,
                    "schema_version": 4,
                    "observation_id": observation_id,
                    "window_meta": {
                        "app_name": app,
                        "bundle_id": bundle_id,
                        "title": title,
                    },
                    "focused_element": {
                        "role": focused_role,
                        "value": value,
                    },
                    "visible_text": text,
                    "url": url,
                }
            ),
            encoding="utf-8",
        )
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    observation_id = str(capture.get("observation_id") or f"legacy:{capture_path.stem}")
    fts.insert_capture(
        conn,
        id=id,
        timestamp=ts,
        app_name=app,
        bundle_id=bundle_id,
        window_title=title,
        focused_role=focused_role,
        focused_value=value,
        visible_text=text,
        url=url,
        observation_id=str(capture.get("observation_id") or ""),
    )
    return EvidenceRef(
        kind="observation",
        id=observation_id,
        path=capture_path.name,
        timestamp=ts,
        content_hash=observation_digest(capture),
    )


def _attested_screenshot_capture() -> dict[str, object]:
    observation_meta = {
        "app_name": "Editor",
        "bundle_id": "com.example.editor",
        "title": "Roadmap",
        "pid": 1234,
        "window_id": 5678,
        "bounds": {"x": 10.0, "y": 20.0, "width": 900.0, "height": 700.0},
    }
    return {
        "timestamp": "2026-08-08T12:00:00+08:00",
        "schema_version": 4,
        "observation_id": "obs_0123456789abcdef",
        "window_meta": observation_meta,
        "focused_element": {"role": "AXTextArea", "value": "safe text"},
        "visible_text": "safe text",
        "url": "",
        "screenshot": {
            "capture_mode": "exact_window_v1",
            "image_base64": "/9j/attested-jpeg",
            "mime_type": "image/jpeg",
            "width": 900,
            "height": 700,
            "window_meta": {"schema_version": 1, **observation_meta},
        },
    }


def test_mcp_returns_only_configured_exact_window_attested_screenshot(
    ac_root: Path,
) -> None:
    stem = "2026-08-08T12-00-00p08-00_obs_0123456789abcdef"
    (paths.capture_buffer_dir() / f"{stem}.json").write_text(
        json.dumps(_attested_screenshot_capture()),
        encoding="utf-8",
    )
    enabled = config_mod.Config()
    enabled.capture.include_screenshot = True

    result = captures_mod.read_recent_capture(
        cfg=enabled,
        include_screenshot=True,
    )

    assert result is not None
    assert result["screenshot_b64"] == "/9j/attested-jpeg"
    assert result["screenshot_mime"] == "image/jpeg"

    disabled = config_mod.Config()
    disabled_result = captures_mod.read_recent_capture(
        cfg=disabled,
        include_screenshot=True,
    )
    assert disabled_result is not None
    assert "screenshot_b64" not in disabled_result
    assert "screenshot_mime" not in disabled_result


@pytest.mark.parametrize(
    "mismatch",
    (
        "observation_schema",
        "nested_schema",
        "mime_type",
        "capture_mode",
        "nested_pid",
        "nested_window_id",
        "nested_bounds",
        "nested_title",
    ),
)
def test_mcp_rejects_unattested_or_mismatched_screenshot(mismatch: str) -> None:
    capture = _attested_screenshot_capture()
    shot = capture["screenshot"]
    assert isinstance(shot, dict)
    nested_meta = shot["window_meta"]
    assert isinstance(nested_meta, dict)

    if mismatch == "observation_schema":
        capture["schema_version"] = 5
    elif mismatch == "nested_schema":
        nested_meta["schema_version"] = 2
    elif mismatch == "mime_type":
        shot["mime_type"] = "image/png"
    elif mismatch == "capture_mode":
        shot["capture_mode"] = "full_display_v0"
    elif mismatch == "nested_pid":
        nested_meta["pid"] = 9999
    elif mismatch == "nested_window_id":
        nested_meta["window_id"] = 9999
    elif mismatch == "nested_bounds":
        nested_meta["bounds"] = {
            "x": 11.0,
            "y": 20.0,
            "width": 900.0,
            "height": 700.0,
        }
    elif mismatch == "nested_title":
        nested_meta["title"] = "Different window"
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(f"unknown mismatch: {mismatch}")

    result = captures_mod._format_response(
        Path("capture.json"),
        capture,
        include_screenshot=True,
    )

    assert "screenshot_b64" not in result
    assert "screenshot_mime" not in result


def test_search_captures_returns_bm25_hits_with_snippet(ac_root: Path) -> None:
    with fts.cursor() as conn:
        _seed_capture(
            conn,
            id="c1",
            ts="2026-04-22T14:00:00+08:00",
            app="Cursor",
            title="main.py",
            value="def foo()",
            text="def foo(): return 1",
        )
        _seed_capture(
            conn,
            id="c2",
            ts="2026-04-22T14:05:00+08:00",
            app="Safari",
            title="docs",
            value="",
            text="reading about rate limiter design",
        )

    results = captures_mod.search_captures(cfg=config_mod.Config(), query="rate limiter")
    assert len(results) == 1
    r = results[0]
    assert r["file_stem"] == "c2"
    assert r["app_name"] == "Safari"
    assert "[rate]" in r["snippet"] and "[limiter]" in r["snippet"]
    assert r["content_mode"] == "normal"


def test_capture_reads_page_past_hidden_recall_rows(ac_root: Path) -> None:
    marker = "CAPTURE_DEEP_PAGE_MARKER"
    with fts.cursor() as conn:
        for index in range(100):
            fts.insert_capture(
                conn,
                id=f"missing-capture-{index:03d}",
                timestamp="2026-08-08T13:00:00+00:00",
                app_name="Editor",
                bundle_id="com.test.editor",
                window_title="Deep page",
                focused_role="AXTextArea",
                focused_value=marker,
                visible_text=marker,
                url="",
            )
        _seed_capture(
            conn,
            id="visible-capture-101",
            ts="2026-08-08T12:00:00+00:00",
            app="Editor",
            title="Deep page",
            value=marker,
            text=marker,
        )

    search = captures_mod.search_captures(
        cfg=config_mod.Config(),
        query=marker,
        limit=1,
    )
    context = captures_mod.current_context(
        cfg=config_mod.Config(),
        headline_limit=1,
        fulltext_limit=1,
        timeline_limit=0,
    )

    assert [item["file_stem"] for item in search] == ["visible-capture-101"]
    assert [item["file_stem"] for item in context["recent_captures_headline"]] == [
        "visible-capture-101"
    ]
    assert [item["file_stem"] for item in context["recent_captures_fulltext"]] == [
        "visible-capture-101"
    ]


def test_current_context_pages_past_invalid_timeline_rows(ac_root: Path) -> None:
    start = datetime.fromisoformat("2026-08-08T12:00:00+00:00")
    with fts.cursor() as conn:
        source = _seed_capture(
            conn,
            id="timeline-page-source",
            ts=start.isoformat(),
            app="Editor",
            title="Timeline paging",
            value="CURRENT_TIMELINE_PAGE",
            text="CURRENT_TIMELINE_PAGE",
        )
        block = timeline_store.TimelineBlock(
            id="visible-timeline-101",
            start_time=start,
            end_time=start + timedelta(minutes=1),
            entries=["CURRENT_TIMELINE_PAGE"],
            apps_used=["Editor"],
            capture_count=1,
        )
        timeline_store.insert(conn, block)
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=block.id),
            sources=[source],
        )
        for index in range(100):
            invalid_start = start + timedelta(days=1, minutes=index)
            conn.execute(
                """
                INSERT INTO timeline_blocks(
                    id, start_time, end_time, timezone, entries, apps_used,
                    capture_count, created_at, projection_digest, source_digest
                ) VALUES (?, ?, ?, 'UTC', '[]', '[]', 0, ?, '', '')
                """,
                (
                    f"invalid-timeline-{index:03d}",
                    invalid_start.isoformat(),
                    (invalid_start + timedelta(minutes=1)).isoformat(),
                    invalid_start.isoformat(),
                ),
            )

    context = captures_mod.current_context(
        cfg=config_mod.Config(),
        headline_limit=0,
        fulltext_limit=0,
        timeline_limit=1,
    )

    assert [item["id"] for item in context["recent_timeline_blocks"]] == [block.id]


def test_mcp_capture_results_label_url_metadata_as_uncommitted_address_text(
    ac_root: Path,
) -> None:
    capture_id = "url-metadata-capture"
    capture = {
        "timestamp": "2026-04-22T14:05:00+08:00",
        "window_meta": {
            "app_name": "Safari",
            "bundle_id": "com.apple.Safari",
            "title": "",
        },
        "privacy": {
            "policy_version": 3,
            "content_mode": "url_metadata_only",
        },
        "url": "https://allowed.example/uncommitted",
        "visible_text": "",
    }
    (ac_root / "capture-buffer" / f"{capture_id}.json").write_text(
        json.dumps(capture),
        encoding="utf-8",
    )
    with fts.cursor() as conn:
        _seed_capture(
            conn,
            id=capture_id,
            ts=capture["timestamp"],
            app="Safari",
            title="",
            value="",
            text="",
            url=capture["url"],
            bundle_id="com.apple.Safari",
            focused_role="",
        )

    search = captures_mod.search_captures(cfg=config_mod.Config(), query="allowed uncommitted")
    assert search[0]["content_mode"] == "url_metadata_only"
    assert "may_be_uncommitted" in search[0]["url_semantics"]
    context = captures_mod.current_context(
        cfg=config_mod.Config(), headline_limit=1, fulltext_limit=1
    )
    full = context["recent_captures_fulltext"][0]
    assert full["content_mode"] == "url_metadata_only"
    assert "not_visit_or_read_evidence" in full["url_semantics"]
    assert full["visible_text"] == ""


def test_search_captures_app_and_time_filters(ac_root: Path) -> None:
    with fts.cursor() as conn:
        _seed_capture(
            conn,
            id="c1",
            ts="2026-04-22T13:00:00+08:00",
            app="Cursor",
            title="a.py",
            value="",
            text="login flow stuff",
        )
        _seed_capture(
            conn,
            id="c2",
            ts="2026-04-22T14:00:00+08:00",
            app="Safari",
            title="docs",
            value="",
            text="login flow stuff",
        )
        _seed_capture(
            conn,
            id="c3",
            ts="2026-04-22T15:00:00+08:00",
            app="Cursor",
            title="b.py",
            value="",
            text="login flow stuff",
        )

    cursor_only = captures_mod.search_captures(
        cfg=config_mod.Config(), query="login flow", app_name="Cursor"
    )
    assert {h["file_stem"] for h in cursor_only} == {"c1", "c3"}

    bounded = captures_mod.search_captures(
        cfg=config_mod.Config(),
        query="login flow",
        since="2026-04-22T13:30:00+08:00",
        until="2026-04-22T14:30:00+08:00",
    )
    assert {h["file_stem"] for h in bounded} == {"c2"}


def test_current_context_shape(ac_root: Path) -> None:
    """Headlines newest-first, fulltext deduped by (app,window), timeline blocks ordered."""
    tz = timezone(timedelta(hours=8))
    with fts.cursor() as conn:
        # Five captures, two from the same (app, window) pair so dedup should drop one.
        cursor_source = _seed_capture(
            conn,
            id="c1",
            ts="2026-04-22T14:00:00+08:00",
            app="Cursor",
            title="main.py",
            value="x=1",
            text="A",
        )
        safari_source = _seed_capture(
            conn,
            id="c2",
            ts="2026-04-22T14:01:00+08:00",
            app="Safari",
            title="docs",
            value="",
            text="B",
        )
        _seed_capture(
            conn,
            id="c3",
            ts="2026-04-22T14:02:00+08:00",
            app="Cursor",
            title="main.py",
            value="x=2",
            text="C",
        )
        _seed_capture(
            conn,
            id="c4",
            ts="2026-04-22T14:03:00+08:00",
            app="Slack",
            title="#general",
            value="",
            text="D",
        )
        _seed_capture(
            conn,
            id="c5",
            ts="2026-04-22T14:04:00+08:00",
            app="Mail",
            title="Inbox",
            value="",
            text="E",
        )

        # Two timeline blocks
        cursor_block = timeline_store.TimelineBlock(
            start_time=datetime(2026, 4, 22, 14, 0, tzinfo=tz),
            end_time=datetime(2026, 4, 22, 14, 1, tzinfo=tz),
            entries=["[Cursor] editing main.py"],
            apps_used=["Cursor"],
            capture_count=2,
        )
        safari_block = timeline_store.TimelineBlock(
            start_time=datetime(2026, 4, 22, 14, 1, tzinfo=tz),
            end_time=datetime(2026, 4, 22, 14, 2, tzinfo=tz),
            entries=["[Safari] reading docs"],
            apps_used=["Safari"],
            capture_count=1,
        )
        timeline_store.insert(conn, cursor_block)
        timeline_store.insert(conn, safari_block)
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=cursor_block.id),
            sources=[cursor_source],
        )
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="timeline_block", id=safari_block.id),
            sources=[safari_source],
        )

    cfg = config_mod.Config()
    cfg.capture.deny_unknown_windows = False
    ctx = captures_mod.current_context(
        cfg=cfg,
        headline_limit=5,
        fulltext_limit=3,
        timeline_limit=10,
    )
    # Headlines: newest-first, all 5 captures.
    assert [h["file_stem"] for h in ctx["recent_captures_headline"]] == [
        "c5",
        "c4",
        "c3",
        "c2",
        "c1",
    ]

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
        _seed_capture(
            conn,
            id="c1",
            ts="2026-04-22T14:00:00+08:00",
            app="Cursor",
            title="a",
            value="",
            text="A",
        )
        _seed_capture(
            conn,
            id="c2",
            ts="2026-04-22T14:01:00+08:00",
            app="Safari",
            title="b",
            value="",
            text="B",
        )

    ctx = captures_mod.current_context(
        cfg=config_mod.Config(), app_filter="Safari", headline_limit=5
    )
    assert [h["file_stem"] for h in ctx["recent_captures_headline"]] == ["c2"]
