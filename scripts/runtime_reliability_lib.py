"""Auditable Stage 0 replay and soak helpers.

This module intentionally uses only deterministic synthetic capture data. It
does not invoke the writer/provider stack, make network requests, or copy any
capture field into its reports.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import resource
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openchronicle import paths
from openchronicle.capture import filenames as capture_filenames
from openchronicle.capture import reconcile as capture_reconcile
from openchronicle.capture import scheduler as capture_scheduler
from openchronicle.memory_candidates import store as candidate_store
from openchronicle.store import files as memory_files
from openchronicle.store import fts

REPORT_SCHEMA_VERSION = 2
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "tests" / "runtime" / "reliability" / "manifest.json"
)
_WORKER_OUTPUT_LIMIT = 64 * 1024
_FORBIDDEN_REPORT_KEYS = re.compile(
    r"(?:content|visible_text|focused_value|prompt|api.?key|secret|password|credential|token)",
    re.IGNORECASE,
)
_RESOURCE_FIELDS = {
    "rss_bytes": "max_rss_bytes",
    "cpu_percent": "max_cpu_percent",
    "fd_count": "max_fd_count",
    "thread_count": "max_thread_count",
    "child_process_count": "max_child_process_count",
    "db_bytes": "max_db_bytes",
    "wal_bytes": "max_wal_bytes",
    "shm_bytes": "max_shm_bytes",
    "owned_temp_file_count": "max_owned_temp_file_count",
    "owned_temp_bytes": "max_owned_temp_bytes",
    "capture_buffer_bytes": "max_capture_buffer_bytes",
    "capture_file_count": "max_capture_file_count",
}
_STORAGE_RESOURCE_FIELDS = frozenset(
    {
        "db_bytes",
        "wal_bytes",
        "shm_bytes",
        "owned_temp_file_count",
        "owned_temp_bytes",
        "capture_buffer_bytes",
        "capture_file_count",
    }
)
_TERMINAL_RESOURCE_FIELDS = frozenset(
    {"elapsed_monotonic_ns", *_RESOURCE_FIELDS}
)
_HARD_LIMIT_CEILINGS = {
    "max_rss_bytes": 536_870_912,
    "max_cpu_percent": 200.0,
    "max_fd_count": 256,
    "max_thread_count": 32,
    "max_child_process_count": 0,
    "max_db_bytes": 536_870_912,
    "max_wal_bytes": 67_108_864,
    "max_shm_bytes": 16_777_216,
    "max_owned_temp_file_count": 1,
    "max_owned_temp_bytes": 1_048_576,
    "max_capture_buffer_bytes": 1_073_741_824,
    "max_capture_file_count": 100_000,
}
_MAX_AUDIT_REPLAY_COUNT = 100_000
_PROCESS_GROUP_POLL_SECONDS = 0.05


class AuditError(RuntimeError):
    """The reliability audit could not produce trustworthy evidence."""


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    """Load a closed policy that may tighten, but never weaken, issue #2."""
    data = json.loads(path.read_text(encoding="utf-8"))
    manifest_keys = {
        "schema_version",
        "minimum_replay_capture_count",
        "minimum_soak_duration_seconds",
        "production_daemon_queue_evidence_required",
        "maximum_sample_interval_seconds",
        "sample_timing_tolerance_seconds",
        "maximum_terminal_sample_gap_seconds",
        "minimum_metric_coverage_ratio",
        "limits",
    }
    if (
        not isinstance(data, dict)
        or set(data) != manifest_keys
        or _safe_int(data.get("schema_version")) != MANIFEST_SCHEMA_VERSION
    ):
        raise AuditError("unsupported reliability manifest")
    if not isinstance(data.get("limits"), dict) or set(data["limits"]) != set(
        _HARD_LIMIT_CEILINGS
    ):
        raise AuditError("reliability manifest has no limits")
    if data.get("production_daemon_queue_evidence_required") is not True:
        raise AuditError("reliability manifest must retain the production queue gate")
    required_policy_values = (
        "minimum_replay_capture_count",
        "minimum_soak_duration_seconds",
        "maximum_sample_interval_seconds",
        "sample_timing_tolerance_seconds",
        "maximum_terminal_sample_gap_seconds",
        "minimum_metric_coverage_ratio",
    )
    if any(_safe_number(data.get(key)) is None for key in required_policy_values):
        raise AuditError("reliability manifest has an invalid policy value")
    if _safe_int(data.get("minimum_replay_capture_count")) is None:
        raise AuditError("reliability replay floor must be an integer")
    if any(_safe_number(data["limits"].get(key)) is None for key in _RESOURCE_FIELDS.values()):
        raise AuditError("reliability manifest has an invalid resource limit")
    coverage = float(data["minimum_metric_coverage_ratio"])
    if (
        int(data["minimum_replay_capture_count"]) < 10_000
        or float(data["minimum_soak_duration_seconds"]) < 86_400
        or not 0 < float(data["maximum_sample_interval_seconds"]) <= 60
        or not 0 <= float(data["sample_timing_tolerance_seconds"]) <= 2
        or float(data["maximum_terminal_sample_gap_seconds"])
        < float(data["maximum_sample_interval_seconds"])
        or float(data["maximum_terminal_sample_gap_seconds"]) > 90
        or not 0.95 <= coverage <= 1
    ):
        raise AuditError("reliability manifest policy is outside safe bounds")
    if any(data["limits"][key] > ceiling for key, ceiling in _HARD_LIMIT_CEILINGS.items()):
        raise AuditError("reliability manifest cannot weaken a resource ceiling")
    return data


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        manifest,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest_records(records: Sequence[tuple[str, str, str]]) -> str:
    digest = hashlib.sha256()
    for capture_id, observation_id, timestamp in records:
        for value in (capture_id, observation_id, timestamp):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def _safe_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0:
        return None
    return value


def _safe_int(value: object) -> int | None:
    number = _safe_number(value)
    if number is None or int(number) != number:
        return None
    return int(number)


def _prepare_empty_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    if resolved.exists():
        if not resolved.is_dir() or resolved.is_symlink():
            raise AuditError("audit root must be a real directory")
        if any(resolved.iterdir()):
            raise AuditError("audit root must be empty")
    else:
        resolved.mkdir(parents=True, mode=0o700)
    resolved.chmod(0o700)
    return resolved


@contextlib.contextmanager
def _openchronicle_root(root: Path) -> Iterator[None]:
    previous = os.environ.get("OPENCHRONICLE_ROOT")
    os.environ["OPENCHRONICLE_ROOT"] = str(root)
    try:
        paths.ensure_dirs()
        yield
    finally:
        if previous is None:
            os.environ.pop("OPENCHRONICLE_ROOT", None)
        else:
            os.environ["OPENCHRONICLE_ROOT"] = previous


def _capture_identity(index: int) -> tuple[str, str, str]:
    timestamp = (
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index)
    ).isoformat(timespec="milliseconds")
    observation_id = f"obs_{index + 1:032x}"
    stem = capture_filenames.capture_stem(timestamp, observation_id)
    return stem, observation_id, timestamp


def _capture_record(index: int) -> tuple[str, dict[str, Any]]:
    stem, observation_id, timestamp = _capture_identity(index)
    payload = {
        "schema_version": 4,
        "observation_id": observation_id,
        "timestamp": timestamp,
        "window_meta": {
            "app_name": "RuntimeReplay",
            "bundle_id": "org.openchronicle.runtime-replay",
            "title": f"deterministic-window-{index:08d}",
        },
        "focused_element": {
            "role": "AXTextArea",
            "value": f"deterministic-focused-record-{index:08d}",
        },
        "visible_text": f"deterministic-replay-record-{index:08d}",
        "url": "",
    }
    return stem, payload


def _encoded_capture(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _deterministic_replay_evidence(count: int) -> dict[str, int | str]:
    if not 4 <= count <= _MAX_AUDIT_REPLAY_COUNT:
        raise AuditError("replay count is outside the auditable deterministic range")
    records: list[tuple[str, str, str]] = []
    capture_buffer_bytes = 0
    for index in range(count):
        stem, payload = _capture_record(index)
        records.append((stem, str(payload["observation_id"]), str(payload["timestamp"])))
        capture_buffer_bytes += len(_encoded_capture(payload))
    records.sort()
    return {
        "source_set_digest": _digest_records(records),
        "expected_visible_set_digest": _digest_records(records[:-1]),
        "capture_buffer_bytes": capture_buffer_bytes,
    }


def _write_replay_fixture(directory: Path, *, count: int) -> list[tuple[str, str, str]]:
    records: list[tuple[str, str, str]] = []
    for index in range(count):
        stem, payload = _capture_record(index)
        encoded = _encoded_capture(payload)
        path = directory / f"{stem}.json"
        path.write_bytes(encoded)
        path.chmod(0o600)
        records.append((stem, str(payload["observation_id"]), str(payload["timestamp"])))
    return sorted(records)


def _projection_state(conn: sqlite3.Connection) -> tuple[int, int, str, list[str]]:
    records = [
        (str(row["id"]), str(row["observation_id"] or ""), str(row["timestamp"]))
        for row in conn.execute(
            "SELECT id, observation_id, timestamp FROM captures ORDER BY id"
        )
    ]
    fts_count = int(conn.execute("SELECT COUNT(*) FROM captures_fts").fetchone()[0])
    return len(records), fts_count, _digest_records(records), [record[0] for record in records]


def _sqlite_health(conn: sqlite3.Connection) -> tuple[str, str]:
    row = conn.execute("PRAGMA quick_check").fetchone()
    quick_check = "ok" if row and str(row[0]) == "ok" else ("failed" if row else "missing")
    try:
        conn.execute("INSERT INTO captures_fts(captures_fts) VALUES('integrity-check')")
    except sqlite3.DatabaseError:
        fts_check = "failed"
    else:
        fts_check = "ok"
    return quick_check, fts_check


def _owned_temp_metrics(root: Path) -> tuple[int, int]:
    count = 0
    total = 0
    for candidate in root.rglob("*"):
        try:
            is_file = candidate.is_file() and not candidate.is_symlink()
        except OSError:
            continue
        if not is_file:
            continue
        if not (
            capture_filenames.is_capture_temp_name(candidate.name)
            or memory_files.is_memory_temp_name(candidate.name)
        ):
            continue
        count += 1
        with contextlib.suppress(OSError):
            total += candidate.stat().st_size
    return count, total


def _storage_metrics(root: Path) -> dict[str, int]:
    database = root / "index.db"
    capture_dir = root / "capture-buffer"
    capture_bytes = 0
    capture_count = 0
    if capture_dir.exists():
        for candidate in capture_dir.glob("*.json"):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            capture_count += 1
            with contextlib.suppress(OSError):
                capture_bytes += candidate.stat().st_size
    temp_count, temp_bytes = _owned_temp_metrics(root)

    def size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    return {
        "db_bytes": size(database),
        "wal_bytes": size(Path(f"{database}-wal")),
        "shm_bytes": size(Path(f"{database}-shm")),
        "capture_buffer_bytes": capture_bytes,
        "capture_file_count": capture_count,
        "owned_temp_file_count": temp_count,
        "owned_temp_bytes": temp_bytes,
    }


def _expected_capture_buffer_bytes(count: int) -> int | None:
    """Return the exact deterministic worker payload bytes for ``count`` cycles."""
    if not 0 <= count <= _MAX_AUDIT_REPLAY_COUNT:
        return None
    return sum(
        len(_encoded_capture(_capture_record(index)[1])) for index in range(count)
    )


def _complete_terminal_resource_snapshot(value: object) -> bool:
    """Require one fully numeric worker snapshot, not coverage-based sampling."""
    if not isinstance(value, Mapping) or set(value) != _TERMINAL_RESOURCE_FIELDS:
        return False
    return (
        _safe_int(value.get("elapsed_monotonic_ns")) is not None
        and all(_safe_number(value.get(field)) is not None for field in _RESOURCE_FIELDS)
    )


def _complete_terminal_storage_snapshot(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _STORAGE_RESOURCE_FIELDS:
        return False
    return all(_safe_int(value.get(field)) is not None for field in _STORAGE_RESOURCE_FIELDS)


def _terminal_resource_checks(
    *,
    worker_snapshot: object,
    parent_storage_snapshot: object,
    worker_cycles: int | None,
    worker_indexed: int | None,
    worker_observed_ns: int | None,
    manifest: Mapping[str, Any],
) -> dict[str, bool]:
    """Cross-check terminal worker evidence against parent-observed storage."""
    worker_complete = _complete_terminal_resource_snapshot(worker_snapshot)
    parent_complete = _complete_terminal_storage_snapshot(parent_storage_snapshot)
    snapshots_match = bool(
        worker_complete
        and parent_complete
        and all(
            worker_snapshot[field] == parent_storage_snapshot[field]
            for field in _STORAGE_RESOURCE_FIELDS
        )
    )

    expected_bytes = (
        _expected_capture_buffer_bytes(worker_cycles)
        if worker_cycles is not None
        else None
    )
    capture_accounting = bool(
        worker_complete
        and parent_complete
        and worker_cycles is not None
        and worker_cycles > 0
        and worker_indexed == worker_cycles
        and worker_snapshot["capture_file_count"] == worker_cycles
        and parent_storage_snapshot["capture_file_count"] == worker_cycles
        and expected_bytes is not None
        and worker_snapshot["capture_buffer_bytes"] == expected_bytes
        and parent_storage_snapshot["capture_buffer_bytes"] == expected_bytes
        and worker_observed_ns is not None
        and worker_snapshot["elapsed_monotonic_ns"] <= worker_observed_ns
    )

    limits = manifest["limits"]
    worker_within_limits = bool(
        worker_complete
        and all(
            worker_snapshot[field] <= limits[limit_name]
            for field, limit_name in _RESOURCE_FIELDS.items()
        )
    )
    parent_within_limits = bool(
        parent_complete
        and all(
            parent_storage_snapshot[field] <= limits[_RESOURCE_FIELDS[field]]
            for field in _STORAGE_RESOURCE_FIELDS
        )
    )
    return {
        "terminal_snapshot_complete": worker_complete and parent_complete,
        "terminal_snapshots_match": snapshots_match,
        "terminal_capture_accounting_exact": capture_accounting,
        "terminal_resource_limits_respected": (
            worker_within_limits and parent_within_limits
        ),
    }


def _replay_resources_within_limits(
    metrics: Mapping[str, Any], manifest: Mapping[str, Any]
) -> bool:
    final = metrics.get("final") if isinstance(metrics.get("final"), dict) else {}
    limits = manifest["limits"]
    values = {
        "rss_bytes": metrics.get("rss_bytes"),
        "db_bytes": final.get("db_bytes"),
        "wal_bytes": final.get("wal_bytes"),
        "shm_bytes": final.get("shm_bytes"),
        "owned_temp_file_count": final.get("owned_temp_file_count"),
        "owned_temp_bytes": final.get("owned_temp_bytes"),
        "capture_buffer_bytes": final.get("capture_buffer_bytes"),
        "capture_file_count": final.get("capture_file_count"),
    }
    return all(
        (number := _safe_number(value)) is not None
        and number <= limits[_RESOURCE_FIELDS[field]]
        for field, value in values.items()
    )


def _replay_storage_accounting_exact(
    metrics: Mapping[str, Any],
    *,
    count: int,
    expected_capture_bytes: int,
    checkpoint: object,
) -> bool:
    fixture = metrics.get("after_fixture") if isinstance(metrics.get("after_fixture"), dict) else {}
    faults = (
        metrics.get("after_fault_injection")
        if isinstance(metrics.get("after_fault_injection"), dict)
        else {}
    )
    final = metrics.get("final") if isinstance(metrics.get("final"), dict) else {}
    common_exact = {
        "capture_buffer_bytes": expected_capture_bytes,
        "capture_file_count": count,
    }
    return (
        all(fixture.get(key) == value for key, value in common_exact.items())
        and fixture.get("db_bytes") == 0
        and fixture.get("wal_bytes") == 0
        and fixture.get("shm_bytes") == 0
        and fixture.get("owned_temp_file_count") == 0
        and fixture.get("owned_temp_bytes") == 0
        and all(faults.get(key) == value for key, value in common_exact.items())
        and _safe_int(faults.get("db_bytes")) is not None
        and faults.get("db_bytes") > 0
        and faults.get("wal_bytes") == 0
        and faults.get("shm_bytes") == 0
        and faults.get("owned_temp_file_count") == 1
        and faults.get("owned_temp_bytes") == len(b"deterministic-orphan-temp")
        and all(final.get(key) == value for key, value in common_exact.items())
        and _safe_int(final.get("db_bytes")) is not None
        and final.get("db_bytes") > 0
        and final.get("wal_bytes") == 0
        and final.get("shm_bytes") == 0
        and final.get("owned_temp_file_count") == 0
        and final.get("owned_temp_bytes") == 0
        and checkpoint == [0, 0, 0]
    )


def _ps_process_metrics(pid: int) -> dict[str, int | float | None]:
    result: dict[str, int | float | None] = {
        "rss_bytes": None,
        "cpu_percent": None,
    }
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "rss=", "-o", "%cpu="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return result
    if completed.returncode != 0:
        return result
    parts = completed.stdout.strip().split()
    if len(parts) < 2:
        return result
    try:
        result["rss_bytes"] = int(parts[0]) * 1024
        result["cpu_percent"] = float(parts[1].replace(",", "."))
    except ValueError:
        pass
    return result


def _fd_count(pid: int) -> int | None:
    proc_fd = Path("/proc") / str(pid) / "fd"
    if proc_fd.is_dir():
        try:
            return len(list(proc_fd.iterdir()))
        except OSError:
            return None
    try:
        completed = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-Fn"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return sum(1 for line in completed.stdout.splitlines() if re.fullmatch(r"f\d+", line))


def _thread_count(pid: int) -> int | None:
    status = Path("/proc") / str(pid) / "status"
    if status.is_file():
        try:
            for line in status.read_text(encoding="utf-8").splitlines():
                if line.startswith("Threads:"):
                    return int(line.split(":", 1)[1].strip())
        except (OSError, ValueError):
            return None
    try:
        completed = subprocess.run(
            ["ps", "-M", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return max(0, len(lines) - 1)


def _descendant_count(pid: int) -> int | None:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=", "-o", "ppid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    children: dict[int, list[int]] = {}
    try:
        for line in completed.stdout.splitlines():
            child, parent = (int(part) for part in line.split())
            children.setdefault(parent, []).append(child)
    except ValueError:
        return None
    descendants: set[int] = set()
    pending = list(children.get(pid, []))
    while pending:
        child = pending.pop()
        if child in descendants:
            continue
        descendants.add(child)
        pending.extend(children.get(child, []))
    return len(descendants)


def _sample_process(root: Path, *, pid: int, elapsed_ns: int) -> dict[str, int | float | None]:
    sample: dict[str, int | float | None] = {
        "elapsed_monotonic_ns": elapsed_ns,
        **_ps_process_metrics(pid),
        "fd_count": _fd_count(pid),
        "thread_count": _thread_count(pid),
        "child_process_count": _descendant_count(pid),
    }
    sample.update(_storage_metrics(root))
    return sample


def _sample_current_worker_terminal(
    root: Path,
    *,
    elapsed_ns: int,
) -> dict[str, int | float]:
    """Take the worker's final snapshot without spawning measurement children.

    The ordinary supervisor probes use ``ps``/``lsof`` where portable kernel
    files are unavailable. Running those probes *inside* the worker would make
    the supervisor correctly observe the audit tools themselves as forbidden
    descendants. The deterministic worker is single-threaded and never starts
    functional children, so use in-process counters for this terminal frame;
    process-group drainage remains the independent no-descendant proof.
    """
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss_bytes = int(usage.ru_maxrss)
    if sys.platform != "darwin":
        rss_bytes *= 1024
    try:
        fd_count = len(os.listdir("/dev/fd"))
    except OSError:
        fd_count = 0
        soft_limit = int(resource.getrlimit(resource.RLIMIT_NOFILE)[0])
        for fd in range(min(soft_limit, 4096)):
            with contextlib.suppress(OSError):
                os.fstat(fd)
                fd_count += 1
    elapsed_seconds = max(elapsed_ns / 1_000_000_000, 1e-9)
    sample: dict[str, int | float] = {
        "elapsed_monotonic_ns": elapsed_ns,
        "rss_bytes": rss_bytes,
        "cpu_percent": 100.0 * time.process_time() / elapsed_seconds,
        "fd_count": fd_count,
        "thread_count": threading.active_count(),
        "child_process_count": 0,
    }
    sample.update(_storage_metrics(root))
    return sample


def summarize_samples(
    samples: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Recompute bounded-resource evidence from raw numeric samples."""
    limits = manifest["limits"]
    minimum_coverage = float(manifest["minimum_metric_coverage_ratio"])
    summary: dict[str, Any] = {
        "sample_count": len(samples),
        "metrics": {},
        "coverage_complete": bool(samples),
        "within_limits": bool(samples),
    }
    for field, limit_name in _RESOURCE_FIELDS.items():
        values = [
            number
            for sample in samples
            if (number := _safe_number(sample.get(field))) is not None
        ]
        coverage = len(values) / len(samples) if samples else 0.0
        maximum = max(values) if values else None
        limit = limits[limit_name]
        within = maximum is not None and maximum <= limit
        covered = coverage >= minimum_coverage
        summary["metrics"][field] = {
            "maximum": maximum,
            "limit": limit,
            "coverage_ratio": round(coverage, 6),
            "covered": covered,
            "within_limit": within,
        }
        summary["coverage_complete"] = summary["coverage_complete"] and covered
        summary["within_limits"] = summary["within_limits"] and within
    return summary


def _sample_limit_breaches(
    sample: Mapping[str, Any], manifest: Mapping[str, Any]
) -> list[str]:
    limits = manifest["limits"]
    breaches: list[str] = []
    for field, limit_name in _RESOURCE_FIELDS.items():
        value = _safe_number(sample.get(field))
        if value is not None and value > limits[limit_name]:
            breaches.append(field)
    return breaches


def summarize_sample_timeline(
    samples: Sequence[Mapping[str, Any]],
    *,
    observed_duration_ns: int,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate that samples cover the monotonic run instead of just existing."""
    elapsed = [_safe_int(sample.get("elapsed_monotonic_ns")) for sample in samples]
    well_formed = bool(elapsed) and all(value is not None for value in elapsed)
    values = [value for value in elapsed if value is not None]
    monotonic = well_formed and all(
        left < right for left, right in zip(values, values[1:], strict=False)
    )
    inside_run = well_formed and all(0 <= value <= observed_duration_ns for value in values)
    adjacent_gaps = [
        right - left for left, right in zip(values, values[1:], strict=False)
    ]
    maximum_adjacent_gap = max(adjacent_gaps, default=0) if values else None
    first_gap = values[0] if values else None
    terminal_gap = observed_duration_ns - values[-1] if values else None
    maximum_gap_ns = int(
        (
            float(manifest["maximum_sample_interval_seconds"])
            + float(manifest["sample_timing_tolerance_seconds"])
        )
        * 1_000_000_000
    )
    maximum_terminal_gap_ns = int(
        float(manifest["maximum_terminal_sample_gap_seconds"]) * 1_000_000_000
    )
    complete = bool(
        well_formed
        and monotonic
        and inside_run
        and first_gap is not None
        and first_gap <= maximum_gap_ns
        and maximum_adjacent_gap is not None
        and maximum_adjacent_gap <= maximum_gap_ns
        and terminal_gap is not None
        and terminal_gap <= maximum_terminal_gap_ns
    )
    return {
        "complete": complete,
        "sample_count": len(samples),
        "first_sample_gap_ns": first_gap,
        "maximum_adjacent_gap_ns": maximum_adjacent_gap,
        "terminal_sample_gap_ns": terminal_gap,
        "maximum_allowed_gap_ns": maximum_gap_ns,
        "maximum_allowed_terminal_gap_ns": maximum_terminal_gap_ns,
        "strictly_monotonic": monotonic,
        "inside_observed_run": inside_run,
    }


def run_replay_audit(
    root: Path,
    *,
    count: int,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay deterministic authoritative JSON and verify projection convergence."""
    if not 4 <= count <= _MAX_AUDIT_REPLAY_COUNT:
        raise AuditError("replay count is outside the auditable deterministic range")
    policy = load_manifest() if manifest is None else manifest
    deterministic = _deterministic_replay_evidence(count)
    root = _prepare_empty_root(root)
    started_ns = time.monotonic_ns()
    with _openchronicle_root(root):
        records = _write_replay_fixture(paths.capture_buffer_dir(), count=count)
        source_digest = _digest_records(records)
        storage_after_fixture = _storage_metrics(root)
        initial = capture_reconcile.reconcile_capture_index()

        missing_records = (records[0], records[count // 2])
        tombstoned_record = records[-1]
        stale_id = "runtime-stale-search-projection"
        with fts.cursor() as conn:
            for capture_id, _, _ in missing_records:
                fts.delete_capture(conn, capture_id)
            fts.insert_capture(
                conn,
                id=stale_id,
                observation_id="obs_ffffffffffffffffffffffffffffffff",
                timestamp="2025-12-31T23:59:59.000+00:00",
                app_name="RuntimeReplay",
                bundle_id="org.openchronicle.runtime-replay",
                window_title="stale-projection",
                focused_role="AXTextArea",
                focused_value="stale-projection",
                visible_text="stale-projection",
                url="",
            )
            candidate_store.put_tombstone(
                conn,
                kind="capture_file",
                artifact_id=f"{tombstoned_record[0]}.json",
            )

        temp_path = paths.capture_buffer_dir() / f".{records[0][0]}.json.audit.tmp"
        temp_path.write_bytes(b"deterministic-orphan-temp")
        temp_path.chmod(0o600)
        storage_after_faults = _storage_metrics(root)

        cleanup = capture_scheduler.cleanup_buffer(retention_hours=168)
        recovered = capture_reconcile.reconcile_capture_index()
        expected_records = records[:-1]
        expected_digest = _digest_records(expected_records)
        with fts.cursor() as conn:
            first_count, first_fts_count, first_digest, first_ids = _projection_state(conn)
            first_quick_check, first_fts_check = _sqlite_health(conn)

        second = capture_reconcile.reconcile_capture_index()
        with fts.cursor() as conn:
            second_count, second_fts_count, second_digest, second_ids = _projection_state(conn)
            second_quick_check, second_fts_check = _sqlite_health(conn)
            tombstone_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM purge_tombstones WHERE kind='capture_file'"
                ).fetchone()[0]
            )
        checkpoint = fts.checkpoint("TRUNCATE")
        storage_final = _storage_metrics(root)

    checkpoint_list = list(checkpoint)
    metrics = {
        "elapsed_monotonic_ns": time.monotonic_ns() - started_ns,
        "after_fixture": storage_after_fixture,
        "after_fault_injection": storage_after_faults,
        "final": storage_final,
        "rss_bytes": _ps_process_metrics(os.getpid())["rss_bytes"],
    }
    reconcile = {
        "initial": asdict(initial),
        "recovery": asdict(recovered),
        "idempotence": asdict(second),
    }
    expected_reconcile = {
        "initial": {
            "scanned": count,
            "indexed": count,
            "removed": 0,
            "skipped": 0,
            "hidden": 0,
        },
        "recovery": {
            "scanned": count,
            "indexed": count - 1,
            "removed": 2,
            "skipped": 0,
            "hidden": 1,
        },
        "idempotence": {
            "scanned": count,
            "indexed": count - 1,
            "removed": 0,
            "skipped": 0,
            "hidden": 1,
        },
    }
    checks = {
        "source_count_exact": len(records) == count,
        "deterministic_fixture_exact": source_digest
        == deterministic["source_set_digest"]
        and expected_digest == deterministic["expected_visible_set_digest"],
        "faults_injected": storage_after_faults["owned_temp_file_count"] == 1,
        "temp_recovered": cleanup["deleted"] == 1
        and storage_final["owned_temp_file_count"] == 0,
        "set_count_exact": first_count == count - 1 == second_count,
        "fts_count_exact": first_fts_count == count - 1 == second_fts_count,
        "set_digest_exact": first_digest == expected_digest == second_digest,
        "stale_removed": stale_id not in first_ids and stale_id not in second_ids,
        "missing_repaired": all(record[0] in first_ids for record in missing_records),
        "tombstone_hidden": tombstoned_record[0] not in first_ids and tombstone_count == 1,
        "sqlite_quick_check": first_quick_check == "ok" == second_quick_check,
        "fts_integrity_check": first_fts_check == "ok" == second_fts_check,
        "second_reconcile_idempotent": (
            first_count,
            first_fts_count,
            first_digest,
        )
        == (second_count, second_fts_count, second_digest)
        and second.removed == 0,
        "reconcile_accounting_exact": reconcile == expected_reconcile,
        "storage_accounting_exact": _replay_storage_accounting_exact(
            metrics,
            count=count,
            expected_capture_bytes=int(deterministic["capture_buffer_bytes"]),
            checkpoint=checkpoint_list,
        ),
        "resource_limits_respected": _replay_resources_within_limits(metrics, policy),
    }
    return {
        "run_status": "passed" if all(checks.values()) else "failed",
        "requested_capture_count": count,
        "model_calls": 0,
        "source_set_digest": source_digest,
        "expected_visible_set_digest": expected_digest,
        "actual_visible_set_digest": second_digest,
        "visible_capture_count": second_count,
        "fts_row_count": second_fts_count,
        "faults": {
            "missing_projection_rows": len(missing_records),
            "stale_projection_rows": 1,
            "orphan_capture_temps": 1,
            "tombstoned_capture_files": 1,
        },
        "reconcile": reconcile,
        "checks": checks,
        "sqlite": {
            "quick_check": second_quick_check,
            "fts_integrity_check": second_fts_check,
            "checkpoint": checkpoint_list,
        },
        "metrics": metrics,
    }


def _sanitized_worker_environment(root: Path) -> dict[str, str]:
    forbidden = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in forbidden)
        and not key.startswith("OPENCHRONICLE_TEST_")
    }
    environment["OPENCHRONICLE_ROOT"] = str(root)
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _process_table() -> dict[int, tuple[int, str]] | None:
    """Return PID -> (PGID, state) without reaping an owned child."""
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=", "-o", "pgid=", "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    table: dict[int, tuple[int, str]] = {}
    try:
        for line in completed.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            if len(parts) != 3:
                return None
            pid, pgid = int(parts[0]), int(parts[1])
            table[pid] = (pgid, parts[2])
    except ValueError:
        return None
    return table


def _leader_exited_without_reaping(process: subprocess.Popen[bytes]) -> bool | None:
    """Observe leader exit state while preserving its PID/PGID generation."""
    if process.returncode is not None:
        return True
    table = _process_table()
    if table is None:
        return None
    row = table.get(process.pid)
    return row is None or row[1].startswith("Z")


def _owned_group_is_drained(
    *,
    owned_pgid: int,
    leader_pid: int,
) -> bool | None:
    """Require every descendant PID to disappear and the leader to stop running."""
    table = _process_table()
    if table is None:
        return None
    members = {
        pid: state
        for pid, (pgid, state) in table.items()
        if pgid == owned_pgid
    }
    return all(
        pid == leader_pid and state.startswith("Z")
        for pid, state in members.items()
    )


def _wait_for_owned_group_drain(
    *,
    owned_pgid: int,
    leader_pid: int,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _owned_group_is_drained(
            owned_pgid=owned_pgid,
            leader_pid=leader_pid,
        ) is True:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_PROCESS_GROUP_POLL_SECONDS, remaining))


def _signal_owned_group(owned_pgid: int, signum: int) -> None:
    # A group containing only an exited leader may no longer be signalable on
    # macOS even though the unreaped leader still reserves its numeric PID.
    # Depending on the exact exit/reap boundary, Darwin reports either ESRCH
    # or EPERM for that zombie-only group; neither means a different process
    # generation can have reused the still-unreaped leader's PGID.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(owned_pgid, signum)


def _stop_owned_process(
    process: subprocess.Popen[bytes],
    *,
    owned_pgid: int,
    grace_seconds: float,
) -> str:
    """Drain the exact process group while its leader PID cannot be reused.

    ``run_soak_audit`` records ``owned_pgid`` immediately after spawning the
    worker with ``start_new_session=True``. This function deliberately avoids
    ``poll`` and ``wait`` until the whole group is gone: reaping an exited
    leader first would free its numeric PID/PGID while a TERM-resistant
    descendant could remain alive.
    """
    if owned_pgid != process.pid:
        raise AuditError("owned process group does not match its leader")
    if process.returncode is not None:
        return "already-exited"

    leader_was_exited = _leader_exited_without_reaping(process) is True
    if _owned_group_is_drained(
        owned_pgid=owned_pgid,
        leader_pid=process.pid,
    ) is True:
        process.wait(timeout=max(1.0, grace_seconds))
        return "already-exited" if leader_was_exited else "terminated"

    _signal_owned_group(owned_pgid, signal.SIGTERM)
    if _wait_for_owned_group_drain(
        owned_pgid=owned_pgid,
        leader_pid=process.pid,
        timeout_seconds=grace_seconds,
    ):
        process.wait(timeout=max(1.0, grace_seconds))
        return "terminated"

    # The leader is still unreaped here, so its numeric PID keeps this PGID
    # generation pinned while KILL is delivered to every residual member.
    _signal_owned_group(owned_pgid, signal.SIGKILL)
    if not _wait_for_owned_group_drain(
        owned_pgid=owned_pgid,
        leader_pid=process.pid,
        timeout_seconds=max(1.0, grace_seconds),
    ):
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1.0)
        raise AuditError("owned process group did not drain after SIGKILL")
    process.wait(timeout=max(1.0, grace_seconds))
    return "killed"


class _ProcessExitObserver:
    """Wait for child exit without calling a reaping Popen method."""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._exited = False
        self._kqueue: Any | None = None
        self._pidfd: int | None = None
        if hasattr(select, "kqueue"):
            queue = select.kqueue()
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                queue.control([event], 0, 0)
            except OSError:
                queue.close()
            else:
                self._kqueue = queue
        elif hasattr(os, "pidfd_open"):
            with contextlib.suppress(OSError):
                self._pidfd = os.pidfd_open(pid)

    def wait(self, timeout_seconds: float) -> bool:
        if self._exited:
            return True
        timeout_seconds = max(0.0, timeout_seconds)
        if self._kqueue is not None:
            self._exited = bool(self._kqueue.control(None, 1, timeout_seconds))
            return self._exited
        if self._pidfd is not None:
            readable, _writable, _exceptional = select.select(
                [self._pidfd],
                [],
                [],
                timeout_seconds,
            )
            self._exited = bool(readable)
            return self._exited

        deadline = time.monotonic() + timeout_seconds
        while True:
            observed = _leader_exited_without_reaping_pid(self._pid)
            if observed is True:
                self._exited = True
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.25, remaining))

    def close(self) -> None:
        if self._kqueue is not None:
            self._kqueue.close()
            self._kqueue = None
        if self._pidfd is not None:
            os.close(self._pidfd)
            self._pidfd = None


def _leader_exited_without_reaping_pid(pid: int) -> bool | None:
    table = _process_table()
    if table is None:
        return None
    row = table.get(pid)
    return row is None or row[1].startswith("Z")


def _soak_worker_command(
    *,
    duration_seconds: float,
    work_interval_seconds: float,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).with_name("run_runtime_reliability_audit.py").resolve()),
        "_soak-worker",
        "--duration-seconds",
        repr(duration_seconds),
        "--work-interval-seconds",
        repr(work_interval_seconds),
    ]


def _decode_worker_output(output_file: Any) -> dict[str, Any] | None:
    output_file.seek(0)
    encoded = output_file.read(_WORKER_OUTPUT_LIMIT + 1)
    if len(encoded) > _WORKER_OUTPUT_LIMIT:
        return None
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def run_soak_audit(
    root: Path,
    *,
    duration_seconds: float,
    sample_interval_seconds: float,
    work_interval_seconds: float,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Run and sample a dedicated synthetic workload subprocess."""
    intervals = (duration_seconds, sample_interval_seconds, work_interval_seconds)
    if any(not math.isfinite(value) or value <= 0 for value in intervals):
        raise AuditError("soak durations and intervals must be positive")
    requested_ns = int(duration_seconds * 1_000_000_000)
    sample_interval_ns = int(sample_interval_seconds * 1_000_000_000)
    work_interval_ns = int(work_interval_seconds * 1_000_000_000)
    if min(requested_ns, sample_interval_ns, work_interval_ns) < 1:
        raise AuditError("soak durations and intervals must be at least one nanosecond")
    root = _prepare_empty_root(root)
    command = _soak_worker_command(
        duration_seconds=duration_seconds,
        work_interval_seconds=work_interval_seconds,
    )
    started_ns = time.monotonic_ns()
    grace_seconds = max(10.0, min(60.0, sample_interval_seconds * 2))
    samples: list[dict[str, int | float | None]] = []
    stop_disposition = "not-needed"
    timed_out = False
    resource_limit_breaches: list[str] = []
    worker_result: dict[str, Any] | None = None
    terminal_storage_snapshot: dict[str, int] | None = None
    next_sample_ns = started_ns
    with tempfile.TemporaryFile(mode="w+b") as output_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output_file,
            stderr=subprocess.DEVNULL,
            env=_sanitized_worker_environment(root),
            start_new_session=True,
        )
        # ``start_new_session=True`` makes the worker PID its PGID. Capture
        # that generation before the worker can exit; macOS getpgid() returns
        # ESRCH for an unreaped zombie even while its descendants retain the
        # original process group.
        owned_pgid = process.pid
        exit_observer = _ProcessExitObserver(process.pid)
        deadline_ns = started_ns + requested_ns + int(grace_seconds * 1_000_000_000)
        try:
            while not exit_observer.wait(0):
                now_ns = time.monotonic_ns()
                if now_ns >= deadline_ns:
                    timed_out = True
                    stop_disposition = _stop_owned_process(
                        process,
                        owned_pgid=owned_pgid,
                        grace_seconds=grace_seconds,
                    )
                    break
                if now_ns < next_sample_ns:
                    wait_seconds = min(
                        (next_sample_ns - now_ns) / 1_000_000_000,
                        (deadline_ns - now_ns) / 1_000_000_000,
                        60.0,
                    )
                    exit_observer.wait(wait_seconds)
                    continue
                sample = _sample_process(root, pid=process.pid, elapsed_ns=now_ns - started_ns)
                process_fields = (
                    "rss_bytes",
                    "cpu_percent",
                    "fd_count",
                    "thread_count",
                    "child_process_count",
                )
                # A worker can exit between the non-reaping observer and a
                # later lsof/ps probe. Drop only that terminal partial sample;
                # missing metrics while the process is still live remain
                # visible and lower coverage, as they should.
                leader_exited = exit_observer.wait(0)
                if not leader_exited or all(
                    sample[field] is not None for field in process_fields
                ):
                    samples.append(sample)
                    resource_limit_breaches = _sample_limit_breaches(sample, manifest)
                    if resource_limit_breaches and not leader_exited:
                        stop_disposition = _stop_owned_process(
                            process,
                            owned_pgid=owned_pgid,
                            grace_seconds=grace_seconds,
                        )
                        break
                next_sample_ns += sample_interval_ns
                completed_probe_ns = time.monotonic_ns()
                if next_sample_ns <= completed_probe_ns:
                    missed_slots = (completed_probe_ns - next_sample_ns) // sample_interval_ns + 1
                    next_sample_ns += missed_slots * sample_interval_ns
        finally:
            try:
                if process.returncode is None:
                    stop_disposition = _stop_owned_process(
                        process,
                        owned_pgid=owned_pgid,
                        grace_seconds=grace_seconds,
                    )
            finally:
                exit_observer.close()
        returncode = process.wait()
        worker_result = _decode_worker_output(output_file)
        # The worker has exited and no writer remains for this private root.
        # Re-sample storage from the supervising process so a worker cannot
        # hide a terminal growth spike behind the allowed final sample gap.
        terminal_storage_snapshot = _storage_metrics(root)

    observed_ns = time.monotonic_ns() - started_ns
    summary = summarize_samples(samples, manifest)
    timeline_summary = summarize_sample_timeline(
        samples,
        observed_duration_ns=observed_ns,
        manifest=manifest,
    )
    worker_completed = bool(worker_result and worker_result.get("completed_requested_duration"))
    worker_healthy = bool(
        worker_result
        and worker_result.get("quick_check") == "ok"
        and worker_result.get("fts_integrity_check") == "ok"
        and worker_result.get("owned_temp_file_count") == 0
        and worker_result.get("model_calls") == 0
    )
    worker_requested_ns = _safe_int(worker_result.get("requested_duration_ns")) if worker_result else None
    worker_observed_ns = _safe_int(worker_result.get("observed_duration_ns")) if worker_result else None
    worker_cycles = _safe_int(worker_result.get("cycles")) if worker_result else None
    worker_indexed = _safe_int(worker_result.get("indexed_capture_count")) if worker_result else None
    worker_terminal_snapshot = (
        worker_result.get("terminal_resource_snapshot") if worker_result else None
    )
    terminal_checks = _terminal_resource_checks(
        worker_snapshot=worker_terminal_snapshot,
        parent_storage_snapshot=terminal_storage_snapshot,
        worker_cycles=worker_cycles,
        worker_indexed=worker_indexed,
        worker_observed_ns=worker_observed_ns,
        manifest=manifest,
    )
    worker_accounting = bool(
        worker_requested_ns == requested_ns
        and worker_observed_ns is not None
        and requested_ns <= worker_observed_ns <= observed_ns
        and worker_cycles is not None
        and worker_cycles > 0
        and worker_indexed == worker_cycles
    )
    requested_completed = (
        observed_ns >= requested_ns
        and worker_completed
        and not timed_out
        and returncode == 0
    )
    sample_interval_allowed = sample_interval_seconds <= float(
        manifest["maximum_sample_interval_seconds"]
    )
    checks = {
        "worker_exit_zero": returncode == 0,
        "worker_report_bounded_and_valid": worker_result is not None,
        "requested_duration_completed": requested_completed,
        "sqlite_and_fts_healthy": worker_healthy,
        "worker_accounting_exact": worker_accounting,
        "resource_metric_coverage": summary["coverage_complete"],
        "resource_limits_respected": summary["within_limits"],
        "sample_interval_allowed": sample_interval_allowed,
        "sample_timeline_covered": timeline_summary["complete"],
        "no_descendant_leak": (
            summary["metrics"]["child_process_count"]["maximum"] == 0
            and terminal_checks["terminal_snapshot_complete"]
            and worker_terminal_snapshot["child_process_count"] == 0
        ),
        **terminal_checks,
    }
    return {
        "run_status": "passed" if all(checks.values()) else "failed",
        "requested_duration_ns": requested_ns,
        "observed_duration_ns": observed_ns,
        "sample_interval_seconds": sample_interval_seconds,
        "work_interval_seconds": work_interval_seconds,
        "model_calls": 0,
        "worker": worker_result,
        "process": {
            "returncode": returncode,
            "timed_out": timed_out,
            "stop_disposition": stop_disposition,
            "resource_limit_breaches": resource_limit_breaches,
        },
        "checks": checks,
        "samples": samples,
        "resource_summary": summary,
        "sample_timeline": timeline_summary,
        "terminal_storage_snapshot": terminal_storage_snapshot,
    }


def _failure_phase(phase: str, error: BaseException) -> dict[str, Any]:
    if isinstance(error, AuditError):
        category = "audit"
    elif isinstance(error, sqlite3.Error):
        category = "sqlite"
    elif isinstance(error, OSError):
        category = "io"
    else:
        category = "unexpected"
    return {
        "run_status": "failed",
        "failure": {"phase": phase, "error_type": category},
    }


def evaluate_report(report: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Independently derive complete/incomplete/failed from report evidence."""
    manifest_matches = report.get("manifest_sha256") == manifest_digest(manifest)
    replay = report.get("replay") if isinstance(report.get("replay"), dict) else {}
    soak = report.get("soak") if isinstance(report.get("soak"), dict) else {}
    replay_checks = replay.get("checks") if isinstance(replay.get("checks"), dict) else {}
    soak_checks = soak.get("checks") if isinstance(soak.get("checks"), dict) else {}
    samples = soak.get("samples") if isinstance(soak.get("samples"), list) else []
    recalculated_summary = summarize_samples(samples, manifest)

    requested_replay = _safe_int(replay.get("requested_capture_count"))
    visible_count = _safe_int(replay.get("visible_capture_count"))
    fts_count = _safe_int(replay.get("fts_row_count"))
    faults = replay.get("faults") if isinstance(replay.get("faults"), dict) else {}
    sqlite_evidence = replay.get("sqlite") if isinstance(replay.get("sqlite"), dict) else {}
    replay_metrics = replay.get("metrics") if isinstance(replay.get("metrics"), dict) else {}
    replay_reconcile = (
        replay.get("reconcile") if isinstance(replay.get("reconcile"), dict) else {}
    )
    try:
        deterministic = (
            _deterministic_replay_evidence(requested_replay)
            if requested_replay is not None
            else {}
        )
    except AuditError:
        deterministic = {}
    expected_reconcile = (
        {
            "initial": {
                "scanned": requested_replay,
                "indexed": requested_replay,
                "removed": 0,
                "skipped": 0,
                "hidden": 0,
            },
            "recovery": {
                "scanned": requested_replay,
                "indexed": requested_replay - 1,
                "removed": 2,
                "skipped": 0,
                "hidden": 1,
            },
            "idempotence": {
                "scanned": requested_replay,
                "indexed": requested_replay - 1,
                "removed": 0,
                "skipped": 0,
                "hidden": 1,
            },
        }
        if requested_replay is not None
        else {}
    )
    replay_storage_exact = bool(
        deterministic
        and requested_replay is not None
        and _replay_storage_accounting_exact(
            replay_metrics,
            count=requested_replay,
            expected_capture_bytes=int(deterministic["capture_buffer_bytes"]),
            checkpoint=sqlite_evidence.get("checkpoint"),
        )
    )
    replay_evidence = (
        requested_replay is not None
        and visible_count == requested_replay - 1
        and fts_count == requested_replay - 1
        and replay.get("source_set_digest") == deterministic.get("source_set_digest")
        and replay.get("expected_visible_set_digest")
        == deterministic.get("expected_visible_set_digest")
        and replay.get("expected_visible_set_digest") == replay.get("actual_visible_set_digest")
        and _safe_int(faults.get("missing_projection_rows")) == 2
        and _safe_int(faults.get("stale_projection_rows")) == 1
        and _safe_int(faults.get("orphan_capture_temps")) == 1
        and _safe_int(faults.get("tombstoned_capture_files")) == 1
        and sqlite_evidence.get("quick_check") == "ok"
        and sqlite_evidence.get("fts_integrity_check") == "ok"
        and replay_reconcile == expected_reconcile
        and replay_storage_exact
        and _replay_resources_within_limits(replay_metrics, manifest)
    )

    replay_functional = (
        replay.get("run_status") == "passed"
        and bool(replay_checks)
        and all(value is True for value in replay_checks.values())
        and replay.get("model_calls") == 0
        and replay_evidence
    )
    replay_acceptance = replay_functional and requested_replay >= int(
        manifest["minimum_replay_capture_count"]
    )
    requested_soak_ns = _safe_int(soak.get("requested_duration_ns"))
    observed_soak_ns = _safe_int(soak.get("observed_duration_ns"))
    sample_interval = _safe_number(soak.get("sample_interval_seconds"))
    worker = soak.get("worker") if isinstance(soak.get("worker"), dict) else {}
    worker_requested_ns = _safe_int(worker.get("requested_duration_ns"))
    worker_observed_ns = _safe_int(worker.get("observed_duration_ns"))
    worker_cycles = _safe_int(worker.get("cycles"))
    worker_indexed = _safe_int(worker.get("indexed_capture_count"))
    worker_terminal_snapshot = worker.get("terminal_resource_snapshot")
    parent_terminal_storage = soak.get("terminal_storage_snapshot")
    recalculated_terminal_checks = _terminal_resource_checks(
        worker_snapshot=worker_terminal_snapshot,
        parent_storage_snapshot=parent_terminal_storage,
        worker_cycles=worker_cycles,
        worker_indexed=worker_indexed,
        worker_observed_ns=worker_observed_ns,
        manifest=manifest,
    )
    process = soak.get("process") if isinstance(soak.get("process"), dict) else {}
    recalculated_timeline = (
        summarize_sample_timeline(
            samples,
            observed_duration_ns=observed_soak_ns,
            manifest=manifest,
        )
        if observed_soak_ns is not None
        else {"complete": False}
    )
    soak_evidence = (
        requested_soak_ns is not None
        and observed_soak_ns is not None
        and observed_soak_ns >= requested_soak_ns
        and worker_requested_ns == requested_soak_ns
        and worker_observed_ns is not None
        and requested_soak_ns <= worker_observed_ns <= observed_soak_ns
        and worker_cycles is not None
        and worker_cycles > 0
        and worker_indexed == worker_cycles
        and worker.get("completed_requested_duration") is True
        and worker.get("quick_check") == "ok"
        and worker.get("fts_integrity_check") == "ok"
        and worker.get("owned_temp_file_count") == 0
        and worker.get("model_calls") == 0
        and process.get("returncode") == 0
        and process.get("timed_out") is False
        and process.get("resource_limit_breaches") == []
        and sample_interval is not None
        and all(recalculated_terminal_checks.values())
        and all(
            soak_checks.get(key) is expected
            for key, expected in recalculated_terminal_checks.items()
        )
    )
    soak_functional = (
        soak.get("run_status") == "passed"
        and bool(soak_checks)
        and all(value is True for value in soak_checks.values())
        and soak.get("model_calls") == 0
        and recalculated_summary["coverage_complete"]
        and recalculated_summary["within_limits"]
        and recalculated_timeline["complete"]
        and soak.get("resource_summary") == recalculated_summary
        and soak.get("sample_timeline") == recalculated_timeline
        and soak_evidence
    )
    minimum_soak_ns = int(float(manifest["minimum_soak_duration_seconds"]) * 1_000_000_000)
    soak_acceptance = (
        soak_functional
        and requested_soak_ns >= minimum_soak_ns
        and observed_soak_ns >= minimum_soak_ns
        and worker_observed_ns >= minimum_soak_ns
        and sample_interval <= float(manifest["maximum_sample_interval_seconds"])
    )
    functional = manifest_matches and replay_functional and soak_functional
    storage_harness_complete = functional and replay_acceptance and soak_acceptance
    # Schema v1 does not observe a production daemon queue. A synchronous
    # synthetic worker has no queue, so zero would be an assertion, not
    # evidence. Full issue #2 completion stays closed until a later schema
    # carries independently verified daemon queue samples.
    production_daemon_queue_measured = False
    complete = storage_harness_complete and production_daemon_queue_measured
    status = "complete" if complete else ("incomplete" if functional else "failed")
    return {
        "status": status,
        "complete": complete,
        "storage_harness_complete": storage_harness_complete,
        "production_daemon_queue_measured": production_daemon_queue_measured,
        "manifest_matches": manifest_matches,
        "replay_functional": replay_functional,
        "replay_meets_minimum": replay_acceptance,
        "soak_functional": soak_functional,
        "soak_meets_24h_minimum": soak_acceptance,
        "recalculated_resource_summary": recalculated_summary,
        "recalculated_sample_timeline": recalculated_timeline,
        "recalculated_terminal_checks": recalculated_terminal_checks,
    }


def forbidden_report_key_paths(value: object, prefix: str = "$") -> list[str]:
    """Return report fields capable of carrying user/provider payloads or secrets."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}"
            if _FORBIDDEN_REPORT_KEYS.search(str(key)):
                found.append(child_path)
            found.extend(forbidden_report_key_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(forbidden_report_key_paths(child, f"{prefix}[{index}]"))
    return found


def _unknown_keys(value: object, allowed: set[str], prefix: str) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [f"{prefix}.{key}" for key in value if key not in allowed]


def unknown_report_key_paths(report: Mapping[str, Any]) -> list[str]:
    """Apply a closed report schema so innocuous keys cannot smuggle payloads."""
    unknown = _unknown_keys(
        report,
        {
            "schema_version",
            "kind",
            "generated_at",
            "manifest_sha256",
            "privacy",
            "replay",
            "soak",
            "status",
            "acceptance",
        },
        "$",
    )
    unknown.extend(
        _unknown_keys(
            report.get("privacy"),
            {
                "synthetic_data_only",
                "capture_fields_recorded",
                "environment_values_recorded",
                "model_calls",
            },
            "$.privacy",
        )
    )
    unknown.extend(
        _unknown_keys(
            report.get("acceptance"),
            {
                "complete",
                "storage_harness_complete",
                "replay_meets_10000_minimum",
                "soak_meets_24h_minimum",
                "production_daemon_queue_measured",
            },
            "$.acceptance",
        )
    )
    replay = report.get("replay")
    if isinstance(replay, dict) and "failure" in replay:
        unknown.extend(_unknown_keys(replay, {"run_status", "failure"}, "$.replay"))
        unknown.extend(
            _unknown_keys(replay.get("failure"), {"phase", "error_type"}, "$.replay.failure")
        )
    else:
        unknown.extend(
            _unknown_keys(
                replay,
                {
                    "run_status",
                    "requested_capture_count",
                    "model_calls",
                    "source_set_digest",
                    "expected_visible_set_digest",
                    "actual_visible_set_digest",
                    "visible_capture_count",
                    "fts_row_count",
                    "faults",
                    "reconcile",
                    "checks",
                    "sqlite",
                    "metrics",
                },
                "$.replay",
            )
        )
        unknown.extend(
            _unknown_keys(
                replay.get("faults") if isinstance(replay, dict) else None,
                {
                    "missing_projection_rows",
                    "stale_projection_rows",
                    "orphan_capture_temps",
                    "tombstoned_capture_files",
                },
                "$.replay.faults",
            )
        )
        reconcile = replay.get("reconcile") if isinstance(replay, dict) else None
        unknown.extend(_unknown_keys(reconcile, {"initial", "recovery", "idempotence"}, "$.replay.reconcile"))
        if isinstance(reconcile, dict):
            for name in ("initial", "recovery", "idempotence"):
                unknown.extend(
                    _unknown_keys(
                        reconcile.get(name),
                        {"scanned", "indexed", "removed", "skipped", "hidden"},
                        f"$.replay.reconcile.{name}",
                    )
                )
        unknown.extend(
            _unknown_keys(
                replay.get("checks") if isinstance(replay, dict) else None,
                {
                    "source_count_exact",
                    "deterministic_fixture_exact",
                    "faults_injected",
                    "temp_recovered",
                    "set_count_exact",
                    "fts_count_exact",
                    "set_digest_exact",
                    "stale_removed",
                    "missing_repaired",
                    "tombstone_hidden",
                    "sqlite_quick_check",
                    "fts_integrity_check",
                    "second_reconcile_idempotent",
                    "reconcile_accounting_exact",
                    "storage_accounting_exact",
                    "resource_limits_respected",
                },
                "$.replay.checks",
            )
        )
        unknown.extend(
            _unknown_keys(
                replay.get("sqlite") if isinstance(replay, dict) else None,
                {"quick_check", "fts_integrity_check", "checkpoint"},
                "$.replay.sqlite",
            )
        )
        replay_metrics = replay.get("metrics") if isinstance(replay, dict) else None
        unknown.extend(
            _unknown_keys(
                replay_metrics,
                {
                    "elapsed_monotonic_ns",
                    "after_fixture",
                    "after_fault_injection",
                    "final",
                    "rss_bytes",
                },
                "$.replay.metrics",
            )
        )
        if isinstance(replay_metrics, dict):
            storage_keys = {
                "db_bytes",
                "wal_bytes",
                "shm_bytes",
                "capture_buffer_bytes",
                "capture_file_count",
                "owned_temp_file_count",
                "owned_temp_bytes",
            }
            for name in ("after_fixture", "after_fault_injection", "final"):
                unknown.extend(
                    _unknown_keys(
                        replay_metrics.get(name),
                        storage_keys,
                        f"$.replay.metrics.{name}",
                    )
                )

    soak = report.get("soak")
    if isinstance(soak, dict) and "failure" in soak:
        unknown.extend(_unknown_keys(soak, {"run_status", "failure"}, "$.soak"))
        unknown.extend(
            _unknown_keys(soak.get("failure"), {"phase", "error_type"}, "$.soak.failure")
        )
    else:
        unknown.extend(
            _unknown_keys(
                soak,
                {
                    "run_status",
                    "requested_duration_ns",
                    "observed_duration_ns",
                    "sample_interval_seconds",
                    "work_interval_seconds",
                    "model_calls",
                    "worker",
                    "process",
                    "checks",
                    "samples",
                    "resource_summary",
                    "sample_timeline",
                    "terminal_storage_snapshot",
                },
                "$.soak",
            )
        )
        worker = soak.get("worker") if isinstance(soak, dict) else None
        unknown.extend(
            _unknown_keys(
                worker,
                {
                    "completed_requested_duration",
                    "requested_duration_ns",
                    "observed_duration_ns",
                    "cycles",
                    "indexed_capture_count",
                    "quick_check",
                    "fts_integrity_check",
                    "checkpoint",
                    "owned_temp_file_count",
                    "terminal_resource_snapshot",
                    "model_calls",
                },
                "$.soak.worker",
            )
        )
        unknown.extend(
            _unknown_keys(
                soak.get("process") if isinstance(soak, dict) else None,
                {
                    "returncode",
                    "timed_out",
                    "stop_disposition",
                    "resource_limit_breaches",
                },
                "$.soak.process",
            )
        )
        unknown.extend(
            _unknown_keys(
                soak.get("checks") if isinstance(soak, dict) else None,
                {
                    "worker_exit_zero",
                    "worker_report_bounded_and_valid",
                    "requested_duration_completed",
                    "sqlite_and_fts_healthy",
                    "worker_accounting_exact",
                    "resource_metric_coverage",
                    "resource_limits_respected",
                    "sample_interval_allowed",
                    "sample_timeline_covered",
                    "no_descendant_leak",
                    "terminal_snapshot_complete",
                    "terminal_snapshots_match",
                    "terminal_capture_accounting_exact",
                    "terminal_resource_limits_respected",
                },
                "$.soak.checks",
            )
        )
        sample_keys = {
            "elapsed_monotonic_ns",
            "rss_bytes",
            "cpu_percent",
            "fd_count",
            "thread_count",
            "child_process_count",
            "db_bytes",
            "wal_bytes",
            "shm_bytes",
            "capture_buffer_bytes",
            "capture_file_count",
            "owned_temp_file_count",
            "owned_temp_bytes",
        }
        samples = soak.get("samples") if isinstance(soak, dict) else None
        if isinstance(samples, list):
            for index, sample in enumerate(samples):
                unknown.extend(_unknown_keys(sample, sample_keys, f"$.soak.samples[{index}]"))
        if isinstance(worker, dict):
            unknown.extend(
                _unknown_keys(
                    worker.get("terminal_resource_snapshot"),
                    set(_TERMINAL_RESOURCE_FIELDS),
                    "$.soak.worker.terminal_resource_snapshot",
                )
            )
        unknown.extend(
            _unknown_keys(
                soak.get("terminal_storage_snapshot")
                if isinstance(soak, dict)
                else None,
                set(_STORAGE_RESOURCE_FIELDS),
                "$.soak.terminal_storage_snapshot",
            )
        )
        summary = soak.get("resource_summary") if isinstance(soak, dict) else None
        unknown.extend(
            _unknown_keys(
                summary,
                {"sample_count", "metrics", "coverage_complete", "within_limits"},
                "$.soak.resource_summary",
            )
        )
        metric_map = summary.get("metrics") if isinstance(summary, dict) else None
        unknown.extend(_unknown_keys(metric_map, set(_RESOURCE_FIELDS), "$.soak.resource_summary.metrics"))
        if isinstance(metric_map, dict):
            for name, metric in metric_map.items():
                unknown.extend(
                    _unknown_keys(
                        metric,
                        {"maximum", "limit", "coverage_ratio", "covered", "within_limit"},
                        f"$.soak.resource_summary.metrics.{name}",
                    )
                )
        unknown.extend(
            _unknown_keys(
                soak.get("sample_timeline") if isinstance(soak, dict) else None,
                {
                    "complete",
                    "sample_count",
                    "first_sample_gap_ns",
                    "maximum_adjacent_gap_ns",
                    "terminal_sample_gap_ns",
                    "maximum_allowed_gap_ns",
                    "maximum_allowed_terminal_gap_ns",
                    "strictly_monotonic",
                    "inside_observed_run",
                },
                "$.soak.sample_timeline",
            )
        )
    return unknown


def _nonnegative_int(value: object, *, allow_none: bool = False) -> bool:
    return (allow_none and value is None) or (
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
    )


def _finite_number(value: object, *, allow_none: bool = False) -> bool:
    return (allow_none and value is None) or _safe_number(value) is not None


def _exact_bool_map(value: object, keys: set[str]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == keys
        and all(isinstance(item, bool) for item in value.values())
    )


def _valid_checkpoint(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(_nonnegative_int(item) for item in value)
    )


def _valid_storage_metrics(value: object) -> bool:
    keys = {
        "db_bytes",
        "wal_bytes",
        "shm_bytes",
        "capture_buffer_bytes",
        "capture_file_count",
        "owned_temp_file_count",
        "owned_temp_bytes",
    }
    return (
        isinstance(value, dict)
        and set(value) == keys
        and all(_nonnegative_int(item) for item in value.values())
    )


def _valid_failure_phase(value: Mapping[str, Any], phase: str) -> bool:
    failure = value.get("failure")
    error_type = failure.get("error_type") if isinstance(failure, dict) else None
    return (
        value.get("run_status") == "failed"
        and isinstance(failure, dict)
        and set(failure) == {"phase", "error_type"}
        and failure.get("phase") == phase
        and error_type in {"audit", "sqlite", "io", "unexpected"}
    )


def _valid_replay_values(replay: Mapping[str, Any]) -> bool:
    if "failure" in replay:
        return _valid_failure_phase(replay, "replay")
    if replay.get("run_status") not in {"passed", "failed"}:
        return False
    for key in (
        "source_set_digest",
        "expected_visible_set_digest",
        "actual_visible_set_digest",
    ):
        digest = replay.get(key)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False
    if not all(
        _nonnegative_int(replay.get(key))
        for key in ("requested_capture_count", "visible_capture_count", "fts_row_count")
    ):
        return False
    if not _nonnegative_int(replay.get("model_calls")) or replay.get("model_calls") != 0:
        return False
    faults = replay.get("faults")
    fault_keys = {
        "missing_projection_rows",
        "stale_projection_rows",
        "orphan_capture_temps",
        "tombstoned_capture_files",
    }
    if not (
        isinstance(faults, dict)
        and set(faults) == fault_keys
        and all(_nonnegative_int(item) for item in faults.values())
    ):
        return False
    reconcile = replay.get("reconcile")
    reconcile_keys = {"scanned", "indexed", "removed", "skipped", "hidden"}
    if not isinstance(reconcile, dict) or set(reconcile) != {"initial", "recovery", "idempotence"}:
        return False
    for stage in reconcile.values():
        if not (
            isinstance(stage, dict)
            and set(stage) == reconcile_keys
            and all(_nonnegative_int(item) for item in stage.values())
        ):
            return False
    replay_check_keys = {
        "source_count_exact",
        "deterministic_fixture_exact",
        "faults_injected",
        "temp_recovered",
        "set_count_exact",
        "fts_count_exact",
        "set_digest_exact",
        "stale_removed",
        "missing_repaired",
        "tombstone_hidden",
        "sqlite_quick_check",
        "fts_integrity_check",
        "second_reconcile_idempotent",
        "reconcile_accounting_exact",
        "storage_accounting_exact",
        "resource_limits_respected",
    }
    if not _exact_bool_map(replay.get("checks"), replay_check_keys):
        return False
    sqlite_evidence = replay.get("sqlite")
    if not isinstance(sqlite_evidence, dict) or set(sqlite_evidence) != {
        "quick_check",
        "fts_integrity_check",
        "checkpoint",
    }:
        return False
    if sqlite_evidence.get("quick_check") not in {"ok", "failed", "missing"}:
        return False
    if sqlite_evidence.get("fts_integrity_check") not in {"ok", "failed"}:
        return False
    if not _valid_checkpoint(sqlite_evidence.get("checkpoint")):
        return False
    metrics = replay.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != {
        "elapsed_monotonic_ns",
        "after_fixture",
        "after_fault_injection",
        "final",
        "rss_bytes",
    }:
        return False
    return (
        _nonnegative_int(metrics.get("elapsed_monotonic_ns"))
        and _nonnegative_int(metrics.get("rss_bytes"), allow_none=True)
        and _valid_storage_metrics(metrics.get("after_fixture"))
        and _valid_storage_metrics(metrics.get("after_fault_injection"))
        and _valid_storage_metrics(metrics.get("final"))
    )


def _valid_soak_sample(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    integer_or_none = {
        "rss_bytes",
        "fd_count",
        "thread_count",
        "child_process_count",
    }
    storage_integers = {
        "elapsed_monotonic_ns",
        "db_bytes",
        "wal_bytes",
        "shm_bytes",
        "capture_buffer_bytes",
        "capture_file_count",
        "owned_temp_file_count",
        "owned_temp_bytes",
    }
    expected = integer_or_none | storage_integers | {"cpu_percent"}
    return (
        set(value) == expected
        and all(_nonnegative_int(value[key], allow_none=True) for key in integer_or_none)
        and all(_nonnegative_int(value[key]) for key in storage_integers)
        and _finite_number(value["cpu_percent"], allow_none=True)
    )


def _valid_terminal_resource_snapshot(value: object) -> bool:
    if not _valid_soak_sample(value):
        return False
    assert isinstance(value, dict)
    return all(
        value[field] is not None
        for field in (
            "rss_bytes",
            "cpu_percent",
            "fd_count",
            "thread_count",
            "child_process_count",
        )
    )


def _valid_resource_summary(value: object, sample_count: int) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "sample_count",
        "metrics",
        "coverage_complete",
        "within_limits",
    }:
        return False
    if not _nonnegative_int(value.get("sample_count")) or value.get("sample_count") != sample_count:
        return False
    if not isinstance(value.get("coverage_complete"), bool) or not isinstance(
        value.get("within_limits"), bool
    ):
        return False
    metrics = value.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != set(_RESOURCE_FIELDS):
        return False
    metric_keys = {"maximum", "limit", "coverage_ratio", "covered", "within_limit"}
    for metric in metrics.values():
        if not isinstance(metric, dict) or set(metric) != metric_keys:
            return False
        if not _finite_number(metric.get("maximum"), allow_none=True):
            return False
        if not _finite_number(metric.get("limit")):
            return False
        coverage = metric.get("coverage_ratio")
        if not _finite_number(coverage) or not 0 <= float(coverage) <= 1:
            return False
        if not isinstance(metric.get("covered"), bool) or not isinstance(
            metric.get("within_limit"), bool
        ):
            return False
    return True


def _valid_sample_timeline(value: object, sample_count: int) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "complete",
        "sample_count",
        "first_sample_gap_ns",
        "maximum_adjacent_gap_ns",
        "terminal_sample_gap_ns",
        "maximum_allowed_gap_ns",
        "maximum_allowed_terminal_gap_ns",
        "strictly_monotonic",
        "inside_observed_run",
    }:
        return False
    if not _nonnegative_int(value.get("sample_count")) or value.get("sample_count") != sample_count:
        return False
    if not all(
        isinstance(value.get(key), bool)
        for key in ("complete", "strictly_monotonic", "inside_observed_run")
    ):
        return False
    optional_gaps = (
        "first_sample_gap_ns",
        "maximum_adjacent_gap_ns",
        "terminal_sample_gap_ns",
    )
    required_gaps = ("maximum_allowed_gap_ns", "maximum_allowed_terminal_gap_ns")
    return all(_nonnegative_int(value.get(key), allow_none=True) for key in optional_gaps) and all(
        _nonnegative_int(value.get(key)) for key in required_gaps
    )


def _valid_soak_values(soak: Mapping[str, Any]) -> bool:
    if "failure" in soak:
        return _valid_failure_phase(soak, "soak")
    if soak.get("run_status") not in {"passed", "failed"}:
        return False
    if not _nonnegative_int(soak.get("model_calls")) or soak.get("model_calls") != 0:
        return False
    if not all(
        _nonnegative_int(soak.get(key))
        for key in ("requested_duration_ns", "observed_duration_ns")
    ):
        return False
    if not all(
        _finite_number(soak.get(key)) and float(soak[key]) > 0
        for key in ("sample_interval_seconds", "work_interval_seconds")
    ):
        return False
    worker = soak.get("worker")
    if worker is not None:
        if not isinstance(worker, dict) or set(worker) != {
            "completed_requested_duration",
            "requested_duration_ns",
            "observed_duration_ns",
            "cycles",
            "indexed_capture_count",
            "quick_check",
            "fts_integrity_check",
            "checkpoint",
            "owned_temp_file_count",
            "terminal_resource_snapshot",
            "model_calls",
        }:
            return False
        if not isinstance(worker.get("completed_requested_duration"), bool):
            return False
        if not all(
            _nonnegative_int(worker.get(key))
            for key in (
                "requested_duration_ns",
                "observed_duration_ns",
                "cycles",
                "indexed_capture_count",
                "owned_temp_file_count",
                "model_calls",
            )
        ):
            return False
        if worker.get("quick_check") not in {"ok", "failed", "missing"}:
            return False
        if worker.get("fts_integrity_check") not in {"ok", "failed"}:
            return False
        if not _valid_checkpoint(worker.get("checkpoint")):
            return False
        if not _valid_terminal_resource_snapshot(
            worker.get("terminal_resource_snapshot")
        ):
            return False
    process = soak.get("process")
    if not isinstance(process, dict) or set(process) != {
        "returncode",
        "timed_out",
        "stop_disposition",
        "resource_limit_breaches",
    }:
        return False
    returncode = process.get("returncode")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        return False
    if not isinstance(process.get("timed_out"), bool):
        return False
    if process.get("stop_disposition") not in {
        "not-needed",
        "already-exited",
        "terminated",
        "killed",
    }:
        return False
    breaches = process.get("resource_limit_breaches")
    if not isinstance(breaches, list) or any(
        not isinstance(item, str) or item not in _RESOURCE_FIELDS for item in breaches
    ):
        return False
    soak_check_keys = {
        "worker_exit_zero",
        "worker_report_bounded_and_valid",
        "requested_duration_completed",
        "sqlite_and_fts_healthy",
        "worker_accounting_exact",
        "resource_metric_coverage",
        "resource_limits_respected",
        "sample_interval_allowed",
        "sample_timeline_covered",
        "no_descendant_leak",
        "terminal_snapshot_complete",
        "terminal_snapshots_match",
        "terminal_capture_accounting_exact",
        "terminal_resource_limits_respected",
    }
    if not _exact_bool_map(soak.get("checks"), soak_check_keys):
        return False
    samples = soak.get("samples")
    if not isinstance(samples, list) or not all(_valid_soak_sample(sample) for sample in samples):
        return False
    return (
        _valid_storage_metrics(soak.get("terminal_storage_snapshot"))
        and _valid_resource_summary(soak.get("resource_summary"), len(samples))
        and _valid_sample_timeline(soak.get("sample_timeline"), len(samples))
    )


def _fixed_report_values_valid(report: Mapping[str, Any]) -> bool:
    if not _nonnegative_int(report.get("schema_version")) or report.get(
        "schema_version"
    ) != REPORT_SCHEMA_VERSION:
        return False
    if report.get("kind") != "openchronicle-runtime-reliability":
        return False
    privacy = report.get("privacy")
    if not isinstance(privacy, dict) or set(privacy) != {
        "synthetic_data_only",
        "capture_fields_recorded",
        "environment_values_recorded",
        "model_calls",
    }:
        return False
    if (
        privacy.get("synthetic_data_only") is not True
        or privacy.get("capture_fields_recorded") is not False
        or privacy.get("environment_values_recorded") is not False
        or not _nonnegative_int(privacy.get("model_calls"))
        or privacy.get("model_calls") != 0
    ):
        return False
    manifest_sha256 = report.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", manifest_sha256
    ):
        return False
    generated_at = report.get("generated_at")
    if not isinstance(generated_at, str):
        return False
    try:
        generated = datetime.fromisoformat(generated_at)
    except ValueError:
        return False
    if generated.tzinfo is None:
        return False
    if report.get("status") not in {"complete", "incomplete", "failed"}:
        return False
    acceptance_keys = {
        "complete",
        "storage_harness_complete",
        "replay_meets_10000_minimum",
        "soak_meets_24h_minimum",
        "production_daemon_queue_measured",
    }
    if not _exact_bool_map(report.get("acceptance"), acceptance_keys):
        return False
    replay = report.get("replay")
    soak = report.get("soak")
    if not isinstance(replay, dict) or not isinstance(soak, dict):
        return False
    return _valid_replay_values(replay) and _valid_soak_values(soak)


def verify_report(report: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(report, Mapping):
        return {
            "valid": False,
            "schema_valid": False,
            "forbidden_key_paths": forbidden_report_key_paths(report),
            "unknown_key_paths": [],
            "fixed_values_valid": False,
            "claims_consistent": False,
            "evaluation": None,
            "verification_scope": "closed-schema-and-internal-consistency-only",
            "authenticity_verified": False,
        }
    schema_valid = report.get("schema_version") == REPORT_SCHEMA_VERSION
    forbidden_keys = forbidden_report_key_paths(report)
    unknown_keys = unknown_report_key_paths(report)
    fixed_values_valid = _fixed_report_values_valid(report)
    try:
        evaluation = evaluate_report(report, manifest) if schema_valid else None
    except (KeyError, TypeError, ValueError, OverflowError):
        evaluation = None
    claimed_acceptance = report.get("acceptance")
    claims_consistent = bool(
        evaluation
        and report.get("status") == evaluation["status"]
        and isinstance(claimed_acceptance, dict)
        and claimed_acceptance.get("complete") == evaluation["complete"]
        and claimed_acceptance.get("storage_harness_complete")
        == evaluation["storage_harness_complete"]
        and claimed_acceptance.get("replay_meets_10000_minimum")
        == evaluation["replay_meets_minimum"]
        and claimed_acceptance.get("soak_meets_24h_minimum")
        == evaluation["soak_meets_24h_minimum"]
        and claimed_acceptance.get("production_daemon_queue_measured")
        == evaluation["production_daemon_queue_measured"]
    )
    return {
        "valid": (
            schema_valid
            and not forbidden_keys
            and not unknown_keys
            and fixed_values_valid
            and claims_consistent
        ),
        "schema_valid": schema_valid,
        "forbidden_key_paths": forbidden_keys,
        "unknown_key_paths": unknown_keys,
        "fixed_values_valid": fixed_values_valid,
        "claims_consistent": claims_consistent,
        "evaluation": evaluation,
        "verification_scope": "closed-schema-and-internal-consistency-only",
        "authenticity_verified": False,
    }


def run_full_audit(
    workspace_root: Path,
    *,
    replay_count: int,
    soak_duration_seconds: float,
    sample_interval_seconds: float,
    work_interval_seconds: float,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    root = _prepare_empty_root(workspace_root)
    try:
        replay = run_replay_audit(root / "replay", count=replay_count, manifest=manifest)
    except Exception as exc:  # noqa: BLE001 - report only a safe exception class
        replay = _failure_phase("replay", exc)
    try:
        soak = run_soak_audit(
            root / "soak",
            duration_seconds=soak_duration_seconds,
            sample_interval_seconds=sample_interval_seconds,
            work_interval_seconds=work_interval_seconds,
            manifest=manifest,
        )
    except Exception as exc:  # noqa: BLE001 - report only a safe exception class
        soak = _failure_phase("soak", exc)
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "openchronicle-runtime-reliability",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "manifest_sha256": manifest_digest(manifest),
        "privacy": {
            "synthetic_data_only": True,
            "capture_fields_recorded": False,
            "environment_values_recorded": False,
            "model_calls": 0,
        },
        "replay": replay,
        "soak": soak,
    }
    evaluation = evaluate_report(report, manifest)
    report["status"] = evaluation["status"]
    report["acceptance"] = {
        "complete": evaluation["complete"],
        "storage_harness_complete": evaluation["storage_harness_complete"],
        "replay_meets_10000_minimum": evaluation["replay_meets_minimum"],
        "soak_meets_24h_minimum": evaluation["soak_meets_24h_minimum"],
        "production_daemon_queue_measured": evaluation[
            "production_daemon_queue_measured"
        ],
    }
    return report


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
    target.chmod(0o600)


_SOAK_STOP_REQUESTED = False


def _request_soak_stop(_signum: int, _frame: object) -> None:
    global _SOAK_STOP_REQUESTED
    _SOAK_STOP_REQUESTED = True


def _atomic_worker_capture(directory: Path, *, index: int) -> tuple[str, dict[str, Any]]:
    stem, payload = _capture_record(index)
    target = directory / f"{stem}.json"
    encoded = _encoded_capture(payload)
    fd, temp_name = tempfile.mkstemp(
        dir=directory,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
    return stem, payload


def soak_worker(*, duration_seconds: float, work_interval_seconds: float) -> dict[str, Any]:
    """Internal single-process, zero-model soak workload."""
    global _SOAK_STOP_REQUESTED
    _SOAK_STOP_REQUESTED = False
    signal.signal(signal.SIGTERM, _request_soak_stop)
    signal.signal(signal.SIGINT, _request_soak_stop)
    root = paths.root()
    paths.ensure_dirs()
    started_ns = time.monotonic_ns()
    requested_ns = int(duration_seconds * 1_000_000_000)
    deadline_ns = started_ns + requested_ns
    cycles = 0
    conn = fts.connect()
    try:
        while not _SOAK_STOP_REQUESTED and time.monotonic_ns() < deadline_ns:
            stem, payload = _atomic_worker_capture(paths.capture_buffer_dir(), index=cycles)
            fts.insert_capture(
                conn,
                id=stem,
                observation_id=str(payload["observation_id"]),
                timestamp=str(payload["timestamp"]),
                app_name="RuntimeReplay",
                bundle_id="org.openchronicle.runtime-replay",
                window_title=f"deterministic-window-{cycles:08d}",
                focused_role="AXTextArea",
                focused_value=f"deterministic-focused-record-{cycles:08d}",
                visible_text=f"deterministic-replay-record-{cycles:08d}",
                url="",
            )
            cycles += 1
            if cycles % 120 == 0:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            remaining_ns = deadline_ns - time.monotonic_ns()
            if remaining_ns > 0:
                time.sleep(min(work_interval_seconds, remaining_ns / 1_000_000_000))
    finally:
        conn.close()

    capture_scheduler.cleanup_buffer(retention_hours=168)
    capture_reconcile.reconcile_capture_index()
    checkpoint = fts.checkpoint("TRUNCATE")
    with fts.cursor() as conn:
        indexed = int(conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
        quick_check, fts_check = _sqlite_health(conn)
    terminal_resource_snapshot = _sample_current_worker_terminal(
        root,
        elapsed_ns=time.monotonic_ns() - started_ns,
    )
    observed_ns = time.monotonic_ns() - started_ns
    return {
        "completed_requested_duration": (
            not _SOAK_STOP_REQUESTED and observed_ns >= requested_ns
        ),
        "requested_duration_ns": requested_ns,
        "observed_duration_ns": observed_ns,
        "cycles": cycles,
        "indexed_capture_count": indexed,
        "quick_check": quick_check,
        "fts_integrity_check": fts_check,
        "checkpoint": list(checkpoint),
        "owned_temp_file_count": terminal_resource_snapshot["owned_temp_file_count"],
        "terminal_resource_snapshot": terminal_resource_snapshot,
        "model_calls": 0,
    }
