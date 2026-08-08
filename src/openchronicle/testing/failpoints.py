"""Deterministic process-death failpoints for the Stage 0 runtime audit.

Failpoints are dormant unless every guard below is present. They are intended
for a dedicated child process rooted in a disposable temporary directory, not
for ordinary unit tests or a user's OpenChronicle store.
"""

from __future__ import annotations

import contextlib
import os
import signal
import tempfile
from pathlib import Path

FAILPOINT_ENV = "OPENCHRONICLE_TEST_FAILPOINT"
ACTION_ENV = "OPENCHRONICLE_TEST_FAILPOINT_ACTION"
TOKEN_ENV = "OPENCHRONICLE_TEST_AUTH_TOKEN"
ACTION_SIGKILL_V1 = "sigkill-v1"
AUTHORIZATION_FILE = ".runtime-audit-authorization"
HIT_DIRECTORY = ".runtime-audit-failpoints"

KNOWN_FAILPOINTS = frozenset(
    {
        "capture.json.before_rename",
        "capture.json.after_rename",
        "capture.fts.before_write",
        "capture.fts.after_write",
        "timeline.block.before_commit",
        "timeline.block.after_commit",
        "timeline.watermark.before_write",
        "timeline.watermark.after_write",
        "memory.markdown.before_rename",
        "memory.markdown.after_rename",
        "memory.fts.before_write",
        "memory.fts.after_write",
        "session.status.before_ended",
        "session.status.after_ended",
        "session.status.before_reduced",
        "session.status.after_reduced",
        "session.status.before_failed",
        "session.status.after_failed",
        "session.status.before_flush",
        "session.status.after_flush",
    }
)


class FailpointRefused(RuntimeError):
    """Raised when test instrumentation is requested outside its safe envelope."""


def hit(name: str) -> None:
    """Kill this process at ``name`` when a fully authorized audit arms it."""

    armed = os.environ.get(FAILPOINT_ENV)
    if armed is None:
        return
    if name not in KNOWN_FAILPOINTS:
        raise ValueError(f"unknown runtime failpoint: {name}")
    if armed != name:
        return

    root, _token = _authorized_root()
    marker_dir = root / HIT_DIRECTORY
    marker_dir.mkdir(mode=0o700, exist_ok=True)
    marker_dir.chmod(0o700)
    marker = marker_dir / f"{name}.hit"
    _write_private_marker(marker, f"{name}\n{os.getpid()}\n")

    # SIGKILL is deliberate: no Python finally block, SQLite close, lock
    # release, or temp cleanup may run. That is the state the restart audit is
    # meant to inspect. The authorization token is never copied into markers.
    os.kill(os.getpid(), signal.SIGKILL)
    raise AssertionError("SIGKILL returned unexpectedly")


def _authorized_root() -> tuple[Path, str]:
    if os.environ.get(ACTION_ENV) != ACTION_SIGKILL_V1:
        raise FailpointRefused("runtime failpoint action was not explicitly authorized")
    raw_root = os.environ.get("OPENCHRONICLE_ROOT")
    token = os.environ.get(TOKEN_ENV)
    if not raw_root or not token or len(token) < 32:
        raise FailpointRefused("runtime failpoint root/token authorization is incomplete")

    root = Path(raw_root).expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if root == temp_root or temp_root not in root.parents:
        raise FailpointRefused("runtime failpoints require a child of the system temp directory")
    if Path(raw_root).expanduser().is_symlink() or not root.is_dir():
        raise FailpointRefused("runtime failpoint root must be a real directory")

    auth = root / AUTHORIZATION_FILE
    try:
        stat = auth.lstat()
        value = auth.read_text(encoding="utf-8")
    except OSError as exc:
        raise FailpointRefused("runtime failpoint authorization file is unavailable") from exc
    if auth.is_symlink() or not auth.is_file() or stat.st_uid != os.getuid():
        raise FailpointRefused("runtime failpoint authorization file is not trusted")
    if stat.st_mode & 0o077:
        raise FailpointRefused("runtime failpoint authorization file must be private")
    if value != token:
        raise FailpointRefused("runtime failpoint authorization token does not match")
    return root, token


def _write_private_marker(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
