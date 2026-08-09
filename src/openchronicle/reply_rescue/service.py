"""Strict no-tool generation and review operations for Reply Rescue."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlparse

from ..config import Config
from ..privacy import policy as privacy_policy
from ..privacy.egress import model_egress_lock
from ..prompts import load as load_prompt
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef, canonical_digest
from ..writer import llm as llm_mod
from . import store

TEMPLATE_VERSION = 1
_OUTPUT_FIELDS = {
    "schema_version",
    "workflow",
    "action_capability",
    "reply_body",
    "addressed_questions",
    "unresolved_questions",
    "assumptions",
    "warnings",
    "claims",
}
_SOURCE_FIELDS = {
    "schema_version",
    "identity_assurance",
    "conversation_text",
    "participants",
    "intended_recipients",
    "reply_mode",
    "goal",
    "tone",
    "style_instructions",
    "commitments",
}


class ReplyRescueValidationError(RuntimeError):
    """The source or model output did not match the closed workflow schema."""


class ReplyRescueService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        *,
        llm_caller: Callable[..., Any] | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.llm_caller = llm_caller or llm_mod.call_llm
        store.ensure_schema(conn)

    def provider_summary(self) -> dict[str, str]:
        return provider_summary(self.cfg)

    def queue_manual(
        self,
        *,
        conversation_text: str,
        participants: Sequence[str] = (),
        intended_recipients: Sequence[str] = (),
        reply_mode: str = "unspecified",
        goal: str = "",
        tone: str = "",
        style_instructions: Sequence[str] = (),
        commitments: Sequence[str] = (),
    ) -> tuple[store.ReplyRescueJob, bool]:
        validate_config(self.cfg)
        if not self.cfg.reply_rescue.enabled:
            raise ValueError("reply rescue is disabled")
        source = validate_manual_source(
            self.cfg,
            conversation_text=conversation_text,
            participants=participants,
            intended_recipients=intended_recipients,
            reply_mode=reply_mode,
            goal=goal,
            tone=tone,
            style_instructions=style_instructions,
            commitments=commitments,
        )
        template = load_prompt("reply_rescue.md")
        provider = self.provider_summary()
        return store.create(
            self.conn,
            source_kind="manual_conversation",
            source=source,
            policy_digest=privacy_policy.stored_observation_policy_digest(self.cfg.capture),
            template_version=TEMPLATE_VERSION,
            template_digest=canonical_digest(
                {"schema": "reply-rescue-template-v1", "text": template}
            ),
            model_identity=provider["model"],
            provider_location=provider["location"],
        )

    def list(self, *, limit: int = 50) -> list[store.ReplyRescueJob]:
        return [job for job in store.list_jobs(self.conn, limit=limit) if self._current(job)]

    def get(self, job_id: str) -> store.ReplyRescueJob | None:
        job = store.get(self.conn, job_id)
        return job if job is not None and self._current(job) else None

    def process_next(self) -> store.ReplyRescueJob | None:
        validate_config(self.cfg)
        if not self.cfg.reply_rescue.enabled:
            return None
        lease_seconds = max(
            self.cfg.reply_rescue.lease_seconds,
            math.ceil(llm_mod.call_budget_seconds(self.cfg, "reply_rescue")),
        )
        if lease_seconds > 21_600:
            raise ValueError("reply rescue provider budget exceeds safe lease")
        lease_token = uuid.uuid4().hex
        claimed = store.claim_next(self.conn, lease_token=lease_token, lease_seconds=lease_seconds)
        if claimed is None:
            return None
        try:
            with model_egress_lock():
                current = store.get(self.conn, claimed.id)
                if (
                    current is None
                    or current.status != "leased"
                    or current.lease_token != lease_token
                    or not self._current(current)
                ):
                    raise ReplyRescueValidationError("reply rescue input changed")
                output = generate_output(
                    self.cfg, source=current.source, llm_caller=self.llm_caller
                )
            return store.complete(
                self.conn,
                job_id=claimed.id,
                lease_token=lease_token,
                output=output,
            )
        except llm_mod.ProviderCallCancelledError:
            store.release_claim(self.conn, job_id=claimed.id, lease_token=lease_token)
            raise
        except store.ReplyRescueConflict:
            raise
        except ReplyRescueValidationError as exc:
            error_code = "input_changed" if "input changed" in str(exc) else "invalid_output"
            return store.fail(
                self.conn,
                job_id=claimed.id,
                lease_token=lease_token,
                error_code=error_code,
            )
        except Exception:  # noqa: BLE001 - durable public state is sanitized
            return store.fail(
                self.conn,
                job_id=claimed.id,
                lease_token=lease_token,
                error_code="provider_failed",
            )

    def retry(self, job_id: str, *, expected_version: int) -> store.ReplyRescueJob:
        validate_config(self.cfg)
        if not self.cfg.reply_rescue.enabled:
            raise ValueError("reply rescue is disabled")
        if self.get(job_id) is None:
            raise store.ReplyRescueConflict("reply rescue changed")
        return store.retry(self.conn, job_id=job_id, expected_version=expected_version)

    def edit(
        self,
        job_id: str,
        *,
        expected_version: int,
        reply_body: str,
    ) -> store.ReplyRescueJob:
        current = self.get(job_id)
        if current is None or current.output is None:
            raise store.ReplyRescueConflict("reply rescue changed")
        output = dict(current.output)
        output["reply_body"] = reply_body
        output["claims"] = []
        output["addressed_questions"] = []
        warnings = list(output["warnings"])
        edit_warning = "Reply body was manually edited; re-check every fact and commitment."
        if edit_warning not in warnings:
            warnings = [*warnings[:29], edit_warning]
        output["warnings"] = warnings
        validated = validate_output(self.cfg, output)
        return store.edit_output(
            self.conn,
            job_id=job_id,
            expected_version=expected_version,
            output=validated,
        )

    def delete(self, job_id: str, *, expected_version: int) -> None:
        store.delete(self.conn, job_id=job_id, expected_version=expected_version)

    def _current(self, job: store.ReplyRescueJob) -> bool:
        sources = provenance_store.direct_sources_checked(
            self.conn, EvidenceRef(kind="reply_rescue", id=job.id)
        )
        return bool(
            sources == [job.input_ref] and provenance_store.is_current(self.conn, job.input_ref)
        )


def provider_summary(cfg: Config) -> dict[str, str]:
    model_cfg = cfg.model_for("reply_rescue")
    model = model_cfg.model
    if not isinstance(model, str) or not model.strip() or len(model) > 256 or "\x00" in model:
        raise ValueError("reply rescue model identity is invalid")
    return {"model": model, "location": _provider_location(model, model_cfg.base_url)}


def generate_output(
    cfg: Config,
    *,
    source: dict[str, Any],
    llm_caller: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    normalized = validate_source_object(cfg, source)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise ReplyRescueValidationError("reply rescue input changed")
    response = (llm_caller or llm_mod.call_llm)(
        cfg,
        "reply_rescue",
        messages=[
            {"role": "system", "content": load_prompt("reply_rescue.md")},
            {"role": "user", "content": encoded},
        ],
        json_mode=True,
    )
    text = llm_mod.extract_text(response).strip()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReplyRescueValidationError("reply rescue output is invalid") from exc
    return validate_output(cfg, raw)


def validate_config(cfg: Config) -> None:
    value = cfg.reply_rescue
    if type(value.enabled) is not bool:
        raise ValueError("reply_rescue.enabled must be a boolean")
    ranges = {
        "poll_seconds": (1, 300),
        "lease_seconds": (30, 21_600),
        "max_input_chars": (100, 100_000),
        "max_output_chars": (100, 50_000),
    }
    for name, (minimum, maximum) in ranges.items():
        field_value = getattr(value, name)
        if type(field_value) is not int or not minimum <= field_value <= maximum:
            raise ValueError(f"reply_rescue.{name} is invalid")


def validate_manual_source(
    cfg: Config,
    *,
    conversation_text: str,
    participants: Sequence[str],
    intended_recipients: Sequence[str],
    reply_mode: str,
    goal: str,
    tone: str,
    style_instructions: Sequence[str],
    commitments: Sequence[str],
) -> dict[str, Any]:
    declared_lists = {
        "participants": participants,
        "intended_recipients": intended_recipients,
        "style_instructions": style_instructions,
        "commitments": commitments,
    }
    if any(not isinstance(value, (list, tuple)) for value in declared_lists.values()):
        raise ReplyRescueValidationError("reply rescue source is invalid")
    source = {
        "schema_version": 1,
        "identity_assurance": "manual_unverified",
        "conversation_text": conversation_text,
        "participants": list(participants),
        "intended_recipients": list(intended_recipients),
        "reply_mode": reply_mode,
        "goal": goal,
        "tone": tone,
        "style_instructions": list(style_instructions),
        "commitments": list(commitments),
    }
    return validate_source_object(cfg, source)


def validate_source_object(cfg: Config, raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _SOURCE_FIELDS:
        raise ReplyRescueValidationError("reply rescue source schema is invalid")
    conversation = raw.get("conversation_text")
    if (
        raw.get("schema_version") != 1
        or raw.get("identity_assurance") != "manual_unverified"
        or not isinstance(conversation, str)
        or not conversation.strip()
        or "\x00" in conversation
        or len(conversation) > cfg.reply_rescue.max_input_chars
        or raw.get("reply_mode") not in {"reply", "reply_all", "unspecified"}
    ):
        raise ReplyRescueValidationError("reply rescue source is invalid")
    result: dict[str, Any] = {
        "schema_version": 1,
        "identity_assurance": "manual_unverified",
        "conversation_text": conversation,
    }
    for name in ("goal", "tone"):
        value = raw.get(name)
        if not isinstance(value, str) or "\x00" in value or len(value) > 1_000:
            raise ReplyRescueValidationError("reply rescue source is invalid")
        result[name] = value.strip()
    result["reply_mode"] = raw["reply_mode"]
    for name, maximum in (
        ("participants", 50),
        ("intended_recipients", 50),
        ("style_instructions", 20),
        ("commitments", 20),
    ):
        result[name] = _string_list(raw.get(name), maximum=maximum, item_limit=1_000)
    declared_chars = len(conversation) + len(result["goal"]) + len(result["tone"])
    declared_chars += sum(
        len(item)
        for name in (
            "participants",
            "intended_recipients",
            "style_instructions",
            "commitments",
        )
        for item in result[name]
    )
    if declared_chars > cfg.reply_rescue.max_input_chars:
        raise ReplyRescueValidationError("reply rescue input exceeds max_input_chars")
    return result


def validate_output(cfg: Config, raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _OUTPUT_FIELDS:
        raise ReplyRescueValidationError("reply rescue output schema is invalid")
    body = raw.get("reply_body")
    if (
        raw.get("schema_version") != 1
        or raw.get("workflow") != "reply_rescue"
        or raw.get("action_capability") != "none"
        or not isinstance(body, str)
        or not body.strip()
        or "\x00" in body
        or len(body) > cfg.reply_rescue.max_output_chars
    ):
        raise ReplyRescueValidationError("reply rescue output is invalid")
    result: dict[str, Any] = {
        "schema_version": 1,
        "workflow": "reply_rescue",
        "action_capability": "none",
        "reply_body": body,
    }
    for name in ("addressed_questions", "unresolved_questions", "assumptions", "warnings"):
        result[name] = _string_list(raw.get(name), maximum=30, item_limit=1_000)
    claims = raw.get("claims")
    if not isinstance(claims, list) or len(claims) > 50:
        raise ReplyRescueValidationError("reply rescue output is invalid")
    cleaned_claims: list[dict[str, str]] = []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"text", "support"}:
            raise ReplyRescueValidationError("reply rescue output is invalid")
        text = claim.get("text")
        support = claim.get("support")
        if (
            not isinstance(text, str)
            or not text.strip()
            or "\x00" in text
            or len(text) > 1_000
            or support not in {"conversation", "user_direction"}
        ):
            raise ReplyRescueValidationError("reply rescue output is invalid")
        cleaned_claims.append({"text": text.strip(), "support": support})
    result["claims"] = cleaned_claims
    if len(json.dumps(result, ensure_ascii=False)) > cfg.reply_rescue.max_output_chars * 2:
        raise ReplyRescueValidationError("reply rescue output is invalid")
    return result


def _string_list(raw: object, *, maximum: int, item_limit: int) -> list[str]:
    if not isinstance(raw, list) or len(raw) > maximum:
        raise ReplyRescueValidationError("reply rescue value is invalid")
    cleaned: list[str] = []
    for item in raw:
        if (
            not isinstance(item, str)
            or not item.strip()
            or "\x00" in item
            or len(item) > item_limit
        ):
            raise ReplyRescueValidationError("reply rescue value is invalid")
        cleaned.append(item.strip())
    return cleaned


def _provider_location(model: str, base_url: object) -> str:
    lowered = model.casefold()
    if lowered.startswith(("ollama/", "lm_studio/", "local/")):
        return "local"
    if isinstance(base_url, str) and base_url:
        hostname = urlparse(base_url).hostname
        if hostname in {"localhost", "127.0.0.1", "::1"}:
            return "local"
    return "remote_or_unknown"
