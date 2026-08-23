"""A/B evaluation for reviewed procedural memory in Prompt Rescue."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config as config_mod
from ..config import Config
from ..prompt_rescue.service import generate_output

Provider = Callable[["PromptMemoryCase", bool], object]


@dataclass(frozen=True, slots=True)
class CaseInput:
    rough_prompt: str
    target: str
    audience: str
    constraints: tuple[str, ...]
    desired_format: str


@dataclass(frozen=True, slots=True)
class MemoryItem:
    memory_id: str
    path: str
    content: str

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "path": self.path,
            "content": self.content,
            "truncated": False,
        }


@dataclass(frozen=True, slots=True)
class Gold:
    memory_should_apply: bool
    required_current_anchors: tuple[str, ...]
    required_memory_anchors: tuple[str, ...]
    forbidden_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromptMemoryCase:
    id: str
    input: CaseInput
    memory_items: tuple[MemoryItem, ...]
    gold: Gold


@dataclass(frozen=True, slots=True)
class Dataset:
    id: str
    split: str
    cases: tuple[PromptMemoryCase, ...]
    digest: str


def load_dataset(path: Path) -> Dataset:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("prompt memory fixture is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "dataset_id",
        "split",
        "cases",
    }:
        raise ValueError("prompt memory fixture envelope is invalid")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported prompt memory fixture schema")
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or not 4 <= len(raw_cases) <= 100:
        raise ValueError("prompt memory fixture requires 4-100 cases")
    cases = tuple(_parse_case(value) for value in raw_cases)
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("prompt memory case ids must be unique")
    if not any(case.gold.memory_should_apply for case in cases) or all(
        case.gold.memory_should_apply for case in cases
    ):
        raise ValueError("prompt memory fixture requires apply and ignore cases")
    return Dataset(
        id=_identifier(payload["dataset_id"]),
        split=_identifier(payload["split"]),
        cases=cases,
        digest=hashlib.sha256(raw).hexdigest(),
    )


def run_evaluation(
    *,
    dataset_path: Path,
    metric_contract_path: Path,
    provider: Provider,
    provider_identity: dict[str, str],
) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    contract_bytes = metric_contract_path.read_bytes()
    try:
        contract = json.loads(contract_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("prompt memory metric contract is not valid JSON") from exc
    _validate_contract(contract, dataset)

    variants: dict[str, Any] = {}
    for name, with_memory in (("no_memory", False), ("reviewed_memory", True)):
        outcomes = [_evaluate_case(case, with_memory, provider) for case in dataset.cases]
        metrics = _metrics(outcomes)
        variants[name] = {
            "description": (
                "Prompt Rescue receives no reviewed memory."
                if not with_memory
                else "Prompt Rescue receives the frozen reviewed procedure context."
            ),
            "provider": dict(provider_identity),
            "action_capability": "none",
            "metrics": metrics,
            "cases": outcomes,
        }

    baseline = variants["no_memory"]["metrics"]
    conditioned = variants["reviewed_memory"]["metrics"]
    delta = round(
        conditioned["applicable_memory_anchor_coverage"]
        - baseline["applicable_memory_anchor_coverage"],
        6,
    )
    verdict = _gate(conditioned, delta, contract["gates"])
    variants["reviewed_memory"]["gate_verdict"] = verdict
    variants["no_memory"]["gate_verdict"] = {"passed": False, "diagnostic_only": True}
    return {
        "schema_version": 1,
        "evaluation_id": "vida-prompt-memory-v1",
        "dataset": {
            "id": dataset.id,
            "split": dataset.split,
            "case_count": len(dataset.cases),
            "sha256": dataset.digest,
        },
        "metric_contract": {"sha256": hashlib.sha256(contract_bytes).hexdigest()},
        "variants": variants,
        "comparison": {"applicable_memory_anchor_coverage_delta": delta},
        "decision": {
            "reviewed_memory_ready_for_prompt_rescue_pilot": verdict["passed"],
            "reply_rescue_scope_changed": False,
            "computer_use_added": False,
        },
    }


def configured_provider(cfg: Config) -> Provider:
    def call(case: PromptMemoryCase, with_memory: bool) -> object:
        return generate_output(
            cfg,
            rough_prompt=case.input.rough_prompt,
            target=case.input.target,
            audience=case.input.audience,
            constraints=case.input.constraints,
            desired_format=case.input.desired_format,
            reviewed_memory_context={
                "items": [item.to_prompt_dict() for item in case.memory_items]
                if with_memory
                else []
            },
        )

    return call


def write_report(report: dict[str, Any], output: Path | None) -> str:
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    return encoded


def _evaluate_case(
    case: PromptMemoryCase,
    with_memory: bool,
    provider: Provider,
) -> dict[str, Any]:
    try:
        output = _parse_output(provider(case, with_memory))
    except Exception as exc:  # noqa: BLE001 - closed evaluation failure state
        return {
            "case_id": case.id,
            "parse_success": False,
            "error": type(exc).__name__,
            "current_anchor_hits": [],
            "memory_anchor_hits": [],
            "forbidden_hits": [],
            "action_boundary": False,
        }
    text = " ".join(output["improved_prompt"].split()).casefold()
    current_hits = _hits(text, case.gold.required_current_anchors)
    memory_hits = _hits(text, case.gold.required_memory_anchors)
    forbidden_hits = _hits(text, case.gold.forbidden_terms)
    return {
        "case_id": case.id,
        "parse_success": True,
        "memory_should_apply": case.gold.memory_should_apply,
        "current_anchor_hits": current_hits,
        "current_anchor_total": len(case.gold.required_current_anchors),
        "memory_anchor_hits": memory_hits,
        "memory_anchor_total": len(case.gold.required_memory_anchors),
        "forbidden_hits": forbidden_hits,
        "forbidden_total": len(case.gold.forbidden_terms),
        "action_boundary": output["action_capability"] == "none",
        "output": output,
    }


def _metrics(outcomes: list[dict[str, Any]]) -> dict[str, float]:
    parsed = [row for row in outcomes if row["parse_success"]]
    applicable = [row for row in parsed if row["memory_should_apply"]]
    return {
        "parse_success_rate": _ratio(len(parsed), len(outcomes)),
        "current_anchor_coverage": _coverage(parsed, "current_anchor"),
        "applicable_memory_anchor_coverage": _coverage(applicable, "memory_anchor"),
        "forbidden_hit_rate": _ratio(
            sum(len(row["forbidden_hits"]) for row in parsed),
            sum(row["forbidden_total"] for row in parsed),
        ),
        "action_boundary_rate": _ratio(
            sum(bool(row["action_boundary"]) for row in parsed),
            len(parsed),
        ),
    }


def _coverage(rows: list[dict[str, Any]], prefix: str) -> float:
    return _ratio(
        sum(len(row[f"{prefix}_hits"]) for row in rows),
        sum(row[f"{prefix}_total"] for row in rows),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


def _gate(metrics: dict[str, float], delta: float, gates: dict[str, object]) -> dict[str, Any]:
    checks = {
        "parse_success_rate": metrics["parse_success_rate"]
        >= float(gates["parse_success_rate_min"]),
        "current_anchor_coverage": metrics["current_anchor_coverage"]
        >= float(gates["current_anchor_coverage_min"]),
        "applicable_memory_anchor_coverage": metrics[
            "applicable_memory_anchor_coverage"
        ]
        >= float(gates["applicable_memory_anchor_coverage_min"]),
        "forbidden_hit_rate": metrics["forbidden_hit_rate"]
        <= float(gates["forbidden_hit_rate_max"]),
        "action_boundary_rate": metrics["action_boundary_rate"]
        >= float(gates["action_boundary_rate_min"]),
        "memory_coverage_delta": delta >= float(gates["memory_coverage_delta_min"]),
    }
    return {"passed": all(checks.values()), "checks": checks}


def _parse_case(value: object) -> PromptMemoryCase:
    if not isinstance(value, dict) or set(value) != {"id", "input", "memory_items", "gold"}:
        raise ValueError("prompt memory case envelope is invalid")
    raw_input = value["input"]
    raw_memory = value["memory_items"]
    raw_gold = value["gold"]
    if not isinstance(raw_input, dict) or set(raw_input) != {
        "rough_prompt",
        "target",
        "audience",
        "constraints",
        "desired_format",
    }:
        raise ValueError("prompt memory input is invalid")
    constraints = _strings(raw_input["constraints"], allow_empty=True)
    if not isinstance(raw_memory, list) or len(raw_memory) > 3:
        raise ValueError("prompt memory items are invalid")
    memory = tuple(_parse_memory(item) for item in raw_memory)
    if not isinstance(raw_gold, dict) or set(raw_gold) != {
        "memory_should_apply",
        "required_current_anchors",
        "required_memory_anchors",
        "forbidden_terms",
    }:
        raise ValueError("prompt memory gold contract is invalid")
    should_apply = raw_gold["memory_should_apply"]
    if type(should_apply) is not bool:
        raise ValueError("prompt memory apply label is invalid")
    required_memory = _strings(raw_gold["required_memory_anchors"], allow_empty=True)
    if should_apply != bool(required_memory):
        raise ValueError("prompt memory apply label and anchors disagree")
    return PromptMemoryCase(
        id=_identifier(value["id"]),
        input=CaseInput(
            rough_prompt=_text(raw_input["rough_prompt"], nonempty=True),
            target=_text(raw_input["target"]),
            audience=_text(raw_input["audience"]),
            constraints=constraints,
            desired_format=_text(raw_input["desired_format"]),
        ),
        memory_items=memory,
        gold=Gold(
            memory_should_apply=should_apply,
            required_current_anchors=_strings(
                raw_gold["required_current_anchors"], allow_empty=False
            ),
            required_memory_anchors=required_memory,
            forbidden_terms=_strings(raw_gold["forbidden_terms"], allow_empty=True),
        ),
    )


def _parse_memory(value: object) -> MemoryItem:
    if not isinstance(value, dict) or set(value) != {"memory_id", "path", "content"}:
        raise ValueError("prompt memory item is invalid")
    path = _text(value["path"], nonempty=True)
    if not path.startswith("procedure-") or not path.endswith(".md"):
        raise ValueError("prompt memory item path is invalid")
    return MemoryItem(
        memory_id=_identifier(value["memory_id"]),
        path=path,
        content=_text(value["content"], nonempty=True),
    )


def _parse_output(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    expected = {
        "schema_version",
        "workflow",
        "action_capability",
        "improved_prompt",
        "assumptions",
        "missing_context",
        "changes",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != 1
        or value.get("workflow") != "prompt_rescue"
        or value.get("action_capability") != "none"
        or not isinstance(value.get("improved_prompt"), str)
        or not value["improved_prompt"].strip()
        or any(not isinstance(value.get(field), list) for field in expected & {
            "assumptions", "missing_context", "changes"
        })
    ):
        raise ValueError("prompt memory output is invalid")
    return dict(value)


def _hits(normalized_text: str, anchors: tuple[str, ...]) -> list[str]:
    return [
        anchor
        for anchor in anchors
        if " ".join(anchor.split()).casefold() in normalized_text
    ]


def _validate_contract(contract: object, dataset: Dataset) -> None:
    if not isinstance(contract, dict) or set(contract) != {
        "schema_version",
        "evaluation_id",
        "dataset",
        "gates",
    }:
        raise ValueError("prompt memory metric contract envelope is invalid")
    expected_gates = {
        "parse_success_rate_min",
        "current_anchor_coverage_min",
        "applicable_memory_anchor_coverage_min",
        "forbidden_hit_rate_max",
        "action_boundary_rate_min",
        "memory_coverage_delta_min",
    }
    if (
        contract["schema_version"] != 1
        or contract["evaluation_id"] != "vida-prompt-memory-v1"
        or contract["dataset"] != {"id": dataset.id, "split": dataset.split}
        or not isinstance(contract["gates"], dict)
        or set(contract["gates"]) != expected_gates
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= float(value) <= 1
            for value in contract["gates"].values()
        )
    ):
        raise ValueError("prompt memory metric contract does not match dataset")


def _identifier(value: object) -> str:
    text = _text(value, nonempty=True)
    if len(text) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in text):
        raise ValueError("prompt memory identifier is invalid")
    return text


def _text(value: object, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > 20_000:
        raise ValueError("prompt memory text is invalid")
    if nonempty and not value.strip():
        raise ValueError("prompt memory text is required")
    return value


def _strings(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("prompt memory string list is invalid")
    result = tuple(_text(item, nonempty=True) for item in value)
    if not allow_empty and not result:
        raise ValueError("prompt memory string list is required")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    benchmark_root = repository_root / "benchmarks" / "vida-prompt-memory-v1"
    dataset_path = args.dataset or benchmark_root / "fixtures" / "cases.json"
    contract_path = args.contract or benchmark_root / "json" / "metric_contract.json"
    cfg = config_mod.load(args.config) if args.config else config_mod.load()
    model_cfg = cfg.model_for("prompt_rescue")
    report = run_evaluation(
        dataset_path=dataset_path,
        metric_contract_path=contract_path,
        provider=configured_provider(cfg),
        provider_identity={
            "stage": "prompt_rescue",
            "provider": model_cfg.provider,
            "model": model_cfg.model,
            "reasoning_effort": model_cfg.reasoning_effort,
        },
    )
    encoded = write_report(report, args.output)
    if not args.quiet:
        sys.stdout.write(encoded)
    return 0 if report["variants"]["reviewed_memory"]["gate_verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
