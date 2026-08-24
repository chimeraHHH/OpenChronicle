from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

from openchronicle import config, daemon, paths
from openchronicle.capture import store_lock
from openchronicle.store import files, fts


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_sensitive_directories_are_tightened_to_0700(ac_root: Path) -> None:
    paths.capture_buffer_dir().chmod(0o755)
    paths.memory_dir().chmod(0o755)

    paths.ensure_dirs()

    for directory in (
        paths.root(),
        paths.capture_buffer_dir(),
        paths.memory_dir(),
        paths.logs_dir(),
    ):
        assert _mode(directory) == 0o700


def test_config_and_sqlite_files_are_private(ac_root: Path) -> None:
    assert config.write_default_if_missing() is True
    assert _mode(paths.config_file()) == 0o600

    conn = fts.connect()
    try:
        conn.execute("INSERT OR REPLACE INTO files(path) VALUES ('private.md')")
        assert _mode(paths.index_db()) == 0o600
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{paths.index_db()}{suffix}")
            if sidecar.exists():
                assert _mode(sidecar) == 0o600
    finally:
        conn.close()


def test_custom_storage_does_not_chmod_existing_parent(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)

    custom_config = shared / "config.toml"
    custom_db = shared / "index.db"
    assert config.write_default_if_missing(custom_config) is True
    conn = fts.connect(custom_db)
    conn.close()

    assert _mode(shared) == 0o755
    assert _mode(custom_config) == 0o600
    assert _mode(custom_db) == 0o600


def test_memory_file_lock_is_private_and_cross_process(ac_root: Path) -> None:
    target = paths.memory_dir() / "event-2026-08-07.md"
    sidecar = target.parent / f".{target.name}.lock"
    script = """
import fcntl
import os
import sys

fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(0)
raise SystemExit(1)
"""

    with files.file_lock(target):
        result = subprocess.run(
            [sys.executable, "-c", script, str(sidecar)],
            check=False,
        )

    assert result.returncode == 0
    assert _mode(sidecar) == 0o600


def test_capture_store_lock_is_fixed_private_and_cross_process(ac_root: Path) -> None:
    sidecar = paths.root() / ".capture-store.lock"
    script = """
import fcntl
import os
import sys

fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(0)
raise SystemExit(1)
"""

    with store_lock.capture_store_lock():
        result = subprocess.run(
            [sys.executable, "-c", script, str(sidecar)],
            check=False,
        )

    assert result.returncode == 0
    assert _mode(sidecar) == 0o600


def test_store_write_lock_is_private_and_cross_process(ac_root: Path) -> None:
    sidecar = paths.root() / ".store-write.lock"
    script = """
import fcntl
import os
import sys

fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(0)
raise SystemExit(1)
"""

    with files.store_write_lock():
        result = subprocess.run(
            [sys.executable, "-c", script, str(sidecar)],
            check=False,
        )

    assert result.returncode == 0
    assert _mode(sidecar) == 0o600


def test_daemon_instance_lock_is_private(ac_root: Path) -> None:
    fd = daemon._acquire_daemon_lock()
    try:
        assert _mode(paths.daemon_lock_file()) == 0o600
    finally:
        daemon._release_daemon_lock(fd)
