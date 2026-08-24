"""Read-side helper for ``read_recent_capture``.

Reads JSON files straight out of ``~/.openchronicle/capture-buffer/`` and
returns the closest match to an optional timestamp with optional app / title
filters. The shared filename codec accepts current fraction/observation-ID
stems and legacy variants; parsed instants, rather than wall-clock filename
order, determine recency across timezone and daylight-saving changes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import paths
from ..capture import filenames
from ..capture.window_meta import parse_window_meta
from ..config import Config
from ..memory_candidates import store as candidate_store
from ..privacy import policy as privacy_policy
from ..privacy.egress import privacy_egress_lock
from ..provenance.models import EvidenceRef
from ..services.context import ContextService
from ..store import fts as fts_store
from ..timeline import store as timeline_store

_ADDRESS_VALUE_SEMANTICS = (
    "browser_address_control_value; may_be_uncommitted; not_visit_or_read_evidence"
)
_POLICY_RECALL_PAGE = 100


def _parse_stem(stem: str) -> datetime | None:
    """Parse legacy and current capture stems through the shared codec."""
    return filenames.parse_capture_stem(stem)


def _parse_at(text: str) -> datetime:
    """Accept ISO timestamps or bare ``HH:MM[:SS]``. Bare times use today (local)."""
    s = text.strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        now = datetime.now().astimezone()
        today = now.date()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                t = datetime.strptime(s, fmt).time()
                return datetime.combine(today, t, tzinfo=now.tzinfo)
            except ValueError:
                continue
        raise ValueError(f"cannot parse time: {text!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _matches(
    data: dict[str, Any],
    app_name: str | None,
    window_title_substring: str | None,
) -> bool:
    if app_name is None and window_title_substring is None:
        return True
    meta = data.get("window_meta") or {}
    name = (meta.get("app_name") or "").lower()
    title = (meta.get("title") or "").lower()
    if app_name is not None and app_name.lower() not in name:
        return False
    return not (window_title_substring is not None and window_title_substring.lower() not in title)


def _load_capture(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _content_mode(data: dict[str, Any]) -> str:
    privacy = data.get("privacy")
    if isinstance(privacy, dict) and privacy.get("content_mode") == "url_metadata_only":
        return "url_metadata_only"
    return "normal"


def _url_semantics(url: object) -> str:
    return _ADDRESS_VALUE_SEMANTICS if isinstance(url, str) and url else ""


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _capture_projection(data: dict[str, Any]) -> dict[str, str]:
    meta = data.get("window_meta")
    focused = data.get("focused_element")
    meta = meta if isinstance(meta, dict) else {}
    focused = focused if isinstance(focused, dict) else {}
    return {
        "observation_id": _text(data.get("observation_id")),
        "timestamp": _text(data.get("timestamp")),
        "app_name": _text(meta.get("app_name")),
        "bundle_id": _text(meta.get("bundle_id")),
        "window_title": _text(meta.get("title")),
        "focused_role": _text(focused.get("role")),
        "focused_value": _text(focused.get("value")),
        "visible_text": _text(data.get("visible_text")),
        "url": _text(data.get("url")),
    }


def _load_allowed_capture(stem: str, cfg: Config) -> dict[str, Any] | None:
    if not isinstance(stem, str) or not stem or Path(stem).name != stem:
        return None
    data = _load_capture(paths.capture_buffer_dir() / f"{stem}.json")
    if not isinstance(data, dict):
        return None
    decision = privacy_policy.evaluate_stored_observation(
        cfg.capture,
        observation=data,
    )
    return data if decision.allowed else None


def _hit_matches_capture(
    conn: sqlite3.Connection,
    hit: fts_store.CaptureHit,
    data: dict[str, Any],
) -> bool:
    """Treat FTS as recall only; every returned byte must match its JSON."""
    projected = _capture_projection(data)
    return (
        hit.observation_id == projected["observation_id"]
        and hit.timestamp == projected["timestamp"]
        and hit.app_name == projected["app_name"]
        and hit.bundle_id == projected["bundle_id"]
        and hit.window_title == projected["window_title"]
        and hit.focused_role == projected["focused_role"]
        and hit.focused_value == projected["focused_value"]
        and hit.url == projected["url"]
        and fts_store.get_capture_visible_text(conn, hit.id) == projected["visible_text"]
    )


def _format_response(path: Path, data: dict[str, Any], include_screenshot: bool) -> dict[str, Any]:
    meta = data.get("window_meta")
    focused = data.get("focused_element")
    shot = data.get("screenshot")
    meta = meta if isinstance(meta, dict) else {}
    focused = focused if isinstance(focused, dict) else {}
    shot = shot if isinstance(shot, dict) else {}
    focused_value = _text(focused.get("value"))
    raw_value_length = focused.get("value_length")
    value_length = (
        raw_value_length
        if isinstance(raw_value_length, int)
        and not isinstance(raw_value_length, bool)
        and raw_value_length >= 0
        else len(focused_value)
    )
    out: dict[str, Any] = {
        "timestamp": _text(data.get("timestamp")),
        "observation_id": _text(data.get("observation_id")),
        "file": path.name,
        "app_name": _text(meta.get("app_name")),
        "bundle_id": _text(meta.get("bundle_id")),
        "window_title": _text(meta.get("title")),
        "url": _text(data.get("url")),
        "url_semantics": _url_semantics(data.get("url")),
        "content_mode": _content_mode(data),
        "focused_element": {
            "role": _text(focused.get("role")),
            "title": _text(focused.get("title")),
            "value": focused_value,
            "is_editable": focused.get("is_editable") is True,
            "value_length": value_length,
        },
        "visible_text": _text(data.get("visible_text")),
        "screenshot_stripped": data.get("screenshot_stripped") is True,
    }
    image_base64 = shot.get("image_base64")
    if (
        include_screenshot
        and _has_exact_window_screenshot_attestation(data, shot)
        and isinstance(image_base64, str)
        and image_base64
    ):
        out["screenshot_b64"] = image_base64
        out["screenshot_mime"] = _text(shot.get("mime_type")) or "image/jpeg"
    return out


def _has_exact_window_screenshot_attestation(data: dict[str, Any], shot: dict[str, Any]) -> bool:
    """Reject legacy full-display pixels that cannot be policy re-evaluated."""
    if (
        data.get("schema_version") != 4
        or shot.get("capture_mode") != "exact_window_v1"
        or shot.get("mime_type") != "image/jpeg"
    ):
        return False
    identity = parse_window_meta(shot.get("window_meta"))
    observation_meta = data.get("window_meta")
    if identity is None or not isinstance(observation_meta, dict) or identity.bounds is None:
        return False
    return observation_meta == {
        "app_name": identity.app_name,
        "title": identity.title,
        "bundle_id": identity.bundle_id,
        "pid": identity.pid,
        "window_id": identity.window_id,
        "bounds": identity.bounds.to_dict(),
    }


def read_recent_capture(
    *,
    cfg: Config,
    at: str | None = None,
    app_name: str | None = None,
    window_title_substring: str | None = None,
    include_screenshot: bool = False,
    max_age_minutes: int = 15,
) -> dict[str, Any] | None:
    """Return one capture while serialized with cleanup and capture writes."""
    with privacy_egress_lock():
        return _read_recent_capture_locked(
            cfg=cfg,
            at=at,
            app_name=app_name,
            window_title_substring=window_title_substring,
            include_screenshot=include_screenshot,
            max_age_minutes=max_age_minutes,
        )


def _read_recent_capture_locked(
    *,
    cfg: Config,
    at: str | None = None,
    app_name: str | None = None,
    window_title_substring: str | None = None,
    include_screenshot: bool = False,
    max_age_minutes: int = 15,
) -> dict[str, Any] | None:
    """Return the capture that best matches the given time + filters.

    ``at`` None → newest matching capture overall.
    ``at`` set → nearest-in-time match, bounded by ``max_age_minutes`` on either side.
    """
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return None

    target: datetime | None = _parse_at(at) if at else None
    with fts_store.cursor() as conn:
        hidden_files = {
            tombstone.artifact_id
            for tombstone in candidate_store.list_tombstones(conn, kind="capture_file")
        }

    # Parse before sorting: lexicographic wall-clock order is wrong during a
    # daylight-saving fallback (01:59-04:00 is older than 01:00-05:00).
    stems: list[tuple[datetime, Path]] = []
    for path in buf.iterdir():
        if not path.is_file() or path.is_symlink() or path.suffix != ".json":
            continue
        if path.name in hidden_files:
            continue
        timestamp = _parse_stem(path.stem)
        if timestamp is not None:
            stems.append((timestamp, path))
    if target is None:
        stems.sort(key=lambda item: item[0].timestamp(), reverse=True)

    best: tuple[float, Path, dict[str, Any]] | None = None

    for ts, path in stems:
        if target is not None:
            delta = abs((ts - target).total_seconds())
            if delta > max_age_minutes * 60:
                # With no ordering guarantee across timezones we can't short-
                # circuit, but the buffer is small enough post-cleanup that
                # a full pass is cheap.
                continue
        data = _load_capture(path)
        if data is None:
            continue
        if not privacy_policy.evaluate_stored_observation(
            cfg.capture,
            observation=data,
        ).allowed:
            continue
        if not _matches(data, app_name, window_title_substring):
            continue
        if target is None:
            return _format_response(
                path,
                data,
                include_screenshot and cfg.capture.include_screenshot,
            )
        delta = abs((ts - target).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, path, data)

    if best is None:
        return None
    _, path, data = best
    return _format_response(
        path,
        data,
        include_screenshot and cfg.capture.include_screenshot,
    )


# ─── search_captures + current_context (FTS-backed) ───────────────────────


def search_captures(
    *,
    cfg: Config,
    query: str,
    since: str | None = None,
    until: str | None = None,
    app_name: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search capture projections while serialized with destructive cleanup."""
    with privacy_egress_lock():
        return _search_captures_locked(
            cfg=cfg,
            query=query,
            since=since,
            until=until,
            app_name=app_name,
            limit=limit,
        )


def _search_captures_locked(
    *,
    cfg: Config,
    query: str,
    since: str | None = None,
    until: str | None = None,
    app_name: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """BM25 + snippet search over the S1 FTS index.

    Returns light-weight hits. Follow up with
    `read_recent_capture(at=hit["timestamp"], app_name=hit["app_name"])` to
    hydrate available content. Normal schema-v4 rows may expose full
    ``visible_text`` and, when explicitly requested and attested, an exact-window
    screenshot. Schema-v5 ``url_metadata_only`` rows expose only window identity
    plus an editable address-control value; that value never proves a page was
    visited, loaded, displayed, or read. ``file_stem`` is an opaque provenance
    handle, not a valid value for ``at``.
    """
    allowed_hits: list[tuple[fts_store.CaptureHit, dict[str, Any]]] = []
    with fts_store.cursor() as conn:
        requested_limit = max(0, min(limit, 100))
        if requested_limit == 0:
            return []
        offset = 0
        while len(allowed_hits) < requested_limit:
            hits = fts_store.search_captures(
                conn,
                query=query,
                since=since,
                until=until,
                app_name=app_name,
                limit=_POLICY_RECALL_PAGE,
                offset=offset,
            )
            if not hits:
                break
            offset += len(hits)
            for hit in hits:
                data = _load_allowed_capture(hit.id, cfg)
                if data is None or not _hit_matches_capture(conn, hit, data):
                    continue
                if candidate_store.is_tombstoned(
                    conn, kind="capture_file", artifact_id=f"{hit.id}.json"
                ):
                    continue
                allowed_hits.append((hit, data))
                if len(allowed_hits) >= requested_limit:
                    break
            if len(hits) < _POLICY_RECALL_PAGE:
                break
    results: list[dict[str, Any]] = []
    for hit, data in allowed_hits:
        projected = _capture_projection(data)
        results.append(
            {
                "timestamp": projected["timestamp"],
                "app_name": projected["app_name"],
                "bundle_id": projected["bundle_id"],
                "window_title": projected["window_title"],
                "url": projected["url"],
                "url_semantics": _url_semantics(projected["url"]),
                "content_mode": _content_mode(data),
                "snippet": hit.snippet,
                "rank": hit.rank,
                "file_stem": hit.id,
                "observation_id": projected["observation_id"],
                "focused_role": projected["focused_role"],
                "focused_value_preview": projected["focused_value"][:200],
            }
        )
    return results


def _dedupe_recent_captures(
    rows: list[fts_store.CaptureHit],
    *,
    limit: int,
) -> list[fts_store.CaptureHit]:
    """Pick up to ``limit`` rows distinct by (app_name, window_title)."""
    seen: set[tuple[str, str]] = set()
    out: list[fts_store.CaptureHit] = []
    for r in rows:
        key = (r.app_name or "", r.window_title or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _recent_timeline_blocks(
    conn: sqlite3.Connection,
    limit: int,
    cfg: Config,
) -> list[dict[str, Any]]:
    requested_limit = max(0, min(limit, 100))
    if requested_limit == 0:
        return []
    out: list[dict[str, Any]] = []
    context = ContextService(conn, cfg)
    offset = 0
    while len(out) < requested_limit:
        rows = conn.execute(
            """
            SELECT id FROM timeline_blocks
             ORDER BY julianday(end_time) DESC, id DESC
             LIMIT ? OFFSET ?
            """,
            (_POLICY_RECALL_PAGE, offset),
        ).fetchall()
        if not rows:
            break
        offset += len(rows)
        for row in rows:
            block_id = row["id"]
            if not isinstance(block_id, str):
                continue
            block = timeline_store.get_by_id(conn, block_id)
            if block is None:
                continue
            subject = EvidenceRef(kind="timeline_block", id=block.id)
            if not context.evidence_allowed(subject):
                continue
            out.append(
                {
                    "id": block.id,
                    "start_time": block.start_time.isoformat(),
                    "end_time": block.end_time.isoformat(),
                    "entries": block.entries,
                    "apps_used": block.apps_used,
                    "capture_count": block.capture_count,
                }
            )
            if len(out) >= requested_limit:
                break
        if len(rows) < _POLICY_RECALL_PAGE:
            break
    # Newest first looks weird in a context block; reverse to time-ordered.
    return list(reversed(out))


def current_context(
    *,
    cfg: Config,
    app_filter: str | None = None,
    headline_limit: int = 5,
    fulltext_limit: int = 3,
    timeline_limit: int = 8,
) -> dict[str, Any]:
    """Build current context while serialized with destructive cleanup."""
    with privacy_egress_lock():
        return _current_context_locked(
            cfg=cfg,
            app_filter=app_filter,
            headline_limit=headline_limit,
            fulltext_limit=fulltext_limit,
            timeline_limit=timeline_limit,
        )


def _current_context_locked(
    *,
    cfg: Config,
    app_filter: str | None = None,
    headline_limit: int = 5,
    fulltext_limit: int = 3,
    timeline_limit: int = 8,
) -> dict[str, Any]:
    """One-shot snapshot of "what's happening on screen right now".

    Mirrors the payload Einsia-Partner auto-injects every chat turn:

      * ``recent_captures_headline`` — last N captures as ``[HH:MM] App — Title [Role]``
      * ``recent_captures_fulltext`` — top M captures deduped by (app, window),
        carrying the FULL visible_text + focused_element.value so the model can
        actually read what's on screen
      * ``recent_timeline_blocks`` — the last K LLM-summarized 1-min blocks
    """
    headline_limit = max(0, min(headline_limit, 100))
    fulltext_limit = max(0, min(fulltext_limit, 100))
    timeline_limit = max(0, min(timeline_limit, 100))
    with fts_store.cursor() as conn:
        headline_rows: list[tuple[fts_store.CaptureHit, dict[str, Any]]] = []
        full_rows: list[tuple[fts_store.CaptureHit, dict[str, Any]]] = []
        full_keys: set[tuple[str, str]] = set()
        offset = 0
        while len(headline_rows) < headline_limit or len(full_rows) < fulltext_limit:
            page = fts_store.recent_captures(
                conn,
                app_name=app_filter,
                limit=_POLICY_RECALL_PAGE,
                offset=offset,
            )
            if not page:
                break
            offset += len(page)
            for row in page:
                data = _load_allowed_capture(row.id, cfg)
                if data is None or not _hit_matches_capture(conn, row, data):
                    continue
                if candidate_store.is_tombstoned(
                    conn, kind="capture_file", artifact_id=f"{row.id}.json"
                ):
                    continue
                if len(headline_rows) < headline_limit:
                    headline_rows.append((row, data))
                key = (row.app_name or "", row.window_title or "")
                if len(full_rows) < fulltext_limit and key not in full_keys:
                    full_keys.add(key)
                    full_rows.append((row, data))
                if len(headline_rows) >= headline_limit and len(full_rows) >= fulltext_limit:
                    break
            if len(page) < _POLICY_RECALL_PAGE:
                break
        full: list[dict[str, Any]] = []
        for r, data in full_rows:
            projected = _capture_projection(data)
            full.append(
                {
                    "timestamp": projected["timestamp"],
                    "app_name": projected["app_name"],
                    "window_title": projected["window_title"],
                    "url": projected["url"],
                    "url_semantics": _url_semantics(projected["url"]),
                    "content_mode": _content_mode(data),
                    "focused_role": projected["focused_role"],
                    "focused_value": projected["focused_value"],
                    "visible_text": projected["visible_text"],
                    "file_stem": r.id,
                    "observation_id": r.observation_id,
                }
            )
        timeline = _recent_timeline_blocks(conn, timeline_limit, cfg)

    headlines: list[dict[str, Any]] = []
    for r, data in headline_rows:
        projected = _capture_projection(data)
        ts_short = projected["timestamp"][11:16]  # HH:MM from ISO
        headlines.append(
            {
                "time": ts_short,
                "app_name": projected["app_name"],
                "window_title": projected["window_title"],
                "focused_role": projected["focused_role"],
                "content_mode": _content_mode(data),
                "file_stem": r.id,
                "observation_id": r.observation_id,
            }
        )

    return {
        "recent_captures_headline": headlines,
        "recent_captures_fulltext": full,
        "recent_timeline_blocks": timeline,
    }
