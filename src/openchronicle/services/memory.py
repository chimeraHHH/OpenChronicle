"""Review-first durable-memory operations."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

import frontmatter

from ..capture import store_lock as capture_store
from ..config import Config
from ..logger import get
from ..memory_candidates import store as candidate_store
from ..memory_candidates.store import MemoryCandidate
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, content_digest
from ..store import entries as entries_store
from ..store import files as files_store
from .context import ContextService

logger = get("openchronicle.memory")


@dataclass(frozen=True, slots=True)
class PurgeResult:
    candidate_id: str
    removed_entry: bool
    removed_files: tuple[str, ...]
    invalidated_wraps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PurgePreview:
    """Canonical, content-free deletion closure for an explicit review action."""

    candidate_id: str
    expected_version: int
    candidate_ids: tuple[str, ...]
    entries: tuple[dict[str, str], ...]
    files: tuple[dict[str, str], ...]
    wrap_ids: tuple[str, ...]
    plan_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "expected_version": self.expected_version,
            "candidate_ids": list(self.candidate_ids),
            "entries": [dict(entry) for entry in self.entries],
            "files": [dict(file) for file in self.files],
            "wrap_ids": list(self.wrap_ids),
            "counts": {
                "candidates": len(self.candidate_ids),
                "memory_entries": len(self.entries),
                "memory_files": len(self.files),
                "daily_wraps": len(self.wrap_ids),
            },
            "plan_digest": self.plan_digest,
        }


class StalePurgePlan(RuntimeError):
    """The deletion closure changed after the user reviewed its preview."""


class PurgeClosureUnverifiable(RuntimeError):
    """A damaged provenance frame prevents safe deletion-closure discovery."""


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

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        soft_limit_tokens: int | None = None,
        cfg: Config | None = None,
    ):
        self.conn = conn
        self.soft_limit_tokens = soft_limit_tokens
        # Trusted product/CLI adapters always pass their freshly loaded config
        # so approval is fenced by the current privacy policy. ``None`` keeps
        # the lower-level service usable for isolated local migration/tests
        # that do not perform a product-facing approval.
        self.cfg = cfg

    def propose_candidate(
        self,
        *,
        kind: str,
        operation: str = "append",
        target_path: str,
        target_entry_id: str = "",
        content: str,
        tags: list[str],
        evidence: list[EvidenceRef],
        claim_evidence: list[EvidenceRef] | None = None,
        confidence: float | None = None,
        conflict_key: str = "",
        producer_run_key: str = "",
        proposal_slot: int = 0,
        transaction_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> MemoryCandidate:
        # The transaction guard revalidates capture-derived evidence. Acquire
        # capture before BEGIN IMMEDIATE so capture persistence (capture→DB)
        # cannot deadlock against a proposal holding DB→capture.
        with _review_operation_lock(), capture_store.capture_store_lock():
            return self._propose_candidate_locked(
                kind=kind,
                operation=operation,
                target_path=target_path,
                target_entry_id=target_entry_id,
                content=content,
                tags=tags,
                evidence=evidence,
                claim_evidence=claim_evidence,
                confidence=confidence,
                conflict_key=conflict_key,
                producer_run_key=producer_run_key,
                proposal_slot=proposal_slot,
                transaction_guard=transaction_guard,
            )

    def _propose_candidate_locked(
        self,
        *,
        kind: str,
        operation: str,
        target_path: str,
        target_entry_id: str,
        content: str,
        tags: list[str],
        evidence: list[EvidenceRef],
        claim_evidence: list[EvidenceRef] | None,
        confidence: float | None,
        conflict_key: str,
        producer_run_key: str,
        proposal_slot: int,
        transaction_guard: Callable[[sqlite3.Connection], None] | None,
    ) -> MemoryCandidate:
        target_path = _normalize_target_path(target_path)
        normalized_content = _normalize_content(content)
        clean_tags = _normalize_tags(tags)
        clean_kind = kind.strip().lower()
        if not clean_kind:
            raise ValueError("candidate kind is required")
        clean_operation = operation.strip().lower()
        if clean_operation not in {"append", "supersede"}:
            raise ValueError("candidate operation must be append or supersede")
        clean_target_entry_id = target_entry_id.strip()
        if clean_operation == "append" and clean_target_entry_id:
            raise ValueError("append candidates cannot target an existing entry")
        if clean_operation == "supersede" and not clean_target_entry_id:
            raise ValueError("supersede candidates require target_entry_id")
        if not evidence:
            raise ValueError("memory candidates require at least one evidence reference")
        if any(not ref.content_hash for ref in evidence):
            raise ValueError("memory candidate evidence must bind a source content hash")
        claims = _unique_evidence_refs(
            evidence if claim_evidence is None else claim_evidence
        )
        if not claims:
            raise ValueError("memory candidates require at least one claim-support source")
        if any(claim not in evidence for claim in claims):
            raise ValueError("claim-support sources must be part of the input evidence closure")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        conflict_key = conflict_key.strip().casefold()
        digest = content_digest(normalized_content)
        target_entry_hash = ""
        if clean_operation == "supersede":
            target = _current_supersede_target(target_path, clean_target_entry_id)
            target_entry_hash = content_digest(target.body)
            if any(
                source.kind == "memory_entry"
                and source.path == target_path
                and source.id == clean_target_entry_id
                for source in evidence
            ):
                raise ValueError(
                    "supersede target is a revision precondition, not proposal evidence"
                )
        proposal_digest = candidate_store.proposal_digest(
            kind=clean_kind,
            operation=clean_operation,
            target_path=target_path,
            target_entry_id=clean_target_entry_id,
            target_entry_hash=target_entry_hash,
            content_hash=digest,
            tags=clean_tags,
            evidence=evidence,
            claim_evidence=claims,
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
            if transaction_guard is not None:
                transaction_guard(self.conn)
            if candidate_store.is_tombstoned(
                self.conn, kind="memory_file", artifact_id=target_path
            ):
                raise ValueError("candidate target is pending permanent purge")
            invalid_sources = [
                source for source in evidence if not provenance_store.is_current(self.conn, source)
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
                operation=clean_operation,
                target_path=target_path,
                target_entry_id=clean_target_entry_id,
                target_entry_hash=target_entry_hash,
                content=normalized_content,
                content_hash=digest,
                claim_evidence=claims,
                tags=clean_tags,
                confidence=confidence,
                conflict_key=conflict_key,
                status=status,
            )
            existing_sources = provenance_store.direct_sources(
                self.conn, _candidate_ref(candidate.id)
            )
            if created or (candidate.proposal_digest == proposal_digest and not existing_sources):
                provenance_store.replace_sources(
                    self.conn,
                    subject=_candidate_ref(candidate.id),
                    sources=evidence,
                )
            elif (
                candidate.proposal_digest != proposal_digest
                or existing_sources != evidence
                or bool(candidate.claim_evidence)
                and candidate.claim_evidence != claims
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
                current.conflict_key if conflict_key is None else conflict_key.strip().casefold()
            )
            digest = content_digest(normalized_content)
            sources = provenance_store.direct_sources(
                self.conn,
                _candidate_ref(candidate_id),
            )
            if not candidate_store.proposal_is_current(current, sources):
                raise candidate_store.CandidateConflict(
                    "candidate evidence binding changed"
                )
            next_proposal_digest = candidate_store.proposal_digest(
                kind=current.kind,
                operation=current.operation,
                target_path=current.target_path,
                target_entry_id=current.target_entry_id,
                target_entry_hash=current.target_entry_hash,
                content_hash=digest,
                tags=clean_tags,
                evidence=sources,
                claim_evidence=current.claim_evidence or None,
            )
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
                    proposal_digest=next_proposal_digest,
                    status="conflict" if has_conflict else "pending",
                )
                self.conn.execute("COMMIT")
                return updated
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    def approve_candidate(self, candidate_id: str, *, expected_version: int) -> MemoryCandidate:
        if self.cfg is None:
            raise RuntimeError(
                "candidate approval requires the current privacy configuration"
            )
        with _review_operation_lock():
            return self._approve_candidate_locked(candidate_id, expected_version=expected_version)

    def _approve_candidate_locked(
        self, candidate_id: str, *, expected_version: int
    ) -> MemoryCandidate:
        current = self._required(candidate_id)
        if current.status == "accepted":
            pass
        elif current.status == "applying":
            if expected_version not in {current.version, current.version - 1}:
                raise candidate_store.CandidateConflict("candidate version changed")
        else:
            if current.version != expected_version:
                raise candidate_store.CandidateConflict("candidate version changed")
            if current.status != "pending":
                raise candidate_store.CandidateConflict("candidate must be pending before approval")

        if (
            current.operation == "supersede"
            and current.status != "accepted"
            and not _markdown_entry_exists(
                current.target_path,
                _candidate_entry_id(candidate_id),
            )
        ):
            try:
                _require_current_supersede_target(current)
            except (FileNotFoundError, ValueError) as exc:
                detail = f"supersede target changed: {exc}"
                candidate_store.transition(
                    self.conn,
                    candidate_id=candidate_id,
                    expected_version=current.version,
                    from_statuses=(current.status,),
                    to_status="conflict",
                    error=detail,
                )
                raise candidate_store.CandidateConflict(detail) from exc

        sources = provenance_store.direct_sources(
            self.conn, _candidate_ref(candidate_id)
        )
        invalid_sources = [
            source for source in sources if not provenance_store.is_current(self.conn, source)
        ]
        binding_changed = (
            not candidate_store.projection_is_current(current)
            or not candidate_store.proposal_is_current(current, sources)
        )
        assert self.cfg is not None
        policy_denied = bool(
            sources
            and not ContextService(self.conn, self.cfg).evidence_allowed(
                _candidate_ref(candidate_id), embedded_sources=sources
            )
        )
        if not sources or invalid_sources or binding_changed or policy_denied:
            detail = (
                "candidate has no durable evidence"
                if not sources
                else (
                    "candidate evidence is excluded by the current privacy policy"
                    if policy_denied
                    else "candidate evidence is missing, changed, or rebound"
                )
            )
            if current.status == "accepted":
                raise candidate_store.CandidateConflict(detail)
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
        if current.status == "accepted":
            return current

        if current.status == "applying":
            applying = current
        else:
            applying = candidate_store.transition(
                self.conn,
                candidate_id=candidate_id,
                expected_version=expected_version,
                from_statuses=("pending",),
                to_status="applying",
            )
        entry_id = _candidate_entry_id(candidate_id)
        entry_sources = [_candidate_ref(candidate_id), *sources]
        try:
            # Cleanup and capture writes use the same lock. Re-read both the
            # hashes and current policy inside that fence immediately before
            # publishing the reviewed text to durable memory.
            with capture_store.capture_store_lock():
                publish_sources = provenance_store.direct_sources(
                    self.conn, _candidate_ref(candidate_id)
                )
                publish_candidate = self._required(candidate_id)
                publish_denied = (
                    publish_sources != sources
                    or publish_candidate.status != "applying"
                    or publish_candidate.version != applying.version
                    or not candidate_store.projection_is_current(publish_candidate)
                    or not candidate_store.proposal_is_current(
                        publish_candidate,
                        publish_sources,
                    )
                    or any(
                        not provenance_store.is_current(self.conn, source)
                        for source in publish_sources
                    )
                    or not ContextService(
                            self.conn, self.cfg
                        ).evidence_allowed(
                            _candidate_ref(candidate_id),
                            embedded_sources=publish_sources,
                        )
                )
                if publish_denied:
                    latest = self._required(candidate_id)
                    if latest.status == "applying":
                        candidate_store.transition(
                            self.conn,
                            candidate_id=candidate_id,
                            expected_version=latest.version,
                            from_statuses=("applying",),
                            to_status="conflict",
                            error=(
                                "candidate evidence changed or was excluded "
                                "before publication"
                            ),
                        )
                    raise candidate_store.CandidateConflict(
                        "candidate evidence changed or was excluded before publication"
                    )
                if (
                    applying.operation == "supersede"
                    and not _markdown_entry_exists(applying.target_path, entry_id)
                ):
                    try:
                        _require_current_supersede_target(applying)
                    except (FileNotFoundError, ValueError) as exc:
                        latest = self._required(candidate_id)
                        if latest.status == "applying":
                            candidate_store.transition(
                                self.conn,
                                candidate_id=candidate_id,
                                expected_version=latest.version,
                                from_statuses=("applying",),
                                to_status="conflict",
                                error=f"supersede target changed: {exc}",
                            )
                        raise candidate_store.CandidateConflict(
                            f"supersede target changed: {exc}"
                        ) from exc
                if not files_store.memory_path(applying.target_path).exists():
                    with contextlib.suppress(FileExistsError):
                        entries_store.create_file(
                            self.conn,
                            name=applying.target_path,
                            description=f"Reviewed {applying.kind} memories.",
                            tags=applying.tags,
                            owner_candidate_id=applying.id,
                        )
                if applying.operation == "supersede":
                    entries_store.supersede_entry(
                        self.conn,
                        name=applying.target_path,
                        old_entry_id=applying.target_entry_id,
                        new_content=applying.content,
                        reason=f"reviewed candidate {candidate_id}",
                        tags=applying.tags,
                        new_entry_id=entry_id,
                        additional_evidence_refs=entry_sources,
                    )
                else:
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

    def preview_purge_candidate(self, candidate_id: str, *, expected_version: int) -> PurgePreview:
        """Return the exact deletion closure without authorizing deletion."""
        with _review_operation_lock():
            root = self._required(candidate_id)
            if root.version != expected_version:
                raise candidate_store.CandidateConflict("candidate version changed")
            plan = self._build_purge_plan(root)
            return _purge_preview(plan)

    def purge_candidate(
        self,
        candidate_id: str,
        *,
        expected_version: int | None = None,
        expected_plan_digest: str | None = None,
    ) -> PurgeResult:
        """Crash-resumably forget a proposal and its accepted derivatives.

        Trusted review UIs should supply both compare-and-swap values from
        :meth:`preview_purge_candidate`.  The optional form preserves the
        established local CLI recovery path.
        """
        with _review_operation_lock():
            return self._purge_candidate_locked(
                candidate_id,
                expected_version=expected_version,
                expected_plan_digest=expected_plan_digest,
            )

    def _purge_candidate_locked(
        self,
        candidate_id: str,
        *,
        expected_version: int | None,
        expected_plan_digest: str | None,
    ) -> PurgeResult:
        tombstones = [
            tombstone
            for tombstone in candidate_store.list_tombstones(self.conn, kind="memory_candidate")
            if tombstone.artifact_id == candidate_id
        ]
        if tombstones:
            plan = tombstones[0].plan
            if expected_version is not None and int(plan.get("root_version", -1)) != (
                expected_version
            ):
                raise candidate_store.CandidateConflict("candidate version changed")
            if expected_plan_digest is not None and _purge_plan_digest(plan) != (
                expected_plan_digest
            ):
                raise StalePurgePlan("purge closure changed after preview")
            return self._execute_purge(tombstones[0])

        root = self._required(candidate_id)
        if expected_version is not None and root.version != expected_version:
            raise candidate_store.CandidateConflict("candidate version changed")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # The closure snapshot and every deny-read tombstone commit as one
            # write transaction. Candidate proposals and wrap publication also
            # use BEGIN IMMEDIATE, so no new dependent can land in between.
            root = self._required(candidate_id)
            if expected_version is not None and root.version != expected_version:
                raise candidate_store.CandidateConflict("candidate version changed")
            plan = self._build_purge_plan(root)
            if expected_plan_digest is not None and _purge_plan_digest(plan) != (
                expected_plan_digest
            ):
                raise StalePurgePlan("purge closure changed after preview")
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
            for file in plan["files"]:
                assert isinstance(file, dict)
                path = str(file["path"])
                candidate_store.put_tombstone(
                    self.conn,
                    kind="memory_file",
                    artifact_id=path,
                )
                # Some legacy/internal readers query ``files`` directly.  The
                # deny-read tombstone protects canonical Markdown and rebuilds;
                # deleting the projection in the same transaction also keeps
                # candidate-derived descriptions/tags out of those readers if
                # the process crashes before the physical unlink.
                self.conn.execute("DELETE FROM files WHERE path=?", (path,))
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
            for tombstone in candidate_store.list_tombstones(self.conn, kind="memory_candidate")
            if tombstone.artifact_id == candidate_id
        )
        return self._execute_purge(tombstone)

    def resume_pending_purges(self) -> list[PurgeResult]:
        with _review_operation_lock():
            results: list[PurgeResult] = []
            for tombstone in candidate_store.list_tombstones(self.conn, kind="memory_candidate"):
                results.append(self._execute_purge(tombstone))
            return results

    def _execute_purge(self, tombstone: candidate_store.PurgeTombstone) -> PurgeResult:
        candidate_id, candidates, entries, files, wrap_ids = _decode_purge_plan(tombstone)
        removed_entry = False
        removed_files: list[str] = []
        try:
            from ..daily_wrap import store as daily_wrap_store

            # Validate every planned Markdown path before deleting any wrap or
            # entry. Candidate-owned containers receive the stronger ownership
            # check below; shared/pre-existing files must still fail closed on
            # symlink or non-regular replacement. delete_entry repeats this
            # check while holding the actual rewrite locks.
            for path in sorted({entry["path"] for entry in entries}):
                entries_store.require_regular_purge_entry_path(self.conn, name=path)

            # Revalidate the exact regular files before any Markdown rewrite.
            # Cooperative writers are fenced by the review lock; this also
            # fails closed if an external editor replaced a reviewed path in
            # the gap after the purge intent committed.
            for file in files:
                planned_entry_ids = {
                    entry["id"] for entry in entries if entry["path"] == file["path"]
                }
                entries_store.require_candidate_owned_file(
                    self.conn,
                    name=file["path"],
                    owner_candidate_id=file["owner_candidate_id"],
                    template_digest=file["template_digest"],
                    planned_entry_ids=planned_entry_ids,
                )
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
            for file in files:
                if entries_store.finalize_candidate_owned_file(
                    self.conn,
                    name=file["path"],
                    owner_candidate_id=file["owner_candidate_id"],
                    template_digest=file["template_digest"],
                    delete_if_empty=file["action"] == "delete",
                ):
                    removed_files.append(file["path"])
            for purged_candidate_id in candidates:
                candidate_ref = _candidate_ref(purged_candidate_id)
                provenance_store.delete_subject(self.conn, candidate_ref)
                provenance_store.delete_source_edges(self.conn, candidate_ref)
                candidate_store.delete(self.conn, purged_candidate_id)

            self._verify_purge(
                candidates,
                entries,
                files,
                removed_files,
                wrap_ids,
            )
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
                for file in files:
                    candidate_store.delete_tombstone(
                        self.conn,
                        kind="memory_file",
                        artifact_id=file["path"],
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
            removed_files=tuple(sorted(set(removed_files))),
            invalidated_wraps=tuple(sorted(set(wrap_ids))),
        )

    def _build_purge_plan(self, root: MemoryCandidate) -> dict[str, object]:
        candidates: dict[str, MemoryCandidate] = {root.id: root}
        entries: dict[tuple[str, str], EvidenceRef] = {}
        wrap_ids: set[str] = set()
        queue: list[EvidenceRef] = [_candidate_ref(root.id)]
        markdown_dependents: dict[tuple[str, str, str], list[EvidenceRef]] = {}

        # The SQLite edge graph is a projection written after the atomic
        # Markdown rename. Scan embedded frames as a second source of truth so
        # an append that crashed in that narrow gap still enters the deletion
        # closure instead of leaving a plaintext orphan on disk.
        for path in files_store.list_memory_files():
            if candidate_store.is_tombstoned(self.conn, kind="memory_file", artifact_id=path.name):
                continue
            parsed = files_store.read_file(path)
            for entry in parsed.entries:
                if not entry.provenance_valid:
                    raise PurgeClosureUnverifiable(
                        "cannot establish purge closure: invalid provenance frame "
                        f"in {path.name}#{entry.id}"
                    )
                dependent = EvidenceRef(kind="memory_entry", id=entry.id, path=path.name)
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
                        source.kind == "memory_candidate" and source.id == candidate.id
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

        planned_entry_keys = set(entries)
        purge_files: list[dict[str, str]] = []
        for target_path in sorted({candidate.target_path for candidate in candidates.values()}):
            record = _candidate_owned_file_purge_record(
                target_path,
                candidates=candidates,
                planned_entry_keys=planned_entry_keys,
            )
            if record is not None:
                purge_files.append(record)

        return {
            "candidate_id": root.id,
            "root_version": root.version,
            "candidates": sorted(candidates),
            "entries": [
                {"id": ref.id, "path": ref.path}
                for ref in sorted(entries.values(), key=lambda item: (item.path, item.id))
            ],
            "files": purge_files,
            "wrap_ids": sorted(wrap_ids),
        }

    def _verify_purge(
        self,
        candidates: list[str],
        entries: list[dict[str, str]],
        files: list[dict[str, str]],
        removed_files: list[str],
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
        removed_file_set = set(removed_files)
        for file in files:
            path = files_store.memory_path(file["path"])
            indexed = self.conn.execute(
                "SELECT description, tags, entry_count FROM files WHERE path=? LIMIT 1",
                (file["path"],),
            ).fetchone()
            if file["path"] in removed_file_set:
                if path.exists() or path.is_symlink():
                    raise RuntimeError("candidate-owned memory file survived purge")
                if indexed is not None:
                    raise RuntimeError("candidate-owned memory file projection survived purge")
                continue

            if not path.is_file() or path.is_symlink():
                raise RuntimeError("sanitized candidate-owned memory file is missing")
            parsed = files_store.read_file(path)
            surviving_tags = sorted({tag for entry in parsed.entries for tag in entry.tags})
            if not files_store.candidate_file_is_sanitized(
                parsed.raw_frontmatter,
                surviving_tags=surviving_tags,
                entry_count=len(parsed.entries),
            ):
                raise RuntimeError("candidate-owned memory file metadata survived purge")
            if (
                indexed is None
                or indexed["description"] != files_store.SANITIZED_CANDIDATE_FILE_DESCRIPTION
                or (indexed["tags"] or "").split() != surviving_tags
                or int(indexed["entry_count"] or 0) != len(parsed.entries)
            ):
                raise RuntimeError("sanitized memory file projection is inconsistent")
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


def _unique_evidence_refs(refs: list[EvidenceRef]) -> list[EvidenceRef]:
    result: list[EvidenceRef] = []
    for ref in refs:
        if ref not in result:
            result.append(ref)
    return result


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


def _current_supersede_target(
    target_path: str,
    target_entry_id: str,
) -> files_store.ParsedEntry:
    path = files_store.memory_path(target_path)
    if not path.exists():
        raise FileNotFoundError(path.name)
    parsed = files_store.read_file(path)
    target = next(
        (entry for entry in parsed.entries if entry.id == target_entry_id),
        None,
    )
    if target is None:
        raise ValueError(f"entry {target_entry_id} not found in {path.name}")
    if not target.provenance_valid:
        raise ValueError(f"entry {target_entry_id} has an invalid provenance frame")
    if target.superseded_by:
        raise ValueError(
            f"entry {target_entry_id} is already superseded by {target.superseded_by}"
        )
    return target


def _require_current_supersede_target(candidate: MemoryCandidate) -> None:
    if candidate.operation != "supersede":
        return
    target = _current_supersede_target(
        candidate.target_path,
        candidate.target_entry_id,
    )
    if content_digest(target.body) != candidate.target_entry_hash:
        raise ValueError(f"entry {candidate.target_entry_id} content changed")


def _candidate_owned_file_purge_record(
    path_name: str,
    *,
    candidates: dict[str, MemoryCandidate],
    planned_entry_keys: set[tuple[str, str]],
) -> dict[str, str] | None:
    """Return a content-free deletion record for one safely owned file.

    Ownership alone is insufficient: an unrelated canonical entry or freeform
    body means a user or another workflow has adopted the container.  Such a
    file is preserved and only the explicitly planned entries are removed.
    """
    path = files_store.memory_path(path_name)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        post = frontmatter.load(path)
        fm = dict(post.metadata)
        owner_candidate_id = files_store.candidate_file_owner(fm, path_name=path.name)
        entries = files_store._parse_entries(post.content)
    except Exception as exc:  # fail closed on a user replacement/malformed file
        logger.warning("preserving changed candidate-owned file %s: %s", path.name, exc)
        return None
    owner = candidates.get(owner_candidate_id or "")
    if owner is None or owner.target_path != path.name:
        return None
    matches = list(files_store.ENTRY_HEADING_RE.finditer(post.content))
    freeform_prefix = post.content[: matches[0].start()] if matches else post.content
    has_surviving_content = bool(freeform_prefix.strip()) or any(
        (path.name, entry.id) not in planned_entry_keys for entry in entries
    )

    template_digest = fm.get(files_store.CANDIDATE_FILE_TEMPLATE_DIGEST_KEY)
    assert isinstance(template_digest, str)
    return {
        "path": path.name,
        "owner_candidate_id": owner.id,
        "template_digest": template_digest,
        "action": "sanitize" if has_surviving_content else "delete",
    }


def _decode_purge_plan(
    tombstone: candidate_store.PurgeTombstone,
) -> tuple[
    str,
    list[str],
    list[dict[str, str]],
    list[dict[str, str]],
    list[str],
]:
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

    files: list[dict[str, str]] = []
    raw_files = plan.get("files")
    if isinstance(raw_files, list):
        for value in raw_files:
            if not isinstance(value, dict):
                continue
            path = str(value.get("path") or "")
            owner_candidate_id = str(value.get("owner_candidate_id") or "")
            template_digest = str(value.get("template_digest") or "")
            action = str(value.get("action") or "delete")
            if path and owner_candidate_id and template_digest and action in {"delete", "sanitize"}:
                files.append(
                    {
                        "path": path,
                        "owner_candidate_id": owner_candidate_id,
                        "template_digest": template_digest,
                        "action": action,
                    }
                )

    raw_wrap_ids = plan.get("wrap_ids")
    wrap_ids = [str(value) for value in raw_wrap_ids] if isinstance(raw_wrap_ids, list) else []
    return (
        candidate_id,
        sorted(set(candidates)),
        sorted(entries, key=lambda item: (item["path"], item["id"])),
        sorted(files, key=lambda item: item["path"]),
        sorted(set(wrap_ids)),
    )


def _purge_plan_digest(plan: dict[str, object]) -> str:
    """Hash the canonical closure reviewed by a trusted local UI."""
    canonical = {
        "candidate_id": str(plan.get("candidate_id") or ""),
        "root_version": int(plan.get("root_version") or 0),
        "candidates": sorted(str(value) for value in plan.get("candidates", [])),
        "entries": sorted(
            (
                {
                    "id": str(value.get("id") or ""),
                    "path": str(value.get("path") or ""),
                }
                for value in plan.get("entries", [])
                if isinstance(value, dict)
            ),
            key=lambda value: (value["path"], value["id"]),
        ),
        "files": sorted(
            (
                {
                    "path": str(value.get("path") or ""),
                    "owner_candidate_id": str(value.get("owner_candidate_id") or ""),
                    "template_digest": str(value.get("template_digest") or ""),
                    "action": str(value.get("action") or "delete"),
                }
                for value in plan.get("files", [])
                if isinstance(value, dict)
            ),
            key=lambda value: value["path"],
        ),
        "wrap_ids": sorted(str(value) for value in plan.get("wrap_ids", [])),
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _purge_preview(plan: dict[str, object]) -> PurgePreview:
    candidate_id, candidates, entries, files, wrap_ids = _decode_purge_plan(
        candidate_store.PurgeTombstone(
            kind="memory_candidate",
            artifact_id=str(plan.get("candidate_id") or ""),
            path="",
            plan=plan,
            requested_at="",
            last_error="",
        )
    )
    return PurgePreview(
        candidate_id=candidate_id,
        expected_version=int(plan.get("root_version") or 0),
        candidate_ids=tuple(candidates),
        entries=tuple(entries),
        files=tuple({"path": file["path"]} for file in files),
        wrap_ids=tuple(wrap_ids),
        plan_digest=_purge_plan_digest(plan),
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
