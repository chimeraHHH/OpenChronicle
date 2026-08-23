from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openchronicle import cli
from openchronicle import config as config_mod
from openchronicle.services.memory_recall_explain import explain_memory_recall
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts, semantic


class FixtureEmbedder:
    model_id = "fixture:recall-explain-v1"

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        if "typora" in lowered or "writing application" in lowered:
            return [1.0, 0.0]
        return [0.0, 1.0]


def _seed(conn) -> str:
    entries_store.create_file(
        conn,
        name="user-preferences.md",
        description="Reviewed preferences",
        tags=["preference"],
    )
    return entries_store.append_entry(
        conn,
        name="user-preferences.md",
        content="User prefers Typora for long-form Markdown editing.",
        tags=["editor"],
        origin=files_store.MANUAL_ENTRY_ORIGIN,
    )


def test_bm25_explain_is_content_free_and_does_not_echo_query(ac_root: Path) -> None:
    cfg = config_mod.Config()
    raw_query = "Typora Markdown"
    with fts.cursor() as conn:
        entry_id = _seed(conn)
        report = explain_memory_recall(conn, cfg, query=raw_query)

    assert report["retrieval_mode"] == "bm25"
    assert report["results"] == [
        {
            "position": 1,
            "id": entry_id,
            "path": "user-preferences.md",
            "timestamp": report["results"][0]["timestamp"],
            "bm25_rank": 1,
            "bm25_score": report["results"][0]["bm25_score"],
            "vector_rank": None,
            "vector_similarity": None,
            "rrf_score": None,
        }
    ]
    serialized = json.dumps(report, ensure_ascii=False)
    assert raw_query not in serialized
    assert "User prefers Typora" not in serialized
    assert report["query"]["character_count"] == len(raw_query)


def test_hybrid_explain_preserves_each_fusion_arm(
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
        entry_id = _seed(conn)
        report = explain_memory_recall(conn, cfg, query="Typora Markdown")
        vector_only = explain_memory_recall(conn, cfg, query="writing application")

    assert report["retrieval_mode"] == "hybrid_rrf"
    assert report["results"][0]["id"] == entry_id
    assert report["results"][0]["bm25_rank"] == 1
    assert report["results"][0]["vector_rank"] == 1
    assert report["results"][0]["vector_similarity"] == pytest.approx(1.0)
    assert report["results"][0]["rrf_score"] == pytest.approx(2 / 61)
    assert vector_only["results"][0]["id"] == entry_id
    assert vector_only["results"][0]["bm25_rank"] is None
    assert vector_only["results"][0]["vector_rank"] == 1


def test_hybrid_explain_reports_unavailable_without_bm25_fallback(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config_mod.Config()
    cfg.search.semantic_enabled = True

    def unavailable(search_config):
        raise semantic.SemanticIndexUnavailable("fixture model unavailable")

    monkeypatch.setattr(semantic, "configured_embedder", unavailable)
    with fts.cursor() as conn:
        _seed(conn)
        report = explain_memory_recall(conn, cfg, query="Typora")

    assert report["retrieval_mode"] == "hybrid_unavailable"
    assert report["results"] == []
    assert report["candidate_count"] == 0
    assert "fixture model unavailable" in report["error"]


def test_cli_explain_recall_emits_deterministic_json(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config_mod.Config()
    monkeypatch.setattr(cli, "_init", lambda: cfg)
    with fts.cursor() as conn:
        entry_id = _seed(conn)

    result = CliRunner().invoke(
        cli.app,
        ["memory", "explain-recall", "Typora Markdown", "--json"],
    )

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["results"][0]["id"] == entry_id
    assert "Typora Markdown" not in result.stdout
