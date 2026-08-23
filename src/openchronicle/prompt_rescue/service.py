"""Strict no-tool generation and review operations for Prompt Rescue."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Callable
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
from .memory_context import (
    ReviewedMemorySelection,
    prompt_rescue_query,
    reviewed_procedure_refs_are_current,
    select_reviewed_procedures,
)
from .selection import SelectionReceipt

TEMPLATE_VERSION = 2
_OUTPUT_FIELDS = {
    "schema_version",
    "workflow",
    "action_capability",
    "improved_prompt",
    "assumptions",
    "missing_context",
    "changes",
}


class PromptRescueValidationError(RuntimeError):
    """The model output did not match the closed prepared-artifact schema."""


class PromptRescueService:
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

    def queue(
        self,
        *,
        rough_prompt: str,
        target: str = "",
        audience: str = "",
        constraints: list[str] | tuple[str, ...] = (),
        desired_format: str = "",
    ) -> tuple[store.PromptRescueJob, bool]:
        return self._queue(
            source_kind="manual_paste",
            source_binding={},
            rough_prompt=rough_prompt,
            target=target,
            audience=audience,
            constraints=constraints,
            desired_format=desired_format,
        )

    def queue_selection(
        self,
        receipt: SelectionReceipt,
        *,
        target: str = "",
        audience: str = "",
        constraints: list[str] | tuple[str, ...] = (),
        desired_format: str = "",
    ) -> tuple[store.PromptRescueJob, bool]:
        if not isinstance(receipt, SelectionReceipt):
            raise ValueError("prompt rescue selection receipt is invalid")
        return self._queue(
            source_kind="macos_selection",
            source_binding=receipt.binding,
            rough_prompt=receipt.selected_text,
            target=target,
            audience=audience,
            constraints=constraints,
            desired_format=desired_format,
        )

    def _queue(
        self,
        *,
        source_kind: str,
        source_binding: dict[str, Any],
        rough_prompt: str,
        target: str,
        audience: str,
        constraints: list[str] | tuple[str, ...],
        desired_format: str,
    ) -> tuple[store.PromptRescueJob, bool]:
        validate_config(self.cfg)
        if not self.cfg.prompt_rescue.enabled:
            raise ValueError("prompt rescue is disabled")
        normalized = validate_source(
            self.cfg,
            rough_prompt=rough_prompt,
            target=target,
            audience=audience,
            constraints=constraints,
            desired_format=desired_format,
        )
        template = load_prompt("prompt_rescue.md")
        provider = self.provider_summary()
        create_kwargs = {
            "source_kind": source_kind,
            "source_binding": source_binding,
            "rough_prompt": normalized["rough_prompt"],
            "target": normalized["target"],
            "audience": normalized["audience"],
            "constraints": normalized["constraints"],
            "desired_format": normalized["desired_format"],
            "policy_digest": privacy_policy.stored_observation_policy_digest(
                self.cfg.capture
            ),
            "template_version": TEMPLATE_VERSION,
            "template_digest": canonical_digest(
                {"schema": "prompt-rescue-template-v1", "text": template}
            ),
            "model_identity": provider["model"],
            "provider_location": provider["location"],
        }
        job, created = store.create(self.conn, **create_kwargs)
        if created or self._current(job):
            return job, created

        # A prior artifact can become intentionally hidden when one of its
        # reviewed memories is superseded or forgotten. Give the same explicit
        # input a new stable identity for the current memory snapshot instead
        # of returning an unclaimable stale job or mutating an adopted output.
        selection = select_reviewed_procedures(
            self.conn,
            self.cfg,
            query=prompt_rescue_query(
                rough_prompt=normalized["rough_prompt"],
                target=normalized["target"],
                audience=normalized["audience"],
                constraints=normalized["constraints"],
                desired_format=normalized["desired_format"],
            ),
        )
        refresh_salt = canonical_digest(
            {
                "schema": "prompt-rescue-memory-refresh-v1",
                "retrieval_mode": selection.retrieval_mode,
                "sources": [ref.to_dict() for ref in selection.refs],
            }
        )
        return store.create(
            self.conn,
            **create_kwargs,
            identity_salt=refresh_salt,
        )

    def list(self, *, limit: int = 50) -> list[store.PromptRescueJob]:
        return [job for job in store.list_jobs(self.conn, limit=limit) if self._current(job)]

    def get(self, job_id: str) -> store.PromptRescueJob | None:
        job = store.get(self.conn, job_id)
        return job if job is not None and self._current(job) else None

    def process_next(self) -> store.PromptRescueJob | None:
        validate_config(self.cfg)
        if not self.cfg.prompt_rescue.enabled:
            return None
        lease_seconds = max(
            self.cfg.prompt_rescue.lease_seconds,
            math.ceil(llm_mod.call_budget_seconds(self.cfg, "prompt_rescue")),
        )
        if lease_seconds > 21_600:
            raise ValueError("prompt rescue provider budget exceeds safe lease")
        lease_token = uuid.uuid4().hex
        claimed = store.claim_next(
            self.conn,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
        )
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
                    raise PromptRescueValidationError("prompt rescue input changed")
                selection = select_reviewed_procedures(
                    self.conn,
                    self.cfg,
                    query=prompt_rescue_query(
                        rough_prompt=current.rough_prompt,
                        target=current.target,
                        audience=current.audience,
                        constraints=current.constraints,
                        desired_format=current.desired_format,
                    ),
                )
                output = self._generate(current, selection)
                latest = store.get(self.conn, claimed.id)
                if (
                    latest is None
                    or latest.status != "leased"
                    or latest.lease_token != lease_token
                    or not self._current(latest)
                    or not reviewed_procedure_refs_are_current(
                        self.conn,
                        self.cfg,
                        selection.refs,
                    )
                ):
                    raise PromptRescueValidationError("prompt rescue input changed")
                return store.complete(
                    self.conn,
                    job_id=claimed.id,
                    lease_token=lease_token,
                    output=output,
                    memory_context_refs=selection.refs,
                )
        except llm_mod.ProviderCallCancelledError:
            store.release_claim(
                self.conn,
                job_id=claimed.id,
                lease_token=lease_token,
            )
            raise
        except store.PromptRescueConflict:
            raise
        except PromptRescueValidationError as exc:
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

    def retry(
        self,
        job_id: str,
        *,
        expected_version: int,
    ) -> store.PromptRescueJob:
        validate_config(self.cfg)
        if not self.cfg.prompt_rescue.enabled:
            raise ValueError("prompt rescue is disabled")
        current = self.get(job_id)
        if current is None:
            raise store.PromptRescueConflict("prompt rescue changed")
        return store.retry(
            self.conn,
            job_id=job_id,
            expected_version=expected_version,
        )

    def edit(
        self,
        job_id: str,
        *,
        expected_version: int,
        improved_prompt: str,
    ) -> store.PromptRescueJob:
        current = self.get(job_id)
        if current is None or current.output is None:
            raise store.PromptRescueConflict("prompt rescue changed")
        output = dict(current.output)
        output["improved_prompt"] = improved_prompt
        validated = validate_output(self.cfg, output)
        return store.edit_output(
            self.conn,
            job_id=job_id,
            expected_version=expected_version,
            output=validated,
        )

    def delete(self, job_id: str, *, expected_version: int) -> None:
        store.delete(
            self.conn,
            job_id=job_id,
            expected_version=expected_version,
        )

    def _generate(
        self,
        job: store.PromptRescueJob,
        selection: ReviewedMemorySelection,
    ) -> dict[str, Any]:
        return generate_output(
            self.cfg,
            rough_prompt=job.rough_prompt,
            target=job.target,
            audience=job.audience,
            constraints=job.constraints,
            desired_format=job.desired_format,
            reviewed_memory_context=selection.to_prompt_dict(),
            llm_caller=self.llm_caller,
        )

    def _current(self, job: store.PromptRescueJob) -> bool:
        if not _job_is_current(self.conn, job):
            return False
        if not reviewed_procedure_refs_are_current(
            self.conn,
            self.cfg,
            job.memory_context_refs,
        ):
            return False
        if job.source_kind != "macos_selection":
            return True
        if job.policy_digest != privacy_policy.stored_observation_policy_digest(self.cfg.capture):
            return False
        binding = job.source_binding
        decision = privacy_policy.evaluate_window(
            self.cfg.capture,
            app_name=binding["app_name"],
            bundle_id=binding["bundle_id"],
            window_title=binding["window_title"],
        )
        return decision.allowed and not privacy_policy.has_url_policy(self.cfg.capture)


def provider_summary(cfg: Config) -> dict[str, str]:
    model_cfg = cfg.model_for("prompt_rescue")
    model = model_cfg.model
    if not isinstance(model, str) or not model.strip() or len(model) > 256 or "\x00" in model:
        raise ValueError("prompt rescue model identity is invalid")
    return {
        "model": model,
        "location": _provider_location(model, model_cfg.base_url),
    }


def generate_output(
    cfg: Config,
    *,
    rough_prompt: str,
    target: str,
    audience: str,
    constraints: tuple[str, ...],
    desired_format: str,
    reviewed_memory_context: dict[str, object] | None = None,
    llm_caller: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    normalized = validate_source(
        cfg,
        rough_prompt=rough_prompt,
        target=target,
        audience=audience,
        constraints=constraints,
        desired_format=desired_format,
    )
    payload = {
        "rough_prompt": normalized["rough_prompt"],
        "target": normalized["target"],
        "audience": normalized["audience"],
        "constraints": list(normalized["constraints"]),
        "desired_format": normalized["desired_format"],
        "reviewed_memory_context": reviewed_memory_context
        or {"items": []},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise PromptRescueValidationError("prompt rescue input changed")
    response = (llm_caller or llm_mod.call_llm)(
        cfg,
        "prompt_rescue",
        messages=[
            {"role": "system", "content": load_prompt("prompt_rescue.md")},
            {"role": "user", "content": encoded},
        ],
        json_mode=True,
    )
    text = llm_mod.extract_text(response).strip()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PromptRescueValidationError("prompt rescue output is invalid") from exc
    return validate_output(cfg, raw)


def _job_is_current(
    conn: sqlite3.Connection,
    job: store.PromptRescueJob,
) -> bool:
    sources = provenance_store.direct_sources_checked(
        conn,
        EvidenceRef(kind="prompt_rescue", id=job.id),
    )
    expected_sources = [job.input_ref, *job.memory_context_refs]
    return bool(
        sources == expected_sources
        and all(provenance_store.is_current(conn, source) for source in expected_sources)
    )


def validate_config(cfg: Config) -> None:
    value = cfg.prompt_rescue
    if type(value.enabled) is not bool:
        raise ValueError("prompt_rescue.enabled must be a boolean")
    ranges = {
        "poll_seconds": (1, 300),
        "lease_seconds": (30, 21_600),
        "max_input_chars": (100, 50_000),
        "max_output_chars": (100, 50_000),
    }
    for name, (minimum, maximum) in ranges.items():
        field_value = getattr(value, name)
        if type(field_value) is not int or not minimum <= field_value <= maximum:
            raise ValueError(f"prompt_rescue.{name} is invalid")


def validate_source(
    cfg: Config,
    *,
    rough_prompt: str,
    target: str,
    audience: str,
    constraints: list[str] | tuple[str, ...],
    desired_format: str,
) -> dict[str, Any]:
    if (
        not isinstance(rough_prompt, str)
        or not rough_prompt.strip()
        or "\x00" in rough_prompt
        or len(rough_prompt) > cfg.prompt_rescue.max_input_chars
    ):
        raise ValueError("rough prompt is invalid")
    scalar_limits = {
        "target": (target, 500),
        "audience": (audience, 500),
        "desired_format": (desired_format, 500),
    }
    normalized: dict[str, Any] = {"rough_prompt": rough_prompt}
    for name, (value, limit) in scalar_limits.items():
        if not isinstance(value, str) or "\x00" in value or len(value) > limit:
            raise ValueError(f"prompt rescue {name} is invalid")
        normalized[name] = value.strip()
    if not isinstance(constraints, (list, tuple)) or len(constraints) > 20:
        raise ValueError("prompt rescue constraints are invalid")
    normalized_constraints: list[str] = []
    for value in constraints:
        if not isinstance(value, str) or "\x00" in value or not value.strip() or len(value) > 500:
            raise ValueError("prompt rescue constraint is invalid")
        normalized_constraints.append(value.strip())
    normalized["constraints"] = tuple(normalized_constraints)
    declared_chars = sum(
        len(value)
        for value in (
            normalized["rough_prompt"],
            normalized["target"],
            normalized["audience"],
            normalized["desired_format"],
            *normalized_constraints,
        )
    )
    if declared_chars > cfg.prompt_rescue.max_input_chars:
        raise ValueError("prompt rescue input exceeds max_input_chars")
    return normalized


def validate_output(cfg: Config, raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _OUTPUT_FIELDS:
        raise PromptRescueValidationError("prompt rescue output schema is invalid")
    improved = raw.get("improved_prompt")
    if (
        raw.get("schema_version") != 1
        or raw.get("workflow") != "prompt_rescue"
        or raw.get("action_capability") != "none"
        or not isinstance(improved, str)
        or not improved.strip()
        or "\x00" in improved
        or len(improved) > cfg.prompt_rescue.max_output_chars
    ):
        raise PromptRescueValidationError("prompt rescue output is invalid")
    result: dict[str, Any] = {
        "schema_version": 1,
        "workflow": "prompt_rescue",
        "action_capability": "none",
        "improved_prompt": improved,
    }
    for name in ("assumptions", "missing_context", "changes"):
        value = raw.get(name)
        if not isinstance(value, list) or len(value) > 20:
            raise PromptRescueValidationError("prompt rescue output is invalid")
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str) or "\x00" in item or not item.strip() or len(item) > 1_000:
                raise PromptRescueValidationError("prompt rescue output is invalid")
            cleaned.append(item.strip())
        result[name] = cleaned
    return result


def _provider_location(model: str, base_url: object) -> str:
    lowered = model.casefold()
    if lowered.startswith(("ollama/", "lm_studio/", "local/")):
        return "local"
    if isinstance(base_url, str) and base_url:
        hostname = urlparse(base_url).hostname
        if hostname in {"localhost", "127.0.0.1", "::1"}:
            return "local"
    return "remote_or_unknown"
