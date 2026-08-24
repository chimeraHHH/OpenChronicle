from __future__ import annotations

import pytest

from openchronicle.capture import s1_parser


def _ax_tree(*apps: dict) -> dict:
    return {"apps": list(apps), "timestamp": "2026-04-21T10:00:00+08:00"}


def test_enrich_noop_without_ax_tree() -> None:
    capture = {"timestamp": "x", "window_meta": {"app_name": "A"}}
    s1_parser.enrich(capture)
    assert "focused_element" not in capture
    assert "visible_text" not in capture


def test_enrich_picks_frontmost_app() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {"name": "Background", "bundle_id": "b", "is_frontmost": False, "windows": []},
            {
                "name": "Cursor",
                "bundle_id": "com.todesktop.230313mzl4w4u92",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "s1_parser.py",
                        "focused": True,
                        "elements": [
                            {
                                "role": "AXTextArea",
                                "title": "editor",
                                "value": "def enrich(capture):\n    ...",
                            }
                        ],
                    }
                ],
            },
        )
    }
    s1_parser.enrich(capture)
    assert capture["focused_element"]["role"] == "AXTextArea"
    assert capture["focused_element"]["is_editable"] is True
    assert capture["focused_element"]["has_value"] is True
    assert capture["focused_element"]["value_length"] > 0
    assert "s1_parser.py" in capture["visible_text"]
    assert capture["url"] is None


def test_enrich_extracts_browser_url() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "Chrome",
                "bundle_id": "com.google.Chrome",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "Anthropic",
                        "focused": True,
                        "elements": [
                            {
                                "role": "AXTextField",
                                "title": "Address and search bar",
                                "value": "https://www.anthropic.com/news",
                            }
                        ],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    assert capture["url"] == "https://www.anthropic.com/news"
    assert capture["focused_element"]["role"] == "AXTextField"


def test_enrich_extracts_nested_browser_url_case_insensitively() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "Chrome",
                "bundle_id": "COM.GOOGLE.CHROME",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "Nested address field",
                        "focused": True,
                        "elements": [
                            {
                                "role": "AXGroup",
                                "children": [
                                    {
                                        "role": "AXTextField",
                                        "title": "Address and search bar",
                                        "value": "https://private.example/account",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    }

    s1_parser.enrich(capture)

    assert capture["url"] == "https://private.example/account"
    assert s1_parser.is_browser_bundle("com.google.Chrome") is True


def test_enrich_does_not_present_scheme_less_address_as_https() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "Safari",
                "bundle_id": "com.apple.Safari",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "",
                        "focused": True,
                        "elements": [
                            {
                                "role": "AXTextField",
                                "identifier": "smart-search-field",
                                "value": "anthropic.com",
                            }
                        ],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    assert capture["url"] is None


def test_enrich_non_browser_has_no_url() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "Cursor",
                "bundle_id": "com.todesktop.230313mzl4w4u92",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "file.py",
                        "focused": True,
                        "elements": [
                            {
                                "role": "AXTextField",
                                "value": "https://example.com",
                            }
                        ],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    assert capture["url"] is None


def test_enrich_visible_text_truncation() -> None:
    huge_value = "x" * 20_000
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "App",
                "bundle_id": "b",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "T",
                        "focused": True,
                        "elements": [
                            {"role": "AXStaticText", "title": "header", "value": huge_value}
                        ],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    assert len(capture["visible_text"]) <= 10_000 + len("\n...(truncated)")
    assert capture["visible_text"].endswith("(truncated)")


def test_enrich_no_focused_window_returns_empty_element() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "App",
                "bundle_id": "b",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "unfocused",
                        "focused": False,
                        "elements": [{"role": "AXTextField", "value": "something"}],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    fe = capture["focused_element"]
    assert fe["role"] == ""
    assert fe["value"] == ""
    assert fe["is_editable"] is False


def test_enrich_empty_ax_tree() -> None:
    capture = {"ax_tree": {"apps": []}}
    s1_parser.enrich(capture)
    assert capture["focused_element"]["role"] == ""
    assert capture["visible_text"] == ""
    assert capture["url"] is None


def test_enrich_falls_back_to_first_app_when_no_frontmost() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "OnlyApp",
                "bundle_id": "b",
                "windows": [
                    {
                        "title": "T",
                        "focused": True,
                        "elements": [{"role": "AXStaticText", "value": "hello"}],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    assert "hello" in capture["visible_text"]


def _browser_capture(
    *elements: dict,
    bundle_id: str = "com.google.Chrome",
    app_name: str = "Chrome",
) -> dict:
    return {
        "ax_tree": _ax_tree(
            {
                "name": app_name,
                "bundle_id": bundle_id,
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "Browser",
                        "focused": True,
                        "elements": list(elements),
                    }
                ],
            }
        )
    }


def test_browser_address_uses_complete_chrome_control_value() -> None:
    capture = _browser_capture(
        {
            "role": "AXGroup",
            "children": [
                {
                    "role": "AXTextField",
                    "identifier": "address-and-search-bar",
                    "value": "https://allowed.example/path?item=1",
                }
            ],
        }
    )

    scan = s1_parser.browser_address_candidates(capture)
    s1_parser.enrich(capture)

    assert scan.complete is True
    assert scan.values == ("https://allowed.example/path?item=1",)
    assert scan.provenance == ("explicit_http",)
    assert scan.sources == ("windows[0].elements[0].children[0]:value",)
    assert scan.issues == ()
    assert capture["url"] == "https://allowed.example/path?item=1"


def test_browser_internal_scheme_is_not_replaced_by_allowed_body_decoy() -> None:
    capture = _browser_capture(
        {
            "role": "AXTextField",
            "identifier": "address-and-search-bar",
            "value": "chrome://settings/passwords",
        },
        {
            "role": "AXWebArea",
            "children": [
                {
                    "role": "AXStaticText",
                    "value": "Visit https://allowed.example/public",
                }
            ],
        },
    )

    address_scan = s1_parser.browser_address_candidates(capture)
    full_scan = s1_parser.url_candidates(capture)
    s1_parser.enrich(capture)

    assert address_scan.complete is False
    assert address_scan.values == ()
    assert "unsupported_address_scheme" in address_scan.issues
    assert "https://allowed.example/public" in full_scan.values
    assert full_scan.complete is False
    assert "unsupported_uri_scheme" in full_scan.issues
    assert capture["url"] is None


def test_scheme_less_intranet_address_keeps_provenance_despite_body_decoy() -> None:
    capture = _browser_capture(
        {
            "role": "AXComboBox",
            "identifier": "omnibox",
            "value": "intranet",
        },
        {
            "role": "AXWebArea",
            "children": [
                {
                    "role": "AXStaticText",
                    "value": "https://allowed.example/decoy",
                }
            ],
        },
    )

    address_scan = s1_parser.browser_address_candidates(capture)
    s1_parser.enrich(capture)

    assert address_scan.complete is True
    assert address_scan.values == ("https://intranet",)
    assert address_scan.provenance == ("scheme_less",)
    assert address_scan.has_scheme_less is True
    assert capture["url"] is None


def test_editable_field_inside_web_area_is_not_browser_address_evidence() -> None:
    capture = _browser_capture(
        {
            "role": "AXWebArea",
            "children": [
                {
                    "role": "AXTextField",
                    "identifier": "address-and-search-bar",
                    "value": "https://allowed.example/login",
                }
            ],
        }
    )

    address_scan = s1_parser.browser_address_candidates(capture)
    full_scan = s1_parser.url_candidates(capture)
    s1_parser.enrich(capture)

    assert address_scan.complete is False
    assert address_scan.values == ()
    assert address_scan.issues == ("missing_address_control",)
    assert full_scan.values == ("https://allowed.example/login",)
    assert capture["url"] is None


def test_missing_web_area_role_cannot_expose_page_smart_search_field() -> None:
    capture = _browser_capture(
        {
            # A malformed/stripped AXWebArea boundary must be opaque. Its page
            # child deliberately borrows Safari's exact compatibility label.
            "children": [
                {
                    "role": "AXTextField",
                    "title": "Smart Search Field",
                    "value": "https://page-decoy.example",
                }
            ]
        },
        bundle_id="com.apple.Safari",
        app_name="Safari",
    )

    scan = s1_parser.browser_address_candidates(capture)

    assert scan.complete is False
    assert scan.values == ()
    assert "incomplete_address_scan" in scan.issues
    assert "missing_address_control" in scan.issues


def test_full_tree_url_scan_marks_scheme_less_candidate_provenance() -> None:
    capture = _browser_capture(
        {
            "role": "AXTextField",
            "identifier": "address-and-search-bar",
            "value": "docs.example/path",
        }
    )

    scan = s1_parser.url_candidates(capture)

    assert scan.values == ("https://docs.example/path",)
    assert scan.provenance == ("scheme_less",)
    assert scan.has_scheme_less is True
    assert scan.sources == ("windows[0].elements[0]:value",)


@pytest.mark.parametrize(
    ("bundle_id", "identity_field", "identity_value"),
    [
        ("com.google.Chrome", "identifier", "omnibox"),
        ("com.apple.Safari", "domIdentifier", "smart-search-field"),
        ("org.mozilla.firefox", "identifier", "urlbar-input"),
        ("company.thebrowser.Browser", "identifier", "command-bar-input"),
    ],
)
def test_browser_family_adapter_accepts_only_its_stable_address_identity(
    bundle_id: str,
    identity_field: str,
    identity_value: str,
) -> None:
    capture = _browser_capture(
        {
            "role": "AXTextField",
            identity_field: identity_value,
            "value": "https://trusted.example/path",
        },
        bundle_id=bundle_id,
    )

    scan = s1_parser.browser_address_candidates(capture)

    assert scan.complete is True
    assert scan.values == ("https://trusted.example/path",)
    assert scan.sources == ("windows[0].elements[0]:value",)


def test_unknown_identifier_cannot_borrow_known_address_label() -> None:
    capture = _browser_capture(
        {
            "role": "AXTextField",
            "identifier": "extension-controlled-input",
            "title": "Address and search bar",
            "value": "https://decoy.example",
        }
    )

    scan = s1_parser.browser_address_candidates(capture)

    assert scan.complete is False
    assert scan.values == ()
    assert "unrecognized_address_control" in scan.issues
    assert "missing_address_control" in scan.issues


@pytest.mark.parametrize(
    ("identifier", "dom_identifier"),
    [
        ("omnibox", "extension-controlled-input"),
        ("extension-controlled-input", "omnibox"),
        ("omnibox", 42),
    ],
)
def test_one_matching_identifier_cannot_override_unknown_peer(
    identifier: object,
    dom_identifier: object,
) -> None:
    capture = _browser_capture(
        {
            "role": "AXTextField",
            "identifier": identifier,
            "domIdentifier": dom_identifier,
            "value": "https://decoy.example",
        }
    )

    scan = s1_parser.browser_address_candidates(capture)

    assert scan.complete is False
    assert scan.values == ()
    assert "unrecognized_address_control" in scan.issues
    assert "missing_address_control" in scan.issues


def test_extension_toolbar_url_decoy_is_ignored_when_real_address_exists() -> None:
    capture = _browser_capture(
        {
            "role": "AXTextField",
            "identifier": "address-and-search-bar",
            "value": "https://trusted.example",
        },
        {
            "role": "AXTextField",
            "identifier": "extension-search-input",
            "title": "Extension search",
            "value": "https://decoy.example",
        },
    )

    scan = s1_parser.browser_address_candidates(capture)

    assert scan.complete is True
    assert scan.values == ("https://trusted.example",)
    assert scan.sources == ("windows[0].elements[0]:value",)


def test_two_address_controls_with_same_value_keep_distinct_source_evidence() -> None:
    capture = _browser_capture(
        {
            "role": "AXTextField",
            "identifier": "omnibox",
            "value": "https://same.example",
        },
        {
            "role": "AXComboBox",
            "domIdentifier": "urlbar",
            "value": "https://same.example",
        },
    )

    scan = s1_parser.browser_address_candidates(capture)

    assert scan.complete is True
    assert scan.values == ("https://same.example", "https://same.example")
    assert scan.sources == (
        "windows[0].elements[0]:value",
        "windows[0].elements[1]:value",
    )


def test_full_scan_same_value_uses_conservative_scheme_less_provenance() -> None:
    capture = _browser_capture(
        {"role": "AXStaticText", "value": "https://mixed.example/path"},
        {"role": "AXStaticText", "value": "mixed.example/path"},
    )

    scan = s1_parser.url_candidates(capture)

    assert scan.values == (
        "https://mixed.example/path",
        "https://mixed.example/path",
    )
    assert scan.provenance == ("scheme_less", "scheme_less")
    assert scan.sources == (
        "windows[0].elements[0]:value",
        "windows[0].elements[1]:value",
    )


@pytest.mark.parametrize("uri", ["file:///tmp/private", "about:blank", "mailto:user@example.com"])
def test_full_scan_non_http_uri_scheme_is_incomplete(uri: str) -> None:
    capture = _browser_capture(
        {"role": "AXStaticText", "value": uri},
        {"role": "AXStaticText", "value": "https://allowed.example"},
    )

    scan = s1_parser.url_candidates(capture)

    assert scan.complete is False
    assert "unsupported_uri_scheme" in scan.issues


@pytest.mark.parametrize(
    "prose",
    [
        "Note: remember to review the timeline",
        "GitHub: review pull request 42",
    ],
)
def test_full_scan_prose_label_is_not_treated_as_uri_scheme(prose: str) -> None:
    capture = _browser_capture(
        {"role": "AXStaticText", "value": prose},
        {"role": "AXStaticText", "value": "https://allowed.example"},
    )

    scan = s1_parser.url_candidates(capture)

    assert scan.complete is True
    assert "unsupported_uri_scheme" not in scan.issues


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "Open (https://forbidden.example/private).",
            "https://forbidden.example/private",
        ),
        (
            'Open "https://forbidden.example/private",',
            "https://forbidden.example/private",
        ),
        (
            "See https://example.test/wiki/Foo_(bar).",
            "https://example.test/wiki/Foo_(bar)",
        ),
        (
            "Encoded https://example.test/wiki/Foo_%29.",
            "https://example.test/wiki/Foo_%29",
        ),
        (
            "Nested https://example.test/wiki/Foo_(bar)).",
            "https://example.test/wiki/Foo_(bar)",
        ),
    ],
)
def test_explicit_url_candidate_trims_only_prose_tail(
    body: str,
    expected: str,
) -> None:
    capture = _browser_capture({"role": "AXStaticText", "value": body})

    scan = s1_parser.url_candidates(capture)

    assert scan.values == (expected,)


@pytest.mark.parametrize("separator", [",", ";"])
def test_explicit_url_candidates_split_security_delimiters(separator: str) -> None:
    capture = _browser_capture(
        {
            "role": "AXStaticText",
            "value": (f"https://allowed.example/x{separator}https://forbidden.example/private"),
        }
    )

    scan = s1_parser.url_candidates(capture)

    assert scan.complete is True
    assert scan.values == (
        "https://allowed.example/x",
        "https://forbidden.example/private",
    )


def test_adjacent_urls_without_delimiter_make_scan_incomplete() -> None:
    capture = _browser_capture(
        {
            "role": "AXStaticText",
            "value": ("https://allowed.example/xhttps://forbidden.example/private"),
        }
    )

    scan = s1_parser.url_candidates(capture)

    assert scan.complete is False


def test_policy_mode_disables_label_only_address_fallback() -> None:
    capture = _browser_capture(
        {
            "role": "AXTextField",
            "title": "Address and search bar",
            "value": "https://allowed.example/page",
        }
    )

    display_scan = s1_parser.browser_address_candidates(capture)
    policy_scan = s1_parser.browser_address_candidates(capture, require_stable_id=True)

    assert display_scan.complete is True
    assert display_scan.values == ("https://allowed.example/page",)
    assert policy_scan.complete is False
    assert policy_scan.values == ()
    assert "unrecognized_address_control" in policy_scan.issues


@pytest.mark.parametrize(
    "embedded_uri",
    [
        "target=chrome://settings/passwords",
        "url(chrome://settings/passwords)",
        "See(chrome://settings/passwords)",
    ],
)
def test_full_scan_detects_unsupported_scheme_at_any_string_position(
    embedded_uri: str,
) -> None:
    capture = _browser_capture(
        {"role": "AXStaticText", "value": embedded_uri},
        {"role": "AXStaticText", "value": "https://allowed.example"},
    )

    scan = s1_parser.url_candidates(capture)

    assert scan.complete is False
    assert "unsupported_uri_scheme" in scan.issues
