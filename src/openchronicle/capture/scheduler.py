"""Capture scheduler: event-driven + heartbeat. Writes one JSON per tick to capture-buffer/."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import queue
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import paths
from ..config import CaptureConfig
from ..logger import get
from ..privacy import policy as privacy_policy
from ..store import fts as fts_store
from . import ax_capture, filenames, s1_parser, screenshot, store_lock, window_meta
from .event_dispatcher import EventDispatcher
from .watcher import AXWatcherProcess

logger = get("openchronicle.capture")


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="milliseconds")


def _safe_filename(ts: str) -> str:
    """Backward-compatible wrapper around the canonical filename encoder."""
    return filenames.safe_timestamp(ts)


def _build_capture(
    cfg: CaptureConfig,
    provider: ax_capture.AXProvider,
    trigger: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build an enriched capture dict in memory. Returns None if capturing is paused."""
    paths.ensure_dirs()

    if paths.paused_flag().exists():
        logger.info("capture skipped (paused)")
        return None

    # Watcher events may wait behind other work. Apply policy to the event's
    # source identity before copying any of its details into a capture, then
    # bind it to the window that is actually frontmost below.
    if trigger is not None and not _event_allowed(cfg, trigger):
        return None

    ts = _now_iso()
    out: dict[str, Any] = {
        "timestamp": ts,
        "schema_version": 3,
        "observation_id": f"obs_{uuid.uuid4().hex}",
        "trigger": trigger or {"event_type": "heartbeat"},
        # Collection can take long enough for the timeline producer to close
        # the bucket containing ``ts``.  The writer consumes this private
        # marker and assigns the authoritative timestamp while holding the
        # capture-store lock, immediately before the atomic rename.
        "_timestamp_at_persist": True,
    }

    meta = window_meta.active_window()
    out["window_meta"] = {
        "app_name": meta.app_name,
        "title": meta.title,
        "bundle_id": meta.bundle_id,
    }

    decision = privacy_policy.evaluate_window(
        cfg,
        app_name=meta.app_name,
        bundle_id=meta.bundle_id,
        window_title=meta.title,
    )
    if not decision.allowed:
        logger.info(
            "capture denied by privacy policy: reason=%s",
            decision.reason,
        )
        return None
    if trigger is not None and not _trigger_matches_window(trigger, out["window_meta"]):
        logger.info("capture dropped: queued event no longer matches active window")
        return None
    out["privacy"] = {"decision": "allowed", "policy_version": 1}

    if provider.available:
        result = provider.capture_frontmost(focused_window_only=True)
        if result is not None:
            ax_identity = _ax_identity(result.raw_json)
            if ax_identity is None:
                logger.info("capture dropped: AX result has no verifiable window identity")
                return None
            ax_decision = privacy_policy.evaluate_window(
                cfg,
                app_name=ax_identity["app_name"],
                bundle_id=ax_identity["bundle_id"],
                window_title=ax_identity["window_title"],
            )
            if not ax_decision.allowed:
                logger.info(
                    "capture denied after AX identity check: reason=%s",
                    ax_decision.reason,
                )
                return None
            if not _same_window_identity(out["window_meta"], ax_identity):
                logger.info("capture dropped: active window changed during AX collection")
                return None
            out["ax_tree"] = result.raw_json
            out["ax_metadata"] = result.metadata
    else:
        out["ax_unavailable"] = True

    if cfg.include_screenshot:
        # Screenshot capture cannot be made atomic with AX. Re-check identity
        # immediately before collecting pixels and keep screenshots opt-in.
        latest_meta = window_meta.active_window()
        latest_identity = {
            "app_name": latest_meta.app_name,
            "bundle_id": latest_meta.bundle_id,
            "window_title": latest_meta.title,
        }
        latest_decision = privacy_policy.evaluate_window(
            cfg,
            app_name=latest_meta.app_name,
            bundle_id=latest_meta.bundle_id,
            window_title=latest_meta.title,
        )
        if not latest_decision.allowed or not _same_window_identity(
            out["window_meta"], latest_identity
        ):
            logger.info("screenshot skipped: active window identity changed or was denied")
            return None
        shot = screenshot.grab(
            max_width=cfg.screenshot_max_width, jpeg_quality=cfg.screenshot_jpeg_quality
        )
        if shot is not None:
            out["screenshot"] = {
                "image_base64": shot.image_base64,
                "mime_type": shot.mime_type,
                "width": shot.width,
                "height": shot.height,
            }

    s1_parser.enrich(out)
    return out


def _event_allowed(cfg: CaptureConfig, trigger: dict[str, Any]) -> bool:
    """Apply policy to watcher metadata before an event enters the queue."""
    event_type = str(trigger.get("event_type") or "")
    if event_type in {"heartbeat", "manual"}:
        return True
    decision = privacy_policy.evaluate_window(
        cfg,
        app_name=str(trigger.get("app_name") or ""),
        bundle_id=str(trigger.get("bundle_id") or ""),
        window_title=str(trigger.get("window_title") or ""),
    )
    if not decision.allowed:
        logger.info("watcher event denied by privacy policy: reason=%s", decision.reason)
    return decision.allowed


def _trigger_matches_window(trigger: dict[str, Any], window_identity: dict[str, Any]) -> bool:
    """Bind a queued watcher event to the window captured later."""
    event_type = str(trigger.get("event_type") or "")
    if event_type in {"heartbeat", "manual"}:
        return True
    source = {
        "bundle_id": str(trigger.get("bundle_id") or ""),
        "window_title": str(trigger.get("window_title") or ""),
    }
    # A watcher event can carry user-entered details. Without two concrete
    # titles, bundle-only matching could attach those details to a different
    # window of the same app after the event waited in the queue.
    if not source["window_title"].strip():
        return False
    target_title = str(window_identity.get("window_title") or window_identity.get("title") or "")
    if not target_title.strip():
        return False
    return _same_window_identity(source, window_identity)


def _same_window_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_bundle = str(left.get("bundle_id") or "").strip().casefold()
    right_bundle = str(right.get("bundle_id") or "").strip().casefold()
    if not left_bundle or left_bundle != right_bundle:
        return False
    left_title = str(left.get("window_title") or left.get("title") or "").strip()
    right_title = str(right.get("window_title") or right.get("title") or "").strip()
    return left_title == right_title


def _ax_identity(raw_json: Any) -> dict[str, str] | None:
    """Extract the single frontmost app/window identity from helper output."""
    if not isinstance(raw_json, dict):
        return None
    apps = raw_json.get("apps")
    if not isinstance(apps, list) or len(apps) != 1 or not isinstance(apps[0], dict):
        return None
    app = apps[0]
    windows = app.get("windows")
    # The persisted payload is the whole raw JSON, so validating only one
    # focused window while retaining sibling windows would bypass title-based
    # exclusions. The helper is invoked in focused-window-only mode; enforce
    # that contract at the privacy boundary and fail closed if it is violated.
    if not isinstance(windows, list) or len(windows) != 1 or not isinstance(windows[0], dict):
        return None
    title = str(windows[0].get("title") or "")
    return {
        "app_name": str(app.get("name") or ""),
        "bundle_id": str(app.get("bundle_id") or ""),
        "window_title": title,
    }


def _write_capture(out: dict[str, Any]) -> Path:
    """Persist a built capture dict to the buffer, index it for search, and log."""
    observation_id = str(out.get("observation_id") or f"obs_{uuid.uuid4().hex}")
    if not observation_id.startswith("obs_"):
        observation_id = f"obs_{uuid.uuid4().hex}"
    out["observation_id"] = observation_id
    timestamp_at_persist = out.pop("_timestamp_at_persist", False) is True
    # JSON and its searchable projection are one logical capture-store write.
    # Serialize it with collection-wide cleanup/rebuild commands so a rebuild
    # cannot snapshot the directory between these two operations.  A timeline
    # snapshot takes this same lock; assigning the timestamp here means a
    # capture is either visible in its closed bucket or belongs to a later one.
    with store_lock.capture_store_lock():
        if timestamp_at_persist:
            out["timestamp"] = _now_iso()
        ts = str(out["timestamp"])
        path = paths.capture_buffer_dir() / (
            f"{filenames.capture_stem(ts, observation_id)}.json"
        )
        _atomic_write_json(path, out)
        _index_capture(path.stem, out)
    meta = out.get("window_meta") or {}
    logger.info(
        "capture ok: %s trigger=%s app=%r title=%r ax=%s screenshot=%s",
        path.name,
        (out.get("trigger") or {}).get("event_type"),
        meta.get("app_name"),
        (meta.get("title") or "")[:60],
        "ax_tree" in out,
        "screenshot" in out,
    )
    return path


def _atomic_write_json(
    path: Path,
    data: dict[str, Any],
    *,
    preserve_times: bool = False,
) -> None:
    """Write private capture data atomically with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_stat = None
    if preserve_times:
        with contextlib.suppress(OSError):
            previous_stat = path.stat()
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1  # ownership transferred; do not close a reused descriptor
        with handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_stat is not None:
            os.utime(
                tmp_path,
                ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns),
            )
        os.replace(tmp_path, path)
        with contextlib.suppress(OSError):
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _index_capture(file_stem: str, out: dict[str, Any]) -> None:
    """Insert/upsert the capture's S1 fields into the FTS5 index.

    Failures here are non-fatal — a missed FTS row is recoverable via
    ``openchronicle rebuild-captures-index``; killing the capture worker
    over an indexing hiccup would lose the JSON too.
    """
    meta = out.get("window_meta") or {}
    focused = out.get("focused_element") or {}
    try:
        with fts_store.cursor() as conn:
            fts_store.insert_capture(
                conn,
                id=file_stem,
                observation_id=str(out.get("observation_id") or ""),
                timestamp=out.get("timestamp", ""),
                app_name=meta.get("app_name") or "",
                bundle_id=meta.get("bundle_id") or "",
                window_title=meta.get("title") or "",
                focused_role=focused.get("role") or "",
                focused_value=focused.get("value") or "",
                visible_text=out.get("visible_text") or "",
                url=out.get("url") or "",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("captures FTS insert failed for %s: %s", file_stem, exc)


def _content_fingerprint(out: dict[str, Any]) -> str:
    """Hash the content-bearing fields of a capture for consecutive-duplicate detection.

    Excludes timestamp, trigger metadata, screenshots, and the raw ax_tree (which
    contains coordinate noise). Focuses on what actually drives downstream stages:
    the window identity + what the user can see + what they've typed.
    """
    meta = out.get("window_meta") or {}
    focused = out.get("focused_element") or {}
    payload = "\x1f".join(
        [
            meta.get("bundle_id") or "",
            meta.get("title") or "",
            focused.get("role") or "",
            focused.get("value") or "",
            out.get("visible_text") or "",
            out.get("url") or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def capture_once(
    cfg: CaptureConfig,
    provider: ax_capture.AXProvider,
    *,
    trigger: dict[str, Any] | None = None,
) -> Path | None:
    """Perform one capture and write it to the buffer. Returns the file path on success.

    ``trigger`` (optional) carries the watcher event metadata that caused this
    capture. When absent the capture is treated as a heartbeat / manual tick.

    This helper always writes — content-dedup lives in ``_CaptureRunner`` so the
    CLI ``capture-once`` smoke test still produces a fresh file on demand.
    """
    out = _build_capture(cfg, provider, trigger)
    if out is None:
        return None
    return _write_capture(out)


class _CaptureRunner:
    """Serializes capture_once calls from the watcher thread + heartbeat task.

    Captures execute on a single dedicated worker thread fed by a bounded
    queue, so the watcher reader thread never blocks on AX / screenshot I/O
    and a runaway burst of events can never spawn unbounded threads.

    Also enforces *consecutive-duplicate dedup*: if the content fingerprint
    (bundle+title+focused value+visible_text+url) matches the previously
    written capture, the new one is dropped. Time-based dedup in the
    dispatcher handles rapid-fire bursts; this handles a static screen
    (e.g. the lock screen overnight) that keeps generating identical
    captures. When deduped, the ``pre_capture_hook`` is NOT fired, so the
    session manager's idle timer isn't reset by meaningless repetition.
    """

    # Bounded queue for backpressure. Captures are de-duplicated by the
    # dispatcher upstream and again by content-fingerprint here, so a
    # backlog past this size is a sign the worker is stuck or LLM/AX
    # calls are slow — drop with a warning rather than build an
    # unbounded thread/memory backlog.
    _MAX_PENDING = 16
    _SENTINEL: Any = object()

    def __init__(
        self,
        cfg: CaptureConfig,
        provider: ax_capture.AXProvider,
        *,
        pre_capture_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._provider = provider
        self._pre_capture_hook = pre_capture_hook
        self._lock = threading.Lock()
        self._last_fingerprint: str | None = None
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self._MAX_PENDING)
        self._worker: threading.Thread | None = None

    def start_worker(self) -> None:
        """Spawn the dedicated worker thread. Idempotent."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="capture-worker",
            daemon=True,
        )
        self._worker.start()

    def stop_worker(self, *, timeout: float = 5.0) -> None:
        """Drain the queue and join the worker thread."""
        if self._worker is None:
            return
        with contextlib.suppress(queue.Full):
            self._queue.put(self._SENTINEL, timeout=1.0)
        self._worker.join(timeout=timeout)
        if self._worker.is_alive():
            logger.warning("capture worker did not exit within %.1fs", timeout)
        self._worker = None

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._SENTINEL:
                return
            self.run(item)

    def run(self, trigger: dict[str, Any] | None) -> None:
        # Serialize so two near-simultaneous triggers don't double-capture.
        with self._lock:
            try:
                out = _build_capture(self._cfg, self._provider, trigger)
                if out is None:
                    return
                fingerprint = _content_fingerprint(out)
                if fingerprint == self._last_fingerprint:
                    meta = out.get("window_meta") or {}
                    logger.debug(
                        "capture skipped (content dedup): trigger=%s app=%r title=%r",
                        (trigger or {}).get("event_type"),
                        meta.get("app_name"),
                        (meta.get("title") or "")[:60],
                    )
                    return
                _write_capture(out)
                # A failed atomic write must remain retryable. Advancing the
                # dedup bookmark before persistence would make the next
                # identical observation disappear after a transient I/O error.
                self._last_fingerprint = fingerprint
                if self._pre_capture_hook is not None:
                    hook_trigger = trigger
                    if hook_trigger is None:
                        meta = out.get("window_meta") or {}
                        # Heartbeat-only and watcher-fallback modes still need
                        # session boundaries. Build the smallest identity-only
                        # event after persistence; content dedup above prevents
                        # an unchanged screen from refreshing the idle timer.
                        hook_trigger = {
                            "event_type": "heartbeat",
                            "app_name": str(meta.get("app_name") or ""),
                            "bundle_id": str(meta.get("bundle_id") or ""),
                            "window_title": str(meta.get("title") or ""),
                            "timestamp": str(out.get("timestamp") or ""),
                        }
                    try:
                        self._pre_capture_hook(hook_trigger)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("pre_capture_hook failed: %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.error("capture failed: %s", exc, exc_info=True)

    def run_threaded(self, trigger: dict[str, Any] | None) -> None:
        """Enqueue a capture for the worker thread; drop with a warning if full."""
        try:
            self._queue.put_nowait(trigger)
        except queue.Full:
            logger.warning(
                "capture queue full (%d pending); dropping trigger=%s",
                self._queue.qsize(),
                (trigger or {}).get("event_type") if trigger else "heartbeat",
            )


async def run_forever(
    cfg: CaptureConfig,
    *,
    pre_capture_hook: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Run the capture pipeline until cancelled.

    If ``cfg.event_driven`` is true, starts the watcher subprocess and routes
    events through the dispatcher. A heartbeat timer also runs so long idle
    periods (no window changes, no typing) still get periodic snapshots.

    ``pre_capture_hook`` (optional) fires with the trigger dict for every
    capture that actually wrote new content to the buffer — duplicates
    collapsed by content-dedup do NOT fire it, so the session manager's idle
    timer isn't refreshed by a screen that isn't changing (e.g. the lock
    screen overnight).
    """
    provider = ax_capture.create_provider(depth=cfg.ax_depth, timeout=cfg.ax_timeout_seconds)
    if not provider.available:
        logger.warning("AX capture unavailable: %s", getattr(provider, "reason", "unknown reason"))

    runner = _CaptureRunner(cfg, provider, pre_capture_hook=pre_capture_hook)
    runner.start_worker()
    watcher: AXWatcherProcess | None = None
    dispatcher: EventDispatcher | None = None

    def _on_capture(trigger: dict[str, Any] | None) -> None:
        # Hook firing is deferred into the runner so content-deduped captures
        # (e.g. overnight lock-screen repeats) don't refresh the session timer.
        runner.run_threaded(trigger)

    if cfg.event_driven:
        watcher = AXWatcherProcess()
        if watcher.available:
            dispatcher = EventDispatcher(
                _on_capture,
                event_filter=lambda event: _event_allowed(cfg, event),
                debounce_seconds=cfg.debounce_seconds,
                min_capture_gap_seconds=cfg.min_capture_gap_seconds,
                dedup_interval_seconds=cfg.dedup_interval_seconds,
                same_window_dedup_seconds=cfg.same_window_dedup_seconds,
            )
            watcher.on_event(dispatcher.on_event)
            watcher.start()
            logger.info("event-driven capture started")
        else:
            logger.warning("AX watcher unavailable — falling back to heartbeat-only captures")

    # One capture immediately so the user sees something in the buffer right away.
    runner.run_threaded(None)

    try:
        if cfg.heartbeat_minutes > 0:
            heartbeat_interval = max(60.0, cfg.heartbeat_minutes * 60.0)
            logger.info(
                "heartbeat capture every %.0fs (event_driven=%s)",
                heartbeat_interval,
                cfg.event_driven,
            )
            while True:
                await asyncio.sleep(heartbeat_interval)
                try:
                    await asyncio.to_thread(runner.run, None)
                except Exception as exc:  # noqa: BLE001
                    logger.error("heartbeat capture failed: %s", exc, exc_info=True)
        else:
            logger.info(
                "heartbeat disabled (heartbeat_minutes=%d); event-driven only",
                cfg.heartbeat_minutes,
            )
            # Park until the task is cancelled so the watcher keeps streaming.
            await asyncio.Event().wait()
    finally:
        # Stop in producer→consumer order so no new work piles up after we've
        # told the worker to drain: watcher (no new events) → dispatcher
        # (cancel debounce) → runner worker (drain + join).
        if watcher is not None:
            watcher.stop()
        if dispatcher is not None:
            dispatcher.shutdown()
        runner.stop_worker()


def cleanup_buffer(
    retention_hours: int,
    processed_before_ts: str | None = None,
    *,
    screenshot_retention_hours: int | None = None,
    max_mb: int = 0,
) -> dict[str, int]:
    """Tiered buffer hygiene. Returns {deleted, stripped, evicted}.

    Three passes, all gated on ``processed_before_ts`` so an unprocessed
    trailing capture is never evicted:

    1. **Delete whole file** when mtime is older than ``retention_hours``.
    2. **Strip screenshot** when mtime is older than
       ``screenshot_retention_hours`` (if provided and smaller than
       ``retention_hours``). The screenshot field is 77% of the payload
       and nothing downstream consumes it, so stripping keeps AX+text
       queryable for much longer at ~20% of the original size.
    3. **Evict by size** once total buffer size exceeds ``max_mb`` MB.
       Oldest already-absorbed files go first. ``max_mb=0`` disables this.
    """
    absorbed_before = None
    if processed_before_ts is not None:
        absorbed_before = filenames.parse_timestamp(processed_before_ts)
        if absorbed_before is None:
            logger.error("buffer cleanup skipped: invalid processed boundary")
            return {"deleted": 0, "stripped": 0, "evicted": 0}

    with store_lock.capture_store_lock():
        return _cleanup_buffer_locked(
            retention_hours,
            absorbed_before,
            screenshot_retention_hours=screenshot_retention_hours,
            max_mb=max_mb,
        )


def _cleanup_buffer_locked(
    retention_hours: int,
    absorbed_before: datetime | None,
    *,
    screenshot_retention_hours: int | None,
    max_mb: int,
) -> dict[str, int]:
    """Implement ``cleanup_buffer`` while the global capture-store lock is held."""
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return {"deleted": 0, "stripped": 0, "evicted": 0}

    now = time.time()
    delete_cutoff = now - retention_hours * 3600
    strip_cutoff = (
        now - screenshot_retention_hours * 3600
        if screenshot_retention_hours and screenshot_retention_hours > 0
        else None
    )

    deleted = stripped = evicted = 0
    records: list[tuple[float, Path, int, bool]] = []
    delete_candidates: list[Path] = []
    surviving: list[tuple[float, Path, int]] = []  # (mtime, path, size_after_pass)

    for p in sorted(buf.iterdir()):
        if p.is_file() and filenames.is_capture_temp_name(p.name):
            # Every live capture writer holds capture_store_lock across its
            # temp lifetime. Seeing one while we own that lock proves its
            # writer died before rename, so it is safe to purge immediately.
            try:
                p.unlink()
                deleted += 1
            except OSError:
                pass
            continue
        if not p.is_file() or p.suffix != ".json":
            continue
        capture_time = filenames.parse_capture_stem(p.stem)
        is_absorbed = (
            absorbed_before is not None
            and capture_time is not None
            and capture_time < absorbed_before
        )
        try:
            st = p.stat()
        except OSError:
            continue

        records.append((st.st_mtime, p, st.st_size, is_absorbed))
        if is_absorbed and st.st_mtime <= delete_cutoff:
            delete_candidates.append(p)

    # Remove the complete batch from FTS before unlinking any authoritative
    # JSON. A database failure therefore leaves every source file intact and
    # never leaves sensitive searchable text behind without its source.
    delete_indexed = not delete_candidates or _delete_captures_from_fts(
        [path.stem for path in delete_candidates]
    )
    delete_candidate_set = set(delete_candidates) if delete_indexed else set()

    for mtime, p, size, is_absorbed in records:
        if p in delete_candidate_set:
            try:
                p.unlink()
                deleted += 1
                continue
            except OSError:
                pass

        if (
            is_absorbed
            and strip_cutoff is not None
            and mtime <= strip_cutoff
            and _strip_screenshot_inplace(p)
        ):
            stripped += 1
            try:
                stat_after_strip = p.stat()
                mtime = stat_after_strip.st_mtime
                size = stat_after_strip.st_size
            except OSError:
                continue

        surviving.append((mtime, p, size))

    if max_mb > 0:
        limit = max_mb * 1024 * 1024
        total = sum(sz for _, _, sz in surviving)
        if total > limit:
            surviving.sort()  # oldest first by mtime
            eviction_candidates: list[tuple[Path, int]] = []
            projected_total = total
            for _mtime, path, size in surviving:
                if projected_total <= limit:
                    break
                capture_time = filenames.parse_capture_stem(path.stem)
                if (
                    absorbed_before is None
                    or capture_time is None
                    or capture_time >= absorbed_before
                ):
                    continue  # don't evict un-absorbed captures
                eviction_candidates.append((path, size))
                projected_total -= size

            if eviction_candidates and _delete_captures_from_fts(
                [path.stem for path, _size in eviction_candidates]
            ):
                for path, size in eviction_candidates:
                    try:
                        path.unlink()
                        total -= size
                        evicted += 1
                    except OSError:
                        pass

    return {"deleted": deleted, "stripped": stripped, "evicted": evicted}


def _delete_captures_from_fts(stems: list[str]) -> bool:
    """Drop matching index rows, returning whether deletion succeeded."""
    if not stems:
        return True
    try:
        with fts_store.cursor() as conn:
            conn.execute("BEGIN")
            try:
                conn.executemany(
                    "DELETE FROM captures WHERE id=?",
                    ((stem,) for stem in stems),
                )
                conn.execute("COMMIT")
            except Exception:  # noqa: BLE001
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("captures FTS delete failed for %d stems: %s", len(stems), exc)
        return False
    return True


def _strip_screenshot_inplace(path: Path) -> bool:
    """Rewrite a capture JSON without its ``screenshot`` field. Returns True if stripped."""
    try:
        raw = path.read_text()
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if "screenshot" not in data:
        return False
    data.pop("screenshot", None)
    data["screenshot_stripped"] = True
    try:
        # Screenshot retention must not reset whole-capture retention. Preserve
        # the authoritative file's original age across the atomic rewrite.
        _atomic_write_json(path, data, preserve_times=True)
        return True
    except OSError:
        return False
