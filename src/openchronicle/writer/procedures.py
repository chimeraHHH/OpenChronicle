"""Validation and canonical rendering for reviewed procedural-memory candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass

PROCEDURE_TYPES = frozenset({"workflow", "checklist", "template"})


@dataclass(frozen=True, slots=True)
class ProcedureSpec:
    title: str
    procedure_type: str
    scope: str
    trigger: str
    steps: tuple[str, ...]
    template: str = ""


def make_procedure_spec(
    *,
    title: object,
    procedure_type: object,
    scope: object,
    trigger: object,
    steps: object,
    template: object = "",
) -> ProcedureSpec:
    clean_type = _single_line(procedure_type, "procedure_type", 40).casefold()
    if clean_type not in PROCEDURE_TYPES:
        raise ValueError("procedure_type must be workflow, checklist, or template")
    if not isinstance(steps, list) or not 2 <= len(steps) <= 12:
        raise ValueError("procedure steps must contain 2–12 items")
    clean_steps = tuple(
        _bounded_text(item, f"procedure step {index}", 1_000)
        for index, item in enumerate(steps, start=1)
    )
    clean_template = "" if template in (None, "") else _bounded_text(template, "template", 5_000)
    if clean_type == "template" and not clean_template:
        raise ValueError("template procedures require template text")
    if clean_type != "template" and clean_template:
        raise ValueError("template text is only valid for template procedures")
    return ProcedureSpec(
        title=_single_line(title, "procedure title", 160),
        procedure_type=clean_type,
        scope=_single_line(scope, "procedure scope", 160),
        trigger=_bounded_text(trigger, "procedure trigger", 1_000),
        steps=clean_steps,
        template=clean_template,
    )


def render_procedure(spec: ProcedureSpec) -> str:
    lines = [
        f"**Procedure:** {spec.title}",
        f"**Type:** {spec.procedure_type}",
        f"**Scope:** {spec.scope}",
        f"**Use when:** {spec.trigger}",
        "**Action capability:** text-generation context only; never executes computer actions.",
        "",
        "Steps:",
    ]
    lines.extend(f"{index}. {step}" for index, step in enumerate(spec.steps, start=1))
    if spec.template:
        fence = _markdown_fence(spec.template)
        lines.extend(["", "Template:", f"{fence}text", spec.template, fence])
    return "\n".join(lines)


def _markdown_fence(text: str) -> str:
    longest = max((len(match) for match in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _single_line(value: object, label: str, limit: int) -> str:
    text = _bounded_text(value, label, limit)
    if "\n" in text or "\r" in text:
        raise ValueError(f"{label} must be one line")
    return text


def _bounded_text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} is invalid")
    return value.strip()
