from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from openchronicle.capture import ax_capture


def _provider() -> ax_capture.MacAXHelperProvider:
    return ax_capture.MacAXHelperProvider(
        helper_path=Path("/private/tmp/mac-ax-helper"),
        depth=8,
        timeout=3,
    )


def test_failed_helper_stderr_is_not_a_log_sink(monkeypatch, caplog) -> None:
    secret = "EXCLUDED-AX-STDERR-MARKER"
    monkeypatch.setattr(
        ax_capture,
        "_run_bounded_process",
        lambda *_args, **_kwargs: ax_capture._BoundedProcessResult(
            returncode=1,
            stdout=b"",
        ),
    )

    with caplog.at_level(logging.DEBUG):
        result = _provider().capture_frontmost(focused_window_only=True)

    assert result is None
    assert secret not in caplog.text
    assert "status 1" in caplog.text


def test_nonfinite_or_nonobject_helper_json_fails_closed(monkeypatch, caplog) -> None:
    responses = iter(
        [
            ax_capture._BoundedProcessResult(
                returncode=0, stdout=b'{"timestamp":NaN,"apps":[]}'
            ),
            ax_capture._BoundedProcessResult(returncode=0, stdout=b"[]"),
        ]
    )
    monkeypatch.setattr(
        ax_capture,
        "_run_bounded_process",
        lambda *_args, **_kwargs: next(responses),
    )

    with caplog.at_level(logging.WARNING):
        first = _provider().capture_frontmost(focused_window_only=True)
        second = _provider().capture_frontmost(focused_window_only=True)

    assert first is None
    assert second is None
    assert "invalid JSON" in caplog.text
    assert "invalid payload" in caplog.text


def test_unavailable_provider_never_synthesizes_ax_content() -> None:
    provider = ax_capture.UnavailableAXProvider("permission denied")

    assert provider.available is False
    assert provider.capture_frontmost(focused_window_only=True) is None
    assert provider.capture_all_visible() is None
    assert provider.capture_app("Example", focused_window_only=True) is None


def test_complete_tree_requirement_is_forwarded_to_native_helper(monkeypatch) -> None:
    observed: list[list[str]] = []

    def run(args, **_kwargs):
        observed.append(list(args))
        return ax_capture._BoundedProcessResult(returncode=1, stdout=b"")

    monkeypatch.setattr(ax_capture, "_run_bounded_process", run)

    assert (
        _provider().capture_frontmost(
            focused_window_only=True,
            require_complete_tree=True,
        )
        is None
    )
    assert "--require-complete-tree" in observed[0]


def _native_tree_payload(**receipt_overrides) -> bytes:
    payload = {
        "timestamp": "2026-08-08T00:00:00Z",
        "apps": [],
        "ax_capture_schema_version": 1,
        "tree_complete": True,
        "focused_window_only": True,
        "effective_max_depth": 8,
        "resource_limits_version": 1,
    }
    payload.update(receipt_overrides)
    return json.dumps(payload).encode()


def test_strict_tree_request_rejects_legacy_helper_without_receipt(monkeypatch) -> None:
    monkeypatch.setattr(
        ax_capture,
        "_run_bounded_process",
        lambda *_args, **_kwargs: ax_capture._BoundedProcessResult(
            returncode=0,
            stdout=b'{"timestamp":"2026-08-08T00:00:00Z","apps":[]}',
        ),
    )

    assert (
        _provider().capture_frontmost(
            focused_window_only=True,
            require_complete_tree=True,
        )
        is None
    )


def test_valid_native_tree_receipt_is_verified_then_stripped(monkeypatch) -> None:
    monkeypatch.setattr(
        ax_capture,
        "_run_bounded_process",
        lambda *_args, **_kwargs: ax_capture._BoundedProcessResult(
            returncode=0,
            stdout=_native_tree_payload(),
        ),
    )

    result = _provider().capture_frontmost(
        focused_window_only=True,
        require_complete_tree=True,
    )

    assert result is not None
    assert result.tree_complete_verified is True
    assert result.effective_max_depth == 8
    assert not ax_capture._AX_RECEIPT_KEYS.intersection(result.raw_json)


@pytest.mark.parametrize(
    "override",
    [
        {"ax_capture_schema_version": 2},
        {"ax_capture_schema_version": True},
        {"tree_complete": False},
        {"focused_window_only": False},
        {"effective_max_depth": 7},
        {"resource_limits_version": 2},
        {"resource_limits_version": True},
    ],
)
def test_mismatched_native_tree_receipt_fails_closed(monkeypatch, override) -> None:
    monkeypatch.setattr(
        ax_capture,
        "_run_bounded_process",
        lambda *_args, **_kwargs: ax_capture._BoundedProcessResult(
            returncode=0,
            stdout=_native_tree_payload(**override),
        ),
    )

    assert (
        _provider().capture_frontmost(
            focused_window_only=True,
            require_complete_tree=True,
        )
        is None
    )


def test_native_depth_limit_exit_fails_closed_without_exposing_stderr(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(
        ax_capture,
        "_run_bounded_process",
        lambda *_args, **_kwargs: ax_capture._BoundedProcessResult(
            returncode=4,
            stdout=b'{"tree_complete":false}',
        ),
    )

    with caplog.at_level(logging.DEBUG):
        result = _provider().capture_frontmost(
            focused_window_only=True,
            require_complete_tree=True,
        )

    assert result is None
    assert "status 4" in caplog.text
    assert "tree_complete" not in caplog.text
