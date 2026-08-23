"""Typed semantic metadata for durable Markdown memory entries."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

AssertionKind = Literal["user_asserted", "observed", "inferred"]
TemporalState = Literal["current", "scheduled", "expired"]

ASSERTION_KINDS: frozenset[str] = frozenset(
    {"user_asserted", "observed", "inferred"}
)


@dataclass(frozen=True, slots=True)
class FactMetadata:
    """Stable fact slot and belief/valid-time semantics stored in Markdown."""

    subject_key: str
    assertion_kind: AssertionKind
    valid_from: str = ""
    valid_to: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "subject_key": self.subject_key,
            "assertion_kind": self.assertion_kind,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
        }

    @classmethod
    def from_dict(cls, value: object) -> FactMetadata:
        if not isinstance(value, dict) or set(value) != {
            "subject_key",
            "assertion_kind",
            "valid_from",
            "valid_to",
        }:
            raise ValueError("fact metadata has an invalid shape")
        if any(not isinstance(item, str) for item in value.values()):
            raise ValueError("fact metadata fields must be strings")
        return make_fact_metadata(
            subject_key=value["subject_key"],
            assertion_kind=value["assertion_kind"],
            valid_from=value["valid_from"],
            valid_to=value["valid_to"],
        )


def make_fact_metadata(
    *,
    subject_key: str,
    assertion_kind: str,
    valid_from: str = "",
    valid_to: str = "",
) -> FactMetadata:
    clean_subject = normalize_subject_key(subject_key)
    clean_assertion = assertion_kind.strip().casefold().replace("-", "_")
    if clean_assertion not in ASSERTION_KINDS:
        raise ValueError(
            "assertion_kind must be user_asserted, observed, or inferred"
        )
    clean_from = _normalize_boundary(valid_from)
    clean_to = _normalize_boundary(valid_to)
    if (
        clean_from
        and clean_to
        and _boundary_datetime(clean_to) < _boundary_datetime(clean_from)
    ):
        raise ValueError("valid_to must not be earlier than valid_from")
    return FactMetadata(
        subject_key=clean_subject,
        assertion_kind=clean_assertion,  # type: ignore[arg-type]
        valid_from=clean_from,
        valid_to=clean_to,
    )


def normalize_subject_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("subject_key must be a string")
    clean = unicodedata.normalize("NFKC", value).strip().casefold()
    clean = re.sub(r"\s+", "-", clean)
    if not clean:
        raise ValueError("subject_key is required")
    if len(clean) > 200 or any(unicodedata.category(char) == "Cc" for char in clean):
        raise ValueError("subject_key is invalid")
    return clean


def temporal_state(metadata: FactMetadata, *, as_of: datetime) -> TemporalState:
    instant = _aware(as_of)
    if metadata.valid_from and instant < _boundary_datetime(
        metadata.valid_from, fallback_tz=instant.tzinfo
    ):
        return "scheduled"
    if metadata.valid_to and instant >= _boundary_datetime(
        metadata.valid_to, fallback_tz=instant.tzinfo
    ):
        return "expired"
    return "current"


def _normalize_boundary(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("valid-time boundaries must be strings")
    clean = value.strip()
    if not clean:
        return ""
    _boundary_datetime(clean)
    return clean


def _boundary_datetime(value: str, *, fallback_tz: Any = UTC) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("valid-time boundaries must be ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=fallback_tz or UTC)
    return parsed.astimezone(UTC)


def _aware(value: datetime) -> datetime:
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)
