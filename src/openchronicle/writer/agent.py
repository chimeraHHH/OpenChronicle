"""CLI entry point: reduce any pending sessions and classify their entries.

The v2 writer is driven by session boundaries. `SessionManager.on_session_end`
spawns the reducer asynchronously (see ``session/tick.py``), and the
reducer's success callback kicks the classifier. This module exists for the
manual ``openchronicle writer run`` path — it catches up on any
``ended``/``failed`` sessions whose async work didn't finish (e.g. daemon
crashed mid-reduce) and runs the classifier against each.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..logger import get
from . import classifier_delivery, session_reducer

logger = get("openchronicle.writer")


@dataclass
class WriterRunResult:
    reduced: int = 0
    classified: int = 0
    written_ids: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)


def run(cfg: Config) -> WriterRunResult:
    """Reduce pending sessions, then drain durable classifier deliveries."""
    result = WriterRunResult()
    if not cfg.reducer.enabled:
        logger.info("writer run: reducer disabled, nothing to do")
        return result

    reduce_results = session_reducer.reduce_all_pending(cfg)
    for rr in reduce_results:
        if not rr.succeeded:
            continue
        result.reduced += 1

    for delivery in classifier_delivery.run_recovery_pass(cfg):
        if delivery.status != "succeeded":
            continue
        payload = delivery.result or {}
        result.classified += 1
        written_ids = payload.get("written_ids", [])
        candidate_ids = payload.get("candidate_ids", [])
        if isinstance(written_ids, list):
            result.written_ids.extend(str(value) for value in written_ids)
        if isinstance(candidate_ids, list):
            result.candidate_ids.extend(str(value) for value in candidate_ids)
        summary = payload.get("summary")
        if isinstance(summary, str) and summary:
            result.summaries.append(summary)
    return result
