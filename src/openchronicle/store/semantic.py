"""Rebuildable local embedding projection and BM25/vector RRF retrieval."""

from __future__ import annotations

import fnmatch
import hashlib
import math
import sqlite3
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Protocol

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_embeddings (
    entry_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    model_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    indexed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model
ON memory_embeddings(model_id);
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_path
ON memory_embeddings(path);
"""


class SemanticIndexUnavailable(RuntimeError):
    """The configured local embedding backend is not installed or usable."""


class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class IndexSyncResult:
    model_id: str
    indexed: int
    reused: int
    removed: int


@dataclass(frozen=True, slots=True)
class HybridEntryHit:
    id: str
    path: str
    prefix: str
    timestamp: str
    tags: str
    content: str
    superseded: int
    rank: float
    score: float
    bm25_rank: int | None
    vector_rank: int | None
    vector_similarity: float | None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


class FastEmbedder:
    """Lazy FastEmbed adapter; model weights and inference stay local."""

    def __init__(self, model_name: str, *, cache_dir: str = "") -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise SemanticIndexUnavailable(
                "FastEmbed is not installed; install openchronicle[semantic-memory]."
            ) from None
        kwargs: dict[str, Any] = {"model_name": model_name}
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        try:
            self._model = TextEmbedding(**kwargs)
        except Exception:
            raise SemanticIndexUnavailable("FastEmbed model could not be loaded.") from None
        self._model_name = model_name

    @property
    def model_id(self) -> str:
        return f"fastembed:{self._model_name}"

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [_coerce_vector(item) for item in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        query_embed = getattr(self._model, "query_embed", None)
        values = query_embed(text) if callable(query_embed) else self._model.embed([text])
        try:
            return _coerce_vector(next(iter(values)))
        except StopIteration:
            raise SemanticIndexUnavailable("FastEmbed returned no query vector.") from None


@lru_cache(maxsize=4)
def configured_fastembed(model_name: str, cache_dir: str = "") -> FastEmbedder:
    return FastEmbedder(model_name, cache_dir=cache_dir)


def configured_embedder(search_config: Any) -> Embedder:
    if not bool(getattr(search_config, "semantic_enabled", False)):
        raise SemanticIndexUnavailable("Semantic memory search is disabled.")
    backend = str(getattr(search_config, "embedding_backend", "fastembed")).strip().lower()
    if backend != "fastembed":
        backend_label = backend or "(empty)"
        raise SemanticIndexUnavailable(f"Unsupported embedding backend: {backend_label}.")
    model = str(getattr(search_config, "embedding_model", "")).strip()
    if not model:
        raise SemanticIndexUnavailable("Semantic embedding_model is empty.")
    cache_dir = str(getattr(search_config, "embedding_cache_dir", "") or "").strip()
    return configured_fastembed(model, cache_dir)


def configured_hybrid_search(
    conn: sqlite3.Connection,
    *,
    search_config: Any,
    query: str,
    path_patterns: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    top_k: int = 5,
    include_superseded: bool = False,
) -> list[HybridEntryHit]:
    embedder = configured_embedder(search_config)
    return hybrid_search(
        conn,
        query=query,
        embedder=embedder,
        path_patterns=path_patterns,
        since=since,
        until=until,
        top_k=top_k,
        candidate_k=max(top_k, int(getattr(search_config, "hybrid_candidate_k", 20))),
        rrf_k=int(getattr(search_config, "hybrid_rrf_k", 60)),
        min_similarity=float(getattr(search_config, "semantic_min_similarity", 0.30)),
        include_superseded=include_superseded,
        include_events=bool(getattr(search_config, "semantic_include_events", False)),
    )


def sync_index(
    conn: sqlite3.Connection,
    *,
    embedder: Embedder,
    include_events: bool = False,
    batch_size: int = 64,
) -> IndexSyncResult:
    """Make the derived vector projection match the canonical FTS projection."""
    ensure_schema(conn)
    if type(batch_size) is not int or not 1 <= batch_size <= 512:
        raise ValueError("semantic batch_size must be in [1, 512]")
    rows = conn.execute(
        "SELECT id, path, tags, content FROM entries ORDER BY rowid"
    ).fetchall()
    eligible = [row for row in rows if include_events or not str(row["path"]).startswith("event-")]
    eligible_ids = {str(row["id"]) for row in eligible}
    existing = {
        str(row["entry_id"]): row
        for row in conn.execute("SELECT * FROM memory_embeddings").fetchall()
    }
    stale_ids = set(existing) - eligible_ids
    if stale_ids:
        conn.executemany(
            "DELETE FROM memory_embeddings WHERE entry_id=?",
            [(entry_id,) for entry_id in sorted(stale_ids)],
        )

    pending: list[tuple[sqlite3.Row, str, str]] = []
    reused = 0
    for row in eligible:
        text = _embedding_text(row["path"], row["tags"], row["content"])
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        current = existing.get(str(row["id"]))
        if (
            current is not None
            and current["content_hash"] == digest
            and current["model_id"] == embedder.model_id
        ):
            reused += 1
            continue
        pending.append((row, text, digest))

    indexed = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        vectors = embedder.embed_documents([text for _, text, _ in batch])
        if len(vectors) != len(batch):
            raise SemanticIndexUnavailable("Embedding backend returned the wrong batch size.")
        indexed_at = datetime.now(UTC).isoformat(timespec="microseconds")
        values = []
        for (row, _, digest), vector in zip(batch, vectors, strict=True):
            normalized = _normalize(vector)
            values.append(
                (
                    str(row["id"]),
                    str(row["path"]),
                    digest,
                    embedder.model_id,
                    len(normalized),
                    _pack_vector(normalized),
                    indexed_at,
                )
            )
        conn.executemany(
            """
            INSERT INTO memory_embeddings(
                entry_id, path, content_hash, model_id, dimensions, vector, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_id) DO UPDATE SET
                path=excluded.path,
                content_hash=excluded.content_hash,
                model_id=excluded.model_id,
                dimensions=excluded.dimensions,
                vector=excluded.vector,
                indexed_at=excluded.indexed_at
            """,
            values,
        )
        indexed += len(values)
    return IndexSyncResult(
        model_id=embedder.model_id,
        indexed=indexed,
        reused=reused,
        removed=len(stale_ids),
    )


def hybrid_search(
    conn: sqlite3.Connection,
    *,
    query: str,
    embedder: Embedder,
    path_patterns: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    top_k: int = 5,
    candidate_k: int = 20,
    rrf_k: int = 60,
    min_similarity: float = 0.30,
    include_superseded: bool = False,
    include_events: bool = False,
) -> list[HybridEntryHit]:
    """Fuse production BM25 and local cosine ranks with reciprocal-rank fusion."""
    from . import fts

    if not query.strip() or type(top_k) is not int or not 1 <= top_k <= 100:
        return []
    if type(candidate_k) is not int or not top_k <= candidate_k <= 500:
        raise ValueError("hybrid candidate_k must be between top_k and 500")
    if type(rrf_k) is not int or not 1 <= rrf_k <= 10_000:
        raise ValueError("hybrid rrf_k must be in [1, 10000]")
    if (
        isinstance(min_similarity, bool)
        or not isinstance(min_similarity, (int, float))
        or not math.isfinite(min_similarity)
        or not -1 <= min_similarity <= 1
    ):
        raise ValueError("semantic min_similarity must be finite and in [-1, 1]")
    sync_index(conn, embedder=embedder, include_events=include_events)
    bm25 = fts.search(
        conn,
        query=query,
        path_patterns=path_patterns,
        since=since,
        until=until,
        top_k=candidate_k,
        include_superseded=include_superseded,
    )
    query_vector = _normalize(embedder.embed_query(query))
    vector_rows = _vector_candidates(
        conn,
        query_vector=query_vector,
        model_id=embedder.model_id,
        path_patterns=path_patterns,
        since=since,
        until=until,
        include_superseded=include_superseded,
        include_events=include_events,
        min_similarity=float(min_similarity),
        limit=candidate_k,
    )

    records: dict[str, dict[str, Any]] = {}
    for rank, hit in enumerate(bm25, start=1):
        records[hit.id] = {
            "hit": hit,
            "score": 1 / (rrf_k + rank),
            "bm25_rank": rank,
            "vector_rank": None,
            "vector_similarity": None,
        }
    lexical_paths = {hit.path for hit in bm25}
    for rank, (hit, similarity) in enumerate(vector_rows, start=1):
        # An exact lexical hit is a strong scope anchor (project/person/tool
        # name). In that case vectors may expand within the same Markdown
        # scope, but cannot introduce a similarly worded fact from another
        # entity. With no lexical hit, full semantic recall remains available.
        if hit.id not in records and lexical_paths and hit.path not in lexical_paths:
            continue
        record = records.setdefault(
            hit.id,
            {
                "hit": hit,
                "score": 0.0,
                "bm25_rank": None,
                "vector_rank": None,
                "vector_similarity": None,
            },
        )
        record["score"] += 1 / (rrf_k + rank)
        record["vector_rank"] = rank
        record["vector_similarity"] = similarity

    ordered = sorted(records.values(), key=lambda item: (-item["score"], item["hit"].id))
    return [
        HybridEntryHit(
            id=item["hit"].id,
            path=item["hit"].path,
            prefix=item["hit"].prefix,
            timestamp=item["hit"].timestamp,
            tags=item["hit"].tags,
            content=item["hit"].content,
            superseded=item["hit"].superseded,
            # Preserve the common recall-hit contract: lower ranks sort first.
            # The positive RRF score remains available for diagnostics.
            rank=-item["score"],
            score=item["score"],
            bm25_rank=item["bm25_rank"],
            vector_rank=item["vector_rank"],
            vector_similarity=item["vector_similarity"],
        )
        for item in ordered[:top_k]
    ]


def _vector_candidates(
    conn: sqlite3.Connection,
    *,
    query_vector: list[float],
    model_id: str,
    path_patterns: list[str] | None,
    since: str | None,
    until: str | None,
    include_superseded: bool,
    include_events: bool,
    min_similarity: float,
    limit: int,
) -> list[tuple[Any, float]]:
    from .fts import EntryHit

    rows = conn.execute(
        """
        SELECT e.id, e.path, e.prefix, e.timestamp, e.tags, e.content,
               e.superseded, m.dimensions, m.vector
          FROM memory_embeddings AS m
          JOIN entries AS e ON e.id=m.entry_id
         WHERE m.model_id=?
        """,
        (model_id,),
    ).fetchall()
    candidates: list[tuple[EntryHit, float]] = []
    for row in rows:
        path = str(row["path"])
        if not include_events and path.startswith("event-"):
            continue
        if path_patterns and not any(fnmatch.fnmatchcase(path, pattern) for pattern in path_patterns):
            continue
        timestamp = str(row["timestamp"])
        if since is not None and timestamp < since:
            continue
        if until is not None and timestamp > until:
            continue
        if not include_superseded and int(row["superseded"]):
            continue
        dimensions = int(row["dimensions"])
        if dimensions != len(query_vector):
            continue
        vector = _unpack_vector(row["vector"], dimensions)
        similarity = sum(left * right for left, right in zip(query_vector, vector, strict=True))
        if similarity < min_similarity:
            continue
        candidates.append(
            (
                EntryHit(
                    id=str(row["id"]),
                    path=path,
                    prefix=str(row["prefix"]),
                    timestamp=timestamp,
                    tags=str(row["tags"]),
                    content=str(row["content"]),
                    superseded=int(row["superseded"]),
                    rank=0.0,
                ),
                similarity,
            )
        )
    candidates.sort(key=lambda item: (-item[1], item[0].id))
    return candidates[:limit]


def _embedding_text(path: object, tags: object, content: object) -> str:
    return f"{path}\n{tags}\n{content}".strip()


def _coerce_vector(value: Any) -> list[float]:
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        raise SemanticIndexUnavailable("Embedding backend returned an invalid vector.") from None


def _normalize(vector: Sequence[float]) -> list[float]:
    values = _coerce_vector(vector)
    if not values or any(not math.isfinite(value) for value in values):
        raise SemanticIndexUnavailable("Embedding backend returned a non-finite vector.")
    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude <= 0:
        raise SemanticIndexUnavailable("Embedding backend returned a zero vector.")
    return [value / magnitude for value in values]


def _pack_vector(vector: Sequence[float]) -> bytes:
    values = array("f", vector)
    if values.itemsize != 4:
        raise SemanticIndexUnavailable("This platform lacks float32 array storage.")
    return values.tobytes()


def _unpack_vector(blob: object, dimensions: int) -> list[float]:
    if not isinstance(blob, bytes):
        raise SemanticIndexUnavailable("Stored embedding vector is invalid.")
    values = array("f")
    values.frombytes(blob)
    if values.itemsize != 4 or len(values) != dimensions:
        raise SemanticIndexUnavailable("Stored embedding vector dimensions are invalid.")
    return list(values)
