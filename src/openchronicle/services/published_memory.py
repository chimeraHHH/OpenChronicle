"""Direct user corrections for canonical published memory."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime

from ..config import Config
from ..memory_candidates import store as candidate_store
from ..store import entries as entries_store
from ..store import files as files_store
from ..store.facts import temporal_state
from .context import ContextService
from .current_facts import CurrentFact, list_current_facts

_REVISION_RE = re.compile(r"[0-9a-f]{64}")


class PublishedMemoryConflict(RuntimeError):
    """The canonical fact changed after the user opened the edit form."""


def correct_current_fact(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    path: str,
    entry_id: str,
    expected_revision: str,
    content: str,
    tags: list[str],
    as_of: datetime | None = None,
) -> CurrentFact:
    """Supersede one visible current fact with a user-authored correction.

    This is a local deterministic edit path, not a model proposal. The old
    canonical entry remains in Markdown as provenance-linked history.
    """
    clean_path = _normalize_path(path)
    clean_entry_id = entry_id.strip()
    if not re.fullmatch(r"[a-zA-Z0-9-]+", clean_entry_id):
        raise ValueError("invalid published memory entry id")
    clean_revision = expected_revision.strip().lower()
    if not _REVISION_RE.fullmatch(clean_revision):
        raise ValueError("invalid published memory revision")
    clean_content = _normalize_content(content)
    clean_tags = _normalize_tags(tags)

    target_path = files_store.memory_path(clean_path)
    if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=clean_path):
        raise ValueError("published memory file is pending deletion")
    parsed = files_store.read_file(target_path)
    if parsed.status != "active":
        raise ValueError("published memory file is not active")
    target = next((entry for entry in parsed.entries if entry.id == clean_entry_id), None)
    if target is None:
        raise ValueError("published memory entry was not found")
    if not target.provenance_valid or not target.origin_valid:
        raise ValueError("published memory entry provenance is invalid")
    if candidate_store.is_tombstoned(
        conn,
        kind="memory_entry",
        artifact_id=target.id,
        path=clean_path,
    ):
        raise ValueError("published memory entry is pending deletion")
    if not ContextService(conn, cfg).memory_entry_allowed(path=clean_path, entry=target):
        raise ValueError("published memory entry is not allowed by current policy")
    if target.fact_metadata is not None and temporal_state(
        target.fact_metadata,
        as_of=as_of or datetime.now().astimezone(),
    ) != "current":
        raise ValueError("published memory entry is not currently valid")

    actual_revision = entries_store.memory_fact_revision(path=clean_path, entry=target)
    if actual_revision != clean_revision:
        raise PublishedMemoryConflict("published memory entry revision changed")
    current_content = _normalize_content(entries_store.entry_index_content(target))
    current_tags = [tag for tag in target.tags if not tag.startswith("superseded-by:")]
    if clean_content == current_content and clean_tags == current_tags:
        raise ValueError("published memory correction does not change content or tags")

    new_entry_id = _correction_entry_id(
        path=clean_path,
        entry_id=clean_entry_id,
        expected_revision=clean_revision,
        content=clean_content,
        tags=clean_tags,
    )
    if target.superseded_by and target.superseded_by != new_entry_id:
        raise PublishedMemoryConflict(
            f"published memory entry is already superseded by {target.superseded_by}"
        )
    try:
        replacement_id = entries_store.supersede_entry(
            conn,
            name=clean_path,
            old_entry_id=clean_entry_id,
            new_content=clean_content,
            reason="direct user correction",
            tags=clean_tags,
            new_entry_id=new_entry_id,
            additional_evidence_refs=[],
            fact_metadata=target.fact_metadata,
            expected_old_revision=clean_revision,
        )
    except ValueError as exc:
        if "revision changed" in str(exc) or "already superseded" in str(exc):
            raise PublishedMemoryConflict(str(exc)) from exc
        raise
    replacement = next(
        (
            fact
            for fact in list_current_facts(conn, cfg, as_of=as_of, limit=10_000)
            if fact.path == clean_path and fact.id == replacement_id
        ),
        None,
    )
    if replacement is None:
        raise RuntimeError("corrected published memory is not visible under current policy")
    return replacement


def _normalize_path(path: str) -> str:
    clean = path.strip()
    if not clean.endswith(".md"):
        raise ValueError("published memory path must end with .md")
    if clean.startswith("event-"):
        raise ValueError("activity event entries are not published memory facts")
    files_store.validate_prefix(clean)
    files_store.memory_path(clean)
    return clean


def _normalize_content(content: str) -> str:
    clean = "\n".join(line.rstrip() for line in content.strip().splitlines()).strip()
    if not clean:
        raise ValueError("published memory content is required")
    if len(clean) > 20_000:
        raise ValueError("published memory content exceeds 20,000 characters")
    files_store.validate_entry_body(clean)
    return clean


def _normalize_tags(tags: list[str]) -> list[str]:
    if len(tags) > 100:
        raise ValueError("published memory has too many tags")
    result: list[str] = []
    for raw in tags:
        tag = raw.strip().removeprefix("#").casefold()
        if (
            not tag
            or len(tag) > 100
            or any(char.isspace() for char in tag)
            or tag.startswith("superseded-by:")
            or tag.startswith(files_store.ENTRY_ORIGIN_TAG_PREFIX)
        ):
            raise ValueError(f"invalid published memory tag: {raw!r}")
        if tag not in result:
            result.append(tag)
    return result


def _correction_entry_id(
    *,
    path: str,
    entry_id: str,
    expected_revision: str,
    content: str,
    tags: list[str],
) -> str:
    payload = json.dumps(
        {
            "v": "manual-memory-edit-v1",
            "path": path,
            "entry_id": entry_id,
            "expected_revision": expected_revision,
            "content": content,
            "tags": tags,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "me-" + hashlib.sha256(payload).hexdigest()[:24]
