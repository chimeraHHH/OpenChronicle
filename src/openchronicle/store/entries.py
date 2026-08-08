"""Entry-level operations: create file, append, supersede. Syncs FTS5 on every write."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter

from ..logger import get
from ..memory_candidates import store as candidate_store
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, content_digest
from . import files as files_mod
from . import fts

logger = get("openchronicle.store")


def require_autocommit(conn: sqlite3.Connection) -> None:
    """Reject callers that would invert the global-lock/SQLite lock order."""
    if conn.in_transaction:
        raise RuntimeError(
            "memory file mutations require an autocommit SQLite connection; "
            "finish the existing transaction before writing Markdown"
        )


def _now_iso_minute() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M")


def make_id(timestamp: str) -> str:
    """YYYYMMDD-HHMM-<6-hex>.

    6 hex chars (24 bits) keeps collision probability <0.1% within a single
    minute even under heavy batched writes.
    """
    compact = timestamp.replace("-", "").replace(":", "").replace("T", "-")[:13]
    salt = hashlib.blake2s(os.urandom(8), digest_size=3).hexdigest()
    return f"{compact}-{salt}"


def _ensure_prefix(path_name: str) -> str:
    return files_mod.validate_prefix(path_name)


def create_file(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str,
    tags: list[str],
    owner_candidate_id: str | None = None,
) -> Path:
    with files_mod.review_operation_lock():
        return _create_file_locked(
            conn,
            name=name,
            description=description,
            tags=tags,
            owner_candidate_id=owner_candidate_id,
        )


def _create_file_locked(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str,
    tags: list[str],
    owner_candidate_id: str | None,
) -> Path:
    require_autocommit(conn)
    if not description.strip():
        raise ValueError("description is required")
    prefix = _ensure_prefix(name)
    path = files_mod.memory_path(name)
    if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=path.name):
        raise RuntimeError(f"{path.name} is pending permanent purge")
    # Lock around the exists-check + write so two concurrent classifiers
    # deciding to create the same file don't both pass the check and have
    # the second clobber the first's freshly written content.
    with files_mod.store_write_lock(), files_mod.file_lock(path):
        if path.exists():
            raise FileExistsError(f"{path.name} already exists")

        fm = files_mod.default_frontmatter(description=description, tags=tags)
        if owner_candidate_id is not None:
            files_mod.mark_candidate_owned(
                fm,
                candidate_id=owner_candidate_id,
                path_name=path.name,
            )
        files_mod.write_file(path, fm, body="")
        fts.upsert_file(
            conn,
            fts.FileRow(
                path=path.name,
                prefix=prefix,
                description=description,
                tags=" ".join(tags),
                status="active",
                entry_count=0,
                created=fm["created"],
                updated=fm["updated"],
                needs_compact=0,
            ),
        )
    logger.info("created file: %s", path.name)
    return path


def append_entry(
    conn: sqlite3.Connection,
    *,
    name: str,
    content: str,
    tags: list[str],
    soft_limit_tokens: int | None = None,
    origin: str = files_mod.AUTOMATION_ENTRY_ORIGIN,
) -> str:
    """Append a new entry, returning its id."""
    with files_mod.review_operation_lock():
        entry_id, _created = _append_entry(
            conn,
            name=name,
            content=content,
            tags=tags,
            soft_limit_tokens=soft_limit_tokens,
            requested_id=None,
            origin=origin,
        )
    return entry_id


def append_entry_once(
    conn: sqlite3.Connection,
    *,
    name: str,
    content: str,
    tags: list[str],
    entry_id: str,
    evidence_refs: list[EvidenceRef] | None = None,
    soft_limit_tokens: int | None = None,
    origin: str = files_mod.AUTOMATION_ENTRY_ORIGIN,
) -> tuple[str, bool]:
    """Append a deterministic entry once and repair a missing FTS row.

    Returns ``(entry_id, created)``. If Markdown reached disk just before a
    crash, a retry reuses the existing entry instead of appending a duplicate.
    """
    if not re.fullmatch(r"[a-zA-Z0-9-]+", entry_id):
        raise ValueError(f"invalid deterministic entry id: {entry_id!r}")
    with files_mod.review_operation_lock():
        _require_live_dependency_sources(conn, evidence_refs or [])
        return _append_entry(
            conn,
            name=name,
            content=content,
            tags=tags,
            soft_limit_tokens=soft_limit_tokens,
            requested_id=entry_id,
            evidence_refs=evidence_refs,
            origin=origin,
        )


def _append_entry(
    conn: sqlite3.Connection,
    *,
    name: str,
    content: str,
    tags: list[str],
    soft_limit_tokens: int | None,
    requested_id: str | None,
    evidence_refs: list[EvidenceRef] | None = None,
    origin: str,
) -> tuple[str, bool]:
    require_autocommit(conn)
    path = files_mod.memory_path(name)
    if not path.exists():
        raise FileNotFoundError(f"{path.name} does not exist; call create_file first")
    if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=path.name):
        raise RuntimeError(f"{path.name} is pending permanent purge")
    prefix = _ensure_prefix(name)

    ts = _now_iso_minute()
    entry_id = requested_id or make_id(ts)
    if candidate_store.is_tombstoned(
        conn, kind="memory_entry", artifact_id=entry_id, path=path.name
    ):
        raise RuntimeError(f"entry {entry_id} is pending permanent purge")
    effective_origin = (
        files_mod.DERIVED_ENTRY_ORIGIN if evidence_refs else origin
    )
    if effective_origin not in {
        files_mod.MANUAL_ENTRY_ORIGIN,
        files_mod.AUTOMATION_ENTRY_ORIGIN,
        files_mod.DERIVED_ENTRY_ORIGIN,
    }:
        raise ValueError("invalid memory entry origin")
    heading = files_mod.render_heading(
        timestamp=ts,
        entry_id=entry_id,
        tags=[*tags, f"{files_mod.ENTRY_ORIGIN_TAG_PREFIX}{effective_origin}"],
    )
    body = content.strip()
    files_mod.validate_entry_body(body)
    if any(not tag or any(char.isspace() for char in tag) for tag in tags):
        raise ValueError("entry tags must be non-empty and contain no whitespace")
    rendered_body = body
    if evidence_refs:
        import json

        payload = {
            "v": 1,
            "sources": [source.to_dict() for source in evidence_refs],
        }
        rendered_body += (
            "\n<!-- oc-provenance: "
            + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + " -->"
        )

    # Lock the read-modify-write so a concurrent classifier appending to
    # the same file can't read the same base, append, and clobber this
    # write — both writes claim "+1 entry" but only one entry survives
    # while the FTS index keeps both, leaving file/index inconsistent.
    with files_mod.store_write_lock(), files_mod.file_lock(path):
        if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=path.name):
            raise RuntimeError(f"{path.name} is pending permanent purge")
        post = frontmatter.load(path)
        if requested_id is not None:
            existing = next(
                (entry for entry in files_mod._parse_entries(post.content) if entry.id == entry_id),
                None,
            )
            if existing is not None:
                if not existing.provenance_valid:
                    raise ValueError(
                        f"deterministic entry {entry_id} has an invalid provenance frame"
                    )
                if existing.body.strip() != body or existing.tags != tags:
                    raise ValueError(
                        f"deterministic entry {entry_id} already exists with different content"
                    )
                if evidence_refs is not None and existing.evidence_refs != evidence_refs:
                    raise ValueError(
                        f"deterministic entry {entry_id} already exists with different provenance"
                    )
                indexed = conn.execute(
                    "SELECT 1 FROM entries WHERE id=? AND path=? LIMIT 1",
                    (entry_id, path.name),
                ).fetchone()
                if indexed is None:
                    fts.insert_entry(
                        conn,
                        id=entry_id,
                        path=path.name,
                        prefix=prefix,
                        timestamp=existing.timestamp,
                        tags=" ".join(existing.tags),
                        content=entry_index_content(existing),
                        superseded=entry_index_superseded(existing),
                    )
                if evidence_refs is not None:
                    provenance_store.replace_sources(
                        conn,
                        subject=EvidenceRef(kind="memory_entry", id=entry_id, path=path.name),
                        sources=evidence_refs,
                    )
                fts.upsert_file(
                    conn,
                    fts.FileRow(
                        path=path.name,
                        prefix=prefix,
                        description=str(post.metadata.get("description", "")),
                        tags=" ".join(post.metadata.get("tags", []) or []),
                        status=str(post.metadata.get("status", "active")),
                        entry_count=int(post.metadata.get("entry_count", 0)),
                        created=str(post.metadata.get("created", "")),
                        updated=str(post.metadata.get("updated", "")),
                        needs_compact=1 if post.metadata.get("needs_compact") else 0,
                    ),
                )
                return entry_id, False

        current = post.content.rstrip()
        new_block = (
            f"\n\n{heading}\n{rendered_body}\n" if current else f"{heading}\n{rendered_body}\n"
        )
        post.content = current + new_block
        post.metadata["entry_count"] = int(post.metadata.get("entry_count", 0)) + 1
        post.metadata["updated"] = files_mod.today()

        # Soft limit check
        if soft_limit_tokens is not None:
            est_tokens = len(post.content) // 4
            if est_tokens > soft_limit_tokens and not post.metadata.get("needs_compact"):
                post.metadata["needs_compact"] = True
                logger.info(
                    "flagged %s for compact (est %d tokens > %d)",
                    path.name,
                    est_tokens,
                    soft_limit_tokens,
                )

        indexed_entry = next(
            entry
            for entry in reversed(files_mod._parse_entries(post.content))
            if entry.id == entry_id
        )
        files_mod.atomic_write_text(path, frontmatter.dumps(post) + "\n")

        # Update FTS inside the lock too — a concurrent appender that
        # observes the file post-write must also observe the matching
        # FTS row, otherwise rebuild_index sees a row pointing at an
        # entry that "doesn't exist" until the second writer commits.
        fts.insert_entry(
            conn,
            id=entry_id,
            path=path.name,
            prefix=prefix,
            timestamp=indexed_entry.timestamp,
            tags=" ".join(indexed_entry.tags),
            content=entry_index_content(indexed_entry),
            superseded=entry_index_superseded(indexed_entry),
        )
        if evidence_refs is not None:
            provenance_store.replace_sources(
                conn,
                subject=EvidenceRef(kind="memory_entry", id=entry_id, path=path.name),
                sources=evidence_refs,
            )
        fts.upsert_file(
            conn,
            fts.FileRow(
                path=path.name,
                prefix=prefix,
                description=str(post.metadata.get("description", "")),
                tags=" ".join(post.metadata.get("tags", []) or []),
                status=str(post.metadata.get("status", "active")),
                entry_count=int(post.metadata.get("entry_count", 0)),
                created=str(post.metadata.get("created", "")),
                updated=str(post.metadata.get("updated", "")),
                needs_compact=1 if post.metadata.get("needs_compact") else 0,
            ),
        )
    return entry_id, True


def delete_entry(conn: sqlite3.Connection, *, name: str, entry_id: str) -> bool:
    """Idempotently remove one Markdown entry and every query projection.

    Projection cleanup is unconditional: after a crash between the canonical
    Markdown rename and SQLite deletes, replay must not return early merely
    because the heading is already gone.
    """
    require_autocommit(conn)
    path = files_mod.memory_path(name)
    removed = False
    with files_mod.store_write_lock(), files_mod.file_lock(path):
        _require_regular_memory_path(path)
        post = frontmatter.load(path) if path.exists() else None
        if post is not None:
            matches = list(files_mod.ENTRY_HEADING_RE.finditer(post.content))
            target_index = next(
                (index for index, match in enumerate(matches) if match.group("id") == entry_id),
                None,
            )
            if target_index is not None:
                start = matches[target_index].start()
                end = (
                    matches[target_index + 1].start()
                    if target_index + 1 < len(matches)
                    else len(post.content)
                )
                before = post.content[:start].rstrip()
                after = post.content[end:].lstrip()
                post.content = before + "\n\n" + after if before and after else before or after
                post.metadata["entry_count"] = max(0, len(matches) - 1)
                post.metadata["updated"] = files_mod.today()
                files_mod.atomic_write_text(path, frontmatter.dumps(post) + "\n")
                removed = True

        conn.execute("DELETE FROM entries WHERE id=? AND path=?", (entry_id, path.name))
        subject = EvidenceRef(kind="memory_entry", id=entry_id, path=path.name)
        provenance_store.delete_subject(conn, subject)
        provenance_store.delete_source_edges(conn, subject)
        if post is not None:
            if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=path.name):
                # Do not resurrect candidate-derived file metadata in the
                # crash gap between deleting the final entry and unlinking its
                # candidate-owned container.
                conn.execute("DELETE FROM files WHERE path=?", (path.name,))
            else:
                _upsert_file_projection(conn, path=path, post=post)
    return removed


def require_regular_purge_entry_path(conn: sqlite3.Connection, *, name: str) -> None:
    """Fail closed if a purge entry path is a symlink or non-regular file.

    Purge calls this for every planned entry path before it mutates any Daily
    Wrap or Markdown derivative. ``delete_entry`` repeats the same check while
    holding the rewrite locks, closing the external-editor TOCTOU window for
    each actual Markdown mutation.
    """
    require_autocommit(conn)
    path = files_mod.memory_path(name)
    with files_mod.store_write_lock(), files_mod.file_lock(path):
        _require_regular_memory_path(path)


def _require_regular_memory_path(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("memory purge path changed to a symlink")
    if path.exists() and not path.is_file():
        raise RuntimeError("memory purge path is no longer a regular file")


def finalize_candidate_owned_file(
    conn: sqlite3.Connection,
    *,
    name: str,
    owner_candidate_id: str,
    template_digest: str,
    delete_if_empty: bool,
) -> bool:
    """Delete or sanitize one verified candidate-created container.

    A file that still has unrelated entries or freeform body is retained, but
    candidate-derived description/tags and both owner markers are replaced by
    deterministic safe metadata. A missing path is an idempotent deletion.
    The caller keeps a ``memory_file`` tombstone active through verification.
    """
    require_autocommit(conn)
    path = files_mod.memory_path(name)
    with files_mod.store_write_lock(), files_mod.file_lock(path):
        if path.is_symlink():
            raise RuntimeError("candidate-owned memory path changed to a symlink")
        if not path.exists():
            conn.execute("DELETE FROM files WHERE path=?", (path.name,))
            return True
        if not path.is_file():
            raise RuntimeError("candidate-owned memory path is no longer a regular file")
        try:
            post = frontmatter.load(path)
            fm = dict(post.metadata)
        except Exception as exc:
            raise RuntimeError("candidate-owned memory file is no longer readable") from exc

        parsed_entries = files_mod._parse_entries(post.content)
        surviving_tags = sorted({tag for entry in parsed_entries for tag in entry.tags})
        ownership_is_current = (
            files_mod.candidate_file_owner(fm, path_name=path.name) == owner_candidate_id
            and fm.get(files_mod.CANDIDATE_FILE_TEMPLATE_DIGEST_KEY) == template_digest
        )
        already_sanitized = files_mod.candidate_file_is_sanitized(
            fm,
            surviving_tags=surviving_tags,
            entry_count=len(parsed_entries),
        )
        if not ownership_is_current and not already_sanitized:
            raise RuntimeError("candidate-owned memory file metadata changed after preview")
        if already_sanitized:
            _upsert_file_projection(conn, path=path, post=post)
            return False

        if delete_if_empty and not post.content.strip():
            path.unlink()
            # Persist the unlink just as atomic_write_text persists its rename.
            with contextlib.suppress(OSError):
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            conn.execute("DELETE FROM files WHERE path=?", (path.name,))
            return True

        post.metadata.pop(files_mod.CANDIDATE_FILE_OWNER_KEY, None)
        post.metadata.pop(files_mod.CANDIDATE_FILE_TEMPLATE_DIGEST_KEY, None)
        post.metadata["description"] = files_mod.SANITIZED_CANDIDATE_FILE_DESCRIPTION
        post.metadata["tags"] = surviving_tags
        post.metadata["entry_count"] = len(parsed_entries)
        post.metadata["updated"] = files_mod.today()
        files_mod.atomic_write_text(path, frontmatter.dumps(post) + "\n")
        _upsert_file_projection(conn, path=path, post=post)
        return False


def require_candidate_owned_file(
    conn: sqlite3.Connection,
    *,
    name: str,
    owner_candidate_id: str,
    template_digest: str,
    planned_entry_ids: set[str],
) -> None:
    """Fail before purge rewrites if the reviewed owned path was replaced."""
    require_autocommit(conn)
    path = files_mod.memory_path(name)
    with files_mod.store_write_lock(), files_mod.file_lock(path):
        if path.is_symlink():
            raise RuntimeError("candidate-owned memory path changed to a symlink")
        if not path.exists():
            return
        if not path.is_file():
            raise RuntimeError("candidate-owned memory path is no longer a regular file")
        try:
            post = frontmatter.load(path)
            fm = dict(post.metadata)
        except Exception as exc:
            raise RuntimeError("candidate-owned memory file is no longer readable") from exc

        ownership_is_current = (
            files_mod.candidate_file_owner(fm, path_name=path.name) == owner_candidate_id
            and fm.get(files_mod.CANDIDATE_FILE_TEMPLATE_DIGEST_KEY) == template_digest
        )
        parsed_entries = files_mod._parse_entries(post.content)
        surviving_tags = sorted({tag for entry in parsed_entries for tag in entry.tags})
        already_sanitized = files_mod.candidate_file_is_sanitized(
            fm,
            surviving_tags=surviving_tags,
            entry_count=len(parsed_entries),
        ) and not any(entry.id in planned_entry_ids for entry in parsed_entries)
        if not ownership_is_current and not already_sanitized:
            raise RuntimeError("candidate-owned memory file metadata changed after preview")


def _upsert_file_projection(
    conn: sqlite3.Connection, *, path: Path, post: frontmatter.Post
) -> None:
    fts.upsert_file(
        conn,
        fts.FileRow(
            path=path.name,
            prefix=_ensure_prefix(path.name),
            description=str(post.metadata.get("description", "")),
            tags=" ".join(post.metadata.get("tags", []) or []),
            status=str(post.metadata.get("status", "active")),
            entry_count=int(post.metadata.get("entry_count", 0)),
            created=str(post.metadata.get("created", "")),
            updated=str(post.metadata.get("updated", "")),
            needs_compact=1 if post.metadata.get("needs_compact") else 0,
        ),
    )


def supersede_entry(
    conn: sqlite3.Connection,
    *,
    name: str,
    old_entry_id: str,
    new_content: str,
    reason: str,
    tags: list[str] | None = None,
) -> str:
    """Mark old entry superseded and append a provenance-linked replacement."""
    with files_mod.review_operation_lock():
        return _supersede_entry_locked(
            conn,
            name=name,
            old_entry_id=old_entry_id,
            new_content=new_content,
            reason=reason,
            tags=tags,
        )


def _supersede_entry_locked(
    conn: sqlite3.Connection,
    *,
    name: str,
    old_entry_id: str,
    new_content: str,
    reason: str,
    tags: list[str] | None,
) -> str:
    require_autocommit(conn)
    path = files_mod.memory_path(name)
    if not path.exists():
        raise FileNotFoundError(path.name)
    if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=path.name):
        raise RuntimeError(f"{path.name} is pending permanent purge")
    body = new_content.strip()
    files_mod.validate_entry_body(body)
    clean_reason = " ".join(reason.strip().splitlines())
    if files_mod.ENTRY_HEADING_RE.search(clean_reason):
        raise ValueError("supersede reason contains a reserved entry heading")
    if "-->" in clean_reason or files_mod.PROVENANCE_MARKER_RE.search(clean_reason):
        raise ValueError("supersede reason contains a reserved comment marker")
    if tags is not None and any(not tag or any(char.isspace() for char in tag) for tag in tags):
        raise ValueError("entry tags must be non-empty and contain no whitespace")

    # Same shape as append_entry: read-modify-write on a markdown file
    # plus an FTS update. Holding the lock across both halves keeps
    # readers from seeing a state where the file has the new entry but
    # FTS still doesn't (or vice versa).
    with files_mod.store_write_lock(), files_mod.file_lock(path):
        if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=path.name):
            raise RuntimeError(f"{path.name} is pending permanent purge")
        parsed = files_mod.read_file(path)
        target = next((e for e in parsed.entries if e.id == old_entry_id), None)
        if target is None:
            raise ValueError(f"entry {old_entry_id} not found in {path.name}")
        if not target.provenance_valid:
            raise ValueError(f"entry {old_entry_id} has an invalid provenance frame")
        current_source = EvidenceRef(
            kind="memory_entry",
            id=old_entry_id,
            path=path.name,
            timestamp=target.timestamp,
            content_hash=content_digest(target.body),
        )
        _require_live_dependency_sources(conn, [current_source])

        # Build replacement heading and body in the file
        ts = _now_iso_minute()
        new_id = make_id(ts)

        new_heading = files_mod.render_heading(
            timestamp=ts,
            entry_id=new_id,
            tags=[
                *(tags or target.tags),
                f"{files_mod.ENTRY_ORIGIN_TAG_PREFIX}{files_mod.DERIVED_ENTRY_ORIGIN}",
            ],
        )

        # Modify file text directly to preserve formatting
        text = path.read_text()
        # 1) append #superseded-by to old heading (only if not already present)
        old_heading = target.heading_line
        if f"superseded-by:{new_id}" not in old_heading:
            updated_heading = old_heading.rstrip() + f" #superseded-by:{new_id}"
            text = text.replace(old_heading, updated_heading, 1)
        # 2) wrap old body in ~~...~~ (only if not already)
        striked = target.body
        if target.body and not _body_is_striked(target.body):
            striked = "~~" + target.body.strip() + "~~"
            text = text.replace(target.body, striked, 1)

        replacement_source = EvidenceRef(
            kind="memory_entry",
            id=old_entry_id,
            path=path.name,
            timestamp=target.timestamp,
            content_hash=content_digest(striked),
        )
        import json

        provenance_payload = {
            "v": 1,
            "sources": [replacement_source.to_dict()],
        }
        provenance_comment = (
            "<!-- oc-provenance: "
            + json.dumps(
                provenance_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + " -->"
        )

        # 3) Append the new entry at the end
        new_block = f"\n\n{new_heading}\n{body}\n{provenance_comment}\n"
        if not text.endswith("\n"):
            text += "\n"
        text += new_block

        # Fold the metadata bump (entry_count, updated) into a SINGLE write
        # via in-memory parse (frontmatter.loads), so the lock holds across
        # one atomic write rather than two writes with a reload between.
        post = frontmatter.loads(text)
        post.metadata["entry_count"] = int(post.metadata.get("entry_count", 0)) + 1
        post.metadata["updated"] = files_mod.today()
        updated_entries = files_mod._parse_entries(post.content)
        updated_target = next(
            entry
            for entry in updated_entries
            if entry.id == old_entry_id
        )
        replacement = next(
            entry for entry in reversed(updated_entries) if entry.id == new_id
        )
        files_mod.atomic_write_text(path, frontmatter.dumps(post) + "\n")

        # FTS
        prefix = _ensure_prefix(name)
        fts.mark_superseded(
            conn,
            old_entry_id,
            path=path.name,
            prefix=prefix,
            timestamp=updated_target.timestamp,
            tags=" ".join(updated_target.tags),
            content=entry_index_content(updated_target),
        )
        fts.insert_entry(
            conn,
            id=new_id,
            path=path.name,
            prefix=prefix,
            timestamp=replacement.timestamp,
            tags=" ".join(replacement.tags),
            content=entry_index_content(replacement),
            superseded=entry_index_superseded(replacement),
        )
        provenance_store.replace_sources(
            conn,
            subject=EvidenceRef(kind="memory_entry", id=new_id, path=path.name),
            sources=[replacement_source],
        )
        fts.upsert_file(
            conn,
            fts.FileRow(
                path=path.name,
                prefix=prefix,
                description=str(post.metadata.get("description", "")),
                tags=" ".join(post.metadata.get("tags", []) or []),
                status=str(post.metadata.get("status", "active")),
                entry_count=int(post.metadata.get("entry_count", 0)),
                created=str(post.metadata.get("created", "")),
                updated=str(post.metadata.get("updated", "")),
                needs_compact=1 if post.metadata.get("needs_compact") else 0,
            ),
        )
    return new_id


def rebuild_index(conn: sqlite3.Connection) -> tuple[int, int]:
    """Full rebuild: drop all FTS rows and files rows, re-ingest from Markdown."""
    require_autocommit(conn)
    with files_mod.review_operation_lock(), files_mod.store_write_lock():
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM entries")
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM provenance_edges WHERE subject_kind='memory_entry'")
            file_count = 0
            entry_count = 0
            pending_entries: list[tuple[Path, str, files_mod.ParsedEntry]] = []
            for path in files_mod.list_memory_files():
                if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=path.name):
                    continue
                try:
                    prefix = _ensure_prefix(path.name)
                except ValueError as exc:
                    logger.warning("skipping %s: %s", path.name, exc)
                    continue
                with files_mod.file_lock(path):
                    parsed = files_mod.read_file(path)
                    fts.upsert_file(
                        conn,
                        fts.FileRow(
                            path=path.name,
                            prefix=prefix,
                            description=parsed.description,
                            tags=" ".join(parsed.tags),
                            status=parsed.status,
                            entry_count=len(parsed.entries),
                            created=parsed.created,
                            updated=parsed.updated,
                            needs_compact=1 if parsed.needs_compact else 0,
                        ),
                    )
                    file_count += 1
                    for e in parsed.entries:
                        if not e.provenance_valid:
                            raise ValueError(
                                f"invalid provenance frame in {path.name}#{e.id}: "
                                f"{e.provenance_error}"
                            )
                        if candidate_store.is_tombstoned(
                            conn,
                            kind="memory_entry",
                            artifact_id=e.id,
                            path=path.name,
                        ):
                            continue
                        pending_entries.append((path, prefix, e))

            # Rebuild provenance in dependency order rather than filename order.
            # ``entries`` was just cleared, so asking the normal current-source
            # predicate during a one-pass lexical scan would incorrectly drop a
            # valid project entry whose user-* source is ingested later.
            rebuilt_hashes: dict[tuple[str, str], str] = {}
            while pending_entries:
                deferred: list[tuple[Path, str, files_mod.ParsedEntry]] = []
                progressed = False
                for path, prefix, e in pending_entries:
                    if not _rebuild_dependency_sources_are_live(
                        conn, e.evidence_refs, rebuilt_hashes
                    ):
                        deferred.append((path, prefix, e))
                        continue
                    fts.insert_entry(
                        conn,
                        id=e.id,
                        path=path.name,
                        prefix=prefix,
                        timestamp=e.timestamp,
                        tags=" ".join(e.tags),
                        content=entry_index_content(e),
                        superseded=entry_index_superseded(e),
                    )
                    provenance_store.record_sources(
                        conn,
                        subject=EvidenceRef(kind="memory_entry", id=e.id, path=path.name),
                        sources=e.evidence_refs,
                    )
                    rebuilt_hashes[(path.name, e.id)] = content_digest(e.body)
                    entry_count += 1
                    progressed = True
                if not progressed:
                    for path, _prefix, e in deferred:
                        logger.warning(
                            "skipping %s#%s: provenance dependency is missing or changed",
                            path.name,
                            e.id,
                        )
                    break
                pending_entries = deferred
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return file_count, entry_count


def _require_live_dependency_sources(conn: sqlite3.Connection, sources: list[EvidenceRef]) -> None:
    if not dependency_sources_are_live(conn, sources):
        raise ValueError("entry provenance dependency is missing or changed")


def dependency_sources_are_live(conn: sqlite3.Connection, sources: list[EvidenceRef]) -> bool:
    return _dependency_sources_are_live_recursive(
        conn,
        sources,
        cache={},
        visiting=set(),
    )


def _dependency_sources_are_live_recursive(
    conn: sqlite3.Connection,
    sources: list[EvidenceRef],
    *,
    cache: dict[tuple[str, str, str, str], bool],
    visiting: set[tuple[str, str, str]],
) -> bool:
    for source in sources:
        if source.kind == "memory_entry":
            cache_key = (
                source.kind,
                source.path,
                source.id,
                source.content_hash,
            )
            cached = cache.get(cache_key)
            if cached is not None:
                if not cached:
                    return False
                continue
            identity = (source.kind, source.path, source.id)
            if identity in visiting or not source.content_hash:
                cache[cache_key] = False
                return False
            if candidate_store.is_tombstoned(
                conn, kind="memory_file", artifact_id=source.path
            ) or candidate_store.is_tombstoned(
                conn,
                kind="memory_entry",
                artifact_id=source.id,
                path=source.path,
            ):
                cache[cache_key] = False
                return False
            indexed = conn.execute(
                "SELECT 1 FROM entries WHERE id=? AND path=? LIMIT 1",
                (source.id, source.path),
            ).fetchone()
            if indexed is None:
                cache[cache_key] = False
                return False
            try:
                parsed = files_mod.read_file(files_mod.memory_path(source.path))
            except Exception:  # noqa: BLE001 - corrupt dependencies fail closed
                cache[cache_key] = False
                return False
            entry = next((item for item in parsed.entries if item.id == source.id), None)
            if (
                entry is None
                or not entry.provenance_valid
                or content_digest(entry.body) != source.content_hash
            ):
                cache[cache_key] = False
                return False
            visiting.add(identity)
            try:
                live = _dependency_sources_are_live_recursive(
                    conn,
                    entry.evidence_refs,
                    cache=cache,
                    visiting=visiting,
                )
            finally:
                visiting.remove(identity)
            cache[cache_key] = live
            if not live:
                return False
        if source.kind == "memory_candidate" and (
            candidate_store.is_tombstoned(conn, kind="memory_candidate", artifact_id=source.id)
            or candidate_store.get(conn, source.id) is None
        ):
            return False
    return True


def _rebuild_dependency_sources_are_live(
    conn: sqlite3.Connection,
    sources: list[EvidenceRef],
    rebuilt_hashes: dict[tuple[str, str], str],
) -> bool:
    for source in sources:
        if (
            source.kind == "memory_entry"
            and rebuilt_hashes.get((source.path, source.id)) != source.content_hash
        ):
            return False
        if source.kind == "memory_candidate" and (
            candidate_store.is_tombstoned(conn, kind="memory_candidate", artifact_id=source.id)
            or candidate_store.get(conn, source.id) is None
        ):
            return False
    return True


_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)


def _body_is_striked(body: str) -> bool:
    stripped = body.strip()
    return stripped.startswith("~~") and stripped.endswith("~~")


def _strip_strike(body: str) -> str:
    return _STRIKE_RE.sub(r"\1", body)


def entry_index_content(entry: files_mod.ParsedEntry) -> str:
    """Return the deterministic FTS content projection for one Markdown entry.

    Supersession wraps the complete old body in ``~~`` while intentionally
    retaining the original searchable text in FTS.  Inline Markdown strikeout
    in an otherwise-current body is ordinary user content and must not be
    rewritten by an index rebuild.
    """
    stripped = entry.body.strip()
    if _body_is_striked(entry.body):
        return stripped[2:-2]
    return entry.body


def entry_index_superseded(entry: files_mod.ParsedEntry) -> int:
    """Return the canonical integer projection for FTS supersession state."""
    return 1 if (entry.superseded_by or _body_is_striked(entry.body)) else 0


def write_preset_files(conn: sqlite3.Connection) -> None:
    """Create user-profile.md and user-preferences.md if absent."""
    presets: dict[str, dict[str, Any]] = {
        "user-profile.md": {
            "description": (
                "User's identity, background, and long-term stable basic information "
                "(name, profession, languages, location, skill stack, etc.)"
            ),
            "tags": ["identity", "background"],
        },
        "user-preferences.md": {
            "description": (
                "User's preferences, habits, working style, and subjective tool choices"
            ),
            "tags": ["preferences"],
        },
    }
    for name, info in presets.items():
        if files_mod.memory_path(name).exists():
            continue
        with contextlib.suppress(FileExistsError):
            create_file(conn, name=name, description=info["description"], tags=info["tags"])
