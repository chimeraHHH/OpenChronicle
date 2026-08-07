"""Exact, policy-aware evidence hydration for the trusted desktop shell."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .. import paths
from ..capture import store_lock as capture_store
from ..config import Config
from ..daily_wrap import store as daily_wrap_store
from ..memory_candidates import store as candidate_store
from ..privacy import policy as privacy_policy
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, content_digest, observation_digest
from ..store import entries as entries_store
from ..store import files as files_store
from ..timeline import store as timeline_store
from .context import ContextService

_HASHED_KINDS = {"observation", "timeline_block", "session", "memory_entry"}
_WRAP_CATEGORIES = ("completed", "progressed", "open", "blocked", "needs_review")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]{0,80}PRIVATE KEY-----.*?"
    r"-----END [^-\r\n]{0,80}PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|"
    r"AKIA[A-Z0-9]{16})\b"
)
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(password|passcode|api[_ -]?key|access[_ -]?token|"
    r"refresh[_ -]?token|client[_ -]?secret)\b(\s*[:=]\s*)"
    r"([^\s,;]{4,})"
)


class EvidenceResolver:
    """Resolve a typed local reference without accepting arbitrary file paths."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg
        self.context = ContextService(conn, cfg)

    def resolve(self, ref: EvidenceRef) -> dict[str, Any]:
        if ref.kind == "observation":
            return self._resolve_observation(ref)
        with files_store.review_operation_lock():
            if ref.kind == "timeline_block":
                return self._resolve_timeline_block(ref)
            if ref.kind == "session":
                return self._resolve_session(ref)
            if ref.kind == "memory_entry":
                return self._resolve_memory_entry(ref)
            if ref.kind == "memory_candidate":
                return self._resolve_candidate(ref)
            if ref.kind == "daily_wrap":
                return self._resolve_wrap(ref)
            if ref.kind == "daily_wrap_item":
                return self._resolve_wrap_item(ref)
        return _resolution(ref, "unsupported")

    def _resolve_observation(self, ref: EvidenceRef) -> dict[str, Any]:
        if not ref.path or Path(ref.path).name != ref.path or not ref.path.endswith(".json"):
            return _resolution(ref, "unverifiable")
        if not ref.content_hash:
            return _resolution(ref, "unverifiable")
        with capture_store.capture_store_lock():
            if candidate_store.is_tombstoned(self.conn, kind="capture_file", artifact_id=ref.path):
                return _resolution(ref, "purging")
            source_path = paths.capture_buffer_dir() / ref.path
            try:
                raw = json.loads(source_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return _resolution(ref, "expired")
            except (OSError, json.JSONDecodeError):
                return _resolution(ref, "unverifiable")
            if not isinstance(raw, dict):
                return _resolution(ref, "unverifiable")
            observation_id = str(raw.get("observation_id") or f"legacy:{source_path.stem}")
            if observation_id != ref.id or observation_digest(raw) != ref.content_hash:
                return _resolution(ref, "changed")
            meta = raw.get("window_meta")
            if not isinstance(meta, dict):
                return _resolution(ref, "unverifiable")
            decision = privacy_policy.evaluate_window(
                self.cfg.capture,
                app_name=str(meta.get("app_name") or ""),
                bundle_id=str(meta.get("bundle_id") or ""),
                window_title=str(meta.get("title") or ""),
            )
            if not decision.allowed:
                return _resolution(ref, "excluded")
            if candidate_store.is_tombstoned(self.conn, kind="capture_file", artifact_id=ref.path):
                return _resolution(ref, "purging")
            focused = raw.get("focused_element")
            if not isinstance(focused, dict):
                focused = {}
            content = {
                "type": "observation",
                "file": ref.path[:1_024],
                "observation_id": observation_id[:512],
                "timestamp": str(raw.get("timestamp") or "")[:100],
                "app_name": _bounded_text(meta.get("app_name"), 200),
                "bundle_id": _bounded_text(meta.get("bundle_id"), 300),
                "window_title": _bounded_text(meta.get("title"), 500),
                "focused_element": {
                    "role": _bounded_text(focused.get("role"), 100),
                    "title": _bounded_text(focused.get("title"), 200),
                    "value": _bounded_text(focused.get("value"), 2_000),
                    "is_editable": bool(focused.get("is_editable")),
                },
                "visible_text": _bounded_text(raw.get("visible_text"), 10_000),
                "screenshot_stripped": bool(raw.get("screenshot_stripped")),
            }
            return _resolution(ref, "current", content)

    def _resolve_timeline_block(self, ref: EvidenceRef) -> dict[str, Any]:
        status = self._hash_status(ref)
        if status != "current":
            return _resolution(ref, status)
        block = timeline_store.get_by_id(self.conn, ref.id)
        if block is None:
            return _resolution(ref, "missing")
        sources = provenance_store.direct_sources(self.conn, ref)
        if sources and not self._sources_current(sources):
            return _resolution(ref, "changed")
        if not self.context.evidence_allowed(ref, embedded_sources=sources or None):
            return _resolution(ref, "excluded")
        return _resolution(
            ref,
            "current",
            {
                "type": "timeline_block",
                "id": str(block.id)[:128],
                "start_time": block.start_time.isoformat(),
                "end_time": block.end_time.isoformat(),
                "timezone": str(block.timezone)[:100],
                "entries": [_bounded_text(value, 2_000) for value in block.entries[:100]],
                "apps_used": [_bounded_text(value, 200) for value in block.apps_used[:50]],
                "capture_count": block.capture_count,
            },
        )

    def _resolve_session(self, ref: EvidenceRef) -> dict[str, Any]:
        status = self._hash_status(ref)
        if status != "current":
            return _resolution(ref, status)
        row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (ref.id,)).fetchone()
        if row is None:
            return _resolution(ref, "missing")
        sources = provenance_store.direct_sources(self.conn, ref)
        if sources and not self._sources_current(sources):
            return _resolution(ref, "changed")
        if not self.context.evidence_allowed(ref, embedded_sources=sources or None):
            return _resolution(ref, "excluded")
        return _resolution(
            ref,
            "current",
            {
                "type": "session",
                "id": str(row["id"])[:128],
                "start_time": str(row["start_time"])[:100],
                "end_time": str(row["end_time"] or "")[:100],
                "status": str(row["status"])[:50],
            },
        )

    def _resolve_memory_entry(self, ref: EvidenceRef) -> dict[str, Any]:
        if not ref.path or not ref.content_hash:
            return _resolution(ref, "unverifiable")
        try:
            source_path = files_store.memory_path(ref.path)
        except ValueError:
            return _resolution(ref, "unverifiable")
        with files_store.store_write_lock(), files_store.file_lock(source_path):
            if candidate_store.is_tombstoned(
                self.conn, kind="memory_file", artifact_id=source_path.name
            ) or candidate_store.is_tombstoned(
                self.conn,
                kind="memory_entry",
                artifact_id=ref.id,
                path=source_path.name,
            ):
                return _resolution(ref, "purging")
            try:
                parsed = files_store.read_file(source_path)
            except FileNotFoundError:
                return _resolution(ref, "missing")
            except (OSError, ValueError):
                return _resolution(ref, "unverifiable")
            entry = next((value for value in parsed.entries if value.id == ref.id), None)
            if entry is None:
                return _resolution(ref, "missing")
            if not entry.provenance_valid or content_digest(entry.body) != ref.content_hash:
                return _resolution(ref, "changed")
            if not entries_store.dependency_sources_are_live(
                self.conn, entry.evidence_refs
            ) or not self._sources_current(entry.evidence_refs):
                return _resolution(ref, "changed")
            if not self.context.evidence_allowed(ref, embedded_sources=entry.evidence_refs):
                return _resolution(ref, "excluded")
            if candidate_store.is_tombstoned(
                self.conn, kind="memory_file", artifact_id=source_path.name
            ) or candidate_store.is_tombstoned(
                self.conn,
                kind="memory_entry",
                artifact_id=ref.id,
                path=source_path.name,
            ):
                return _resolution(ref, "purging")
            return _resolution(
                ref,
                "current",
                {
                    "type": "memory_entry",
                    "id": str(entry.id)[:128],
                    "path": source_path.name[:512],
                    "timestamp": str(entry.timestamp)[:100],
                    "tags": [_bounded_text(value, 100) for value in entry.tags[:100]],
                    "body": _bounded_text(entry.body, 20_000),
                    "superseded_by": str(entry.superseded_by or "")[:128],
                    "evidence": [_bounded_ref(source) for source in entry.evidence_refs[:100]],
                },
            )

    def _resolve_candidate(self, ref: EvidenceRef) -> dict[str, Any]:
        if candidate_store.is_tombstoned(self.conn, kind="memory_candidate", artifact_id=ref.id):
            return _resolution(ref, "purging")
        candidate = candidate_store.get(self.conn, ref.id)
        if candidate is None:
            return _resolution(ref, "missing")
        sources = provenance_store.direct_sources(self.conn, ref)
        if not sources or not self._sources_current(sources):
            return _resolution(ref, "changed")
        if not self.context.evidence_allowed(ref, embedded_sources=sources):
            return _resolution(ref, "excluded")
        return _resolution(
            ref,
            "current",
            {
                "type": "memory_candidate",
                "id": str(candidate.id)[:128],
                "kind": str(candidate.kind)[:100],
                "target_path": str(candidate.target_path)[:512],
                "content": _bounded_text(candidate.content, 20_000),
                "tags": [_bounded_text(value, 100) for value in candidate.tags[:100]],
                "status": str(candidate.status)[:50],
                "version": int(candidate.version),
            },
        )

    def _resolve_wrap(self, ref: EvidenceRef) -> dict[str, Any]:
        if candidate_store.is_tombstoned(self.conn, kind="daily_wrap", artifact_id=ref.id):
            return _resolution(ref, "purging")
        row = daily_wrap_store.get_by_id(self.conn, ref.id)
        if row is None:
            return _resolution(ref, "missing")
        sources = provenance_store.direct_sources(self.conn, ref)
        if sources and not self._sources_current(sources):
            return _resolution(ref, "changed")
        if sources and not self.context.evidence_allowed(ref, embedded_sources=sources):
            return _resolution(ref, "excluded")
        return _resolution(
            ref,
            "current",
            {
                "type": "daily_wrap",
                "id": str(row.id)[:128],
                "local_date": str(row.local_date)[:10],
                "timezone": str(row.timezone)[:100],
                "scope": str(row.scope)[:100],
                "status": str(row.status)[:50],
                "coverage_status": str(row.coverage_status)[:50],
                "revision": int(row.revision),
                "output": _bounded_wrap_output(row.output),
            },
        )

    def _resolve_wrap_item(self, ref: EvidenceRef) -> dict[str, Any]:
        wrap_id = ref.path
        if not wrap_id:
            return _resolution(ref, "unverifiable")
        if candidate_store.is_tombstoned(self.conn, kind="daily_wrap", artifact_id=wrap_id):
            return _resolution(ref, "purging")
        row = daily_wrap_store.get_by_id(self.conn, wrap_id)
        if row is None or not isinstance(row.output, dict):
            return _resolution(ref, "missing")
        item: dict[str, Any] | None = None
        category = ""
        for candidate_category in _WRAP_CATEGORIES:
            values = row.output.get(candidate_category, [])
            if not isinstance(values, list):
                continue
            item = next(
                (
                    value
                    for value in values
                    if isinstance(value, dict) and str(value.get("id") or "") == ref.id
                ),
                None,
            )
            if item is not None:
                category = candidate_category
                break
        if item is None:
            return _resolution(ref, "missing")
        raw_sources = item.get("evidence")
        if not isinstance(raw_sources, list) or not raw_sources:
            return _resolution(ref, "unverifiable")
        try:
            sources = [EvidenceRef.from_dict(value) for value in raw_sources]
        except ValueError:
            return _resolution(ref, "unverifiable")
        if not self._sources_current(sources):
            return _resolution(ref, "changed")
        if not self.context.evidence_allowed(ref, embedded_sources=sources):
            return _resolution(ref, "excluded")
        return _resolution(
            ref,
            "current",
            {
                "type": "daily_wrap_item",
                "wrap_id": wrap_id[:128],
                "category": category[:50],
                "item": _bounded_wrap_item(item),
            },
        )

    def _hash_status(self, ref: EvidenceRef) -> str:
        if ref.kind not in _HASHED_KINDS or not ref.content_hash:
            return "unverifiable"
        availability = provenance_store.availability(self.conn, ref)
        if availability == "expired":
            return "expired"
        if availability != "available":
            return "missing"
        try:
            current_hash = provenance_store.current_content_hash(self.conn, ref)
        except (OSError, ValueError):
            return "unverifiable"
        return "current" if current_hash == ref.content_hash else "changed"

    def _sources_current(self, sources: list[EvidenceRef]) -> bool:
        for source in sources:
            if source.kind == "observation" and candidate_store.is_tombstoned(
                self.conn, kind="capture_file", artifact_id=source.path
            ):
                return False
            if source.kind == "memory_entry" and (
                candidate_store.is_tombstoned(
                    self.conn,
                    kind="memory_entry",
                    artifact_id=source.id,
                    path=source.path,
                )
                or candidate_store.is_tombstoned(
                    self.conn, kind="memory_file", artifact_id=source.path
                )
            ):
                return False
            if source.kind == "memory_candidate" and candidate_store.is_tombstoned(
                self.conn, kind="memory_candidate", artifact_id=source.id
            ):
                return False
            availability = provenance_store.availability(self.conn, source)
            if availability != "available":
                return False
            if source.kind in _HASHED_KINDS and not provenance_store.is_current(self.conn, source):
                return False
        return True


def _resolution(
    ref: EvidenceRef,
    status: str,
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "reference": _bounded_ref(ref),
        "status": status,
        "content": content,
    }


def _bounded_ref(ref: EvidenceRef) -> dict[str, str]:
    return {
        "kind": str(ref.kind)[:64],
        "id": str(ref.id)[:512],
        "path": str(ref.path)[:1_024],
        "timestamp": str(ref.timestamp)[:100],
        "content_hash": str(ref.content_hash)[:128],
    }


def _bounded_text(value: object, limit: int) -> str:
    text = str(value or "")
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _TOKEN_RE.sub("[REDACTED TOKEN]", text)
    text = _ASSIGNMENT_SECRET_RE.sub(r"\1\2[REDACTED]", text)
    return text[:limit]


def _bounded_wrap_output(output: object) -> dict[str, Any] | None:
    if not isinstance(output, dict):
        return None
    result: dict[str, Any] = {
        "schema_version": int(output.get("schema_version") or 0),
        "summary": _bounded_text(output.get("summary"), 1_000),
        "coverage_gaps": [
            _bounded_text(value, 200)
            for value in (
                output.get("coverage_gaps") if isinstance(output.get("coverage_gaps"), list) else []
            )[:64]
        ],
        "generated_at": str(output.get("generated_at") or "")[:100],
    }
    for category in _WRAP_CATEGORIES:
        raw_items = output.get(category)
        result[category] = [
            _bounded_wrap_item(value)
            for value in (raw_items if isinstance(raw_items, list) else [])[:100]
            if isinstance(value, dict)
        ]
    return result


def _bounded_wrap_item(item: dict[str, Any]) -> dict[str, Any]:
    raw_evidence = item.get("evidence")
    evidence: list[dict[str, str]] = []
    for value in (raw_evidence if isinstance(raw_evidence, list) else [])[:20]:
        if not isinstance(value, dict):
            continue
        try:
            evidence.append(_bounded_ref(EvidenceRef.from_dict(value)))
        except ValueError:
            continue
    return {
        "id": str(item.get("id") or "")[:128],
        "kind": str(item.get("kind") or "")[:50],
        "text": _bounded_text(item.get("text"), 500),
        "supporting_text": _bounded_text(item.get("supporting_text"), 500),
        "untrusted_activity_quote": bool(item.get("untrusted_activity_quote")),
        "evidence": evidence,
    }
