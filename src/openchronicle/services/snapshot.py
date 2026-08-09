"""Bounded, zero-network desktop snapshot assembly."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import __version__, paths
from ..capture import store_lock as capture_store
from ..config import Config
from ..daily_wrap.service import DailyWrapService
from ..memory_candidates import store as candidate_store
from ..privacy import policy as privacy_policy
from ..privacy.egress import privacy_egress_fenced
from ..prompt_rescue.service import PromptRescueService
from ..prompt_rescue.service import validate_config as validate_prompt_rescue
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef
from ..reply_rescue.service import ReplyRescueService
from ..reply_rescue.service import validate_config as validate_reply_rescue
from ..services.context import ContextService
from ..services.evidence import EvidenceResolver
from ..services.memory import MemoryService
from ..store import files as files_store
from ..store import fts
from ..suggestions.service import SuggestionKernel
from ..timeline import store as timeline_store


@privacy_egress_fenced
def build_snapshot(
    conn,
    cfg: Config,
    *,
    timeline_limit: int,
    candidate_limit: int,
    wrap_limit: int,
    suggestion_limit: int = 20,
    prompt_rescue_limit: int = 20,
    reply_rescue_limit: int = 20,
) -> dict[str, Any]:
    """Return one bounded product snapshot without probing any model/provider."""
    # Resume only deletion plans the user previously authorized. This is a
    # local privacy recovery step and never invokes a model.
    MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens).resume_pending_purges()

    from .. import cli as cli_mod

    pid = cli_mod._read_pid()
    paused = paths.paused_flag().exists()
    with capture_store.capture_store_lock():
        indexed_capture_count = int(conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
        recent_captures = (
            fts.recent_captures(conn, limit=indexed_capture_count) if indexed_capture_count else []
        )
        authorized_captures = [
            row for row in recent_captures if _capture_row_allowed(conn, cfg, row)
        ]
    capture_count = len(authorized_captures)
    last_capture = authorized_captures[0] if authorized_captures else None
    last_timestamp = last_capture.timestamp if last_capture else None
    health, _style = cli_mod._health_status(pid, last_timestamp)

    conn.execute("BEGIN")
    try:
        context = ContextService(conn, cfg)
        visible_sessions = [
            row
            for row in conn.execute("SELECT * FROM sessions").fetchall()
            if _session_allowed(conn, context, str(row["id"]))
        ]
        session_counts = {
            "total": len(visible_sessions),
            **{
                status: sum(str(row["status"]) == status for row in visible_sessions)
                for status in ("active", "ended", "reduced", "failed")
            },
        }
        memory_counts = {
            "active_files": 0,
            "dormant_files": 0,
            "archived_files": 0,
            "entries": 0,
        }
        for file_row in fts.list_files(conn, include_dormant=True, include_archived=True):
            if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=file_row.path):
                continue
            try:
                parsed = files_store.read_file(files_store.memory_path(file_row.path))
            except (FileNotFoundError, OSError, ValueError):
                continue
            if not context.memory_file_metadata_allowed(parsed):
                continue
            visible_entries = [
                entry
                for entry in parsed.entries
                if not candidate_store.is_tombstoned(
                    conn,
                    kind="memory_entry",
                    artifact_id=entry.id,
                    path=parsed.path.name,
                )
                and context.memory_entry_allowed(path=parsed.path.name, entry=entry)
            ]
            if not visible_entries:
                continue
            status_key = f"{file_row.status}_files"
            if status_key in memory_counts:
                memory_counts[status_key] += 1
            memory_counts["entries"] += len(visible_entries)
        raw_timeline_count = int(conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0])
        visible_timeline = (
            [
                block
                for block in timeline_store.query_recent(conn, limit=raw_timeline_count)
                if context.evidence_allowed(EvidenceRef(kind="timeline_block", id=block.id))
            ]
            if raw_timeline_count
            else []
        )
        timeline_count = len(visible_timeline)
        timeline = visible_timeline[-timeline_limit:] if timeline_limit else []
        resolver = EvidenceResolver(conn, cfg)
        all_candidate_rows = conn.execute("SELECT id, status FROM memory_candidates").fetchall()
        visible_candidate_ids = {
            str(row["id"])
            for row in all_candidate_rows
            if resolver.resolve(EvidenceRef(kind="memory_candidate", id=str(row["id"])))["status"]
            == "current"
        }
        candidates = (
            [
                candidate
                for candidate in candidate_store.list_review_snapshot(conn, limit=1_000)
                if candidate.id in visible_candidate_ids
            ][:candidate_limit]
            if candidate_limit
            else []
        )
        candidate_counts = {
            status: sum(
                str(row["status"]) == status and str(row["id"]) in visible_candidate_ids
                for row in all_candidate_rows
            )
            for status in candidate_store.VALID_STATUSES
        }
        wraps = (
            [
                row
                for row in DailyWrapService(conn, cfg).list(limit=365)
                if context.daily_wrap_allowed(row.id, expected_row=row)
            ][:wrap_limit]
            if wrap_limit
            else []
        )
        suggestions = (
            SuggestionKernel(conn, cfg).list_visible(
                statuses=["ready", "viewed"],
                limit=suggestion_limit,
            )
            if suggestion_limit
            else []
        )
        validate_prompt_rescue(cfg)
        prompt_rescue_service = PromptRescueService(conn, cfg)
        prompt_rescue_jobs = (
            prompt_rescue_service.list(limit=prompt_rescue_limit) if prompt_rescue_limit else []
        )
        prompt_rescue_provider = prompt_rescue_service.provider_summary()
        validate_reply_rescue(cfg)
        reply_rescue_service = ReplyRescueService(conn, cfg)
        reply_rescue_jobs = (
            reply_rescue_service.list(limit=reply_rescue_limit) if reply_rescue_limit else []
        )
        reply_rescue_provider = reply_rescue_service.provider_summary()
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return {
        "version": __version__,
        "daemon": {
            "running": pid is not None,
            "pid": pid,
            "uptime": cli_mod._daemon_uptime(),
            "health": health,
        },
        "capture": {
            "paused": paused,
            "indexed_count": capture_count,
            "last": (
                {
                    "timestamp": str(last_capture.timestamp)[:100],
                    "app_name": str(last_capture.app_name)[:200],
                    "bundle_id": str(last_capture.bundle_id)[:300],
                    "window_title": str(last_capture.window_title)[:500],
                }
                if last_capture
                else None
            ),
        },
        "privacy": {
            "buffer_retention_hours": cfg.capture.buffer_retention_hours,
            "screenshot_retention_hours": cfg.capture.screenshot_retention_hours,
            "allowed_bundle_ids": _bounded_config_list(cfg.capture.allowed_bundle_ids),
            "excluded_bundle_ids": _bounded_config_list(cfg.capture.excluded_bundle_ids),
            "excluded_app_names": _bounded_config_list(cfg.capture.excluded_app_names),
            "excluded_window_title_patterns": _bounded_config_list(
                cfg.capture.excluded_window_title_patterns, max_length=500
            ),
            "deny_unknown_windows": cfg.capture.deny_unknown_windows,
            "include_screenshot": cfg.capture.include_screenshot,
        },
        "counts": {
            "sessions": {
                key: int(session_counts[key])
                for key in ("total", "active", "ended", "reduced", "failed")
            },
            "memory": memory_counts,
            "timeline_blocks": timeline_count,
            "candidates": {
                status: candidate_counts.get(status, 0)
                for status in sorted(candidate_store.VALID_STATUSES)
            },
        },
        "timeline": [
            {
                "id": str(block.id)[:128],
                "start_time": block.start_time.isoformat()[:100],
                "end_time": block.end_time.isoformat()[:100],
                "timezone": str(block.timezone)[:100],
                "entries": [str(value)[:500] for value in block.entries[:20]],
                "apps_used": [str(value)[:200] for value in block.apps_used[:20]],
                "capture_count": block.capture_count,
            }
            for block in timeline
        ],
        "candidates": [
            {
                "id": str(candidate.id)[:128],
                "status": str(candidate.status)[:50],
                "kind": str(candidate.kind)[:100],
                "target_path": str(candidate.target_path)[:512],
                "version": candidate.version,
                "content_preview": " ".join(candidate.content.split())[:240],
                "tags": [str(tag)[:100] for tag in candidate.tags[:100]],
                "confidence": candidate.confidence,
                "updated_at": str(candidate.updated_at)[:100],
            }
            for candidate in candidates
        ],
        "daily_wrap": {
            "enabled": cfg.daily_wrap.enabled,
            "timezone": str(cfg.daily_wrap.timezone)[:100],
            "wraps": [_wrap_summary(row) for row in wraps],
        },
        "suggestions": [
            {
                "id": str(item.id)[:128],
                "workflow": str(item.workflow)[:100],
                "status": str(item.status)[:50],
                "title": str(item.title)[:160],
                "summary": str(item.summary)[:1_000],
                "artifact": item.artifact,
                "score": item.score,
                "version": item.version,
                "detected_at": str(item.detected_at)[:100],
                "expires_at": str(item.expires_at)[:100],
            }
            for item in suggestions
        ],
        "suggestions_enabled": cfg.suggestions.enabled,
        "prompt_rescue": {
            "enabled": cfg.prompt_rescue.enabled,
            "provider": {
                "model": str(prompt_rescue_provider["model"])[:256],
                "location": str(prompt_rescue_provider["location"])[:50],
            },
            "jobs": [
                {
                    "id": str(job.id)[:128],
                    "status": str(job.status)[:50],
                    "source_kind": str(job.source_kind)[:50],
                    "rough_prompt_preview": " ".join(job.rough_prompt.split())[:240],
                    "model_identity": str(job.model_identity)[:256],
                    "provider_location": str(job.provider_location)[:50],
                    "output_edited": bool(job.output_edited),
                    "error_code": str(job.error_code)[:50],
                    "attempt_count": int(job.attempt_count),
                    "created_at": str(job.created_at)[:100],
                    "updated_at": str(job.updated_at)[:100],
                    "version": int(job.version),
                }
                for job in prompt_rescue_jobs
            ],
        },
        "reply_rescue": {
            "enabled": cfg.reply_rescue.enabled,
            "provider": {
                "model": str(reply_rescue_provider["model"])[:256],
                "location": str(reply_rescue_provider["location"])[:50],
            },
            "jobs": [
                {
                    "id": str(job.id)[:128],
                    "status": str(job.status)[:50],
                    "source_kind": str(job.source_kind)[:50],
                    "conversation_preview": " ".join(
                        str(job.source.get("conversation_text") or "").split()
                    )[:240],
                    "identity_assurance": str(job.source.get("identity_assurance") or "")[:50],
                    "model_identity": str(job.model_identity)[:256],
                    "provider_location": str(job.provider_location)[:50],
                    "output_edited": bool(job.output_edited),
                    "error_code": str(job.error_code)[:50],
                    "attempt_count": int(job.attempt_count),
                    "created_at": str(job.created_at)[:100],
                    "updated_at": str(job.updated_at)[:100],
                    "version": int(job.version),
                }
                for job in reply_rescue_jobs
            ],
        },
        "generated_at": datetime.now().astimezone().isoformat(),
    }


def _session_allowed(conn, context: ContextService, session_id: str) -> bool:
    """Expose session status only through a current, policy-allowed entry."""
    for dependent in provenance_store.direct_dependents(
        conn, EvidenceRef(kind="session", id=session_id)
    ):
        if dependent.kind != "memory_entry" or not dependent.path:
            continue
        try:
            parsed = files_store.read_file(files_store.memory_path(dependent.path))
        except (FileNotFoundError, OSError, ValueError):
            continue
        entry = next((item for item in parsed.entries if item.id == dependent.id), None)
        if entry is not None and context.memory_entry_allowed(path=dependent.path, entry=entry):
            return True
    return False


def _wrap_summary(row) -> dict[str, Any]:
    output = row.output if isinstance(row.output, dict) else {}
    return {
        "id": str(row.id)[:128],
        "local_date": str(row.local_date)[:10],
        "timezone": str(row.timezone)[:100],
        "scope": str(row.scope)[:100],
        "status": "succeeded",
        "coverage_status": str(row.coverage_status)[:50],
        "revision": row.revision,
        "summary": str(output.get("summary") or "")[:500],
        "item_counts": {
            category: len(output.get(category, [])) if isinstance(output.get(category), list) else 0
            for category in ("completed", "progressed", "open", "blocked", "needs_review")
        },
    }


def _capture_row_allowed(conn, cfg: Config, row) -> bool:
    """Authenticate one index row against its JSON and current policy."""
    if (
        not isinstance(row.id, str)
        or not row.id
        or Path(row.id).name != row.id
        or candidate_store.is_tombstoned(conn, kind="capture_file", artifact_id=f"{row.id}.json")
    ):
        return False
    capture_path = paths.capture_buffer_dir() / f"{row.id}.json"
    if capture_path.is_symlink() or not capture_path.is_file():
        return False
    try:
        data = json.loads(capture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(data, dict)
        or not privacy_policy.evaluate_stored_observation(cfg.capture, observation=data).allowed
    ):
        return False
    meta = data.get("window_meta")
    focused = data.get("focused_element")
    meta = meta if isinstance(meta, dict) else {}
    focused = focused if isinstance(focused, dict) else {}

    def text(value: object) -> str:
        return value if isinstance(value, str) else ""

    return (
        row.observation_id == text(data.get("observation_id"))
        and row.timestamp == text(data.get("timestamp"))
        and row.app_name == text(meta.get("app_name"))
        and row.bundle_id == text(meta.get("bundle_id"))
        and row.window_title == text(meta.get("title"))
        and row.focused_role == text(focused.get("role"))
        and row.focused_value == text(focused.get("value"))
        and row.url == text(data.get("url"))
        and fts.get_capture_visible_text(conn, row.id) == text(data.get("visible_text"))
        and not candidate_store.is_tombstoned(
            conn, kind="capture_file", artifact_id=f"{row.id}.json"
        )
    )


def _bounded_config_list(value: object, *, max_length: int = 300) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:max_length] for item in value[:100] if isinstance(item, str)]
