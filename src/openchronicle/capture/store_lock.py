"""Cross-process serialization for capture JSON and its FTS projection."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from .. import paths
from ..store import files as store_files


@contextmanager
def capture_store_lock() -> Iterator[None]:
    """Lock all mutations spanning ``capture-buffer`` and ``captures``.

    The target is deliberately fixed rather than tied to an individual capture:
    rebuild and cleanup operate on the collection as a whole and therefore must
    serialize with every writer, including writers in another process.
    """
    with store_files.file_lock(paths.root() / "capture-store"):
        yield
