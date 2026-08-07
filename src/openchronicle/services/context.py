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
from ..provenance import store as provenance_store
from ..provenance.models import (
    EvidenceRef,
    content_digest,
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
    "invalid_timeline_block",
    "excluded_timeline_block",
    "purging_event_entry",
    "purging_memory_file",
    "invalid_event_provenance",
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
            key=lambda item: (item.start_time, item.evidence.kind, item.evidence.path, item.evidence.id),
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

    def for_day(self, local_date: date, timezone: str) -> DayContext:
        zone = _zone(timezone)
        local_start = datetime.combine(local_date, time.min, zone)
        local_end = datetime.combine(local_date + timedelta(days=1), time.min, zone)
        start_utc = local_start.astimezone(UTC)
        end_utc = local_end.astimezone(UTC)
        records: list[ContextRecord] = []
        gaps: list[str] = []

        records.extend(self._timeline_records(start_utc, end_utc, gaps))
        records.extend(self._event_records(start_utc, end_utc, zone, gaps))
        records.extend(self._session_records(start_utc, end_utc, gaps))
        records.sort(
            key=lambda item: (item.start_time, item.evidence.kind, item.evidence.path, item.evidence.id)
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

    def _timeline_records(
        self, start_utc: datetime, end_utc: datetime, gaps: list[str]
    ) -> list[ContextRecord]:
        rows = self.conn.execute(
            "SELECT * FROM timeline_blocks ORDER BY start_time, id"
        ).fetchall()
        excluded_names = {name.casefold() for name in self.cfg.capture.excluded_app_names}
        records: list[ContextRecord] = []
        for row in rows:
            try:
                start = datetime.fromisoformat(row["start_time"])
                end = datetime.fromisoformat(row["end_time"])
                entries = json.loads(row["entries"] or "[]")
                apps = json.loads(row["apps_used"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                gaps.append(f"invalid_timeline_block:{row['id']}")
                continue
            if not _intersects(start, end, start_utc, end_utc):
                continue
            clean_apps = [str(app) for app in apps] if isinstance(apps, list) else []
            if any(app.casefold() in excluded_names for app in clean_apps) or not self._derived_allowed(
                EvidenceRef(kind="timeline_block", id=row["id"])
            ):
                gaps.append(f"excluded_timeline_block:{row['id']}")
                continue
            clean_entries = [str(entry).strip() for entry in entries if str(entry).strip()]
            text = "\n".join(clean_entries)[:_MAX_RECORD_CHARS]
            records.append(
                ContextRecord(
                    evidence=EvidenceRef(
                        kind="timeline_block",
                        id=row["id"],
                        timestamp=start.isoformat(),
                        content_hash=timeline_block_digest(
                            start=row["start_time"],
                            end=row["end_time"],
                            entries=clean_entries,
                            apps=clean_apps,
                        ),
                    ),
                    start_time=start.isoformat(),
                    end_time=end.isoformat(),
                    text=text,
                    metadata={"apps": clean_apps, "capture_count": int(row["capture_count"] or 0)},
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
            if candidate_store.is_tombstoned(
                self.conn, kind="memory_file", artifact_id=path.name
            ):
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
                if not entries_store.dependency_sources_are_live(
                    self.conn, entry.evidence_refs
                ):
                    gaps.append(
                        f"stale_event_provenance:{path.name}#{entry.id}"
                    )
                    continue
                timestamp = _parse_local_timestamp(entry.timestamp, zone)
                if timestamp is None:
                    continue
                instant = timestamp.astimezone(UTC)
                if not start_utc <= instant < end_utc:
                    continue
                entry_ref = EvidenceRef(
                    kind="memory_entry", id=entry.id, path=path.name
                )
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

        When policy has no restrictions, already-ingested records remain usable.
        Once a user configures exclusions/allowlists, missing raw provenance is
        treated conservatively and omitted from remote synthesis.
        """
        if not _policy_is_restrictive(self.cfg):
            return True
        queue = list(embedded_sources or provenance_store.direct_sources(self.conn, subject))
        seen: set[tuple[str, str, str]] = set()
        observations: list[EvidenceRef] = []
        while queue:
            current = queue.pop(0)
            key = (current.kind, current.path, current.id)
            if key in seen:
                continue
            seen.add(key)
            if current.kind == "observation":
                observations.append(current)
                continue
            queue.extend(provenance_store.direct_sources(self.conn, current))
        if not observations:
            return False
        for observation in observations:
            if not observation.path or Path(observation.path).name != observation.path:
                return False
            capture_path = paths.capture_buffer_dir() / observation.path
            try:
                data = json.loads(capture_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            meta = data.get("window_meta") if isinstance(data, dict) else None
            if not isinstance(meta, dict):
                return False
            expected_observation_id = str(
                data.get("observation_id") or f"legacy:{capture_path.stem}"
            )
            if expected_observation_id != observation.id:
                return False
            if (
                observation.content_hash
                and observation_digest(data) != observation.content_hash
            ):
                return False
            decision = privacy_policy.evaluate_window(
                self.cfg.capture,
                app_name=str(meta.get("app_name") or ""),
                bundle_id=str(meta.get("bundle_id") or ""),
                window_title=str(meta.get("title") or ""),
            )
            if not decision.allowed:
                return False
        return True

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
                        content_hash=content_digest(
                            json.dumps(session_payload, sort_keys=True)
                        ),
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
        "excluded_window_title_patterns": sorted(
            cfg.capture.excluded_window_title_patterns
        ),
        "deny_unknown_windows": cfg.capture.deny_unknown_windows,
    }
    return hashlib.sha256(
        json.dumps(policy, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _policy_is_restrictive(cfg: Config) -> bool:
    return bool(
        cfg.capture.allowed_bundle_ids
        or cfg.capture.excluded_bundle_ids
        or cfg.capture.excluded_app_names
        or cfg.capture.excluded_window_title_patterns
        or cfg.capture.deny_unknown_windows
    )


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
    return [
        records[index * (len(records) - 1) // (limit - 1)]
        for index in range(limit)
    ]
