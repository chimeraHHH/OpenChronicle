"""Small trusted control surface for capture pause state."""

from __future__ import annotations

import contextlib
import os
from datetime import datetime

from .. import paths
from ..store import files as files_store


class PauseStateConflict(RuntimeError):
    """The pause sentinel changed after the UI rendered its state."""


def set_paused(*, expected_state: bool, paused: bool) -> dict[str, bool]:
    """Atomically compare-and-set the private cross-process pause sentinel."""
    paths.ensure_dirs()
    sentinel = paths.paused_flag()
    with files_store.file_lock(sentinel):
        current = sentinel.exists()
        if current != expected_state:
            raise PauseStateConflict("capture pause state changed")
        changed = current != paused
        if paused:
            if changed:
                files_store.atomic_write_text(sentinel, datetime.now().astimezone().isoformat())
            os.chmod(sentinel, 0o600)
        elif changed:
            sentinel.unlink()
            with contextlib.suppress(OSError):
                directory_fd = os.open(sentinel.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        return {"paused": paused, "changed": changed}
