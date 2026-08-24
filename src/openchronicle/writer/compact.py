"""Compact an authorized memory file while preserving identity and provenance.

Workflow: LLM rewrites individual leaf bodies, then exact trust/dependency
checks and a noun-phrase-preservation gate reject unsafe compression.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, replace
from typing import Any

import frontmatter

from ..config import Config
from ..logger import get
from ..memory_candidates import store as candidate_store
from ..privacy.egress import model_egress_lock
from ..prompts import load as load_prompt
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef
from ..services.context import ContextService
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts
from . import llm as llm_mod

logger = get("openchronicle.compact")

_UNIQUE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")
_PRESERVATION_THRESHOLD = 0.95  # must keep ≥95% of unique tokens


@dataclass
class CompactResult:
    path: str
    accepted: bool
    before_tokens: int
    after_tokens: int
    before_unique: int
    after_unique: int
    preservation_ratio: float
    note: str = ""


def _unique_tokens(text: str) -> set[str]:
    return {t.lower() for t in _UNIQUE_TOKEN_RE.findall(text)}


def compact_file(cfg: Config, conn: sqlite3.Connection, *, name: str) -> CompactResult:
    entries_mod.require_autocommit(conn)
    path = files_mod.memory_path(name)
    attempt = _snapshot_and_call_compactor(cfg, conn, path=path, name=name)
    if isinstance(attempt, CompactResult):
        return attempt
    original, before_unique, before_tokens, frozen_source_ids, resp = attempt

    new_text = llm_mod.extract_text(resp).strip()
    # Strip markdown code fences if the model wrapped the output
    new_text = _unwrap_code_fence(new_text)

    if not new_text.startswith("---"):
        return CompactResult(
            name, False, before_tokens, len(new_text) // 4,
            len(before_unique), 0, 0.0, "response missing frontmatter — rejected",
        )

    try:
        compacted = frontmatter.loads(new_text)
        original_post = frontmatter.loads(original)
    except Exception as exc:  # noqa: BLE001
        return CompactResult(
            name, False, before_tokens, len(new_text) // 4,
            len(before_unique), 0, 0.0, f"frontmatter parse error: {exc}",
        )
    compacted_entries = files_mod._parse_entries(compacted.content)
    original_entries = files_mod._parse_entries(original_post.content)
    original_identity = [(entry.id, entry.timestamp) for entry in original_entries]
    compacted_identity = [(entry.id, entry.timestamp) for entry in compacted_entries]
    original_by_id = {entry.id: entry for entry in original_entries}
    if (
        not compacted_entries
        or len({entry.id for entry in compacted_entries}) != len(compacted_entries)
        or compacted_identity != original_identity
        or any(
            not entry.origin_valid
            or not entry.provenance_valid
            or entry.origin != original_by_id[entry.id].origin
            or entry.tags != original_by_id[entry.id].tags
            or entry.provenance_present
            != original_by_id[entry.id].provenance_present
            or entry.evidence_refs != original_by_id[entry.id].evidence_refs
            for entry in compacted_entries
        )
    ):
        return CompactResult(
            name,
            False,
            before_tokens,
            len(new_text) // 4,
            len(before_unique),
            0,
            0.0,
            "response changed canonical entry identity or trust markers",
        )
    changed_frozen = [
        entry.id
        for entry in compacted_entries
        if entry.id in frozen_source_ids
        and entry.body != original_by_id[entry.id].body
    ]
    if changed_frozen:
        return CompactResult(
            name,
            False,
            before_tokens,
            len(new_text) // 4,
            len(before_unique),
            0,
            0.0,
            "response changed an entry body referenced by dependent memory",
        )
    # Trust markers, dependency frames, supersede tags, and canonical headings
    # remain local authority even after exact output validation.
    compacted_entries = [
        replace(
            entry,
            heading_line=original_by_id[entry.id].heading_line,
            tags=list(original_by_id[entry.id].tags),
            superseded_by=original_by_id[entry.id].superseded_by,
            evidence_refs=list(original_by_id[entry.id].evidence_refs),
            provenance_present=original_by_id[entry.id].provenance_present,
            provenance_valid=original_by_id[entry.id].provenance_valid,
            provenance_error=original_by_id[entry.id].provenance_error,
            origin=original_by_id[entry.id].origin,
            origin_valid=original_by_id[entry.id].origin_valid,
        )
        for entry in compacted_entries
    ]
    # Frontmatter is local authority, not model output. Preserve it exactly
    # except for the flag this successful operation is meant to clear, and
    # render only parsed canonical entries so unframed model text is dropped.
    safe_metadata = dict(original_post.metadata)
    safe_metadata["needs_compact"] = False
    safe_metadata["entry_count"] = len(compacted_entries)
    compacted = frontmatter.Post(
        content=files_mod.render_file(fm={}, entries=compacted_entries),
        **safe_metadata,
    )
    new_text = frontmatter.dumps(compacted) + "\n"
    prefix = files_mod.validate_prefix(path.name)

    after_unique = _unique_tokens(new_text)
    preserved = len(before_unique & after_unique)
    ratio = preserved / len(before_unique) if before_unique else 1.0

    if ratio < _PRESERVATION_THRESHOLD:
        logger.warning(
            "compact rejected: %.1f%% preservation (need %.0f%%) — %s",
            ratio * 100, _PRESERVATION_THRESHOLD * 100, name,
        )
        return CompactResult(
            name, False, before_tokens, len(new_text) // 4,
            len(before_unique), len(after_unique), ratio,
            f"rejected: preservation {ratio:.1%} < {_PRESERVATION_THRESHOLD:.0%}",
        )

    # Accept only if the file is still the same one the LLM saw. The egress
    # fence serializes OpenChronicle mutations, while this stale-read check also
    # protects against an external editor changing Markdown during the call.
    with (
        files_mod.review_operation_lock(),
        files_mod.store_write_lock(),
        files_mod.file_lock(path),
    ):
        if candidate_store.is_tombstoned(
            conn, kind="memory_file", artifact_id=path.name
        ):
            return CompactResult(
                name,
                False,
                before_tokens,
                before_tokens,
                len(before_unique),
                len(before_unique),
                1.0,
                "file pending permanent purge",
            )
        try:
            current = path.read_text()
        except FileNotFoundError:
            return CompactResult(
                name, False, before_tokens, before_tokens,
                len(before_unique), len(before_unique), 1.0,
                "file missing before writeback",
            )
        if current != original:
            logger.info("compact skipped: %s changed during LLM rewrite", name)
            return CompactResult(
                name, False, before_tokens, before_tokens,
                len(before_unique), len(before_unique), 1.0,
                "file changed during compact — retry later",
            )

        files_mod.atomic_write_text(path, new_text)

        # Re-ingest this file's entries into FTS while still holding the same
        # file lock so on-disk Markdown and index rows move forward together.
        conn.execute("SAVEPOINT compact_file_fts")
        try:
            fts.delete_entries_for(conn, path.name)
            fts.upsert_file(
                conn,
                fts.FileRow(
                    path=path.name,
                    prefix=prefix,
                    description=str(compacted.metadata.get("description", "")),
                    tags=" ".join(compacted.metadata.get("tags", []) or []),
                    status=str(compacted.metadata.get("status", "active")),
                    entry_count=len(compacted_entries),
                    created=str(compacted.metadata.get("created", "")),
                    updated=str(compacted.metadata.get("updated", "")),
                    needs_compact=0,
                ),
            )
            for e in compacted_entries:
                fts.insert_entry(
                    conn,
                    id=e.id,
                    path=path.name,
                    prefix=prefix,
                    timestamp=e.timestamp,
                    tags=" ".join(e.tags),
                    content=entries_mod.entry_index_content(e),
                    superseded=entries_mod.entry_index_superseded(e),
                )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT compact_file_fts")
            conn.execute("RELEASE SAVEPOINT compact_file_fts")
            raise
        conn.execute("RELEASE SAVEPOINT compact_file_fts")

    logger.info(
        "compact accepted: %s  %d→%d tokens (%.1f%% preservation)",
        name, before_tokens, len(new_text) // 4, ratio * 100,
    )
    return CompactResult(
        name, True, before_tokens, len(new_text) // 4,
        len(before_unique), len(after_unique), ratio,
    )


def _snapshot_and_call_compactor(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    path,
    name: str,
) -> tuple[str, set[str], int, set[str], Any] | CompactResult:
    """Authorize the exact file snapshot and keep it stable through egress."""
    with model_egress_lock():
        with files_mod.store_write_lock(), files_mod.file_lock(path):
            if candidate_store.is_tombstoned(
                conn, kind="memory_file", artifact_id=path.name
            ):
                return CompactResult(
                    name,
                    False,
                    0,
                    0,
                    0,
                    0,
                    0.0,
                    "file pending permanent purge",
                )
            try:
                parsed_original = files_mod.read_file(path)
                original = path.read_text(encoding="utf-8")
            except (FileNotFoundError, OSError):
                return CompactResult(name, False, 0, 0, 0, 0, 0.0, "file missing")

        before_unique = _unique_tokens(original)
        before_tokens = len(original) // 4
        invalid = [entry for entry in parsed_original.entries if not entry.provenance_valid]
        if invalid:
            return CompactResult(
                name,
                False,
                before_tokens,
                before_tokens,
                len(before_unique),
                len(before_unique),
                1.0,
                "invalid provenance frame; refusing compaction",
            )
        context = ContextService(conn, cfg)
        if not parsed_original.entries or any(
            not context.memory_entry_allowed(path=path.name, entry=entry)
            for entry in parsed_original.entries
        ):
            return CompactResult(
                name,
                False,
                before_tokens,
                before_tokens,
                len(before_unique),
                len(before_unique),
                1.0,
                "one or more entries are not currently authorized; require explicit "
                "manual-v1 roots or live provenance",
            )
        if len({entry.id for entry in parsed_original.entries}) != len(
            parsed_original.entries
        ):
            return CompactResult(
                name,
                False,
                before_tokens,
                before_tokens,
                len(before_unique),
                len(before_unique),
                1.0,
                "duplicate canonical entry identity; refusing compaction",
            )
        if not ContextService(conn, cfg).memory_file_metadata_allowed(
            parsed_original
        ):
            return CompactResult(
                name,
                False,
                before_tokens,
                before_tokens,
                len(before_unique),
                len(before_unique),
                1.0,
                "file metadata is not currently authorized",
            )

        frozen_source_ids = {
            entry.id
            for entry in parsed_original.entries
            if provenance_store.direct_dependents(
                conn,
                EvidenceRef(kind="memory_entry", id=entry.id, path=path.name),
            )
        }

        system = load_prompt("compact.md")
        frozen_note = (
            "\nBodies that must remain byte-for-byte unchanged because other "
            "memory entries cite them: "
            + (", ".join(sorted(frozen_source_ids)) or "(none)")
            + "\n"
        )
        user = (
            "Compress this file. Output the full new Markdown including frontmatter.\n"
            + frozen_note
            + "\n"
            "```markdown\n" + original + "\n```"
        )
        try:
            response = llm_mod.call_llm(
                cfg,
                "compact",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("compact llm call failed: %s", type(exc).__name__)
            return CompactResult(
                name,
                False,
                before_tokens,
                before_tokens,
                len(before_unique),
                len(before_unique),
                1.0,
                f"llm error: {type(exc).__name__}",
            )
        return original, before_unique, before_tokens, frozen_source_ids, response


def _unwrap_code_fence(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def run_pending(cfg: Config, conn: sqlite3.Connection) -> list[CompactResult]:
    pending = fts.files_needing_compact(conn)
    results: list[CompactResult] = []
    for name in pending:
        results.append(compact_file(cfg, conn, name=name))
    return results
