"""One-shot, capability-scoped JSON bridge for the native desktop shell."""

from __future__ import annotations

import base64
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
from .privacy.egress import privacy_egress_lock
from .prompt_rescue import store as prompt_rescue_store
from .prompt_rescue.selection import SelectionCaptureError, capture_selection
from .prompt_rescue.service import PromptRescueService
from .prompt_rescue.service import validate_config as validate_prompt_rescue
from .provenance import store as provenance_store
from .provenance.models import EvidenceRef
from .reply_rescue import store as reply_rescue_store
from .reply_rescue.service import ReplyRescueService
from .reply_rescue.service import validate_config as validate_reply_rescue
from .resume_rescue import review_store as resume_review_store
from .resume_rescue import rewrite_store as resume_rewrite_store
from .resume_rescue import store as resume_rescue_store
from .resume_rescue.native_export import PdfExportUnavailable
from .resume_rescue.rewrite import rewrite_proposal_digest
from .resume_rescue.rewrite_generation import ResumeRewriteEgressDenied
from .resume_rescue.service import ResumeRescueService
from .services.capture_control import PauseStateConflict, set_paused
from .services.context import ContextService
from .services.evidence import EvidenceResolver
from .services.memory import MemoryService, PurgeClosureUnverifiable, StalePurgePlan
from .services.memory_export import build_current_memory_export
from .services.snapshot import build_snapshot
from .store import files as files_store
from .store import fts
from .suggestions import store as suggestion_store
from .suggestions.service import SuggestionKernel

PROTOCOL_VERSION = 18
MAX_REQUEST_BYTES = 12 * 1024 * 1024
MAX_RESUME_DOCUMENT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BridgeError(Exception):
    code: str
    message: str
    exit_code: int = 2


def handle_request_bytes(payload: bytes) -> tuple[dict[str, Any], int]:
    """Validate and execute exactly one request without writing to stdout.

    The exception boundary deliberately encloses both privacy-fence acquisition
    and release.  Lock backend failures must use the same sanitized, one-line
    protocol response as operation failures instead of escaping the sidecar.
    """
    try:
        with privacy_egress_lock():
            return _handle_request_unfenced(payload)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
            return _error("BUSY", "The local store is busy; retry shortly."), 3
        return _error("INTERNAL_ERROR", "The local operation failed."), 1
    except Exception:  # noqa: BLE001 - lock failures use the sanitized protocol
        return _error("INTERNAL_ERROR", "The local operation failed."), 1


def _handle_request_unfenced(payload: bytes) -> tuple[dict[str, Any], int]:
    """Handle operation errors before they unwind through lock context managers."""
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
    except suggestion_store.SuggestionConflict:
        return _error("VERSION_CONFLICT", "The suggestion changed."), 2
    except prompt_rescue_store.PromptRescueConflict:
        return _error("VERSION_CONFLICT", "The Prompt Rescue job changed."), 2
    except reply_rescue_store.ReplyRescueConflict:
        return _error("VERSION_CONFLICT", "The Reply Rescue job changed."), 2
    except resume_rescue_store.ResumeRescueConflict:
        return _error("VERSION_CONFLICT", "The Résumé Rescue source changed."), 2
    except resume_rewrite_store.ResumeRewriteConflict:
        return _error("VERSION_CONFLICT", "The Résumé Rescue rewrite changed."), 2
    except resume_review_store.ResumeRewriteReviewConflict:
        return _error("VERSION_CONFLICT", "The Résumé Rescue review changed."), 2
    except ResumeRewriteEgressDenied:
        return _error(
            "EGRESS_NOT_AUTHORIZED",
            "The disclosed Résumé Rescue provider is not authorized.",
        ), 2
    except PdfExportUnavailable:
        return _error(
            "EXPORT_UNAVAILABLE",
            "Pinned PDF export is unavailable on this development host.",
        ), 2
    except SelectionCaptureError as exc:
        return _selection_error(exc.code), 2
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
        raise BridgeError("REQUEST_TOO_LARGE", "The bridge request exceeds 12 MiB.")
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
        "memory.export": _memory_export,
        "wrap.get": _wrap_get,
        "suggestion.transition": _suggestion_transition,
        "prompt_rescue.get": _prompt_rescue_get,
        "prompt_rescue.queue": _prompt_rescue_queue,
        "prompt_rescue.queue_selection": _prompt_rescue_queue_selection,
        "prompt_rescue.edit": _prompt_rescue_edit,
        "prompt_rescue.retry": _prompt_rescue_retry,
        "prompt_rescue.delete": _prompt_rescue_delete,
        "reply_rescue.get": _reply_rescue_get,
        "reply_rescue.queue": _reply_rescue_queue,
        "reply_rescue.queue_selection": _reply_rescue_queue_selection,
        "reply_rescue.edit": _reply_rescue_edit,
        "reply_rescue.retry": _reply_rescue_retry,
        "reply_rescue.delete": _reply_rescue_delete,
        "resume_rescue.state": _resume_rescue_state,
        "resume_rescue.save_profile": _resume_rescue_save_profile,
        "resume_rescue.save_opportunity": _resume_rescue_save_opportunity,
        "resume_rescue.replace_opportunity": _resume_rescue_replace_opportunity,
        "resume_rescue.compose_exact": _resume_rescue_compose_exact,
        "resume_rescue.preview": _resume_rescue_preview,
        "resume_rescue.preview_pdf": _resume_rescue_preview_pdf,
        "resume_rescue.review_json": _resume_rescue_review_json,
        "resume_rescue.admit_json": _resume_rescue_admit_json,
        "resume_rescue.export_json": _resume_rescue_export_json,
        "resume_rescue.export_docx": _resume_rescue_export_docx,
        "resume_rescue.export_pdf": _resume_rescue_export_pdf,
        "resume_rescue.review_document": _resume_rescue_review_document,
        "resume_rescue.admit_document": _resume_rescue_admit_document,
        "resume_rescue.queue_rewrite": _resume_rescue_queue_rewrite,
        "resume_rescue.retry_rewrite": _resume_rescue_retry_rewrite,
        "resume_rescue.delete_rewrite": _resume_rescue_delete_rewrite,
        "resume_rescue.decide_rewrite": _resume_rescue_decide_rewrite,
        "resume_rescue.restore_rewrite": _resume_rescue_restore_rewrite,
        "resume_rescue.preview_rewrite": _resume_rescue_preview_rewrite,
        "resume_rescue.preview_rewrite_pdf": _resume_rescue_preview_rewrite_pdf,
        "resume_rescue.export_rewrite_json": _resume_rescue_export_rewrite_json,
        "resume_rescue.export_rewrite_docx": _resume_rescue_export_rewrite_docx,
        "resume_rescue.export_rewrite_pdf": _resume_rescue_export_rewrite_pdf,
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
    _fields(
        params,
        optional={
            "timeline_limit",
            "candidate_limit",
            "memory_limit",
            "wrap_limit",
            "suggestion_limit",
            "prompt_rescue_limit",
            "reply_rescue_limit",
        },
    )
    timeline_limit = _bounded_int(params.get("timeline_limit", 12), 0, 24)
    candidate_limit = _bounded_int(params.get("candidate_limit", 50), 0, 100)
    memory_limit = _bounded_int(params.get("memory_limit", 200), 0, 500)
    wrap_limit = _bounded_int(params.get("wrap_limit", 14), 0, 30)
    suggestion_limit = _bounded_int(params.get("suggestion_limit", 20), 0, 50)
    prompt_rescue_limit = _bounded_int(params.get("prompt_rescue_limit", 20), 0, 50)
    reply_rescue_limit = _bounded_int(params.get("reply_rescue_limit", 20), 0, 50)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        return build_snapshot(
            conn,
            cfg,
            timeline_limit=timeline_limit,
            candidate_limit=candidate_limit,
            memory_limit=memory_limit,
            wrap_limit=wrap_limit,
            suggestion_limit=suggestion_limit,
            prompt_rescue_limit=prompt_rescue_limit,
            reply_rescue_limit=reply_rescue_limit,
        )


def _prompt_rescue_get(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"job_id"})
    job_id = _bounded_string(params["job_id"], 128, nonempty=True)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        job = PromptRescueService(conn, cfg).get(job_id)
        if job is None:
            raise KeyError(job_id)
        return {"job": _prompt_rescue_payload(job)}


def _prompt_rescue_queue(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={
            "rough_prompt",
            "target",
            "audience",
            "constraints",
            "desired_format",
        },
    )
    rough_prompt = _bounded_string(params["rough_prompt"], 20_000, nonempty=True)
    target = _bounded_string(params["target"], 500, nonempty=False)
    audience = _bounded_string(params["audience"], 500, nonempty=False)
    constraints = _string_list(params["constraints"], max_items=20, max_length=500)
    desired_format = _bounded_string(params["desired_format"], 500, nonempty=False)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        job, created = PromptRescueService(conn, cfg).queue(
            rough_prompt=rough_prompt,
            target=target,
            audience=audience,
            constraints=constraints,
            desired_format=desired_format,
        )
        return {"job": _prompt_rescue_payload(job), "created": created}


def _prompt_rescue_queue_selection(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params)
    cfg = config_mod.load()
    validate_prompt_rescue(cfg)
    if not cfg.prompt_rescue.enabled:
        raise ValueError("prompt rescue is disabled")
    receipt = capture_selection(cfg)
    with fts.cursor() as conn:
        job, created = PromptRescueService(conn, cfg).queue_selection(receipt)
        return {"job": _prompt_rescue_payload(job), "created": created}


def _prompt_rescue_edit(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"job_id", "expected_version", "improved_prompt"})
    job_id = _bounded_string(params["job_id"], 128, nonempty=True)
    expected_version = _bounded_int(params["expected_version"], 1, 2_147_483_647)
    improved_prompt = _bounded_string(params["improved_prompt"], 30_000, nonempty=True)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        job = PromptRescueService(conn, cfg).edit(
            job_id,
            expected_version=expected_version,
            improved_prompt=improved_prompt,
        )
        return {"job": _prompt_rescue_payload(job)}


def _prompt_rescue_retry(params: dict[str, Any]) -> dict[str, Any]:
    job_id, expected_version = _prompt_rescue_cas_params(params)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        job = PromptRescueService(conn, cfg).retry(
            job_id,
            expected_version=expected_version,
        )
        return {"job": _prompt_rescue_payload(job)}


def _prompt_rescue_delete(params: dict[str, Any]) -> dict[str, Any]:
    job_id, expected_version = _prompt_rescue_cas_params(params)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        PromptRescueService(conn, cfg).delete(
            job_id,
            expected_version=expected_version,
        )
        return {"job_id": job_id, "deleted": True}


def _reply_rescue_get(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"job_id"})
    job_id = _bounded_string(params["job_id"], 128, nonempty=True)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        job = ReplyRescueService(conn, cfg).get(job_id)
        if job is None:
            raise KeyError(job_id)
        return {"job": _reply_rescue_payload(job)}


def _reply_rescue_queue(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={
            "conversation_text",
            "participants",
            "intended_recipients",
            "reply_mode",
            "goal",
            "tone",
            "style_instructions",
            "commitments",
        },
    )
    conversation_text = _bounded_string(params["conversation_text"], 50_000, nonempty=True)
    participants = _string_list(params["participants"], max_items=50, max_length=1_000)
    intended_recipients = _string_list(
        params["intended_recipients"], max_items=50, max_length=1_000
    )
    reply_mode = _bounded_string(params["reply_mode"], 20, nonempty=True)
    goal = _bounded_string(params["goal"], 1_000, nonempty=False)
    tone = _bounded_string(params["tone"], 1_000, nonempty=False)
    style_instructions = _string_list(params["style_instructions"], max_items=20, max_length=1_000)
    commitments = _string_list(params["commitments"], max_items=20, max_length=1_000)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        job, created = ReplyRescueService(conn, cfg).queue_manual(
            conversation_text=conversation_text,
            participants=participants,
            intended_recipients=intended_recipients,
            reply_mode=reply_mode,
            goal=goal,
            tone=tone,
            style_instructions=style_instructions,
            commitments=commitments,
        )
        return {"job": _reply_rescue_payload(job), "created": created}


def _reply_rescue_queue_selection(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params)
    cfg = config_mod.load()
    validate_reply_rescue(cfg)
    if not cfg.reply_rescue.enabled:
        raise ValueError("reply rescue is disabled")
    receipt = capture_selection(cfg)
    with fts.cursor() as conn:
        job, created = ReplyRescueService(conn, cfg).queue_selection(receipt)
        return {"job": _reply_rescue_payload(job), "created": created}


def _reply_rescue_edit(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"job_id", "expected_version", "reply_body"})
    job_id = _bounded_string(params["job_id"], 128, nonempty=True)
    expected_version = _bounded_int(params["expected_version"], 1, 2_147_483_647)
    reply_body = _bounded_string(params["reply_body"], 30_000, nonempty=True)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        job = ReplyRescueService(conn, cfg).edit(
            job_id,
            expected_version=expected_version,
            reply_body=reply_body,
        )
        return {"job": _reply_rescue_payload(job)}


def _reply_rescue_retry(params: dict[str, Any]) -> dict[str, Any]:
    job_id, expected_version = _reply_rescue_cas_params(params)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        job = ReplyRescueService(conn, cfg).retry(job_id, expected_version=expected_version)
        return {"job": _reply_rescue_payload(job)}


def _reply_rescue_delete(params: dict[str, Any]) -> dict[str, Any]:
    job_id, expected_version = _reply_rescue_cas_params(params)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        ReplyRescueService(conn, cfg).delete(job_id, expected_version=expected_version)
        return {"job_id": job_id, "deleted": True}


def _resume_rescue_state(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        optional={
            "profile_limit",
            "opportunity_limit",
            "projection_limit",
            "rewrite_limit",
            "rewrite_version_limit",
        },
    )
    profile_limit = _bounded_int(params.get("profile_limit", 20), 1, 50)
    opportunity_limit = _bounded_int(params.get("opportunity_limit", 20), 1, 50)
    projection_limit = _bounded_int(params.get("projection_limit", 20), 1, 50)
    rewrite_limit = _bounded_int(params.get("rewrite_limit", 20), 1, 50)
    rewrite_version_limit = _bounded_int(params.get("rewrite_version_limit", 20), 1, 50)
    cfg = config_mod.load()
    if (
        type(cfg.resume_rescue.enabled) is not bool
        or type(cfg.resume_rescue.rewrite_enabled) is not bool
    ):
        raise ValueError("resume rescue enabled state is invalid")
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, cfg)
        rewrites = service.list_rewrites(limit=rewrite_limit)
        return {
            "enabled": cfg.resume_rescue.enabled,
            "rewrite_enabled": cfg.resume_rescue.rewrite_enabled,
            "rewrite_provider": (
                service.rewrite_provider_summary()
                if cfg.resume_rescue.enabled and cfg.resume_rescue.rewrite_enabled
                else None
            ),
            "profiles": [
                _resume_profile_payload(value)
                for value in service.list_profiles(limit=profile_limit)
            ],
            "opportunities": [
                _resume_opportunity_payload(value)
                for value in service.list_opportunities(limit=opportunity_limit)
            ],
            "projections": [
                _resume_projection_payload(value)
                for value in service.list_projections(limit=projection_limit)
            ],
            "rewrites": [
                _resume_rewrite_state_payload(
                    service,
                    value,
                    version_limit=rewrite_version_limit,
                )
                for value in rewrites
            ],
        }


def _resume_rescue_save_profile(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={"profile_id", "display_name", "locale", "facts", "conflicts"},
        optional={"expected_version"},
    )
    expected = params.get("expected_version")
    expected_version = _bounded_int(expected, 1, 2_147_483_647) if expected is not None else None
    cfg = config_mod.load()
    with fts.cursor() as conn:
        profile, created = ResumeRescueService(conn, cfg).save_profile(
            profile_id=_bounded_string(params["profile_id"], 128, nonempty=True),
            display_name=_bounded_string(params["display_name"], 512, nonempty=True),
            locale=_bounded_string(params["locale"], 64, nonempty=False),
            facts=_object_list(params["facts"], 2_000),
            conflicts=_object_list(params["conflicts"], 500),
            expected_version=expected_version,
        )
        return {"profile": _resume_profile_payload(profile), "created": created}


def _resume_rescue_save_opportunity(params: dict[str, Any]) -> dict[str, Any]:
    values = _resume_opportunity_params(params, replacing=False)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        opportunity, created = ResumeRescueService(conn, cfg).save_opportunity(**values)
        return {
            "opportunity": _resume_opportunity_payload(opportunity),
            "created": created,
        }


def _resume_rescue_replace_opportunity(params: dict[str, Any]) -> dict[str, Any]:
    values = _resume_opportunity_params(params, replacing=True)
    opportunity_id = values.pop("opportunity_id")
    expected_digest = values.pop("expected_digest")
    cfg = config_mod.load()
    with fts.cursor() as conn:
        opportunity, created = ResumeRescueService(conn, cfg).replace_opportunity(
            opportunity_id,
            expected_digest=expected_digest,
            **values,
        )
        return {
            "opportunity": _resume_opportunity_payload(opportunity),
            "created": created,
        }


def _resume_rescue_compose_exact(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={"profile_id", "opportunity_id", "sections", "requirements"},
    )
    cfg = config_mod.load()
    with fts.cursor() as conn:
        projection, created = ResumeRescueService(conn, cfg).compose_exact(
            profile_id=_bounded_string(params["profile_id"], 128, nonempty=True),
            opportunity_id=_bounded_string(params["opportunity_id"], 128, nonempty=True),
            sections=_object_list(params["sections"], 8),
            requirements=_object_list(params["requirements"], 200),
        )
        return {
            "projection": _resume_projection_payload(projection),
            "created": created,
        }


def _resume_rescue_preview(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"projection_id"})
    cfg = config_mod.load()
    with fts.cursor() as conn:
        preview = ResumeRescueService(conn, cfg).preview(
            _bounded_string(params["projection_id"], 128, nonempty=True)
        )
        return {"preview": preview.to_dict()}


def _resume_rescue_preview_pdf(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"projection_id", "expected_preview_document_digest"})
    cfg = config_mod.load()
    with fts.cursor() as conn:
        preview = ResumeRescueService(conn, cfg).preview_pdf(
            _bounded_string(params["projection_id"], 128, nonempty=True),
            expected_preview_document_digest=_bounded_string(
                params["expected_preview_document_digest"], 64, nonempty=True
            ),
        )
        return {"pdf_preview": preview.to_dict()}


def _resume_rescue_review_json(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"source_text"})
    cfg = config_mod.load()
    with fts.cursor() as conn:
        review = ResumeRescueService(conn, cfg).review_json_resume(
            _bounded_string(params["source_text"], 500_000, nonempty=True)
        )
        return {"review": review.to_dict()}


def _resume_rescue_review_document(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"source_base64", "source_format"})
    source = _resume_document_source(params["source_base64"])
    source_format = _resume_document_format(params["source_format"])
    cfg = config_mod.load()
    with fts.cursor() as conn:
        review = ResumeRescueService(conn, cfg).review_document(source, source_format=source_format)
        return {"review": review.to_dict()}


def _resume_rescue_admit_json(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={
            "source_text",
            "expected_review_digest",
            "profile_id",
            "display_name",
            "locale",
            "selections",
        },
        optional={"expected_version"},
    )
    raw_expected_version = params.get("expected_version")
    expected_version = (
        _bounded_int(raw_expected_version, 0, 2_147_483_647)
        if raw_expected_version is not None
        else None
    )
    cfg = config_mod.load()
    with fts.cursor() as conn:
        profile, created = ResumeRescueService(conn, cfg).admit_json_resume(
            source_text=_bounded_string(params["source_text"], 500_000, nonempty=True),
            expected_review_digest=_bounded_string(
                params["expected_review_digest"], 64, nonempty=True
            ),
            profile_id=_bounded_string(params["profile_id"], 128, nonempty=True),
            display_name=_bounded_string(params["display_name"], 512, nonempty=True),
            locale=_bounded_string(params["locale"], 64, nonempty=False),
            selections=_object_list(params["selections"], 2_000),
            expected_version=expected_version,
        )
        return {"profile": _resume_profile_payload(profile), "created": created}


def _resume_rescue_admit_document(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={
            "source_base64",
            "source_format",
            "expected_review_digest",
            "profile_id",
            "display_name",
            "locale",
            "selections",
        },
        optional={"expected_version"},
    )
    raw_expected_version = params.get("expected_version")
    expected_version = (
        _bounded_int(raw_expected_version, 0, 2_147_483_647)
        if raw_expected_version is not None
        else None
    )
    cfg = config_mod.load()
    with fts.cursor() as conn:
        profile, created = ResumeRescueService(conn, cfg).admit_document(
            source=_resume_document_source(params["source_base64"]),
            source_format=_resume_document_format(params["source_format"]),
            expected_review_digest=_bounded_string(
                params["expected_review_digest"], 64, nonempty=True
            ),
            profile_id=_bounded_string(params["profile_id"], 128, nonempty=True),
            display_name=_bounded_string(params["display_name"], 512, nonempty=True),
            locale=_bounded_string(params["locale"], 64, nonempty=False),
            selections=_object_list(params["selections"], 2_000),
            expected_version=expected_version,
        )
        return {"profile": _resume_profile_payload(profile), "created": created}


def _resume_rescue_export_json(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"projection_id"})
    cfg = config_mod.load()
    with fts.cursor() as conn:
        exported = ResumeRescueService(conn, cfg).export_json_resume(
            _bounded_string(params["projection_id"], 128, nonempty=True)
        )
        return {"export": exported.to_dict()}


def _resume_rescue_export_docx(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"projection_id", "expected_preview_document_digest"})
    cfg = config_mod.load()
    with fts.cursor() as conn:
        exported = ResumeRescueService(conn, cfg).export_docx(
            _bounded_string(params["projection_id"], 128, nonempty=True),
            expected_preview_document_digest=_bounded_string(
                params["expected_preview_document_digest"], 64, nonempty=True
            ),
        )
        return {
            "export": {
                **exported.metadata(),
                "content_base64": base64.b64encode(exported.content).decode("ascii"),
            }
        }


def _resume_rescue_export_pdf(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"projection_id", "expected_preview_document_digest"})
    cfg = config_mod.load()
    with fts.cursor() as conn:
        exported = ResumeRescueService(conn, cfg).export_pdf(
            _bounded_string(params["projection_id"], 128, nonempty=True),
            expected_preview_document_digest=_bounded_string(
                params["expected_preview_document_digest"], 64, nonempty=True
            ),
        )
        return {
            "export": {
                **exported.metadata(),
                "content_base64": base64.b64encode(exported.content).decode("ascii"),
            }
        }


def _resume_rescue_queue_rewrite(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={
            "projection_id",
            "expected_artifact_digest",
            "expected_model_identity",
            "expected_provider_location",
            "remote_egress_authorized",
        },
    )
    cfg = config_mod.load()
    with fts.cursor() as conn:
        job, created = ResumeRescueService(conn, cfg).queue_rewrite(
            _bounded_string(params["projection_id"], 128, nonempty=True),
            expected_artifact_digest=_bounded_string(
                params["expected_artifact_digest"], 64, nonempty=True
            ),
            expected_model_identity=_bounded_string(
                params["expected_model_identity"], 256, nonempty=True
            ),
            expected_provider_location=_bounded_string(
                params["expected_provider_location"], 50, nonempty=True
            ),
            remote_egress_authorized=_strict_bool(params["remote_egress_authorized"]),
        )
        service = ResumeRescueService(conn, cfg)
        return {
            "rewrite": _resume_rewrite_state_payload(service, job, version_limit=20),
            "created": created,
        }


def _resume_rescue_retry_rewrite(params: dict[str, Any]) -> dict[str, Any]:
    job_id, expected_version = _resume_rewrite_cas_params(params)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, cfg)
        job = service.retry_rewrite(job_id, expected_version=expected_version)
        return {"rewrite": _resume_rewrite_state_payload(service, job, version_limit=20)}


def _resume_rescue_delete_rewrite(params: dict[str, Any]) -> dict[str, Any]:
    job_id, expected_version = _resume_rewrite_cas_params(params)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        ResumeRescueService(conn, cfg).delete_rewrite(job_id, expected_version=expected_version)
        return {"job_id": job_id, "deleted": True}


def _resume_rescue_decide_rewrite(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={
            "job_id",
            "proposal_id",
            "expected_proposal_digest",
            "expected_job_version",
            "expected_head_id",
            "expected_artifact_digest",
            "decision",
        },
    )
    cfg = config_mod.load()
    with fts.cursor() as conn:
        version, created = ResumeRescueService(conn, cfg).decide_rewrite(
            _bounded_string(params["job_id"], 128, nonempty=True),
            proposal_id=_bounded_string(params["proposal_id"], 128, nonempty=True),
            expected_proposal_digest=_bounded_string(
                params["expected_proposal_digest"], 64, nonempty=True
            ),
            expected_job_version=_bounded_int(params["expected_job_version"], 1, 2_147_483_647),
            expected_head_id=_bounded_string(params["expected_head_id"], 128, nonempty=False),
            expected_artifact_digest=_bounded_string(
                params["expected_artifact_digest"], 64, nonempty=True
            ),
            decision=_bounded_string(params["decision"], 20, nonempty=True),
        )
        return {"version": _resume_rewrite_version_payload(version), "created": created}


def _resume_rescue_restore_rewrite(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={
            "target_version_id",
            "expected_head_id",
            "expected_artifact_digest",
        },
    )
    cfg = config_mod.load()
    with fts.cursor() as conn:
        version = ResumeRescueService(conn, cfg).restore_rewrite(
            target_version_id=_bounded_string(params["target_version_id"], 128, nonempty=True),
            expected_head_id=_bounded_string(params["expected_head_id"], 128, nonempty=True),
            expected_artifact_digest=_bounded_string(
                params["expected_artifact_digest"], 64, nonempty=True
            ),
        )
        return {"version": _resume_rewrite_version_payload(version)}


def _resume_rescue_preview_rewrite(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"version_id"})
    cfg = config_mod.load()
    with fts.cursor() as conn:
        preview = ResumeRescueService(conn, cfg).preview_rewrite(
            _bounded_string(params["version_id"], 128, nonempty=True)
        )
        return {"preview": preview.to_dict()}


def _resume_rescue_preview_rewrite_pdf(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"version_id", "expected_preview_document_digest"})
    cfg = config_mod.load()
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, cfg)
        version_id = _bounded_string(params["version_id"], 128, nonempty=True)
        expected = _bounded_string(params["expected_preview_document_digest"], 64, nonempty=True)
        preview = service.preview_rewrite_pdf(version_id, expected_preview_document_digest=expected)
        return {"pdf_preview": preview.to_dict()}


def _resume_rescue_export_rewrite_json(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"version_id"})
    cfg = config_mod.load()
    with fts.cursor() as conn:
        exported = ResumeRescueService(conn, cfg).export_rewrite_json(
            _bounded_string(params["version_id"], 128, nonempty=True)
        )
        return {"export": exported.to_dict()}


def _resume_rescue_export_rewrite_docx(params: dict[str, Any]) -> dict[str, Any]:
    return _resume_rescue_export_rewrite_native(params, source_format="docx")


def _resume_rescue_export_rewrite_pdf(params: dict[str, Any]) -> dict[str, Any]:
    return _resume_rescue_export_rewrite_native(params, source_format="pdf")


def _resume_rescue_export_rewrite_native(
    params: dict[str, Any], *, source_format: str
) -> dict[str, Any]:
    _fields(params, required={"version_id", "expected_preview_document_digest"})
    cfg = config_mod.load()
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, cfg)
        version_id = _bounded_string(params["version_id"], 128, nonempty=True)
        expected = _bounded_string(params["expected_preview_document_digest"], 64, nonempty=True)
        exported = (
            service.export_rewrite_docx(version_id, expected_preview_document_digest=expected)
            if source_format == "docx"
            else service.export_rewrite_pdf(version_id, expected_preview_document_digest=expected)
        )
        return {
            "export": {
                **exported.metadata(),
                "content_base64": base64.b64encode(exported.content).decode("ascii"),
            }
        }


def _resume_opportunity_params(params: dict[str, Any], *, replacing: bool) -> dict[str, Any]:
    common = {
        "employer",
        "title",
        "source_text",
        "source_url",
        "priorities",
        "locale",
    }
    required = common | ({"opportunity_id", "expected_digest"} if replacing else set())
    _fields(params, required=required, optional={"captured_at"})
    result: dict[str, Any] = {
        "employer": _bounded_string(params["employer"], 512, nonempty=True),
        "title": _bounded_string(params["title"], 512, nonempty=True),
        "source_text": _bounded_string(params["source_text"], 50_000, nonempty=True),
        "source_url": _bounded_string(params["source_url"], 4_096, nonempty=False),
        "priorities": _string_list(params["priorities"], max_items=50, max_length=2_000),
        "locale": _bounded_string(params["locale"], 64, nonempty=False),
        "captured_at": (
            _bounded_string(params["captured_at"], 100, nonempty=True)
            if "captured_at" in params
            else None
        ),
    }
    if replacing:
        result["opportunity_id"] = _bounded_string(params["opportunity_id"], 128, nonempty=True)
        result["expected_digest"] = _bounded_string(params["expected_digest"], 64, nonempty=True)
    return result


def _suggestion_transition(params: dict[str, Any]) -> dict[str, Any]:
    _fields(
        params,
        required={"suggestion_id", "expected_version", "status"},
        optional={"reason"},
    )
    suggestion_id = _bounded_string(params["suggestion_id"], 128, nonempty=True)
    expected_version = _bounded_int(params["expected_version"], 1, 2_147_483_647)
    status = _bounded_string(params["status"], 50, nonempty=True)
    reason = _bounded_string(params.get("reason", ""), 1_000, nonempty=False)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        updated = SuggestionKernel(conn, cfg).transition(
            suggestion_id,
            expected_version=expected_version,
            to_status=status,
            reason=reason,
        )
        return {"suggestion": _suggestion_payload(updated)}


def _candidate_get(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"candidate_id"})
    candidate_id = _bounded_string(params["candidate_id"], 128, nonempty=True)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg)
        service.resume_pending_purges()
        candidate = service.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        ref = EvidenceRef(kind="memory_candidate", id=candidate_id)
        _require_visible_subject(conn, cfg, ref)
        evidence = [
            _reference_summary(conn, source)
            for source in provenance_store.direct_sources(conn, ref)[:100]
        ]
        return {"candidate": _candidate_payload(candidate), "evidence": evidence}


def _memory_export(params: dict[str, Any]) -> dict[str, Any]:
    _fields(params, required={"format"})
    export_format = _bounded_string(params["format"], 20, nonempty=True)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens).resume_pending_purges()
        return {"export": build_current_memory_export(conn, cfg, format=export_format)}


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
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg)
        service.resume_pending_purges()
        _require_visible_subject(conn, cfg, EvidenceRef(kind="memory_candidate", id=candidate_id))
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
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg)
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
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg)
        service.resume_pending_purges()
        _require_visible_subject(conn, cfg, EvidenceRef(kind="memory_candidate", id=candidate_id))
        rejected = service.reject_candidate(
            candidate_id, expected_version=expected_version, reason=reason
        )
        return {"candidate": _candidate_payload(rejected)}


def _candidate_forget_preview(params: dict[str, Any]) -> dict[str, Any]:
    candidate_id, expected_version = _candidate_cas_params(params)
    cfg = config_mod.load()
    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg)
        service.resume_pending_purges()
        _require_visible_subject(conn, cfg, EvidenceRef(kind="memory_candidate", id=candidate_id))
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
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg)
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
        if row is None or not ContextService(conn, cfg).daily_wrap_allowed(
            row.id, expected_row=row
        ):
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
    cfg = config_mod.load()
    with fts.cursor() as conn, files_store.review_operation_lock():
        _require_visible_subject(conn, cfg, ref)
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


def _prompt_rescue_cas_params(params: dict[str, Any]) -> tuple[str, int]:
    _fields(params, required={"job_id", "expected_version"})
    return (
        _bounded_string(params["job_id"], 128, nonempty=True),
        _bounded_int(params["expected_version"], 1, 2_147_483_647),
    )


def _reply_rescue_cas_params(params: dict[str, Any]) -> tuple[str, int]:
    _fields(params, required={"job_id", "expected_version"})
    return (
        _bounded_string(params["job_id"], 128, nonempty=True),
        _bounded_int(params["expected_version"], 1, 2_147_483_647),
    )


def _resume_rewrite_cas_params(params: dict[str, Any]) -> tuple[str, int]:
    _fields(params, required={"job_id", "expected_version"})
    return (
        _bounded_string(params["job_id"], 128, nonempty=True),
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
        "subject_key": str(candidate.subject_key)[:200],
        "assertion_kind": str(candidate.assertion_kind)[:50],
        "valid_from": str(candidate.valid_from)[:100],
        "valid_to": str(candidate.valid_to)[:100],
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


def _suggestion_payload(suggestion) -> dict[str, Any]:
    return {
        "id": str(suggestion.id)[:128],
        "workflow": str(suggestion.workflow)[:100],
        "status": str(suggestion.status)[:50],
        "title": str(suggestion.title)[:160],
        "summary": str(suggestion.summary)[:1_000],
        "artifact": suggestion.artifact,
        "score": float(suggestion.score),
        "version": int(suggestion.version),
        "detected_at": str(suggestion.detected_at)[:100],
        "expires_at": str(suggestion.expires_at)[:100],
        "feedback_reason": str(suggestion.feedback_reason)[:1_000],
    }


def _prompt_rescue_payload(job) -> dict[str, Any]:
    output = job.output if isinstance(job.output, dict) else None
    return {
        "id": str(job.id)[:128],
        "status": str(job.status)[:50],
        "source_kind": str(job.source_kind)[:50],
        "source_binding": _bounded_prompt_rescue_binding(job.source_binding),
        "rough_prompt": str(job.rough_prompt)[:20_000],
        "target": str(job.target)[:500],
        "audience": str(job.audience)[:500],
        "constraints": [str(value)[:500] for value in job.constraints[:20]],
        "desired_format": str(job.desired_format)[:500],
        "model_identity": str(job.model_identity)[:256],
        "provider_location": str(job.provider_location)[:50],
        "output": _bounded_prompt_rescue_output(output),
        "output_edited": bool(job.output_edited),
        "error_code": str(job.error_code)[:50],
        "attempt_count": int(job.attempt_count),
        "created_at": str(job.created_at)[:100],
        "updated_at": str(job.updated_at)[:100],
        "version": int(job.version),
    }


def _bounded_prompt_rescue_binding(binding: object) -> dict[str, Any]:
    if not isinstance(binding, dict) or not binding:
        return {}
    return {
        "schema_version": int(binding.get("schema_version") or 0),
        "captured_at": str(binding.get("captured_at") or "")[:100],
        "app_name": str(binding.get("app_name") or "")[:512],
        "bundle_id": str(binding.get("bundle_id") or "")[:512],
        "pid": int(binding.get("pid") or 0),
        "window_title": str(binding.get("window_title") or "")[:512],
        "element_role": str(binding.get("element_role") or "")[:128],
        "element_subrole": str(binding.get("element_subrole") or "")[:128],
        "selection_location": int(binding.get("selection_location") or 0),
        "selection_length": int(binding.get("selection_length") or 0),
    }


def _bounded_prompt_rescue_output(output: dict[str, Any] | None) -> dict[str, Any] | None:
    if output is None:
        return None
    return {
        "schema_version": int(output.get("schema_version") or 0),
        "workflow": str(output.get("workflow") or "")[:50],
        "action_capability": str(output.get("action_capability") or "")[:50],
        "improved_prompt": str(output.get("improved_prompt") or "")[:30_000],
        "assumptions": [
            str(value)[:1_000]
            for value in (
                output.get("assumptions") if isinstance(output.get("assumptions"), list) else []
            )[:20]
        ],
        "missing_context": [
            str(value)[:1_000]
            for value in (
                output.get("missing_context")
                if isinstance(output.get("missing_context"), list)
                else []
            )[:20]
        ],
        "changes": [
            str(value)[:1_000]
            for value in (output.get("changes") if isinstance(output.get("changes"), list) else [])[
                :20
            ]
        ],
    }


def _reply_rescue_payload(job) -> dict[str, Any]:
    source = job.source if isinstance(job.source, dict) else {}
    output = job.output if isinstance(job.output, dict) else None
    bounded_source = {
        "schema_version": int(source.get("schema_version") or 0),
        "identity_assurance": str(source.get("identity_assurance") or "")[:50],
        "conversation_text": str(source.get("conversation_text") or "")[:50_000],
        "participants": _bounded_output_strings(source.get("participants"), 50),
        "intended_recipients": _bounded_output_strings(source.get("intended_recipients"), 50),
        "reply_mode": str(source.get("reply_mode") or "")[:20],
        "goal": str(source.get("goal") or "")[:1_000],
        "tone": str(source.get("tone") or "")[:1_000],
        "style_instructions": _bounded_output_strings(source.get("style_instructions"), 20),
        "commitments": _bounded_output_strings(source.get("commitments"), 20),
    }
    if job.source_kind == "macos_selection":
        bounded_source["selection_binding"] = _bounded_prompt_rescue_binding(
            source.get("selection_binding")
        )
    return {
        "id": str(job.id)[:128],
        "status": str(job.status)[:50],
        "source_kind": str(job.source_kind)[:50],
        "source": bounded_source,
        "model_identity": str(job.model_identity)[:256],
        "provider_location": str(job.provider_location)[:50],
        "output": _bounded_reply_rescue_output(output),
        "output_edited": bool(job.output_edited),
        "error_code": str(job.error_code)[:50],
        "attempt_count": int(job.attempt_count),
        "created_at": str(job.created_at)[:100],
        "updated_at": str(job.updated_at)[:100],
        "version": int(job.version),
    }


def _bounded_reply_rescue_output(output: dict[str, Any] | None) -> dict[str, Any] | None:
    if output is None:
        return None
    claims = output.get("claims") if isinstance(output.get("claims"), list) else []
    return {
        "schema_version": int(output.get("schema_version") or 0),
        "workflow": str(output.get("workflow") or "")[:50],
        "action_capability": str(output.get("action_capability") or "")[:50],
        "reply_body": str(output.get("reply_body") or "")[:30_000],
        "addressed_questions": _bounded_output_strings(output.get("addressed_questions"), 30),
        "unresolved_questions": _bounded_output_strings(output.get("unresolved_questions"), 30),
        "assumptions": _bounded_output_strings(output.get("assumptions"), 30),
        "warnings": _bounded_output_strings(output.get("warnings"), 30),
        "claims": [
            {
                "text": str(value.get("text") or "")[:1_000],
                "support": str(value.get("support") or "")[:50],
            }
            for value in claims[:50]
            if isinstance(value, dict)
        ],
    }


def _resume_profile_payload(value: resume_rescue_store.ProfileVersion) -> dict[str, Any]:
    return {
        "id": value.profile_id,
        "version": value.version,
        "digest": value.digest,
        "created_at": value.created_at,
        "profile": value.profile,
    }


def _resume_opportunity_payload(
    value: resume_rescue_store.OpportunitySnapshot,
) -> dict[str, Any]:
    return {
        "id": value.id,
        "digest": value.digest,
        "created_at": value.created_at,
        "snapshot": value.snapshot,
    }


def _resume_projection_payload(
    value: resume_rescue_store.ResumeProjection,
) -> dict[str, Any]:
    return {
        "id": value.id,
        "profile_id": value.profile_id,
        "profile_version": value.profile_version,
        "profile_digest": value.profile_digest,
        "opportunity_id": value.opportunity_id,
        "opportunity_digest": value.opportunity_digest,
        "request": value.request,
        "artifact": value.artifact,
        "artifact_digest": value.artifact_digest,
        "created_at": value.created_at,
    }


def _resume_rewrite_state_payload(
    service: ResumeRescueService,
    value: resume_rewrite_store.ResumeRewriteJob,
    *,
    version_limit: int,
) -> dict[str, Any]:
    head = service.get_rewrite_head(value.id)
    versions = service.list_rewrite_versions(value.id, limit=version_limit)
    proposals = value.output.get("proposals", []) if value.output is not None else []
    return {
        "id": value.id,
        "status": value.status,
        "projection_id": value.projection_id,
        "projection_artifact_digest": value.projection_artifact_digest,
        "model_identity": value.model_identity,
        "provider_location": value.provider_location,
        "remote_egress_authorized": value.remote_egress_authorized,
        "proposals": [
            {
                "proposal_id": proposal["proposal_id"],
                "proposal_digest": rewrite_proposal_digest(proposal),
                "operation": proposal["operation"],
                "section": proposal["section"],
                "fact_id": proposal["fact_id"],
                "original_text": proposal["original_text"],
                "proposed_text": proposal["proposed_text"],
                "rationale": proposal["rationale"],
                "requirement_ids": proposal["requirement_ids"],
                "evidence_fragments": proposal["evidence_fragments"],
            }
            for proposal in proposals[:200]
        ],
        "output_digest": value.output_digest,
        "error_code": value.error_code,
        "attempt_count": value.attempt_count,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
        "version": value.version,
        "head": _resume_rewrite_version_payload(head) if head is not None else None,
        "versions": [_resume_rewrite_version_payload(item) for item in versions],
    }


def _resume_rewrite_version_payload(
    value: resume_review_store.ResumeRewriteVersion,
) -> dict[str, Any]:
    return {
        "id": value.id,
        "lineage_id": value.lineage_id,
        "version": value.version,
        "parent_id": value.parent_id,
        "action": value.action,
        "proposal_id": value.proposal_id,
        "proposal_digest": value.proposal_digest,
        "restore_target_id": value.restore_target_id,
        "decision": value.decision,
        "base_projection_id": value.base_projection_id,
        "base_artifact_digest": value.base_artifact_digest,
        "rewrite_job_id": value.rewrite_job_id,
        "rewrite_output_digest": value.rewrite_output_digest,
        "decisions": value.decisions,
        "artifact": value.artifact,
        "artifact_digest": value.artifact_digest,
        "created_at": value.created_at,
    }


def _bounded_output_strings(value: object, maximum: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:1_000] for item in value[:maximum]]


def _wrap_payload(row) -> dict[str, Any]:
    return {
        "id": str(row.id)[:128],
        "local_date": str(row.local_date)[:10],
        "timezone": str(row.timezone)[:100],
        "scope": str(row.scope)[:100],
        "window_start_utc": str(row.window_start_utc)[:100],
        "window_end_utc": str(row.window_end_utc)[:100],
        "workflow_version": int(row.workflow_version),
        # Only authorized published revisions reach this projection.  Do not
        # expose mutable scheduler status, attempts, errors, or job timestamps.
        "status": "succeeded",
        "coverage_status": str(row.coverage_status)[:50],
        "published_input_digest": str(row.published_input_digest)[:128],
        "output": _bounded_wrap_output(row.output),
        "revision": int(row.revision),
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


def _require_visible_subject(conn, cfg: config_mod.Config, ref: EvidenceRef) -> None:
    supported = {
        "observation",
        "timeline_block",
        "session",
        "memory_entry",
        "memory_candidate",
        "daily_wrap",
        "daily_wrap_item",
        "daily_wrap_revision",
        "suggestion",
    }
    if ref.kind not in supported:
        raise ValueError("unsupported provenance kind")

    if ref.kind == "suggestion":
        suggestion = suggestion_store.get(conn, ref.id)
        if suggestion is None or all(
            item.id != ref.id for item in SuggestionKernel(conn, cfg).list_visible(limit=1_000)
        ):
            raise KeyError(ref.id)
        return

    if ref.kind == "daily_wrap_revision":
        row = daily_wrap_store.get_by_id(conn, ref.path) if ref.path else None
        if (
            not ref.path
            or candidate_store.is_tombstoned(conn, kind="daily_wrap", artifact_id=ref.path)
            or row is None
            or provenance_store.availability(conn, ref) != "available"
            or not ContextService(conn, cfg).daily_wrap_allowed(ref.path, expected_row=row)
            or not ContextService(conn, cfg).evidence_allowed(ref)
        ):
            raise KeyError(ref.id)
        return

    canonical = ref
    if ref.kind in {"observation", "timeline_block", "memory_entry"}:
        current_hash = provenance_store.current_content_hash(conn, ref)
        if not current_hash:
            raise KeyError(ref.id)
        canonical = EvidenceRef(
            kind=ref.kind,
            id=ref.id,
            path=ref.path,
            timestamp=ref.timestamp,
            content_hash=current_hash,
        )
    resolution = EvidenceResolver(conn, cfg).resolve(canonical)
    if resolution.get("status") != "current":
        raise KeyError(ref.id)


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


def _resume_document_source(value: object) -> bytes:
    maximum_encoded = ((MAX_RESUME_DOCUMENT_BYTES + 2) // 3) * 4
    encoded = _bounded_string(value, maximum_encoded, nonempty=True)
    try:
        source = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid document source encoding") from exc
    if not 1 <= len(source) <= MAX_RESUME_DOCUMENT_BYTES:
        raise ValueError("invalid document source size")
    return source


def _resume_document_format(value: object) -> str:
    source_format = _bounded_string(value, 8, nonempty=True)
    if source_format not in {"pdf", "docx"}:
        raise ValueError("unsupported document source format")
    return source_format


def _string_list(value: object, *, max_items: int, max_length: int) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError("bounded string list required")
    return [_bounded_string(item, max_length, nonempty=True) for item in value]


def _object_list(value: object, maximum: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("bounded object list required")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError("bounded object list required")
    return value


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


def _selection_error(code: str) -> dict[str, Any]:
    if code == "accessibility_untrusted":
        return _error(
            "ACCESSIBILITY_REQUIRED",
            "Accessibility permission is required to read the explicit selection.",
        )
    if code in {"no_selection", "multiple_selection"}:
        return _error(
            "NO_EXACT_SELECTION",
            "Select one non-empty text range in another app and try again.",
        )
    if code in {"secure_field", "privacy_denied", "url_policy_unverifiable"}:
        return _error(
            "SELECTION_EXCLUDED",
            "The selected source is excluded by the local privacy boundary.",
        )
    if code == "focus_changed":
        return _error(
            "SELECTION_CHANGED",
            "The selected source changed before it could be bound.",
        )
    return _error(
        "SELECTION_UNAVAILABLE",
        "An exact external text selection is not currently available.",
    )


if __name__ == "__main__":
    main()
