"""One-shot, capability-scoped JSON bridge for the native desktop shell."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config as config_mod
from . import paths
from .daily_wrap import store as daily_wrap_store
from .daily_wrap.service import DailyWrapService
from .memory_candidates import store as candidate_store
from .provenance import store as provenance_store
from .provenance.models import EvidenceRef
from .services.capture_control import PauseStateConflict, set_paused
from .services.evidence import EvidenceResolver
from .services.memory import MemoryService, PurgeClosureUnverifiable, StalePurgePlan
from .services.snapshot import build_snapshot
from .store import files as files_store
from .store import fts

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class BridgeError(Exception):
    code: str
    message: str
    exit_code: int = 2


def handle_request_bytes(payload: bytes) -> tuple[dict[str, Any], int]:
    """Validate and execute exactly one request without writing to stdout."""
    try:
        request = _decode_request(payload)
        result = _dispatch(request["operation"], request["params"])
        return {"version": PROTOCOL_VERSION, "ok": True, "result": result}, 0
    except BridgeError as exc:
        return {
            "version": PROTOCOL_VERSION,
            "ok": False,
            "error": {"code": exc.code, "message": exc.message},
        }, exc.exit_code
    except candidate_store.CandidateConflict:
        return _error("VERSION_CONFLICT", "The reviewed candidate changed."), 2
    except StalePurgePlan:
        return _error("STALE_PURGE_PLAN", "The deletion preview is stale."), 2
    except PurgeClosureUnverifiable:
        return _error(
            "PURGE_CLOSURE_UNVERIFIABLE",
            "A damaged provenance frame prevents a safe deletion preview.",
        ), 2
    except PauseStateConflict:
        return _error("VERSION_CONFLICT", "The capture pause state changed."), 2
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return _error("INVALID_PARAMS", "The operation parameters are invalid."), 2
    except KeyError:
        return _error("NOT_FOUND", "The requested local record was not found."), 2
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
            return _error("BUSY", "The local store is busy; retry shortly."), 3
        return _error("INTERNAL_ERROR", "The local operation failed."), 1
    except Exception:  # noqa: BLE001 - protocol must return one sanitized response
        return _error("INTERNAL_ERROR", "The local operation failed."), 1


def main() -> None:
    payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    response, exit_code = handle_request_bytes(payload)
    encoded = json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()
    raise SystemExit(exit_code)


def _decode_request(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_REQUEST_BYTES:
        raise BridgeError("REQUEST_TOO_LARGE", "The bridge request exceeds 64 KiB.")
    if not payload:
        raise BridgeError("INVALID_REQUEST", "A single JSON request is required.")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("INVALID_JSON", "The bridge request is not valid JSON.") from exc
    if not isinstance(raw, dict) or set(raw) != {"version", "operation", "params"}:
        raise BridgeError("INVALID_REQUEST", "The bridge request shape is invalid.")
    if raw["version"] != PROTOCOL_VERSION:
        raise BridgeError("INVALID_REQUEST", "The bridge protocol version is unsupported.")
    if not isinstance(raw["operation"], str) or not isinstance(raw["params"], dict):
        raise BridgeError("INVALID_REQUEST", "The bridge request shape is invalid.")
    return raw


def _dispatch(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "snapshot": _snapshot,
        "candidate.get": _candidate_get,
        "candidate.edit": _candidate_edit,
        "candidate.approve": _candidate_approve,
        "candidate.reject": _candidate_reject,
        "candidate.forget_preview": _candidate_forget_preview,
        "candidate.forget_commit": _candidate_forget_commit,
        "wrap.get": _wrap_get,
        "provenance.trace": _provenance_trace,
        "evidence.resolve": _evidence_resolve,
        "capture.set_paused": _capture_set_paused,
    }
    handler = handlers.get(operation)
    if handler is None:
        raise BridgeError("UNKNOWN_OPERATION", "The requested bridge operation is unavailable.")
    paths.ensure_dirs()
    config_mod.write_default_if_missing()
    return handler(params)


def _snapshot(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, optional={"timeline_limit", "candidate_limit", "wrap_limit"})
    timeline_limit = _bounded_int(params.get("timeline_limit", 12), 0, 24)
    candidate_limit = _bounded_int(params.get("candidate_limit", 50), 0, 100)
    wrap_limit = _bounded_int(params.get("wrap_limit", 14), 0, 30)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        return build_snapshot(
            conn,
            cfg,
            timeline_limit=timeline_limit,
            candidate_limit=candidate_limit,
            wrap_limit=wrap_limit,
        )


def _candidate_get(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"candidate_id"})
    candidate_id = _bounded_string(params["candidate_id"], 128, nonempty=True)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens)
        service.resume_pending_purges()
        candidate = service.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        ref = EvidenceRef(kind="memory_candidate", id=candidate_id)
        evidence = [
            _reference_summary(conn, source)
            for source in provenance_store.direct_sources(conn, ref)[:100]
        ]
        return {"candidate": _candidate_payload(candidate), "evidence": evidence}


def _candidate_edit(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={"candidate_id", "expected_version", "content", "tags"},
    )
    candidate_id = _bounded_string(params["candidate_id"], 128, nonempty=True)
    expected_version = _bounded_int(params["expected_version"], 1, 2_147_483_647)
    content = _bounded_string(params["content"], 20_000, nonempty=True)
    tags = _string_list(params["tags"], max_items=100, max_length=100)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens)
        service.resume_pending_purges()
        updated = service.edit_candidate(
            candidate_id,
            expected_version=expected_version,
            content=content,
            tags=tags,
            # The desktop review surface may edit content and tags, but it may
            # not escape a conflict by changing the classifier-owned grouping
            # key. MemoryService preserves the current key when this is None.
            conflict_key=None,
        )
        return {"candidate": _candidate_payload(updated)}


def _candidate_approve(params: dict[str, Any]) -> dict[str, Any]:
    candidate_id, expected_version = _candidate_cas_params(params)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens)
        service.resume_pending_purges()
        approved = service.approve_candidate(candidate_id, expected_version=expected_version)
        return {"candidate": _candidate_payload(approved)}


def _candidate_reject(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"candidate_id", "expected_version"}, optional={"reason"})
    candidate_id = _bounded_string(params["candidate_id"], 128, nonempty=True)
    expected_version = _bounded_int(params["expected_version"], 1, 2_147_483_647)
    reason = _bounded_string(params.get("reason", ""), 1_000, nonempty=False)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens)
        service.resume_pending_purges()
        rejected = service.reject_candidate(
            candidate_id, expected_version=expected_version, reason=reason
        )
        return {"candidate": _candidate_payload(rejected)}


def _candidate_forget_preview(params: dict[str, Any]) -> dict[str, Any]:
    candidate_id, expected_version = _candidate_cas_params(params)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens)
        service.resume_pending_purges()
        return service.preview_purge_candidate(
            candidate_id, expected_version=expected_version
        ).to_dict()


def _candidate_forget_commit(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"candidate_id", "expected_version", "plan_digest"})
    candidate_id = _bounded_string(params["candidate_id"], 128, nonempty=True)
    expected_version = _bounded_int(params["expected_version"], 1, 2_147_483_647)
    plan_digest = _bounded_string(params["plan_digest"], 64, nonempty=True)
    if len(plan_digest) != 64 or any(char not in "0123456789abcdef" for char in plan_digest):
        raise ValueError("invalid purge plan digest")
    cfg = config_mod.load()
    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens)
        result = service.purge_candidate(
            candidate_id,
            expected_version=expected_version,
            expected_plan_digest=plan_digest,
        )
        return {
            "candidate_id": result.candidate_id,
            "removed_entry": result.removed_entry,
            "removed_file_count": len(result.removed_files),
            "invalidated_wrap_ids": list(result.invalidated_wraps)[:100],
        }


def _wrap_get(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"local_date", "timezone"}, optional={"scope"})
    local_date = _bounded_string(params["local_date"], 10, nonempty=True)
    target_date = date.fromisoformat(local_date)
    timezone = _bounded_string(params["timezone"], 100, nonempty=True)
    ZoneInfo(timezone)
    scope = _bounded_string(params.get("scope", "default"), 100, nonempty=True)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        row = DailyWrapService(conn, cfg).get(target_date, timezone, scope=scope)
        if row is None:
            raise KeyError(local_date)
        return {"wrap": _wrap_payload(row)}


def _provenance_trace(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={"kind", "artifact_id"},
        optional={"path", "max_depth"},
    )
    ref = _request_ref(params, id_field="artifact_id")
    max_depth = _bounded_int(params.get("max_depth", 4), 1, 8)
    with fts.cursor() as conn, files_store.review_operation_lock():
        _require_visible_subject(conn, ref)
        direct = provenance_store.direct_sources(conn, ref)[:100]
        trace = _trace_sources_bounded(conn, ref, max_depth=max_depth, max_nodes=256)
        return {
            "subject": _reference_summary(conn, ref),
            "direct_sources": [_reference_summary(conn, source) for source in direct],
            "trace": trace,
        }


def _evidence_resolve(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={"kind", "id"},
        optional={"path", "timestamp", "content_hash"},
    )
    ref = _request_ref(params, id_field="id")
    cfg = config_mod.load()
    with fts.cursor() as conn:
        return EvidenceResolver(conn, cfg).resolve(ref)


def _capture_set_paused(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"expected_state", "paused"})
    expected_state = _strict_bool(params["expected_state"])
    paused = _strict_bool(params["paused"])
    return set_paused(expected_state=expected_state, paused=paused)


def _candidate_cas_params(params: dict[str, Any]) -> tuple[str, int]:
    _fields(params, required={"candidate_id", "expected_version"})
    return (
        _bounded_string(params["candidate_id"], 128, nonempty=True),
        _bounded_int(params["expected_version"], 1, 2_147_483_647),
    )


def _request_ref(params: dict[str, Any], *, id_field: str) -> EvidenceRef:
    return EvidenceRef(
        kind=_bounded_string(params["kind"], 64, nonempty=True),
        id=_bounded_string(params[id_field], 512, nonempty=True),
        path=_bounded_string(params.get("path", ""), 1_024, nonempty=False),
        timestamp=_bounded_string(params.get("timestamp", ""), 100, nonempty=False),
        content_hash=_bounded_string(params.get("content_hash", ""), 128, nonempty=False),
    )


def _candidate_payload(candidate) -> dict[str, Any]:
    return {
        "id": str(candidate.id)[:128],
        "proposal_digest": str(candidate.proposal_digest)[:128],
        "producer_run_key": str(candidate.producer_run_key)[:256],
        "proposal_slot": int(candidate.proposal_slot),
        "kind": str(candidate.kind)[:100],
        "operation": str(candidate.operation)[:100],
        "target_path": str(candidate.target_path)[:512],
        "content": str(candidate.content)[:20_000],
        "content_hash": str(candidate.content_hash)[:128],
        "tags": [str(tag)[:100] for tag in candidate.tags[:100]],
        "confidence": candidate.confidence,
        "conflict_key": str(candidate.conflict_key)[:500],
        "status": str(candidate.status)[:50],
        "version": int(candidate.version),
        "applied_entry_id": (
            str(candidate.applied_entry_id)[:128] if candidate.applied_entry_id else None
        ),
        "created_at": str(candidate.created_at)[:100],
        "updated_at": str(candidate.updated_at)[:100],
        "reviewed_at": str(candidate.reviewed_at)[:100] if candidate.reviewed_at else None,
        "review_reason": str(candidate.review_reason)[:1_000],
        "last_error": str(candidate.last_error)[:1_000],
    }


def _wrap_payload(row) -> dict[str, Any]:
    return {
        "id": str(row.id)[:128],
        "local_date": str(row.local_date)[:10],
        "timezone": str(row.timezone)[:100],
        "scope": str(row.scope)[:100],
        "window_start_utc": str(row.window_start_utc)[:100],
        "window_end_utc": str(row.window_end_utc)[:100],
        "workflow_version": int(row.workflow_version),
        "status": str(row.status)[:50],
        "coverage_status": str(row.coverage_status)[:50],
        "attempt_count": int(row.attempt_count),
        "output": _bounded_wrap_output(row.output),
        "revision": int(row.revision),
        "created_at": str(row.created_at)[:100],
        "updated_at": str(row.updated_at)[:100],
        "completed_at": str(row.completed_at)[:100] if row.completed_at else None,
        "last_error": str(row.last_error)[:1_000],
    }


def _bounded_wrap_output(output: object) -> dict[str, Any] | None:
    if not isinstance(output, dict):
        return None
    result: dict[str, Any] = {
        "schema_version": int(output.get("schema_version") or 0),
        "local_date": str(output.get("local_date") or "")[:10],
        "timezone": str(output.get("timezone") or "")[:100],
        "status": str(output.get("status") or "")[:50],
        "summary": str(output.get("summary") or "")[:1_000],
        "coverage_gaps": [
            str(value)[:200]
            for value in (
                output.get("coverage_gaps") if isinstance(output.get("coverage_gaps"), list) else []
            )[:64]
        ],
        "generated_at": str(output.get("generated_at") or "")[:100],
    }
    for category in ("completed", "progressed", "open", "blocked", "needs_review"):
        values = output.get(category)
        result[category] = [
            _bounded_wrap_item(value)
            for value in (values if isinstance(values, list) else [])[:100]
            if isinstance(value, dict)
        ]
    return result


def _bounded_wrap_item(item: dict[str, Any]) -> dict[str, Any]:
    raw_evidence = item.get("evidence")
    return {
        "id": str(item.get("id") or "")[:128],
        "kind": str(item.get("kind") or "")[:50],
        "text": str(item.get("text") or "")[:500],
        "supporting_text": str(item.get("supporting_text") or "")[:500],
        "untrusted_activity_quote": bool(item.get("untrusted_activity_quote")),
        "evidence": [
            _bounded_ref_dict(value)
            for value in (raw_evidence if isinstance(raw_evidence, list) else [])[:20]
            if isinstance(value, dict)
        ],
    }


def _bounded_ref_dict(value: dict[str, Any]) -> dict[str, str]:
    return {
        "kind": str(value.get("kind") or "")[:64],
        "id": str(value.get("id") or "")[:512],
        "path": str(value.get("path") or "")[:1_024],
        "timestamp": str(value.get("timestamp") or "")[:100],
        "content_hash": str(value.get("content_hash") or "")[:128],
    }


def _reference_summary(conn, ref: EvidenceRef) -> dict[str, Any]:
    availability = provenance_store.availability(conn, ref)
    integrity = "unverified"
    if ref.content_hash and ref.kind in {
        "observation",
        "timeline_block",
        "session",
        "memory_entry",
    }:
        integrity = "current" if provenance_store.is_current(conn, ref) else "changed"
    return {
        **_bounded_ref_dict(ref.to_dict()),
        "availability": str(availability)[:50],
        "integrity": integrity,
    }


def _trace_sources_bounded(
    conn,
    subject: EvidenceRef,
    *,
    max_depth: int,
    max_nodes: int,
) -> list[dict[str, Any]]:
    queue: list[tuple[EvidenceRef, int]] = [(subject, 0)]
    seen = {(subject.kind, subject.path, subject.id)}
    result: list[dict[str, Any]] = []
    while queue and len(result) < max_nodes:
        current, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for source in provenance_store.direct_sources(conn, current)[:100]:
            key = (source.kind, source.path, source.id)
            if key in seen:
                continue
            seen.add(key)
            result.append({"depth": depth + 1, "source": _reference_summary(conn, source)})
            if len(result) >= max_nodes:
                break
            queue.append((source, depth + 1))
    return result


def _require_visible_subject(conn, ref: EvidenceRef) -> None:
    if ref.kind == "memory_candidate":
        if (
            candidate_store.is_tombstoned(conn, kind="memory_candidate", artifact_id=ref.id)
            or candidate_store.get(conn, ref.id) is None
        ):
            raise KeyError(ref.id)
        return
    if ref.kind == "memory_entry" and candidate_store.is_tombstoned(
        conn, kind="memory_entry", artifact_id=ref.id, path=ref.path
    ):
        raise KeyError(ref.id)
    wrap_id = ref.id if ref.kind == "daily_wrap" else ref.path
    if ref.kind in {"daily_wrap", "daily_wrap_item", "daily_wrap_revision"} and (
        not wrap_id
        or candidate_store.is_tombstoned(conn, kind="daily_wrap", artifact_id=wrap_id)
        or daily_wrap_store.get_by_id(conn, wrap_id) is None
    ):
        raise KeyError(ref.id)
    if ref.kind not in {
        "observation",
        "timeline_block",
        "session",
        "memory_entry",
        "memory_candidate",
        "daily_wrap",
        "daily_wrap_item",
        "daily_wrap_revision",
    }:
        raise ValueError("unsupported provenance kind")


def _fields(
    params: dict[str, Any],
    *,
    required: set[str] | None = None,
    optional: set[str] | None = None,
) -> None:
    required = required or set()
    optional = optional or set()
    if set(params) - required - optional or required - set(params):
        raise BridgeError("INVALID_PARAMS", "The operation parameters are invalid.")


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("integer required")
    if value < minimum or value > maximum:
        raise ValueError("integer out of range")
    return value


def _bounded_string(value: object, maximum: int, *, nonempty: bool) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("bounded string required")
    if nonempty and not value.strip():
        raise ValueError("non-empty string required")
    return value


def _string_list(value: object, *, max_items: int, max_length: int) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError("bounded string list required")
    return [_bounded_string(item, max_length, nonempty=True) for item in value]


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("boolean required")
    return value


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "ok": False,
        "error": {"code": code, "message": message},
    }


if __name__ == "__main__":
    main()
