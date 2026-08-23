"""Read-only diagnostics for durable-memory recall ranking."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from ..config import Config
from ..store import files as files_store
from ..store import fts, semantic
from .memory_projection import canonical_entry_hits_locked

_RECALL_PAGE_SIZE = 100
_MAX_TOP_K = 20


def explain_memory_recall(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    query: str,
    top_k: int = 5,
) -> dict[str, Any]:
    """Explain the existing durable-memory ranker without returning content.

    The query is used transiently and represented in the report only by a
    digest and character count.  This function does not call an LLM or change
    ranking policy.  The local embedding table remains a rebuildable search
    projection and may be synchronized by the existing hybrid search path.
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "query must be a non-empty string", "results": []}
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= _MAX_TOP_K:
        return {"error": f"top_k must be an integer in [1, {_MAX_TOP_K}]", "results": []}

    mode = "hybrid_rrf" if cfg.search.semantic_enabled else "bm25"
    report: dict[str, Any] = {
        "schema_version": 1,
        "retrieval_mode": mode,
        "query": {
            "sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "character_count": len(query),
        },
        "config": _config_payload(cfg, top_k=top_k),
        "candidate_count": 0,
        "returned_count": 0,
        "results": [],
    }
    path_patterns = [
        f"{prefix}*" for prefix in files_store.VALID_PREFIXES if prefix != "event-"
    ]

    with files_store.store_write_lock():
        if cfg.search.semantic_enabled:
            try:
                raw_hits = semantic.configured_hybrid_search(
                    conn,
                    search_config=cfg.search,
                    query=query,
                    path_patterns=path_patterns,
                    top_k=_RECALL_PAGE_SIZE,
                )
            except semantic.SemanticIndexUnavailable as exc:
                report.update(
                    retrieval_mode="hybrid_unavailable",
                    error=str(exc),
                )
                return report

            report["candidate_count"] = len(raw_hits)
            raw_by_key = {(hit.path, hit.id): hit for hit in raw_hits}
            visible = canonical_entry_hits_locked(conn, cfg, raw_hits)[:top_k]
            results = []
            for position, hit in enumerate(visible, start=1):
                raw = raw_by_key[(hit.path, hit.id)]
                results.append(
                    {
                        "position": position,
                        "id": hit.id,
                        "path": hit.path,
                        "timestamp": hit.timestamp,
                        "rrf_score": raw.score,
                        "bm25_rank": raw.bm25_rank,
                        "vector_rank": raw.vector_rank,
                        "vector_similarity": raw.vector_similarity,
                    }
                )
            report["results"] = results
        else:
            report["results"], report["candidate_count"] = _explain_bm25_locked(
                conn,
                cfg,
                query=query,
                path_patterns=path_patterns,
                top_k=top_k,
            )

    report["returned_count"] = len(report["results"])
    return report


def _explain_bm25_locked(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    query: str,
    path_patterns: list[str],
    top_k: int,
) -> tuple[list[dict[str, Any]], int]:
    results: list[dict[str, Any]] = []
    offset = 0
    candidate_count = 0
    while len(results) < top_k:
        raw_hits = fts.search(
            conn,
            query=query,
            path_patterns=path_patterns,
            top_k=_RECALL_PAGE_SIZE,
            offset=offset,
        )
        if not raw_hits:
            break
        raw_positions = {
            (hit.path, hit.id): offset + index
            for index, hit in enumerate(raw_hits, start=1)
        }
        offset += len(raw_hits)
        candidate_count += len(raw_hits)
        for hit in canonical_entry_hits_locked(conn, cfg, raw_hits):
            results.append(
                {
                    "position": len(results) + 1,
                    "id": hit.id,
                    "path": hit.path,
                    "timestamp": hit.timestamp,
                    "bm25_rank": raw_positions[(hit.path, hit.id)],
                    "bm25_score": hit.rank,
                    "vector_rank": None,
                    "vector_similarity": None,
                    "rrf_score": None,
                }
            )
            if len(results) >= top_k:
                break
        if len(raw_hits) < _RECALL_PAGE_SIZE:
            break
    return results, candidate_count


def _config_payload(cfg: Config, *, top_k: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "top_k": top_k,
        "event_entries_included": False,
    }
    if cfg.search.semantic_enabled:
        payload.update(
            embedding_model_id=(
                f"{cfg.search.embedding_backend}:{cfg.search.embedding_model}"
            ),
            candidate_k=max(_RECALL_PAGE_SIZE, cfg.search.hybrid_candidate_k),
            rrf_k=cfg.search.hybrid_rrf_k,
            min_similarity=cfg.search.semantic_min_similarity,
        )
    return payload
