"""Supervised durable Reply Rescue queue worker."""

from __future__ import annotations

import asyncio

from ..config import Config
from ..logger import get
from ..store import fts
from .service import ReplyRescueService, validate_config

logger = get("openchronicle.reply_rescue")


def process_once(cfg: Config) -> str | None:
    with fts.cursor() as conn:
        job = ReplyRescueService(conn, cfg).process_next()
        return job.id if job is not None else None


async def run_forever(cfg: Config) -> None:
    validate_config(cfg)
    while True:
        try:
            job_id = await asyncio.to_thread(process_once, cfg)
            if job_id is not None:
                logger.info("reply rescue job settled: %s", job_id)
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - supervised loop stays available
            logger.error("reply rescue worker failed: %s", type(exc).__name__)
        await asyncio.sleep(float(cfg.reply_rescue.poll_seconds))
