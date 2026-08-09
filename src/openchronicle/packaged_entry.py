"""Closed worker dispatch for the frozen desktop bridge executable."""

from __future__ import annotations

import sys
from collections.abc import Sequence

DOCUMENT_WORKER = "document-extract"
PROVIDER_WORKER = "provider"
_WORKERS = {DOCUMENT_WORKER, PROVIDER_WORKER}


def worker_command(worker: str, source_command: Sequence[str]) -> tuple[str, ...]:
    """Select a frozen self-dispatch argv without placing request data in it."""
    if worker not in _WORKERS:
        raise ValueError("packaged worker is not allowlisted")
    if getattr(sys, "frozen", False):
        return (sys.executable, f"--openchronicle-worker={worker}")
    return tuple(source_command)


def run(argv: Sequence[str] | None = None) -> int:
    """Run the bridge or one exact internal worker mode; reject every other argv."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if not arguments:
        from .desktop_bridge import main

        main()
        return 0
    if arguments == (f"--openchronicle-worker={DOCUMENT_WORKER}",):
        from .resume_rescue.document_worker import main

        return main()
    if arguments == (f"--openchronicle-worker={PROVIDER_WORKER}",):
        from .writer.llm import _provider_worker_main

        _provider_worker_main()
        return 0
    return 2
