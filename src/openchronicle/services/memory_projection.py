"""Canonical projection of SQLite memory-entry recall hits."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..config import Config
from ..memory_candidates import store as candidate_store
from ..store import entries as entries_store
from ..store import files as files_store
from ..store import fts
from ..store.facts import temporal_state
from .context import ContextService


@dataclass(frozen=True, slots=True)
class CanonicalEntryHit:
    """An FTS-ranked hit whose public fields come from current Markdown."""

    id: str
    path: str
    timestamp: str
    tags: tuple[str, ...]
    content: str
    superseded_by: str | None
    rank: float


def canonical_entry_hits_locked(
    conn: sqlite3.Connection,
    cfg: Config,
    hits: list[fts.EntryHit],
) -> list[CanonicalEntryHit]:
    """Authorize recall hits and replace their metadata with Markdown values.

    The caller must hold :func:`files_store.store_write_lock` across this
    function.  Every SQLite entry column is only a rebuildable recall
    projection.  An exact disagreement with the current Markdown projection,
    a missing/tombstoned entry, or a current-policy rejection drops the row.
    """
    context = ContextService(conn, cfg)
    parsed_by_path: dict[str, files_store.ParsedFile | None] = {}
    visible: list[CanonicalEntryHit] = []
    emitted: set[tuple[str, str]] = set()

    for hit in hits:
        if not _hit_has_safe_types(hit):
            continue
        key = (hit.path, hit.id)
        if key in emitted:
            continue
        if candidate_store.is_tombstoned(
            conn, kind="memory_file", artifact_id=hit.path
        ) or candidate_store.is_tombstoned(
            conn,
            kind="memory_entry",
            artifact_id=hit.id,
            path=hit.path,
        ):
            continue
        if hit.path not in parsed_by_path:
            try:
                parsed_by_path[hit.path] = files_store.read_file(
                    files_store.memory_path(hit.path)
                )
            except (FileNotFoundError, OSError, TypeError, ValueError):
                parsed_by_path[hit.path] = None
        parsed = parsed_by_path[hit.path]
        if parsed is None:
            continue
        entry = next((item for item in parsed.entries if item.id == hit.id), None)
        if entry is None or not _projection_matches(hit, entry):
            continue
        if entry.fact_metadata is not None and temporal_state(
            entry.fact_metadata,
            as_of=datetime.now().astimezone(),
        ) != "current":
            continue
        if not context.memory_entry_allowed(path=hit.path, entry=entry):
            continue
        visible.append(
            CanonicalEntryHit(
                id=entry.id,
                path=parsed.path.name,
                timestamp=entry.timestamp,
                tags=tuple(entry.tags),
                content=entry.body,
                superseded_by=entry.superseded_by,
                rank=float(hit.rank),
            )
        )
        emitted.add(key)
    return visible


def _hit_has_safe_types(hit: fts.EntryHit) -> bool:
    return bool(
        isinstance(hit.id, str)
        and isinstance(hit.path, str)
        and isinstance(hit.prefix, str)
        and isinstance(hit.timestamp, str)
        and isinstance(hit.tags, str)
        and isinstance(hit.content, str)
        and type(hit.superseded) is int
        and hit.superseded in (0, 1)
        and isinstance(hit.rank, (int, float))
        and not isinstance(hit.rank, bool)
        and math.isfinite(float(hit.rank))
    )


def _projection_matches(
    hit: fts.EntryHit,
    entry: files_store.ParsedEntry,
) -> bool:
    try:
        prefix = files_store.validate_prefix(hit.path)
    except (TypeError, ValueError):
        return False
    return bool(
        hit.id == entry.id
        and hit.path
        and hit.prefix == prefix
        and hit.timestamp == entry.timestamp
        and hit.tags == " ".join(entry.tags)
        and hit.content == entries_store.entry_index_content(entry)
        and hit.superseded == entries_store.entry_index_superseded(entry)
    )
