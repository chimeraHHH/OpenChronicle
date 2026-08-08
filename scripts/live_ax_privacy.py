"""Redaction-safe primitives for the opt-in macOS live AX privacy audit.

This module deliberately has no OpenChronicle imports.  Unit tests can exercise
the report contract on every platform, while the live runner imports production
capture modules only after it has redirected ``OPENCHRONICLE_ROOT`` to a
throw-away directory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

REPORT_SCHEMA_VERSION = 1
PUBLIC_VISUAL_CANARY = (20, 194, 77)
PRIVATE_VISUAL_CANARY = (224, 20, 179)
_VISUAL_COLOR_TOLERANCE = 55
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Marker:
    """A canary held in memory while only its digest is reportable."""

    value: str
    classification: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("marker value must not be empty")
        if self.classification not in {"control", "forbidden"}:
            raise ValueError("marker classification must be control or forbidden")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.value.encode("utf-8")).hexdigest()


@dataclass
class _Sink:
    units_scanned: int = 0
    bytes_scanned: int = 0
    unavailable_units: int = 0
    hits: dict[str, int] = field(default_factory=dict)


class SinkScanner:
    """Count marker occurrences without retaining or returning source text."""

    def __init__(self, markers: Sequence[Marker]) -> None:
        if not markers:
            raise ValueError("at least one marker is required")
        digests = [marker.sha256 for marker in markers]
        if len(digests) != len(set(digests)):
            raise ValueError("marker values must be distinct")
        self._markers = tuple(markers)
        self._sinks: dict[str, _Sink] = {}

    def scan_text(self, sink: str, text: str, *, units: int = 1) -> None:
        raw = text.encode("utf-8", errors="replace")
        self.scan_bytes(sink, raw, units=units)

    def scan_bytes(self, sink: str, raw: bytes, *, units: int = 1) -> None:
        state = self._sinks.setdefault(sink, _Sink())
        state.units_scanned += max(0, int(units))
        state.bytes_scanned += len(raw)
        for marker in self._markers:
            needle = marker.value.encode("utf-8")
            count = raw.count(needle)
            if count:
                state.hits[marker.sha256] = state.hits.get(marker.sha256, 0) + count

    def scan_files(self, sink: str, paths: Iterable[Path]) -> None:
        state = self._sinks.setdefault(sink, _Sink())
        for path in sorted(paths):
            try:
                raw = path.read_bytes()
            except OSError:
                state.unavailable_units += 1
                continue
            self.scan_bytes(sink, raw)

    def scan_sqlite(self, sink: str, db_path: Path, tables: Sequence[str]) -> None:
        """Scan scalar values from selected SQLite/FTS tables read-only.

        Virtual FTS tables are queried through their public table names.  Blob
        shadow tables are intentionally not needed: querying the virtual table
        yields the same searchable text and avoids recording opaque index data.
        """

        state = self._sinks.setdefault(sink, _Sink())
        if not db_path.is_file():
            state.unavailable_units += len(tables)
            return
        for table in tables:
            if not _IDENTIFIER_RE.fullmatch(table):
                raise ValueError(f"unsafe SQLite table name: {table!r}")

        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            state.unavailable_units += len(tables)
            return
        try:
            existing = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            for table in tables:
                if table not in existing:
                    state.unavailable_units += 1
                    continue
                try:
                    rows = conn.execute(f'SELECT * FROM "{table}"')
                    for row in rows:
                        # JSON is used only as an in-memory, deterministic scalar
                        # separator; this serialized row is never written.
                        values = [_safe_scalar(value) for value in row]
                        self.scan_text(
                            sink,
                            json.dumps(values, ensure_ascii=False, separators=(",", ":")),
                        )
                except sqlite3.Error:
                    state.unavailable_units += 1
        finally:
            conn.close()

    def report(self) -> dict[str, dict[str, Any]]:
        marker_meta = {
            marker.sha256: marker.classification for marker in self._markers
        }
        out: dict[str, dict[str, Any]] = {}
        for sink, state in sorted(self._sinks.items()):
            markers = [
                {
                    "sha256": digest,
                    "classification": marker_meta[digest],
                    "hits": state.hits.get(digest, 0),
                }
                for digest in sorted(marker_meta)
            ]
            out[sink] = {
                "units_scanned": state.units_scanned,
                "bytes_scanned": state.bytes_scanned,
                "unavailable_units": state.unavailable_units,
                "markers": markers,
            }
        return out


def analyze_exact_window_jpeg(shot: Any, target: Any) -> dict[str, int | bool]:
    """Prove pixels are the public fixture window, not a metadata-labelled screen."""

    metrics: dict[str, int | bool] = {
        "jpeg_valid": False,
        "aspect_matches": False,
        "public_pixels": 0,
        "private_pixels": 0,
        "public_samples": 0,
        "private_samples": 0,
        "pixel_count": 0,
    }
    if shot is None or target.bounds is None:
        return metrics
    try:
        from PIL import Image
    except ImportError:
        return metrics

    try:
        encoded = base64.b64decode(shot.image_base64, validate=True)
        with Image.open(BytesIO(encoded)) as image:
            if image.format != "JPEG" or image.width <= 0 or image.height <= 0:
                return metrics
            image.load()
            metrics["jpeg_valid"] = True
            expected_aspect = target.bounds.width / target.bounds.height
            actual_aspect = image.width / image.height
            metrics["aspect_matches"] = (
                abs(actual_aspect - expected_aspect) / expected_aspect <= 0.08
            )
            rgb = image.convert("RGB")
            rgb.thumbnail((256, 256), Image.Resampling.BILINEAR)
            flat_pixels = rgb.tobytes()
            pixels = list(
                zip(
                    flat_pixels[0::3],
                    flat_pixels[1::3],
                    flat_pixels[2::3],
                    strict=True,
                )
            )
            metrics["pixel_count"] = len(pixels)
            metrics["public_pixels"] = sum(
                _near_color(pixel, PUBLIC_VISUAL_CANARY) for pixel in pixels
            )
            metrics["private_pixels"] = sum(
                _near_color(pixel, PRIVATE_VISUAL_CANARY) for pixel in pixels
            )
            sample_points = (
                (0.03, 0.45),
                (0.97, 0.45),
                (0.03, 0.80),
                (0.97, 0.80),
                (0.50, 0.78),
            )
            sampled = [
                rgb.getpixel(
                    (
                        min(rgb.width - 1, max(0, int(rgb.width * x))),
                        min(rgb.height - 1, max(0, int(rgb.height * y))),
                    )
                )
                for x, y in sample_points
            ]
            metrics["public_samples"] = sum(
                _near_color(pixel, PUBLIC_VISUAL_CANARY) for pixel in sampled
            )
            metrics["private_samples"] = sum(
                _near_color(pixel, PRIVATE_VISUAL_CANARY) for pixel in sampled
            )
    except (
        OSError,
        ValueError,
        TypeError,
        SyntaxError,
        OverflowError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return metrics
    return metrics


def _near_color(pixel: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    return all(
        abs(int(observed) - channel) <= _VISUAL_COLOR_TOLERANCE
        for observed, channel in zip(pixel, expected, strict=True)
    )


def marker_hit_count(
    sinks: Mapping[str, Mapping[str, Any]],
    sink: str,
    *,
    classification: str,
) -> int:
    record = sinks.get(sink, {})
    markers = record.get("markers", [])
    if not isinstance(markers, list):
        return 0
    total = 0
    for marker in markers:
        if not isinstance(marker, dict):
            continue
        if marker.get("classification") != classification:
            continue
        hits = marker.get("hits", 0)
        if isinstance(hits, int) and not isinstance(hits, bool):
            total += max(0, hits)
    return total


def safe_report_bytes(report: Mapping[str, Any], markers: Sequence[Marker]) -> bytes:
    """Serialize a report and prove that no plaintext canary survived."""

    raw = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    for marker in markers:
        if marker.value.encode("utf-8") in raw:
            raise ValueError("report contains a plaintext audit marker")
    return raw


def write_private_report(path: Path, report: Mapping[str, Any], markers: Sequence[Marker]) -> None:
    """Write the final redacted report with owner-only permissions."""

    raw = safe_report_bytes(report, markers)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_bytes(raw)
    path.chmod(0o600)


def verify_report(
    report: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    allow_incomplete: bool = False,
) -> list[str]:
    """Return stable violation codes for the public live-audit contract."""

    violations: list[str] = []
    expected_schema = manifest.get("report_schema_version")
    if report.get("schema_version") != expected_schema:
        violations.append("schema_version_mismatch")

    status = report.get("status")
    if status not in {"complete", "incomplete"}:
        violations.append("invalid_audit_status")
    elif status != "complete" and not allow_incomplete:
        violations.append("audit_incomplete")

    policy = report.get("artifact_policy")
    if not isinstance(policy, dict):
        violations.append("artifact_policy_missing")
    else:
        if policy.get("plaintext_markers_retained") != 0:
            violations.append("plaintext_markers_retained")
        if policy.get("raw_ax_payloads_retained") != 0:
            violations.append("raw_ax_payloads_retained")
        if policy.get("temporary_root_removed") is not True:
            violations.append("temporary_root_not_removed")

    marker_digests_raw = report.get("marker_digests")
    marker_digests: set[str] = set()
    if not isinstance(marker_digests_raw, list) or not marker_digests_raw:
        violations.append("marker_digests_missing")
    else:
        for digest in marker_digests_raw:
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                violations.append("invalid_marker_digest")
            else:
                marker_digests.add(digest)
        if len(marker_digests_raw) != len(marker_digests):
            violations.append("duplicate_marker_digest")

    checks_raw = report.get("checks")
    checks: dict[str, Mapping[str, Any]] = {}
    seen_check_ids: set[str] = set()
    if isinstance(checks_raw, list):
        for item in checks_raw:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                violations.append("invalid_check_record")
                continue
            check_id = item["id"]
            if check_id in seen_check_ids:
                violations.append(f"duplicate_check:{check_id}")
                continue
            seen_check_ids.add(check_id)
            if item.get("status") not in {"pass", "fail", "skipped"}:
                violations.append(f"invalid_check_status:{check_id}")
            if not _is_nonnegative_int(item.get("observed_count")):
                violations.append(f"invalid_check_count:{check_id}")
            checks[check_id] = item
    else:
        violations.append("checks_missing")
    for check_id in _string_list(manifest.get("required_checks")):
        check = checks.get(check_id)
        if check is None:
            if not allow_incomplete:
                violations.append(f"check_missing:{check_id}")
        elif check.get("status") != "pass" and not allow_incomplete:
            violations.append(f"check_not_passed:{check_id}")

    sinks_raw = report.get("sinks")
    sinks = sinks_raw if isinstance(sinks_raw, dict) else {}
    if not isinstance(sinks_raw, dict):
        violations.append("sinks_missing")
    marker_classifications: dict[str, str] = {}
    for sink, record in sinks.items():
        if not isinstance(sink, str) or not isinstance(record, dict):
            violations.append("invalid_sink_record")
            continue
        for metric in ("units_scanned", "bytes_scanned", "unavailable_units"):
            if not _is_nonnegative_int(record.get(metric)):
                violations.append(f"invalid_sink_metric:{sink}:{metric}")
        if status == "complete" and record.get("unavailable_units") != 0:
            violations.append(f"sink_unavailable:{sink}")

        markers = record.get("markers")
        if not isinstance(markers, list):
            violations.append(f"sink_markers_missing:{sink}")
            continue
        seen_sink_digests: set[str] = set()
        for marker in markers:
            if not isinstance(marker, dict):
                violations.append(f"invalid_sink_marker:{sink}")
                continue
            digest = marker.get("sha256")
            classification = marker.get("classification")
            hits = marker.get("hits")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                violations.append(f"invalid_sink_marker_digest:{sink}")
                continue
            if digest in seen_sink_digests:
                violations.append(f"duplicate_sink_marker:{sink}")
            seen_sink_digests.add(digest)
            if classification not in {"control", "forbidden"}:
                violations.append(f"invalid_sink_marker_classification:{sink}")
            elif (
                digest in marker_classifications
                and marker_classifications[digest] != classification
            ):
                violations.append(f"inconsistent_marker_classification:{digest}")
            else:
                marker_classifications[digest] = classification
            if not _is_nonnegative_int(hits):
                violations.append(f"invalid_sink_marker_hits:{sink}")
        if marker_digests and seen_sink_digests != marker_digests:
            violations.append(f"sink_marker_set_mismatch:{sink}")

    for sink in _string_list(manifest.get("required_sinks")):
        record = sinks.get(sink)
        if not isinstance(record, dict):
            if not allow_incomplete:
                violations.append(f"sink_missing:{sink}")
            continue
        units = record.get("units_scanned")
        if (
            not isinstance(units, int) or isinstance(units, bool) or units < 1
        ) and not allow_incomplete:
            violations.append(f"sink_not_observed:{sink}")

    for sink in _string_list(manifest.get("forbidden_zero_sinks")):
        if sink not in sinks and not allow_incomplete:
            violations.append(f"sink_missing:{sink}")
        if marker_hit_count(sinks, sink, classification="forbidden") != 0:
            violations.append(f"forbidden_marker_hit:{sink}")

    minimum_counts = manifest.get("minimum_marker_counts", {})
    if isinstance(minimum_counts, dict) and not allow_incomplete:
        for classification in ("control", "forbidden"):
            minimum = minimum_counts.get(classification, 0)
            if not _is_nonnegative_int(minimum):
                violations.append(f"invalid_manifest_marker_minimum:{classification}")
                continue
            observed = sum(
                value == classification for value in marker_classifications.values()
            )
            if observed < minimum:
                violations.append(f"insufficient_{classification}_markers")

    return sorted(set(violations))


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
