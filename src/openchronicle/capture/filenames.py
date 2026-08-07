"""Stable capture filenames that preserve ordering and timezone information."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_CAPTURE_STEM_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<hour>\d{2})-(?P<minute>\d{2})-(?P<second>\d{2})"
    r"(?P<fraction>\.\d+)?"
    # The legacy scheduler encoded negative offsets as ``-05-00`` even
    # though older readers expected ``m05-00``. Accept both permanently.
    r"(?:(?:(?P<sign>[pm])|(?P<legacy_negative>-))"
    r"(?P<offset_hour>\d{2})-(?P<offset_minute>\d{2}))?"
    r"(?:_obs_[0-9a-f]+)?$"
)


def normalize_datetime(value: datetime) -> datetime:
    """Return an offset-aware local datetime for safe comparisons."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.astimezone()
    return value


def parse_timestamp(value: str) -> datetime | None:
    """Parse an ISO timestamp and normalize legacy naive values."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return normalize_datetime(parsed)


def safe_timestamp(value: str) -> str:
    """Encode an ISO timestamp as a filename-safe, round-trippable string."""
    parsed = parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"invalid ISO timestamp: {value!r}")

    stem = parsed.strftime("%Y-%m-%dT%H-%M-%S")
    if parsed.microsecond:
        stem += f".{parsed.microsecond:06d}".rstrip("0")

    offset = parsed.utcoffset()
    if offset is not None:
        total_minutes = int(offset.total_seconds() // 60)
        sign = "p" if total_minutes >= 0 else "m"
        total_minutes = abs(total_minutes)
        hours, minutes = divmod(total_minutes, 60)
        stem += f"{sign}{hours:02d}-{minutes:02d}"
    return stem


def capture_stem(timestamp: str, observation_id: str) -> str:
    """Build a unique capture stem without weakening timestamp ordering."""
    if not re.fullmatch(r"obs_[0-9a-f]+", observation_id):
        raise ValueError(f"invalid observation id: {observation_id!r}")
    return f"{safe_timestamp(timestamp)}_{observation_id}"


def parse_capture_stem(stem: str) -> datetime | None:
    """Parse both legacy timestamp-only and v3 timestamp+observation stems."""
    match = _CAPTURE_STEM_RE.fullmatch(stem)
    if match is None:
        return None

    fraction = match.group("fraction") or ""
    iso = (
        f"{match.group('date')}T{match.group('hour')}:"
        f"{match.group('minute')}:{match.group('second')}{fraction}"
    )

    sign = "m" if match.group("legacy_negative") else match.group("sign")
    if sign:
        try:
            offset = timedelta(
                hours=int(match.group("offset_hour")),
                minutes=int(match.group("offset_minute")),
            )
            if sign == "m":
                offset = -offset
            return datetime.fromisoformat(iso).replace(tzinfo=timezone(offset))
        except (OverflowError, ValueError):
            return None

    # Older captures may have omitted an offset. Interpret them in the
    # machine's local timezone so comparisons with current aware values work.
    try:
        return datetime.fromisoformat(iso).astimezone()
    except (OverflowError, ValueError):
        return None


def is_capture_temp_name(name: str) -> bool:
    """Recognize only temp files produced by scheduler ``mkstemp`` writes."""
    if not name.startswith(".") or not name.endswith(".tmp"):
        return False
    payload = name[1:-4]
    try:
        stem, nonce = payload.rsplit(".json.", 1)
    except ValueError:
        return False
    return bool(nonce) and "." not in nonce and parse_capture_stem(stem) is not None
