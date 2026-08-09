"""Owned-process rendering with the pinned development PDF engine."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

COMMAND_OUTPUT_LIMIT = 1024 * 1024


class PinnedPdfProcessError(RuntimeError):
    """The pinned browser failed or could not be completely drained."""


def print_pdf(chrome: Path, html_path: Path, pdf_path: Path, *, timeout: int) -> str:
    """Print local inert HTML and drain every process in the owned browser group."""

    with tempfile.TemporaryDirectory(prefix="openchronicle-pdf-chrome-") as profile_dir:
        command = [
            str(chrome),
            "--headless",
            "--disable-background-networking",
            "--disable-breakpad",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-default-browser-check",
            "--no-first-run",
            "--no-pings",
            "--no-pdf-header-footer",
            "--host-resolver-rules=MAP * 0.0.0.0",
            f"--user-data-dir={profile_dir}",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ]
        disposition = run_browser(command, pdf_path=pdf_path, timeout=timeout)
    if not pdf_path.is_file() or pdf_path.is_symlink():
        raise PinnedPdfProcessError("Chrome did not create a regular PDF")
    return disposition


def run_browser(command: Sequence[str], *, pdf_path: Path, timeout: int) -> str:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            stdout, stderr = process.communicate(timeout=0.1)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            if len(stdout) > COMMAND_OUTPUT_LIMIT or len(stderr) > COMMAND_OUTPUT_LIMIT:
                drain_owned_process_group(process)
                raise PinnedPdfProcessError("Chrome emitted oversized diagnostic output") from None
            output_ready = (
                b"bytes written to file" in stderr
                and pdf_path.is_file()
                and not pdf_path.is_symlink()
                and has_pdf_magic(pdf_path)
            )
            if output_ready:
                drain_owned_process_group(process)
                return "terminated_after_output_ready"
            continue
        if len(stdout) > COMMAND_OUTPUT_LIMIT or len(stderr) > COMMAND_OUTPUT_LIMIT:
            drain_owned_process_group(process)
            raise PinnedPdfProcessError("Chrome emitted oversized diagnostic output")
        if process.returncode != 0:
            drain_owned_process_group(process)
            raise PinnedPdfProcessError(f"Chrome render failed with exit code {process.returncode}")
        if pdf_path.is_file() and not pdf_path.is_symlink() and has_pdf_magic(pdf_path):
            # A successful leader may have spawned a stdio-closed descendant.
            # Drain the PGID before returning so it cannot escape this export.
            drain_owned_process_group(process)
            return "exited_after_output_ready"
        drain_owned_process_group(process)
        raise PinnedPdfProcessError("Chrome exited without a complete PDF")
    drain_owned_process_group(process)
    raise PinnedPdfProcessError("Chrome render timed out")


def drain_owned_process_group(process: subprocess.Popen[bytes]) -> None:
    """TERM/KILL residual PGID members even when the direct leader already exited."""

    pgid = process.pid
    members = owned_process_group_members(pgid)
    if members:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            for pid in members:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
        time.sleep(0.2)
    for _attempt in range(3):
        members = owned_process_group_members(pgid)
        if not members:
            break
        for pid in members:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        time.sleep(0.05)
    if owned_process_group_members(pgid):
        raise PinnedPdfProcessError("Chrome process group could not be drained")
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - kernel/process failure
        raise PinnedPdfProcessError("Chrome process group could not be reaped") from exc


def owned_process_group_members(pgid: int) -> list[int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,stat=,uid="],
        check=False,
        capture_output=True,
        timeout=5,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode != 0 or len(result.stdout) > COMMAND_OUTPUT_LIMIT:
        raise PinnedPdfProcessError("could not inspect Chrome process group")
    members = []
    for raw in result.stdout.decode("ascii", errors="strict").splitlines():
        parts = raw.split()
        if len(parts) != 4:
            continue
        try:
            pid, observed_pgid, uid = int(parts[0]), int(parts[1]), int(parts[3])
        except ValueError as exc:
            raise PinnedPdfProcessError("process table contains an invalid row") from exc
        if observed_pgid != pgid or parts[2].startswith("Z"):
            continue
        if uid != os.getuid():
            raise PinnedPdfProcessError("Chrome process group contains a foreign process")
        members.append(pid)
    return members


def has_pdf_magic(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False
