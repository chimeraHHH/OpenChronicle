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
PROJECTION_REQUEST_FIELDS = {"schema_version", "sections", "requirements"}
REQUEST_SECTION_FIELDS = {"kind", "fact_ids"}
REQUEST_REQUIREMENT_FIELDS = {"id", "text", "fact_ids"}
ARTIFACT_FIELDS = {
    "schema_version",
    "workflow",
    "action_capability",
    "generation_mode",
    "profile_binding",
    "opportunity_binding",
    "sections",
    "requirement_coverage",
    "conflicts",
    "missing_evidence",
    "excluded_fact_ids",
    "warnings",
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


def validate_projection_request(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PROJECTION_REQUEST_FIELDS:
        raise ResumeSchemaError("resume projection request must use the closed schema")
    if value.get("schema_version") != 1:
        raise ResumeSchemaError("resume projection request version is unsupported")
    sections_raw = value.get("sections")
    requirements_raw = value.get("requirements")
    if not isinstance(sections_raw, list) or len(sections_raw) > len(VALID_SECTIONS):
        raise ResumeSchemaError("resume projection sections must be a bounded list")
    if not isinstance(requirements_raw, list) or len(requirements_raw) > 200:
        raise ResumeSchemaError("resume projection requirements must be a bounded list")

    sections: list[dict[str, Any]] = []
    seen_sections: set[str] = set()
    selected_ids: list[str] = []
    for raw in sections_raw:
        if not isinstance(raw, dict) or set(raw) != REQUEST_SECTION_FIELDS:
            raise ResumeSchemaError("resume projection section must use the closed schema")
        kind = raw.get("kind")
        if kind not in VALID_SECTIONS or kind in seen_sections:
            raise ResumeSchemaError("resume projection section kind is invalid or duplicated")
        ids = _identifier_list(raw.get("fact_ids"), "section fact IDs", maximum_items=2_000)
        seen_sections.add(kind)
        selected_ids.extend(ids)
        sections.append({"kind": kind, "fact_ids": ids})
    if len(set(selected_ids)) != len(selected_ids):
        raise ResumeSchemaError("resume projection facts may appear only once")

    requirements: list[dict[str, Any]] = []
    seen_requirements: set[str] = set()
    for raw in requirements_raw:
        if not isinstance(raw, dict) or set(raw) != REQUEST_REQUIREMENT_FIELDS:
            raise ResumeSchemaError("resume requirement must use the closed schema")
        requirement_id = _identifier(raw.get("id"), "requirement id")
        if requirement_id in seen_requirements:
            raise ResumeSchemaError("resume requirement IDs must be unique")
        seen_requirements.add(requirement_id)
        requirements.append(
            {
                "id": requirement_id,
                "text": _text(raw.get("text"), "requirement text", maximum=5_000),
                "fact_ids": _identifier_list(
                    raw.get("fact_ids"), "requirement fact IDs", maximum_items=50
                ),
            }
        )
    return {"schema_version": 1, "sections": sections, "requirements": requirements}


def build_exact_artifact(
    *,
    profile: dict[str, Any],
    profile_version: int,
    profile_digest_value: str,
    opportunity: dict[str, Any],
    opportunity_id: str,
    opportunity_digest_value: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    normalized_profile = validate_profile(profile)
    normalized_opportunity = validate_opportunity(opportunity)
    normalized_request = validate_projection_request(request)
    if type(profile_version) is not int or profile_version < 1:
        raise ResumeSchemaError("resume profile version is invalid")
    _digest(profile_digest_value, "profile digest")
    _identifier(opportunity_id, "opportunity id")
    _digest(opportunity_digest_value, "opportunity digest")

    facts = {item["id"]: item for item in normalized_profile["facts"]}
    selected_ids = [
        fact_id for section in normalized_request["sections"] for fact_id in section["fact_ids"]
    ]
    if any(fact_id not in facts for fact_id in selected_ids):
        raise ResumeSchemaError("resume projection selected an unknown fact")
    conflict_fact_ids = {
        fact_id for conflict in normalized_profile["conflicts"] for fact_id in conflict["fact_ids"]
    }
    if conflict_fact_ids.intersection(selected_ids):
        raise ResumeSchemaError("resume projection cannot select an unresolved conflict")
    selected_set = set(selected_ids)

    output_sections = []
    for section in normalized_request["sections"]:
        items = []
        for fact_id in section["fact_ids"]:
            fact = facts[fact_id]
            if fact["section"] != section["kind"]:
                raise ResumeSchemaError("resume projection fact is in the wrong section")
            items.append(
                {
                    "fact_id": fact_id,
                    "text": fact["text"],
                    "transformation": "selected_exact",
                    "confidentiality": fact["confidentiality"],
                    "ownership_scope": fact["ownership_scope"],
                    "provenance": copy.deepcopy(fact["provenance"]),
                }
            )
        output_sections.append({"kind": section["kind"], "items": items})

    coverage = []
    missing = []
    for requirement in normalized_request["requirements"]:
        if requirement["text"] not in normalized_opportunity["source_text"]:
            raise ResumeSchemaError("resume requirement is not an exact opportunity excerpt")
        mapped_ids = requirement["fact_ids"]
        if any(fact_id not in selected_set for fact_id in mapped_ids):
            raise ResumeSchemaError("resume requirement mapped an unselected fact")
        status = "candidate_supported" if mapped_ids else "missing_evidence"
        coverage.append(
            {
                "id": requirement["id"],
                "text": requirement["text"],
                "status": status,
                "fact_ids": copy.deepcopy(mapped_ids),
                "support_assurance": ("manual_mapping_unverified" if mapped_ids else "no_evidence"),
            }
        )
        if not mapped_ids:
            missing.append({"requirement_id": requirement["id"], "text": requirement["text"]})

    warnings = [
        "Requirement mappings require review; no ATS or hiring outcome is claimed.",
        "Opportunity text was treated as untrusted data; embedded instructions were not executed.",
    ]
    selected_facts = [facts[fact_id] for fact_id in selected_ids]
    if any(item["confidentiality"] != "public" for item in selected_facts):
        warnings.append("Review private or confidential facts before any export.")
    if any(item["ownership_scope"] != "individual" for item in selected_facts):
        warnings.append("Review shared or non-individual ownership wording before any export.")

    artifact = {
        "schema_version": 1,
        "workflow": "resume_rescue",
        "action_capability": "none",
        "generation_mode": "deterministic_exact_projection",
        "profile_binding": {
            "id": normalized_profile["profile_id"],
            "version": profile_version,
            "digest": profile_digest_value,
        },
        "opportunity_binding": {
            "id": opportunity_id,
            "digest": opportunity_digest_value,
            "employer": normalized_opportunity["employer"],
            "title": normalized_opportunity["title"],
        },
        "sections": output_sections,
        "requirement_coverage": coverage,
        "conflicts": copy.deepcopy(normalized_profile["conflicts"]),
        "missing_evidence": missing,
        "excluded_fact_ids": [
            fact["id"] for fact in normalized_profile["facts"] if fact["id"] not in selected_set
        ],
        "warnings": warnings,
    }
    return validate_artifact(artifact)


def validate_artifact(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != ARTIFACT_FIELDS:
        raise ResumeSchemaError("resume artifact must use the closed schema")
    if (
        value.get("schema_version") != 1
        or value.get("workflow") != "resume_rescue"
        or value.get("action_capability") != "none"
        or value.get("generation_mode") != "deterministic_exact_projection"
    ):
        raise ResumeSchemaError("resume artifact identity is invalid")
    profile_binding = value.get("profile_binding")
    if not isinstance(profile_binding, dict) or set(profile_binding) != {"id", "version", "digest"}:
        raise ResumeSchemaError("resume artifact profile binding is invalid")
    profile_id = _identifier(profile_binding.get("id"), "profile binding id")
    profile_version = profile_binding.get("version")
    if type(profile_version) is not int or profile_version < 1:
        raise ResumeSchemaError("resume artifact profile binding version is invalid")
    profile_hash = _digest(profile_binding.get("digest"), "profile binding digest")

    opportunity_binding = value.get("opportunity_binding")
    if not isinstance(opportunity_binding, dict) or set(opportunity_binding) != {
        "id",
        "digest",
        "employer",
        "title",
    }:
        raise ResumeSchemaError("resume artifact opportunity binding is invalid")
    opportunity_id = _identifier(opportunity_binding.get("id"), "opportunity binding id")
    opportunity_hash = _digest(opportunity_binding.get("digest"), "opportunity binding digest")
    employer = _text(opportunity_binding.get("employer"), "employer", maximum=512)
    title = _text(opportunity_binding.get("title"), "title", maximum=512)

    sections_raw = value.get("sections")
    if not isinstance(sections_raw, list) or len(sections_raw) > len(VALID_SECTIONS):
        raise ResumeSchemaError("resume artifact sections are invalid")
    sections = []
    selected_ids: list[str] = []
    seen_sections: set[str] = set()
    for raw in sections_raw:
        if not isinstance(raw, dict) or set(raw) != {"kind", "items"}:
            raise ResumeSchemaError("resume artifact section is invalid")
        kind = raw.get("kind")
        items_raw = raw.get("items")
        if kind not in VALID_SECTIONS or kind in seen_sections or not isinstance(items_raw, list):
            raise ResumeSchemaError("resume artifact section is invalid")
        items = [_validate_artifact_item(item) for item in items_raw]
        if any(item["fact_id"] in selected_ids for item in items):
            raise ResumeSchemaError("resume artifact fact IDs must be unique")
        selected_ids.extend(item["fact_id"] for item in items)
        seen_sections.add(kind)
        sections.append({"kind": kind, "items": items})

    excluded = _identifier_list(
        value.get("excluded_fact_ids"), "excluded fact IDs", maximum_items=2_000
    )
    if set(selected_ids).intersection(excluded):
        raise ResumeSchemaError("resume artifact selected and excluded facts overlap")
    all_fact_ids = set(selected_ids).union(excluded)
    conflicts_raw = value.get("conflicts")
    if not isinstance(conflicts_raw, list) or len(conflicts_raw) > 500:
        raise ResumeSchemaError("resume artifact conflicts are invalid")
    conflicts = [_validate_conflict(item, all_fact_ids) for item in conflicts_raw]
    if len({item["id"] for item in conflicts}) != len(conflicts):
        raise ResumeSchemaError("resume artifact conflict IDs must be unique")

    coverage_raw = value.get("requirement_coverage")
    if not isinstance(coverage_raw, list) or len(coverage_raw) > 200:
        raise ResumeSchemaError("resume artifact requirement coverage is invalid")
    coverage = [_validate_artifact_coverage(item, set(selected_ids)) for item in coverage_raw]
    if len({item["id"] for item in coverage}) != len(coverage):
        raise ResumeSchemaError("resume artifact requirement IDs must be unique")
    missing_raw = value.get("missing_evidence")
    if not isinstance(missing_raw, list) or len(missing_raw) > 200:
        raise ResumeSchemaError("resume artifact missing evidence is invalid")
    missing = []
    for raw in missing_raw:
        if not isinstance(raw, dict) or set(raw) != {"requirement_id", "text"}:
            raise ResumeSchemaError("resume artifact missing evidence is invalid")
        missing.append(
            {
                "requirement_id": _identifier(raw.get("requirement_id"), "missing requirement id"),
                "text": _text(raw.get("text"), "missing requirement text", maximum=5_000),
            }
        )
    expected_missing = [
        {"requirement_id": item["id"], "text": item["text"]}
        for item in coverage
        if item["status"] == "missing_evidence"
    ]
    if missing != expected_missing:
        raise ResumeSchemaError("resume artifact missing evidence ledger differs")
    warnings = _string_list(
        value.get("warnings"), "artifact warnings", maximum_items=20, maximum_chars=2_000
    )
    return {
        "schema_version": 1,
        "workflow": "resume_rescue",
        "action_capability": "none",
        "generation_mode": "deterministic_exact_projection",
        "profile_binding": {"id": profile_id, "version": profile_version, "digest": profile_hash},
        "opportunity_binding": {
            "id": opportunity_id,
            "digest": opportunity_hash,
            "employer": employer,
            "title": title,
        },
        "sections": sections,
        "requirement_coverage": coverage,
        "conflicts": conflicts,
        "missing_evidence": missing,
        "excluded_fact_ids": excluded,
        "warnings": warnings,
    }


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
    if kind == "json_resume_field":
        expected = {
            "kind",
            "reviewed_at",
            "source_id",
            "source_digest",
            "json_pointer",
            "value_digest",
            "mapping",
            "upstream_schema_version",
        }
        if set(value) != expected:
            raise ResumeSchemaError("JSON Resume provenance must use the closed schema")
        mapping = value.get("mapping")
        if mapping not in {
            "exact_field",
            "deterministic_composite",
            "openchronicle_extension_exact",
        }:
            raise ResumeSchemaError("JSON Resume provenance mapping is invalid")
        pointer = value.get("json_pointer")
        if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 1_024:
            raise ResumeSchemaError("JSON Resume provenance pointer is invalid")
        return {
            "kind": kind,
            "reviewed_at": _timestamp(value.get("reviewed_at"), "reviewed_at"),
            "source_id": _identifier(value.get("source_id"), "source_id"),
            "source_digest": _digest(value.get("source_digest"), "source_digest"),
            "json_pointer": pointer,
            "value_digest": _digest(value.get("value_digest"), "value_digest"),
            "mapping": mapping,
            "upstream_schema_version": _text(
                value.get("upstream_schema_version"),
                "upstream_schema_version",
                maximum=64,
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


def _validate_artifact_item(value: object) -> dict[str, Any]:
    expected = {
        "fact_id",
        "text",
        "transformation",
        "confidentiality",
        "ownership_scope",
        "provenance",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ResumeSchemaError("resume artifact item must use the closed schema")
    if value.get("transformation") != "selected_exact":
        raise ResumeSchemaError("resume artifact transformation is invalid")
    confidentiality = value.get("confidentiality")
    ownership = value.get("ownership_scope")
    if confidentiality not in VALID_CONFIDENTIALITY or ownership not in VALID_OWNERSHIP:
        raise ResumeSchemaError("resume artifact fact policy is invalid")
    provenance_raw = value.get("provenance")
    if not isinstance(provenance_raw, list) or not provenance_raw or len(provenance_raw) > 20:
        raise ResumeSchemaError("resume artifact fact provenance is invalid")
    provenance = [_validate_provenance(item) for item in provenance_raw]
    return {
        "fact_id": _identifier(value.get("fact_id"), "artifact fact id"),
        "text": _text(value.get("text"), "artifact fact text", maximum=8_000),
        "transformation": "selected_exact",
        "confidentiality": confidentiality,
        "ownership_scope": ownership,
        "provenance": provenance,
    }


def _validate_artifact_coverage(value: object, selected_ids: set[str]) -> dict[str, Any]:
    expected = {"id", "text", "status", "fact_ids", "support_assurance"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ResumeSchemaError("resume artifact requirement coverage is invalid")
    status = value.get("status")
    assurance = value.get("support_assurance")
    ids = _identifier_list(value.get("fact_ids"), "coverage fact IDs", maximum_items=50)
    if any(fact_id not in selected_ids for fact_id in ids):
        raise ResumeSchemaError("resume artifact coverage mapped an unknown fact")
    if (status, assurance, bool(ids)) not in {
        ("candidate_supported", "manual_mapping_unverified", True),
        ("missing_evidence", "no_evidence", False),
    }:
        raise ResumeSchemaError("resume artifact coverage assurance is invalid")
    return {
        "id": _identifier(value.get("id"), "coverage requirement id"),
        "text": _text(value.get("text"), "coverage requirement text", maximum=5_000),
        "status": status,
        "fact_ids": ids,
        "support_assurance": assurance,
    }


def _identifier_list(value: object, name: str, *, maximum_items: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ResumeSchemaError(f"resume {name} must be a bounded list")
    result = [_identifier(item, name) for item in value]
    if len(set(result)) != len(result):
        raise ResumeSchemaError(f"resume {name} must not contain duplicates")
    return result


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
