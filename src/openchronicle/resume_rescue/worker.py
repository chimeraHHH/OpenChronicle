"""Supervised durable Résumé Rescue rewrite worker."""

from __future__ import annotations

import asyncio

from ..config import Config
from ..logger import get
from ..store import fts
from .rewrite_generation import validate_rewrite_config
from .service import ResumeRescueService

logger = get("openchronicle.resume_rescue")


def process_once(cfg: Config) -> str | None:
    with fts.cursor() as conn:
        job = ResumeRescueService(conn, cfg).process_next_rewrite()
        return job.id if job is not None else None


async def run_forever(cfg: Config) -> None:
    validate_rewrite_config(cfg)
    while True:
        try:
            job_id = await asyncio.to_thread(process_once, cfg)
            if job_id is not None:
                logger.info("resume rewrite job settled: %s", job_id)
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - supervised loop stays available
            logger.error("resume rewrite worker failed: %s", type(exc).__name__)
        await asyncio.sleep(float(cfg.resume_rescue.rewrite_poll_seconds))
