"""Deterministic pre-capture policy for active-window metadata."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import SplitResult, urlsplit, urlunsplit

from ..config import CaptureConfig


@dataclass(frozen=True)
class CaptureDecision:
    allowed: bool
    reason: str = "allowed"
    matched_rule: str = ""


_MAX_URL_LENGTH = 4096
_MAX_URL_RULES = 128
_MAX_URL_RULE_LENGTH = 512
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_UNRESERVED_URL_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_URL_METADATA_ROOT_KEYS = frozenset(
    {
        "timestamp",
        "schema_version",
        "observation_id",
        "trigger",
        "window_meta",
        "privacy",
        "url",
        "visible_text",
    }
)
_URL_METADATA_WINDOW_KEYS = frozenset(
    {"app_name", "title", "bundle_id", "pid", "window_id", "bounds"}
)
_URL_METADATA_TRIGGER_KEYS = frozenset(
    {"event_type", "app_name", "bundle_id", "window_title", "pid", "window_id"}
)
_URL_METADATA_PRIVACY_KEYS = frozenset({"decision", "policy_version", "content_mode"})
_URL_METADATA_EVENT_TYPES = frozenset(
    {
        "heartbeat",
        "manual",
        "AXApplicationActivated",
        "AXFocusedWindowChanged",
        "AXValueChanged",
        "UserMouseClick",
        "UserTextInput",
        "unknown",
    }
)
_OBSERVATION_ID_RE = re.compile(r"obs_[0-9a-f]{32}\Z")


def _strings(values: object, *, field: str) -> list[str]:
    """Validate policy lists so malformed TOML cannot weaken an exclusion."""
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{field} must be a list of strings")
    return values


def _normalized(values: object, *, field: str) -> set[str]:
    return {value.strip().casefold() for value in _strings(values, field=field) if value.strip()}


def _url_rules(values: object, *, field: str) -> list[str]:
    """Return validated, normalized literal URL rules.

    Rules are deliberately not compiled as regular expressions.  Bounding the
    list and each literal keeps matching predictable even when configuration is
    attacker-controlled, and regex-looking input has no special meaning.
    """
    raw = _strings(values, field=field)
    if len(raw) > _MAX_URL_RULES:
        raise ValueError(f"{field} must contain at most {_MAX_URL_RULES} entries")

    normalized: list[str] = []
    for index, value in enumerate(raw):
        rule = value.strip()
        if not rule:
            raise ValueError(f"{field}[{index}] must not be empty")
        if len(rule) > _MAX_URL_RULE_LENGTH:
            raise ValueError(f"{field}[{index}] must be at most {_MAX_URL_RULE_LENGTH} characters")
        if any(character.isspace() or ord(character) < 32 for character in rule):
            raise ValueError(f"{field}[{index}] must not contain whitespace or controls")

        if "://" in rule:
            canonical = _normalize_url(rule)
            if canonical is None:
                raise ValueError(f"{field}[{index}] must contain a valid http(s) URL literal")
            # _normalize_url already canonicalizes scheme and host. Preserve
            # path/query/fragment case because servers may distinguish it.
            normalized.append(canonical)
        else:
            canonical = _normalize_percent_component(rule)
            if canonical is None:
                raise ValueError(f"{field}[{index}] contains an invalid percent escape")
            if field == "allowed_url_patterns" and not _looks_like_host_rule(canonical):
                raise ValueError(f"{field}[{index}] must be a bare hostname or full http(s) URL")
            normalized.append(canonical.casefold())
    return normalized


def _normalize_host(host: str) -> str | None:
    host = host.rstrip(".")
    if not host:
        return None
    try:
        return ipaddress.ip_address(host).compressed.casefold()
    except ValueError:
        pass

    try:
        ascii_host = host.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return None
    if len(ascii_host) > 253:
        return None
    labels = ascii_host.split(".")
    if any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        return None
    return ascii_host


def _normalize_url(value: str) -> str | None:
    """Canonicalize a bounded http(s) URL without exposing it in errors."""
    if not isinstance(value, str) or not value or len(value) > _MAX_URL_LENGTH:
        return None
    if "\\" in value or any(character.isspace() or ord(character) < 32 for character in value):
        return None
    try:
        split = urlsplit(value)
        port = split.port
    except ValueError:
        return None
    scheme = split.scheme.casefold()
    if scheme not in {"http", "https"} or not split.netloc or split.hostname is None:
        return None
    # Credentials in an AX address value are too easy to persist accidentally
    # and make host interpretation less obvious, so reject them fail closed.
    if split.username is not None or split.password is not None:
        return None
    host = _normalize_host(split.hostname)
    if host is None:
        return None

    host_for_netloc = f"[{host}]" if ":" in host else host
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host_for_netloc if port is None or default_port else f"{host_for_netloc}:{port}"

    path = _normalize_percent_component(split.path or "/")
    query = _normalize_percent_component(split.query)
    fragment = _normalize_percent_component(split.fragment)
    if path is None or query is None or fragment is None:
        return None

    canonical = SplitResult(
        scheme,
        netloc,
        path,
        query,
        fragment,
    )
    return urlunsplit(canonical)


def _normalize_percent_component(value: str) -> str | None:
    """Canonicalize escapes and decode ASCII unreserved bytes.

    Browsers and servers commonly render ``%70`` and ``p`` equivalently. URL
    policy must not let that harmless representation difference bypass a path
    exclusion. Reserved and non-ASCII bytes remain escaped (with uppercase
    hex) so component boundaries and Unicode byte sequences stay unambiguous.
    """
    output: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "%":
            output.append(character)
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in _HEX_DIGITS
            or value[index + 2] not in _HEX_DIGITS
        ):
            return None
        byte = int(value[index + 1 : index + 3], 16)
        if byte in _UNRESERVED_URL_BYTES:
            output.append(chr(byte))
        else:
            output.append(f"%{byte:02X}")
        index += 3
    return "".join(output)


def _looks_like_host_rule(rule: str) -> bool:
    """Whether a literal should use domain-boundary rather than substring matching."""
    if any(marker in rule for marker in ("/", "?", "#", "[", "]")):
        return False
    # A port is not a bare-host rule.  IPv6 literals are intentionally written
    # as full URLs so brackets and ports normalize unambiguously.
    if ":" in rule:
        return False
    return _normalize_host(rule) is not None


def _url_rule_matches(*, normalized_url: str, host: str, rule: str) -> bool:
    if _looks_like_host_rule(rule):
        normalized_rule_host = _normalize_host(rule)
        return normalized_rule_host is not None and (
            host == normalized_rule_host or host.endswith(f".{normalized_rule_host}")
        )
    if rule.startswith(("http://", "https://")):
        # A full-URL allow rule must describe the observed URL itself, not text
        # embedded in an unrelated URL's query or fragment. Canonical URLs
        # always contain a slash after the authority, so this prefix comparison
        # also preserves exact scheme/host/port boundaries.
        candidate = normalized_url
        if candidate == rule:
            return True
        if not candidate.startswith(rule):
            return False
        if rule.endswith(("/", "?", "#", "&", "=")):
            return True
        return candidate[len(rule)] in "/?#&"
    return rule in normalized_url.casefold()


def has_url_policy(cfg: CaptureConfig) -> bool:
    """Return whether URL extraction must be policy-gated.

    Only two exactly empty lists mean "disabled".  Malformed falsey values are
    intentionally treated as configured so callers cannot skip
    :func:`evaluate_url` and accidentally weaken a bad privacy configuration.
    """
    return cfg.allowed_url_patterns != [] or cfg.excluded_url_patterns != []


def validate_url_policy(cfg: CaptureConfig) -> CaptureDecision:
    """Validate URL policy shape and bounds without inspecting an observed URL."""
    try:
        _url_rules(cfg.allowed_url_patterns, field="allowed_url_patterns")
        _url_rules(cfg.excluded_url_patterns, field="excluded_url_patterns")
    except ValueError as exc:
        return CaptureDecision(False, "invalid_privacy_policy", str(exc))
    return CaptureDecision(True)


def evaluate_url(cfg: CaptureConfig, *, url: object) -> CaptureDecision:
    """Decide whether an AX-extracted URL may enter the capture buffer.

    Call this pure policy function immediately after S1 URL extraction and
    before persistence, screenshots, or logging.  URL exclusions take
    precedence.  Missing or invalid URLs fail closed whenever either URL rule
    list is active.  With no URL policy, a missing URL remains allowed so
    non-browser capture keeps its default behavior.

    Denial metadata names only the policy field or rule index; it never echoes
    the observed URL.
    """
    try:
        allowed = _url_rules(cfg.allowed_url_patterns, field="allowed_url_patterns")
        excluded = _url_rules(cfg.excluded_url_patterns, field="excluded_url_patterns")
    except ValueError as exc:
        return CaptureDecision(False, "invalid_privacy_policy", str(exc))

    policy_enabled = bool(allowed or excluded)
    if url is None or (isinstance(url, str) and not url.strip()):
        if policy_enabled:
            return CaptureDecision(False, "unknown_url")
        return CaptureDecision(True)
    if not isinstance(url, str):
        reason = "unknown_url" if policy_enabled else "invalid_url"
        return CaptureDecision(False, reason)

    normalized_url = _normalize_url(url.strip())
    if normalized_url is None:
        reason = "unknown_url" if policy_enabled else "invalid_url"
        return CaptureDecision(False, reason)
    split = urlsplit(normalized_url)
    host = split.hostname or ""

    for index, rule in enumerate(excluded):
        if _url_rule_matches(normalized_url=normalized_url, host=host, rule=rule):
            return CaptureDecision(False, "excluded_url", f"excluded_url_patterns[{index}]")
    if allowed and not any(
        _url_rule_matches(normalized_url=normalized_url, host=host, rule=rule) for rule in allowed
    ):
        return CaptureDecision(False, "url_not_allowed", "allowed_url_patterns")
    return CaptureDecision(True)


def evaluate_url_candidate(
    cfg: CaptureConfig,
    *,
    url: object,
    scheme_known: bool,
) -> CaptureDecision:
    """Evaluate one URL candidate without inventing an omitted scheme.

    Accessibility often renders an address without ``http://`` or
    ``https://``. The parser keeps a canonical HTTPS-shaped value for existing
    consumers, but that representation is not evidence that the real page used
    HTTPS. A scheme-less candidate is therefore accepted only when *both* HTTP
    and HTTPS interpretations pass. This makes scheme-specific exclusions win
    and prevents a scheme-specific allow rule from granting access on an
    unprovable assumption.
    """
    if not isinstance(scheme_known, bool):
        return CaptureDecision(False, "unknown_url_scheme")
    if scheme_known:
        return evaluate_url(cfg, url=url)
    if not isinstance(url, str) or not url.casefold().startswith("https://"):
        return CaptureDecision(False, "unknown_url_scheme")

    suffix = url[len("https://") :]
    decisions = (
        evaluate_url(cfg, url=f"http://{suffix}"),
        evaluate_url(cfg, url=f"https://{suffix}"),
    )
    denied = next((decision for decision in decisions if not decision.allowed), None)
    if denied is not None:
        return denied
    return CaptureDecision(True)


def evaluate_stored_observation(
    cfg: CaptureConfig,
    *,
    observation: object,
) -> CaptureDecision:
    """Re-evaluate retained capture evidence under the current policy.

    Capture policy is also an egress boundary: a capture that was legal when
    collected can become excluded before a later MCP/model read.  Every such
    read must call this function instead of trusting a historical ``allowed``
    marker.  When URL policy is active, only the exact content-free schema-v5
    projection is eligible; legacy or content-bearing observations fail
    closed because their page/title text cannot be re-bound atomically to the
    retained address-control value.
    """
    if not isinstance(observation, dict):
        return CaptureDecision(False, "invalid_stored_observation")
    timestamp = observation.get("timestamp")
    meta = observation.get("window_meta")
    if not isinstance(timestamp, str) or not isinstance(meta, dict):
        return CaptureDecision(False, "invalid_stored_observation")
    identity_fields = tuple(meta.get(field) for field in ("app_name", "bundle_id", "title"))
    if not all(isinstance(field, str) for field in identity_fields):
        return CaptureDecision(False, "invalid_stored_observation")
    app_name, bundle_id, title = identity_fields
    window_decision = evaluate_window(
        cfg,
        app_name=app_name,
        bundle_id=bundle_id,
        window_title=title,
    )
    if not window_decision.allowed:
        return window_decision
    if has_url_policy(cfg):
        validation = validate_url_policy(cfg)
        if not validation.allowed:
            return validation
        if not _is_exact_url_metadata_observation(observation):
            return CaptureDecision(False, "unverifiable_url_metadata")
        return evaluate_url_candidate(
            cfg,
            url=observation.get("url"),
            scheme_known=True,
        )

    # Older helpers could persist every window belonging to the frontmost app.
    # Rechecking only the top-level window title would then let an excluded
    # sibling window survive in visible_text or a legacy AX rendering. Whenever
    # current window filters are active, require a complete single-window
    # projection whose app/window identity matches the public metadata.
    if _has_window_title_filters(cfg) and not _is_single_window_content_observation(observation):
        return CaptureDecision(False, "unverifiable_window_content")
    return CaptureDecision(True)


def _has_window_title_filters(cfg: CaptureConfig) -> bool:
    # All windows in one historical app-wide AX tree share app/bundle
    # identity. Title filters are the policy dimension that can differ across
    # sibling windows and therefore require a single-window proof.
    return cfg.excluded_window_title_patterns != []


def _is_single_window_content_observation(observation: dict[str, object]) -> bool:
    """Prove that retained normal content came from one identified AX window."""
    if not isinstance(observation.get("visible_text"), str):
        return False
    meta = observation.get("window_meta")
    tree = observation.get("ax_tree")
    if not isinstance(meta, dict) or not isinstance(tree, dict):
        return False
    apps = tree.get("apps")
    if not isinstance(apps, list) or len(apps) != 1 or not isinstance(apps[0], dict):
        return False
    app = apps[0]
    windows = app.get("windows")
    if (
        app.get("is_frontmost") is not True
        or not isinstance(windows, list)
        or len(windows) != 1
        or not isinstance(windows[0], dict)
    ):
        return False
    window = windows[0]
    return bool(
        window.get("focused") is True
        and isinstance(window.get("title"), str)
        and isinstance(app.get("name"), str)
        and isinstance(app.get("bundle_id"), str)
        and app.get("name") == meta.get("app_name")
        and app.get("bundle_id") == meta.get("bundle_id")
        and window.get("title") == meta.get("title")
    )


def _is_exact_url_metadata_observation(observation: dict[str, object]) -> bool:
    """Validate the durable allowlist shape emitted by the URL-policy gate."""
    if set(observation) != _URL_METADATA_ROOT_KEYS:
        return False
    schema_version = observation.get("schema_version")
    observation_id = observation.get("observation_id")
    timestamp = observation.get("timestamp")
    if (
        isinstance(schema_version, bool)
        or schema_version != 5
        or not isinstance(observation_id, str)
        or _OBSERVATION_ID_RE.fullmatch(observation_id) is None
        or not isinstance(timestamp, str)
        or len(timestamp) > 100
        or observation.get("visible_text") != ""
    ):
        return False
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
        return False

    privacy = observation.get("privacy")
    meta = observation.get("window_meta")
    trigger = observation.get("trigger")
    if (
        not isinstance(privacy, dict)
        or set(privacy) != _URL_METADATA_PRIVACY_KEYS
        or privacy.get("decision") != "allowed"
        or isinstance(privacy.get("policy_version"), bool)
        or privacy.get("policy_version") != 3
        or privacy.get("content_mode") != "url_metadata_only"
        or not isinstance(meta, dict)
        or set(meta) != _URL_METADATA_WINDOW_KEYS
        or meta.get("title") != ""
        or not isinstance(trigger, dict)
        or set(trigger) != _URL_METADATA_TRIGGER_KEYS
        or trigger.get("window_title") != ""
    ):
        return False

    # Reuse the exact-window parser so retained geometry and identifiers obey
    # the same finite/type/size bounds as the capture boundary.
    from ..capture.s1_parser import is_browser_bundle
    from ..capture.window_meta import parse_window_meta

    parsed_meta = parse_window_meta({"schema_version": 1, **meta})
    if parsed_meta is None or not is_browser_bundle(parsed_meta.bundle_id):
        return False
    if (
        trigger.get("app_name") != parsed_meta.app_name
        or trigger.get("bundle_id") != parsed_meta.bundle_id
        or trigger.get("pid") != parsed_meta.pid
        or trigger.get("window_id") != parsed_meta.window_id
        or trigger.get("event_type") not in _URL_METADATA_EVENT_TYPES
    ):
        return False

    url = observation.get("url")
    return isinstance(url, str) and url.casefold().startswith(("http://", "https://"))


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
        excluded_bundles = _normalized(cfg.excluded_bundle_ids, field="excluded_bundle_ids")
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
