"""Capture scheduler: event-driven + heartbeat. Writes one JSON per tick to capture-buffer/."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import queue
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import paths
from ..config import CaptureConfig
from ..local_time import ClockSample
from ..logger import get
from ..memory_candidates import store as candidate_store
from ..privacy import policy as privacy_policy
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, observation_digest
from ..store import files as store_files
from ..store import fts as fts_store
from ..testing import failpoints
from ..timeline import store as timeline_store
from . import ax_capture, filenames, s1_parser, screenshot, store_lock, window_meta
from .event_dispatcher import EventDispatcher
from .watcher import AXWatcherProcess

logger = get("openchronicle.capture")


@dataclass(frozen=True, slots=True)
class _BufferRecord:
    mtime: float
    path: Path
    size: int
    capture_time: datetime | None
    binding: tuple[str, str, str, str] | None


@dataclass(frozen=True, slots=True)
class _WindowCleanupGroup:
    receipt: timeline_store.WindowReceipt
    records: tuple[_BufferRecord, ...]


_PERSISTED_TRIGGER_TYPES = {
    "heartbeat",
    "manual",
    "AXApplicationActivated",
    "AXFocusedWindowChanged",
    "AXValueChanged",
    "UserMouseClick",
    "UserTextInput",
}

_AX_ROOT_KEYS = frozenset({"timestamp", "apps", "window_meta"})
_AX_APP_KEYS = frozenset({"pid", "name", "bundle_id", "is_frontmost", "windows"})
_AX_WINDOW_KEYS = frozenset(
    {"title", "subrole", "description", "identifier", "focused", "elements"}
)
_AX_ELEMENT_STRING_KEYS = (
    "role",
    "subrole",
    "title",
    "description",
    "value",
    "identifier",
    "domIdentifier",
)
_AX_ELEMENT_STRING_LIST_KEYS = ("domClassList", "attributeNames")
_AX_ELEMENT_KEYS = frozenset((*_AX_ELEMENT_STRING_KEYS, *_AX_ELEMENT_STRING_LIST_KEYS, "children"))
_MAX_AX_SCHEMA_NODES = 20_000
_MAX_AX_SCHEMA_DEPTH = 128
_MAX_AX_SCHEMA_STRINGS = 50_000
_MAX_AX_SCHEMA_STRING_BYTES = 16 * 1024
_MAX_AX_SCHEMA_TOTAL_STRING_BYTES = 4 * 1024 * 1024


class _AXSchemaError(ValueError):
    """An AX helper payload did not match the persistable wire schema."""


class _AXSchemaBudget:
    """Mirror native AX limits while rebuilding an untrusted provider result."""

    __slots__ = ("nodes", "string_bytes", "strings")

    def __init__(self) -> None:
        self.nodes = 0
        self.strings = 0
        self.string_bytes = 0

    def consume_node(self, *, depth: int) -> None:
        self.nodes += 1
        if self.nodes > _MAX_AX_SCHEMA_NODES or depth > _MAX_AX_SCHEMA_DEPTH:
            raise _AXSchemaError

    def consume_string(self, value: object) -> str:
        if not isinstance(value, str):
            raise _AXSchemaError
        if len(value) > _MAX_AX_SCHEMA_STRING_BYTES:
            raise _AXSchemaError
        byte_count = len(value.encode("utf-8"))
        self.strings += 1
        self.string_bytes += byte_count
        if (
            byte_count > _MAX_AX_SCHEMA_STRING_BYTES
            or self.strings > _MAX_AX_SCHEMA_STRINGS
            or self.string_bytes > _MAX_AX_SCHEMA_TOTAL_STRING_BYTES
        ):
            raise _AXSchemaError
        return value


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="milliseconds")


def _timestamp_sample_from_provider(
    provider: Callable[[], datetime],
) -> tuple[str, float | None]:
    sample_provider = getattr(provider, "sample", None)
    sample = sample_provider() if callable(sample_provider) else None
    value = sample.wall_time if isinstance(sample, ClockSample) else provider()
    if not isinstance(value, datetime):
        raise TypeError("capture timestamp provider must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("capture timestamp provider must return an offset-aware datetime")
    tick = sample.monotonic_tick if isinstance(sample, ClockSample) else None
    if tick is not None and (not math.isfinite(tick) or tick < 0):
        raise ValueError("capture timestamp provider returned an invalid monotonic tick")
    return value.isoformat(timespec="milliseconds"), tick


def _safe_filename(ts: str) -> str:
    """Backward-compatible wrapper around the canonical filename encoder."""
    return filenames.safe_timestamp(ts)


def _build_capture(
    cfg: CaptureConfig,
    provider: ax_capture.AXProvider,
    trigger: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build one exact-window observation, or fail closed before persistence."""
    paths.ensure_dirs()

    if paths.paused_flag().exists():
        logger.info("capture skipped (paused)")
        return None

    # Validate URL rules before asking either native helper for user content.
    # Active URL policy supports only recognized browser adapters below;
    # malformed policy must deny before any Accessibility collection.
    url_policy = privacy_policy.validate_url_policy(cfg)
    if not url_policy.allowed:
        logger.info("capture denied by privacy policy: reason=%s", url_policy.reason)
        return None
    has_url_policy = privacy_policy.has_url_policy(cfg)
    required_ax_depth = (
        ax_capture._expected_effective_depth(cfg.ax_depth) if has_url_policy else None
    )
    if has_url_policy and required_ax_depth is None:
        logger.info("capture denied by privacy policy: reason=invalid_ax_depth")
        return None

    # Watcher events may wait behind other work. Apply policy to the event's
    # source identity, then treat it only as a wake-up signal and bind the
    # observation to the window that is actually frontmost below.
    if trigger is not None and not _event_allowed(cfg, trigger):
        return None

    meta = window_meta.active_window()
    if not isinstance(meta, window_meta.WindowMeta) or not meta.capture_ready:
        logger.info("capture dropped: no verifiable focused-window identity")
        return None

    decision = privacy_policy.evaluate_window(
        cfg,
        app_name=meta.app_name,
        bundle_id=meta.bundle_id,
        window_title=meta.title,
    )
    if not decision.allowed:
        logger.info(
            "capture denied by privacy policy: reason=%s",
            decision.reason,
        )
        return None
    is_browser = s1_parser.is_browser_bundle(meta.bundle_id)
    if has_url_policy and not is_browser:
        # Arbitrary AX text cannot prove that an unknown bundle is or is not a
        # browser, nor that a URL-like value came from its address bar.
        logger.info("capture denied after URL policy check: reason=unsupported_browser_bundle")
        return None
    if trigger is not None and not _trigger_matches_window(trigger, meta):
        logger.info("capture dropped: queued event no longer matches active window")
        return None

    if not provider.available:
        logger.info("capture dropped: AX provider unavailable")
        return None
    result = provider.capture_frontmost(
        focused_window_only=True,
        require_complete_tree=has_url_policy,
    )
    if not isinstance(result, ax_capture.AXCaptureResult):
        logger.info("capture dropped: AX collection unavailable")
        return None
    if has_url_policy and not _has_verified_complete_tree(result, expected_depth=required_ax_depth):
        logger.info("capture dropped: AX completeness receipt unavailable")
        return None

    ax_tree = _canonical_ax_tree(result.raw_json)
    if ax_tree is None:
        logger.info("capture dropped: AX result did not match the bounded wire schema")
        return None
    ax_identity = _ax_identity(ax_tree)
    if ax_identity is None:
        logger.info("capture dropped: AX result has no verifiable window identity")
        return None
    ax_decision = privacy_policy.evaluate_window(
        cfg,
        app_name=ax_identity.app_name,
        bundle_id=ax_identity.bundle_id,
        window_title=ax_identity.title,
    )
    if not ax_decision.allowed:
        logger.info(
            "capture denied after AX identity check: reason=%s",
            ax_decision.reason,
        )
        return None
    if not meta.same_capture_target(ax_identity):
        logger.info("capture dropped: active window changed during AX collection")
        return None

    ts = _now_iso()
    out: dict[str, Any] = {
        "timestamp": ts,
        "schema_version": 4,
        "observation_id": f"obs_{uuid.uuid4().hex}",
        "trigger": _sanitize_trigger(trigger, meta),
        "window_meta": _serialize_window_meta(meta),
        "privacy": {"decision": "allowed", "policy_version": 2},
        "ax_tree": ax_tree,
        # Never persist provider-controlled metadata. It is outside the AX wire
        # schema and could otherwise smuggle content around URL policy.
        "ax_metadata": {
            "mode": "frontmost",
            "platform": "macos",
            "focused_window_only": True,
        },
        # Collection can take long enough for the timeline producer to close
        # the bucket containing ``ts``.  The writer consumes this private
        # marker and assigns the authoritative timestamp while holding the
        # capture-store lock, immediately before the atomic rename.
        "_timestamp_at_persist": True,
    }

    # URL extraction must precede pixels and every durable sink. For supported
    # browsers the strict address adapter and full-tree deny scan run below.
    try:
        s1_parser.enrich(out)
    except Exception:  # noqa: BLE001
        # Treat helper content as untrusted even after its identity envelope
        # passed validation. Parser exception text can contain an AX value, so
        # keep the diagnostic generic and discard the in-memory observation.
        logger.warning("capture dropped: AX content could not be parsed")
        return None
    if has_url_policy:
        first_url_evidence = _gate_active_url_policy(cfg, out)
        if first_url_evidence is None:
            return None
        if cfg.include_screenshot:
            logger.info("capture dropped: screenshot disabled by active URL policy")
            return None

        # A same-title SPA/navigation can retain PID/CGWindowID/bounds while
        # its address changes. Take a second complete AX snapshot and require
        # stable evidence. AX traversal is not an atomic browser transaction,
        # so the page tree remains ephemeral and is projected away below.
        second_result = provider.capture_frontmost(
            focused_window_only=True,
            require_complete_tree=True,
        )
        if not isinstance(second_result, ax_capture.AXCaptureResult):
            logger.info("capture dropped: AX stability collection unavailable")
            return None
        if not _has_verified_complete_tree(second_result, expected_depth=required_ax_depth):
            logger.info("capture dropped: AX stability completeness receipt unavailable")
            return None
        second_tree = _canonical_ax_tree(second_result.raw_json)
        second_identity = _ax_identity(second_tree)
        if (
            second_tree is None
            or second_identity is None
            or not meta.same_capture_target(second_identity)
        ):
            logger.info("capture dropped: AX stability identity changed")
            return None
        second_out = dict(out)
        second_out["ax_tree"] = second_tree
        try:
            s1_parser.enrich(second_out)
        except Exception:  # noqa: BLE001
            logger.warning("capture dropped: AX stability content could not be parsed")
            return None
        second_url_evidence = _gate_active_url_policy(cfg, second_out)
        if second_url_evidence is None:
            return None
        if first_url_evidence != second_url_evidence:
            logger.info("capture dropped: URL evidence changed during AX collection")
            return None

        final_meta = window_meta.active_window()
        if not isinstance(final_meta, window_meta.WindowMeta) or not meta.same_capture_target(
            final_meta
        ):
            logger.info("capture dropped: window changed after AX stability check")
            return None
        final_decision = privacy_policy.evaluate_window(
            cfg,
            app_name=final_meta.app_name,
            bundle_id=final_meta.bundle_id,
            window_title=final_meta.title,
        )
        if not final_decision.allowed:
            logger.info(
                "capture denied after AX stability check: reason=%s",
                final_decision.reason,
            )
            return None
        projected = _project_url_policy_observation(
            second_out,
            address_evidence=second_url_evidence[0],
        )
        if projected is None:
            logger.info("capture dropped: URL metadata projection unavailable")
            return None
        out = projected
    elif is_browser:
        # Preserve the default supported-browser validity check without making
        # non-browser captures depend on heuristic URL extraction.
        url_decision = privacy_policy.evaluate_url(cfg, url=out.get("url"))
        if not url_decision.allowed:
            logger.info(
                "capture denied after URL policy check: reason=%s",
                url_decision.reason,
            )
            return None

    if cfg.include_screenshot:
        # Screenshot capture cannot be made atomic with AX. Active URL policy
        # returned above because it disables pixels entirely; for ordinary
        # opt-in screenshots, re-check exact identity before collection.
        latest_meta = window_meta.active_window()
        if not isinstance(latest_meta, window_meta.WindowMeta) or not latest_meta.capture_ready:
            logger.info("capture dropped: screenshot target identity unavailable")
            return None
        latest_decision = privacy_policy.evaluate_window(
            cfg,
            app_name=latest_meta.app_name,
            bundle_id=latest_meta.bundle_id,
            window_title=latest_meta.title,
        )
        if not latest_decision.allowed or not meta.same_capture_target(latest_meta):
            logger.info("capture dropped: screenshot target changed or was denied")
            return None
        shot = screenshot.grab(
            target=meta,
            max_width=cfg.screenshot_max_width,
            jpeg_quality=cfg.screenshot_jpeg_quality,
        )
        if shot is None or not meta.same_capture_target(shot.window_meta):
            logger.info("capture dropped: exact-window screenshot unavailable")
            return None

        final_meta = window_meta.active_window()
        if not isinstance(final_meta, window_meta.WindowMeta) or not meta.same_capture_target(
            final_meta
        ):
            logger.info("capture dropped: window changed after screenshot")
            return None
        final_decision = privacy_policy.evaluate_window(
            cfg,
            app_name=final_meta.app_name,
            bundle_id=final_meta.bundle_id,
            window_title=final_meta.title,
        )
        if not final_decision.allowed:
            logger.info(
                "capture denied after screenshot identity check: reason=%s",
                final_decision.reason,
            )
            return None
        out["screenshot"] = {
            "capture_mode": "exact_window_v1",
            "image_base64": shot.image_base64,
            "mime_type": shot.mime_type,
            "width": shot.width,
            "height": shot.height,
            "window_meta": shot.window_meta.to_capture_request(),
        }

    return out


def _has_verified_complete_tree(
    result: ax_capture.AXCaptureResult,
    *,
    expected_depth: int | None,
) -> bool:
    return (
        isinstance(result, ax_capture.AXCaptureResult)
        and expected_depth is not None
        and result.tree_complete_verified is True
        and isinstance(result.effective_max_depth, int)
        and not isinstance(result.effective_max_depth, bool)
        and result.effective_max_depth == expected_depth
    )


def _scan_evidence(
    scan: s1_parser.URLCandidateScan,
) -> tuple[tuple[str, str, str], ...]:
    return tuple(zip(scan.values, scan.provenance, scan.sources, strict=True))


def _gate_active_url_policy(
    cfg: CaptureConfig,
    capture: dict[str, Any],
) -> tuple[tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]] | None:
    """Require one trusted address and gate every additional URI candidate."""
    address_scan = s1_parser.browser_address_candidates(capture, require_stable_id=True)
    if not address_scan.complete or address_scan.issues or len(address_scan.values) != 1:
        logger.info("capture denied after URL policy check: reason=unverified_address")
        return None
    if address_scan.provenance != ("explicit_http",):
        # A scheme-less omnibox string may be a search query or an uncommitted
        # edit. The durable URL-only projection requires one explicit HTTP(S)
        # address; evaluating guessed HTTP/HTTPS variants is not sufficient.
        logger.info("capture denied after URL policy check: reason=ambiguous_address_scheme")
        return None

    url_scan = s1_parser.url_candidates(capture)
    if not url_scan.complete:
        logger.info("capture denied after URL policy check: reason=incomplete_url_scan")
        return None

    # The stable address control is already evaluated as one complete value.
    # Exclude that exact AX field from the generic prose tokenizer so legal URL
    # path punctuation cannot create a contradictory duplicate interpretation.
    # Every other tree field remains an additional deny surface; none of its
    # contents or a document/title claim is persisted.
    address_evidence = _scan_evidence(address_scan)
    address_sources = {source for _value, _provenance, source in address_evidence}
    url_evidence = tuple(
        evidence for evidence in _scan_evidence(url_scan) if evidence[2] not in address_sources
    )
    candidates = (*address_evidence, *url_evidence)
    denied_url = next(
        (
            decision
            for decision in (
                privacy_policy.evaluate_url_candidate(
                    cfg,
                    url=value,
                    scheme_known=provenance == "explicit_http",
                )
                for value, provenance, _source in candidates
            )
            if not decision.allowed
        ),
        None,
    )
    if denied_url is not None:
        logger.info(
            "capture denied after URL policy check: reason=%s",
            denied_url.reason,
        )
        return None
    return address_evidence, url_evidence


def _project_url_policy_observation(
    capture: dict[str, Any],
    *,
    address_evidence: tuple[tuple[str, str, str], ...],
) -> dict[str, Any] | None:
    """Rebuild the only durable shape allowed while URL policy is active.

    Accessibility traversal and window titles are not atomically bound to a
    browser document. Even two stable snapshots can observe an ABA navigation,
    so page AX content, focused values, and titles must not cross the durable
    boundary. The exact identity fence and full tree remain in-memory checks;
    downstream JSON, FTS, hooks, prompts, and logs see only this allowlist.
    """
    meta = capture.get("window_meta")
    trigger = capture.get("trigger")
    if len(address_evidence) != 1:
        return None
    address, provenance, _source = address_evidence[0]
    if (
        not isinstance(meta, dict)
        or not isinstance(trigger, dict)
        or not isinstance(address, str)
        or provenance != "explicit_http"
        or not address.casefold().startswith(("http://", "https://"))
    ):
        return None

    bounds = meta.get("bounds")
    if not isinstance(bounds, dict):
        return None

    return {
        "timestamp": capture["timestamp"],
        "schema_version": 5,
        "observation_id": capture["observation_id"],
        "trigger": {
            "event_type": trigger.get("event_type", "unknown"),
            "app_name": meta.get("app_name", ""),
            "bundle_id": meta.get("bundle_id", ""),
            "window_title": "",
            "pid": meta.get("pid"),
            "window_id": meta.get("window_id"),
        },
        "window_meta": {
            "app_name": meta.get("app_name", ""),
            "title": "",
            "bundle_id": meta.get("bundle_id", ""),
            "pid": meta.get("pid"),
            "window_id": meta.get("window_id"),
            "bounds": dict(bounds),
        },
        "privacy": {
            "decision": "allowed",
            "policy_version": 3,
            "content_mode": "url_metadata_only",
        },
        "url": address,
        "visible_text": "",
        "_timestamp_at_persist": capture.get("_timestamp_at_persist") is True,
    }


def _event_allowed(cfg: CaptureConfig, trigger: dict[str, Any]) -> bool:
    """Apply policy to watcher metadata before an event enters the queue."""
    url_policy = privacy_policy.validate_url_policy(cfg)
    if not url_policy.allowed:
        logger.info("watcher event denied by privacy policy: reason=%s", url_policy.reason)
        return False
    event_type = str(trigger.get("event_type") or "")
    if event_type in {"heartbeat", "manual"}:
        return True
    decision = privacy_policy.evaluate_window(
        cfg,
        app_name=str(trigger.get("app_name") or ""),
        bundle_id=str(trigger.get("bundle_id") or ""),
        window_title=str(trigger.get("window_title") or ""),
    )
    if not decision.allowed:
        logger.info("watcher event denied by privacy policy: reason=%s", decision.reason)
    return decision.allowed


def _sanitize_trigger(
    trigger: dict[str, Any] | None, meta: window_meta.WindowMeta
) -> dict[str, Any]:
    """Project a watcher frame to non-content exact identity fields.

    Watcher ``details`` can contain a focused AX value, including a URL or
    authored text observed before the queued capture runs. It is useful only
    as a wake-up signal: the exact AX snapshot below is the authoritative
    content. Never copy raw details or arbitrary event strings into JSON,
    session hooks, FTS, logs, or prompts.
    """
    raw_type = "heartbeat" if trigger is None else trigger.get("event_type")
    event_type = (
        raw_type
        if isinstance(raw_type, str) and raw_type in _PERSISTED_TRIGGER_TYPES
        else "unknown"
    )
    return {
        "event_type": event_type,
        "app_name": meta.app_name,
        "bundle_id": meta.bundle_id,
        "window_title": meta.title,
        "pid": meta.pid,
        "window_id": meta.window_id,
    }


def _session_hook_event(out: dict[str, Any]) -> dict[str, Any]:
    """Build the identity-only event handed to SessionManager after write."""
    meta = out.get("window_meta") or {}
    trigger = out.get("trigger") or {}
    event = {
        "event_type": str(trigger.get("event_type") or "unknown"),
        "app_name": str(meta.get("app_name") or ""),
        "bundle_id": str(meta.get("bundle_id") or ""),
        "window_title": str(meta.get("title") or ""),
        "timestamp": str(out.get("timestamp") or ""),
    }
    for field in ("pid", "window_id"):
        value = meta.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            event[field] = value
    persisted_tick = out.get("_persisted_monotonic_tick")
    if (
        isinstance(persisted_tick, (int, float))
        and not isinstance(persisted_tick, bool)
        and math.isfinite(float(persisted_tick))
        and persisted_tick >= 0
    ):
        event["_persisted_monotonic_tick"] = float(persisted_tick)
    return event


def _trigger_matches_window(
    trigger: dict[str, Any], window_identity: window_meta.WindowMeta | dict[str, Any]
) -> bool:
    """Bind a queued watcher event to the window captured later."""
    event_type = str(trigger.get("event_type") or "")
    if event_type in {"heartbeat", "manual"}:
        return True
    source = {
        "bundle_id": str(trigger.get("bundle_id") or ""),
        "window_title": str(trigger.get("window_title") or ""),
    }
    # A watcher event can carry user-entered details. Without two concrete
    # titles, bundle-only matching could attach those details to a different
    # window of the same app after the event waited in the queue.
    if not source["window_title"].strip():
        return False
    if isinstance(window_identity, window_meta.WindowMeta):
        target_bundle = window_identity.bundle_id
        target_title = window_identity.title
        target_pid = window_identity.pid
    else:
        target_bundle = str(window_identity.get("bundle_id") or "")
        target_title = str(
            window_identity.get("window_title") or window_identity.get("title") or ""
        )
        target_pid = window_identity.get("pid")
    if not target_title.strip():
        return False
    if source["bundle_id"].strip().casefold() != target_bundle.strip().casefold():
        return False
    if source["window_title"].strip() != target_title.strip():
        return False

    # The native watcher includes a PID when available. Treat a present but
    # malformed value as a mismatch, and use a valid one to distinguish app
    # relaunches that retained the same title and bundle ID.
    if "pid" in trigger:
        source_pid = trigger.get("pid")
        if (
            isinstance(source_pid, bool)
            or not isinstance(source_pid, int)
            or source_pid <= 0
            or isinstance(target_pid, bool)
            or not isinstance(target_pid, int)
            or source_pid != target_pid
        ):
            return False
    return True


def _serialize_window_meta(meta: window_meta.WindowMeta) -> dict[str, Any]:
    """Persist the exact identity without the helper transport schema field."""
    if not meta.capture_ready or meta.bounds is None:
        raise ValueError("window metadata is not capture-ready")
    return {
        "app_name": meta.app_name,
        "title": meta.title,
        "bundle_id": meta.bundle_id,
        "pid": meta.pid,
        "window_id": meta.window_id,
        "bounds": meta.bounds.to_dict(),
    }


def _canonical_ax_tree(raw_json: object) -> dict[str, Any] | None:
    """Strictly rebuild the only AX helper schema safe to persist.

    The provider protocol is injectable and native helpers can be stale or
    damaged. Scanning selected fields is insufficient when the entire object
    becomes durable JSON: an unknown root, node, or metadata key could carry a
    URL that never reached policy. Unknown keys/types and resource overages
    therefore reject the observation instead of being copied through.
    """
    if not isinstance(raw_json, dict) or not set(raw_json).issubset(_AX_ROOT_KEYS):
        return None
    try:
        raw_apps = raw_json.get("apps")
        if not isinstance(raw_apps, list) or len(raw_apps) != 1:
            raise _AXSchemaError
        raw_app = raw_apps[0]
        if not isinstance(raw_app, dict) or set(raw_app) != _AX_APP_KEYS:
            raise _AXSchemaError

        budget = _AXSchemaBudget()
        app_pid = raw_app.get("pid")
        if isinstance(app_pid, bool) or not isinstance(app_pid, int) or app_pid <= 0:
            raise _AXSchemaError
        if raw_app.get("is_frontmost") is not True:
            raise _AXSchemaError
        app_name = budget.consume_string(raw_app.get("name"))
        bundle_id = budget.consume_string(raw_app.get("bundle_id"))

        raw_windows = raw_app.get("windows")
        if not isinstance(raw_windows, list) or len(raw_windows) != 1:
            raise _AXSchemaError
        window = _canonical_ax_window(raw_windows[0], budget)

        identity = window_meta.parse_window_meta(raw_json.get("window_meta"))
        if identity is None:
            raise _AXSchemaError
        identity_dict = identity.to_capture_request()
        if identity_dict is None:
            raise _AXSchemaError

        # The helper timestamp is redundant with the scheduler's authoritative
        # timestamp and is deliberately not copied from the untrusted payload.
        return {
            "window_meta": identity_dict,
            "apps": [
                {
                    "pid": app_pid,
                    "name": app_name,
                    "bundle_id": bundle_id,
                    "is_frontmost": True,
                    "windows": [window],
                }
            ],
        }
    except (_AXSchemaError, RecursionError, UnicodeError):
        return None


def _canonical_ax_window(value: object, budget: _AXSchemaBudget) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value).issubset(_AX_WINDOW_KEYS):
        raise _AXSchemaError
    if "title" not in value or value.get("focused") is not True:
        raise _AXSchemaError

    out: dict[str, Any] = {"title": budget.consume_string(value.get("title"))}
    for field in ("subrole", "description", "identifier"):
        if field in value:
            out[field] = budget.consume_string(value[field])
    out["focused"] = True

    raw_elements = value.get("elements", [])
    if not isinstance(raw_elements, list):
        raise _AXSchemaError
    out["elements"] = [_canonical_ax_element(element, budget, depth=2) for element in raw_elements]
    return out


def _canonical_ax_element(
    value: object,
    budget: _AXSchemaBudget,
    *,
    depth: int,
) -> dict[str, Any]:
    if not isinstance(value, dict) or not value or not set(value).issubset(_AX_ELEMENT_KEYS):
        raise _AXSchemaError
    budget.consume_node(depth=depth)
    out: dict[str, Any] = {}
    for field in _AX_ELEMENT_STRING_KEYS:
        if field == "value":
            continue
        if field in value:
            out[field] = budget.consume_string(value[field])
    if "value" in value:
        if not isinstance(value["value"], str):
            raise _AXSchemaError
        # Redact again at the Python trust boundary. The bundled helper already
        # replaces secure values, but an injected, stale, or damaged provider
        # must never be able to persist plaintext by claiming a secure subrole.
        safe_value = "[REDACTED]" if out.get("subrole") == "AXSecureTextField" else value["value"]
        out["value"] = budget.consume_string(safe_value)
    for field in _AX_ELEMENT_STRING_LIST_KEYS:
        if field not in value:
            continue
        raw_strings = value[field]
        if not isinstance(raw_strings, list):
            raise _AXSchemaError
        out[field] = [budget.consume_string(item) for item in raw_strings]
    if "children" in value:
        raw_children = value["children"]
        if not isinstance(raw_children, list):
            raise _AXSchemaError
        out["children"] = [
            _canonical_ax_element(child, budget, depth=depth + 1) for child in raw_children
        ]
    return out


def _ax_identity(raw_json: Any) -> window_meta.WindowMeta | None:
    """Extract the single frontmost app/window identity from helper output."""
    if not isinstance(raw_json, dict):
        return None
    apps = raw_json.get("apps")
    if not isinstance(apps, list) or len(apps) != 1 or not isinstance(apps[0], dict):
        return None
    app = apps[0]
    if app.get("is_frontmost") is not True:
        return None
    app_pid = app.get("pid")
    if isinstance(app_pid, bool) or not isinstance(app_pid, int) or app_pid <= 0:
        return None
    app_name = app.get("name")
    bundle_id = app.get("bundle_id")
    if not isinstance(app_name, str) or not isinstance(bundle_id, str):
        return None
    windows = app.get("windows")
    # The persisted payload is the whole raw JSON, so validating only one
    # focused window while retaining sibling windows would bypass title-based
    # exclusions. The helper is invoked in focused-window-only mode; enforce
    # that contract at the privacy boundary and fail closed if it is violated.
    if not isinstance(windows, list) or len(windows) != 1 or not isinstance(windows[0], dict):
        return None
    window = windows[0]
    if window.get("focused") is not True or not isinstance(window.get("title"), str):
        return None

    identity = window_meta.parse_window_meta(raw_json.get("window_meta"))
    if identity is None:
        return None
    if (
        identity.pid != app_pid
        or identity.app_name != app_name
        or identity.bundle_id != bundle_id
        or identity.title != window["title"]
    ):
        return None
    return identity


def _write_capture(out: dict[str, Any]) -> Path:
    """Persist a built capture dict to the buffer, index it for search, and log."""
    observation_id = str(out.get("observation_id") or f"obs_{uuid.uuid4().hex}")
    if not observation_id.startswith("obs_"):
        observation_id = f"obs_{uuid.uuid4().hex}"
    out["observation_id"] = observation_id
    timestamp_at_persist = out.pop("_timestamp_at_persist", False) is True
    timestamp_provider = out.pop("_timestamp_provider", None)
    out.pop("_persisted_monotonic_tick", None)
    persisted_monotonic_tick: float | None = None
    # JSON and its searchable projection are one logical capture-store write.
    # Serialize it with collection-wide cleanup/rebuild commands so a rebuild
    # cannot snapshot the directory between these two operations.  A timeline
    # snapshot takes this same lock; assigning the timestamp here means a
    # capture is either visible in its closed bucket or belongs to a later one.
    with store_lock.capture_store_lock():
        if timestamp_at_persist:
            if timestamp_provider is None:
                out["timestamp"] = _now_iso()
            elif callable(timestamp_provider):
                out["timestamp"], persisted_monotonic_tick = _timestamp_sample_from_provider(
                    timestamp_provider
                )
            else:
                raise TypeError("capture timestamp provider is not callable")
        ts = str(out["timestamp"])
        path = paths.capture_buffer_dir() / (f"{filenames.capture_stem(ts, observation_id)}.json")
        _atomic_write_json(path, out)
        failpoints.hit("capture.fts.before_write")
        if _index_capture(path.stem, out):
            failpoints.hit("capture.fts.after_write")
        if persisted_monotonic_tick is not None:
            # Private post-write envelope only. It is assigned after JSON/FTS
            # publication and consumed synchronously by the session hook.
            out["_persisted_monotonic_tick"] = persisted_monotonic_tick
    meta = out.get("window_meta") or {}
    logger.info(
        "capture ok: %s trigger=%s app=%r title=%r ax=%s screenshot=%s",
        path.name,
        (out.get("trigger") or {}).get("event_type"),
        meta.get("app_name"),
        (meta.get("title") or "")[:60],
        "ax_tree" in out,
        "screenshot" in out,
    )
    return path


def _atomic_write_json(
    path: Path,
    data: dict[str, Any],
    *,
    preserve_times: bool = False,
) -> None:
    """Write private capture data atomically with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_stat = None
    if preserve_times:
        with contextlib.suppress(OSError):
            previous_stat = path.stat()
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1  # ownership transferred; do not close a reused descriptor
        with handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_stat is not None:
            os.utime(
                tmp_path,
                ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns),
            )
        failpoints.hit("capture.json.before_rename")
        os.replace(tmp_path, path)
        failpoints.hit("capture.json.after_rename")
        with contextlib.suppress(OSError):
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _index_capture(file_stem: str, out: dict[str, Any]) -> bool:
    """Insert/upsert the capture's S1 fields into the FTS5 index.

    Failures here are non-fatal — a missed FTS row is recoverable via
    ``openchronicle rebuild-captures-index``; killing the capture worker
    over an indexing hiccup would lose the JSON too.
    """
    meta = out.get("window_meta") or {}
    focused = out.get("focused_element") or {}
    try:
        with fts_store.cursor() as conn:
            fts_store.insert_capture(
                conn,
                id=file_stem,
                observation_id=str(out.get("observation_id") or ""),
                timestamp=out.get("timestamp", ""),
                app_name=meta.get("app_name") or "",
                bundle_id=meta.get("bundle_id") or "",
                window_title=meta.get("title") or "",
                focused_role=focused.get("role") or "",
                focused_value=focused.get("value") or "",
                visible_text=out.get("visible_text") or "",
                url=out.get("url") or "",
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("captures FTS insert failed for %s: %s", file_stem, exc)
        return False


def _content_fingerprint(out: dict[str, Any]) -> str:
    """Hash the content-bearing fields of a capture for consecutive-duplicate detection.

    Excludes timestamp, trigger metadata, screenshots, and the raw ax_tree (which
    contains coordinate noise). Focuses on what actually drives downstream stages:
    the window identity + what the user can see + what they've typed.
    """
    meta = out.get("window_meta") or {}
    focused = out.get("focused_element") or {}
    payload = "\x1f".join(
        [
            meta.get("bundle_id") or "",
            meta.get("title") or "",
            focused.get("role") or "",
            focused.get("value") or "",
            out.get("visible_text") or "",
            out.get("url") or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def capture_once(
    cfg: CaptureConfig,
    provider: ax_capture.AXProvider,
    *,
    trigger: dict[str, Any] | None = None,
) -> Path | None:
    """Perform one capture and write it to the buffer. Returns the file path on success.

    ``trigger`` (optional) carries the watcher event metadata that caused this
    capture. When absent the capture is treated as a heartbeat / manual tick.

    This helper always writes — content-dedup lives in ``_CaptureRunner`` so the
    CLI ``capture-once`` smoke test still produces a fresh file on demand.
    """
    out = _build_capture(cfg, provider, trigger)
    if out is None:
        return None
    return _write_capture(out)


class _CaptureRunner:
    """Serializes capture_once calls from the watcher thread + heartbeat task.

    Captures execute on a single dedicated worker thread fed by a bounded
    queue, so the watcher reader thread never blocks on AX / screenshot I/O
    and a runaway burst of events can never spawn unbounded threads.

    Also enforces *consecutive-duplicate dedup*: if the content fingerprint
    (bundle+title+focused value+visible_text+url) matches the previously
    written capture, the new one is dropped. Time-based dedup in the
    dispatcher handles rapid-fire bursts; this handles a static screen
    (e.g. the lock screen overnight) that keeps generating identical
    captures. When deduped, the ``pre_capture_hook`` is NOT fired, so the
    session manager's idle timer isn't reset by meaningless repetition.
    """

    # Bounded queue for backpressure. Captures are de-duplicated by the
    # dispatcher upstream and again by content-fingerprint here, so a
    # backlog past this size is a sign the worker is stuck or LLM/AX
    # calls are slow — drop with a warning rather than build an
    # unbounded thread/memory backlog.
    _MAX_PENDING = 16
    _SENTINEL: Any = object()

    def __init__(
        self,
        cfg: CaptureConfig,
        provider: ax_capture.AXProvider,
        *,
        pre_capture_hook: Callable[[dict[str, Any]], None] | None = None,
        timestamp_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._cfg = cfg
        self._provider = provider
        self._pre_capture_hook = pre_capture_hook
        self._timestamp_provider = timestamp_provider
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._last_fingerprint: str | None = None
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self._MAX_PENDING)
        self._worker: threading.Thread | None = None
        # Direct ``run`` calls are used by capture-once style callers and by
        # focused tests, so a newly-created runner starts open. ``stop_worker``
        # closes this publish gate even when no dedicated worker was started.
        self._accepting = True

    def start_worker(self) -> None:
        """Spawn the dedicated worker thread. Idempotent."""
        with self._lifecycle_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            # A stopped runner gets a fresh queue. Reusing a queue that still
            # contains a shutdown sentinel would make the replacement worker
            # exit before accepting its first trigger.
            self._queue = queue.Queue(maxsize=self._MAX_PENDING)
            self._accepting = True
            worker = threading.Thread(
                target=self._worker_loop,
                name="capture-worker",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def stop_worker(self, *, timeout: float = 5.0) -> None:
        """Stop accepting work and prevent persistence after this returns.

        Pending triggers are intentionally discarded. A trigger already in
        AX/screenshot collection may finish building, but the lifecycle gate
        below prevents it from writing or firing the session hook once stop
        has begun. If the native call outlives ``timeout``, retain the live
        thread handle; it will clear itself only after it actually exits.
        """
        with self._lifecycle_lock:
            self._accepting = False
            worker = self._worker
            pending_queue = self._queue
        if worker is None:
            return

        # No producer can enqueue after ``_accepting`` flips under the same
        # lifecycle lock. Emptying the bounded queue therefore guarantees
        # room for the sentinel even when shutdown begins at full capacity.
        while True:
            try:
                pending_queue.get_nowait()
            except queue.Empty:
                break
        pending_queue.put_nowait(self._SENTINEL)

        worker.join(timeout=timeout)
        if worker.is_alive():
            logger.warning("capture worker did not exit within %.1fs", timeout)
            return
        with self._lifecycle_lock:
            if self._worker is worker:
                self._worker = None

    def _worker_loop(self) -> None:
        worker = threading.current_thread()
        try:
            while True:
                item = self._queue.get()
                if item is self._SENTINEL:
                    return
                self.run(item)
        finally:
            with self._lifecycle_lock:
                if self._worker is worker:
                    self._worker = None
                    self._accepting = False

    def run(self, trigger: dict[str, Any] | None) -> None:
        # Serialize so two near-simultaneous triggers don't double-capture.
        with self._lock:
            try:
                with self._lifecycle_lock:
                    if not self._accepting:
                        return
                out = _build_capture(self._cfg, self._provider, trigger)
                if out is None:
                    return
                fingerprint = _content_fingerprint(out)
                # This lock is the shutdown publication barrier. ``stop_worker``
                # either closes the gate before we enter (drop the completed
                # native result), or waits until write + hook have both ended.
                with self._lifecycle_lock:
                    if not self._accepting:
                        logger.debug("capture discarded because runner is stopping")
                        return
                    if fingerprint == self._last_fingerprint:
                        meta = out.get("window_meta") or {}
                        logger.debug(
                            "capture skipped (content dedup): trigger=%s app=%r title=%r",
                            (trigger or {}).get("event_type"),
                            meta.get("app_name"),
                            (meta.get("title") or "")[:60],
                        )
                        return
                    if self._timestamp_provider is not None:
                        # Private callable consumed under the capture-store
                        # lock; it can never cross the JSON/FTS boundary.
                        out["_timestamp_provider"] = self._timestamp_provider
                    _write_capture(out)
                    # A failed atomic write must remain retryable. Advancing the
                    # dedup bookmark before persistence would make the next
                    # identical observation disappear after a transient I/O error.
                    self._last_fingerprint = fingerprint
                    if self._pre_capture_hook is not None:
                        try:
                            self._pre_capture_hook(_session_hook_event(out))
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("pre_capture_hook failed: %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.error("capture failed: %s", exc, exc_info=True)

    def run_threaded(self, trigger: dict[str, Any] | None) -> None:
        """Enqueue a capture for the worker thread; drop with a warning if full."""
        with self._lifecycle_lock:
            worker = self._worker
            if not self._accepting or worker is None or not worker.is_alive():
                logger.debug("capture trigger ignored because worker is not accepting")
                return
            try:
                self._queue.put_nowait(trigger)
            except queue.Full:
                logger.warning(
                    "capture queue full (%d pending); dropping trigger=%s",
                    self._queue.qsize(),
                    (trigger or {}).get("event_type") if trigger else "heartbeat",
                )


async def run_forever(
    cfg: CaptureConfig,
    *,
    pre_capture_hook: Callable[[dict[str, Any]], None] | None = None,
    timestamp_provider: Callable[[], datetime] | None = None,
) -> None:
    """Run the capture pipeline until cancelled.

    If ``cfg.event_driven`` is true, starts the watcher subprocess and routes
    events through the dispatcher. A heartbeat timer also runs so long idle
    periods (no window changes, no typing) still get periodic snapshots.

    ``pre_capture_hook`` (optional) fires with a fixed identity-only event for
    every capture that actually wrote new content to the buffer — duplicates
    collapsed by content-dedup do NOT fire it, so the session manager's idle
    timer isn't refreshed by a screen that isn't changing (e.g. the lock
    screen overnight).
    """
    provider = ax_capture.create_provider(depth=cfg.ax_depth, timeout=cfg.ax_timeout_seconds)
    if not provider.available:
        logger.warning("AX capture unavailable: %s", getattr(provider, "reason", "unknown reason"))

    runner = _CaptureRunner(
        cfg,
        provider,
        pre_capture_hook=pre_capture_hook,
        timestamp_provider=timestamp_provider,
    )
    runner.start_worker()
    watcher: AXWatcherProcess | None = None
    dispatcher: EventDispatcher | None = None

    def _on_capture(trigger: dict[str, Any] | None) -> None:
        # Hook firing is deferred into the runner so content-deduped captures
        # (e.g. overnight lock-screen repeats) don't refresh the session timer.
        runner.run_threaded(trigger)

    if cfg.event_driven:
        watcher = AXWatcherProcess()
        if watcher.available:
            dispatcher = EventDispatcher(
                _on_capture,
                event_filter=lambda event: _event_allowed(cfg, event),
                debounce_seconds=cfg.debounce_seconds,
                min_capture_gap_seconds=cfg.min_capture_gap_seconds,
                dedup_interval_seconds=cfg.dedup_interval_seconds,
                same_window_dedup_seconds=cfg.same_window_dedup_seconds,
            )
            watcher.on_event(dispatcher.on_event)
            watcher.start()
            logger.info("event-driven capture started")
        else:
            logger.warning("AX watcher unavailable — falling back to heartbeat-only captures")

    # One capture immediately so the user sees something in the buffer right away.
    runner.run_threaded(None)

    try:
        if cfg.heartbeat_minutes > 0:
            heartbeat_interval = max(60.0, cfg.heartbeat_minutes * 60.0)
            logger.info(
                "heartbeat capture every %.0fs (event_driven=%s)",
                heartbeat_interval,
                cfg.event_driven,
            )
            while True:
                await asyncio.sleep(heartbeat_interval)
                # Heartbeats share the same bounded worker and shutdown gate as
                # watcher events. An executor future cannot be cancelled once
                # its native capture has started and could otherwise write
                # after this coroutine's ``finally`` has returned.
                runner.run_threaded(None)
        else:
            logger.info(
                "heartbeat disabled (heartbeat_minutes=%d); event-driven only",
                cfg.heartbeat_minutes,
            )
            # Park until the task is cancelled so the watcher keeps streaming.
            await asyncio.Event().wait()
    finally:
        # Stop in producer→consumer order so no new work piles up after we've
        # closed the worker: watcher (no new events) → dispatcher (cancel
        # debounce) → runner worker (discard backlog + join).
        if watcher is not None:
            watcher.stop()
        if dispatcher is not None:
            dispatcher.shutdown()
        runner.stop_worker()


def cleanup_buffer(
    retention_hours: int,
    processed_before_ts: str | None = None,
    *,
    screenshot_retention_hours: int | None = None,
    max_mb: int = 0,
    capture_config: CaptureConfig | None = None,
) -> dict[str, int]:
    """Tiered buffer hygiene. Returns {deleted, stripped, evicted}.

    Three passes, all gated on ``processed_before_ts`` so an unprocessed
    trailing capture is never evicted:

    1. **Delete whole file** when mtime is older than ``retention_hours``.
    2. **Strip screenshot** when mtime is older than
       ``screenshot_retention_hours`` (if provided and smaller than
       ``retention_hours``). The screenshot field is 77% of the payload and is
       excluded from model/timeline/reducer/classifier inputs; only an explicit
       attested MCP read consumes it. Stripping keeps AX+text queryable for much
       longer at ~20% of the original size.
    3. **Evict by size** once total buffer size exceeds ``max_mb`` MB.
       Oldest already-absorbed files go first. ``max_mb=0`` disables this.
    """
    absorbed_before = None
    if processed_before_ts is not None:
        absorbed_before = filenames.parse_timestamp(processed_before_ts)
        if absorbed_before is None:
            logger.error("buffer cleanup skipped: invalid processed boundary")
            return {"deleted": 0, "stripped": 0, "evicted": 0}

    with store_files.review_operation_lock(), store_lock.capture_store_lock():
        return _cleanup_buffer_locked(
            retention_hours,
            absorbed_before,
            screenshot_retention_hours=screenshot_retention_hours,
            max_mb=max_mb,
            capture_config=capture_config or CaptureConfig(),
        )


def _cleanup_buffer_locked(
    retention_hours: int,
    absorbed_before: datetime | None,
    *,
    screenshot_retention_hours: int | None,
    max_mb: int,
    capture_config: CaptureConfig,
) -> dict[str, int]:
    """Implement cleanup with durable all-or-none window retirement."""
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return {"deleted": 0, "stripped": 0, "evicted": 0}

    now = time.time()
    delete_cutoff = now - retention_hours * 3600
    strip_cutoff = (
        now - screenshot_retention_hours * 3600
        if screenshot_retention_hours and screenshot_retention_hours > 0
        else None
    )

    deleted = stripped = evicted = 0
    for path in sorted(buf.iterdir()):
        if path.is_file() and filenames.is_capture_temp_name(path.name):
            # Every live capture writer holds capture_store_lock across its
            # temp lifetime. Seeing one while we own that lock proves its
            # writer died before rename, so it is safe to purge immediately.
            try:
                path.unlink()
                deleted += 1
            except OSError:
                pass
    records = _buffer_records(buf)
    live_groups, retiring_groups = _eligible_window_cleanup_groups(
        records,
        absorbed_before=absorbed_before,
        capture_config=capture_config,
    )

    # A crash/partial unlink leaves a durable retiring root plus the original
    # per-path manifest. Retry only exact residual members, independent of the
    # current retention clock, then finalize once the entire manifest is absent.
    retry_records = [record for group in retiring_groups for record in group.records]
    if retry_records:
        removed, failures = _unlink_authorized_capture_paths(retry_records)
        deleted += len(removed)
        _finish_capture_unlinks(
            removed=removed,
            failures=failures,
            operation="automatic retirement retry",
        )
    else:
        _finish_capture_unlinks(
            removed=[],
            failures=[],
            operation="automatic retirement recovery",
        )

    # Retention is a whole-window decision: every manifest member must be old.
    retention_groups = [
        group
        for group in live_groups
        if group.records and all(record.mtime <= delete_cutoff for record in group.records)
    ]
    retention_records = [record for group in retention_groups for record in group.records]
    retention_paths = [record.path for record in retention_records]
    if retention_paths and _delete_captures_from_fts([path.stem for path in retention_paths]):
        removed, failures = _unlink_authorized_capture_paths(retention_records)
        deleted += len(removed)
        _finish_capture_unlinks(
            removed=removed,
            failures=failures,
            operation="automatic retention",
        )

    # Pixel stripping is projection-neutral under observation digest v2. It is
    # intentionally per-file and may run even before the whole window is
    # absorbed, while malformed/symlinked files remain untouched.
    if strip_cutoff is not None:
        for record in _buffer_records(buf):
            if (
                record.binding is not None
                and record.mtime <= strip_cutoff
                and _strip_screenshot_inplace(record.path)
            ):
                stripped += 1

    if max_mb > 0:
        limit = max_mb * 1024 * 1024
        records = _buffer_records(buf)
        total = sum(record.size for record in records)
        if total > limit:
            live_groups, _retiring_groups = _eligible_window_cleanup_groups(
                records,
                absorbed_before=absorbed_before,
                capture_config=capture_config,
            )
            live_groups.sort(
                key=lambda group: timeline_store.as_instant(group.receipt.window_start)
            )
            eviction_groups: list[_WindowCleanupGroup] = []
            projected_total = total
            for group in live_groups:
                if projected_total <= limit:
                    break
                eviction_groups.append(group)
                projected_total -= sum(record.size for record in group.records)

            eviction_records = [record for group in eviction_groups for record in group.records]
            eviction_paths = [record.path for record in eviction_records]
            if eviction_paths and _delete_captures_from_fts([path.stem for path in eviction_paths]):
                evicted_paths, eviction_failures = _unlink_authorized_capture_paths(
                    eviction_records
                )
                evicted += len(evicted_paths)
                _finish_capture_unlinks(
                    removed=evicted_paths,
                    failures=eviction_failures,
                    operation="automatic size eviction",
                )

    return {"deleted": deleted, "stripped": stripped, "evicted": evicted}


def _buffer_records(buf: Path) -> list[_BufferRecord]:
    records: list[_BufferRecord] = []
    for path in sorted(buf.iterdir(), key=lambda candidate: candidate.name):
        if path.suffix != ".json":
            continue
        try:
            stat = path.lstat()
        except OSError:
            continue
        records.append(
            _BufferRecord(
                mtime=stat.st_mtime,
                path=path,
                size=stat.st_size,
                capture_time=filenames.parse_capture_stem(path.stem),
                binding=_capture_semantic_binding(path),
            )
        )
    return records


def _capture_semantic_binding(path: Path) -> tuple[str, str, str, str] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or any(
        marker in data
        for marker in (
            "__openchronicle_dropped_screenshot",
            "__openchronicle_source_digest",
        )
    ):
        return None
    timestamp_raw = data.get("timestamp")
    observation_id = data.get("observation_id")
    timestamp = filenames.parse_timestamp(timestamp_raw) if isinstance(timestamp_raw, str) else None
    stem_timestamp = filenames.parse_capture_stem(path.stem)
    if (
        timestamp is None
        or stem_timestamp is None
        or timeline_store.as_instant(timestamp) != timeline_store.as_instant(stem_timestamp)
        or not isinstance(observation_id, str)
        or not observation_id.startswith("obs_")
    ):
        return None
    try:
        canonical_stem = filenames.capture_stem(timestamp_raw, observation_id)
        legacy_stem = filenames.safe_timestamp(timestamp_raw)
    except ValueError:
        return None
    if path.stem not in {canonical_stem, legacy_stem}:
        return None
    return (path.name, observation_id, observation_digest(data), timestamp_raw)


def _eligible_window_cleanup_groups(
    records: list[_BufferRecord],
    *,
    absorbed_before: datetime | None,
    capture_config: CaptureConfig,
) -> tuple[list[_WindowCleanupGroup], list[_WindowCleanupGroup]]:
    try:
        with fts_store.cursor() as conn:
            if not timeline_store.capture_receipts_enabled(conn):
                return [], []
            policy_digest = privacy_policy.stored_observation_policy_digest(capture_config)
            receipts = timeline_store.window_receipts_in_raw_states(
                conn,
                "live",
                "retiring",
            )
            live: list[_WindowCleanupGroup] = []
            retiring: list[_WindowCleanupGroup] = []
            for receipt in receipts:
                if not timeline_store.window_receipt_is_current(conn, receipt):
                    continue
                manifest = timeline_store.capture_bindings_for_window(conn, receipt)
                expected = {binding[0]: binding for binding in manifest}
                window_records = tuple(
                    record
                    for record in records
                    if record.capture_time is not None
                    and timeline_store.as_instant(receipt.window_start)
                    <= timeline_store.as_instant(record.capture_time)
                    < timeline_store.as_instant(receipt.window_end)
                )
                if receipt.raw_state == "retiring":
                    if any(record.path.name not in expected for record in window_records):
                        continue
                    # An expected pathname that still exists but cannot be
                    # classified is neither proof that the old bytes remain
                    # nor proof that a valid new observation replaced them.
                    # Keep the frozen manifest and its deny marker intact for
                    # malformed files, symlinks, and filename/payload identity
                    # mismatches. Only a different *valid* binding may be
                    # released as new late evidence.
                    if any(record.binding is None for record in window_records):
                        continue
                    residual_list: list[_BufferRecord] = []
                    for record in window_records:
                        if record.binding == expected.get(record.path.name):
                            residual_list.append(record)
                        else:
                            # This filename now holds new evidence, not the old
                            # authorized bytes. Release its obsolete deny marker
                            # and exclude it from the frozen retry manifest.
                            candidate_store.delete_tombstone(
                                conn,
                                kind="capture_file",
                                artifact_id=record.path.name,
                            )
                    residual = tuple(residual_list)
                    retiring.append(_WindowCleanupGroup(receipt, residual))
                    continue
                if receipt.policy_digest != policy_digest:
                    continue
                if absorbed_before is None:
                    continue
                bindings = [record.binding for record in window_records]
                if (
                    any(binding is None for binding in bindings)
                    or sorted(binding for binding in bindings if binding is not None)
                    != sorted(manifest)
                    or any(
                        record.capture_time is None
                        or timeline_store.as_instant(record.capture_time)
                        >= timeline_store.as_instant(absorbed_before)
                        for record in window_records
                    )
                ):
                    continue
                allowed = [
                    record.binding
                    for record in window_records
                    if record.binding is not None
                    and _capture_allowed_by_policy(record.path, capture_config)
                ]
                if not _receipt_matches_allowed_bindings(conn, receipt, allowed):
                    continue
                live.append(_WindowCleanupGroup(receipt, window_records))
            return live, retiring
    except Exception as exc:  # noqa: BLE001
        logger.warning("capture cleanup could not verify window receipts: %s", exc)
        return [], []


def _capture_allowed_by_policy(path: Path, cfg: CaptureConfig) -> bool:
    try:
        data = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return False
    return privacy_policy.evaluate_stored_observation(cfg, observation=data).allowed


def _receipt_matches_allowed_bindings(
    conn,
    receipt: timeline_store.WindowReceipt,
    allowed_bindings: list[tuple[str, str, str, str]],
) -> bool:
    if receipt.outcome == "policy_excluded":
        return (
            not allowed_bindings
            and timeline_store.window_state(
                conn,
                receipt.window_start,
                receipt.window_end,
            )
            == "missing"
        )
    if receipt.outcome != "block":
        return False
    block = timeline_store.get_window(conn, receipt.window_start, receipt.window_end)
    if block is None:
        return False
    expected_sources = [
        EvidenceRef(
            kind="observation",
            id=observation_id,
            path=capture_path,
            timestamp=capture_time,
            content_hash=source_hash,
        )
        for capture_path, observation_id, source_hash, capture_time in allowed_bindings
    ]
    return (
        provenance_store.direct_sources_checked(
            conn,
            EvidenceRef(kind="timeline_block", id=block.id),
        )
        == expected_sources
    )


def _unlink_authorized_capture_paths(
    records_to_unlink: list[_BufferRecord],
) -> tuple[list[Path], list[tuple[Path, OSError]]]:
    """Unlink only bytes still equal to the frozen receipt binding.

    The database transition happens before filesystem mutation so readers are
    denied first. An external sync tool does not honor our advisory capture
    lock, however, and can replace a pathname after that transition. Recheck
    the semantic identity immediately before unlink; a changed valid file is
    released later as new evidence, while an unclassifiable residual remains
    tombstoned and retiring.
    """
    removed: list[Path] = []
    failures: list[tuple[Path, OSError]] = []
    for record in records_to_unlink:
        path = record.path
        if record.binding is None or _capture_semantic_binding(path) != record.binding:
            continue
        try:
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append((path, exc))
    return removed, failures


def _delete_captures_from_fts(stems: list[str]) -> bool:
    """Atomically deny reads, drop FTS rows, and start window retirement."""
    if not stems:
        return True
    selected_names = {f"{stem}.json" for stem in stems}
    try:
        with fts_store.cursor() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                window_keys = conn.execute(
                    """
                    SELECT DISTINCT window_start, window_end
                      FROM timeline_capture_receipts
                     WHERE capture_path IN ({})
                    """.format(",".join("?" for _ in selected_names)),
                    tuple(sorted(selected_names)),
                ).fetchall()
                receipts: list[timeline_store.WindowReceipt] = []
                authorized_names: set[str] = set()
                for start_raw, end_raw in window_keys:
                    receipt = timeline_store.window_receipt_for(
                        conn,
                        datetime.fromisoformat(start_raw),
                        datetime.fromisoformat(end_raw),
                    )
                    if (
                        receipt is None
                        or receipt.raw_state not in {"live", "retiring"}
                        or not timeline_store.window_receipt_is_current(conn, receipt)
                    ):
                        raise RuntimeError("capture window retirement proof changed")
                    manifest_names = {
                        binding[0]
                        for binding in timeline_store.capture_bindings_for_window(conn, receipt)
                    }
                    if receipt.raw_state == "live" and not manifest_names.issubset(selected_names):
                        raise RuntimeError("partial live window retirement denied")
                    selected_manifest_names = manifest_names & selected_names
                    if not selected_manifest_names:
                        raise RuntimeError("retirement selection has no manifest member")
                    authorized_names.update(selected_manifest_names)
                    receipts.append(receipt)
                if authorized_names != selected_names:
                    raise RuntimeError("capture retirement selection is not fully receipted")

                for name in sorted(selected_names):
                    candidate_store.put_tombstone(
                        conn,
                        kind="capture_file",
                        artifact_id=name,
                    )
                conn.executemany(
                    "DELETE FROM captures WHERE id=?",
                    ((stem,) for stem in stems),
                )
                for receipt in receipts:
                    if receipt.raw_state == "live":
                        timeline_store.transition_window_receipt_raw_state(
                            conn,
                            receipt,
                            "retiring",
                        )
                conn.execute("COMMIT")
            except Exception:  # noqa: BLE001
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("captures FTS delete failed for %d stems: %s", len(stems), exc)
        return False
    return True


def _finish_capture_unlinks(
    *,
    removed: list[Path],
    failures: list[tuple[Path, OSError]],
    operation: str,
) -> None:
    """Finalize complete retiring manifests; retain partial failures safely."""
    current_records = _buffer_records(paths.capture_buffer_dir())
    try:
        with fts_store.cursor() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for path, exc in failures:
                    candidate_store.set_tombstone_error(
                        conn,
                        kind="capture_file",
                        artifact_id=path.name,
                        path="",
                        error=f"{type(exc).__name__}: {operation} unlink failed",
                    )
                for receipt in timeline_store.window_receipts_in_raw_states(
                    conn,
                    "retiring",
                ):
                    manifest = timeline_store.capture_bindings_for_window(conn, receipt)
                    if len(
                        manifest
                    ) != receipt.capture_count or not timeline_store.window_receipt_is_current(
                        conn, receipt
                    ):
                        continue
                    manifest_names = {binding[0] for binding in manifest}
                    window_records = [
                        record
                        for record in current_records
                        if record.capture_time is not None
                        and timeline_store.as_instant(receipt.window_start)
                        <= timeline_store.as_instant(record.capture_time)
                        < timeline_store.as_instant(receipt.window_end)
                    ]
                    if any(record.path.name not in manifest_names for record in window_records):
                        continue
                    complete = True
                    for binding in manifest:
                        name = binding[0]
                        if Path(name).name != name:
                            complete = False
                            break
                        residual = paths.capture_buffer_dir() / name
                        if residual.exists() or residual.is_symlink():
                            current_binding = _capture_semantic_binding(residual)
                            if current_binding is None or current_binding == binding:
                                complete = False
                    if not complete:
                        continue
                    timeline_store.transition_window_receipt_raw_state(
                        conn,
                        receipt,
                        "retired",
                    )
                    conn.execute(
                        "DELETE FROM timeline_capture_receipts "
                        "WHERE window_start=? AND window_end=?",
                        (
                            receipt.window_start.isoformat(timespec="microseconds"),
                            receipt.window_end.isoformat(timespec="microseconds"),
                        ),
                    )
                    for capture_path, _obs, _digest, _timestamp in manifest:
                        candidate_store.delete_tombstone(
                            conn,
                            kind="capture_file",
                            artifact_id=capture_path,
                        )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    except Exception as exc:  # noqa: BLE001
        # A stale marker after a successful unlink is fail-closed. More
        # importantly, a failed unlink's marker was already committed before
        # filesystem mutation and must never be removed by this error path.
        logger.warning("capture tombstone finalization failed: %s", exc)
    if failures:
        logger.warning(
            "%s left %d capture file(s) tombstoned after unlink failure",
            operation,
            len(failures),
        )


def _strip_screenshot_inplace(path: Path) -> bool:
    """Rewrite a capture JSON without its ``screenshot`` field. Returns True if stripped."""
    try:
        raw = path.read_text()
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if "screenshot" not in data:
        return False
    data.pop("screenshot", None)
    data["screenshot_stripped"] = True
    try:
        # Screenshot retention must not reset whole-capture retention. Preserve
        # the authoritative file's original age across the atomic rewrite.
        _atomic_write_json(path, data, preserve_times=True)
        return True
    except OSError:
        return False
