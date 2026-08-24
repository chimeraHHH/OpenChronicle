"""Closed, fact-bound model proposal validation for Résumé Rescue."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from typing import Any

from ..provenance.models import canonical_digest
from .models import VALID_SECTIONS, validate_artifact

MODEL_OUTPUT_FIELDS = {"schema_version", "proposals"}
PROPOSAL_FIELDS = {
    "proposal_id",
    "operation",
    "section",
    "fact_id",
    "original_text",
    "proposed_text",
    "rationale",
    "requirement_ids",
    "evidence_fragments",
}
VALID_ERROR_CODES = {
    "invalid_output",
    "source_mismatch",
    "unsupported_claim",
    "secret_echo",
}

MAX_SELECTED_FACTS = 200
MAX_FACT_CHARS = 8_000
MAX_REQUIREMENTS = 200
MAX_REQUIREMENT_CHARS = 5_000
MAX_PROVIDER_OUTPUT_CHARS = 200_000
MAX_PROPOSALS = 200
MAX_PROPOSAL_TEXT_CHARS = 8_000
MAX_RATIONALE_CHARS = 2_000
MAX_EVIDENCE_FRAGMENTS = 32
MAX_EVIDENCE_FRAGMENT_CHARS = 2_000

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PROTECTED_ATOM_RE = re.compile(
    r"(?P<url>\b(?:https?://|www\.)[^\s<>()]+)|"
    r"(?P<email>(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-]))|"
    r"(?P<money>(?:[$€£¥₹]\s*|\b(?:USD|EUR|GBP|CNY|RMB|JPY|INR)\s+)"
    r"\d+(?:[.,]\d+)*(?:\s*(?:k|m|b|million|billion|thousand))?)|"
    r"(?P<date>\b\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?\b)|"
    r"(?P<number>(?<![\w])\d+(?:[.,]\d+)*(?:\s*(?:%|x|k|m|b|million|billion|thousand))?(?![\w]))",
    re.IGNORECASE,
)
_CONTENT_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9+#./-]*|[\u3400-\u9fff]|[^\W\d_]+",
    re.UNICODE,
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]{0,80}PRIVATE KEY-----.*?"
    r"-----END [^-\r\n]{0,80}PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_SECRET_RE = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|AKIA[A-Z0-9]{16})\b|"
    r"(?i:\b(?:password|passcode|api[_ -]?key|access[_ -]?token|"
    r"refresh[_ -]?token|client[_ -]?secret)\b\s*[:=]\s*[^\s,;]{4,})"
)
_OUTCOME_CLAIM_RE = re.compile(
    r"\b(?:guarantee(?:d|s)?|ensure(?:d|s)?|will)\b.{0,80}"
    r"\b(?:ats|interview|hired|offer)\b|\b(?:pass|beat)\s+(?:the\s+)?ats\b",
    re.IGNORECASE,
)
_NEUTRAL_TOKENS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "per",
    "the",
    "through",
    "to",
    "using",
    "via",
    "while",
    "with",
    "within",
    "across",
    "及",
    "和",
    "与",
    "在",
    "为",
    "由",
    "的",
    "了",
    "并",
    "通",
    "过",
}


class ResumeRewriteValidationError(ValueError):
    """A model proposal crossed one closed rewrite boundary."""

    def __init__(self, code: str, message: str) -> None:
        if code not in VALID_ERROR_CODES:
            raise ValueError("resume rewrite error code is invalid")
        super().__init__(message)
        self.code = code


def validate_rewrite_artifact_for_egress(value: object) -> dict[str, Any]:
    """Validate the exact projection subset allowed to cross a provider boundary."""

    try:
        artifact = validate_artifact(value)
    except ValueError as exc:
        raise ResumeRewriteValidationError(
            "source_mismatch", "resume rewrite source is invalid"
        ) from exc

    selected = [item for section in artifact["sections"] for item in section["items"]]
    if len(selected) > MAX_SELECTED_FACTS:
        raise ResumeRewriteValidationError(
            "source_mismatch", "resume rewrite selected fact limit exceeded"
        )
    for item in selected:
        text = item["text"]
        if len(text) > MAX_FACT_CHARS:
            raise ResumeRewriteValidationError(
                "source_mismatch", "resume rewrite selected fact limit exceeded"
            )
        if _contains_secret(text):
            raise ResumeRewriteValidationError(
                "secret_echo", "resume rewrite source contains a credential-like value"
            )

    coverage = artifact["requirement_coverage"]
    if len(coverage) > MAX_REQUIREMENTS:
        raise ResumeRewriteValidationError(
            "source_mismatch", "resume rewrite requirement limit exceeded"
        )
    for requirement in coverage:
        if len(requirement["text"]) > MAX_REQUIREMENT_CHARS:
            raise ResumeRewriteValidationError(
                "source_mismatch", "resume rewrite requirement limit exceeded"
            )
        if _contains_secret(requirement["text"]):
            raise ResumeRewriteValidationError(
                "secret_echo", "resume rewrite requirement contains a credential-like value"
            )
    return artifact


def validate_rewrite_model_output(value: object, *, artifact: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one no-tool provider proposal set."""

    source = validate_rewrite_artifact_for_egress(artifact)
    if not isinstance(value, dict) or set(value) != MODEL_OUTPUT_FIELDS:
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite output must use the closed schema"
        )
    if value.get("schema_version") != 1:
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite output version is unsupported"
        )
    raw_proposals = value.get("proposals")
    if not isinstance(raw_proposals, list) or len(raw_proposals) > MAX_PROPOSALS:
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite proposals must be a bounded list"
        )
    try:
        serialized_chars = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite output is not JSON compatible"
        ) from exc
    if serialized_chars > MAX_PROVIDER_OUTPUT_CHARS:
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite output exceeds the size limit"
        )

    selected: dict[str, dict[str, str]] = {}
    for section in source["sections"]:
        for item in section["items"]:
            selected[item["fact_id"]] = {"section": section["kind"], "text": item["text"]}
    mapped_requirements: dict[str, set[str]] = {fact_id: set() for fact_id in selected}
    for requirement in source["requirement_coverage"]:
        for fact_id in requirement["fact_ids"]:
            if fact_id in mapped_requirements:
                mapped_requirements[fact_id].add(requirement["id"])

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_facts: set[str] = set()
    for raw in raw_proposals:
        proposal = _validate_proposal(
            raw,
            selected=selected,
            mapped_requirements=mapped_requirements,
        )
        if proposal["proposal_id"] in seen_ids or proposal["fact_id"] in seen_facts:
            raise ResumeRewriteValidationError(
                "invalid_output", "resume rewrite proposals must target distinct facts"
            )
        seen_ids.add(proposal["proposal_id"])
        seen_facts.add(proposal["fact_id"])
        normalized.append(proposal)
    return {"schema_version": 1, "proposals": normalized}


def rewrite_output_digest(value: dict[str, Any]) -> str:
    """Return a stable digest for a previously validated model output."""

    return canonical_digest({"schema": "resume-rewrite-model-output-v1", "output": value})


def rewrite_proposal_digest(value: dict[str, Any]) -> str:
    """Bind one review decision to the exact locally validated proposal."""

    return canonical_digest({"schema": "resume-rewrite-proposal-v1", "proposal": value})


def _validate_proposal(
    value: object,
    *,
    selected: dict[str, dict[str, str]],
    mapped_requirements: dict[str, set[str]],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PROPOSAL_FIELDS:
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite proposal must use the closed schema"
        )
    proposal_id = _identifier(value.get("proposal_id"), "proposal id")
    if value.get("operation") != "replace_text":
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite proposal operation is unsupported"
        )
    section = value.get("section")
    if section not in VALID_SECTIONS:
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite proposal section is invalid"
        )
    fact_id = _identifier(value.get("fact_id"), "fact id")
    bound = selected.get(fact_id)
    if bound is None or bound["section"] != section:
        raise ResumeRewriteValidationError(
            "source_mismatch", "resume rewrite proposal fact binding is invalid"
        )
    original = _text(value.get("original_text"), "original text", maximum=MAX_PROPOSAL_TEXT_CHARS)
    if original != bound["text"]:
        raise ResumeRewriteValidationError(
            "source_mismatch", "resume rewrite proposal original text changed"
        )
    proposed = _text(value.get("proposed_text"), "proposed text", maximum=MAX_PROPOSAL_TEXT_CHARS)
    if proposed == original:
        raise ResumeRewriteValidationError(
            "invalid_output", "resume rewrite proposal must change the text"
        )
    rationale = _text(value.get("rationale"), "rationale", maximum=MAX_RATIONALE_CHARS)
    if _contains_secret(proposed) or _contains_secret(rationale):
        raise ResumeRewriteValidationError(
            "secret_echo", "resume rewrite proposal contains a credential-like value"
        )
    if _OUTCOME_CLAIM_RE.search(rationale):
        raise ResumeRewriteValidationError(
            "unsupported_claim", "resume rewrite rationale makes an outcome claim"
        )
    _verify_claim_atoms(original, proposed)

    requirement_ids = _identifier_list(
        value.get("requirement_ids"), "requirement ids", maximum=MAX_REQUIREMENTS
    )
    if not set(requirement_ids).issubset(mapped_requirements[fact_id]):
        raise ResumeRewriteValidationError(
            "source_mismatch", "resume rewrite proposal requirement binding is invalid"
        )
    fragments = _text_list(
        value.get("evidence_fragments"),
        "evidence fragments",
        maximum_items=MAX_EVIDENCE_FRAGMENTS,
        maximum_chars=MAX_EVIDENCE_FRAGMENT_CHARS,
        require_nonempty=True,
    )
    if any(fragment not in original for fragment in fragments):
        raise ResumeRewriteValidationError(
            "unsupported_claim", "resume rewrite evidence is not an exact source excerpt"
        )
    return {
        "proposal_id": proposal_id,
        "operation": "replace_text",
        "section": section,
        "fact_id": fact_id,
        "original_text": original,
        "proposed_text": proposed,
        "rationale": rationale,
        "requirement_ids": requirement_ids,
        "evidence_fragments": fragments,
    }


def _verify_claim_atoms(original: str, proposed: str) -> None:
    if _protected_atoms(original) != _protected_atoms(proposed):
        raise ResumeRewriteValidationError(
            "unsupported_claim", "resume rewrite changed a protected claim atom"
        )
    source_tokens = _content_tokens(original)
    proposed_tokens = _content_tokens(proposed)
    unsupported = {
        token
        for token, count in proposed_tokens.items()
        if token not in _NEUTRAL_TOKENS and count > source_tokens[token]
    }
    if unsupported:
        raise ResumeRewriteValidationError(
            "unsupported_claim", "resume rewrite introduced unsupported content"
        )


def _protected_atoms(value: str) -> Counter[str]:
    atoms: Counter[str] = Counter()
    for match in _PROTECTED_ATOM_RE.finditer(unicodedata.normalize("NFKC", value)):
        kind = match.lastgroup or "value"
        raw = match.group(0)
        if kind == "url":
            raw = raw.rstrip(".,;:!?)]}")
        normalized = re.sub(r"\s+", " ", raw.casefold().strip())
        atoms[f"{kind}:{normalized}"] += 1
    return atoms


def _content_tokens(value: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    without_protected = _PROTECTED_ATOM_RE.sub(" ", normalized)
    tokens = (
        match.group(0).strip("./-") for match in _CONTENT_TOKEN_RE.finditer(without_protected)
    )
    return Counter(token for token in tokens if token)


def _contains_secret(value: str) -> bool:
    return bool(_PRIVATE_KEY_RE.search(value) or _SECRET_RE.search(value))


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ResumeRewriteValidationError("invalid_output", f"resume rewrite {label} is invalid")
    return value


def _identifier_list(value: object, label: str, *, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ResumeRewriteValidationError(
            "invalid_output", f"resume rewrite {label} must be a bounded list"
        )
    result = [_identifier(item, label) for item in value]
    if len(set(result)) != len(result):
        raise ResumeRewriteValidationError(
            "invalid_output", f"resume rewrite {label} must not contain duplicates"
        )
    return result


def _text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > maximum:
        raise ResumeRewriteValidationError("invalid_output", f"resume rewrite {label} is invalid")
    return value


def _text_list(
    value: object,
    label: str,
    *,
    maximum_items: int,
    maximum_chars: int,
    require_nonempty: bool,
) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > maximum_items
        or (require_nonempty and not value)
    ):
        raise ResumeRewriteValidationError(
            "invalid_output", f"resume rewrite {label} must be a bounded list"
        )
    result = [_text(item, label, maximum=maximum_chars) for item in value]
    if len(set(result)) != len(result):
        raise ResumeRewriteValidationError(
            "invalid_output", f"resume rewrite {label} must not contain duplicates"
        )
    return result
