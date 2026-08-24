from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_WORKER = [sys.executable, "-m", "openchronicle.testing.runtime_worker"]


def _env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCHRONICLE_ROOT"] = str(root)
    return env


def _run(root: Path, mode: str, *extra: str) -> dict:
    completed = subprocess.run(
        [*_WORKER, mode, *extra],
        env=_env(root),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads([line for line in completed.stdout.splitlines() if line.strip()][-1])


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path.name}")


def test_real_process_singleton_sigkill_and_repeated_cycles(tmp_path: Path) -> None:
    root = tmp_path / "daemon-root"
    root.mkdir()
    ready = root / "holder.ready"
    holder = subprocess.Popen(
        [*_WORKER, "daemon-lock-hold", "--ready", str(ready)],
        env=_env(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_for(ready)
        assert _run(root, "daemon-lock-try") == {"acquired": False}
    finally:
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait(timeout=5)

    assert holder.returncode == -signal.SIGKILL
    # SIGKILL leaves the PID file behind, but the kernel releases flock. The
    # stale PID must not block a replacement or be trusted as a signal target.
    assert _run(root, "pid-probe") == {"pid": None}
    assert _run(root, "daemon-lock-try") == {"acquired": True}

    cycles = _run(root, "daemon-lock-cycles", "--cycles", "50")
    assert cycles == {"cycles": 50, "pid_file_exists": False}


def test_stale_pid_file_never_targets_an_unrelated_live_process(tmp_path: Path) -> None:
    root = tmp_path / "pid-reuse-root"
    root.mkdir()
    canary = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        (root / ".pid").write_text(f"{canary.pid}\n", encoding="ascii")
        (root / ".daemon.lock").write_text(f"{canary.pid}\n", encoding="ascii")

        assert _run(root, "pid-probe") == {"pid": None}
        assert canary.poll() is None
    finally:
        os.killpg(canary.pid, signal.SIGKILL)
        canary.wait(timeout=5)


def test_real_daemon_repeated_fresh_process_sigterm_cycles(tmp_path: Path) -> None:
    root = tmp_path / "daemon-restart-root"
    root.mkdir()

    for cycle in range(50):
        ready = root / f"cycle-{cycle:02d}.ready"
        process = subprocess.Popen(
            [*_WORKER, "daemon-run-stub", "--ready", str(ready)],
            env=_env(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            _wait_for(ready)
            assert int(ready.read_text(encoding="ascii")) == process.pid
            assert int((root / ".pid").read_text(encoding="ascii")) == process.pid
            assert _run(root, "daemon-lock-try") == {"acquired": False}

            process.send_signal(signal.SIGTERM)
            _stdout, stderr = process.communicate(timeout=8)
            assert process.returncode == 0, stderr
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

        assert not (root / ".pid").exists()
        assert _run(root, "daemon-lock-try") == {"acquired": True}
