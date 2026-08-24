"""Reviewed JSON Resume interoperability without automatic source admission."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..provenance.models import canonical_digest
from .models import (
    VALID_CONFIDENTIALITY,
    VALID_OWNERSHIP,
    VALID_SECTIONS,
    validate_artifact,
    validate_profile,
)
from .store import ProfileVersion, ResumeProjection

UPSTREAM_SCHEMA_VERSION = "v1.0.0"
UPSTREAM_SCHEMA_COMMIT = "272929d51b450dbd5a0d242af24c60252904f405"
UPSTREAM_SCHEMA_URL = (
    "https://raw.githubusercontent.com/jsonresume/jsonresume.org/"
    f"{UPSTREAM_SCHEMA_COMMIT}/packages/schema/schema.json"
)
IMPORT_REVIEW_SCHEMA_VERSION = 1
EXPORT_SCHEMA_VERSION = 1
MAX_SOURCE_BYTES = 500_000
MAX_SOURCE_STRING_CHARS = 50_000
MAX_CANDIDATE_CHARS = 8_000
MAX_CANDIDATES = 2_000
MAX_ARRAY_ITEMS = 500
MAX_SOURCE_FIELDS = 20
MAX_UNKNOWN_FIELDS = 2_000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DATE = re.compile(r"(?:[12][0-9]{3})(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12][0-9]|3[01]))?)?\Z")

_ROOT_FIELDS = {
    "$schema",
    "basics",
    "work",
    "volunteer",
    "education",
    "awards",
    "certificates",
    "publications",
    "skills",
    "languages",
    "interests",
    "references",
    "projects",
    "meta",
}
_BASICS_FIELDS = {
    "name",
    "label",
    "image",
    "email",
    "phone",
    "url",
    "summary",
    "location",
    "profiles",
}
_LOCATION_FIELDS = {"address", "postalCode", "city", "countryCode", "region"}
_PROFILE_FIELDS = {"network", "username", "url"}
_SECTION_FIELDS = {
    "work": {
        "name",
        "location",
        "description",
        "position",
        "url",
        "startDate",
        "endDate",
        "summary",
        "highlights",
    },
    "volunteer": {
        "organization",
        "position",
        "url",
        "startDate",
        "endDate",
        "summary",
        "highlights",
    },
    "education": {
        "institution",
        "url",
        "area",
        "studyType",
        "startDate",
        "endDate",
        "score",
        "courses",
    },
    "awards": {"title", "date", "awarder", "summary"},
    "certificates": {"name", "date", "url", "issuer"},
    "publications": {"name", "publisher", "releaseDate", "url", "summary"},
    "skills": {"name", "level", "keywords"},
    "languages": {"language", "fluency"},
    "interests": {"name", "keywords"},
    "references": {"name", "reference"},
    "projects": {
        "name",
        "description",
        "highlights",
        "keywords",
        "startDate",
        "endDate",
        "url",
        "roles",
        "entity",
        "type",
    },
}
_LIST_FIELDS = {"highlights", "courses", "keywords", "roles"}
_DATE_FIELDS = {"startDate", "endDate", "date", "releaseDate"}
_META_FIELDS = {"canonical", "version", "lastModified", "openchronicle"}


class JsonResumeError(ValueError):
    """A JSON Resume source or review selection is malformed or stale."""


@dataclass(frozen=True, slots=True)
class JsonResumeImportReview:
    source_id: str
    source_digest: str
    source_byte_count: int
    display_name_candidate: str
    candidates: tuple[dict[str, Any], ...]
    omissions: tuple[dict[str, Any], ...]
    unknown_fields: tuple[str, ...]
    warnings: tuple[str, ...]
    review_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": IMPORT_REVIEW_SCHEMA_VERSION,
            "format": "json_resume_v1",
            "upstream_schema": {
                "version": UPSTREAM_SCHEMA_VERSION,
                "commit": UPSTREAM_SCHEMA_COMMIT,
                "url": UPSTREAM_SCHEMA_URL,
            },
            "source": {
                "id": self.source_id,
                "digest": self.source_digest,
                "byte_count": self.source_byte_count,
            },
            "display_name_candidate": self.display_name_candidate,
            "candidates": copy.deepcopy(list(self.candidates)),
            "omissions": copy.deepcopy(list(self.omissions)),
            "unknown_fields": list(self.unknown_fields),
            "warnings": list(self.warnings),
            "action_capability": "none",
            "review_digest": self.review_digest,
        }


@dataclass(frozen=True, slots=True)
class JsonResumeExport:
    projection_id: str
    artifact_digest: str
    profile_id: str
    profile_version: int
    profile_digest: str
    document: dict[str, Any]
    json_text: str
    document_digest: str
    interoperability_losses: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "format": "json_resume_v1",
            "upstream_schema": {
                "version": UPSTREAM_SCHEMA_VERSION,
                "commit": UPSTREAM_SCHEMA_COMMIT,
                "url": UPSTREAM_SCHEMA_URL,
            },
            "projection_binding": {
                "id": self.projection_id,
                "artifact_digest": self.artifact_digest,
            },
            "profile_binding": {
                "id": self.profile_id,
                "version": self.profile_version,
                "digest": self.profile_digest,
            },
            "document": copy.deepcopy(self.document),
            "json_text": self.json_text,
            "document_digest": self.document_digest,
            "interoperability_losses": copy.deepcopy(list(self.interoperability_losses)),
            "warnings": list(self.warnings),
            "action_capability": "none",
        }


def parse_json_resume(source_text: str) -> JsonResumeImportReview:
    """Map an untrusted JSON Resume source into an unadmitted review ledger."""

    if not isinstance(source_text, str) or "\x00" in source_text:
        raise JsonResumeError("JSON Resume source text is invalid")
    source_bytes = source_text.encode("utf-8")
    if not 2 <= len(source_bytes) <= MAX_SOURCE_BYTES:
        raise JsonResumeError("JSON Resume source size is invalid")
    document = _loads_strict(source_text)
    _validate_json_tree(document)
    unknown_fields = _validate_standard_shape(document)
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    source_id = f"json-resume-{source_digest[:32]}"
    builder = _ImportBuilder(source_id=source_id, source_digest=source_digest)

    basics = _optional_object(document, "basics")
    display_name = _string(basics, "name") if basics else ""
    extension = _openchronicle_extension(document)
    if extension is not None:
        _map_openchronicle_extension(builder, extension)
        builder.warnings.append(
            "OpenChronicle extension data is untrusted and still requires explicit review."
        )
    else:
        _map_standard_document(builder, document)

    for pointer in unknown_fields:
        builder.omit(pointer, "unknown_extension_not_mapped", _pointer_value(document, pointer))
    if unknown_fields:
        builder.warnings.append(
            "Unknown extension fields were preserved only as paths and were not admitted."
        )
    if any(item["reason"].startswith("contact_") for item in builder.omissions):
        builder.warnings.append(
            "Contact, profile, image, and location fields were not admitted as résumé facts."
        )
    if any(item["mapping"] == "deterministic_composite" for item in builder.candidates):
        builder.warnings.append(
            "Composite candidates must be compared with every displayed source field."
        )
    candidate_texts = [item["suggested_text"] for item in builder.candidates]
    if len(candidate_texts) != len(set(candidate_texts)):
        builder.warnings.append("Duplicate candidate text is present and requires review.")
    builder.warnings.extend(
        [
            "JSON Resume content was treated as untrusted data; embedded instructions were not executed.",
            "No candidate enters the master profile until the user explicitly admits it.",
        ]
    )
    payload = _review_payload(
        source_id=source_id,
        source_digest=source_digest,
        source_byte_count=len(source_bytes),
        display_name_candidate=display_name,
        candidates=builder.candidates,
        omissions=builder.omissions,
        unknown_fields=unknown_fields,
        warnings=builder.warnings,
    )
    review_digest = canonical_digest({"schema": "json-resume-import-review-v1", "review": payload})
    return JsonResumeImportReview(
        source_id=source_id,
        source_digest=source_digest,
        source_byte_count=len(source_bytes),
        display_name_candidate=display_name,
        candidates=tuple(copy.deepcopy(builder.candidates)),
        omissions=tuple(copy.deepcopy(builder.omissions)),
        unknown_fields=tuple(unknown_fields),
        warnings=tuple(builder.warnings),
        review_digest=review_digest,
    )


def admit_json_resume_candidates(
    review: JsonResumeImportReview,
    selections: list[dict[str, Any]],
    *,
    reviewed_at: str,
) -> list[dict[str, Any]]:
    """Turn explicitly selected review candidates into profile fact payloads."""

    if not isinstance(review, JsonResumeImportReview):
        raise JsonResumeError("JSON Resume review is invalid")
    expected_review_digest = canonical_digest(
        {
            "schema": "json-resume-import-review-v1",
            "review": _review_payload(
                source_id=review.source_id,
                source_digest=review.source_digest,
                source_byte_count=review.source_byte_count,
                display_name_candidate=review.display_name_candidate,
                candidates=list(review.candidates),
                omissions=list(review.omissions),
                unknown_fields=list(review.unknown_fields),
                warnings=list(review.warnings),
            ),
        }
    )
    if expected_review_digest != review.review_digest:
        raise JsonResumeError("JSON Resume review changed since parsing")
    _reviewed_timestamp(reviewed_at)
    if not isinstance(selections, list) or len(selections) > MAX_CANDIDATES:
        raise JsonResumeError("JSON Resume selections must be a bounded list")
    candidates = {item["id"]: item for item in review.candidates}
    seen_candidates: set[str] = set()
    seen_facts: set[str] = set()
    facts = []
    for selection in selections:
        expected = {
            "candidate_id",
            "fact_id",
            "section",
            "confidentiality",
            "ownership_scope",
        }
        if not isinstance(selection, dict) or set(selection) != expected:
            raise JsonResumeError("JSON Resume selection must use the closed schema")
        candidate_id = selection.get("candidate_id")
        fact_id = selection.get("fact_id")
        section = selection.get("section")
        confidentiality = selection.get("confidentiality")
        ownership = selection.get("ownership_scope")
        if (
            not isinstance(candidate_id, str)
            or candidate_id not in candidates
            or candidate_id in seen_candidates
            or not isinstance(fact_id, str)
            or _ID.fullmatch(fact_id) is None
            or fact_id in seen_facts
            or section not in VALID_SECTIONS
            or confidentiality not in VALID_CONFIDENTIALITY
            or ownership not in VALID_OWNERSHIP
        ):
            raise JsonResumeError("JSON Resume selection binding is invalid")
        candidate = candidates[candidate_id]
        provenance = [
            {
                "kind": "json_resume_field",
                "reviewed_at": reviewed_at,
                "source_id": review.source_id,
                "source_digest": review.source_digest,
                "json_pointer": source["pointer"],
                "value_digest": canonical_digest(
                    {
                        "schema": "json-resume-source-field-v1",
                        "pointer": source["pointer"],
                        "value": source["value"],
                    }
                ),
                "mapping": candidate["mapping"],
                "upstream_schema_version": UPSTREAM_SCHEMA_VERSION,
            }
            for source in candidate["source_fields"]
        ]
        facts.append(
            {
                "id": fact_id,
                "section": section,
                "text": candidate["suggested_text"],
                "confidentiality": confidentiality,
                "ownership_scope": ownership,
                "provenance": provenance,
            }
        )
        seen_candidates.add(candidate_id)
        seen_facts.add(fact_id)

    validate_profile(
        {
            "schema_version": 1,
            "profile_id": "json-resume-admission-check",
            "display_name": "JSON Resume admission check",
            "locale": "",
            "facts": facts,
            "conflicts": [],
        }
    )
    return facts


def export_projection_json_resume(
    *,
    profile: ProfileVersion,
    projection: ResumeProjection,
) -> JsonResumeExport:
    """Export only one selected projection, never the whole master profile."""

    normalized_profile = validate_profile(profile.profile)
    artifact = validate_artifact(projection.artifact)
    expected_binding = {
        "id": profile.profile_id,
        "version": profile.version,
        "digest": profile.digest,
    }
    if (
        projection.profile_id != profile.profile_id
        or projection.profile_version != profile.version
        or projection.profile_digest != profile.digest
        or artifact["profile_binding"] != expected_binding
    ):
        raise JsonResumeError("JSON Resume export profile binding differs")

    items = [
        item | {"section": section["kind"]}
        for section in artifact["sections"]
        for item in section["items"]
    ]
    standard: dict[str, Any] = {
        "$schema": UPSTREAM_SCHEMA_URL,
        "basics": {"name": normalized_profile["display_name"]},
    }
    losses = []
    summaries = [item for item in items if item["section"] == "summary"]
    if summaries:
        standard["basics"]["summary"] = "\n\n".join(item["text"] for item in summaries)
        if len(summaries) > 1:
            losses.extend(
                _loss(item, "multiple_summary_facts_joined_in_standard_field") for item in summaries
            )
    section_targets = {
        "skill": ("skills", "name"),
        "certification": ("certificates", "name"),
        "language": ("languages", "language"),
    }
    for section, (target, key) in section_targets.items():
        values = [{key: item["text"]} for item in items if item["section"] == section]
        if values:
            standard[target] = values
    for item in items:
        if item["section"] in {"experience", "education", "project", "other"}:
            losses.append(_loss(item, "no_safe_flat_fact_mapping_in_standard_schema"))

    extension_items = [
        {
            "fact_id": item["fact_id"],
            "section": item["section"],
            "text": item["text"],
            "confidentiality": item["confidentiality"],
            "ownership_scope": item["ownership_scope"],
        }
        for item in items
    ]
    standard["meta"] = {
        "version": UPSTREAM_SCHEMA_VERSION,
        "lastModified": projection.created_at,
        "openchronicle": {
            "schema_version": 1,
            "kind": "openchronicle_resume_projection",
            "action_capability": "none",
            "projection_binding": {
                "id": projection.id,
                "artifact_digest": projection.artifact_digest,
            },
            "profile_binding": expected_binding,
            "items": extension_items,
            "interoperability_losses": losses,
        },
    }
    warnings = [
        "External tools may ignore the OpenChronicle extension and lose explicitly listed facts.",
        "No application upload, submission, or other external action is authorized.",
    ]
    if losses:
        warnings.insert(0, "Review the interoperability loss ledger before export.")
    if any(item["confidentiality"] != "public" for item in items):
        warnings.insert(0, "The selected projection contains private or confidential facts.")
    if any(item["ownership_scope"] != "individual" for item in items):
        warnings.insert(0, "The selected projection contains shared or non-individual wording.")
    json_text = json.dumps(standard, ensure_ascii=False, indent=2) + "\n"
    document_digest = hashlib.sha256(json_text.encode("utf-8")).hexdigest()
    return JsonResumeExport(
        projection_id=projection.id,
        artifact_digest=projection.artifact_digest,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        profile_digest=profile.digest,
        document=standard,
        json_text=json_text,
        document_digest=document_digest,
        interoperability_losses=tuple(losses),
        warnings=tuple(warnings),
    )


class _ImportBuilder:
    def __init__(self, *, source_id: str, source_digest: str) -> None:
        self.source_id = source_id
        self.source_digest = source_digest
        self.candidates: list[dict[str, Any]] = []
        self.omissions: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def exact(self, section: str, text: str, pointer: str) -> None:
        self._candidate(
            section=section,
            text=text,
            mapping="exact_field",
            source_fields=[{"pointer": pointer, "value": text}],
        )

    def composite(
        self,
        section: str,
        text: str,
        source_fields: list[dict[str, str]],
    ) -> None:
        self._candidate(
            section=section,
            text=text,
            mapping="deterministic_composite",
            source_fields=source_fields,
        )

    def extension(self, section: str, text: str, pointer: str) -> None:
        self._candidate(
            section=section,
            text=text,
            mapping="openchronicle_extension_exact",
            source_fields=[{"pointer": pointer, "value": text}],
        )

    def _candidate(
        self,
        *,
        section: str,
        text: str,
        mapping: str,
        source_fields: list[dict[str, str]],
    ) -> None:
        if not text:
            return
        if len(text) > MAX_CANDIDATE_CHARS:
            self.omit(source_fields[0]["pointer"], "candidate_text_too_long", text)
            return
        if len(source_fields) > MAX_SOURCE_FIELDS:
            self.omit(
                source_fields[MAX_SOURCE_FIELDS]["pointer"],
                "candidate_source_field_limit_exceeded",
                [item["value"] for item in source_fields[MAX_SOURCE_FIELDS:]],
            )
            source_fields = source_fields[:MAX_SOURCE_FIELDS]
        if len(self.candidates) >= MAX_CANDIDATES:
            raise JsonResumeError("JSON Resume candidate count exceeds limit")
        sequence = len(self.candidates) + 1
        candidate_id = f"json-resume-candidate-{self.source_digest[:12]}-{sequence:04d}"
        self.candidates.append(
            {
                "id": candidate_id,
                "suggested_section": section,
                "suggested_text": text,
                "mapping": mapping,
                "source_fields": copy.deepcopy(source_fields),
                "review_status": "unreviewed",
            }
        )

    def omit(self, pointer: str, reason: str, value: object) -> None:
        self.omissions.append(
            {
                "pointer": pointer,
                "reason": reason,
                "value_digest": canonical_digest(
                    {"schema": "json-resume-omitted-value-v1", "value": value}
                ),
            }
        )


def _map_standard_document(builder: _ImportBuilder, document: dict[str, Any]) -> None:
    basics = _optional_object(document, "basics")
    if basics:
        _exact_field(builder, "summary", basics, "label", "/basics/label")
        _exact_field(builder, "summary", basics, "summary", "/basics/summary")
        for key in ("image", "email", "phone", "url", "location", "profiles"):
            if key in basics and basics[key] not in (None, "", (), []):
                builder.omit(f"/basics/{key}", f"contact_{key}_not_admitted", basics[key])

    for index, item in enumerate(_optional_items(document, "work")):
        base = f"/work/{index}"
        identity = _identity_at(item.get("position", ""), item.get("name", ""))
        identity = _append_qualifiers(
            identity,
            item,
            base,
            fields=("location",),
            include_dates=True,
        )
        sources = _source_fields(
            item, base, ("position", "name", "location", "startDate", "endDate")
        )
        if identity and sources:
            builder.composite("experience", identity, sources)
        for key in ("description", "summary"):
            _exact_field(builder, "experience", item, key, f"{base}/{key}")
        _exact_list(builder, "experience", item, "highlights", base)
        _omit_url(builder, item, base)

    for index, item in enumerate(_optional_items(document, "volunteer")):
        base = f"/volunteer/{index}"
        identity = _identity_at(item.get("position", ""), item.get("organization", ""))
        identity = _append_qualifiers(identity, item, base, fields=(), include_dates=True)
        sources = _source_fields(item, base, ("position", "organization", "startDate", "endDate"))
        if identity and sources:
            builder.composite("experience", identity, sources)
        _exact_field(builder, "experience", item, "summary", f"{base}/summary")
        _exact_list(builder, "experience", item, "highlights", base)
        _omit_url(builder, item, base)

    for index, item in enumerate(_optional_items(document, "education")):
        base = f"/education/{index}"
        degree = " in ".join(
            value for value in (item.get("studyType", ""), item.get("area", "")) if value
        )
        identity = _identity_at(degree, item.get("institution", ""))
        identity = _append_qualifiers(
            identity,
            item,
            base,
            fields=("score",),
            include_dates=True,
        )
        sources = _source_fields(
            item,
            base,
            ("studyType", "area", "institution", "score", "startDate", "endDate"),
        )
        if identity and sources:
            builder.composite("education", identity, sources)
        _exact_list(builder, "education", item, "courses", base)
        _omit_url(builder, item, base)

    _map_simple_composites(builder, document)


def _map_simple_composites(builder: _ImportBuilder, document: dict[str, Any]) -> None:
    specifications = {
        "awards": ("other", ("title", "awarder", "date"), ("summary",)),
        "certificates": ("certification", ("name", "issuer", "date"), ()),
        "publications": ("other", ("name", "publisher", "releaseDate"), ("summary",)),
        "skills": ("skill", ("name", "level", "keywords"), ()),
        "languages": ("language", ("language", "fluency"), ()),
        "interests": ("other", ("name", "keywords"), ()),
        "projects": (
            "project",
            ("name", "entity", "type", "roles", "keywords", "startDate", "endDate"),
            ("description", "highlights"),
        ),
    }
    for root_key, (section, composite_fields, exact_fields) in specifications.items():
        for index, item in enumerate(_optional_items(document, root_key)):
            base = f"/{root_key}/{index}"
            sources = _source_fields(item, base, composite_fields)
            if sources:
                text = " | ".join(
                    ", ".join(value) if isinstance(value, list) else value
                    for key in composite_fields
                    if (value := item.get(key))
                )
                builder.composite(section, text, sources)
            for field in exact_fields:
                if field in _LIST_FIELDS:
                    _exact_list(builder, section, item, field, base)
                else:
                    _exact_field(builder, section, item, field, f"{base}/{field}")
            _omit_url(builder, item, base)
    for index, item in enumerate(_optional_items(document, "references")):
        builder.omit(
            f"/references/{index}",
            "third_party_reference_requires_manual_entry",
            item,
        )
    if document.get("meta"):
        builder.omit("/meta", "source_metadata_not_profile_fact", document["meta"])
    if document.get("$schema"):
        builder.omit("/$schema", "source_schema_metadata_not_profile_fact", document["$schema"])


def _map_openchronicle_extension(builder: _ImportBuilder, extension: dict[str, Any]) -> None:
    for index, item in enumerate(extension["items"]):
        builder.extension(
            item["section"],
            item["text"],
            f"/meta/openchronicle/items/{index}/text",
        )
    builder.omit(
        "/meta/openchronicle/projection_binding",
        "source_binding_not_profile_fact",
        extension["projection_binding"],
    )
    builder.omit(
        "/meta/openchronicle/profile_binding",
        "source_binding_not_profile_fact",
        extension["profile_binding"],
    )


def _validate_standard_shape(document: dict[str, Any]) -> list[str]:
    unknown = _unknown(document, _ROOT_FIELDS, "")
    if "$schema" in document:
        _known_string(document["$schema"], "/$schema")
    basics = _optional_object(document, "basics")
    if basics is not None:
        unknown.extend(_unknown(basics, _BASICS_FIELDS, "/basics"))
        for key in _BASICS_FIELDS - {"location", "profiles"}:
            if key in basics:
                _known_string(basics[key], f"/basics/{key}")
        location = _optional_object(basics, "location")
        if location is not None:
            unknown.extend(_unknown(location, _LOCATION_FIELDS, "/basics/location"))
            for key, value in location.items():
                if key in _LOCATION_FIELDS:
                    _known_string(value, f"/basics/location/{key}")
        profiles = _optional_array(basics, "profiles")
        for index, profile in enumerate(profiles or []):
            if not isinstance(profile, dict):
                raise JsonResumeError(f"/basics/profiles/{index} must be an object")
            base = f"/basics/profiles/{index}"
            unknown.extend(_unknown(profile, _PROFILE_FIELDS, base))
            for key, value in profile.items():
                if key in _PROFILE_FIELDS:
                    _known_string(value, f"{base}/{key}")

    for root_key, fields in _SECTION_FIELDS.items():
        for index, item in enumerate(_optional_items(document, root_key)):
            base = f"/{root_key}/{index}"
            unknown.extend(_unknown(item, fields, base))
            for key, value in item.items():
                if key not in fields:
                    continue
                pointer = f"{base}/{key}"
                if key in _LIST_FIELDS:
                    if not isinstance(value, list) or len(value) > MAX_ARRAY_ITEMS:
                        raise JsonResumeError(f"{pointer} must be a bounded string list")
                    for item_index, list_value in enumerate(value):
                        _known_string(list_value, f"{pointer}/{item_index}")
                else:
                    _known_string(value, pointer)
                    if key in _DATE_FIELDS and value and _DATE.fullmatch(value) is None:
                        raise JsonResumeError(f"{pointer} must use YYYY, YYYY-MM, or YYYY-MM-DD")

    meta = _optional_object(document, "meta")
    if meta is not None:
        unknown.extend(_unknown(meta, _META_FIELDS, "/meta"))
        for key in ("canonical", "version", "lastModified"):
            if key in meta:
                _known_string(meta[key], f"/meta/{key}")
        if "openchronicle" in meta:
            _validate_openchronicle_extension(meta["openchronicle"])
    if len(unknown) > MAX_UNKNOWN_FIELDS:
        raise JsonResumeError("JSON Resume unknown-field count exceeds limit")
    return sorted(unknown)


def _validate_openchronicle_extension(value: object) -> None:
    expected = {
        "schema_version",
        "kind",
        "action_capability",
        "projection_binding",
        "profile_binding",
        "items",
        "interoperability_losses",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != 1
        or value.get("kind") != "openchronicle_resume_projection"
        or value.get("action_capability") != "none"
    ):
        raise JsonResumeError("OpenChronicle JSON Resume extension is invalid")
    projection = value.get("projection_binding")
    profile = value.get("profile_binding")
    if not isinstance(projection, dict) or set(projection) != {"id", "artifact_digest"}:
        raise JsonResumeError("OpenChronicle projection binding is invalid")
    if not isinstance(profile, dict) or set(profile) != {"id", "version", "digest"}:
        raise JsonResumeError("OpenChronicle profile binding is invalid")
    _identifier(projection.get("id"), "extension projection id")
    _digest(projection.get("artifact_digest"), "extension artifact digest")
    _identifier(profile.get("id"), "extension profile id")
    _digest(profile.get("digest"), "extension profile digest")
    if type(profile.get("version")) is not int or profile["version"] < 1:
        raise JsonResumeError("OpenChronicle profile binding version is invalid")
    items = value.get("items")
    if not isinstance(items, list) or len(items) > MAX_CANDIDATES:
        raise JsonResumeError("OpenChronicle extension items are invalid")
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "fact_id",
            "section",
            "text",
            "confidentiality",
            "ownership_scope",
        }:
            raise JsonResumeError("OpenChronicle extension item is invalid")
        _identifier(item.get("fact_id"), "extension fact id")
        if item.get("section") not in VALID_SECTIONS:
            raise JsonResumeError("OpenChronicle extension section is invalid")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_CANDIDATE_CHARS:
            raise JsonResumeError("OpenChronicle extension text is invalid")
        if item.get("confidentiality") not in VALID_CONFIDENTIALITY:
            raise JsonResumeError("OpenChronicle extension confidentiality is invalid")
        if item.get("ownership_scope") not in VALID_OWNERSHIP:
            raise JsonResumeError("OpenChronicle extension ownership is invalid")
    losses = value.get("interoperability_losses")
    if not isinstance(losses, list) or len(losses) > MAX_CANDIDATES:
        raise JsonResumeError("OpenChronicle extension losses are invalid")
    for loss in losses:
        if not isinstance(loss, dict) or set(loss) != {"fact_id", "section", "reason"}:
            raise JsonResumeError("OpenChronicle extension loss is invalid")
        _identifier(loss.get("fact_id"), "extension loss fact id")
        if loss.get("section") not in VALID_SECTIONS:
            raise JsonResumeError("OpenChronicle extension loss section is invalid")
        if not isinstance(loss.get("reason"), str) or not loss["reason"]:
            raise JsonResumeError("OpenChronicle extension loss reason is invalid")


def _openchronicle_extension(document: dict[str, Any]) -> dict[str, Any] | None:
    meta = document.get("meta")
    return meta.get("openchronicle") if isinstance(meta, dict) else None


def _exact_field(
    builder: _ImportBuilder,
    section: str,
    item: dict[str, Any],
    key: str,
    pointer: str,
) -> None:
    value = item.get(key)
    if isinstance(value, str) and value:
        builder.exact(section, value, pointer)


def _exact_list(
    builder: _ImportBuilder,
    section: str,
    item: dict[str, Any],
    key: str,
    base: str,
) -> None:
    for index, value in enumerate(item.get(key) or []):
        if value:
            builder.exact(section, value, f"{base}/{key}/{index}")


def _source_fields(
    item: dict[str, Any], base: str, fields: tuple[str, ...]
) -> list[dict[str, str]]:
    result = []
    for key in fields:
        value = item.get(key)
        if isinstance(value, list):
            result.extend(
                {"pointer": f"{base}/{key}/{index}", "value": item_value}
                for index, item_value in enumerate(value)
                if item_value
            )
        elif isinstance(value, str) and value:
            result.append({"pointer": f"{base}/{key}", "value": value})
    return result


def _identity_at(role: str, organization: str) -> str:
    if role and organization:
        return f"{role} at {organization}"
    return role or organization


def _append_qualifiers(
    identity: str,
    item: dict[str, Any],
    base: str,
    *,
    fields: tuple[str, ...],
    include_dates: bool,
) -> str:
    del base
    parts = [identity] if identity else []
    parts.extend(item[field] for field in fields if item.get(field))
    if include_dates:
        start = item.get("startDate", "")
        end = item.get("endDate", "")
        if start or end:
            parts.append(f"{start or 'unspecified'} to {end or 'present'}")
    return " | ".join(parts)


def _omit_url(builder: _ImportBuilder, item: dict[str, Any], base: str) -> None:
    if item.get("url"):
        builder.omit(f"{base}/url", "external_url_not_admitted", item["url"])


def _loss(item: dict[str, Any], reason: str) -> dict[str, str]:
    return {"fact_id": item["fact_id"], "section": item["section"], "reason": reason}


def _loads_strict(source_text: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in values:
            if key in result:
                raise JsonResumeError(f"JSON Resume contains duplicate key: {key}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise JsonResumeError(f"JSON Resume contains non-finite number: {value}")

    try:
        value = json.loads(source_text, object_pairs_hook=pairs, parse_constant=constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise JsonResumeError("JSON Resume source is not valid JSON") from exc
    if not isinstance(value, dict):
        raise JsonResumeError("JSON Resume root must be an object")
    return value


def _review_payload(
    *,
    source_id: str,
    source_digest: str,
    source_byte_count: int,
    display_name_candidate: str,
    candidates: list[dict[str, Any]],
    omissions: list[dict[str, Any]],
    unknown_fields: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": IMPORT_REVIEW_SCHEMA_VERSION,
        "format": "json_resume_v1",
        "upstream_schema": {
            "version": UPSTREAM_SCHEMA_VERSION,
            "commit": UPSTREAM_SCHEMA_COMMIT,
            "url": UPSTREAM_SCHEMA_URL,
        },
        "source": {
            "id": source_id,
            "digest": source_digest,
            "byte_count": source_byte_count,
        },
        "display_name_candidate": display_name_candidate,
        "candidates": copy.deepcopy(candidates),
        "omissions": copy.deepcopy(omissions),
        "unknown_fields": list(unknown_fields),
        "warnings": list(warnings),
        "action_capability": "none",
    }


def _validate_json_tree(value: object, *, depth: int = 0, counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > 20_000 or depth > 32:
        raise JsonResumeError("JSON Resume structure exceeds limit")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JsonResumeError("JSON Resume contains a non-finite number")
        return
    if isinstance(value, str):
        if "\x00" in value or len(value) > MAX_SOURCE_STRING_CHARS:
            raise JsonResumeError("JSON Resume string exceeds limit")
        return
    if isinstance(value, list):
        if len(value) > MAX_ARRAY_ITEMS:
            raise JsonResumeError("JSON Resume array exceeds limit")
        for item in value:
            _validate_json_tree(item, depth=depth + 1, counter=counter)
        return
    if isinstance(value, dict):
        if len(value) > MAX_ARRAY_ITEMS:
            raise JsonResumeError("JSON Resume object exceeds limit")
        for key, item in value.items():
            if not isinstance(key, str) or "\x00" in key or len(key) > 512:
                raise JsonResumeError("JSON Resume key is invalid")
            _validate_json_tree(item, depth=depth + 1, counter=counter)
        return
    raise JsonResumeError("JSON Resume contains an unsupported JSON value")


def _optional_object(parent: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = parent.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise JsonResumeError(f"/{key} must be an object")
    return value


def _optional_array(parent: dict[str, Any], key: str) -> list[Any] | None:
    value = parent.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_ARRAY_ITEMS:
        raise JsonResumeError(f"/{key} must be a bounded array")
    return value


def _optional_items(parent: dict[str, Any], key: str) -> list[dict[str, Any]]:
    values = _optional_array(parent, key) or []
    if any(not isinstance(item, dict) for item in values):
        raise JsonResumeError(f"/{key} items must be objects")
    return values


def _string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key, "")
    return value if isinstance(value, str) else ""


def _known_string(value: object, pointer: str) -> None:
    if not isinstance(value, str):
        raise JsonResumeError(f"{pointer} must be a string")


def _unknown(value: dict[str, Any], expected: set[str], base: str) -> list[str]:
    return [f"{base}/{_escape_pointer(key)}" for key in value if key not in expected]


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _pointer_value(document: object, pointer: str) -> object:
    current = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:  # pragma: no cover - pointers are generated from the validated tree
            raise JsonResumeError("JSON Resume pointer is invalid")
    return current


def _reviewed_timestamp(value: str) -> None:
    if not isinstance(value, str):
        raise JsonResumeError("reviewed_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JsonResumeError("reviewed_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JsonResumeError("reviewed_at must be timezone-aware")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise JsonResumeError(f"{name} is invalid")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise JsonResumeError(f"{name} is invalid")
    return value
