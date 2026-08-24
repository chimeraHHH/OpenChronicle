"""LongMemEval-V2 adapter for OpenChronicle's local hybrid retrieval.

The benchmark backend is deliberately separate from the user's canonical
Markdown store. It converts public benchmark trajectories into an isolated,
disposable FTS/vector projection and exercises the production BM25/vector RRF
implementation. Query code receives only the question text and optional image
path exposed by the upstream blind-memory interface; images are not consumed.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..store import fts, semantic

UPSTREAM_REPOSITORY = "https://github.com/xiaowu0162/LongMemEval-V2"
UPSTREAM_COMMIT = "2cc8c540bdb87fe6761629b585e727e1c4704520"
DATASET_REPOSITORY = "xiaowu0162/longmemeval-v2"
DATASET_REVISION = "f152293e235517d504809563c833d7190b8c713b"

_STATE_OBJECT_LINE = re.compile(r"^\s*\[([A-Za-z0-9_-]+)\]\s*(.+)$")
_ACTION_OBJECT_ID = re.compile(r"['\"]([A-Za-z]*\d+[A-Za-z0-9_-]*)['\"]")
_SAVED_DB = "openchronicle-longmemeval-v2.db"
_SAVED_MANIFEST = "openchronicle-longmemeval-v2.json"


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_cache_dir: str = ""
    top_k: int = 6
    candidate_k: int = 20
    rrf_k: int = 60
    min_similarity: float = 0.30
    slice_radius: int = 1
    max_state_chars: int = 30_000

    @classmethod
    def from_mapping(cls, value: dict[str, object]) -> AdapterConfig:
        defaults = cls()
        allowed = {
            "embedding_model",
            "embedding_cache_dir",
            "top_k",
            "candidate_k",
            "rrf_k",
            "min_similarity",
            "slice_radius",
            "max_state_chars",
        }
        unexpected = sorted(set(value) - allowed)
        if unexpected:
            raise ValueError(f"unexpected OpenChronicle memory parameters: {unexpected}")
        config = cls(
            embedding_model=_text(
                value.get("embedding_model", defaults.embedding_model),
                "embedding_model",
            ),
            embedding_cache_dir=str(value.get("embedding_cache_dir", "") or "").strip(),
            top_k=_bounded_int(value.get("top_k", defaults.top_k), "top_k", 1, 50),
            candidate_k=_bounded_int(
                value.get("candidate_k", defaults.candidate_k),
                "candidate_k",
                1,
                500,
            ),
            rrf_k=_bounded_int(value.get("rrf_k", defaults.rrf_k), "rrf_k", 1, 10_000),
            min_similarity=_bounded_float(
                value.get("min_similarity", defaults.min_similarity),
                "min_similarity",
                -1.0,
                1.0,
            ),
            slice_radius=_bounded_int(
                value.get("slice_radius", defaults.slice_radius),
                "slice_radius",
                0,
                10,
            ),
            max_state_chars=_bounded_int(
                value.get("max_state_chars", defaults.max_state_chars),
                "max_state_chars",
                1_000,
                200_000,
            ),
        )
        if config.candidate_k < config.top_k:
            raise ValueError("candidate_k must be greater than or equal to top_k")
        return config

    def to_dict(self) -> dict[str, object]:
        return {
            "embedding_model": self.embedding_model,
            "embedding_cache_dir": self.embedding_cache_dir,
            "top_k": self.top_k,
            "candidate_k": self.candidate_k,
            "rrf_k": self.rrf_k,
            "min_similarity": self.min_similarity,
            "slice_radius": self.slice_radius,
            "max_state_chars": self.max_state_chars,
        }


class LongMemEvalV2Corpus:
    """Isolated trajectory corpus backed by production FTS/vector retrieval."""

    def __init__(
        self,
        config: AdapterConfig,
        *,
        embedder: semantic.Embedder | None = None,
    ) -> None:
        self.config = config
        self._temporary_root = tempfile.TemporaryDirectory(
            prefix="openchronicle-longmemeval-v2-"
        )
        self._db_path = Path(self._temporary_root.name) / _SAVED_DB
        self._embedder = embedder or semantic.FastEmbedder(
            config.embedding_model,
            cache_dir=config.embedding_cache_dir,
        )
        self._trajectory_ids: set[str] = set()
        self._entry_count = 0
        with fts.cursor(self._db_path):
            pass

    @property
    def model_id(self) -> str:
        return self._embedder.model_id

    @property
    def entry_count(self) -> int:
        return self._entry_count

    @property
    def trajectory_count(self) -> int:
        return len(self._trajectory_ids)

    def insert(self, trajectory: dict[str, object]) -> None:
        trajectory_id = _text(trajectory.get("id"), "trajectory.id")
        if trajectory_id in self._trajectory_ids:
            raise ValueError(f"duplicate trajectory id: {trajectory_id}")
        domain = _text(trajectory.get("domain"), "trajectory.domain")
        environment = str(trajectory.get("environment") or "").strip()
        goal = _text(trajectory.get("goal"), "trajectory.goal")
        outcome = str(trajectory.get("outcome") or "unknown").strip()
        start_url = str(trajectory.get("start_url") or "").strip()
        states = trajectory.get("states")
        if not isinstance(states, list) or not states:
            raise ValueError(f"trajectory {trajectory_id} has no states")
        path = f"topic-lme-{trajectory_id}.md"
        rows: list[tuple[str, str, str, str]] = []
        for ordinal, raw_state in enumerate(states):
            if not isinstance(raw_state, dict):
                continue
            state_index = raw_state.get("state_index", ordinal)
            if isinstance(state_index, bool) or not isinstance(state_index, int):
                state_index = ordinal
            content = _state_document(
                trajectory_id=trajectory_id,
                domain=domain,
                environment=environment,
                goal=goal,
                outcome=outcome,
                start_url=start_url,
                state_index=state_index,
                state=raw_state,
                max_chars=self.config.max_state_chars,
            )
            rows.append(
                (
                    f"lme-{trajectory_id}-{state_index:06d}-{ordinal:06d}",
                    f"{state_index:012d}",
                    f"longmemeval-v2 {domain} {environment}".strip(),
                    content,
                )
            )
        if not rows:
            raise ValueError(f"trajectory {trajectory_id} has no valid states")
        with fts.cursor(self._db_path) as conn:
            for entry_id, timestamp, tags, content in rows:
                fts.insert_entry(
                    conn,
                    id=entry_id,
                    path=path,
                    prefix="topic-",
                    timestamp=timestamp,
                    tags=tags,
                    content=content,
                )
            # Bound first-query memory: index this immutable trajectory now.
            # A later query still verifies hashes across the complete corpus,
            # but unchanged state bodies are streamed and never accumulated.
            semantic.sync_index(
                conn,
                embedder=self._embedder,
                paths=[path],
            )
        self._trajectory_ids.add(trajectory_id)
        self._entry_count += len(rows)

    def query(self, query: str) -> list[dict[str, str]]:
        if not isinstance(query, str) or not query.strip() or self._entry_count == 0:
            return []
        with fts.cursor(self._db_path) as conn:
            hits = semantic.hybrid_search(
                conn,
                query=query,
                embedder=self._embedder,
                top_k=self.config.top_k,
                candidate_k=max(self.config.candidate_k, self.config.top_k),
                rrf_k=self.config.rrf_k,
                min_similarity=self.config.min_similarity,
            )
            contexts = _expand_state_slices(
                conn,
                hits,
                radius=self.config.slice_radius,
            )
        return [{"type": "text", "value": context} for context in contexts]

    def metadata(self) -> dict[str, object]:
        return {
            "backend": "openchronicle_hybrid_rrf",
            "upstream_commit": UPSTREAM_COMMIT,
            "dataset_revision": DATASET_REVISION,
            "embedding_model": self.model_id,
            "trajectory_count": self.trajectory_count,
            "entry_count": self.entry_count,
            "image_memory": False,
        }

    def save(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        with fts.cursor(self._db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(self._db_path, output_dir / _SAVED_DB)
        (output_dir / _SAVED_MANIFEST).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "adapter_config": self.config.to_dict(),
                    **self.metadata(),
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def load(self, input_dir: Path) -> None:
        manifest_path = input_dir / _SAVED_MANIFEST
        database_path = input_dir / _SAVED_DB
        if not manifest_path.is_file() or not database_path.is_file():
            raise ValueError("saved OpenChronicle LongMemEval-V2 memory is incomplete")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1:
            raise ValueError("unsupported OpenChronicle LongMemEval-V2 memory schema")
        if manifest.get("adapter_config") != self.config.to_dict():
            raise ValueError("saved OpenChronicle adapter configuration does not match")
        if manifest.get("embedding_model") != self.model_id:
            raise ValueError("saved OpenChronicle embedding model does not match")
        shutil.copy2(database_path, self._db_path)
        self._trajectory_ids = {
            str(row["path"])[len("topic-lme-") : -len(".md")]
            for row in _database_rows(
                self._db_path,
                "SELECT DISTINCT path FROM entries ORDER BY path",
            )
        }
        self._entry_count = int(
            _database_rows(self._db_path, "SELECT COUNT(*) AS count FROM entries")[0][
                "count"
            ]
        )

    def close(self) -> None:
        self._temporary_root.cleanup()


def register_upstream_backend(upstream_root: Path) -> type[Any]:
    """Register the adapter with one pinned LongMemEval-V2 checkout."""
    import sys

    root = upstream_root.expanduser().resolve()
    if not (root / "memory_modules" / "memory.py").is_file():
        raise ValueError(f"not a LongMemEval-V2 checkout: {root}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from memory_modules.memory import MEMORY_TYPES, Memory, register_memory

    existing = MEMORY_TYPES.get("openchronicle")
    if existing is not None:
        return existing

    @register_memory
    class OpenChronicleMemory(Memory):
        memory_type = "openchronicle"

        def __init__(self, memory_params: dict[str, object]) -> None:
            super().__init__(memory_params)
            self._adapter_config = AdapterConfig.from_mapping(memory_params)
            self._corpus = LongMemEvalV2Corpus(self._adapter_config)

        def insert(self, trajectory: dict[str, object]) -> None:
            self._corpus.insert(trajectory)

        def query(
            self,
            query: str,
            query_image: str | None = None,
        ) -> list[dict[str, str]]:
            # The product's memory model is text-only; keep image questions in
            # the external report as a declared unsupported modality.
            return self._corpus.query(query)

        def post_query_hook(
            self,
            *,
            query: str,
            query_image: str | None,
            memory_context: list[dict[str, str]],
        ) -> dict[str, object]:
            return {
                **self._corpus.metadata(),
                "query_image_present": query_image is not None,
                "returned_context_items": len(memory_context),
            }

        def _save_backend(self, output_dir: Path) -> None:
            self._corpus.save(output_dir)

        def _load_backend(self, input_dir: Path) -> None:
            self._corpus.load(input_dir)

    return OpenChronicleMemory


def default_memory_config() -> dict[str, object]:
    return {
        "memory_type": "openchronicle",
        "memory_params": AdapterConfig().to_dict(),
    }


def _state_document(
    *,
    trajectory_id: str,
    domain: str,
    environment: str,
    goal: str,
    outcome: str,
    start_url: str,
    state_index: int,
    state: dict[str, object],
    max_chars: int,
) -> str:
    url = str(state.get("url") or "").strip()
    thought = str(state.get("thought") or "").strip()
    action = str(state.get("action") or "").strip()
    tree = str(state.get("accessibility_tree") or "").strip()
    interaction = _interaction_lines(action, tree)
    sections = [
        f"Trajectory: {trajectory_id}",
        f"Domain: {domain}",
        f"Environment: {environment}" if environment else "",
        f"Goal: {goal}",
        f"Outcome: {outcome}",
        f"Start URL: {start_url}" if start_url else "",
        f"State: {state_index}",
        f"Current URL: {url}" if url else "",
        f"Action: {action}" if action else "",
        f"Interaction context:\n{interaction}" if interaction else "",
        f"Agent observation: {thought}" if thought else "",
        f"Accessibility tree:\n{tree}" if tree else "",
    ]
    document = "\n".join(section for section in sections if section).strip()
    return document[:max_chars].rstrip()


def _interaction_lines(action: str, tree: str) -> str:
    object_ids = set(_ACTION_OBJECT_ID.findall(action))
    if not object_ids:
        return ""
    matches: list[str] = []
    for line in tree.splitlines():
        match = _STATE_OBJECT_LINE.match(line)
        if match and match.group(1) in object_ids:
            matches.append(line.strip())
    return "\n".join(matches)


def _expand_state_slices(
    conn: sqlite3.Connection,
    hits: Sequence[semantic.HybridEntryHit],
    *,
    radius: int,
) -> list[str]:
    contexts: list[str] = []
    emitted: set[str] = set()
    for hit in hits:
        center = int(hit.timestamp)
        rows = conn.execute(
            """
            SELECT id, path, timestamp, content
              FROM entries
             WHERE path=? AND CAST(timestamp AS INTEGER) BETWEEN ? AND ?
             ORDER BY CAST(timestamp AS INTEGER), id
            """,
            (hit.path, center - radius, center + radius),
        ).fetchall()
        selected = [row for row in rows if str(row["id"]) not in emitted]
        if not selected:
            continue
        emitted.update(str(row["id"]) for row in selected)
        contexts.append(
            "\n\n--- neighboring state ---\n\n".join(
                str(row["content"]) for row in selected
            )
        )
    return contexts


def _database_rows(db_path: Path, sql: str) -> list[sqlite3.Row]:
    with fts.cursor(db_path) as conn:
        return conn.execute(sql).fetchall()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value


def _bounded_float(value: object, label: str, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError(f"{label} must be in [{minimum}, {maximum}]")
    return float(value)
