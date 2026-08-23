from __future__ import annotations

from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.config import SearchConfig
from openchronicle.mcp import server as mcp_server
from openchronicle.store import entries as entries_mod
from openchronicle.store import files as files_mod
from openchronicle.store import fts, semantic
from openchronicle.writer import tools as writer_tools


class FixtureEmbedder:
    model_id = "fixture:semantic-v1"

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.lower()
        if "typora" in lowered or "writing application" in lowered:
            return [1.0, 0.0, 0.0, 0.0]
        if (
            "sqlite" in lowered
            or "postgresql" in lowered
            or "database" in lowered
            or "本地 数据库" in lowered
        ):
            return [0.0, 1.0, 0.0, 0.0]
        if "cursor" in lowered:
            return [0.0, 0.0, 1.0, 0.0]
        return [0.0, 0.0, 0.0, 1.0]


def _seed(conn) -> dict[str, str]:
    entries_mod.create_file(
        conn,
        name="user-preferences.md",
        description="preferences",
        tags=["user", "preference"],
    )
    typora = entries_mod.append_entry(
        conn,
        name="user-preferences.md",
        content="User prefers Typora for long-form Markdown editing.",
        tags=["editor"],
        origin=files_mod.MANUAL_ENTRY_ORIGIN,
    )
    entries_mod.create_file(
        conn,
        name="project-openchronicle.md",
        description="project",
        tags=["project"],
    )
    sqlite = entries_mod.append_entry(
        conn,
        name="project-openchronicle.md",
        content="OpenChronicle uses SQLite as its local database.",
        tags=["database"],
        origin=files_mod.MANUAL_ENTRY_ORIGIN,
    )
    return {"typora": typora, "sqlite": sqlite}


def test_hybrid_search_recalls_semantic_and_cross_language_queries(ac_root: Path) -> None:
    with fts.cursor() as conn:
        ids = _seed(conn)
        embedder = FixtureEmbedder()

        semantic_hits = semantic.hybrid_search(
            conn,
            query="writing application",
            embedder=embedder,
            top_k=3,
        )
        cross_language_hits = semantic.hybrid_search(
            conn,
            query="本地 数据库",
            embedder=embedder,
            top_k=3,
        )

    assert semantic_hits[0].id == ids["typora"]
    assert semantic_hits[0].bm25_rank is None
    assert semantic_hits[0].vector_rank == 1
    assert cross_language_hits[0].id == ids["sqlite"]


def test_hybrid_search_fuses_bm25_and_vector_ranks(ac_root: Path) -> None:
    with fts.cursor() as conn:
        ids = _seed(conn)
        hits = semantic.hybrid_search(
            conn,
            query="Typora Markdown",
            embedder=FixtureEmbedder(),
            top_k=3,
        )

    assert hits[0].id == ids["typora"]
    assert hits[0].bm25_rank == 1
    assert hits[0].vector_rank == 1
    assert hits[0].score == pytest.approx(2 / 61)


def test_semantic_threshold_preserves_abstention(ac_root: Path) -> None:
    with fts.cursor() as conn:
        _seed(conn)
        hits = semantic.hybrid_search(
            conn,
            query="passport number",
            embedder=FixtureEmbedder(),
            top_k=3,
            min_similarity=0.75,
        )

    assert hits == []


def test_lexical_entity_anchor_blocks_cross_scope_vector_expansion(ac_root: Path) -> None:
    with fts.cursor() as conn:
        for project, database in (("atlas", "PostgreSQL"), ("apollo", "SQLite")):
            name = f"project-{project}.md"
            entries_mod.create_file(
                conn,
                name=name,
                description=project,
                tags=["project"],
            )
            entries_mod.append_entry(
                conn,
                name=name,
                content=f"Project {project.title()} deploys with {database} as its database.",
                tags=["database"],
            )
        hits = semantic.hybrid_search(
            conn,
            query="Atlas database",
            embedder=FixtureEmbedder(),
            top_k=5,
        )

    assert [hit.path for hit in hits] == ["project-atlas.md"]


def test_semantic_projection_is_rebuildable_and_removes_orphans(ac_root: Path) -> None:
    with fts.cursor() as conn:
        ids = _seed(conn)
        first = semantic.sync_index(conn, embedder=FixtureEmbedder())
        second = semantic.sync_index(conn, embedder=FixtureEmbedder())
        conn.execute("DELETE FROM entries WHERE id=?", (ids["sqlite"],))
        third = semantic.sync_index(conn, embedder=FixtureEmbedder())
        remaining = {
            row["entry_id"] for row in conn.execute("SELECT entry_id FROM memory_embeddings")
        }

    assert first.indexed == 2 and first.reused == 0
    assert second.indexed == 0 and second.reused == 2
    assert third.removed == 1
    assert remaining == {ids["typora"]}


def test_event_entries_are_excluded_unless_enabled(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="event-2026-08-23.md",
            description="events",
            tags=["event", "daily"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name="event-2026-08-23.md",
            content="Used Cursor for editing.",
            tags=["session"],
        )
        excluded = semantic.sync_index(conn, embedder=FixtureEmbedder())
        included = semantic.sync_index(
            conn,
            embedder=FixtureEmbedder(),
            include_events=True,
        )
        indexed = conn.execute(
            "SELECT 1 FROM memory_embeddings WHERE entry_id=?", (entry_id,)
        ).fetchone()

    assert excluded.indexed == 0
    assert included.indexed == 1
    assert indexed is not None


def test_configured_hybrid_search_fails_explicitly_when_disabled(ac_root: Path) -> None:
    with fts.cursor() as conn, pytest.raises(
        semantic.SemanticIndexUnavailable,
        match="disabled",
    ):
        semantic.configured_hybrid_search(
            conn,
            search_config=SearchConfig(),
            query="anything",
        )


def test_mcp_and_classifier_use_hybrid_search_when_enabled(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config_mod.Config()
    cfg.search.semantic_enabled = True
    monkeypatch.setattr(
        semantic,
        "configured_embedder",
        lambda search_config: FixtureEmbedder(),
    )
    with fts.cursor() as conn:
        ids = _seed(conn)
        mcp = mcp_server._search(conn, cfg=cfg, query="writing application")
        classifier = writer_tools.tool_search_memory(
            conn,
            cfg,
            query="writing application",
        )

    assert mcp["retrieval_mode"] == "hybrid_rrf"
    assert mcp["results"][0]["id"] == ids["typora"]
    assert classifier["retrieval_mode"] == "hybrid_rrf"
    assert classifier["results"][0]["id"] == ids["typora"]


def test_product_search_reports_enabled_semantic_backend_failure(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config_mod.Config()
    cfg.search.semantic_enabled = True

    def unavailable(search_config):
        raise semantic.SemanticIndexUnavailable("local embedding model unavailable")

    monkeypatch.setattr(semantic, "configured_embedder", unavailable)
    with fts.cursor() as conn:
        mcp = mcp_server._search(conn, cfg=cfg, query="anything")
        classifier = writer_tools.tool_search_memory(conn, cfg, query="anything")

    assert mcp["retrieval_mode"] == "hybrid_unavailable"
    assert "local embedding model unavailable" in mcp["error"]
    assert mcp["results"] == []
    assert classifier["retrieval_mode"] == "hybrid_unavailable"
    assert "local embedding model unavailable" in classifier["error"]
    assert classifier["results"] == []
