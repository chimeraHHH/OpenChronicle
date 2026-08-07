"""Reliability contract for normal (non-ping) LLM calls."""

from __future__ import annotations

from typing import Any

import pytest

from openchronicle.config import Config, ModelConfig
from openchronicle.writer import llm as llm_mod


class TransientError(Exception):
    """Test double for one of LiteLLM's mapped transient failures."""


def _config(*, timeout: float | None = None, retries: int | None = None) -> Config:
    return Config(
        models={
            "default": ModelConfig(
                model="test/model",
                api_key="sk-test",
                timeout_seconds=timeout,
                num_retries=retries,
            )
        }
    )


def _call(cfg: Config) -> Any:
    return llm_mod.call_llm(
        cfg,
        "timeline",
        messages=[{"role": "user", "content": "hello"}],
    )


def _install_transient_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    monkeypatch.setattr(litellm, "Timeout", TransientError)


def test_call_llm_passes_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    import litellm

    calls: list[dict[str, Any]] = []
    expected = object()

    def completion(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(litellm, "completion", completion)

    assert _call(_config()) is expected
    assert len(calls) == 1
    assert calls[0]["timeout"] == llm_mod.DEFAULT_TIMEOUT_SECONDS
    # The wrapper owns retry classification and attempt accounting.
    assert calls[0]["num_retries"] == 0


def test_call_llm_clears_litellm_global_retry_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    import litellm

    monkeypatch.setattr(litellm, "num_retries", 4)

    def completion(**_kwargs):
        assert litellm.num_retries == 0
        return object()

    monkeypatch.setattr(litellm, "completion", completion)

    _call(_config(retries=0))
    assert litellm.num_retries == 0


def test_retryable_error_retries_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    import litellm

    _install_transient_error(monkeypatch)
    calls: list[dict[str, Any]] = []
    sleeps: list[float] = []
    expected = object()

    def completion(**kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise TransientError("temporary")
        return expected

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)

    assert _call(_config(timeout=7.5, retries=2)) is expected
    assert len(calls) == 3
    assert {call["timeout"] for call in calls} == {7.5}
    assert sleeps == [1.0, 2.0]


def test_non_retryable_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    import litellm

    calls = 0
    sleeps: list[float] = []

    def completion(**kwargs):  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise ValueError("invalid request")

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)

    with pytest.raises(ValueError, match="invalid request"):
        _call(_config(retries=2))
    assert calls == 1
    assert sleeps == []


@pytest.mark.parametrize("status_code", [408, 409, 429, 500, 502, 503])
def test_generic_api_error_retries_by_http_status(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    import litellm

    class GenericAPIError(Exception):
        def __init__(self) -> None:
            self.status_code = status_code

    calls = 0

    def completion(**kwargs):  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GenericAPIError
        return object()

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _seconds: None)

    _call(_config(retries=1))
    assert calls == 2


def test_generic_api_error_does_not_retry_permanent_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    import litellm

    class GenericAPIError(Exception):
        status_code = 400

    calls = 0

    def completion(**kwargs):  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise GenericAPIError

    monkeypatch.setattr(litellm, "completion", completion)

    with pytest.raises(GenericAPIError):
        _call(_config(retries=2))
    assert calls == 1


def test_zero_retries_attempts_retryable_error_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    import litellm

    _install_transient_error(monkeypatch)
    calls = 0
    sleeps: list[float] = []

    def completion(**kwargs):  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise TransientError("temporary")

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)

    with pytest.raises(TransientError, match="temporary"):
        _call(_config(retries=0))
    assert calls == 1
    assert sleeps == []


def test_retryable_error_stops_at_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    import litellm

    _install_transient_error(monkeypatch)
    calls = 0
    sleeps: list[float] = []

    def completion(**kwargs):  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise TransientError("still unavailable")

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)

    with pytest.raises(TransientError, match="still unavailable"):
        _call(_config(retries=2))
    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_mock_path_never_waits_or_calls_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCHRONICLE_LLM_MOCK", "1")
    import litellm

    def unexpected_call(**kwargs):  # noqa: ARG001
        pytest.fail("mock path must not call litellm")

    def unexpected_sleep(seconds):  # noqa: ARG001
        pytest.fail("mock path must not wait")

    monkeypatch.setattr(litellm, "completion", unexpected_call)
    monkeypatch.setattr(llm_mod.time, "sleep", unexpected_sleep)

    response = _call(_config(timeout=0, retries=-1))
    assert llm_mod.extract_text(response)


@pytest.mark.parametrize(
    ("timeout", "retries", "message"),
    [
        (0, 0, "timeout_seconds"),
        (-1, 0, "timeout_seconds"),
        (float("nan"), 0, "timeout_seconds"),
        (float("inf"), 0, "timeout_seconds"),
        (True, 0, "timeout_seconds"),
        (llm_mod.MAX_TIMEOUT_SECONDS + 1, 0, "timeout_seconds"),
        (1, -1, "num_retries"),
        (1, 1.5, "num_retries"),
        (1, True, "num_retries"),
        (1, llm_mod.MAX_NUM_RETRIES + 1, "num_retries"),
    ],
)
def test_invalid_limits_fail_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    timeout: float,
    retries: Any,
    message: str,
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    import litellm

    def unexpected_call(**kwargs):  # noqa: ARG001
        pytest.fail("invalid limits must fail before the provider call")

    monkeypatch.setattr(litellm, "completion", unexpected_call)

    with pytest.raises(ValueError, match=message):
        _call(_config(timeout=timeout, retries=retries))
