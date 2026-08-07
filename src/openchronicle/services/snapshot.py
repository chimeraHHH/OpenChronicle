"""Bounded, zero-network desktop snapshot assembly."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import __version__, paths
from ..capture import store_lock as capture_store
from ..config import Config
from ..daily_wrap.service import DailyWrapService
from ..memory_candidates import store as candidate_store
from ..services.memory import MemoryService
from ..store import fts
from ..timeline import store as timeline_store


def build_snapshot(
    conn,
    cfg: Config,
    *,
    timeline_limit: int,
    candidate_limit: int,
    wrap_limit: int,
) -> dict[str, Any]:
    """Return one bounded product snapshot without probing any model/provider."""
    # Resume only deletion plans the user previously authorized. This is a
    # local privacy recovery step and never invokes a model.
    MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens).resume_pending_purges()

    from .. import cli as cli_mod

    pid = cli_mod._read_pid()
    paused = paths.paused_flag().exists()
    with capture_store.capture_store_lock():
        recent_captures = fts.recent_captures(conn, limit=1)
        capture_count = int(conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
    last_capture = recent_captures[0] if recent_captures else None
    last_timestamp = last_capture.timestamp if last_capture else None
    health, _style = cli_mod._health_status(pid, last_timestamp)

    conn.execute("BEGIN")
    try:
        session_row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(status='active') AS active,
                   SUM(status='ended') AS ended,
                   SUM(status='reduced') AS reduced,
                   SUM(status='failed') AS failed
              FROM sessions
            """
        ).fetchone()
        file_rows = [
            row
            for row in fts.list_files(conn, include_dormant=True, include_archived=True)
            if not candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=row.path)
        ]
        entry_count = int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        timeline_count = int(conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0])
        timeline = timeline_store.query_recent(conn, limit=timeline_limit) if timeline_limit else []
        candidate_counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM memory_candidates GROUP BY status"
            ).fetchall()
        }
        candidates = (
            candidate_store.list_review_snapshot(conn, limit=candidate_limit)
            if candidate_limit
            else []
        )
        wraps = DailyWrapService(conn, cfg).list(limit=wrap_limit) if wrap_limit else []
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
                key: int(session_row[key] or 0)
                for key in ("total", "active", "ended", "reduced", "failed")
            },
            "memory": {
                "active_files": sum(row.status == "active" for row in file_rows),
                "dormant_files": sum(row.status == "dormant" for row in file_rows),
                "archived_files": sum(row.status == "archived" for row in file_rows),
                "entries": entry_count,
            },
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
        "generated_at": datetime.now().astimezone().isoformat(),
    }


def _wrap_summary(row) -> dict[str, Any]:
    output = row.output if isinstance(row.output, dict) else {}
    return {
        "id": str(row.id)[:128],
        "local_date": str(row.local_date)[:10],
        "timezone": str(row.timezone)[:100],
        "scope": str(row.scope)[:100],
        "status": str(row.status)[:50],
        "coverage_status": str(row.coverage_status)[:50],
        "revision": row.revision,
        "summary": str(output.get("summary") or "")[:500],
        "item_counts": {
            category: len(output.get(category, [])) if isinstance(output.get(category), list) else 0
            for category in ("completed", "progressed", "open", "blocked", "needs_review")
        },
        "updated_at": str(row.updated_at)[:100],
        "completed_at": str(row.completed_at)[:100] if row.completed_at else None,
    }


def _bounded_config_list(value: object, *, max_length: int = 300) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:max_length] for item in value[:100] if isinstance(item, str)]
