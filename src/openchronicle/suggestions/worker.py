"""Supervised local opportunity detector loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime

from ..config import Config
from ..local_time import ClockSample
from ..logger import get
from ..store import fts
from .activity import CaptureActivityGate
from .service import _validate_config
from .work_resumption import WorkResumptionService

logger = get("openchronicle.suggestions")


def scan_once(
    cfg: Config,
    *,
    now: datetime | None = None,
    activity_gate: CaptureActivityGate | None = None,
    now_tick: float | None = None,
):
    with fts.cursor() as conn:
        return WorkResumptionService(conn, cfg).scan(
            now=now,
            activity_gate=activity_gate,
            now_tick=now_tick,
        )


async def run_forever(
    cfg: Config,
    *,
    now_provider: Callable[[], datetime] | None = None,
    activity_gate: CaptureActivityGate | None = None,
) -> None:
    _validate_config(cfg)
    while True:
        try:
            now, now_tick = _clock_sample(now_provider)
            decision = scan_once(
                cfg,
                now=now,
                activity_gate=activity_gate,
                now_tick=now_tick,
            )
            if decision.emitted and decision.suggestion is not None:
                logger.info(
                    "suggestion ready: workflow=%s id=%s",
                    decision.suggestion.workflow,
                    decision.suggestion.id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - supervised loop stays available
            logger.error("suggestion scan failed: %s", type(exc).__name__)
        await asyncio.sleep(float(cfg.suggestions.scan_seconds))


def _clock_sample(
    provider: Callable[[], datetime] | None,
) -> tuple[datetime | None, float | None]:
    if provider is None:
        return None, None
    sample_provider = getattr(provider, "sample", None)
    if callable(sample_provider):
        sample = sample_provider()
        if not isinstance(sample, ClockSample):
            raise TypeError("suggestion clock sample is invalid")
        return sample.wall_time, sample.monotonic_tick
    return provider(), None
