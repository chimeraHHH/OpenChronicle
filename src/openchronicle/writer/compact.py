"""Compact an explicitly manual memory file while preserving identity and facts.

Workflow: LLM rewrites the file, then a noun-phrase-preservation check blocks
compressions that drop too many distinct tokens.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

import frontmatter

from ..config import Config
from ..logger import get
from ..memory_candidates import store as candidate_store
from ..privacy.egress import model_egress_lock
from ..prompts import load as load_prompt
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
    original, before_unique, before_tokens, resp = attempt

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
    if (
        not compacted_entries
        or len({entry.id for entry in compacted_entries}) != len(compacted_entries)
        or compacted_identity != original_identity
        or any(
            not entry.origin_valid
            or entry.origin != files_mod.MANUAL_ENTRY_ORIGIN
            or not entry.provenance_valid
            or entry.provenance_present
            or entry.evidence_refs
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
) -> tuple[str, set[str], int, Any] | CompactResult:
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
        invalid = [
            entry
            for entry in parsed_original.entries
            if not entry.provenance_valid
        ]
        projected = conn.execute(
            """
            SELECT 1 FROM provenance_edges
             WHERE subject_kind='memory_entry' AND subject_path=? LIMIT 1
            """,
            (path.name,),
        ).fetchone()
        if invalid or projected or any(
            entry.provenance_present or entry.evidence_refs
            for entry in parsed_original.entries
        ):
            reason = (
                "invalid provenance frame; refusing compaction"
                if invalid
                else "provenance-bearing entries require a provenance-aware compactor"
            )
            return CompactResult(
                name,
                False,
                before_tokens,
                before_tokens,
                len(before_unique),
                len(before_unique),
                1.0,
                reason,
            )
        if not parsed_original.entries or any(
            not entry.origin_valid
            or entry.origin != files_mod.MANUAL_ENTRY_ORIGIN
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
                "only explicit manual-v1 entries may be compacted",
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

        system = load_prompt("compact.md")
        user = (
            "Compress this file. Output the full new Markdown including frontmatter.\n\n"
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
        return original, before_unique, before_tokens, response


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
