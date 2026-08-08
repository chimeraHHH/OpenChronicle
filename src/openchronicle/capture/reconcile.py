"""Reconcile authoritative capture JSON with its disposable SQLite projection."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths
from ..logger import get
from ..memory_candidates import store as candidate_store
from ..store import fts
from . import store_lock

logger = get("openchronicle.capture")


@dataclass(frozen=True)
class CaptureReconcileStats:
    scanned: int = 0
    indexed: int = 0
    removed: int = 0
    skipped: int = 0
    hidden: int = 0


def reconcile_capture_index() -> CaptureReconcileStats:
    """Make ``captures`` exactly match valid, non-tombstoned JSON files.

    Capture JSON is canonical. A process death after its atomic rename but
    before the FTS transaction can leave a missing projection; a death during
    cleanup can leave the opposite. Startup and the manual rebuild command use
    this same streaming reconciliation under the collection lock so neither
    state becomes a silent, permanent gap.

    Only compact searchable fields are retained while scanning. Screenshots
    and raw AX trees are never accumulated in memory. Invalid roots, symlinks,
    and tombstoned files are excluded from the projection, and an older
    same-stem row is removed in the same SQLite transaction.
    """

    with store_lock.capture_store_lock():
        return _reconcile_capture_index_locked()


def _reconcile_capture_index_locked() -> CaptureReconcileStats:
    buffer_dir = paths.capture_buffer_dir()
    files = _capture_files(buffer_dir)
    indexed = skipped = hidden = 0
    valid_ids: set[str] = set()

    with fts.cursor() as conn:
        hidden_files = {
            row.artifact_id
            for row in candidate_store.list_tombstones(conn, kind="capture_file")
        }
        existing_ids = {str(row["id"]) for row in conn.execute("SELECT id FROM captures")}

        conn.execute("BEGIN IMMEDIATE")
        try:
            # The projection is disposable. Rebuilding it inside this one
            # transaction makes duplicate-observation winner selection depend
            # only on the sorted canonical filenames, never on whichever row
            # happened to survive a prior crash or partial migration.
            conn.execute("DELETE FROM captures")
            for path in files:
                if path.name in hidden_files:
                    hidden += 1
                    continue
                try:
                    data = json.loads(path.read_bytes())
                    if not isinstance(data, dict):
                        raise ValueError("capture JSON root must be an object")
                    record = _search_projection(data)
                    fts.insert_capture(conn, id=path.stem, **record)
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    sqlite3.IntegrityError,
                    ValueError,
                    TypeError,
                ):
                    skipped += 1
                    continue
                valid_ids.add(path.stem)
                indexed += 1

            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    stats = CaptureReconcileStats(
        scanned=len(files),
        indexed=indexed,
        removed=len(existing_ids - valid_ids),
        skipped=skipped,
        hidden=hidden,
    )
    logger.info(
        "capture index reconciled: scanned=%d indexed=%d removed=%d skipped=%d hidden=%d",
        stats.scanned,
        stats.indexed,
        stats.removed,
        stats.skipped,
        stats.hidden,
    )
    return stats


def _capture_files(buffer_dir: Path) -> list[Path]:
    try:
        children = list(buffer_dir.iterdir())
    except FileNotFoundError:
        return []
    return sorted(
        path
        for path in children
        if path.suffix == ".json" and not path.is_symlink() and path.is_file()
    )


def _search_projection(data: dict[str, Any]) -> dict[str, str]:
    meta_value = data.get("window_meta")
    meta = meta_value if isinstance(meta_value, dict) else {}
    focused_value = data.get("focused_element")
    focused = focused_value if isinstance(focused_value, dict) else {}
    return {
        "observation_id": _text(data.get("observation_id")),
        "timestamp": _text(data.get("timestamp")),
        "app_name": _text(meta.get("app_name")),
        "bundle_id": _text(meta.get("bundle_id")),
        "window_title": _text(meta.get("title")),
        "focused_role": _text(focused.get("role")),
        "focused_value": _text(focused.get("value")),
        "visible_text": _text(data.get("visible_text")),
        "url": _text(data.get("url")),
    }


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
