from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.provenance import store as provenance_store
from openchronicle.provenance.models import EvidenceRef
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.timeline import aggregator
from openchronicle.timeline import store as timeline_store


def test_markdown_provenance_roundtrip_and_rebuild(ac_root: Path) -> None:
    source = EvidenceRef(
        kind="observation",
        id="obs_source",
        path="capture.json",
        timestamp="2026-04-21T10:00:00+00:00",
        content_hash="abc123",
    )
    with fts.cursor() as conn:
        entries_store.create_file(
            conn, name="project-provenance.md", description="test", tags=["project"]
        )
        entries_store.append_entry_once(
            conn,
            name="project-provenance.md",
            content="Grounded fact.",
            tags=["fact"],
            entry_id="grounded-entry",
            evidence_refs=[source],
        )
        subject = EvidenceRef(
            kind="memory_entry", id="grounded-entry", path="project-provenance.md"
        )
        assert provenance_store.direct_sources(conn, subject) == [source]

        parsed = files_store.read_file(files_store.memory_path("project-provenance.md"))
        assert parsed.entries[0].body == "Grounded fact."
        assert parsed.entries[0].evidence_refs == [source]
        assert "oc-provenance" in files_store.memory_path(
            "project-provenance.md"
        ).read_text()

        provenance_store.delete_subject(conn, subject)
        assert provenance_store.direct_sources(conn, subject) == []
        entries_store.rebuild_index(conn)
        assert provenance_store.direct_sources(conn, subject) == [source]
        hit = fts.search(conn, query="Grounded", top_k=3)[0]
        assert hit.content == "Grounded fact."
        assert "oc-provenance" not in hit.content


def test_deterministic_entry_replay_rejects_content_or_provenance_mismatch(
    ac_root: Path,
) -> None:
    first_source = EvidenceRef(kind="observation", id="obs_one")
    with fts.cursor() as conn:
        entries_store.create_file(
            conn, name="project-collision.md", description="test", tags=["project"]
        )
        entries_store.append_entry_once(
            conn,
            name="project-collision.md",
            content="Original fact.",
            tags=["fact"],
            entry_id="stable-entry",
            evidence_refs=[first_source],
        )
        with pytest.raises(ValueError, match="different content"):
            entries_store.append_entry_once(
                conn,
                name="project-collision.md",
                content="Changed fact.",
                tags=["fact"],
                entry_id="stable-entry",
                evidence_refs=[first_source],
            )
        with pytest.raises(ValueError, match="different provenance"):
            entries_store.append_entry_once(
                conn,
                name="project-collision.md",
                content="Original fact.",
                tags=["fact"],
                entry_id="stable-entry",
                evidence_refs=[EvidenceRef(kind="observation", id="obs_two")],
            )


def test_timeline_block_and_observation_edges_are_atomic(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK_JSON", '{"entries":["Completed release"]}')
    cfg = config_mod.Config()
    start = datetime(2026, 4, 21, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    capture_path = ac_root / "capture.json"
    parsed = [
        (
            capture_path,
            {
                "observation_id": "obs_atomic",
                "timestamp": start.isoformat(),
                "window_meta": {
                    "app_name": "Cursor",
                    "bundle_id": "com.cursor.Cursor",
                    "title": "project",
                },
                "visible_text": "Completed release",
            },
        )
    ]
    with fts.cursor() as conn:
        block = aggregator.produce_block_for_window(
            cfg, conn, start=start, end=end, parsed_captures=parsed
        )
        assert block is not None
        sources = provenance_store.direct_sources(
            conn, EvidenceRef(kind="timeline_block", id=block.id)
        )
        assert [source.id for source in sources] == ["obs_atomic"]

        assert (
            aggregator.produce_block_for_window(
                cfg, conn, start=start, end=end, parsed_captures=parsed
            )
            is None
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM provenance_edges WHERE subject_id=?", (block.id,)
        ).fetchone()[0]
        assert count == 1
        late = [
            *parsed,
            (
                ac_root / "late.json",
                {
                    "observation_id": "obs_late",
                    "timestamp": start.isoformat(),
                    "window_meta": {"app_name": "LateApp"},
                    "visible_text": "late capture",
                },
            ),
        ]
        assert (
            aggregator.produce_block_for_window(
                cfg, conn, start=start, end=end, parsed_captures=late
            )
            is None
        )
        assert [
            source.id
            for source in provenance_store.direct_sources(
                conn, EvidenceRef(kind="timeline_block", id=block.id)
            )
        ] == ["obs_atomic"]

        failed_start = end
        failed_end = failed_start + timedelta(minutes=1)

        def fail_record(*args, **kwargs):
            raise RuntimeError("injected edge failure")

        monkeypatch.setattr(provenance_store, "replace_sources", fail_record)
        with pytest.raises(RuntimeError, match="injected"):
            aggregator.produce_block_for_window(
                cfg,
                conn,
                start=failed_start,
                end=failed_end,
                parsed_captures=parsed,
            )
        assert timeline_store.get_window(conn, failed_start, failed_end) is None


def test_malformed_or_embedded_provenance_marker_fails_closed_without_truncation(
    ac_root: Path,
) -> None:
    path = files_store.memory_path("project-malformed.md")
    files_store.write_file(
        path,
        files_store.default_frontmatter(description="test", tags=["project"]),
        (
            "## [2026-04-21T10:00:00+00:00] {id: malformed-entry}\n"
            "Before\n"
            '<!-- oc-provenance: {"v":1,"sources":[]} -->\n'
            "SECRET_AFTER_MARKER\n"
        ),
    )
    parsed = files_store.read_file(path)
    assert parsed.entries[0].provenance_present is True
    assert parsed.entries[0].provenance_valid is False
    assert "SECRET_AFTER_MARKER" in parsed.entries[0].body

    with (
        fts.cursor() as conn,
        pytest.raises(ValueError, match="invalid provenance frame"),
    ):
        entries_store.rebuild_index(conn)


def test_capture_schema_adds_observation_id_to_legacy_database(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE captures (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL,
            timestamp TEXT NOT NULL,
            app_name TEXT,
            bundle_id TEXT,
            window_title TEXT,
            focused_role TEXT,
            focused_value TEXT,
            visible_text TEXT,
            url TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    migrated = fts.connect(db_path)
    try:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(captures)")}
        assert "observation_id" in columns
        fts.insert_capture(
            migrated,
            id="capture-1",
            observation_id="obs_1",
            timestamp="2026-04-21T10:00:00+00:00",
            app_name="Cursor",
            bundle_id="com.cursor.Cursor",
            window_title="project",
            focused_role="AXTextArea",
            focused_value="",
            visible_text="hello",
            url="",
        )
        assert migrated.execute(
            "SELECT observation_id FROM captures WHERE id='capture-1'"
        ).fetchone()[0] == "obs_1"
    finally:
        migrated.close()


def test_sqlite_connections_enable_secure_delete(tmp_path: Path) -> None:
    conn = fts.connect(tmp_path / "secure.db")
    try:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        conn.close()
