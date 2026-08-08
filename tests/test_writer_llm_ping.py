"""Unit tests for ``writer.llm.ping_stage``.

The integration path (status command rendering ✓ / ✗) is covered in
``test_cli_status.py``. These tests pin down ping_stage's own contract:
mock-env shortcut, success latency, and sanitized error labels.
"""

from __future__ import annotations

import builtins

import pytest

from openchronicle.config import Config, ModelConfig
from openchronicle.writer import llm as llm_mod


def _cfg_with_model(model: str = "gpt-5.4-nano", api_key: str = "sk-test") -> Config:
    return Config(models={"default": ModelConfig(model=model, api_key=api_key)})


def test_ping_stage_mock_env_returns_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """OPENCHRONICLE_LLM_MOCK=1 short-circuits before any litellm import."""
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    cfg = _cfg_with_model()

    res = llm_mod.ping_stage(cfg, "timeline")

    assert res.ok is True
    assert res.mocked is True
    assert res.latency_ms == 0
    assert res.error is None
    assert res.stage == "timeline"
    assert res.model == "gpt-5.4-nano"


def test_ping_stage_success_records_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ping delegates to the child without importing LiteLLM in the parent."""
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    calls: list[dict] = []
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "litellm" or name.startswith("litellm."):
            raise AssertionError("ping parent attempted to import LiteLLM")
        return real_import(name, *args, **kwargs)

    def fake_attempt(kwargs, *, timeout_seconds):
        assert timeout_seconds == kwargs["timeout"]
        calls.append(kwargs)
        return object()  # ping_stage doesn't read the response

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(llm_mod, "_run_provider_attempt", fake_attempt)
    cfg = _cfg_with_model()

    res = llm_mod.ping_stage(cfg, "reducer")

    assert res.ok is True
    assert res.mocked is False
    assert res.error is None
    assert res.latency_ms is not None and res.latency_ms >= 0
    # ping should keep the request small and bounded.
    assert calls[0]["max_tokens"] == 4
    assert "timeout" in calls[0]
    assert calls[0]["num_retries"] == 0


def test_ping_stage_failure_label_excludes_provider_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider-controlled exception text never enters status output."""
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)

    class AuthenticationError(Exception):
        pass

    def boom(_kwargs, *, timeout_seconds):  # noqa: ARG001
        raise AuthenticationError("Invalid api key sk-bo***ee")

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", boom)
    cfg = _cfg_with_model()

    res = llm_mod.ping_stage(cfg, "classifier")

    assert res.ok is False
    assert res.error == "AuthenticationError"
    assert "Invalid api key" not in res.error
    assert "sk-bo" not in res.error


def test_ping_stage_failure_with_empty_message_falls_back_to_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When str(exc) is empty, the error label is just the class name."""
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    class Timeout(Exception):
        pass

    def boom(_kwargs, *, timeout_seconds):  # noqa: ARG001
        raise Timeout()

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", boom)
    cfg = _cfg_with_model()

    res = llm_mod.ping_stage(cfg, "compact")

    assert res.ok is False
    assert res.error == "Timeout"


def test_ping_stage_truncates_long_error_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even an unusual exception class cannot expand status without bound."""
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)

    long_error = type("Provider" + "X" * 100, (Exception,), {})

    def boom(_kwargs, *, timeout_seconds):  # noqa: ARG001
        raise long_error("SECRET")

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", boom)
    cfg = _cfg_with_model()

    res = llm_mod.ping_stage(cfg, "timeline")

    assert res.ok is False
    assert res.error is not None
    assert len(res.error) <= 80
    assert "SECRET" not in res.error
