"""SQLite memory-entry rows are recall hints, never public authority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle import desktop_bridge
from openchronicle.mcp import server as mcp_server
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef, content_digest
from openchronicle.store import entries as entries_mod
from openchronicle.store import files as files_mod
from openchronicle.store import fts
from openchronicle.writer import tools as writer_tools

_MARKER = "CANONICAL_FTS_PROJECTION_MARKER"
_PATH = "topic-fts-projection.md"


def test_mcp_pages_past_one_hundred_hidden_recall_rows(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config_mod.Config()
    path = "project-mcp-deep-page.md"
    marker = "MCP_DEEP_PAGE_CANONICAL_MARKER"
    hidden_ids = [f"hidden-recall-{index:03d}" for index in range(100)]
    visible_id = "visible-recall-101"
    timestamps = iter(["2026-08-08T12:01"] * len(hidden_ids) + ["2026-08-08T12:00"])
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: next(timestamps))

    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=path,
            description="MCP paging regression fixture",
            tags=["project"],
        )
        for entry_id in [*hidden_ids, visible_id]:
            entries_mod.append_entry_once(
                conn,
                name=path,
                content=marker,
                tags=["paging"],
                entry_id=entry_id,
                origin=files_mod.MANUAL_ENTRY_ORIGIN,
            )
        for entry_id in hidden_ids:
            candidate_store.put_tombstone(
                conn,
                kind="memory_entry",
                artifact_id=entry_id,
                path=path,
            )

        first_search_page = fts.search(conn, query=marker, top_k=100)
        first_recent_page = fts.recent(conn, limit=100)
        assert len(first_search_page) == len(first_recent_page) == 100
        assert visible_id not in {hit.id for hit in first_search_page}
        assert visible_id not in {hit.id for hit in first_recent_page}

        search = mcp_server._search(conn, cfg=cfg, query=marker, top_k=1)
        recent = mcp_server._recent_activity(conn, cfg=cfg, limit=1)
        writer_search = writer_tools.tool_search_memory(
            conn,
            cfg,
            query=marker,
            top_k=1,
        )

    assert [item["id"] for item in search["results"]] == [visible_id]
    assert [item["id"] for item in recent["entries"]] == [visible_id]
    assert [item["id"] for item in writer_search["results"]] == [visible_id]


@pytest.mark.parametrize(
    ("column", "forged_value"),
    [
        ("id", "forged-entry-id"),
        ("path", "topic-forged-path.md"),
        ("prefix", "forged-prefix"),
        ("timestamp", "9999-12-31T23:59:FTS_TIMESTAMP_SECRET"),
        ("tags", "canonical-tag FTS_TAG_SECRET"),
        ("content", f"{_MARKER} FTS_CONTENT_SECRET"),
        ("superseded", 1),
    ],
)
def test_sqlite_entry_projection_tamper_is_hidden_from_mcp_and_writer(
    ac_root: Path,
    column: str,
    forged_value: object,
) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=_PATH,
            description="canonical projection fixture",
            tags=["topic"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name=_PATH,
            content=_MARKER,
            tags=["canonical-tag"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )

        assert [
            item["id"]
            for item in mcp_server._search(
                conn,
                cfg=cfg,
                query=_MARKER,
                include_superseded=True,
            )["results"]
        ] == [entry_id]
        assert [
            item["id"]
            for item in writer_tools.tool_search_memory(
                conn,
                cfg,
                query=_MARKER,
                include_superseded=True,
            )["results"]
        ] == [entry_id]

        # The column name comes only from the fixed parameter table above.
        conn.execute(
            f"UPDATE entries SET {column}=? WHERE id=? AND path=?",
            (forged_value, entry_id, _PATH),
        )

        mcp_search = mcp_server._search(
            conn,
            cfg=cfg,
            query=_MARKER,
            include_superseded=True,
        )
        mcp_recent = mcp_server._recent_activity(conn, cfg=cfg)
        writer_search = writer_tools.tool_search_memory(
            conn,
            cfg,
            query=_MARKER,
            include_superseded=True,
        )

    assert mcp_search["results"] == []
    assert mcp_recent["entries"] == []
    assert writer_search["results"] == []
    serialized = json.dumps(
        [mcp_search, mcp_recent, writer_search],
        ensure_ascii=False,
    )
    assert str(forged_value) not in serialized


def test_forged_active_superseded_row_cannot_bypass_default_filters(
    ac_root: Path,
) -> None:
    cfg = config_mod.Config()
    marker = "SUPERSEDED_FTS_BYPASS_MARKER"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=_PATH,
            description="superseded projection fixture",
            tags=["topic"],
        )
        old_id = entries_mod.append_entry(
            conn,
            name=_PATH,
            content=marker,
            tags=["canonical-tag"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        entries_mod.supersede_entry(
            conn,
            name=_PATH,
            old_entry_id=old_id,
            new_content="Canonical replacement fact.",
            reason="fixture replacement",
            tags=["canonical-tag"],
        )

        # A legitimate superseded row has a complete, matching projection and
        # is available only when the caller explicitly asks for old entries.
        explicit = mcp_server._search(
            conn,
            cfg=cfg,
            query=marker,
            include_superseded=True,
        )
        assert [item["id"] for item in explicit["results"]] == [old_id]

        conn.execute(
            "UPDATE entries SET superseded=0 WHERE id=? AND path=?",
            (old_id, _PATH),
        )
        assert [hit.id for hit in fts.search(conn, query=marker, include_superseded=False)] == [
            old_id
        ]

        mcp_search = mcp_server._search(conn, cfg=cfg, query=marker)
        mcp_recent = mcp_server._recent_activity(conn, cfg=cfg)
        writer_search = writer_tools.tool_search_memory(
            conn,
            cfg,
            query=marker,
        )

    assert all(item["id"] != old_id for item in mcp_search["results"])
    assert all(item["id"] != old_id for item in mcp_recent["entries"])
    assert all(item["id"] != old_id for item in writer_search["results"])


def test_inline_markdown_strike_is_stable_across_index_rebuild(ac_root: Path) -> None:
    cfg = config_mod.Config()
    content = "The ~~legacy~~ canonical value remains searchable."
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=_PATH,
            description="inline strike fixture",
            tags=["topic"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name=_PATH,
            content=content,
            tags=["canonical-tag"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        before = mcp_server._search(conn, cfg=cfg, query="canonical value")
        entries_mod.rebuild_index(conn)
        after = writer_tools.tool_search_memory(
            conn,
            cfg,
            query="canonical value",
        )

    assert [(item["id"], item["content"]) for item in before["results"]] == [(entry_id, content)]
    assert [(item["id"], item["content"]) for item in after["results"]] == [(entry_id, content)]


def test_forged_manual_entry_edges_are_hidden_from_every_provenance_surface(
    ac_root: Path,
) -> None:
    marker = "MANUAL_CANONICAL_PROVENANCE_MARKER"
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=_PATH,
            description="manual provenance fixture",
            tags=["topic"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name=_PATH,
            content=marker,
            tags=["manual"],
            origin=files_mod.MANUAL_ENTRY_ORIGIN,
        )
        subject = EvidenceRef(
            kind="memory_entry",
            id=entry_id,
            path=_PATH,
            content_hash=content_digest(marker),
        )
        forged = EvidenceRef(
            kind="observation",
            id="FORGED_EDGE_ID_SECRET",
            path="FORGED_EDGE_PATH_SECRET.json",
            timestamp="FORGED_EDGE_TS_SECRET",
            content_hash="FORGED_EDGE_HASH_SECRET",
        )
        provenance_store.record_sources(conn, subject=subject, sources=[forged])

        search = mcp_server._search(conn, cfg=cfg, query=marker)
        recent = mcp_server._recent_activity(conn, cfg=cfg)
        provenance = mcp_server._get_provenance(
            conn,
            cfg=cfg,
            kind="memory_entry",
            artifact_id=entry_id,
            path=_PATH,
        )

    assert search["results"] == []
    assert all(item["id"] != entry_id for item in recent["entries"])
    assert "error" in provenance
    with pytest.raises(KeyError):
        desktop_bridge._provenance_trace(
            {
                "kind": "memory_entry",
                "artifact_id": entry_id,
                "path": _PATH,
                "max_depth": 4,
            }
        )
    serialized = json.dumps([search, recent, provenance], ensure_ascii=False)
    for forbidden in (
        "FORGED_EDGE_ID_SECRET",
        "FORGED_EDGE_PATH_SECRET",
        "FORGED_EDGE_TS_SECRET",
        "FORGED_EDGE_HASH_SECRET",
    ):
        assert forbidden not in serialized
