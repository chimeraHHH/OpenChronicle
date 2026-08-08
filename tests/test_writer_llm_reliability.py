"""Reliability contract for normal (non-ping) LLM calls."""

from __future__ import annotations

import builtins
import contextvars
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from openchronicle.config import Config, ModelConfig
from openchronicle.writer import llm as llm_mod


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


def _success_envelope(content: str = "ok") -> dict[str, Any]:
    return {
        "version": llm_mod._PROVIDER_PROTOCOL_VERSION,
        "status": "ok",
        "response": {
            "id": "test-response",
            "model": "test/model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        },
    }


def _command_returning(envelope: dict[str, Any]) -> tuple[str, ...]:
    payload = json.dumps(envelope, separators=(",", ":"))
    return _command_returning_raw(payload)


def _command_returning_raw(payload: str) -> tuple[str, ...]:
    code = f"import sys; sys.stdin.buffer.read(); sys.stdout.write({payload!r})"
    return (sys.executable, "-c", code)


def _hanging_worker_command() -> tuple[str, ...]:
    code = "import sys,time; sys.stdin.buffer.read(); time.sleep(60)"
    return (sys.executable, "-c", code)


def test_call_llm_passes_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)

    calls: list[tuple[dict[str, Any], float]] = []
    expected = object()

    def provider_attempt(kwargs, *, timeout_seconds):
        calls.append((kwargs, timeout_seconds))
        return expected

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", provider_attempt)

    assert _call(_config()) is expected
    assert len(calls) == 1
    kwargs, outer_timeout = calls[0]
    assert kwargs["timeout"] == llm_mod.DEFAULT_TIMEOUT_SECONDS
    assert outer_timeout == llm_mod.DEFAULT_TIMEOUT_SECONDS
    # The wrapper owns retry classification and attempt accounting.
    assert kwargs["num_retries"] == 0


def test_parent_call_and_decode_paths_never_import_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    real_import = builtins.__import__
    imported: list[str] = []

    def guarded_import(name, *args, **kwargs):
        if name == "litellm" or name.startswith("litellm."):
            imported.append(name)
            raise AssertionError("parent attempted to import LiteLLM")
        return real_import(name, *args, **kwargs)

    def provider_attempt(_kwargs, *, timeout_seconds):  # noqa: ARG001
        return llm_mod._decode_provider_response(
            json.dumps(_success_envelope("no parent import")).encode()
        )

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(llm_mod, "_run_provider_attempt", provider_attempt)

    response = _call(_config(retries=0))
    assert llm_mod.extract_text(response) == "no parent import"
    assert imported == []


def test_retryable_error_retries_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)

    calls: list[dict[str, Any]] = []
    sleeps: list[float] = []
    expected = object()

    def provider_attempt(kwargs, *, timeout_seconds):  # noqa: ARG001
        calls.append(kwargs)
        if len(calls) < 3:
            raise llm_mod.ProviderCallError(retryable=True)
        return expected

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", provider_attempt)
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)

    assert _call(_config(timeout=7.5, retries=2)) is expected
    assert len(calls) == 3
    assert {call["timeout"] for call in calls} == {7.5}
    assert sleeps == [1.0, 2.0]


def test_non_retryable_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)

    calls = 0
    sleeps: list[float] = []
    secret = "SECRET_PROVIDER_MESSAGE sk-live-secret"

    def provider_attempt(_kwargs, *, timeout_seconds):  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise ValueError(secret)

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", provider_attempt)
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)

    with pytest.raises(llm_mod.ProviderCallError) as raised:
        _call(_config(retries=2))
    assert calls == 1
    assert sleeps == []
    assert str(raised.value) == "Model provider call failed."
    assert secret not in str(raised.value)
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize("status_code", [408, 409, 429, 500, 502, 503])
def test_parent_does_not_classify_untrusted_http_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)

    class GenericAPIError(Exception):
        def __init__(self) -> None:
            self.status_code = status_code

    calls = 0

    def provider_attempt(_kwargs, *, timeout_seconds):  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise GenericAPIError

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", provider_attempt)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _seconds: None)

    with pytest.raises(llm_mod.ProviderCallError) as raised:
        _call(_config(retries=1))
    assert calls == 1
    assert raised.value.retryable is False


def test_generic_api_error_does_not_retry_permanent_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)

    class GenericAPIError(Exception):
        status_code = 400

    calls = 0

    def provider_attempt(_kwargs, *, timeout_seconds):  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise GenericAPIError

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", provider_attempt)

    with pytest.raises(llm_mod.ProviderCallError) as raised:
        _call(_config(retries=2))
    assert calls == 1
    assert str(raised.value) == "Model provider call failed."


def test_zero_retries_attempts_retryable_error_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)

    calls = 0
    sleeps: list[float] = []

    def provider_attempt(_kwargs, *, timeout_seconds):  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise llm_mod.ProviderCallError(retryable=True)

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", provider_attempt)
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)

    with pytest.raises(llm_mod.ProviderCallError) as raised:
        _call(_config(retries=0))
    assert calls == 1
    assert sleeps == []
    assert raised.value.retryable is True
    assert "temporary" not in str(raised.value)


def test_retryable_error_stops_at_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)

    calls = 0
    sleeps: list[float] = []

    def provider_attempt(_kwargs, *, timeout_seconds):  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise llm_mod.ProviderCallError(retryable=True)

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", provider_attempt)
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)

    with pytest.raises(llm_mod.ProviderCallError) as raised:
        _call(_config(retries=2))
    assert calls == 3
    assert sleeps == [1.0, 2.0]
    assert raised.value.retryable is True
    assert "still unavailable" not in str(raised.value)


def test_provider_ignoring_sdk_timeout_hits_outer_deadline_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    monkeypatch.setattr(llm_mod, "_OUTER_TIMEOUT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(llm_mod, "_PROVIDER_TERMINATE_GRACE_SECONDS", 0.03)
    # Ignore TERM so the test exercises the TERM -> KILL escalation path.
    code = (
        "import signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "sys.stdin.buffer.read(); time.sleep(60)"
    )
    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: (sys.executable, "-c", code),
    )

    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(llm_mod.subprocess, "Popen", recording_popen)

    started = time.monotonic()
    with pytest.raises(llm_mod.ProviderCallTimeoutError) as raised:
        _call(_config(timeout=0.5, retries=llm_mod.MAX_NUM_RETRIES))

    assert time.monotonic() - started < 1.5
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].returncode == -9
    assert str(raised.value) == "Model provider call exceeded its outer deadline."
    assert raised.value.__cause__ is None
    assert "hello" not in str(raised.value)


def test_deadline_kills_pipe_inheriting_provider_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(llm_mod, "_OUTER_TIMEOUT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(llm_mod, "_PROVIDER_TERMINATE_GRACE_SECONDS", 0.03)
    grandchild_pid_file = tmp_path / "grandchild.pid"
    grandchild_code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)"
    )
    worker_code = (
        "import pathlib,signal,subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{grandchild_code!r}]); "
        f"pathlib.Path({str(grandchild_pid_file)!r}).write_text(str(child.pid)); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "sys.stdin.buffer.read(); time.sleep(60)"
    )
    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: (sys.executable, "-c", worker_code),
    )

    real_popen = subprocess.Popen
    direct_children: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        direct_children.append(process)
        return process

    monkeypatch.setattr(llm_mod.subprocess, "Popen", recording_popen)

    started = time.monotonic()
    with pytest.raises(llm_mod.ProviderCallTimeoutError):
        llm_mod._run_provider_attempt({"model": "test"}, timeout_seconds=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert grandchild_pid_file.exists()
    assert len(direct_children) == 1
    direct_child = direct_children[0]
    assert direct_child.poll() is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(direct_child.pid, os.WNOHANG)

    grandchild_pid = int(grandchild_pid_file.read_text())
    monkeypatch.setattr(llm_mod.subprocess, "Popen", real_popen)
    deadline = time.monotonic() + 2.0
    state = ""
    while time.monotonic() < deadline:
        checked = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(grandchild_pid)],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        state = checked.stdout.strip()
        if not state or state.startswith("Z"):
            break
        time.sleep(0.02)
    assert not state or state.startswith("Z")


def test_success_frame_kills_stdio_detached_provider_descendant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    grandchild_pid_file = tmp_path / "success-grandchild.pid"
    grandchild_code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)"
    )
    payload = json.dumps(_success_envelope("sealed success"), separators=(",", ":"))
    worker_code = (
        "import os,pathlib,subprocess,sys; "
        f"child=subprocess.Popen([sys.executable,'-c',{grandchild_code!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,close_fds=True); "
        f"pathlib.Path({str(grandchild_pid_file)!r}).write_text(str(child.pid)); "
        "sys.stdin.buffer.read(); "
        f"os.write(sys.stdout.fileno(),{payload.encode()!r}); "
        "os.close(sys.stdout.fileno())"
    )
    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: (sys.executable, "-c", worker_code),
    )

    real_popen = subprocess.Popen
    direct_children: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        direct_children.append(process)
        return process

    monkeypatch.setattr(llm_mod.subprocess, "Popen", recording_popen)

    response = llm_mod._run_provider_attempt({"model": "test"}, timeout_seconds=2.0)

    assert llm_mod.extract_text(response) == "sealed success"
    assert grandchild_pid_file.exists()
    assert len(direct_children) == 1
    direct_child = direct_children[0]
    assert direct_child.poll() is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(direct_child.pid, os.WNOHANG)

    grandchild_pid = int(grandchild_pid_file.read_text())
    monkeypatch.setattr(llm_mod.subprocess, "Popen", real_popen)
    deadline = time.monotonic() + 2.0
    state = ""
    while time.monotonic() < deadline:
        checked = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(grandchild_pid)],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        state = checked.stdout.strip()
        if not state or state.startswith("Z"):
            break
        time.sleep(0.02)
    assert not state or state.startswith("Z")


def test_child_classified_timeout_returned_before_outer_deadline_still_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    calls = 0
    sleeps: list[float] = []
    expected = object()

    def provider_attempt(_kwargs, *, timeout_seconds):  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise llm_mod.ProviderCallError(retryable=True)
        return expected

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", provider_attempt)
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)

    assert _call(_config(timeout=1.0, retries=1)) is expected
    assert calls == 2
    assert sleeps == [1.0]


def test_completed_and_failed_provider_calls_release_their_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_mod, "_provider_call_slots", threading.BoundedSemaphore(1))
    provider_error = {
        "version": llm_mod._PROVIDER_PROTOCOL_VERSION,
        "status": "provider_error",
        "retryable": False,
    }
    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: _command_returning(provider_error),
    )

    with pytest.raises(llm_mod.ProviderCallError):
        llm_mod._run_provider_attempt({"model": "test"}, timeout_seconds=1.0)

    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: _command_returning(_success_envelope("recovered")),
    )
    response = llm_mod._run_provider_attempt({"model": "test"}, timeout_seconds=1.0)
    assert llm_mod.extract_text(response) == "recovered"


def test_process_start_failure_releases_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod, "_provider_call_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: ("/definitely/missing/openchronicle-provider-worker",),
    )

    with pytest.raises(llm_mod.ProviderCallUnavailableError) as raised:
        llm_mod._run_provider_attempt({"model": "test"}, timeout_seconds=1.0)
    assert "definitely" not in str(raised.value)

    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: _command_returning(_success_envelope("available")),
    )
    response = llm_mod._run_provider_attempt({"model": "test"}, timeout_seconds=1.0)
    assert llm_mod.extract_text(response) == "available"


def test_hung_provider_calls_exhaust_a_fixed_number_of_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    monkeypatch.setattr(llm_mod, "_OUTER_TIMEOUT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(llm_mod, "_PROVIDER_TERMINATE_GRACE_SECONDS", 0.03)
    monkeypatch.setattr(llm_mod, "_provider_call_slots", threading.BoundedSemaphore(2))
    monkeypatch.setattr(llm_mod, "_provider_worker_command", _hanging_worker_command)

    real_popen = subprocess.Popen
    process_lock = threading.Lock()
    two_started = threading.Event()
    processes: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        with process_lock:
            processes.append(process)
            if len(processes) == 2:
                two_started.set()
        return process

    monkeypatch.setattr(llm_mod.subprocess, "Popen", recording_popen)
    errors: list[BaseException] = []

    def run_hung_attempt() -> None:
        try:
            llm_mod._run_provider_attempt({"model": "test"}, timeout_seconds=0.5)
        except BaseException as exc:  # recorded and asserted in the parent thread
            errors.append(exc)

    workers = [threading.Thread(target=run_hung_attempt) for _ in range(2)]
    for worker in workers:
        worker.start()
    assert two_started.wait(timeout=1.0)

    started = time.monotonic()
    with pytest.raises(llm_mod.ProviderCallCapacityError) as raised:
        _call(_config(timeout=1.0, retries=llm_mod.MAX_NUM_RETRIES))
    assert time.monotonic() - started < 0.2
    assert len(processes) == 2
    assert str(raised.value) == "Model provider capacity is exhausted."
    assert "secret-provider-payload" not in str(raised.value)

    for worker in workers:
        worker.join(timeout=2.0)
        assert not worker.is_alive()
    assert len(errors) == 2
    assert all(isinstance(exc, llm_mod.ProviderCallTimeoutError) for exc in errors)
    assert all(process.poll() is not None for process in processes)

    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: _command_returning(_success_envelope("available")),
    )
    response = llm_mod._run_provider_attempt({"model": "test"}, timeout_seconds=1.0)
    assert llm_mod.extract_text(response) == "available"


def test_daemon_cancel_reaps_hung_provider_and_closes_generation_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    monkeypatch.setattr(llm_mod, "_PROVIDER_TERMINATE_GRACE_SECONDS", 0.03)
    code = (
        "import signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "sys.stdin.buffer.read(); time.sleep(60)"
    )
    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: (sys.executable, "-c", code),
    )

    real_popen = subprocess.Popen
    provider_started = threading.Event()
    processes: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        provider_started.set()
        return process

    monkeypatch.setattr(llm_mod.subprocess, "Popen", recording_popen)
    errors: list[BaseException] = []
    runtime = llm_mod.begin_daemon_provider_runtime()

    def invoke() -> None:
        try:
            _call(_config(timeout=60.0, retries=llm_mod.MAX_NUM_RETRIES))
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=invoke, name="daemon-provider-caller")
    caller.start()
    try:
        assert provider_started.wait(timeout=2.0)
        started = time.monotonic()
        llm_mod.cancel_daemon_provider_runtime(runtime)
        llm_mod.drain_daemon_provider_runtime(runtime)
        caller.join(timeout=2.0)

        assert time.monotonic() - started < 1.0
        assert not caller.is_alive()
        assert len(processes) == 1
        assert processes[0].poll() is not None
        assert len(errors) == 1
        assert isinstance(errors[0], llm_mod.ProviderCallCancelledError)

        with pytest.raises(llm_mod.ProviderCallCancelledError):
            _call(_config(timeout=60.0, retries=llm_mod.MAX_NUM_RETRIES))
        assert len(processes) == 1
    finally:
        llm_mod.finish_daemon_provider_runtime(runtime)
        caller.join(timeout=2.0)


def test_daemon_cancel_keeps_term_compliant_leader_unreaped_until_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    monkeypatch.setattr(llm_mod, "_PROVIDER_TERMINATE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(llm_mod, "_provider_worker_command", _hanging_worker_command)

    real_popen = subprocess.Popen
    provider_started = threading.Event()
    processes: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        provider_started.set()
        return process

    real_signal_group = llm_mod._signal_provider_process_group
    signals: list[signal.Signals] = []

    def recording_signal_group(process, signum):
        # ``returncode`` is populated only by a Popen wait/poll/communicate.
        # It must remain unset through the second group signal, proving that
        # the TERM-compliant leader still reserves this numeric PGID.
        if signum == signal.SIGKILL:
            assert process.returncode is None
        signals.append(signum)
        real_signal_group(process, signum)

    monkeypatch.setattr(llm_mod.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(
        llm_mod,
        "_signal_provider_process_group",
        recording_signal_group,
    )
    errors: list[BaseException] = []
    runtime = llm_mod.begin_daemon_provider_runtime()

    def invoke() -> None:
        try:
            _call(_config(timeout=60.0, retries=0))
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=invoke, name="term-compliant-provider")
    caller.start()
    try:
        assert provider_started.wait(timeout=2.0)
        llm_mod.cancel_daemon_provider_runtime(runtime)
        llm_mod.drain_daemon_provider_runtime(runtime)
        caller.join(timeout=2.0)

        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert not caller.is_alive()
        assert len(processes) == 1
        assert processes[0].poll() is not None
        assert len(errors) == 1
        assert isinstance(errors[0], llm_mod.ProviderCallCancelledError)
    finally:
        llm_mod.finish_daemon_provider_runtime(runtime)
        caller.join(timeout=2.0)


def test_finished_generation_does_not_capture_cli_or_replacement_daemon_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCHRONICLE_LLM_MOCK", raising=False)
    expected = object()
    attempts = 0

    def provider_attempt(_kwargs, *, timeout_seconds):  # noqa: ARG001
        nonlocal attempts
        attempts += 1
        return expected

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", provider_attempt)
    first = llm_mod.begin_daemon_provider_runtime()
    token = llm_mod.bind_daemon_provider_runtime(first)
    stale_context = contextvars.copy_context()
    llm_mod.reset_daemon_provider_runtime(token)
    llm_mod.finish_daemon_provider_runtime(first)

    # A standalone CLI call has no daemon generation and remains supported.
    assert _call(_config(retries=0)) is expected

    second = llm_mod.begin_daemon_provider_runtime()
    try:
        assert second.generation > first.generation
        # Work copied from the old asyncio context must fail closed rather
        # than silently attaching to the replacement daemon generation.
        with pytest.raises(llm_mod.ProviderCallCancelledError):
            stale_context.run(_call, _config(retries=0))
        assert _call(_config(retries=0)) is expected
        assert attempts == 2
    finally:
        llm_mod.finish_daemon_provider_runtime(second)


def test_timed_out_provider_child_is_reaped_before_parent_process_exit() -> None:
    script = textwrap.dedent(
        """
        import sys
        from openchronicle.writer import llm

        llm._OUTER_TIMEOUT_GRACE_SECONDS = 0.0
        llm._PROVIDER_TERMINATE_GRACE_SECONDS = 0.03
        llm._provider_worker_command = lambda: (
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.buffer.read(); time.sleep(60)",
        )
        try:
            llm._run_provider_attempt(
                {"model": "test"},
                timeout_seconds=0.05,
            )
        except llm.ProviderCallTimeoutError:
            pass
        else:
            raise SystemExit(2)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_provider_worker_envelope_never_contains_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    secret = "SECRET_PROVIDER_MESSAGE sk-live-secret"

    class SecretTimeout(Exception):
        pass

    def completion(**_kwargs):
        raise SecretTimeout(secret)

    monkeypatch.setattr(litellm, "Timeout", SecretTimeout)
    monkeypatch.setattr(litellm, "completion", completion)
    request = llm_mod._encode_provider_request(
        {
            "model": "test/model",
            "api_key": "sk-request-secret",
            "messages": [{"role": "user", "content": "prompt secret"}],
        }
    )

    envelope = llm_mod._provider_worker_envelope(request)

    assert envelope == {
        "version": llm_mod._PROVIDER_PROTOCOL_VERSION,
        "status": "provider_error",
        "retryable": True,
    }
    serialized = json.dumps(envelope)
    assert secret not in serialized
    assert "sk-request-secret" not in serialized
    assert "prompt secret" not in serialized


def test_provider_worker_success_round_trips_model_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    expected = litellm.ModelResponse(**_success_envelope("round trip")["response"])
    monkeypatch.setattr(litellm, "completion", lambda **_kwargs: expected)
    request = llm_mod._encode_provider_request({"model": "test/model"})

    envelope = llm_mod._provider_worker_envelope(request)
    payload = llm_mod._encode_provider_envelope(envelope)
    response = llm_mod._decode_provider_response(payload)

    assert llm_mod.extract_text(response) == "round trip"


def test_fresh_provider_subprocess_round_trips_litellm_response() -> None:
    response = llm_mod._run_provider_attempt(
        {
            "model": "openai/test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "mock_response": "isolated process response",
            "timeout": 5.0,
            "num_retries": 0,
        },
        timeout_seconds=5.0,
    )

    assert llm_mod.extract_text(response) == "isolated process response"


def test_provider_command_contains_no_request_or_api_key() -> None:
    secret = "sk-command-line-secret"
    request = llm_mod._encode_provider_request(
        {"model": "test/model", "api_key": secret}
    )

    assert secret.encode() in request
    assert secret not in " ".join(llm_mod._provider_worker_command())


def test_provider_protocol_rejects_oversized_frames_without_echoing_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SECRET_FRAME_CONTENT"
    monkeypatch.setattr(llm_mod, "_MAX_PROVIDER_REQUEST_BYTES", 32)
    with pytest.raises(llm_mod.ProviderCallUnavailableError) as request_error:
        llm_mod._encode_provider_request({"messages": [secret * 4]})
    assert secret not in str(request_error.value)

    monkeypatch.setattr(llm_mod, "_MAX_PROVIDER_RESPONSE_BYTES", 32)
    with pytest.raises(llm_mod.ProviderCallUnavailableError) as response_error:
        llm_mod._decode_provider_response((secret * 4).encode())
    assert secret not in str(response_error.value)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"version":1',
        b'{"version":1,"status":"ok"}',
        b'{"version":1,"status":"provider_error","retryable":"yes"}',
        b'{"version":1,"status":"worker_error","secret":"do-not-echo"}',
    ],
)
def test_provider_protocol_rejects_malformed_or_truncated_response_frames(
    payload: bytes,
) -> None:
    with pytest.raises(llm_mod.ProviderCallUnavailableError) as raised:
        llm_mod._decode_provider_response(payload)

    assert str(raised.value) in {
        "The model provider worker returned an invalid response.",
        "The model provider worker failed.",
    }
    assert "do-not-echo" not in str(raised.value)


def test_truncated_subprocess_frame_fails_closed_and_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_mod, "_provider_call_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: _command_returning_raw('{"version":1,"status":"ok"'),
    )

    with pytest.raises(llm_mod.ProviderCallUnavailableError) as raised:
        llm_mod._run_provider_attempt({"model": "test"}, timeout_seconds=1.0)
    assert str(raised.value) == (
        "The model provider worker returned an invalid response."
    )

    monkeypatch.setattr(
        llm_mod,
        "_provider_worker_command",
        lambda: _command_returning(_success_envelope("after truncated frame")),
    )
    response = llm_mod._run_provider_attempt({"model": "test"}, timeout_seconds=1.0)
    assert llm_mod.extract_text(response) == "after truncated frame"


def test_malformed_request_frame_fails_closed() -> None:
    assert llm_mod._provider_worker_envelope(b'{"version":1') == {
        "version": llm_mod._PROVIDER_PROTOCOL_VERSION,
        "status": "worker_error",
    }


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

    def unexpected_call(*_args, **_kwargs):
        pytest.fail("invalid limits must fail before the provider call")

    monkeypatch.setattr(llm_mod, "_run_provider_attempt", unexpected_call)

    with pytest.raises(ValueError, match=message):
        _call(_config(timeout=timeout, retries=retries))
