"""Canonical current-fact view derived from local Markdown memory."""

from __future__ import annotations

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
class CurrentFact:
    id: str
    path: str
    content: str
    tags: tuple[str, ...]
    origin: str
    recorded_at: str
    source_count: int
    subject_key: str
    assertion_kind: str
    valid_from: str
    valid_to: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "path": self.path,
            "content": self.content,
            "tags": list(self.tags),
            "origin": self.origin,
            "recorded_at": self.recorded_at,
            "source_count": self.source_count,
            "subject_key": self.subject_key,
            "assertion_kind": self.assertion_kind,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "state": "current",
        }


def list_current_facts(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    as_of: datetime | None = None,
    limit: int = 200,
) -> list[CurrentFact]:
    """Return authorized, non-superseded facts valid at ``as_of``.

    Markdown remains authoritative. SQLite only supplies the bounded file list,
    so a stale or forged entry row cannot manufacture current user context.
    Legacy entries remain visible with empty semantic fields.
    """
    if limit < 0:
        raise ValueError("current fact limit must be non-negative")
    if limit == 0:
        return []
    instant = as_of or datetime.now().astimezone()
    context = ContextService(conn, cfg)
    result: list[CurrentFact] = []
    for file_row in fts.list_files(conn, include_dormant=False, include_archived=False):
        path = str(file_row.path)
        if path.startswith("event-") or candidate_store.is_tombstoned(
            conn, kind="memory_file", artifact_id=path
        ):
            continue
        try:
            parsed = files_store.read_file(files_store.memory_path(path))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            continue
        if parsed.status != "active" or not context.memory_file_metadata_allowed(parsed):
            continue
        for entry in parsed.entries:
            if (
                candidate_store.is_tombstoned(
                    conn,
                    kind="memory_entry",
                    artifact_id=entry.id,
                    path=parsed.path.name,
                )
                or entries_store.entry_index_superseded(entry)
                or not context.memory_entry_allowed(path=parsed.path.name, entry=entry)
            ):
                continue
            metadata = entry.fact_metadata
            if metadata is not None and temporal_state(metadata, as_of=instant) != "current":
                continue
            result.append(
                CurrentFact(
                    id=entry.id,
                    path=parsed.path.name,
                    content=entry.body,
                    tags=tuple(entry.tags),
                    origin=entry.origin,
                    recorded_at=entry.timestamp,
                    source_count=len(entry.evidence_refs),
                    subject_key=metadata.subject_key if metadata else "",
                    assertion_kind=metadata.assertion_kind if metadata else "",
                    valid_from=metadata.valid_from if metadata else "",
                    valid_to=metadata.valid_to if metadata else "",
                )
            )
    result.sort(key=lambda fact: (fact.recorded_at, fact.path, fact.id), reverse=True)
    return result[:limit] if limit else []
