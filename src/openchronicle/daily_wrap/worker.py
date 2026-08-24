"""Supervised post-midnight Daily Wrap scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import Config
from ..logger import get
from ..services.memory import MemoryService
from ..store import fts
from ..writer import llm as llm_mod
from . import store
from .service import DailyWrapService

logger = get("openchronicle.daily_wrap")


def local_timezone_name(cfg: Config) -> str:
    configured = cfg.daily_wrap.timezone.strip() or os.environ.get("TZ", "").strip()
    if configured:
        try:
            ZoneInfo(configured)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown Daily Wrap timezone: {configured}") from exc
        return configured
    tzinfo = datetime.now().astimezone().tzinfo
    key = getattr(tzinfo, "key", "")
    if key:
        return str(key)
    with contextlib.suppress(OSError):
        resolved = Path("/etc/localtime").resolve()
        marker = "zoneinfo/"
        if marker in str(resolved):
            return str(resolved).split(marker, 1)[1]
    raise ValueError("cannot infer an IANA timezone for Daily Wrap")


def validate_config(cfg: Config) -> None:
    if not 0 <= cfg.daily_wrap.hour <= 23:
        raise ValueError("daily_wrap.hour must be in [0, 23]")
    if not 0 <= cfg.daily_wrap.minute <= 59:
        raise ValueError("daily_wrap.minute must be in [0, 59]")
    if not 30 <= cfg.daily_wrap.retry_seconds <= 3600:
        raise ValueError("daily_wrap.retry_seconds must be in [30, 3600]")
    if not 0 <= cfg.daily_wrap.late_data_grace_hours <= 24:
        raise ValueError("daily_wrap.late_data_grace_hours must be in [0, 24]")
    if not 30 <= cfg.daily_wrap.lease_seconds <= 21_600:
        raise ValueError("daily_wrap.lease_seconds must be in [30, 21600]")
    local_timezone_name(cfg)


def run_for_day(
    cfg: Config,
    local_day: date,
    timezone: str | None = None,
    *,
    lease_token: str | None = None,
    cancelled: threading.Event | None = None,
    claim_guard: contextlib.AbstractContextManager[object] | None = None,
) -> str:
    timezone = timezone or local_timezone_name(cfg)
    with fts.cursor() as conn:
        memory = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens)
        memory.resume_pending_purges()
        row = DailyWrapService(conn, cfg).run(
            local_day,
            timezone,
            lease_token=lease_token,
            cancelled=cancelled.is_set if cancelled is not None else None,
            claim_guard=claim_guard,
        )
    return row.id


def run_for_previous_day(cfg: Config) -> str:
    timezone = local_timezone_name(cfg)
    local_day = datetime.now(ZoneInfo(timezone)).date() - timedelta(days=1)
    return run_for_day(cfg, local_day, timezone)


def _scheduled_today(cfg: Config, now: datetime) -> datetime:
    return now.replace(
        hour=cfg.daily_wrap.hour,
        minute=cfg.daily_wrap.minute,
        second=0,
        microsecond=0,
    )


def _startup_catchup_day(
    cfg: Config, timezone: str, *, now: datetime | None = None
) -> date | None:
    zone = ZoneInfo(timezone)
    now = now.astimezone(zone) if now is not None else datetime.now(zone)
    if now >= _scheduled_today(cfg, now):
        return now.date() - timedelta(days=1)
    return None


def _seconds_until_next(
    cfg: Config, timezone: str, *, now: datetime | None = None
) -> float:
    zone = ZoneInfo(timezone)
    now = now.astimezone(zone) if now is not None else datetime.now(zone)
    target = _scheduled_today(cfg, now)
    if target <= now:
        target = _scheduled_today(cfg, now + timedelta(days=1))
    # Same-zone wall-time subtraction ignores DST offset transitions. Compare
    # absolute instants so spring/fall days sleep for 23/25 hours as required.
    return max(
        1.0,
        (target.astimezone(UTC) - now.astimezone(UTC)).total_seconds(),
    )


async def _monitor_day(cfg: Config, timezone: str, local_day: date) -> None:
    zone = ZoneInfo(timezone)
    scheduled = datetime.combine(
        local_day + timedelta(days=1),
        datetime.min.time(),
        zone,
    ).replace(hour=cfg.daily_wrap.hour, minute=cfg.daily_wrap.minute)
    grace_end = scheduled + timedelta(hours=cfg.daily_wrap.late_data_grace_hours)
    while True:
        try:
            wrap_id = await _run_for_day_async(cfg, local_day, timezone)
            logger.info("daily wrap ready: %s", wrap_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("daily wrap generation failed: %s", type(exc).__name__)
            now = datetime.now(zone)
            if now >= grace_end:
                return
            await asyncio.sleep(
                min(
                    float(cfg.daily_wrap.retry_seconds),
                    max(
                        1.0,
                        (
                            grace_end.astimezone(UTC) - now.astimezone(UTC)
                        ).total_seconds(),
                    ),
                )
            )
            continue

        now = datetime.now(zone)
        if now >= grace_end:
            return
        await asyncio.sleep(
            min(
                float(cfg.daily_wrap.retry_seconds),
                max(1.0, (grace_end.astimezone(UTC) - now.astimezone(UTC)).total_seconds()),
            )
        )


async def _run_for_day_async(cfg: Config, local_day: date, timezone: str) -> str:
    """Run a synchronous provider call on a cancellable one-shot thread.

    This avoids pinning Python's default executor. When hosted by the daemon,
    the provider lifecycle registers and joins the concrete thread before the
    singleton lease is released. Direct scheduler use remains non-joining on
    task cancellation; its claim is still revoked before cancellation returns.
    """
    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[str] = loop.create_future()
    cancelled = threading.Event()
    claim_guard = threading.Lock()
    lease_token = uuid.uuid4().hex

    def settle(result: str | None, error: BaseException | None) -> None:
        if result_future.done():
            return
        if error is not None:
            result_future.set_exception(error)
        else:
            assert result is not None
            result_future.set_result(result)

    def invoke() -> None:
        result: str | None = None
        error: BaseException | None = None
        try:
            result = run_for_day(
                cfg,
                local_day,
                timezone,
                lease_token=lease_token,
                cancelled=cancelled,
                claim_guard=claim_guard,
            )
        except BaseException as exc:  # propagate into the supervising task
            error = exc
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(settle, result, error)

    thread = threading.Thread(
        target=invoke,
        name=f"openchronicle-daily-wrap-{local_day.isoformat()}",
        daemon=True,
    )
    thread.start()
    # Registration is deliberately adjacent to ``start`` with no await in
    # between. Shutdown can then join a worker even if it has not reached its
    # first model call yet.
    llm_mod.track_daemon_provider_worker(thread)
    try:
        return await result_future
    except asyncio.CancelledError:
        cancelled.set()
        wrap_id = store.make_id(local_day.isoformat(), timezone, "default")
        with claim_guard, contextlib.suppress(Exception), fts.cursor() as conn:
            store.cancel_claim(conn, wrap_id=wrap_id, lease_token=lease_token)
        raise


async def run_forever(cfg: Config) -> None:
    validate_config(cfg)
    timezone = local_timezone_name(cfg)
    logger.info(
        "daily wrap loop started (fires at %02d:%02d %s)",
        cfg.daily_wrap.hour,
        cfg.daily_wrap.minute,
        timezone,
    )
    with fts.cursor() as conn:
        MemoryService(
            conn, soft_limit_tokens=cfg.writer.soft_limit_tokens
        ).resume_pending_purges()

    # A day's late-data grace can extend to the next day's scheduled instant.
    # Supervise each date independently so a 24-hour monitor cannot make the
    # single scheduler loop skip the intervening calendar day.
    async with asyncio.TaskGroup() as monitors:
        catchup = _startup_catchup_day(cfg, timezone)
        if catchup is not None:
            monitors.create_task(
                _monitor_day(cfg, timezone, catchup),
                name=f"daily-wrap-{catchup.isoformat()}",
            )

        while True:
            await asyncio.sleep(_seconds_until_next(cfg, timezone))
            zone = ZoneInfo(timezone)
            local_day = datetime.now(zone).date() - timedelta(days=1)
            monitors.create_task(
                _monitor_day(cfg, timezone, local_day),
                name=f"daily-wrap-{local_day.isoformat()}",
            )
