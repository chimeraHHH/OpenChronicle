"""Supervised durable Prompt Rescue queue worker."""

from __future__ import annotations

import asyncio

from ..config import Config
from ..logger import get
from ..store import fts
from .selection import prepare_selection_helper
from .service import PromptRescueService, validate_config

logger = get("openchronicle.prompt_rescue")


def process_once(cfg: Config) -> str | None:
    with fts.cursor() as conn:
        job = PromptRescueService(conn, cfg).process_next()
        return job.id if job is not None else None


async def run_forever(cfg: Config) -> None:
    validate_config(cfg)
    if cfg.prompt_rescue.enabled:
        await asyncio.to_thread(prepare_selection_helper)
    while True:
        try:
            job_id = await asyncio.to_thread(process_once, cfg)
            if job_id is not None:
                logger.info("prompt rescue job settled: %s", job_id)
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - supervised loop stays available
            logger.error("prompt rescue worker failed: %s", type(exc).__name__)
        await asyncio.sleep(float(cfg.prompt_rescue.poll_seconds))
