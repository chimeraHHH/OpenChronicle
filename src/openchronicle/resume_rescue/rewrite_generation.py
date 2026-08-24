"""Minimized, disclosed, no-tool provider boundary for résumé rewrite proposals."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from ..config import Config
from ..prompts import load as load_prompt
from ..provenance.models import canonical_digest
from ..writer import llm as llm_mod
from .rewrite import (
    ResumeRewriteValidationError,
    validate_rewrite_artifact_for_egress,
    validate_rewrite_model_output,
)

TEMPLATE_VERSION = 1
PROVIDER_INPUT_FIELDS = {
    "schema_version",
    "workflow",
    "action_capability",
    "data_trust",
    "facts",
}
PROVIDER_FACT_FIELDS = {"fact_id", "section", "text", "requirements"}
PROVIDER_REQUIREMENT_FIELDS = {"requirement_id", "text"}
VALID_PROVIDER_LOCATIONS = {"local", "remote_or_unknown"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ResumeRewriteEgressDenied(ValueError):
    """The visible provider disclosure did not authorize this egress."""


def provider_summary(cfg: Config) -> dict[str, str]:
    model_cfg = cfg.model_for("resume_rescue")
    model = model_cfg.model
    if not isinstance(model, str) or not model.strip() or len(model) > 256 or "\x00" in model:
        raise ValueError("resume rewrite model identity is invalid")
    return {"model": model, "location": _provider_location(model, model_cfg.base_url)}


def validate_rewrite_config(cfg: Config) -> None:
    value = cfg.resume_rescue
    for name in ("enabled", "rewrite_enabled"):
        if type(getattr(value, name)) is not bool:
            raise ValueError(f"resume_rescue.{name} must be a boolean")
    ranges = {
        "rewrite_poll_seconds": (1, 300),
        "rewrite_lease_seconds": (30, 21_600),
        "rewrite_max_input_chars": (100, 200_000),
        "rewrite_max_output_chars": (100, 200_000),
    }
    for name, (minimum, maximum) in ranges.items():
        field_value = getattr(value, name)
        if type(field_value) is not int or not minimum <= field_value <= maximum:
            raise ValueError(f"resume_rescue.{name} is invalid")


def build_rewrite_provider_input(artifact: dict[str, Any]) -> dict[str, Any]:
    """Build the only selected-data payload a rewrite provider may receive."""

    source = validate_rewrite_artifact_for_egress(artifact)
    requirement_by_id = {
        item["id"]: item for item in source["requirement_coverage"] if item["fact_ids"]
    }
    mapped_by_fact: dict[str, list[dict[str, str]]] = {}
    for requirement in requirement_by_id.values():
        for fact_id in requirement["fact_ids"]:
            mapped_by_fact.setdefault(fact_id, []).append(
                {"requirement_id": requirement["id"], "text": requirement["text"]}
            )

    facts: list[dict[str, Any]] = []
    for section in source["sections"]:
        for item in section["items"]:
            facts.append(
                {
                    "fact_id": item["fact_id"],
                    "section": section["kind"],
                    "text": item["text"],
                    "requirements": mapped_by_fact.get(item["fact_id"], []),
                }
            )
    return validate_rewrite_provider_input(
        {
            "schema_version": 1,
            "workflow": "resume_rescue",
            "action_capability": "none",
            "data_trust": "untrusted_quoted_data",
            "facts": facts,
        }
    )


def validate_rewrite_provider_input(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PROVIDER_INPUT_FIELDS:
        raise ResumeRewriteValidationError(
            "source_mismatch", "resume rewrite provider input must use the closed schema"
        )
    if (
        value.get("schema_version") != 1
        or value.get("workflow") != "resume_rescue"
        or value.get("action_capability") != "none"
        or value.get("data_trust") != "untrusted_quoted_data"
    ):
        raise ResumeRewriteValidationError(
            "source_mismatch", "resume rewrite provider input identity is invalid"
        )
    raw_facts = value.get("facts")
    if not isinstance(raw_facts, list) or len(raw_facts) > 200:
        raise ResumeRewriteValidationError(
            "source_mismatch", "resume rewrite provider facts must be bounded"
        )
    facts: list[dict[str, Any]] = []
    fact_ids: set[str] = set()
    for raw in raw_facts:
        if not isinstance(raw, dict) or set(raw) != PROVIDER_FACT_FIELDS:
            raise ResumeRewriteValidationError(
                "source_mismatch", "resume rewrite provider fact is invalid"
            )
        fact_id = _identifier(raw.get("fact_id"), "provider fact id")
        if fact_id in fact_ids:
            raise ResumeRewriteValidationError(
                "source_mismatch", "resume rewrite provider facts are duplicated"
            )
        fact_ids.add(fact_id)
        section = raw.get("section")
        text = raw.get("text")
        if (
            section
            not in {
                "summary",
                "experience",
                "education",
                "skill",
                "project",
                "certification",
                "language",
                "other",
            }
            or not isinstance(text, str)
            or not text.strip()
            or "\x00" in text
            or len(text) > 8_000
        ):
            raise ResumeRewriteValidationError(
                "source_mismatch", "resume rewrite provider fact is invalid"
            )
        raw_requirements = raw.get("requirements")
        if not isinstance(raw_requirements, list) or len(raw_requirements) > 200:
            raise ResumeRewriteValidationError(
                "source_mismatch", "resume rewrite provider requirements must be bounded"
            )
        requirements: list[dict[str, str]] = []
        requirement_ids: set[str] = set()
        for requirement in raw_requirements:
            if not isinstance(requirement, dict) or set(requirement) != PROVIDER_REQUIREMENT_FIELDS:
                raise ResumeRewriteValidationError(
                    "source_mismatch", "resume rewrite provider requirement is invalid"
                )
            requirement_id = _identifier(
                requirement.get("requirement_id"), "provider requirement id"
            )
            requirement_text = requirement.get("text")
            if (
                requirement_id in requirement_ids
                or not isinstance(requirement_text, str)
                or not requirement_text.strip()
                or "\x00" in requirement_text
                or len(requirement_text) > 5_000
            ):
                raise ResumeRewriteValidationError(
                    "source_mismatch", "resume rewrite provider requirement is invalid"
                )
            requirement_ids.add(requirement_id)
            requirements.append({"requirement_id": requirement_id, "text": requirement_text})
        facts.append(
            {
                "fact_id": fact_id,
                "section": section,
                "text": text,
                "requirements": requirements,
            }
        )
    return {
        "schema_version": 1,
        "workflow": "resume_rescue",
        "action_capability": "none",
        "data_trust": "untrusted_quoted_data",
        "facts": facts,
    }


def rewrite_provider_input_digest(value: dict[str, Any]) -> str:
    return canonical_digest({"schema": "resume-rewrite-provider-input-v1", "input": value})


def rewrite_template_digest() -> str:
    return canonical_digest(
        {"schema": "resume-rewrite-template-v1", "text": load_prompt("resume_rescue.md")}
    )


def generate_rewrite_output(
    cfg: Config,
    *,
    artifact: dict[str, Any],
    expected_model_identity: str,
    expected_provider_location: str,
    remote_egress_authorized: bool,
    llm_caller: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Call one disclosed provider with minimized input and no tools."""

    validate_rewrite_config(cfg)
    if not cfg.resume_rescue.enabled or not cfg.resume_rescue.rewrite_enabled:
        raise ResumeRewriteEgressDenied("resume rewrite is disabled")
    if type(remote_egress_authorized) is not bool:
        raise ResumeRewriteEgressDenied("resume rewrite egress authorization is invalid")
    provider = provider_summary(cfg)
    if (
        not isinstance(expected_model_identity, str)
        or not isinstance(expected_provider_location, str)
        or not hmac.compare_digest(provider["model"], expected_model_identity)
        or not hmac.compare_digest(provider["location"], expected_provider_location)
    ):
        raise ResumeRewriteEgressDenied("resume rewrite provider disclosure changed")
    if provider["location"] == "remote_or_unknown" and not remote_egress_authorized:
        raise ResumeRewriteEgressDenied("resume rewrite remote egress is not authorized")

    payload = build_rewrite_provider_input(artifact)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > cfg.resume_rescue.rewrite_max_input_chars:
        raise ResumeRewriteValidationError(
            "source_mismatch", "resume rewrite provider input exceeds the configured limit"
        )
    response = (llm_caller or llm_mod.call_llm)(
        cfg,
        "resume_rescue",
        messages=[
            {"role": "system", "content": load_prompt("resume_rescue.md")},
            {"role": "user", "content": encoded},
        ],
        tools=None,
        json_mode=True,
    )
    if llm_mod.extract_tool_calls(response):
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite provider returned a tool call"
        )
    text = llm_mod.extract_text(response).strip()
    if not text or len(text) > cfg.resume_rescue.rewrite_max_output_chars:
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite provider output exceeds the configured limit"
        )
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite provider output is invalid"
        ) from exc
    return validate_rewrite_model_output(raw, artifact=artifact)


def _provider_location(model: str, base_url: object) -> str:
    lowered = model.casefold()
    if lowered.startswith(("ollama/", "lm_studio/", "local/")):
        return "local"
    if isinstance(base_url, str) and base_url:
        hostname = urlparse(base_url).hostname
        if hostname in {"localhost", "127.0.0.1", "::1"}:
            return "local"
    return "remote_or_unknown"


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ResumeRewriteValidationError("source_mismatch", f"resume rewrite {label} is invalid")
    return value
