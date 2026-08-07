"""Review-first durable-memory operations."""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
from dataclasses import dataclass

from ..logger import get
from ..memory_candidates import store as candidate_store
from ..memory_candidates.store import MemoryCandidate
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, content_digest
from ..store import entries as entries_store
from ..store import files as files_store

logger = get("openchronicle.memory")


@dataclass(frozen=True, slots=True)
class PurgeResult:
    candidate_id: str
    removed_entry: bool
    invalidated_wraps: tuple[str, ...]


@contextlib.contextmanager
def _review_operation_lock():
    """Serialize candidate mutations and purge fencing across processes."""
    with files_store.review_operation_lock():
        yield


class MemoryService:
    """Typed mutation boundary for the local review inbox.

    The MCP adapter intentionally does not expose these methods. Mutations are
    available through trusted local CLI/UI adapters where a human can review
    the exact content and evidence first.
    """

    def __init__(self, conn: sqlite3.Connection, *, soft_limit_tokens: int | None = None):
        self.conn = conn
        self.soft_limit_tokens = soft_limit_tokens

    def propose_candidate(
        self,
        *,
        kind: str,
        target_path: str,
        content: str,
        tags: list[str],
        evidence: list[EvidenceRef],
        confidence: float | None = None,
        conflict_key: str = "",
        producer_run_key: str = "",
        proposal_slot: int = 0,
    ) -> MemoryCandidate:
        with _review_operation_lock():
            return self._propose_candidate_locked(
                kind=kind,
                target_path=target_path,
                content=content,
                tags=tags,
                evidence=evidence,
                confidence=confidence,
                conflict_key=conflict_key,
                producer_run_key=producer_run_key,
                proposal_slot=proposal_slot,
            )

    def _propose_candidate_locked(
        self,
        *,
        kind: str,
        target_path: str,
        content: str,
        tags: list[str],
        evidence: list[EvidenceRef],
        confidence: float | None,
        conflict_key: str,
        producer_run_key: str,
        proposal_slot: int,
    ) -> MemoryCandidate:
        target_path = _normalize_target_path(target_path)
        normalized_content = _normalize_content(content)
        clean_tags = _normalize_tags(tags)
        clean_kind = kind.strip().lower()
        if not clean_kind:
            raise ValueError("candidate kind is required")
        if not evidence:
            raise ValueError("memory candidates require at least one evidence reference")
        if any(not ref.content_hash for ref in evidence):
            raise ValueError("memory candidate evidence must bind a source content hash")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        conflict_key = conflict_key.strip().casefold()
        digest = content_digest(normalized_content)
        proposal_digest = _candidate_idempotency_key(
            kind=clean_kind,
            target_path=target_path,
            content_hash=digest,
            tags=clean_tags,
            evidence=evidence,
        )
        clean_run_key = producer_run_key.strip()
        if proposal_slot < 0:
            raise ValueError("proposal_slot must be non-negative")
        idempotency_key = (
            hashlib.sha256(
                f"memory-candidate-run-v1\0{clean_run_key}\0{proposal_slot}".encode()
            ).hexdigest()
            if clean_run_key
            else proposal_digest
        )
        candidate_id = "mc-" + idempotency_key[:24]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if candidate_store.is_tombstoned(
                self.conn, kind="memory_file", artifact_id=target_path
            ):
                raise ValueError("candidate target is pending permanent purge")
            invalid_sources = [
                source
                for source in evidence
                if not provenance_store.is_current(self.conn, source)
            ]
            if invalid_sources:
                raise ValueError("memory candidate evidence is missing or changed")
            status = (
                "conflict"
                if candidate_store.active_conflicts(
                    self.conn,
                    target_path=target_path,
                    conflict_key=conflict_key,
                    content_hash=digest,
                )
                else "pending"
            )
            candidate, created = candidate_store.insert(
                self.conn,
                candidate_id=candidate_id,
                idempotency_key=idempotency_key,
                proposal_digest=proposal_digest,
                producer_run_key=clean_run_key,
                proposal_slot=proposal_slot,
                kind=clean_kind,
                operation="append",
                target_path=target_path,
                content=normalized_content,
                content_hash=digest,
                tags=clean_tags,
                confidence=confidence,
                conflict_key=conflict_key,
                status=status,
            )
            existing_sources = provenance_store.direct_sources(
                self.conn, _candidate_ref(candidate.id)
            )
            if created or (
                candidate.proposal_digest == proposal_digest
                and not existing_sources
            ):
                provenance_store.replace_sources(
                    self.conn,
                    subject=_candidate_ref(candidate.id),
                    sources=evidence,
                )
            elif (
                candidate.proposal_digest != proposal_digest
                or existing_sources != evidence
            ):
                candidate_store.record_replay_mismatch(self.conn, candidate.id)
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return candidate_store.get(self.conn, candidate.id) or candidate

    def list_candidates(
        self, *, statuses: list[str] | None = None, limit: int = 100
    ) -> list[MemoryCandidate]:
        return candidate_store.list_candidates(self.conn, statuses=statuses, limit=limit)

    def get_candidate(self, candidate_id: str) -> MemoryCandidate | None:
        return candidate_store.get(self.conn, candidate_id)

    def edit_candidate(
        self,
        candidate_id: str,
        *,
        expected_version: int,
        content: str,
        tags: list[str],
        conflict_key: str | None = None,
    ) -> MemoryCandidate:
        with _review_operation_lock():
            current = self._required(candidate_id)
            normalized_content = _normalize_content(content)
            clean_tags = _normalize_tags(tags)
            next_conflict_key = (
                current.conflict_key
                if conflict_key is None
                else conflict_key.strip().casefold()
            )
            digest = content_digest(normalized_content)
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                has_conflict = any(
                    candidate.id != candidate_id
                    for candidate in candidate_store.active_conflicts(
                        self.conn,
                        target_path=current.target_path,
                        conflict_key=next_conflict_key,
                        content_hash=digest,
                    )
                )
                updated = candidate_store.update_content(
                    self.conn,
                    candidate_id=candidate_id,
                    expected_version=expected_version,
                    content=normalized_content,
                    content_hash=digest,
                    tags=clean_tags,
                    conflict_key=next_conflict_key,
                    status="conflict" if has_conflict else "pending",
                )
                self.conn.execute("COMMIT")
                return updated
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    def approve_candidate(
        self, candidate_id: str, *, expected_version: int
    ) -> MemoryCandidate:
        with _review_operation_lock():
            return self._approve_candidate_locked(
                candidate_id, expected_version=expected_version
            )

    def _approve_candidate_locked(
        self, candidate_id: str, *, expected_version: int
    ) -> MemoryCandidate:
        current = self._required(candidate_id)
        if current.status == "accepted":
            return current
        if current.status == "applying":
            if expected_version not in {current.version, current.version - 1}:
                raise candidate_store.CandidateConflict("candidate version changed")
        elif current.version != expected_version:
            raise candidate_store.CandidateConflict("candidate version changed")

        sources = provenance_store.direct_sources(self.conn, _candidate_ref(candidate_id))
        invalid_sources = [source for source in sources if not provenance_store.is_current(self.conn, source)]
        if not sources or invalid_sources:
            detail = (
                "candidate has no durable evidence"
                if not sources
                else "candidate evidence is missing or changed"
            )
            latest = self._required(candidate_id)
            candidate_store.transition(
                self.conn,
                candidate_id=candidate_id,
                expected_version=latest.version,
                from_statuses=(latest.status,),
                to_status="conflict",
                error=detail,
            )
            raise candidate_store.CandidateConflict(detail)

        if current.status == "applying":
            applying = current
        else:
            applying = candidate_store.transition(
                self.conn,
                candidate_id=candidate_id,
                expected_version=expected_version,
                from_statuses=("pending", "conflict"),
                to_status="applying",
            )
        entry_id = _candidate_entry_id(candidate_id)
        entry_sources = [_candidate_ref(candidate_id), *sources]
        try:
            if not files_store.memory_path(applying.target_path).exists():
                with contextlib.suppress(FileExistsError):
                    entries_store.create_file(
                        self.conn,
                        name=applying.target_path,
                        description=f"Reviewed {applying.kind} memories.",
                        tags=applying.tags,
                    )
            entries_store.append_entry_once(
                self.conn,
                name=applying.target_path,
                content=applying.content,
                tags=applying.tags,
                entry_id=entry_id,
                evidence_refs=entry_sources,
                soft_limit_tokens=self.soft_limit_tokens,
            )
        except BaseException as exc:
            latest = self._required(candidate_id)
            if latest.status == "applying":
                error = f"{type(exc).__name__}: {exc}"[:1000]
                if _markdown_entry_exists(applying.target_path, entry_id):
                    candidate_store.set_last_error(self.conn, candidate_id, error)
                else:
                    candidate_store.transition(
                        self.conn,
                        candidate_id=candidate_id,
                        expected_version=latest.version,
                        from_statuses=("applying",),
                        to_status="pending",
                        error=error,
                    )
            raise
        try:
            return candidate_store.transition(
                self.conn,
                candidate_id=candidate_id,
                expected_version=applying.version,
                from_statuses=("applying",),
                to_status="accepted",
                applied_entry_id=entry_id,
            )
        except candidate_store.CandidateConflict:
            latest = self._required(candidate_id)
            if latest.status == "accepted" and latest.applied_entry_id == entry_id:
                return latest
            raise

    def reject_candidate(
        self,
        candidate_id: str,
        *,
        expected_version: int,
        reason: str = "",
    ) -> MemoryCandidate:
        with _review_operation_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                rejected = candidate_store.transition(
                    self.conn,
                    candidate_id=candidate_id,
                    expected_version=expected_version,
                    from_statuses=("pending", "conflict"),
                    to_status="rejected",
                    reason=reason.strip()[:1000],
                )
                self.conn.execute("COMMIT")
                return rejected
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    def purge_candidate(self, candidate_id: str) -> PurgeResult:
        """Crash-resumably forget a proposal and its accepted derivatives."""
        with _review_operation_lock():
            return self._purge_candidate_locked(candidate_id)

    def _purge_candidate_locked(self, candidate_id: str) -> PurgeResult:
        tombstones = [
            tombstone
            for tombstone in candidate_store.list_tombstones(
                self.conn, kind="memory_candidate"
            )
            if tombstone.artifact_id == candidate_id
        ]
        if tombstones:
            return self._execute_purge(tombstones[0])

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # The closure snapshot and every deny-read tombstone commit as one
            # write transaction. Candidate proposals and wrap publication also
            # use BEGIN IMMEDIATE, so no new dependent can land in between.
            plan = self._build_purge_plan(self._required(candidate_id))
            candidate_store.put_tombstone(
                self.conn,
                kind="memory_candidate",
                artifact_id=candidate_id,
                plan=plan,
            )
            for entry in plan["entries"]:
                assert isinstance(entry, dict)
                candidate_store.put_tombstone(
                    self.conn,
                    kind="memory_entry",
                    artifact_id=str(entry["id"]),
                    path=str(entry["path"]),
                )
                # Hide plaintext from FTS immediately, before canonical file
                # deletion. Replay remains protected by the tombstone.
                self.conn.execute(
                    "DELETE FROM entries WHERE id=? AND path=?",
                    (str(entry["id"]), str(entry["path"])),
                )
            for wrap_id in plan["wrap_ids"]:
                candidate_store.put_tombstone(
                    self.conn, kind="daily_wrap", artifact_id=str(wrap_id)
                )
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        tombstone = next(
            tombstone
            for tombstone in candidate_store.list_tombstones(
                self.conn, kind="memory_candidate"
            )
            if tombstone.artifact_id == candidate_id
        )
        return self._execute_purge(tombstone)

    def resume_pending_purges(self) -> list[PurgeResult]:
        with _review_operation_lock():
            results: list[PurgeResult] = []
            for tombstone in candidate_store.list_tombstones(
                self.conn, kind="memory_candidate"
            ):
                results.append(self._execute_purge(tombstone))
            return results

    def _execute_purge(
        self, tombstone: candidate_store.PurgeTombstone
    ) -> PurgeResult:
        candidate_id, candidates, entries, wrap_ids = _decode_purge_plan(tombstone)
        removed_entry = False
        try:
            from ..daily_wrap import store as daily_wrap_store

            for wrap_id in wrap_ids:
                daily_wrap_store.purge(self.conn, wrap_id)
            for entry in entries:
                removed_entry = (
                    entries_store.delete_entry(
                        self.conn,
                        name=entry["path"],
                        entry_id=entry["id"],
                    )
                    or removed_entry
                )
            for purged_candidate_id in candidates:
                candidate_ref = _candidate_ref(purged_candidate_id)
                provenance_store.delete_subject(self.conn, candidate_ref)
                provenance_store.delete_source_edges(self.conn, candidate_ref)
                candidate_store.delete(self.conn, purged_candidate_id)

            self._verify_purge(candidates, entries, wrap_ids)
            checkpoint = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise RuntimeError("secure purge checkpoint is busy; retry required")

            self.conn.execute("SAVEPOINT memory_purge_finish")
            try:
                for entry in entries:
                    candidate_store.delete_tombstone(
                        self.conn,
                        kind="memory_entry",
                        artifact_id=entry["id"],
                        path=entry["path"],
                    )
                for wrap_id in wrap_ids:
                    candidate_store.delete_tombstone(
                        self.conn, kind="daily_wrap", artifact_id=wrap_id
                    )
                candidate_store.delete_tombstone(
                    self.conn,
                    kind="memory_candidate",
                    artifact_id=tombstone.artifact_id,
                    path=tombstone.path,
                )
                self.conn.execute("RELEASE SAVEPOINT memory_purge_finish")
            except BaseException:
                self.conn.execute("ROLLBACK TO SAVEPOINT memory_purge_finish")
                self.conn.execute("RELEASE SAVEPOINT memory_purge_finish")
                raise
        except BaseException as exc:
            candidate_store.set_tombstone_error(
                self.conn,
                kind="memory_candidate",
                artifact_id=tombstone.artifact_id,
                path=tombstone.path,
                error=f"{type(exc).__name__}: purge incomplete",
            )
            raise
        return PurgeResult(
            candidate_id=candidate_id,
            removed_entry=removed_entry,
            invalidated_wraps=tuple(sorted(set(wrap_ids))),
        )

    def _build_purge_plan(self, root: MemoryCandidate) -> dict[str, object]:
        candidates: dict[str, MemoryCandidate] = {root.id: root}
        entries: dict[tuple[str, str], EvidenceRef] = {}
        wrap_ids: set[str] = set()
        queue: list[EvidenceRef] = [_candidate_ref(root.id)]
        markdown_dependents: dict[
            tuple[str, str, str], list[EvidenceRef]
        ] = {}

        # The SQLite edge graph is a projection written after the atomic
        # Markdown rename. Scan embedded frames as a second source of truth so
        # an append that crashed in that narrow gap still enters the deletion
        # closure instead of leaving a plaintext orphan on disk.
        for path in files_store.list_memory_files():
            if candidate_store.is_tombstoned(
                self.conn, kind="memory_file", artifact_id=path.name
            ):
                continue
            parsed = files_store.read_file(path)
            for entry in parsed.entries:
                if not entry.provenance_valid:
                    logger.warning(
                        "cannot infer purge dependency from invalid frame %s#%s",
                        path.name,
                        entry.id,
                    )
                    continue
                dependent = EvidenceRef(
                    kind="memory_entry", id=entry.id, path=path.name
                )
                for source in entry.evidence_refs:
                    markdown_dependents.setdefault(
                        (source.kind, source.path, source.id), []
                    ).append(dependent)

        def add_candidate_entries(candidate: MemoryCandidate) -> None:
            entry_ids = {
                _candidate_entry_id(candidate.id),
                candidate.applied_entry_id or "",
            }
            target = files_store.memory_path(candidate.target_path)
            if target.exists():
                parsed = files_store.read_file(target)
                entry_ids.update(
                    entry.id
                    for entry in parsed.entries
                    if any(
                        source.kind == "memory_candidate"
                        and source.id == candidate.id
                        for source in entry.evidence_refs
                    )
                )
            for entry_id in entry_ids:
                if not entry_id:
                    continue
                entry_ref = EvidenceRef(
                    kind="memory_entry", id=entry_id, path=candidate.target_path
                )
                entries[(entry_ref.path, entry_ref.id)] = entry_ref
                queue.append(entry_ref)

        add_candidate_entries(root)

        seen: set[tuple[str, str, str]] = set()
        while queue:
            source = queue.pop(0)
            key = (source.kind, source.path, source.id)
            if key in seen:
                continue
            seen.add(key)
            for dependent in markdown_dependents.get(key, []):
                entry_key = (dependent.path, dependent.id)
                if entry_key not in entries:
                    entries[entry_key] = dependent
                    queue.append(dependent)
            for dependent in provenance_store.direct_dependents(self.conn, source):
                if dependent.kind == "memory_candidate":
                    candidate = candidate_store.get(self.conn, dependent.id)
                    if candidate is not None and candidate.id not in candidates:
                        candidates[candidate.id] = candidate
                        queue.append(_candidate_ref(candidate.id))
                        add_candidate_entries(candidate)
                elif dependent.kind == "memory_entry":
                    entry_key = (dependent.path, dependent.id)
                    if entry_key not in entries:
                        entries[entry_key] = dependent
                        queue.append(dependent)
                elif dependent.kind == "daily_wrap":
                    wrap_ids.add(dependent.id)
                elif dependent.kind in {"daily_wrap_item", "daily_wrap_revision"}:
                    if dependent.path:
                        wrap_ids.add(dependent.path)

        return {
            "candidate_id": root.id,
            "candidates": sorted(candidates),
            "entries": [
                {"id": ref.id, "path": ref.path}
                for ref in sorted(entries.values(), key=lambda item: (item.path, item.id))
            ],
            "wrap_ids": sorted(wrap_ids),
        }

    def _verify_purge(
        self,
        candidates: list[str],
        entries: list[dict[str, str]],
        wrap_ids: list[str],
    ) -> None:
        from ..daily_wrap import store as daily_wrap_store

        for entry in entries:
            if self.conn.execute(
                "SELECT 1 FROM entries WHERE id=? AND path=? LIMIT 1",
                (entry["id"], entry["path"]),
            ).fetchone():
                raise RuntimeError("memory entry projection survived purge")
            path = files_store.memory_path(entry["path"])
            if path.exists() and any(
                item.id == entry["id"] for item in files_store.read_file(path).entries
            ):
                raise RuntimeError("memory entry Markdown survived purge")
            if self.conn.execute(
                """
                SELECT 1 FROM provenance_edges
                 WHERE (subject_kind='memory_entry' AND subject_id=? AND subject_path=?)
                    OR (source_kind='memory_entry' AND source_id=? AND source_path=?)
                 LIMIT 1
                """,
                (entry["id"], entry["path"], entry["id"], entry["path"]),
            ).fetchone():
                raise RuntimeError("memory entry provenance survived purge")
        for purged_candidate_id in candidates:
            if candidate_store.get(self.conn, purged_candidate_id) is not None:
                raise RuntimeError("memory candidate survived purge")
            if self.conn.execute(
                """
                SELECT 1 FROM provenance_edges
                 WHERE (subject_kind='memory_candidate' AND subject_id=?)
                    OR (source_kind='memory_candidate' AND source_id=?)
                 LIMIT 1
                """,
                (purged_candidate_id, purged_candidate_id),
            ).fetchone():
                raise RuntimeError("memory candidate provenance survived purge")
        for wrap_id in wrap_ids:
            if daily_wrap_store.get_by_id(self.conn, wrap_id) is not None:
                raise RuntimeError("Daily Wrap survived purge")
            if self.conn.execute(
                "SELECT 1 FROM daily_wrap_revisions WHERE wrap_id=? LIMIT 1",
                (wrap_id,),
            ).fetchone():
                raise RuntimeError("Daily Wrap revision survived purge")

    def _required(self, candidate_id: str) -> MemoryCandidate:
        candidate = candidate_store.get(self.conn, candidate_id)
        if candidate is None:
            raise KeyError(f"memory candidate not found: {candidate_id}")
        return candidate


def _candidate_ref(candidate_id: str) -> EvidenceRef:
    return EvidenceRef(kind="memory_candidate", id=candidate_id)


def _candidate_entry_id(candidate_id: str) -> str:
    return "candidate-" + hashlib.sha256(candidate_id.encode()).hexdigest()[:20]


def _markdown_entry_exists(path: str, entry_id: str) -> bool:
    target = files_store.memory_path(path)
    if not target.exists():
        return False
    try:
        parsed = files_store.read_file(target)
    except (OSError, ValueError):
        return False
    return any(entry.id == entry_id for entry in parsed.entries)


def _decode_purge_plan(
    tombstone: candidate_store.PurgeTombstone,
) -> tuple[str, list[str], list[dict[str, str]], list[str]]:
    plan = tombstone.plan
    candidate_id = str(plan.get("candidate_id") or tombstone.artifact_id)
    raw_candidates = plan.get("candidates")
    candidates = (
        [str(value) for value in raw_candidates]
        if isinstance(raw_candidates, list)
        else [candidate_id]
    )
    if candidate_id not in candidates:
        candidates.append(candidate_id)

    entries: list[dict[str, str]] = []
    raw_entries = plan.get("entries")
    if isinstance(raw_entries, list):
        for value in raw_entries:
            if not isinstance(value, dict):
                continue
            entry_id = str(value.get("id") or "")
            path = str(value.get("path") or "")
            if entry_id and path:
                entries.append({"id": entry_id, "path": path})
    else:
        # Backward-compatible replay for tombstones created by early Stage 1 builds.
        entry_id = str(plan.get("entry_id") or "")
        path = str(plan.get("target_path") or "")
        if entry_id and path:
            entries.append({"id": entry_id, "path": path})

    raw_wrap_ids = plan.get("wrap_ids")
    wrap_ids = (
        [str(value) for value in raw_wrap_ids]
        if isinstance(raw_wrap_ids, list)
        else []
    )
    return (
        candidate_id,
        sorted(set(candidates)),
        sorted(entries, key=lambda item: (item["path"], item["id"])),
        sorted(set(wrap_ids)),
    )


def _normalize_target_path(path: str) -> str:
    normalized = path.strip()
    if not normalized.endswith(".md"):
        normalized += ".md"
    files_store.validate_prefix(normalized)
    files_store.memory_path(normalized)
    return normalized


def _normalize_content(content: str) -> str:
    normalized = "\n".join(line.rstrip() for line in content.strip().splitlines()).strip()
    if not normalized:
        raise ValueError("candidate content is required")
    if len(normalized) > 20_000:
        raise ValueError("candidate content exceeds 20,000 characters")
    if files_store.PROVENANCE_MARKER_RE.search(normalized):
        raise ValueError("candidate content contains the reserved oc-provenance marker")
    if files_store.ENTRY_HEADING_RE.search(normalized):
        raise ValueError("candidate content contains a reserved canonical entry heading")
    return normalized


def _normalize_tags(tags: list[str]) -> list[str]:
    result: list[str] = []
    for raw in tags:
        tag = raw.strip().removeprefix("#").casefold()
        if not tag or any(char.isspace() for char in tag):
            raise ValueError(f"invalid memory tag: {raw!r}")
        if tag not in result:
            result.append(tag)
    return result


def _candidate_idempotency_key(
    *,
    kind: str,
    target_path: str,
    content_hash: str,
    tags: list[str],
    evidence: list[EvidenceRef],
) -> str:
    source_keys = sorted(
        f"{ref.kind}\0{ref.path}\0{ref.id}\0{ref.content_hash}"
        for ref in evidence
    )
    payload = "\0".join(
        ["memory-candidate-v1", kind, target_path, content_hash, *sorted(tags), *source_keys]
    )
    return hashlib.sha256(payload.encode()).hexdigest()
