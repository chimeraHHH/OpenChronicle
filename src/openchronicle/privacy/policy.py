"""Deterministic pre-capture policy for active-window metadata."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import CaptureConfig


@dataclass(frozen=True)
class CaptureDecision:
    allowed: bool
    reason: str = "allowed"
    matched_rule: str = ""


def _strings(values: object, *, field: str) -> list[str]:
    """Validate policy lists so malformed TOML cannot weaken an exclusion."""
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{field} must be a list of strings")
    return values


def _normalized(values: object, *, field: str) -> set[str]:
    return {
        value.strip().casefold()
        for value in _strings(values, field=field)
        if value.strip()
    }


def evaluate_window(
    cfg: CaptureConfig,
    *,
    app_name: str,
    bundle_id: str,
    window_title: str,
) -> CaptureDecision:
    """Decide from metadata available before AX or screenshot collection.

    A non-empty bundle allowlist is restrictive: an empty or unknown bundle is
    denied unless explicitly listed. Exclusions always win over the allowlist.
    """
    app = app_name.strip().casefold()
    bundle = bundle_id.strip().casefold()
    title = window_title.strip().casefold()

    # The config loader intentionally ignores unknown keys, but known privacy
    # keys must have the expected shape. Iterating a mistaken TOML string as a
    # list of characters could otherwise turn an exclusion into an allow.
    try:
        if not isinstance(cfg.deny_unknown_windows, bool):
            raise ValueError("deny_unknown_windows must be a boolean")
        if not isinstance(cfg.include_screenshot, bool):
            raise ValueError("include_screenshot must be a boolean")
        excluded_bundles = _normalized(
            cfg.excluded_bundle_ids, field="excluded_bundle_ids"
        )
        excluded_apps = _normalized(cfg.excluded_app_names, field="excluded_app_names")
        title_patterns = _strings(
            cfg.excluded_window_title_patterns,
            field="excluded_window_title_patterns",
        )
        allowed_bundles = _normalized(cfg.allowed_bundle_ids, field="allowed_bundle_ids")
    except ValueError as exc:
        return CaptureDecision(False, "invalid_privacy_policy", str(exc))

    if cfg.deny_unknown_windows and not bundle:
        return CaptureDecision(False, "unknown_bundle_id")

    if bundle in excluded_bundles:
        return CaptureDecision(False, "excluded_bundle_id", bundle_id)

    if excluded_apps and not app:
        return CaptureDecision(False, "unknown_app_name")
    if app in excluded_apps:
        return CaptureDecision(False, "excluded_app_name", app_name)

    title_patterns = [pattern for pattern in title_patterns if pattern.strip()]
    if title_patterns and not title:
        return CaptureDecision(False, "unknown_window_title")
    for pattern in title_patterns:
        normalized_pattern = pattern.strip().casefold()
        if normalized_pattern and normalized_pattern in title:
            return CaptureDecision(False, "excluded_window_title", pattern)

    if allowed_bundles and bundle not in allowed_bundles:
        return CaptureDecision(False, "bundle_not_allowed", bundle_id)

    return CaptureDecision(True)
