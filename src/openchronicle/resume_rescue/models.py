"""Closed, canonical source schemas for Résumé Rescue."""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from ..provenance.models import canonical_digest

PROFILE_FIELDS = {
    "schema_version",
    "profile_id",
    "display_name",
    "locale",
    "facts",
    "conflicts",
}
FACT_FIELDS = {
    "id",
    "section",
    "text",
    "confidentiality",
    "ownership_scope",
    "provenance",
}
CONFLICT_FIELDS = {"id", "fact_ids", "description"}
OPPORTUNITY_FIELDS = {
    "schema_version",
    "employer",
    "title",
    "source_url",
    "source_text",
    "priorities",
    "locale",
    "captured_at",
}

VALID_SECTIONS = {
    "summary",
    "experience",
    "education",
    "skill",
    "project",
    "certification",
    "language",
    "other",
}
VALID_CONFIDENTIALITY = {"public", "private", "confidential"}
VALID_OWNERSHIP = {"individual", "shared", "organization", "unspecified"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ResumeSchemaError(ValueError):
    """An object does not match the closed Résumé Rescue schema."""


def validate_profile(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PROFILE_FIELDS:
        raise ResumeSchemaError("resume profile must use the closed schema")
    if value.get("schema_version") != 1:
        raise ResumeSchemaError("resume profile schema version is unsupported")
    profile_id = _identifier(value.get("profile_id"), "profile_id")
    display_name = _text(value.get("display_name"), "display_name", maximum=512)
    locale = _text(value.get("locale"), "locale", maximum=64, required=False)
    facts_raw = value.get("facts")
    conflicts_raw = value.get("conflicts")
    if not isinstance(facts_raw, list) or len(facts_raw) > 2_000:
        raise ResumeSchemaError("resume facts must be a bounded list")
    if not isinstance(conflicts_raw, list) or len(conflicts_raw) > 500:
        raise ResumeSchemaError("resume conflicts must be a bounded list")

    facts = [_validate_fact(item) for item in facts_raw]
    fact_ids = [item["id"] for item in facts]
    if len(set(fact_ids)) != len(fact_ids):
        raise ResumeSchemaError("resume fact IDs must be unique")
    conflicts = [_validate_conflict(item, set(fact_ids)) for item in conflicts_raw]
    conflict_ids = [item["id"] for item in conflicts]
    if len(set(conflict_ids)) != len(conflict_ids):
        raise ResumeSchemaError("resume conflict IDs must be unique")

    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "display_name": display_name,
        "locale": locale,
        "facts": facts,
        "conflicts": conflicts,
    }


def validate_opportunity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != OPPORTUNITY_FIELDS:
        raise ResumeSchemaError("resume opportunity must use the closed schema")
    if value.get("schema_version") != 1:
        raise ResumeSchemaError("resume opportunity schema version is unsupported")
    source_url = _text(value.get("source_url"), "source_url", maximum=4_096, required=False)
    if source_url:
        parsed = urlparse(source_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ResumeSchemaError("resume opportunity source URL is invalid")
    priorities = _string_list(
        value.get("priorities"), "priorities", maximum_items=50, maximum_chars=2_000
    )
    return {
        "schema_version": 1,
        "employer": _text(value.get("employer"), "employer", maximum=512),
        "title": _text(value.get("title"), "title", maximum=512),
        "source_url": source_url,
        "source_text": _text(value.get("source_text"), "source_text", maximum=200_000),
        "priorities": priorities,
        "locale": _text(value.get("locale"), "locale", maximum=64, required=False),
        "captured_at": _timestamp(value.get("captured_at"), "captured_at"),
    }


def profile_digest(value: dict[str, Any]) -> str:
    return canonical_digest({"schema": "resume-profile-v1", "profile": value})


def opportunity_digest(value: dict[str, Any]) -> str:
    return canonical_digest({"schema": "resume-opportunity-v1", "opportunity": value})


def _validate_fact(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != FACT_FIELDS:
        raise ResumeSchemaError("resume fact must use the closed schema")
    section = value.get("section")
    confidentiality = value.get("confidentiality")
    ownership = value.get("ownership_scope")
    if section not in VALID_SECTIONS:
        raise ResumeSchemaError("resume fact section is invalid")
    if confidentiality not in VALID_CONFIDENTIALITY:
        raise ResumeSchemaError("resume fact confidentiality is invalid")
    if ownership not in VALID_OWNERSHIP:
        raise ResumeSchemaError("resume fact ownership scope is invalid")
    provenance_raw = value.get("provenance")
    if not isinstance(provenance_raw, list) or not provenance_raw or len(provenance_raw) > 20:
        raise ResumeSchemaError("resume fact provenance must be a non-empty bounded list")
    provenance = [_validate_provenance(item) for item in provenance_raw]
    if len({canonical_digest(item) for item in provenance}) != len(provenance):
        raise ResumeSchemaError("resume fact provenance entries must be unique")
    return {
        "id": _identifier(value.get("id"), "fact id"),
        "section": section,
        "text": _text(value.get("text"), "fact text", maximum=8_000),
        "confidentiality": confidentiality,
        "ownership_scope": ownership,
        "provenance": provenance,
    }


def _validate_provenance(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResumeSchemaError("resume fact provenance must be an object")
    kind = value.get("kind")
    if kind == "manual_reviewed":
        if set(value) != {"kind", "reviewed_at"}:
            raise ResumeSchemaError("manual provenance must use the closed schema")
        return {"kind": kind, "reviewed_at": _timestamp(value.get("reviewed_at"), "reviewed_at")}
    if kind == "document_excerpt":
        expected = {
            "kind",
            "reviewed_at",
            "source_id",
            "source_digest",
            "page",
            "section",
            "start",
            "end",
            "extraction_method",
        }
        if set(value) != expected:
            raise ResumeSchemaError("document provenance must use the closed schema")
        page = value.get("page")
        start = value.get("start")
        end = value.get("end")
        if type(page) is not int or page < 0 or page > 100_000:
            raise ResumeSchemaError("document provenance page is invalid")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= 10_000_000:
            raise ResumeSchemaError("document provenance span is invalid")
        return {
            "kind": kind,
            "reviewed_at": _timestamp(value.get("reviewed_at"), "reviewed_at"),
            "source_id": _identifier(value.get("source_id"), "source_id"),
            "source_digest": _digest(value.get("source_digest"), "source_digest"),
            "page": page,
            "section": _text(value.get("section"), "source section", maximum=512, required=False),
            "start": start,
            "end": end,
            "extraction_method": _text(
                value.get("extraction_method"), "extraction_method", maximum=128
            ),
        }
    if kind == "reviewed_memory":
        expected = {
            "kind",
            "reviewed_at",
            "memory_id",
            "memory_path",
            "memory_digest",
        }
        if set(value) != expected:
            raise ResumeSchemaError("memory provenance must use the closed schema")
        return {
            "kind": kind,
            "reviewed_at": _timestamp(value.get("reviewed_at"), "reviewed_at"),
            "memory_id": _identifier(value.get("memory_id"), "memory_id"),
            "memory_path": _text(value.get("memory_path"), "memory_path", maximum=1_024),
            "memory_digest": _digest(value.get("memory_digest"), "memory_digest"),
        }
    raise ResumeSchemaError("resume fact provenance kind is invalid")


def _validate_conflict(value: object, fact_ids: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != CONFLICT_FIELDS:
        raise ResumeSchemaError("resume conflict must use the closed schema")
    ids = value.get("fact_ids")
    if (
        not isinstance(ids, list)
        or len(ids) < 2
        or len(ids) > 20
        or not all(isinstance(item, str) and item in fact_ids for item in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ResumeSchemaError("resume conflict must bind distinct known facts")
    return {
        "id": _identifier(value.get("id"), "conflict id"),
        "fact_ids": copy.deepcopy(ids),
        "description": _text(value.get("description"), "conflict description", maximum=2_000),
    }


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ResumeSchemaError(f"resume {name} is invalid")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ResumeSchemaError(f"resume {name} is invalid")
    return value


def _text(value: object, name: str, *, maximum: int, required: bool = True) -> str:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or len(value) > maximum
        or (required and not value.strip())
    ):
        raise ResumeSchemaError(f"resume {name} is invalid")
    return value


def _string_list(value: object, name: str, *, maximum_items: int, maximum_chars: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ResumeSchemaError(f"resume {name} must be a bounded list")
    result = [_text(item, name, maximum=maximum_chars) for item in value]
    if len(set(result)) != len(result):
        raise ResumeSchemaError(f"resume {name} must not contain duplicates")
    return result


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) > 100 or "\x00" in value:
        raise ResumeSchemaError(f"resume {name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResumeSchemaError(f"resume {name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResumeSchemaError(f"resume {name} must be timezone-aware")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")
