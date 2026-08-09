"""litellm wrapper with per-stage model resolution."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import math
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Config, resolve_api_key
from ..logger import get
from ..packaged_entry import PROVIDER_WORKER, worker_command

logger = get("openchronicle.writer")

DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_NUM_RETRIES = 2
MAX_TIMEOUT_SECONDS = 1800.0
MAX_NUM_RETRIES = 5
MAX_INFLIGHT_PROVIDER_CALLS = 8
_OUTER_TIMEOUT_GRACE_SECONDS = 1.0
_PROVIDER_TERMINATE_GRACE_SECONDS = 0.25
_MAX_PROVIDER_REQUEST_BYTES = 64 * 1024 * 1024
_MAX_PROVIDER_RESPONSE_BYTES = 16 * 1024 * 1024
_PROVIDER_PROTOCOL_VERSION = 1
_PROVIDER_RESPONSE_FIELDS = frozenset(
    {
        "id",
        "choices",
        "created",
        "model",
        "object",
        "system_fingerprint",
        "usage",
    }
)
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

_provider_call_slots = threading.BoundedSemaphore(MAX_INFLIGHT_PROVIDER_CALLS)
_provider_runtime_condition = threading.Condition(threading.RLock())
_provider_call_context = threading.local()
_provider_runtime_generation = 0


class ProviderCallError(RuntimeError):
    """A provider attempt failed behind the sanitized process boundary."""

    def __init__(
        self,
        message: str = "Model provider call failed.",
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable


class ProviderCallTimeoutError(ProviderCallError, TimeoutError):
    """The provider crossed the outer deadline and was terminated."""


class ProviderCallCapacityError(ProviderCallError):
    """All bounded provider process slots are occupied."""


class ProviderCallUnavailableError(ProviderCallError):
    """The isolated provider worker failed outside the provider protocol."""


class ProviderCallCancelledError(ProviderCallError):
    """Daemon shutdown revoked this provider attempt."""


@dataclass(eq=False)
class ProviderDaemonRuntime:
    """One daemon run's provider admission and drain state."""

    generation: int
    accepting: bool = True
    active_calls: int = 0
    processes: set[_ManagedProviderProcess] = field(default_factory=set)
    worker_threads: set[threading.Thread] = field(default_factory=set)
    cancelled: threading.Event = field(default_factory=threading.Event)
    finished: bool = False


@dataclass(eq=False)
class _ManagedProviderProcess:
    process: subprocess.Popen[bytes]
    runtime: ProviderDaemonRuntime | None
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    termination_lock: threading.Lock = field(default_factory=threading.Lock)
    termination_started: bool = False
    termination_done: threading.Event = field(default_factory=threading.Event)


@dataclass
class PingResult:
    stage: str
    model: str
    ok: bool
    latency_ms: int | None
    error: str | None
    mocked: bool = False


_active_provider_runtime: ProviderDaemonRuntime | None = None
_provider_runtime_context: contextvars.ContextVar[ProviderDaemonRuntime | None] = (
    contextvars.ContextVar("openchronicle_provider_runtime", default=None)
)


def begin_daemon_provider_runtime() -> ProviderDaemonRuntime:
    """Open one daemon-scoped provider admission generation."""
    global _active_provider_runtime, _provider_runtime_generation

    with _provider_runtime_condition:
        if _active_provider_runtime is not None:
            raise RuntimeError("a daemon provider runtime is already active")
        _provider_runtime_generation += 1
        runtime = ProviderDaemonRuntime(generation=_provider_runtime_generation)
        _active_provider_runtime = runtime
        return runtime


def ensure_daemon_provider_runtime() -> tuple[ProviderDaemonRuntime, bool]:
    """Return the active generation, creating one for a direct daemon run."""
    global _active_provider_runtime, _provider_runtime_generation

    with _provider_runtime_condition:
        runtime = _active_provider_runtime
        if runtime is not None:
            if runtime.finished:
                raise RuntimeError("the active daemon provider runtime is finished")
            return runtime, False
        _provider_runtime_generation += 1
        runtime = ProviderDaemonRuntime(generation=_provider_runtime_generation)
        _active_provider_runtime = runtime
        return runtime, True


def bind_daemon_provider_runtime(
    runtime: ProviderDaemonRuntime,
) -> contextvars.Token[ProviderDaemonRuntime | None]:
    """Bind daemon work so copied executor contexts cannot join a later run."""
    with _provider_runtime_condition:
        if _active_provider_runtime is not runtime or runtime.finished:
            raise RuntimeError("daemon provider runtime is not active")
    return _provider_runtime_context.set(runtime)


def reset_daemon_provider_runtime(
    token: contextvars.Token[ProviderDaemonRuntime | None],
) -> None:
    """Restore the caller's prior provider runtime binding."""
    _provider_runtime_context.reset(token)


def cancel_daemon_provider_runtime(runtime: ProviderDaemonRuntime) -> None:
    """Close admission and synchronously terminate every active provider group."""
    with _provider_runtime_condition:
        if runtime.finished:
            return
        if _active_provider_runtime is not runtime:
            raise RuntimeError("daemon provider runtime is not active")
        runtime.accepting = False
        runtime.cancelled.set()
        processes = tuple(runtime.processes)
        for managed in processes:
            managed.cancel_requested.set()
        _provider_runtime_condition.notify_all()

    _terminate_provider_processes(processes)


def drain_daemon_provider_runtime(runtime: ProviderDaemonRuntime) -> None:
    """Wait for calls to unwind and join daemon worker threads that used them."""
    current = threading.current_thread()
    while True:
        with _provider_runtime_condition:
            if runtime.finished:
                return
            if _active_provider_runtime is not runtime:
                raise RuntimeError("daemon provider runtime is not active")
            while runtime.active_calls or runtime.processes:
                _provider_runtime_condition.wait()
            threads = tuple(runtime.worker_threads)

        if current in threads:
            raise RuntimeError("a daemon provider worker cannot drain its own runtime")
        for thread in threads:
            thread.join()

        with _provider_runtime_condition:
            runtime.worker_threads.difference_update(
                thread for thread in threads if not thread.is_alive()
            )
            if not runtime.worker_threads and not runtime.active_calls and not runtime.processes:
                return


def finish_daemon_provider_runtime(runtime: ProviderDaemonRuntime) -> None:
    """Idempotently cancel, drain, and close one daemon provider generation."""
    global _active_provider_runtime

    with _provider_runtime_condition:
        if runtime.finished:
            return
    cancel_daemon_provider_runtime(runtime)
    while True:
        drain_daemon_provider_runtime(runtime)
        with _provider_runtime_condition:
            if runtime.finished:
                return
            if _active_provider_runtime is not runtime:
                raise RuntimeError("daemon provider runtime is not active")
            # A one-shot thread can register between ``drain`` returning and
            # this lock acquisition. Seal the generation only when the state
            # is still empty while holding the same lock used by registration.
            if runtime.active_calls or runtime.processes or runtime.worker_threads:
                continue
            runtime.finished = True
            _active_provider_runtime = None
            _provider_runtime_condition.notify_all()
            return


def track_daemon_provider_worker(thread: threading.Thread) -> None:
    """Keep a daemon-owned one-shot worker under the active runtime's drain."""
    with _provider_runtime_condition:
        runtime = _provider_runtime_context.get() or _active_provider_runtime
        if (
            runtime is not None
            and _active_provider_runtime is runtime
            and not runtime.finished
        ):
            runtime.worker_threads.add(thread)
            _provider_runtime_condition.notify_all()


def _enter_provider_call() -> ProviderDaemonRuntime | None:
    with _provider_runtime_condition:
        runtime = _provider_runtime_context.get() or _active_provider_runtime
        if runtime is not None:
            if _active_provider_runtime is not runtime or runtime.finished:
                raise ProviderCallCancelledError(
                    "Model provider call cancelled during daemon shutdown."
                )
            if threading.current_thread().daemon:
                runtime.worker_threads.add(threading.current_thread())
            if not runtime.accepting:
                raise ProviderCallCancelledError(
                    "Model provider call cancelled during daemon shutdown."
                )
            runtime.active_calls += 1
        stack = getattr(_provider_call_context, "runtime_stack", None)
        if stack is None:
            stack = []
            _provider_call_context.runtime_stack = stack
        stack.append(runtime)
        return runtime


def _exit_provider_call(runtime: ProviderDaemonRuntime | None) -> None:
    stack = getattr(_provider_call_context, "runtime_stack", None)
    if not stack or stack[-1] is not runtime:
        raise RuntimeError("provider call runtime stack is inconsistent")
    stack.pop()
    with _provider_runtime_condition:
        if runtime is not None:
            runtime.active_calls -= 1
            if runtime.active_calls < 0:
                raise RuntimeError("provider call runtime count is inconsistent")
        _provider_runtime_condition.notify_all()


def _runtime_for_provider_attempt() -> ProviderDaemonRuntime | None:
    stack = getattr(_provider_call_context, "runtime_stack", None)
    if stack:
        return stack[-1]
    bound_runtime = _provider_runtime_context.get()
    if bound_runtime is not None:
        return bound_runtime
    with _provider_runtime_condition:
        return _active_provider_runtime


def call_llm(
    cfg: Config,
    stage: str,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    json_mode: bool = False,
) -> Any:
    """Invoke the configured model stage through the isolated provider worker.

    Respects OPENCHRONICLE_LLM_MOCK=1 for tests: returns a minimal stub.
    """
    runtime = _enter_provider_call()
    try:
        if os.environ.get("OPENCHRONICLE_LLM_MOCK") == "1":
            return _mock_response(stage, messages, tools, json_mode)
        return _call_llm_unmocked(
            cfg,
            stage,
            messages=messages,
            tools=tools,
            json_mode=json_mode,
        )
    finally:
        _exit_provider_call(runtime)


def _call_llm_unmocked(
    cfg: Config,
    stage: str,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    json_mode: bool,
) -> Any:
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

    # Keep retries here instead of nesting LiteLLM's own retry loop. The child
    # maps provider failures to a sanitized ``retryable`` boolean, so the
    # parent never imports provider libraries or trusts exception details.
    kwargs["timeout"] = timeout
    kwargs["num_retries"] = 0
    max_attempts = retries + 1
    for attempt in range(1, max_attempts + 1):
        runtime = _runtime_for_provider_attempt()
        if runtime is not None and runtime.cancelled.is_set():
            raise ProviderCallCancelledError(
                "Model provider call cancelled during daemon shutdown."
            )
        logger.debug(
            "llm call stage=%s model=%s attempt=%d/%d",
            stage,
            model_cfg.model,
            attempt,
            max_attempts,
        )
        try:
            return _run_provider_attempt(
                kwargs,
                timeout_seconds=timeout,
            )
        except ProviderCallCancelledError:
            logger.info("llm provider cancelled during daemon shutdown stage=%s", stage)
            raise
        except ProviderCallTimeoutError:
            # The provider ignored or crossed its SDK timeout. The isolated
            # process has been killed and reaped, but retrying a hard deadline
            # would silently multiply the configured wall-clock budget.
            logger.error(
                "llm provider exceeded outer deadline stage=%s attempt=%d/%d",
                stage,
                attempt,
                max_attempts,
            )
            raise
        except ProviderCallCapacityError:
            logger.error("llm provider capacity exhausted stage=%s", stage)
            raise
        except ProviderCallUnavailableError:
            logger.error("llm provider worker unavailable stage=%s", stage)
            raise
        except ProviderCallError as exc:
            retryable = exc.retryable
            if attempt >= max_attempts or not retryable:
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
            if runtime is not None:
                if runtime.cancelled.wait(delay):
                    raise ProviderCallCancelledError(
                        "Model provider call cancelled during daemon shutdown."
                    ) from None
            else:
                time.sleep(delay)
        except Exception:
            # Any unexpected parent-side failure is local and unclassified.
            # It must not be retried or expose its message across the wrapper.
            raise ProviderCallError(retryable=False) from None

    raise AssertionError("unreachable")


def _run_provider_attempt(kwargs: dict[str, Any], *, timeout_seconds: float) -> Any:
    """Run one synchronous provider attempt in a killable process group.

    The request is the only input frame and is written over stdin, so secrets
    are never placed in argv. The child returns one bounded JSON envelope.
    Crossing the deadline always terminates the entire process group and reaps
    the direct child before this function returns.
    """
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("provider timeout must be a positive finite number")

    request = _encode_provider_request(kwargs)
    slots = _provider_call_slots
    if not slots.acquire(blocking=False):
        raise ProviderCallCapacityError(
            "Model provider capacity is exhausted."
        )

    managed: _ManagedProviderProcess | None = None
    try:
        managed = _start_provider_process()
        process = managed.process

        outer_timeout = float(timeout_seconds) + _OUTER_TIMEOUT_GRACE_SECONDS
        try:
            output = _exchange_provider_process(
                managed,
                request,
                timeout_seconds=outer_timeout,
            )
        except ProviderCallCancelledError:
            raise
        except subprocess.TimeoutExpired:
            _terminate_provider_process(managed)
            if managed.cancel_requested.is_set():
                raise ProviderCallCancelledError(
                    "Model provider call cancelled during daemon shutdown."
                ) from None
            raise ProviderCallTimeoutError(
                "Model provider call exceeded its outer deadline."
            ) from None
        except Exception:
            _terminate_provider_process(managed)
            if managed.cancel_requested.is_set():
                raise ProviderCallCancelledError(
                    "Model provider call cancelled during daemon shutdown."
                ) from None
            raise ProviderCallUnavailableError(
                "The model provider worker failed."
            ) from None

        if managed.cancel_requested.is_set():
            raise ProviderCallCancelledError(
                "Model provider call cancelled during daemon shutdown."
            )
        # A complete, bounded protocol frame is the success authority. The
        # exchange path seals the owned process group with SIGKILL before
        # reaping, so a leader that had not quite exited after closing the
        # protocol fd legitimately reports ``-SIGKILL``. Any other non-zero
        # status still fails closed even if it emitted plausible JSON.
        if process.returncode not in {0, -signal.SIGKILL}:
            raise ProviderCallUnavailableError(
                "The model provider worker failed."
            )
        response = _decode_provider_response(output)
        if managed.cancel_requested.is_set():
            raise ProviderCallCancelledError(
                "Model provider call cancelled during daemon shutdown."
            )
        return response
    finally:
        try:
            if managed is not None and not managed.termination_done.is_set():
                _terminate_provider_process(managed)
        finally:
            try:
                if managed is not None:
                    _close_provider_pipes(managed.process)
            finally:
                try:
                    slots.release()
                finally:
                    if managed is not None:
                        _unregister_provider_process(managed)


def _start_provider_process() -> _ManagedProviderProcess:
    runtime = _runtime_for_provider_attempt()
    with _provider_runtime_condition:
        if runtime is not None and (
            _active_provider_runtime is not runtime or not runtime.accepting
        ):
            raise ProviderCallCancelledError(
                "Model provider call cancelled during daemon shutdown."
            )
        try:
            process = subprocess.Popen(
                _provider_worker_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except Exception:
            raise ProviderCallUnavailableError(
                "The model provider worker could not be started."
            ) from None
        managed = _ManagedProviderProcess(process=process, runtime=runtime)
        if runtime is not None:
            runtime.processes.add(managed)
            _provider_runtime_condition.notify_all()
        return managed


def _unregister_provider_process(managed: _ManagedProviderProcess) -> None:
    runtime = managed.runtime
    if runtime is None:
        return
    with _provider_runtime_condition:
        runtime.processes.discard(managed)
        _provider_runtime_condition.notify_all()


def _provider_worker_command() -> tuple[str, ...]:
    """Return an argv with no request data or credentials."""
    return worker_command(
        PROVIDER_WORKER,
        (
            sys.executable,
            "-c",
            "from openchronicle.writer.llm import _provider_worker_main; "
            "_provider_worker_main()",
        ),
    )


def _encode_provider_request(kwargs: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            {
                "version": _PROVIDER_PROTOCOL_VERSION,
                "parent_pid": os.getpid(),
                "kwargs": kwargs,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ProviderCallUnavailableError(
            "The model provider request could not be serialized."
        ) from None
    if len(payload) > _MAX_PROVIDER_REQUEST_BYTES:
        raise ProviderCallUnavailableError(
            "The model provider request exceeds the process boundary limit."
        )
    return payload


class ProviderResponse(dict[str, Any]):
    """JSON-backed response with LiteLLM-compatible attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def model_dump(self, *, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
        """Return a plain JSON-compatible mapping for compatibility callers."""
        return json.loads(json.dumps(self, ensure_ascii=False))


def _response_value(value: Any) -> Any:
    if isinstance(value, dict):
        return ProviderResponse(
            {str(key): _response_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return [_response_value(item) for item in value]
    return value


def _decode_provider_response(payload: bytes) -> Any:
    if not payload or len(payload) > _MAX_PROVIDER_RESPONSE_BYTES:
        raise ProviderCallUnavailableError(
            "The model provider worker returned an invalid response."
        )
    try:
        envelope = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderCallUnavailableError(
            "The model provider worker returned an invalid response."
        ) from None
    if (
        not isinstance(envelope, dict)
        or isinstance(envelope.get("version"), bool)
        or envelope.get("version") != _PROVIDER_PROTOCOL_VERSION
    ):
        raise ProviderCallUnavailableError(
            "The model provider worker returned an invalid response."
        )

    status = envelope.get("status")
    if (
        status == "provider_error"
        and set(envelope) == {"version", "status", "retryable"}
        and isinstance(envelope.get("retryable"), bool)
    ):
        raise ProviderCallError(retryable=envelope["retryable"])
    if status == "worker_error" and set(envelope) == {"version", "status"}:
        raise ProviderCallUnavailableError("The model provider worker failed.")
    response_payload = envelope.get("response")
    if (
        status != "ok"
        or set(envelope) != {"version", "status", "response"}
        or not isinstance(response_payload, dict)
        or not set(response_payload).issubset(_PROVIDER_RESPONSE_FIELDS)
        or not isinstance(response_payload.get("choices"), list)
    ):
        raise ProviderCallUnavailableError(
            "The model provider worker returned an invalid response."
        )

    for choice in response_payload["choices"]:
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ProviderCallUnavailableError(
                "The model provider worker returned an invalid response."
            )
        message = choice["message"]
        if message.get("content") is not None and not isinstance(
            message.get("content"), str
        ):
            raise ProviderCallUnavailableError(
                "The model provider worker returned an invalid response."
            )
        if message.get("tool_calls") is not None and not isinstance(
            message.get("tool_calls"), list
        ):
            raise ProviderCallUnavailableError(
                "The model provider worker returned an invalid response."
            )

    response = _response_value(response_payload)
    if not isinstance(response, ProviderResponse):
        raise ProviderCallUnavailableError(
            "The model provider worker returned an invalid response."
        )
    return response


def _exchange_provider_process(
    managed: _ManagedProviderProcess,
    request: bytes,
    *,
    timeout_seconds: float,
) -> bytes:
    """Exchange bounded pipe data without implicitly reaping the child.

    ``Popen.communicate`` calls ``wait`` internally. During concurrent daemon
    cancellation that could reap a TERM-compliant process-group leader before
    the shutdown owner sends its post-grace KILL, allowing the numeric PGID to
    be recycled. Non-blocking pipe I/O leaves every reap behind the same lock
    used to claim group termination, so no group signal can follow a reap.
    """
    process = managed.process
    stdin = process.stdin
    stdout = process.stdout
    if stdin is None or stdout is None:
        raise ProviderCallUnavailableError("The model provider worker failed.")

    stdin_fd = stdin.fileno()
    stdout_fd = stdout.fileno()
    os.set_blocking(stdin_fd, False)
    os.set_blocking(stdout_fd, False)
    selector = selectors.DefaultSelector()
    selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
    if request:
        selector.register(stdin_fd, selectors.EVENT_WRITE, "stdin")
        stdin_open = True
    else:
        stdin.close()
        stdin_open = False

    output = bytearray()
    request_view = memoryview(request)
    request_offset = 0
    stdout_open = True
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            if managed.cancel_requested.is_set():
                _terminate_provider_process(managed)
                raise ProviderCallCancelledError(
                    "Model provider call cancelled during daemon shutdown."
                )

            if not stdout_open:
                _seal_completed_provider_process(managed)
                return bytes(output)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(
                    _provider_worker_command(), timeout_seconds
                )

            # The short ceiling is only a cancellation responsiveness bound;
            # readable/writable pipes and process-group teardown wake sooner.
            events = selector.select(min(remaining, 0.05))
            for key, mask in events:
                if key.data == "stdin" and mask & selectors.EVENT_WRITE:
                    try:
                        written = os.write(
                            stdin_fd,
                            request_view[request_offset : request_offset + 64 * 1024],
                        )
                    except (BlockingIOError, InterruptedError):
                        continue
                    except BrokenPipeError:
                        written = 0
                    if written:
                        request_offset += written
                    if not written or request_offset == len(request_view):
                        with contextlib.suppress(Exception):
                            selector.unregister(stdin_fd)
                        stdin.close()
                        stdin_open = False

                if key.data == "stdout" and mask & selectors.EVENT_READ:
                    try:
                        chunk = os.read(stdout_fd, 64 * 1024)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if chunk:
                        output.extend(chunk)
                        if len(output) > _MAX_PROVIDER_RESPONSE_BYTES:
                            raise ProviderCallUnavailableError(
                                "The model provider worker returned an invalid response."
                            )
                    else:
                        with contextlib.suppress(Exception):
                            selector.unregister(stdout_fd)
                        stdout.close()
                        stdout_open = False
    finally:
        selector.close()
        if stdin_open:
            with contextlib.suppress(OSError, ValueError):
                stdin.close()


def _seal_completed_provider_process(managed: _ManagedProviderProcess) -> None:
    """Fence every owned descendant before reaping a protocol-complete leader.

    A provider library can spawn a background process that closes inherited
    stdio and survives after the direct worker emits a valid frame. Reaping the
    leader first would free its numeric PID/PGID, making a later group signal
    unsafe. Claim teardown while the leader remains unreaped, kill the exact
    owned group, then reap. There is no TERM grace on this path: the complete
    response is already buffered and no provider-side work remains authorized.
    """
    owner = False
    with managed.termination_lock:
        if managed.termination_done.is_set():
            return
        if managed.termination_started:
            pass
        else:
            managed.termination_started = True
            owner = True

    if not owner:
        _terminate_provider_process(managed)
        return

    first_error: BaseException | None = None
    try:
        _signal_provider_process_group(managed.process, signal.SIGKILL)
    except BaseException as exc:
        first_error = exc
    try:
        managed.process.wait()
    except BaseException as exc:
        if first_error is None:
            first_error = exc
    finally:
        managed.termination_done.set()

    if first_error is not None:
        raise first_error


def _terminate_provider_process(managed: _ManagedProviderProcess) -> None:
    """Idempotently terminate and reap one managed provider process group."""
    _terminate_provider_processes((managed,))


def _terminate_provider_processes(
    processes: tuple[_ManagedProviderProcess, ...],
) -> None:
    """TERM, then KILL, provider groups in parallel and reap every leader.

    Daemon shutdown and an attempt's timeout/finally path can race. Exactly one
    caller owns each process's escalation; the others wait for that owner to
    finish reaping it. A batch pays one grace interval rather than one interval
    per process, keeping shutdown bounded when all provider slots are occupied.
    """
    owners: list[_ManagedProviderProcess] = []
    waiters: list[_ManagedProviderProcess] = []
    for managed in dict.fromkeys(processes):
        with managed.termination_lock:
            if managed.termination_done.is_set():
                continue
            if managed.termination_started:
                waiters.append(managed)
            else:
                managed.termination_started = True
                owners.append(managed)

    first_error: BaseException | None = None

    # Do not reap a group leader during the grace period. Its unreaped pid
    # prevents the process-group id from being recycled while descendants may
    # still be handling TERM.
    for managed in owners:
        try:
            _signal_provider_process_group(managed.process, signal.SIGTERM)
        except BaseException as exc:  # cleanup must continue for every group
            if first_error is None:
                first_error = exc
    if owners:
        try:
            time.sleep(_PROVIDER_TERMINATE_GRACE_SECONDS)
        except BaseException as exc:
            if first_error is None:
                first_error = exc

    for managed in owners:
        try:
            _signal_provider_process_group(managed.process, signal.SIGKILL)
        except BaseException as exc:
            if first_error is None:
                first_error = exc

    for managed in owners:
        try:
            # POSIX offers no stronger userspace action after SIGKILL. Waiting
            # without a second timeout preserves the no-zombie/no-orphan
            # contract; only an exceptional uninterruptible kernel sleep can
            # delay reaping.
            managed.process.wait()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        finally:
            managed.termination_done.set()

    for managed in waiters:
        managed.termination_done.wait()

    if first_error is not None:
        raise first_error


def _signal_provider_process_group(
    process: subprocess.Popen[bytes], signum: signal.Signals
) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        return
    except OSError:
        # ``start_new_session=True`` should make pid the process-group id. The
        # direct-process fallback still guarantees that the child is reaped.
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signum)


def _close_provider_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdin, process.stdout):
        if pipe is not None:
            with contextlib.suppress(OSError, ValueError):
                pipe.close()


def _provider_worker_main() -> None:
    """Child entry point: read one frame and emit one bounded envelope."""
    protocol_fd = os.dup(sys.stdout.fileno())
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        # Provider libraries occasionally print diagnostics. Keep stdout as a
        # protocol-only channel and discard child diagnostics at the boundary.
        os.dup2(devnull_fd, sys.stdout.fileno())
        os.dup2(devnull_fd, sys.stderr.fileno())
    finally:
        os.close(devnull_fd)

    try:
        request_bytes = sys.stdin.buffer.read(_MAX_PROVIDER_REQUEST_BYTES + 1)
        logging.disable(logging.CRITICAL)
        envelope = _provider_worker_envelope(request_bytes, enforce_parent=True)
        response_bytes = _encode_provider_envelope(envelope)
    except BaseException:  # child must never serialize exception text
        response_bytes = _worker_error_bytes()

    try:
        view = memoryview(response_bytes)
        while view:
            written = os.write(protocol_fd, view)
            view = view[written:]
    finally:
        os.close(protocol_fd)


def _provider_worker_envelope(
    request_bytes: bytes,
    *,
    enforce_parent: bool = False,
) -> dict[str, Any]:
    if not request_bytes or len(request_bytes) > _MAX_PROVIDER_REQUEST_BYTES:
        return _worker_error_envelope()
    try:
        request = json.loads(request_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _worker_error_envelope()
    if (
        not isinstance(request, dict)
        or request.get("version") != _PROVIDER_PROTOCOL_VERSION
        or isinstance(request.get("parent_pid"), bool)
        or not isinstance(request.get("parent_pid"), int)
        or request["parent_pid"] <= 1
        or not isinstance(request.get("kwargs"), dict)
    ):
        return _worker_error_envelope()
    if enforce_parent:
        if os.getppid() != request["parent_pid"]:
            return _worker_error_envelope()
        _start_parent_watchdog(request["parent_pid"])

    # No child log or exception message is allowed to cross the process
    # boundary. The parent receives only a retry classification.
    try:
        # LiteLLM otherwise fetches its model-cost map during import. Keep
        # worker startup local and deterministic; actual provider egress still
        # happens only when ``completion`` executes below.
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        import litellm

        _disable_litellm_global_retries(litellm)
    except Exception:
        return _worker_error_envelope()

    try:
        response = litellm.completion(**request["kwargs"])
    except Exception as exc:
        try:
            retryable = _is_retryable_error(litellm, exc)
        except Exception:
            retryable = False
        return {
            "version": _PROVIDER_PROTOCOL_VERSION,
            "status": "provider_error",
            "retryable": retryable,
        }
    except BaseException:
        return _worker_error_envelope()

    try:
        response_payload = response.model_dump(mode="json")
    except Exception:
        return _worker_error_envelope()
    if not isinstance(response_payload, dict):
        return _worker_error_envelope()
    response_payload = {
        key: value
        for key, value in response_payload.items()
        if key in _PROVIDER_RESPONSE_FIELDS
    }
    return {
        "version": _PROVIDER_PROTOCOL_VERSION,
        "status": "ok",
        "response": response_payload,
    }


def _start_parent_watchdog(parent_pid: int) -> None:
    """Kill the provider group if its supervising parent disappears."""

    def watch() -> None:
        while os.getppid() == parent_pid:
            time.sleep(0.1)
        with contextlib.suppress(OSError):
            os.killpg(os.getpid(), signal.SIGKILL)

    threading.Thread(
        target=watch,
        name="openchronicle-provider-parent-watchdog",
        daemon=True,
    ).start()


def _encode_provider_envelope(envelope: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return _worker_error_bytes()
    if len(payload) > _MAX_PROVIDER_RESPONSE_BYTES:
        return _worker_error_bytes()
    return payload


def _worker_error_envelope() -> dict[str, Any]:
    return {
        "version": _PROVIDER_PROTOCOL_VERSION,
        "status": "worker_error",
    }


def _worker_error_bytes() -> bytes:
    return (
        b'{"version":'
        + str(_PROVIDER_PROTOCOL_VERSION).encode("ascii")
        + b',"status":"worker_error"}'
    )


def _is_retryable_error(litellm_module: Any, exc: Exception) -> bool:
    """Return whether *exc* is an explicitly transient LiteLLM failure."""
    if isinstance(exc, ProviderCallError):
        return exc.retryable

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


def call_budget_seconds(cfg: Config, stage: str) -> float:
    """Conservative wall-clock budget for one fully retried provider call."""
    timeout, retries = _resolved_limits(cfg.model_for(stage))
    backoff = sum(
        min(_RETRY_BACKOFF_SECONDS * (2**attempt), _MAX_RETRY_BACKOFF_SECONDS)
        for attempt in range(retries)
    )
    process_budget = (timeout + _OUTER_TIMEOUT_GRACE_SECONDS) * (retries + 1)
    return process_budget + backoff + _PROVIDER_TERMINATE_GRACE_SECONDS + 30.0


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
        _run_provider_attempt(
            kwargs,
            timeout_seconds=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return PingResult(
            stage=stage,
            model=model_cfg.model,
            ok=False,
            latency_ms=None,
            error=type(exc).__name__[:80],
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
