"""Enrich capture JSON with structured S1 fields.

Downstream stages (timeline aggregator, session reducer, classifier) read
``focused_element`` / ``visible_text`` / ``url`` instead of re-parsing the
raw AX tree every time. Cutting the prompt size and giving the LLM a
consistent schema is the point.

Ported from Einsia-Partner's S1 extraction (``s1_collector`` —
``_extract_focused_element`` / ``_render_visible_text`` / ``_extract_url``).
Runs inline inside ``capture_once`` so every capture-buffer JSON carries
these fields.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from .ax_models import ax_app_to_markdown

_BROWSER_BUNDLES = {
    "com.google.chrome",
    "com.apple.safari",
    "org.mozilla.firefox",
    "com.microsoft.edgemac",
    "company.thebrowser.browser",
    "com.brave.browser",
    "com.operasoftware.opera",
}

_URL_RE = re.compile(r"https?://[^\s,;]+", re.IGNORECASE)
_URL_START_RE = re.compile(r"https?://", re.IGNORECASE)
_URI_SCHEME_ANYWHERE_RE = re.compile(
    r"(?<![a-z0-9+.-])(?P<scheme>[a-z][a-z0-9+.-]*):(?P<slashes>//)?",
    re.IGNORECASE,
)
_SINGLE_LABEL_ADDRESS_RE = re.compile(
    r"^[a-z0-9-]+(?:(?::\d+)|(?:[/?#].*))$",
    re.IGNORECASE,
)
_BRACKETED_ADDRESS_RE = re.compile(
    r"^\[[0-9a-f:.]+\](?::\d+)?(?:[/?#].*)?$",
    re.IGNORECASE,
)
_SCHEME_PREFIX_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_HOST_PORT_PREFIX_RE = re.compile(r"^[^/?#\s]+:\d+(?:[/?#].*)?$", re.IGNORECASE)
_BARE_TOKEN_EDGE = "\"'()<>{},;!—–"
_URL_TRAILING_SENTENCE_PUNCTUATION = ".,;:!?"
_URL_TRAILING_QUOTES = "\"'\u2018\u2019\u201c\u201d"
_UNSUPPORTED_URI_SCHEMES = frozenset(
    {
        "about",
        "blob",
        "brave",
        "chrome",
        "chrome-extension",
        "data",
        "edge",
        "file",
        "ftp",
        "javascript",
        "mailto",
        "moz-extension",
        "opera",
        "safari-extension",
        "view-source",
        "ws",
        "wss",
    }
)

_EDITABLE_ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}
_STATIC_ROLES = {"AXStaticText", "AXWebArea"}
_BROWSER_ADDRESS_ROLES = {"AXTextField", "AXComboBox", "AXSearchField"}

_BROWSER_ADDRESS_FAMILY = {
    "com.google.chrome": "chromium",
    "com.microsoft.edgemac": "chromium",
    "com.brave.browser": "chromium",
    "com.operasoftware.opera": "chromium",
    "com.apple.safari": "safari",
    "org.mozilla.firefox": "firefox",
    "company.thebrowser.browser": "arc",
}
_ADDRESS_IDENTIFIERS = {
    "chromium": frozenset(
        {
            "address-and-search-bar",
            "address_and_search_bar",
            "address and search bar",
            "location-bar",
            "location_bar",
            "omnibox",
            "urlbar",
        }
    ),
    "safari": frozenset(
        {
            "address-and-search-field",
            "address_and_search_field",
            "address-field",
            "smart-search-field",
            "smart_search_field",
        }
    ),
    "firefox": frozenset({"urlbar", "urlbar-input"}),
    "arc": frozenset(
        {
            "address-bar",
            "address_bar",
            "command-bar-input",
            "command_bar_input",
            "location-bar",
        }
    ),
}
_ADDRESS_LABELS = {
    "chromium": frozenset({"address and search bar", "search or type web address"}),
    "safari": frozenset(
        {"smart search field", "address and search", "search or enter website name"}
    ),
    "firefox": frozenset({"search with google or enter address", "search or enter address"}),
    "arc": frozenset({"search or enter url", "command bar"}),
}

_VISIBLE_TEXT_MAX = 10_000
_FOCUS_TITLE_MAX = 200
_FOCUS_VALUE_MAX = 2_000
_MAX_URL_SCAN_NODES = 4_096
_MAX_URL_CANDIDATES = 128
_MAX_URL_CANDIDATE_LENGTH = 4_096
_MAX_URL_TEXT_SCAN_LENGTH = 65_536


@dataclass
class FocusedElement:
    role: str = ""
    title: str = ""
    value: str = ""
    is_editable: bool = False
    has_value: bool = False
    value_length: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        stripped = (self.value or "").strip()
        d["has_value"] = bool(stripped)
        d["value_length"] = len(stripped)
        return d


@dataclass(frozen=True)
class URLCandidateScan:
    values: tuple[str, ...] = ()
    complete: bool = True
    # Aligned with ``values``. Older consumers can keep reading ``values``;
    # policy code can distinguish an observed explicit scheme from the
    # compatibility normalization applied to a scheme-less address.
    provenance: tuple[str, ...] = ()
    # Content-free evidence for fail-closed browser-address handling.
    issues: tuple[str, ...] = ()
    # Aligned with ``values``. Sources are stable AX child paths ending in the
    # exact metadata field (for browser addresses, always ``:value``).
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.provenance and self.values:
            object.__setattr__(self, "provenance", ("explicit_http",) * len(self.values))
        if len(self.provenance) != len(self.values):
            raise ValueError("URL candidate provenance must align with values")
        if not self.sources and self.values:
            object.__setattr__(self, "sources", ("unknown:value",) * len(self.values))
        if len(self.sources) != len(self.values):
            raise ValueError("URL candidate sources must align with values")

    @property
    def has_scheme_less(self) -> bool:
        return "scheme_less" in self.provenance


def enrich(capture: dict[str, Any]) -> None:
    """Mutate ``capture`` in place: add ``focused_element`` / ``visible_text`` / ``url``.

    No-op when there is no ``ax_tree`` (e.g. AX unavailable, permission denied).
    """
    ax_tree = capture.get("ax_tree")
    if not isinstance(ax_tree, dict):
        return

    app_data = _frontmost_app(ax_tree)
    if app_data is None:
        capture["focused_element"] = FocusedElement().to_dict()
        capture["visible_text"] = ""
        capture["url"] = None
        return

    capture["focused_element"] = _extract_focused_element(app_data).to_dict()
    capture["visible_text"] = _render_visible_text(app_data)
    capture["url"] = _extract_url(app_data)


def is_browser_bundle(bundle_id: object) -> bool:
    """Return whether URL extraction is supported for this exact app bundle."""
    return isinstance(bundle_id, str) and bundle_id.strip().casefold() in _BROWSER_BUNDLES


def url_candidates(capture: dict[str, Any]) -> URLCandidateScan:
    """Return every bounded URL-like AX field plus scan completeness.

    Privacy policy must evaluate the whole set, not merely the first address-
    looking field. A malformed tree, oversized field, candidate flood, or node
    budget exhaustion marks the scan incomplete so an enabled URL policy can
    fail closed instead of persisting an unchecked value in ``ax_tree`` or
    ``visible_text``.
    """
    ax_tree = capture.get("ax_tree")
    if not isinstance(ax_tree, dict):
        return URLCandidateScan(complete=False)
    app_data = _frontmost_app(ax_tree)
    if not isinstance(app_data, dict):
        return URLCandidateScan(complete=False)
    return _extract_url_candidates(app_data)


def browser_url_candidates(capture: dict[str, Any]) -> URLCandidateScan:
    """Backward-compatible alias for the now app-agnostic privacy scan."""
    return url_candidates(capture)


def browser_address_candidates(
    capture: dict[str, Any], *, require_stable_id: bool = False
) -> URLCandidateScan:
    """Return trusted address-bar evidence for a supported frontmost browser.

    Only complete values of editable browser-chrome controls are considered.
    Descendants of ``AXWebArea`` are page content and are deliberately ignored,
    even when a page contains a text field or a convincing URL decoy.

    ``issues`` is content-free and ``complete`` is false for a missing, empty,
    malformed, or non-http(s) address. Scheme-less values remain available for
    compatibility but carry ``scheme_less`` provenance so policy can reject
    them without pretending that AX supplied an HTTPS scheme. Set
    ``require_stable_id`` at a privacy boundary; the exact-label fallback is
    intentionally reserved for non-policy S1 display compatibility.
    """
    ax_tree = capture.get("ax_tree")
    if not isinstance(ax_tree, dict):
        return URLCandidateScan(complete=False, issues=("missing_ax_tree",))
    app_data = _frontmost_app(ax_tree)
    if not isinstance(app_data, dict):
        return URLCandidateScan(complete=False, issues=("missing_app",))
    if not is_browser_bundle(app_data.get("bundle_id")):
        return URLCandidateScan(issues=("unsupported_browser",))
    return _extract_browser_address_candidates(app_data, require_stable_id=require_stable_id)


def _frontmost_app(ax_tree: dict[str, Any]) -> dict[str, Any] | None:
    apps = ax_tree.get("apps") or []
    for app in apps:
        if app.get("is_frontmost"):
            return app
    return apps[0] if apps else None


def _extract_focused_element(app_data: dict[str, Any]) -> FocusedElement:
    for window in app_data.get("windows", []):
        if not window.get("focused"):
            continue
        for el in window.get("elements", []):
            role = el.get("role", "") or ""
            if role in _EDITABLE_ROLES:
                return FocusedElement(
                    role=role,
                    title=(el.get("title") or "")[:_FOCUS_TITLE_MAX],
                    value=(el.get("value") or "")[:_FOCUS_VALUE_MAX],
                    is_editable=True,
                )
            if role in _STATIC_ROLES:
                return FocusedElement(
                    role=role,
                    title=(el.get("title") or "")[:_FOCUS_TITLE_MAX],
                    value=(el.get("value") or el.get("title") or "")[:_FOCUS_VALUE_MAX],
                    is_editable=False,
                )
    return FocusedElement()


def _render_visible_text(app_data: dict[str, Any]) -> str:
    md = ax_app_to_markdown(app_data)
    if len(md) > _VISIBLE_TEXT_MAX:
        md = md[:_VISIBLE_TEXT_MAX] + "\n...(truncated)"
    return md


def _extract_url(app_data: dict[str, Any]) -> str | None:
    if not is_browser_bundle(app_data.get("bundle_id")):
        return None
    scan = _extract_browser_address_candidates(app_data)
    if not scan.complete or scan.has_scheme_less or len(scan.values) != 1:
        return None
    return scan.values[0]


def _extract_browser_address_candidates(
    app_data: dict[str, Any], *, require_stable_id: bool = False
) -> URLCandidateScan:
    windows = app_data.get("windows")
    if not isinstance(windows, list):
        return URLCandidateScan(complete=False, issues=("malformed_windows",))
    bundle_id = str(app_data.get("bundle_id") or "").strip().casefold()
    family = _BROWSER_ADDRESS_FAMILY.get(bundle_id)
    if family is None:
        return URLCandidateScan(complete=False, issues=("unsupported_browser",))

    focused_windows = [
        (index, window)
        for index, window in enumerate(windows)
        if isinstance(window, dict) and window.get("focused")
    ]
    selected_windows = focused_windows or [
        (index, window) for index, window in enumerate(windows) if isinstance(window, dict)
    ]
    complete = bool(focused_windows)
    issues: list[str] = [] if focused_windows else ["missing_focused_window"]
    if any(not isinstance(window, dict) for window in windows):
        complete = False
        issues.append("malformed_window")
    values: list[str] = []
    provenance: list[str] = []
    sources: list[str] = []
    remaining = _MAX_URL_SCAN_NODES
    controls_seen = 0

    for window_index, window in selected_windows:
        walked, remaining, subtree_complete = _walk_browser_chrome_elements(
            window.get("elements", []),
            remaining,
            prefix=f"windows[{window_index}].elements",
        )
        complete = complete and subtree_complete
        if not subtree_complete:
            issues.append("incomplete_address_scan")
        for element, path in walked:
            if element.get("role") not in _BROWSER_ADDRESS_ROLES:
                continue
            matched, contradicted = _matches_address_adapter(
                element, family, require_stable_id=require_stable_id
            )
            if not matched:
                if contradicted:
                    issues.append("unrecognized_address_control")
                    complete = False
                continue
            controls_seen += 1
            value = element.get("value")
            candidate, candidate_provenance, issue = _complete_address_value(value)
            if issue is not None:
                issues.append(issue)
                complete = False
                continue
            if candidate is None or candidate_provenance is None:
                issues.append("unrecognized_address")
                complete = False
                continue
            if len(values) >= _MAX_URL_CANDIDATES:
                issues.append("address_candidate_limit")
                complete = False
                break
            values.append(candidate)
            provenance.append(candidate_provenance)
            sources.append(f"{path}:value")

    if not controls_seen:
        issues.append("missing_address_control")
        complete = False

    return URLCandidateScan(
        values=tuple(values),
        complete=complete,
        provenance=tuple(provenance),
        issues=tuple(dict.fromkeys(issues)),
        sources=tuple(sources),
    )


def _matches_address_adapter(
    element: dict[str, Any], family: str, *, require_stable_id: bool
) -> tuple[bool, bool]:
    """Return (match, contradiction) for a family-specific address signature.

    A present identifier is stronger than a human-readable label: an unknown
    identifier cannot borrow a familiar label to impersonate the address bar.
    Exact labels are only a compatibility fallback when AX exposes no stable
    identifier or DOM identifier.
    """
    raw_identifiers = (element.get("identifier"), element.get("domIdentifier"))
    present_identifiers = [value for value in raw_identifiers if value is not None]
    expected_identifiers = _ADDRESS_IDENTIFIERS[family]
    if present_identifiers:
        normalized_identifiers = [
            value.strip().casefold() if isinstance(value, str) else ""
            for value in present_identifiers
        ]
        matched_identifiers = [value in expected_identifiers for value in normalized_identifiers]
        if any(matched_identifiers):
            # One valid cue cannot override a conflicting or malformed peer.
            matched = all(matched_identifiers)
            return matched, not matched
        labels = {
            value.strip().casefold()
            for value in (element.get("title"), element.get("description"))
            if isinstance(value, str) and value.strip()
        }
        return False, bool(labels & _ADDRESS_LABELS[family])

    labels = {
        value.strip().casefold()
        for value in (element.get("title"), element.get("description"))
        if isinstance(value, str) and value.strip()
    }
    matched = bool(labels & _ADDRESS_LABELS[family])
    if require_stable_id and matched:
        return False, True
    return matched, False


def _complete_address_value(value: object) -> tuple[str | None, str | None, str | None]:
    """Classify one complete chrome-control value without substring matching."""
    if not isinstance(value, str):
        return None, None, "missing_address_value"
    address = value.strip()
    if not address:
        return None, None, "empty_address_value"
    if len(address) > _MAX_URL_CANDIDATE_LENGTH or any(char.isspace() for char in address):
        return None, None, "unrecognized_address"

    lower_address = address.casefold()
    if lower_address.startswith(("http://", "https://")):
        scheme_length = 8 if lower_address.startswith("https://") else 7
        if (
            _URL_START_RE.search(address, scheme_length) is not None
            or _has_unsupported_uri_scheme(address)
        ):
            return None, None, "ambiguous_address_value"
        parsed = urlsplit(address)
        if not parsed.hostname:
            return None, None, "malformed_http_address"
        return address, "explicit_http", None

    if "://" in address or (
        _SCHEME_PREFIX_RE.match(address) and not _HOST_PORT_PREFIX_RE.match(address)
    ):
        return None, None, "unsupported_address_scheme"

    # The browser may omit the scheme from its AX value. Preserve that fact:
    # normalizing is for the existing S1 display API, not evidence of HTTPS.
    if address.startswith(("/", "?", "#")) or "://" in address:
        return None, None, "unrecognized_address"
    if any(char in address for char in "<>\"'{}|\\^"):
        return None, None, "unrecognized_address"
    try:
        parsed = urlsplit(f"//{address}")
        if not parsed.hostname:
            return None, None, "unrecognized_address"
        # Accessing port performs urllib's numeric/range validation.
        _ = parsed.port
    except ValueError:
        return None, None, "unrecognized_address"
    return f"https://{address}", "scheme_less", None


def _extract_url_candidates(app_data: dict[str, Any]) -> URLCandidateScan:
    windows = app_data.get("windows")
    if not isinstance(windows, list):
        return URLCandidateScan(complete=False)

    candidates: list[str] = []
    candidate_sources: list[str] = []
    explicit_candidates: set[str] = set()
    scheme_less_candidates: set[str] = set()
    remaining_nodes = _MAX_URL_SCAN_NODES
    complete = True
    issues: list[str] = []
    for key, raw_value in app_data.items():
        if key == "windows":
            continue
        if _has_unsupported_uri_scheme(raw_value):
            complete = False
            issues.append("unsupported_uri_scheme")
        found, field_complete = _explicit_url_candidates(raw_value)
        explicit_candidates.update(found)
        if key == "name":
            bare, bare_complete = _bare_url_candidates(raw_value)
            scheme_less_candidates.update(bare)
            field_complete = field_complete and bare_complete
            found.extend(candidate for candidate in bare if candidate not in found)
        complete = complete and field_complete
        for candidate in found:
            if len(candidates) >= _MAX_URL_CANDIDATES:
                complete = False
                break
            candidates.append(candidate)
            candidate_sources.append(f"app:{key}")
    for window_index, window in enumerate(windows):
        if not isinstance(window, dict):
            complete = False
            continue
        for key, raw_value in window.items():
            if key == "elements":
                continue
            if _has_unsupported_uri_scheme(raw_value):
                complete = False
                issues.append("unsupported_uri_scheme")
            found, field_complete = _explicit_url_candidates(raw_value)
            explicit_candidates.update(found)
            bare, bare_complete = _bare_url_candidates(raw_value)
            scheme_less_candidates.update(bare)
            field_complete = field_complete and bare_complete
            found.extend(candidate for candidate in bare if candidate not in found)
            complete = complete and field_complete
            for candidate in found:
                if len(candidates) >= _MAX_URL_CANDIDATES:
                    complete = False
                    break
                candidates.append(candidate)
                candidate_sources.append(f"windows[{window_index}]:{key}")
        elements = window.get("elements", [])
        walked, remaining_nodes, subtree_complete = _walk_elements_with_paths_bounded(
            elements,
            remaining_nodes,
            prefix=f"windows[{window_index}].elements",
        )
        complete = complete and subtree_complete
        for element, path in walked:
            for key, raw_value in element.items():
                if key == "children":
                    continue
                if _has_unsupported_uri_scheme(raw_value):
                    complete = False
                    issues.append("unsupported_uri_scheme")
                found, field_complete = _explicit_url_candidates(raw_value)
                explicit_candidates.update(found)
                complete = complete and field_complete
                # Browser address widgets vary by role and attribute. Every
                # persisted AX string is therefore interpreted conservatively,
                # including title/description/identifier metadata.
                bare, bare_complete = _bare_url_candidates(raw_value)
                scheme_less_candidates.update(bare)
                complete = complete and bare_complete
                found.extend(candidate for candidate in bare if candidate not in found)
                for candidate in found:
                    if len(candidates) >= _MAX_URL_CANDIDATES:
                        complete = False
                        break
                    candidates.append(candidate)
                    candidate_sources.append(f"{path}:{key}")

    # Collapse only an exact repeat from the same AX field. The same value in
    # two controls remains two pieces of evidence so address ambiguity cannot
    # disappear through value-only deduplication.
    unique_entries = tuple(dict.fromkeys(zip(candidates, candidate_sources, strict=True)))
    unique_candidates = tuple(value for value, _source in unique_entries)
    unique_sources = tuple(source for _value, source in unique_entries)
    provenance = tuple(
        "scheme_less"
        if candidate in scheme_less_candidates
        else "explicit_http"
        if candidate in explicit_candidates
        else "explicit_http"
        for candidate in unique_candidates
    )
    return URLCandidateScan(
        values=unique_candidates,
        complete=complete,
        provenance=provenance,
        issues=tuple(dict.fromkeys(issues)),
        sources=unique_sources,
    )


def _explicit_url_candidates(value: object) -> tuple[list[str], bool]:
    """Find explicit http(s) strings in scalar or list-valued AX metadata."""
    if value is None or isinstance(value, (bool, int, float)):
        return [], True
    if isinstance(value, str):
        candidates: list[str] = []
        complete = len(value) <= _MAX_URL_TEXT_SCAN_LENGTH
        bounded_value = value[:_MAX_URL_TEXT_SCAN_LENGTH]
        for match in _URL_RE.finditer(bounded_value):
            raw_candidate = match.group(0)
            if len(raw_candidate) > _MAX_URL_CANDIDATE_LENGTH:
                complete = False
                continue
            scheme_length = 8 if raw_candidate[:8].casefold() == "https://" else 7
            if _URL_START_RE.search(raw_candidate, scheme_length) is not None:
                # Two adjacent URLs without a trusted delimiter cannot be
                # separated without guessing where the first host/path ends.
                complete = False
            candidate = _trim_url_candidate(raw_candidate)
            if candidate:
                candidates.append(candidate)
        return candidates, complete
    if isinstance(value, list):
        candidates: list[str] = []
        complete = len(value) <= _MAX_URL_SCAN_NODES
        for item in value[:_MAX_URL_SCAN_NODES]:
            found, item_complete = _explicit_url_candidates(item)
            candidates.extend(found)
            complete = complete and item_complete
        return candidates, complete
    return [], False


def _trim_url_candidate(candidate: str) -> str:
    """Remove prose delimiters without rewriting URL-internal syntax.

    Closing brackets are removed only when unmatched by an opening bracket in
    the candidate. Thus a balanced ``Foo_(bar)`` path remains intact, while
    the wrapper in ``(https://example.test).`` does not become URL evidence.
    Percent-encoded delimiters are ordinary characters here and stay exact.
    """
    end = len(candidate)
    bracket_balance = {
        closing: candidate.count(opening) - candidate.count(closing)
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">"))
    }
    trailing = _URL_TRAILING_SENTENCE_PUNCTUATION + _URL_TRAILING_QUOTES
    while end:
        while end and candidate[end - 1] in trailing:
            end -= 1
        if end and candidate[end - 1] in bracket_balance:
            closing = candidate[end - 1]
            if bracket_balance[closing] < 0:
                bracket_balance[closing] += 1
                end -= 1
                continue
        break
    return candidate[:end]


def _bare_url_candidates(value: object) -> tuple[list[str], bool]:
    """Extract conservative scheme-less address tokens from AX metadata."""
    if value is None or isinstance(value, (bool, int, float)):
        return [], True
    if isinstance(value, list):
        candidates: list[str] = []
        complete = len(value) <= _MAX_URL_SCAN_NODES
        for item in value[:_MAX_URL_SCAN_NODES]:
            found, item_complete = _bare_url_candidates(item)
            candidates.extend(found)
            complete = complete and item_complete
        return candidates, complete
    if not isinstance(value, str):
        return [], False

    complete = len(value) <= _MAX_URL_TEXT_SCAN_LENGTH
    bounded_value = value[:_MAX_URL_TEXT_SCAN_LENGTH]
    candidates: list[str] = []
    for raw_token in bounded_value.split():
        token = raw_token.strip(_BARE_TOKEN_EDGE)
        if not token or _URL_RE.search(token):
            continue
        lower = token.casefold()
        looks_like_address = (
            "." in token
            or lower == "localhost"
            or lower.startswith(("localhost:", "localhost/", "localhost?", "localhost#"))
            or _SINGLE_LABEL_ADDRESS_RE.fullmatch(token) is not None
            or _BRACKETED_ADDRESS_RE.fullmatch(token) is not None
        )
        if not looks_like_address:
            continue
        if len(token) + len("https://") > _MAX_URL_CANDIDATE_LENGTH:
            complete = False
            continue
        candidates.append(f"https://{token}")
    return candidates, complete


def _has_unsupported_uri_scheme(value: object) -> bool:
    """Conservatively flag persisted URI tokens that URL policy cannot verify."""
    if isinstance(value, list):
        return any(_has_unsupported_uri_scheme(item) for item in value)
    if not isinstance(value, str):
        return False
    for match in _URI_SCHEME_ANYWHERE_RE.finditer(value):
        scheme = match.group("scheme").casefold()
        slashes = match.group("slashes")
        if scheme in {"http", "https"}:
            if slashes != "//":
                return True
            continue
        if slashes == "//":
            return True
        if scheme in _UNSUPPORTED_URI_SCHEMES:
            suffix_index = match.end()
            if suffix_index < len(value) and not value[suffix_index].isspace():
                return True
            # A prose label such as "File: open..." is not a URI token.
            if suffix_index == len(value) and scheme in {"about", "data", "javascript"}:
                return True
    return False


def _walk_browser_chrome_elements(
    raw_elements: object, remaining: int, *, prefix: str
) -> tuple[list[tuple[dict[str, Any], str]], int, bool]:
    """Walk browser chrome while treating AXWebArea as an opaque boundary."""
    if not isinstance(raw_elements, list) or remaining < 0:
        return [], max(0, remaining), False
    initial_count = min(len(raw_elements), remaining)
    stack = [
        (value, f"{prefix}[{index}]")
        for index, value in reversed(list(enumerate(raw_elements[:initial_count])))
    ]
    elements: list[tuple[dict[str, Any], str]] = []
    complete = len(raw_elements) <= remaining
    while stack:
        if remaining <= 0:
            complete = False
            break
        value, path = stack.pop()
        remaining -= 1
        if not isinstance(value, dict):
            complete = False
            continue
        role = value.get("role")
        if not isinstance(role, str) or not role.strip():
            # Without a trustworthy role this may be a stripped AXWebArea.
            # Treat it as an opaque malformed boundary instead of descending.
            complete = False
            continue
        elements.append((value, path))
        if role == "AXWebArea":
            continue
        children = value.get("children", [])
        if not isinstance(children, list):
            complete = False
            continue
        available = max(0, remaining - len(stack))
        if len(children) > available:
            complete = False
        if available:
            stack.extend(
                (child, f"{path}.children[{index}]")
                for index, child in reversed(list(enumerate(children[:available])))
            )
    return elements, remaining, complete


def _walk_elements_with_paths_bounded(
    raw_elements: object, remaining: int, *, prefix: str
) -> tuple[list[tuple[dict[str, Any], str]], int, bool]:
    """Walk every AX element while preserving a deterministic child path."""
    if not isinstance(raw_elements, list) or remaining < 0:
        return [], max(0, remaining), False
    initial_count = min(len(raw_elements), remaining)
    stack = [
        (value, f"{prefix}[{index}]")
        for index, value in reversed(list(enumerate(raw_elements[:initial_count])))
    ]
    elements: list[tuple[dict[str, Any], str]] = []
    complete = len(raw_elements) <= remaining
    while stack:
        if remaining <= 0:
            complete = False
            break
        value, path = stack.pop()
        remaining -= 1
        if not isinstance(value, dict):
            complete = False
            continue
        elements.append((value, path))
        children = value.get("children", [])
        if not isinstance(children, list):
            complete = False
            continue
        available = max(0, remaining - len(stack))
        if len(children) > available:
            complete = False
        if available:
            stack.extend(
                (child, f"{path}.children[{index}]")
                for index, child in reversed(list(enumerate(children[:available])))
            )
    return elements, remaining, complete


def _walk_elements(raw_elements: object):
    """Yield a bounded AX subtree without trusting helper nesting depth.

    Browser address fields are frequently nested below toolbar groups.  The
    old top-level-only scan silently missed those URLs, which could weaken an
    exclusion rule.  A hard node budget keeps malformed helper output from
    turning URL policy into an unbounded traversal.
    """
    elements, _remaining, _complete = _walk_elements_bounded(raw_elements, _MAX_URL_SCAN_NODES)
    yield from elements


def _walk_elements_bounded(
    raw_elements: object, remaining: int
) -> tuple[list[dict[str, Any]], int, bool]:
    if not isinstance(raw_elements, list) or remaining < 0:
        return [], max(0, remaining), False
    initial_count = min(len(raw_elements), remaining)
    stack = list(reversed(raw_elements[:initial_count]))
    elements: list[dict[str, Any]] = []
    complete = len(raw_elements) <= remaining
    while stack:
        if remaining <= 0:
            complete = False
            break
        value = stack.pop()
        remaining -= 1
        if not isinstance(value, dict):
            complete = False
            continue
        elements.append(value)
        children = value.get("children", [])
        if not isinstance(children, list):
            complete = False
            continue
        available = max(0, remaining - len(stack))
        if len(children) > available:
            complete = False
        if available:
            stack.extend(reversed(children[:available]))
    return elements, remaining, complete
