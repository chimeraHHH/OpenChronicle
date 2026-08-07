"""litellm wrapper with per-stage model resolution."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from typing import Any

from ..config import Config, resolve_api_key
from ..logger import get

logger = get("openchronicle.writer")

DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_NUM_RETRIES = 2
MAX_TIMEOUT_SECONDS = 1800.0
MAX_NUM_RETRIES = 5
_RETRY_BACKOFF_SECONDS = 1.0
_MAX_RETRY_BACKOFF_SECONDS = 8.0
_RETRYABLE_ERROR_NAMES = (
    "Timeout",
    "APIConnectionError",
    "RateLimitError",
    "InternalServerError",
    "BadGatewayError",
    "ServiceUnavailableError",
)


@dataclass
class PingResult:
    stage: str
    model: str
    ok: bool
    latency_ms: int | None
    error: str | None
    mocked: bool = False


def call_llm(
    cfg: Config,
    stage: str,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    json_mode: bool = False,
) -> Any:
    """Invoke litellm for the given stage. Returns the raw ModelResponse.

    Respects OPENCHRONICLE_LLM_MOCK=1 for tests: returns a minimal stub.
    """
    if os.environ.get("OPENCHRONICLE_LLM_MOCK") == "1":
        return _mock_response(stage, messages, tools, json_mode)

    import litellm  # imported lazily to keep CLI startup fast

    _disable_litellm_global_retries(litellm)

    model_cfg = cfg.model_for(stage)
    kwargs: dict[str, Any] = {
        "model": model_cfg.model,
        "messages": messages,
    }
    if model_cfg.base_url:
        kwargs["api_base"] = model_cfg.base_url
    api_key = resolve_api_key(model_cfg)
    if api_key:
        kwargs["api_key"] = api_key
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if model_cfg.max_tokens:
        kwargs["max_tokens"] = model_cfg.max_tokens

    timeout, retries = _resolved_limits(model_cfg)

    # Keep retries here instead of nesting LiteLLM's own retry loop. This makes
    # the total attempt count explicit and ensures configuration/authentication
    # errors are never repeated. LiteLLM maps provider failures to the stable
    # exception classes checked by _is_retryable_error().
    kwargs["timeout"] = timeout
    kwargs["num_retries"] = 0
    max_attempts = retries + 1
    for attempt in range(1, max_attempts + 1):
        logger.debug(
            "llm call stage=%s model=%s attempt=%d/%d",
            stage,
            model_cfg.model,
            attempt,
            max_attempts,
        )
        try:
            return litellm.completion(**kwargs)
        except Exception as exc:
            if attempt >= max_attempts or not _is_retryable_error(litellm, exc):
                raise
            delay = min(
                _RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                _MAX_RETRY_BACKOFF_SECONDS,
            )
            logger.warning(
                "llm transient error stage=%s model=%s attempt=%d/%d error=%s; retrying in %.1fs",
                stage,
                model_cfg.model,
                attempt,
                max_attempts,
                type(exc).__name__,
                delay,
            )
            time.sleep(delay)

    raise AssertionError("unreachable")


def _is_retryable_error(litellm_module: Any, exc: Exception) -> bool:
    """Return whether *exc* is an explicitly transient LiteLLM failure."""
    retryable_types = tuple(
        error_type
        for name in _RETRYABLE_ERROR_NAMES
        if isinstance((error_type := getattr(litellm_module, name, None)), type)
        and issubclass(error_type, BaseException)
    )
    if isinstance(exc, (*retryable_types, TimeoutError, ConnectionError)):
        return True

    # Older LiteLLM versions and some Azure/OpenAI provider paths map 5xx to
    # the generic APIError class. Fall back to HTTP status without retrying
    # authentication, validation, or other permanent 4xx failures.
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        return False
    return status_code in {408, 409, 429} or status_code >= 500


def _disable_litellm_global_retries(litellm_module: Any) -> None:
    """Keep LiteLLM's process-global fallback from nesting retries.

    The locked LiteLLM version treats request-level ``num_retries=0`` as
    falsy and can fall back to ``litellm.num_retries``. OpenChronicle owns the
    retry budget for this dedicated process, so clear that fallback as well.
    """
    if getattr(litellm_module, "num_retries", None) not in (None, 0):
        logger.warning("clearing LiteLLM global retries; OpenChronicle owns retry accounting")
    litellm_module.num_retries = 0


def _resolved_limits(model_cfg: Any) -> tuple[float, int]:
    timeout = (
        DEFAULT_TIMEOUT_SECONDS if model_cfg.timeout_seconds is None else model_cfg.timeout_seconds
    )
    retries = DEFAULT_NUM_RETRIES if model_cfg.num_retries is None else model_cfg.num_retries

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"model timeout_seconds must be finite and in (0, {MAX_TIMEOUT_SECONDS:g}]"
        )
    if (
        isinstance(retries, bool)
        or not isinstance(retries, int)
        or retries < 0
        or retries > MAX_NUM_RETRIES
    ):
        raise ValueError(f"model num_retries must be an integer in [0, {MAX_NUM_RETRIES}]")
    return float(timeout), retries


def _mock_response(stage: str, messages, tools, json_mode):
    """Minimal stub for offline tests. Customize via OPENCHRONICLE_LLM_MOCK_JSON."""
    override = os.environ.get("OPENCHRONICLE_LLM_MOCK_JSON")
    content = override if override else '{"worth_writing": false, "brief_reason": "mock"}'

    class _Msg:
        def __init__(self, content, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, msg):
            self.message = msg
            self.finish_reason = "stop"

    class _Resp:
        def __init__(self, choices):
            self.choices = choices

    return _Resp([_Choice(_Msg(content))])


def extract_text(response: Any) -> str:
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


def ping_stage(cfg: Config, stage: str, *, timeout: float = 5.0) -> PingResult:
    """Send a tiny round-trip request to the stage's configured model.

    Returns a PingResult with success, latency, and a short error label on
    failure. Honors OPENCHRONICLE_LLM_MOCK=1 by returning a mocked-ok result
    without touching the network. Never raises — `status` and similar
    informational callers must remain non-fatal.
    """
    model_cfg = cfg.model_for(stage)
    if os.environ.get("OPENCHRONICLE_LLM_MOCK") == "1":
        return PingResult(
            stage=stage,
            model=model_cfg.model,
            ok=True,
            latency_ms=0,
            error=None,
            mocked=True,
        )

    try:
        import litellm  # lazy import — keeps CLI startup fast
    except ImportError as exc:
        return PingResult(
            stage=stage,
            model=model_cfg.model,
            ok=False,
            latency_ms=None,
            error=f"ImportError: {exc}",
        )

    _disable_litellm_global_retries(litellm)

    kwargs: dict[str, Any] = {
        "model": model_cfg.model,
        "messages": [{"role": "user", "content": "Reply with 'ok'."}],
        "max_tokens": 4,
        "timeout": timeout,
        "num_retries": 0,
    }
    if model_cfg.base_url:
        kwargs["api_base"] = model_cfg.base_url
    api_key = resolve_api_key(model_cfg)
    if api_key:
        kwargs["api_key"] = api_key

    start = time.monotonic()
    try:
        litellm.completion(**kwargs)
    except Exception as exc:  # noqa: BLE001
        label = type(exc).__name__
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        if msg:
            label = f"{label}: {msg[:60]}"
        return PingResult(
            stage=stage,
            model=model_cfg.model,
            ok=False,
            latency_ms=None,
            error=label[:80],
        )
    latency_ms = int((time.monotonic() - start) * 1000)
    return PingResult(
        stage=stage,
        model=model_cfg.model,
        ok=True,
        latency_ms=latency_ms,
        error=None,
    )


def extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    try:
        calls = response.choices[0].message.tool_calls or []
    except (AttributeError, IndexError):
        return []
    out: list[dict[str, Any]] = []
    for c in calls:
        fn = getattr(c, "function", None) or c.get("function", {})
        args_raw = (
            getattr(fn, "arguments", None) if hasattr(fn, "arguments") else fn.get("arguments")
        )
        name = getattr(fn, "name", None) if hasattr(fn, "name") else fn.get("name")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except json.JSONDecodeError:
            args = {}
        out.append(
            {
                "id": getattr(c, "id", None) or c.get("id"),
                "name": name,
                "arguments": args,
            }
        )
    return out
