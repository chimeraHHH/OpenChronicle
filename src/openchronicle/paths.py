"""Single source of truth for on-disk locations under ~/.openchronicle/."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


def root() -> Path:
    override = os.environ.get("OPENCHRONICLE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".openchronicle"


def memory_dir() -> Path:
    return root() / "memory"


def capture_buffer_dir() -> Path:
    return root() / "capture-buffer"


def logs_dir() -> Path:
    return root() / "logs"


def config_file() -> Path:
    return root() / "config.toml"


def index_db() -> Path:
    return root() / "index.db"


def pid_file() -> Path:
    return root() / ".pid"


def daemon_lock_file() -> Path:
    return root() / ".daemon.lock"


def daemon_control_metadata_file() -> Path:
    """Atomic public descriptor for the active daemon control generation."""
    return root() / ".daemon-control.json"


def daemon_control_dir() -> Path:
    """Return a short, root-scoped runtime directory for the AF_UNIX socket.

    Darwin and Linux cap pathname UNIX sockets at roughly one hundred bytes.
    A user's configured root can be arbitrarily deep, so the socket itself
    lives beneath the short system temporary prefix.  The canonical root and
    uid are represented only by a fixed-size digest; the daemon separately
    enforces private ownership and mode before binding or connecting.
    """
    uid = os.getuid()
    canonical_root = str(root().expanduser().resolve(strict=False))
    identity = f"{uid}\0{canonical_root}".encode("utf-8", "surrogateescape")
    digest = hashlib.sha256(identity).hexdigest()[:24]
    temp_root = Path("/private/tmp") if sys.platform == "darwin" else Path("/tmp")
    return temp_root / f"openchronicle-{uid}-{digest}"


def paused_flag() -> Path:
    return root() / ".paused"


def writer_state() -> Path:
    """Tracks last-commit timestamp and processed capture files."""
    return root() / ".writer-state.json"


def ensure_dirs() -> None:
    for d in (root(), memory_dir(), capture_buffer_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Sensitive AX text, screenshots, logs, and memory all live beneath
        # these directories. Tighten pre-existing installs as well as new ones.
        d.chmod(0o700)
