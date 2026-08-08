"""Day-scoped, policy-aware context retrieval for local product features."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import paths
from ..config import Config
from ..memory_candidates import store as candidate_store
from ..privacy import policy as privacy_policy
from ..privacy.egress import privacy_egress_fenced
from ..provenance import store as provenance_store
from ..provenance.models import (
    EvidenceRef,
    canonical_digest,
    content_digest,
    daily_wrap_sources_digest,
    observation_digest,
    timeline_block_digest,
)
from ..store import entries as entries_store
from ..store import files as files_store
from ..timeline import store as timeline_store

_MAX_RECORD_CHARS = 2_000
_MAX_PROMPT_RECORDS = 400
_MAX_PROMPT_BYTES = 175_000
_MAX_COVERAGE_GAPS = 64
_MAX_COVERAGE_GAP_CHARS = 160
_COUNTED_GAP_KINDS = {
    "invalid_timeline_timestamp",
    "invalid_timeline_block",
    "excluded_timeline_block",
    "purging_event_entry",
    "purging_memory_file",
    "invalid_event_provenance",
    "stale_event_provenance",
    "excluded_or_unverifiable_event_entry",
    "invalid_session",
    "open_session",
    "session_not_reduced",
}


@dataclass(frozen=True, slots=True)
class ContextRecord:
    evidence: EvidenceRef
    start_time: str
    end_time: str
    text: str
    metadata: dict[str, object] = field(default_factory=dict)

    def prompt_dict(self) -> dict[str, object]:
        """Minimal remote payload; excludes screenshots and full AX trees."""
        return {
            "evidence_token": self.evidence.key,
            "kind": self.evidence.kind,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "text": self.text[:_MAX_RECORD_CHARS],
            "metadata": _bounded_metadata(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DayContext:
    local_date: date
    timezone: str
    window_start_utc: datetime
    window_end_utc: datetime
    records: tuple[ContextRecord, ...]
    coverage_status: str
    coverage_gaps: tuple[str, ...]
    policy_digest: str

    def input_digest(self, *, workflow_version: int) -> str:
        material: list[str] = [
            "daily-wrap-input-v1",
            str(workflow_version),
            self.local_date.isoformat(),
            self.timezone,
            self.window_start_utc.isoformat(),
            self.window_end_utc.isoformat(),
            self.coverage_status,
            self.policy_digest,
            *sorted(self.coverage_gaps),
        ]
        for record in sorted(
            self.records,
            key=lambda item: (
                item.start_time,
                item.evidence.kind,
                item.evidence.path,
                item.evidence.id,
            ),
        ):
            material.append(
                json.dumps(
                    record.prompt_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        return hashlib.sha256("\0".join(material).encode()).hexdigest()


class ContextService:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg

    @privacy_egress_fenced
    def for_day(self, local_date: date, timezone: str) -> DayContext:
        zone = _zone(timezone)
        local_start = datetime.combine(local_date, time.min, zone)
        local_end = datetime.combine(local_date + timedelta(days=1), time.min, zone)
        start_utc = local_start.astimezone(UTC)
        end_utc = local_end.astimezone(UTC)
        records: list[ContextRecord] = []
        gaps: list[str] = []

        invalid_timestamp = self.conn.execute(
            """
            SELECT 1 FROM timeline_blocks
             WHERE julianday(start_time) IS NULL
                OR julianday(end_time) IS NULL
             LIMIT 1
            """
        ).fetchone()
        if invalid_timestamp is not None:
            gaps.append("invalid_timeline_timestamp")
        records.extend(self._timeline_records(start_utc, end_utc, gaps))
        records.extend(self._event_records(start_utc, end_utc, zone, gaps))
        # Session rows have no raw-observation ancestry of their own. Their
        # time/status metadata therefore cannot be safely re-authorized after
        # a policy change; policy-grounded event/timeline records already
        # carry the useful session activity and are the only remote inputs.
        records.sort(
            key=lambda item: (
                item.start_time,
                item.evidence.kind,
                item.evidence.path,
                item.evidence.id,
            )
        )
        records, dropped = _bound_records(records)
        if dropped:
            gaps.append(f"remote_payload_truncated:{dropped}")

        processed = timeline_store.get_processed_range(self.conn)
        if processed is None:
            gaps.append("timeline_coverage_unknown")
        else:
            processed_from, processed_through = processed
            if _instant(processed_from) > start_utc or _instant(processed_through) < end_utc:
                gaps.append("timeline_not_covered_through_day_end")
        now = datetime.now(UTC)
        if now < end_utc:
            gaps.append("day_in_progress")
        gaps = _summarize_coverage_gaps(gaps)
        return DayContext(
            local_date=local_date,
            timezone=timezone,
            window_start_utc=start_utc,
            window_end_utc=end_utc,
            records=tuple(records),
            coverage_status="ready" if not gaps else "partial",
            coverage_gaps=tuple(gaps),
            policy_digest=_policy_digest(self.cfg),
        )

    @privacy_egress_fenced
    def evidence_allowed(
        self,
        subject: EvidenceRef,
        *,
        embedded_sources: list[EvidenceRef] | None = None,
    ) -> bool:
        """Return whether current capture policy permits a derived source."""
        return self._derived_allowed(subject, embedded_sources=embedded_sources)

    @privacy_egress_fenced
    def memory_entry_allowed(
        self,
        *,
        path: str,
        entry: files_store.ParsedEntry,
    ) -> bool:
        """Authorize one current, explicitly rooted memory entry.

        Provenance-free entries are local trust roots only when their canonical
        heading carries the reserved ``oc-origin:manual-v1`` marker. Legacy
        unmarked and automation-origin entries are quarantined. Provenance-
        bearing entries must have a valid frame and projection, live direct
        dependencies, and raw-observation branches allowed by current policy.
        """
        if candidate_store.is_tombstoned(
            self.conn, kind="memory_file", artifact_id=path
        ) or candidate_store.is_tombstoned(
            self.conn,
            kind="memory_entry",
            artifact_id=entry.id,
            path=path,
        ):
            return False
        try:
            canonical_path = files_store.memory_path(path)
            current_file = files_store.read_file(canonical_path)
        except ValueError:
            return False
        except (FileNotFoundError, OSError):
            return False
        if canonical_path.is_symlink() or not canonical_path.is_file():
            return False
        # Candidate-created path/frontmatter is derived data too. Keep the
        # container and every entry behind the same ownership/provenance gate;
        # otherwise an entry body could reveal a tainted filename after its
        # owner binding became invalid.
        if not self.memory_file_metadata_allowed(current_file):
            return False
        current_entry = next(
            (candidate for candidate in current_file.entries if candidate.id == entry.id),
            None,
        )
        if current_entry is None or current_entry != entry:
            return False
        entry = current_entry
        subject = EvidenceRef(
            kind="memory_entry",
            id=entry.id,
            path=path,
            timestamp=entry.timestamp,
            content_hash=content_digest(entry.body),
        )
        # Markdown is authoritative even when its evidence list is empty. A
        # forged/stale SQLite edge on a manual root must not become public
        # provenance metadata or a model-visible source drawer.
        if provenance_store.direct_sources(self.conn, subject) != entry.evidence_refs:
            return False
        if not entry.provenance_present:
            return bool(entry.origin_valid and entry.origin == files_store.MANUAL_ENTRY_ORIGIN)
        if (
            not entry.provenance_valid
            or not entry.evidence_refs
            or not entries_store.dependency_sources_are_live(self.conn, entry.evidence_refs)
        ):
            return False
        return self._derived_allowed(
            subject,
            embedded_sources=entry.evidence_refs,
        )

    def memory_file_metadata_allowed(self, parsed: files_store.ParsedFile) -> bool:
        """Authorize frontmatter/path metadata for candidate-created files."""
        owner_fields_present = any(
            key in parsed.raw_frontmatter
            for key in (
                files_store.CANDIDATE_FILE_OWNER_KEY,
                files_store.CANDIDATE_FILE_TEMPLATE_DIGEST_KEY,
            )
        )
        try:
            owner_id = files_store.candidate_file_owner(
                parsed.raw_frontmatter,
                path_name=parsed.path.name,
            )
        except ValueError:
            return False
        # A partial, malformed, or modified ownership frame must not turn
        # provider-derived path/frontmatter back into apparently hand-written
        # metadata.  Removing both reserved fields is the explicit local act
        # that adopts a candidate-created file as user-owned.
        if owner_fields_present and not owner_id:
            return False
        if not owner_id:
            return True
        if (
            candidate_store.is_tombstoned(self.conn, kind="memory_candidate", artifact_id=owner_id)
            or candidate_store.get(self.conn, owner_id) is None
        ):
            return False
        return self._derived_allowed(EvidenceRef(kind="memory_candidate", id=owner_id))

    @privacy_egress_fenced
    def daily_wrap_allowed(
        self,
        wrap_id: str,
        *,
        expected_row=None,
        expected_revision: int | None = None,
        expected_output_digest: str | None = None,
    ) -> bool:
        """Authorize a stored Daily Wrap under the current capture policy."""
        from ..daily_wrap import store as daily_wrap_store

        if candidate_store.is_tombstoned(self.conn, kind="daily_wrap", artifact_id=wrap_id):
            return False
        if expected_row is not None:
            expected_revision = expected_row.revision
            expected_output_digest = canonical_digest(expected_row.output)
        before = daily_wrap_store.get_by_id(self.conn, wrap_id)
        if before is None or not self._wrap_matches_expected(
            before,
            expected_revision=expected_revision,
            expected_output_digest=expected_output_digest,
        ):
            return False
        subject = EvidenceRef(kind="daily_wrap", id=wrap_id)
        sources = provenance_store.direct_sources_checked(self.conn, subject)
        if sources is None:
            return False
        binding = self._published_wrap_binding(before, sources=sources)
        if binding is None:
            return False
        allowed = (
            self._derived_allowed(subject, embedded_sources=sources)
            if sources
            else self._typed_empty_wrap_allowed(before)
        )
        after = daily_wrap_store.get_by_id(self.conn, wrap_id)
        after_sources = provenance_store.direct_sources_checked(self.conn, subject)
        if after_sources is None:
            return False
        after_allowed = bool(
            after is not None
            and (
                self._derived_allowed(subject, embedded_sources=after_sources)
                if after_sources
                else self._typed_empty_wrap_allowed(after)
            )
        )
        return bool(
            allowed
            and after_allowed
            and after is not None
            and after.revision == before.revision
            and canonical_digest(after.output) == canonical_digest(before.output)
            and after_sources == sources
            and self._published_wrap_binding(after, sources=after_sources) == binding
            and self._wrap_matches_expected(
                after,
                expected_revision=expected_revision,
                expected_output_digest=expected_output_digest,
            )
        )

    def _published_wrap_binding(
        self,
        row,
        *,
        sources: list[EvidenceRef],
    ) -> str | None:
        """Bind the mutable job projection to its immutable published revision."""
        from ..daily_wrap import store as daily_wrap_store

        if row.revision < 1 or not row.published_input_digest or not isinstance(row.output, dict):
            return None
        try:
            local_day = date.fromisoformat(row.local_date)
            zone = _zone(row.timezone)
            expected_start = datetime.combine(local_day, time.min, zone).astimezone(UTC)
            expected_end = datetime.combine(
                local_day + timedelta(days=1), time.min, zone
            ).astimezone(UTC)
        except (TypeError, ValueError):
            return None
        if (
            row.id
            != daily_wrap_store.make_id(
                row.local_date,
                row.timezone,
                row.scope,
            )
            or row.window_start_utc != expected_start.isoformat()
            or row.window_end_utc != expected_end.isoformat()
            or row.workflow_version < 1
            or row.output.get("local_date") != row.local_date
            or row.output.get("timezone") != row.timezone
        ):
            return None
        revision = self.conn.execute(
            """
            SELECT local_date, timezone, scope, window_start_utc,
                   window_end_utc, workflow_version, input_digest,
                   coverage_status, source_digest, output_json
              FROM daily_wrap_revisions
             WHERE wrap_id=? AND revision=?
            """,
            (row.id, row.revision),
        ).fetchone()
        if revision is None:
            return None
        try:
            revision_output = json.loads(revision["output_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        revision_ref = EvidenceRef(
            kind="daily_wrap_revision",
            id=f"{row.id}:r{row.revision}",
            path=row.id,
        )
        try:
            revision_sources = provenance_store.direct_sources_checked(
                self.conn,
                revision_ref,
            )
            if revision_sources is None:
                return None
            revision_source_digest = daily_wrap_sources_digest(revision_sources)
        except (TypeError, ValueError):
            return None
        if (
            revision["local_date"] != row.local_date
            or revision["timezone"] != row.timezone
            or revision["scope"] != row.scope
            or revision["window_start_utc"] != row.window_start_utc
            or revision["window_end_utc"] != row.window_end_utc
            or int(revision["workflow_version"]) != row.workflow_version
            or revision["input_digest"] != row.published_input_digest
            or not isinstance(revision["source_digest"], str)
            or not revision["source_digest"]
            or revision["source_digest"] != revision_source_digest
            or revision_output != row.output
            or row.coverage_status != revision["coverage_status"]
            or revision["coverage_status"] != row.output.get("status")
            or revision_sources != sources
        ):
            return None
        return canonical_digest(
            {
                "input_digest": revision["input_digest"],
                "identity": {
                    "local_date": revision["local_date"],
                    "timezone": revision["timezone"],
                    "scope": revision["scope"],
                    "window_start_utc": revision["window_start_utc"],
                    "window_end_utc": revision["window_end_utc"],
                    "workflow_version": int(revision["workflow_version"]),
                },
                "coverage_status": revision["coverage_status"],
                "output": revision_output,
                "sources": [source.to_dict() for source in revision_sources],
            }
        )

    @staticmethod
    def _wrap_matches_expected(
        row,
        *,
        expected_revision: int | None,
        expected_output_digest: str | None,
    ) -> bool:
        return bool(
            (expected_revision is None or row.revision == expected_revision)
            and (
                expected_output_digest is None
                or canonical_digest(row.output) == expected_output_digest
            )
        )

    def _typed_empty_wrap_allowed(self, row) -> bool:
        """Recognize the service's content-free, zero-source wrap artifact."""
        output = row.output
        if row.revision < 1 or not row.published_input_digest or not isinstance(output, dict):
            return False
        categories = ("completed", "progressed", "open", "blocked", "needs_review")
        expected_keys = {
            "schema_version",
            "local_date",
            "timezone",
            "status",
            "summary",
            "coverage_gaps",
            "generated_at",
            *categories,
        }
        if (
            set(output) != expected_keys
            or output.get("schema_version") != 1
            or output.get("local_date") != row.local_date
            or output.get("timezone") != row.timezone
            or output.get("status") != row.coverage_status
            or output.get("summary") != "No grounded activity items were available."
            or any(output.get(category) != [] for category in categories)
        ):
            return False
        gaps = output.get("coverage_gaps")
        if not isinstance(gaps, list) or len(gaps) > _MAX_COVERAGE_GAPS:
            return False
        safe_gap_kinds = {
            *_COUNTED_GAP_KINDS,
            "timeline_coverage_unknown",
            "timeline_not_covered_through_day_end",
            "day_in_progress",
            "remote_payload_truncated",
            "coverage_gap_kinds_truncated",
        }
        for gap in gaps:
            if (
                not isinstance(gap, str)
                or len(gap) > _MAX_COVERAGE_GAP_CHARS
                or gap.split(":", 1)[0] not in safe_gap_kinds
            ):
                return False
        try:
            datetime.fromisoformat(str(output.get("generated_at") or ""))
        except ValueError:
            return False
        return True

    def _timeline_records(
        self, start_utc: datetime, end_utc: datetime, gaps: list[str]
    ) -> list[ContextRecord]:
        # Keep the final publication-time context rebuild bounded to a broad
        # day band. Exact intersection still happens on verified datetimes in
        # Python because ISO offsets do not sort chronologically.
        rows = self.conn.execute(
            """
            SELECT id FROM timeline_blocks
             WHERE julianday(start_time) > julianday(?) - 2
               AND julianday(start_time) < julianday(?) + 2
               AND julianday(end_time) > julianday(?) - 2
            """,
            (
                start_utc.isoformat(),
                end_utc.isoformat(),
                start_utc.isoformat(),
            ),
        ).fetchall()
        excluded_names = {name.casefold() for name in self.cfg.capture.excluded_app_names}
        records: list[ContextRecord] = []
        for row in rows:
            block_id = row["id"]
            if not isinstance(block_id, str):
                gaps.append("invalid_timeline_block:unknown")
                continue
            block = timeline_store.get_by_id(self.conn, block_id)
            if block is None:
                gaps.append(f"invalid_timeline_block:{block_id}")
                continue
            if not _intersects(
                block.start_time,
                block.end_time,
                start_utc,
                end_utc,
            ):
                continue
            clean_apps = list(block.apps_used)
            if any(
                app.casefold() in excluded_names for app in clean_apps
            ) or not self._derived_allowed(EvidenceRef(kind="timeline_block", id=block.id)):
                gaps.append(f"excluded_timeline_block:{block.id}")
                continue
            clean_entries = [entry.strip() for entry in block.entries if entry.strip()]
            text = "\n".join(clean_entries)[:_MAX_RECORD_CHARS]
            records.append(
                ContextRecord(
                    evidence=EvidenceRef(
                        kind="timeline_block",
                        id=block.id,
                        timestamp=block.start_time.isoformat(),
                        content_hash=timeline_block_digest(
                            start=block.start_time.isoformat(),
                            end=block.end_time.isoformat(),
                            entries=block.entries,
                            apps=clean_apps,
                        ),
                    ),
                    start_time=block.start_time.isoformat(),
                    end_time=block.end_time.isoformat(),
                    text=text,
                    metadata={
                        "apps": clean_apps,
                        "capture_count": block.capture_count,
                    },
                )
            )
        return records

    def _event_records(
        self,
        start_utc: datetime,
        end_utc: datetime,
        zone: ZoneInfo,
        gaps: list[str],
    ) -> list[ContextRecord]:
        records: list[ContextRecord] = []
        for path in files_store.list_memory_files():
            if not path.name.startswith("event-"):
                continue
            if candidate_store.is_tombstoned(self.conn, kind="memory_file", artifact_id=path.name):
                gaps.append(f"purging_memory_file:{path.name}")
                continue
            parsed = files_store.read_file(path)
            for entry in parsed.entries:
                if candidate_store.is_tombstoned(
                    self.conn,
                    kind="memory_entry",
                    artifact_id=entry.id,
                    path=path.name,
                ):
                    gaps.append(f"purging_event_entry:{path.name}#{entry.id}")
                    continue
                if not entry.provenance_valid:
                    gaps.append(f"invalid_event_provenance:{path.name}#{entry.id}")
                    continue
                if not entries_store.dependency_sources_are_live(self.conn, entry.evidence_refs):
                    gaps.append(f"stale_event_provenance:{path.name}#{entry.id}")
                    continue
                timestamp = _parse_local_timestamp(entry.timestamp, zone)
                if timestamp is None:
                    continue
                instant = timestamp.astimezone(UTC)
                if not start_utc <= instant < end_utc:
                    continue
                entry_ref = EvidenceRef(kind="memory_entry", id=entry.id, path=path.name)
                if not self._derived_allowed(entry_ref, embedded_sources=entry.evidence_refs):
                    gaps.append(f"excluded_or_unverifiable_event_entry:{path.name}#{entry.id}")
                    continue
                records.append(
                    ContextRecord(
                        evidence=EvidenceRef(
                            kind="memory_entry",
                            id=entry.id,
                            path=path.name,
                            timestamp=timestamp.isoformat(),
                            content_hash=content_digest(entry.body),
                        ),
                        start_time=timestamp.isoformat(),
                        end_time=timestamp.isoformat(),
                        text=entry.body[:_MAX_RECORD_CHARS],
                        metadata={"path": path.name, "tags": entry.tags},
                    )
                )
        return records

    def _derived_allowed(
        self,
        subject: EvidenceRef,
        *,
        embedded_sources: list[EvidenceRef] | None = None,
    ) -> bool:
        """Re-evaluate current capture policy through the provenance graph.

        Every derived artifact needs a live policy root even when the current
        policy has no exclusions. Missing ancestry is indistinguishable from
        lost provenance and is therefore quarantined rather than inferred.
        """
        if subject.kind == "timeline_block":
            block = timeline_store.get_by_id(self.conn, subject.id)
            canonical_sources = provenance_store.direct_sources(
                self.conn,
                EvidenceRef(kind="timeline_block", id=subject.id),
            )
            if (
                block is None
                or subject.path
                or (embedded_sources is not None and list(embedded_sources) != canonical_sources)
            ):
                return False
            current_hash = timeline_block_digest(
                start=block.start_time.isoformat(),
                end=block.end_time.isoformat(),
                entries=block.entries,
                apps=block.apps_used,
            )
            if subject.timestamp and subject.timestamp != block.start_time.isoformat():
                return False
            if subject.content_hash and subject.content_hash != current_hash:
                return False
        if subject.kind == "memory_candidate":
            candidate = candidate_store.get(self.conn, subject.id)
            candidate_sources = provenance_store.direct_sources(self.conn, subject)
            if (
                candidate is None
                or candidate_store.is_tombstoned(
                    self.conn,
                    kind="memory_candidate",
                    artifact_id=subject.id,
                )
                or not candidate_store.projection_is_current(candidate)
                or not candidate_store.proposal_is_current(
                    candidate,
                    candidate_sources,
                )
                or (embedded_sources is not None and list(embedded_sources) != candidate_sources)
            ):
                return False
        if subject.kind == "daily_wrap_revision":
            from ..daily_wrap import store as daily_wrap_store

            if not daily_wrap_store.revision_sources_are_current(
                self.conn,
                subject,
            ):
                return False
        sources = list(
            embedded_sources
            if embedded_sources is not None
            else provenance_store.direct_sources(self.conn, subject)
        )
        if not sources:
            if subject.kind == "observation":
                return self._observation_ref_allowed(subject)
            # Legacy structural rows cannot be distinguished from model-
            # derived content whose provenance was lost. Quarantine them
            # until a trusted migration reconstructs their ancestry.
            return False

        # The second tuple member records a policy root: either an authorized
        # raw observation or a current, explicitly hand-written memory entry.
        # Structural session rows never establish that root on their own.
        memo: dict[tuple[str, str, str, str], tuple[bool, bool]] = {}
        visiting: set[tuple[str, str, str]] = set()

        def visit(source: EvidenceRef) -> tuple[bool, bool]:
            cache_key = (
                source.kind,
                source.path,
                source.id,
                source.content_hash,
            )
            if cache_key in memo:
                return memo[cache_key]
            identity = (source.kind, source.path, source.id)
            if identity in visiting:
                return False, False

            if source.kind == "observation":
                result = (self._observation_ref_allowed(source), True)
                memo[cache_key] = result
                return result

            # Session rows are structural co-evidence for reducer entries.
            # Their historical source hash cannot be reconstructed from the
            # row alone, so require a non-empty binding and a live row. A
            # session never satisfies the required observation ancestry by
            # itself.
            if source.kind == "session":
                result = (
                    provenance_store.availability(self.conn, source) == "available",
                    False,
                )
                memo[cache_key] = result
                return result

            if source.kind == "memory_entry":
                if (
                    candidate_store.is_tombstoned(
                        self.conn, kind="memory_file", artifact_id=source.path
                    )
                    or candidate_store.is_tombstoned(
                        self.conn,
                        kind="memory_entry",
                        artifact_id=source.id,
                        path=source.path,
                    )
                    or not source.content_hash
                    or provenance_store.availability(self.conn, source) != "available"
                ):
                    memo[cache_key] = (False, False)
                    return False, False
                try:
                    parsed = files_store.read_file(files_store.memory_path(source.path))
                except (FileNotFoundError, OSError, ValueError):
                    memo[cache_key] = (False, False)
                    return False, False
                entry = next(
                    (item for item in parsed.entries if item.id == source.id),
                    None,
                )
                if (
                    entry is None
                    or not entry.provenance_valid
                    or content_digest(entry.body) != source.content_hash
                ):
                    memo[cache_key] = (False, False)
                    return False, False
                embedded = list(entry.evidence_refs)
                projected = provenance_store.direct_sources(self.conn, source)
                # Markdown is the durable authority; SQLite is a repairable
                # projection. Any disagreement fails closed until rebuild.
                if projected != embedded:
                    memo[cache_key] = (False, False)
                    return False, False
                if not embedded:
                    manual_allowed = (
                        not entry.provenance_present and self._manual_memory_ref_allowed(source)
                    )
                    result = (manual_allowed, manual_allowed)
                    memo[cache_key] = result
                    return result
                visiting.add(identity)
                try:
                    results = [visit(child) for child in embedded]
                finally:
                    visiting.remove(identity)
                result = (
                    all(valid for valid, _has_policy_root in results),
                    any(has_policy_root for _valid, has_policy_root in results),
                )
                memo[cache_key] = result
                return result

            recursive_kinds = {
                "timeline_block",
                "memory_candidate",
                "daily_wrap",
                "daily_wrap_item",
                "daily_wrap_revision",
            }
            if source.kind not in recursive_kinds:
                memo[cache_key] = (False, False)
                return False, False
            if source.kind == "daily_wrap_revision":
                from ..daily_wrap import store as daily_wrap_store

                if not daily_wrap_store.revision_sources_are_current(
                    self.conn,
                    source,
                ):
                    memo[cache_key] = (False, False)
                    return False, False
            if source.kind == "memory_candidate" and candidate_store.is_tombstoned(
                self.conn, kind="memory_candidate", artifact_id=source.id
            ):
                memo[cache_key] = (False, False)
                return False, False
            if source.kind == "memory_candidate":
                candidate = candidate_store.get(self.conn, source.id)
                candidate_sources = provenance_store.direct_sources(
                    self.conn,
                    source,
                )
                if (
                    candidate is None
                    or not candidate_store.projection_is_current(candidate)
                    or not candidate_store.proposal_is_current(
                        candidate,
                        candidate_sources,
                    )
                ):
                    memo[cache_key] = (False, False)
                    return False, False
            if provenance_store.availability(self.conn, source) != "available":
                memo[cache_key] = (False, False)
                return False, False
            if source.kind == "timeline_block" and not (
                source.content_hash
                and timeline_store.get_by_id(self.conn, source.id) is not None
                and provenance_store.is_current(self.conn, source)
            ):
                memo[cache_key] = (False, False)
                return False, False

            child_sources = provenance_store.direct_sources(self.conn, source)
            if not child_sources:
                memo[cache_key] = (False, False)
                return False, False
            visiting.add(identity)
            try:
                results = [visit(child) for child in child_sources]
            finally:
                visiting.remove(identity)
            result = (
                all(valid for valid, _has_observation in results),
                any(has_observation for _valid, has_observation in results),
            )
            memo[cache_key] = result
            return result

        results = [visit(source) for source in sources]
        return all(valid for valid, _has_observation in results) and any(
            has_observation for _valid, has_observation in results
        )

    def _manual_memory_ref_allowed(self, source: EvidenceRef) -> bool:
        """Recognize a hash-bound user entry as a non-capture trust root."""
        try:
            source_path = files_store.memory_path(source.path)
            parsed = files_store.read_file(source_path)
        except (FileNotFoundError, OSError, ValueError):
            return False
        entry = next((item for item in parsed.entries if item.id == source.id), None)
        return bool(
            entry is not None
            and not entry.provenance_present
            and entry.provenance_valid
            and entry.origin_valid
            and entry.origin == files_store.MANUAL_ENTRY_ORIGIN
            and source.content_hash
            and content_digest(entry.body) == source.content_hash
        )

    def _observation_ref_allowed(self, observation: EvidenceRef) -> bool:
        """Validate one provenance leaf and re-apply the current policy."""
        if (
            not observation.path
            or Path(observation.path).name != observation.path
            or not observation.path.endswith(".json")
            or not observation.content_hash
            or candidate_store.is_tombstoned(
                self.conn, kind="capture_file", artifact_id=observation.path
            )
        ):
            return False
        capture_path = paths.capture_buffer_dir() / observation.path
        if capture_path.is_symlink() or not capture_path.is_file():
            return False
        try:
            data = json.loads(capture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return False
        if not isinstance(data, dict):
            return False
        expected_observation_id = str(data.get("observation_id") or f"legacy:{capture_path.stem}")
        if (
            expected_observation_id != observation.id
            or observation_digest(data) != observation.content_hash
            or not privacy_policy.evaluate_stored_observation(
                self.cfg.capture,
                observation=data,
            ).allowed
        ):
            return False
        return not candidate_store.is_tombstoned(
            self.conn, kind="capture_file", artifact_id=observation.path
        )

    def _session_records(
        self, start_utc: datetime, end_utc: datetime, gaps: list[str]
    ) -> list[ContextRecord]:
        rows = self.conn.execute("SELECT * FROM sessions ORDER BY start_time, id").fetchall()
        records: list[ContextRecord] = []
        for row in rows:
            try:
                start = datetime.fromisoformat(row["start_time"])
                end = datetime.fromisoformat(row["end_time"]) if row["end_time"] else None
            except (TypeError, ValueError):
                gaps.append(f"invalid_session:{row['id']}")
                continue
            effective_end = end or datetime.now().astimezone()
            if not _intersects(start, effective_end, start_utc, end_utc):
                continue
            if row["status"] == "active" or end is None:
                gaps.append(f"open_session:{row['id']}")
            elif row["status"] != "reduced":
                gaps.append(f"session_not_reduced:{row['id']}")
            session_payload = {
                "start": start.isoformat(),
                "end": end.isoformat() if end else "",
                "status": row["status"],
            }
            records.append(
                ContextRecord(
                    evidence=EvidenceRef(
                        kind="session",
                        id=row["id"],
                        timestamp=start.isoformat(),
                        content_hash=content_digest(json.dumps(session_payload, sort_keys=True)),
                    ),
                    start_time=start.isoformat(),
                    end_time=end.isoformat() if end else "",
                    text=(
                        f"Session {row['status']} from {start.isoformat()}"
                        + (f" to {end.isoformat()}" if end else "")
                    ),
                    metadata={"status": row["status"]},
                )
            )
        return records


def _zone(name: str) -> ZoneInfo:
    if not name.strip():
        raise ValueError("an IANA timezone is required")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {name}") from exc


def _parse_local_timestamp(value: str, zone: ZoneInfo) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=zone)
    return parsed


def _intersects(
    start: datetime, end: datetime, window_start_utc: datetime, window_end_utc: datetime
) -> bool:
    return _instant(start) < window_end_utc and _instant(end) > window_start_utc


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _policy_digest(cfg: Config) -> str:
    policy = {
        "allowed_bundle_ids": sorted(cfg.capture.allowed_bundle_ids),
        "excluded_bundle_ids": sorted(cfg.capture.excluded_bundle_ids),
        "excluded_app_names": sorted(cfg.capture.excluded_app_names),
        "excluded_window_title_patterns": sorted(cfg.capture.excluded_window_title_patterns),
        "allowed_url_patterns": sorted(cfg.capture.allowed_url_patterns),
        "excluded_url_patterns": sorted(cfg.capture.excluded_url_patterns),
        "deny_unknown_windows": cfg.capture.deny_unknown_windows,
    }
    return hashlib.sha256(
        json.dumps(policy, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _policy_is_restrictive(cfg: Config) -> bool:
    list_values = (
        cfg.capture.allowed_bundle_ids,
        cfg.capture.excluded_bundle_ids,
        cfg.capture.excluded_app_names,
        cfg.capture.excluded_window_title_patterns,
        cfg.capture.allowed_url_patterns,
        cfg.capture.excluded_url_patterns,
    )
    # A malformed falsey TOML value such as ``allowed_url_patterns = ""``
    # must never masquerade as an unrestricted policy and bypass the derived
    # graph authorizer.  The raw capture gate already rejects these shapes;
    # treat them as restrictive here so every retained artifact fails closed.
    if any(
        not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        for value in list_values
    ):
        return True
    if not isinstance(cfg.capture.deny_unknown_windows, bool):
        return True
    return bool(any(list_values) or cfg.capture.deny_unknown_windows)


def _bounded_metadata(metadata: dict[str, object]) -> dict[str, object]:
    bounded: dict[str, object] = {}
    for raw_key in sorted(metadata, key=str)[:20]:
        key = str(raw_key)[:64]
        value = metadata[raw_key]
        if isinstance(value, list):
            bounded[key] = [str(item)[:200] for item in value[:20]]
        elif isinstance(value, str):
            bounded[key] = value[:500]
        elif isinstance(value, (bool, int, float)) or value is None:
            bounded[key] = value
        else:
            bounded[key] = str(value)[:500]
    return bounded


def _summarize_coverage_gaps(gaps: list[str]) -> list[str]:
    """Keep remote coverage diagnostics bounded and free of unbounded IDs."""
    counted: Counter[str] = Counter()
    details: set[str] = set()
    for raw_gap in gaps:
        gap = str(raw_gap)
        kind = gap.split(":", 1)[0]
        if kind in _COUNTED_GAP_KINDS:
            counted[kind] += 1
        else:
            details.add(gap[:_MAX_COVERAGE_GAP_CHARS])
    result = [f"{kind}:{count}" for kind, count in sorted(counted.items())]
    result.extend(sorted(details))
    if len(result) > _MAX_COVERAGE_GAPS:
        omitted = len(result) - (_MAX_COVERAGE_GAPS - 1)
        result = result[: _MAX_COVERAGE_GAPS - 1]
        result.append(f"coverage_gap_kinds_truncated:{omitted}")
    return sorted(result)


def _bound_records(records: list[ContextRecord]) -> tuple[list[ContextRecord], int]:
    if not records:
        return [], 0
    limit = min(len(records), _MAX_PROMPT_RECORDS)
    while True:
        selected = _even_sample(records, limit)
        payload_size = sum(
            len(
                json.dumps(
                    record.prompt_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            for record in selected
        )
        if payload_size <= _MAX_PROMPT_BYTES or limit == 1:
            return selected, len(records) - len(selected)
        next_limit = max(1, int(limit * _MAX_PROMPT_BYTES / payload_size * 0.95))
        limit = min(limit - 1, next_limit)


def _even_sample(records: list[ContextRecord], limit: int) -> list[ContextRecord]:
    if limit >= len(records):
        return list(records)
    if limit == 1:
        return [records[len(records) // 2]]
    return [records[index * (len(records) - 1) // (limit - 1)] for index in range(limit)]
