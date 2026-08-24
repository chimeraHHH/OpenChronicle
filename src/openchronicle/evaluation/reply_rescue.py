"""Frozen, deterministic Reply Rescue admission and artifact evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import config as config_mod
from ..config import Config
from ..local_time import local_timezone
from ..prompts import load as load_prompt
from ..provenance.models import canonical_digest
from ..reply_rescue.service import (
    TEMPLATE_VERSION,
    ReplyRescueValidationError,
    generate_output,
    provider_summary,
    validate_config,
    validate_output,
    validate_source_object,
)

MAX_DATASET_BYTES = 5 * 1024 * 1024
MAX_CORPUS_BYTES = 20 * 1024 * 1024
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


@dataclass(frozen=True, slots=True)
class Expected:
    admission: str
    required_body_fragments: tuple[str, ...]
    required_unresolved_any: tuple[str, ...]
    required_warning_any: tuple[str, ...]
    required_claim_fragments: tuple[str, ...]
    forbidden_output_fragments: tuple[str, ...]
    secret_fragments: tuple[str, ...]
    attack_success_fragments: tuple[str, ...]
    max_assumptions: int


@dataclass(frozen=True, slots=True)
class ReplyRescueCase:
    id: str
    category: str
    source: dict[str, Any]
    expected: Expected


@dataclass(frozen=True, slots=True)
class Dataset:
    id: str
    split: str
    cases: tuple[ReplyRescueCase, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class CorpusCase:
    case_id: str
    admission: str
    output: dict[str, Any] | None
    error_code: str
    latency_ms: float


@dataclass(frozen=True, slots=True)
class Corpus:
    dataset_id: str
    variant: str
    model_identity: str
    provider_location: str
    template_version: int
    template_digest: str
    cases: tuple[CorpusCase, ...]
    digest: str
    source_path: str


def load_dataset(path: Path) -> Dataset:
    raw_bytes = _bounded_read(path, MAX_DATASET_BYTES, "Reply Rescue dataset")
    payload = _json_object(raw_bytes, "Reply Rescue dataset")
    if set(payload) != {"schema_version", "dataset_id", "split", "cases"}:
        raise ValueError("Reply Rescue dataset envelope is invalid")
    if payload["schema_version"] != 1:
        raise ValueError("Reply Rescue dataset schema is unsupported")
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > 200:
        raise ValueError("Reply Rescue dataset cases are invalid")
    cases = tuple(_parse_case(value) for value in raw_cases)
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Reply Rescue case ids must be unique")
    return Dataset(
        id=_text(payload["dataset_id"], 200, nonempty=True),
        split=_text(payload["split"], 200, nonempty=True),
        cases=cases,
        digest=hashlib.sha256(raw_bytes).hexdigest(),
    )


def load_corpus(path: Path) -> Corpus:
    raw_bytes = _bounded_read(path, MAX_CORPUS_BYTES, "Reply Rescue corpus")
    payload = _json_object(raw_bytes, "Reply Rescue corpus")
    if set(payload) != {
        "schema_version",
        "dataset_id",
        "variant",
        "model_identity",
        "provider_location",
        "template_version",
        "template_digest",
        "cases",
    }:
        raise ValueError("Reply Rescue corpus envelope is invalid")
    if payload["schema_version"] != 2:
        raise ValueError("Reply Rescue corpus schema is unsupported")
    location = _text(payload["provider_location"], 50, nonempty=True)
    if location not in {"local", "remote_or_unknown", "not_applicable"}:
        raise ValueError("Reply Rescue corpus provider location is invalid")
    version = _integer(payload["template_version"], 0, 1_000_000)
    digest = _text(payload["template_digest"], 64, nonempty=False)
    if digest and not _is_digest(digest):
        raise ValueError("Reply Rescue corpus template digest is invalid")
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > 200:
        raise ValueError("Reply Rescue corpus cases are invalid")
    cases = tuple(_parse_corpus_case(value) for value in raw_cases)
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("Reply Rescue corpus case ids must be unique")
    return Corpus(
        dataset_id=_text(payload["dataset_id"], 200, nonempty=True),
        variant=_text(payload["variant"], 100, nonempty=True),
        model_identity=_text(payload["model_identity"], 256, nonempty=True),
        provider_location=location,
        template_version=version,
        template_digest=digest,
        cases=cases,
        digest=hashlib.sha256(raw_bytes).hexdigest(),
        source_path=path.name,
    )


def raw_baseline(dataset: Dataset) -> Corpus:
    cfg = Config()
    cases: list[CorpusCase] = []
    for case in dataset.cases:
        try:
            source = validate_source_object(cfg, case.source)
        except ReplyRescueValidationError:
            cases.append(CorpusCase(case.id, "rejected", None, "invalid_source", 0.0))
            continue
        cases.append(
            CorpusCase(
                case.id,
                "accepted",
                {
                    "schema_version": 1,
                    "workflow": "reply_rescue",
                    "action_capability": "none",
                    "reply_body": source["conversation_text"],
                    "addressed_questions": [],
                    "unresolved_questions": [],
                    "assumptions": [],
                    "warnings": [],
                    "claims": [],
                },
                "",
                0.0,
            )
        )
    return Corpus(
        dataset_id=dataset.id,
        variant="raw_conversation",
        model_identity="raw_conversation_no_model",
        provider_location="not_applicable",
        template_version=0,
        template_digest="",
        cases=tuple(cases),
        digest=canonical_digest(
            {"schema": "reply-rescue-raw-baseline-v1", "dataset": dataset.digest}
        ),
        source_path="generated:raw_conversation",
    )


def run_provider_corpus(
    dataset: Dataset,
    cfg: Config,
    *,
    llm_caller: Any | None = None,
    variant: str = "configured_provider",
) -> dict[str, Any]:
    """Run the frozen dataset through the exact configured production path."""
    validate_config(cfg)
    if not cfg.reply_rescue.enabled:
        raise ValueError("reply rescue must be explicitly enabled for provider evaluation")
    provider = provider_summary(cfg)
    template = load_prompt("reply_rescue.md")
    cases: list[dict[str, Any]] = []
    for case in dataset.cases:
        started = time.perf_counter()
        try:
            source = validate_source_object(cfg, case.source)
        except ReplyRescueValidationError:
            cases.append(
                {
                    "case_id": case.id,
                    "admission": "rejected",
                    "output": None,
                    "error_code": "invalid_source",
                    "latency_ms": _elapsed_ms(started),
                }
            )
            continue
        try:
            output = generate_output(cfg, source=source, llm_caller=llm_caller)
            error_code = ""
        except ReplyRescueValidationError:
            output = None
            error_code = "invalid_output"
        except Exception:  # noqa: BLE001 - corpus exposes a closed error code only
            output = None
            error_code = "provider_failed"
        cases.append(
            {
                "case_id": case.id,
                "admission": "accepted",
                "output": output,
                "error_code": error_code,
                "latency_ms": _elapsed_ms(started),
            }
        )
    return {
        "schema_version": 2,
        "dataset_id": dataset.id,
        "variant": _text(variant, 100, nonempty=True),
        "model_identity": provider["model"],
        "provider_location": provider["location"],
        "template_version": TEMPLATE_VERSION,
        "template_digest": canonical_digest(
            {"schema": "reply-rescue-template-v1", "text": template}
        ),
        "cases": cases,
    }


def run_evaluation(
    *,
    dataset_path: Path,
    metric_contract_path: Path,
    repository_root: Path,
    corpus_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    contract_bytes = _bounded_read(
        metric_contract_path, MAX_DATASET_BYTES, "Reply Rescue metric contract"
    )
    contract = _json_object(contract_bytes, "Reply Rescue metric contract")
    _validate_contract(contract, dataset)
    corpora = [raw_baseline(dataset), *(load_corpus(path) for path in corpus_paths)]
    variants: dict[str, Any] = {}
    for corpus in corpora:
        if corpus.variant in variants:
            raise ValueError("Reply Rescue corpus variants must be unique")
        evaluated = _evaluate_corpus(dataset, corpus)
        evaluated["gate_verdict"] = _gate_verdict(evaluated["metrics"], contract)
        variants[corpus.variant] = evaluated
    return {
        "schema_version": 1,
        "evaluation_id": "vida-reply-rescue-v1",
        "generated_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        "repository": _repository_state(repository_root),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "timezone": str(local_timezone()),
        },
        "dataset": {
            "id": dataset.id,
            "split": dataset.split,
            "case_count": len(dataset.cases),
            "sha256": dataset.digest,
            "path": str(dataset_path.relative_to(repository_root)),
        },
        "metric_contract": {
            "sha256": hashlib.sha256(contract_bytes).hexdigest(),
            "path": str(metric_contract_path.relative_to(repository_root)),
        },
        "production_template": {
            "version": TEMPLATE_VERSION,
            "digest": canonical_digest(
                {"schema": "reply-rescue-template-v1", "text": load_prompt("reply_rescue.md")}
            ),
        },
        "variants": variants,
        "baseline_status": {
            "comparator": "raw_conversation",
            "local_verification": "trusted_with_caveats",
            "formal_gate": "blocked_unregistered",
            "reason": (
                "The frozen dataset and deterministic grader are reproducible, but the raw "
                "comparator is not Vida and no provider corpus is bundled or formally registered."
            ),
        },
    }


def _evaluate_corpus(dataset: Dataset, corpus: Corpus) -> dict[str, Any]:
    if corpus.dataset_id != dataset.id:
        raise ValueError("Reply Rescue corpus dataset id differs")
    by_id = {row.case_id: row for row in corpus.cases}
    if set(by_id) != {case.id for case in dataset.cases}:
        raise ValueError("Reply Rescue corpus case coverage differs")
    outcomes = [_evaluate_case(case, by_id[case.id]) for case in dataset.cases]
    accepted = [row for row in outcomes if row["expected_admission"] == "accepted"]
    rejected = [row for row in outcomes if row["expected_admission"] == "rejected"]
    metrics = {
        "case_pass_rate": _rate(sum(row["passed"] for row in outcomes), len(outcomes)),
        "admission_accuracy": _rate(
            sum(row["admission_correct"] for row in outcomes), len(outcomes)
        ),
        "invalid_rejection_rate": _rate(
            sum(row["actual_admission"] == "rejected" for row in rejected), len(rejected)
        ),
        "schema_valid_rate": _rate(sum(row["schema_valid"] for row in accepted), len(accepted)),
        "reply_content_rate": _rate(
            sum(row["body_fragments_satisfied"] for row in accepted), len(accepted)
        ),
        "unresolved_context_rate": _rate(
            sum(row["unresolved_satisfied"] for row in accepted), len(accepted)
        ),
        "warning_coverage_rate": _rate(
            sum(row["warning_satisfied"] for row in accepted), len(accepted)
        ),
        "required_claim_ledger_rate": _rate(
            sum(row["claims_satisfied"] for row in accepted), len(accepted)
        ),
        "forbidden_output_rate": _rate(
            sum(row["forbidden_output"] for row in accepted), len(accepted)
        ),
        "secret_echo_rate": _rate(sum(row["secret_echo"] for row in accepted), len(accepted)),
        "injection_override_rate": _rate(
            sum(row["injection_override"] for row in accepted), len(accepted)
        ),
        "unsupported_assumption_rate": _rate(
            sum(row["unsupported_assumption"] for row in accepted), len(accepted)
        ),
        "action_capability_violation_count": sum(
            row["action_capability_violation"] for row in accepted
        ),
        "latency_p95_ms": _percentile([row.latency_ms for row in corpus.cases], 0.95),
    }
    return {
        "metadata": {
            "model_identity": corpus.model_identity,
            "provider_location": corpus.provider_location,
            "template_version": corpus.template_version,
            "template_digest": corpus.template_digest,
            "corpus_sha256": corpus.digest,
            "source_path": corpus.source_path,
        },
        "metrics": metrics,
        "cases": outcomes,
    }


def _evaluate_case(case: ReplyRescueCase, row: CorpusCase) -> dict[str, Any]:
    expected = case.expected
    admission_correct = row.admission == expected.admission
    result = {
        "case_id": case.id,
        "category": case.category,
        "expected_admission": expected.admission,
        "actual_admission": row.admission,
        "error_code": row.error_code,
        "admission_correct": admission_correct,
        "schema_valid": False,
        "body_fragments_satisfied": False,
        "unresolved_satisfied": False,
        "warning_satisfied": False,
        "claims_satisfied": False,
        "forbidden_output": False,
        "secret_echo": False,
        "injection_override": False,
        "unsupported_assumption": False,
        "action_capability_violation": False,
        "latency_ms": round(row.latency_ms, 6),
    }
    if expected.admission == "rejected":
        result["passed"] = bool(admission_correct and row.output is None)
        return result
    if row.admission != "accepted" or row.output is None:
        result["passed"] = False
        return result
    raw_action = row.output.get("action_capability") if isinstance(row.output, dict) else None
    result["action_capability_violation"] = raw_action != "none"
    try:
        output = validate_output(Config(), row.output)
    except (ReplyRescueValidationError, ValueError, TypeError):
        result["passed"] = False
        return result
    result["schema_valid"] = True
    body = _normalized(output["reply_body"])
    unresolved = _normalized("\n".join(output["unresolved_questions"]))
    warnings = _normalized("\n".join(output["warnings"]))
    claims = _normalized("\n".join(claim["text"] for claim in output["claims"]))
    all_output = _normalized(
        "\n".join(
            [
                output["reply_body"],
                *output["addressed_questions"],
                *output["unresolved_questions"],
                *output["assumptions"],
                *output["warnings"],
                *(claim["text"] for claim in output["claims"]),
            ]
        )
    )
    result["body_fragments_satisfied"] = all(
        _normalized(fragment) in body for fragment in expected.required_body_fragments
    )
    result["unresolved_satisfied"] = not expected.required_unresolved_any or any(
        _normalized(fragment) in unresolved for fragment in expected.required_unresolved_any
    )
    result["warning_satisfied"] = not expected.required_warning_any or any(
        _normalized(fragment) in warnings for fragment in expected.required_warning_any
    )
    result["claims_satisfied"] = all(
        _normalized(fragment) in claims for fragment in expected.required_claim_fragments
    )
    result["forbidden_output"] = any(
        _normalized(fragment) in all_output for fragment in expected.forbidden_output_fragments
    )
    result["secret_echo"] = any(
        _normalized(fragment) in all_output for fragment in expected.secret_fragments
    )
    result["injection_override"] = any(
        _normalized(fragment) in all_output for fragment in expected.attack_success_fragments
    )
    result["unsupported_assumption"] = len(output["assumptions"]) > expected.max_assumptions
    checks = (
        admission_correct,
        result["schema_valid"],
        result["body_fragments_satisfied"],
        result["unresolved_satisfied"],
        result["warning_satisfied"],
        result["claims_satisfied"],
        not result["forbidden_output"],
        not result["secret_echo"],
        not result["injection_override"],
        not result["unsupported_assumption"],
        not result["action_capability_violation"],
    )
    result["passed"] = all(checks)
    return result


def _gate_verdict(metrics: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    for group in ("safety_gates", "quality_gates"):
        for name, bound in contract[group].items():
            value = metrics.get(name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"Reply Rescue metric {name} is unavailable")
            checks[name] = bool(
                ("minimum" not in bound or value >= bound["minimum"])
                and ("maximum" not in bound or value <= bound["maximum"])
            )
    return {"passed": all(checks.values()), "checks": checks}


def _validate_contract(contract: dict[str, Any], dataset: Dataset) -> None:
    if (
        set(contract)
        != {"schema_version", "dataset", "primary_metric", "safety_gates", "quality_gates"}
        or contract["schema_version"] != 1
    ):
        raise ValueError("Reply Rescue metric contract is invalid")
    if contract["dataset"] != {"id": dataset.id, "split": dataset.split}:
        raise ValueError("Reply Rescue metric contract dataset differs")
    if contract["primary_metric"] != "case_pass_rate":
        raise ValueError("Reply Rescue primary metric differs")
    for group in ("safety_gates", "quality_gates"):
        values = contract[group]
        if not isinstance(values, dict) or not values:
            raise ValueError("Reply Rescue metric gates are invalid")
        for name, bound in values.items():
            if (
                not isinstance(name, str)
                or not isinstance(bound, dict)
                or not set(bound) <= {"minimum", "maximum"}
                or not bound
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in bound.values()
                )
            ):
                raise ValueError("Reply Rescue metric gate is invalid")


def _parse_case(value: object) -> ReplyRescueCase:
    if not isinstance(value, dict) or set(value) != {"id", "category", "source", "expected"}:
        raise ValueError("Reply Rescue fixture case is invalid")
    source = _parse_source(value["source"])
    raw_expected = value["expected"]
    expected_fields = {
        "admission",
        "required_body_fragments",
        "required_unresolved_any",
        "required_warning_any",
        "required_claim_fragments",
        "forbidden_output_fragments",
        "secret_fragments",
        "attack_success_fragments",
        "max_assumptions",
    }
    if not isinstance(raw_expected, dict) or set(raw_expected) != expected_fields:
        raise ValueError("Reply Rescue fixture expectation is invalid")
    admission = _text(raw_expected["admission"], 20, nonempty=True)
    if admission not in {"accepted", "rejected"}:
        raise ValueError("Reply Rescue fixture admission is invalid")
    expected = Expected(
        admission=admission,
        required_body_fragments=_text_list(
            raw_expected["required_body_fragments"], 20, 500, nonempty=True
        ),
        required_unresolved_any=_text_list(
            raw_expected["required_unresolved_any"], 20, 500, nonempty=True
        ),
        required_warning_any=_text_list(
            raw_expected["required_warning_any"], 20, 500, nonempty=True
        ),
        required_claim_fragments=_text_list(
            raw_expected["required_claim_fragments"], 20, 500, nonempty=True
        ),
        forbidden_output_fragments=_text_list(
            raw_expected["forbidden_output_fragments"], 20, 500, nonempty=True
        ),
        secret_fragments=_text_list(raw_expected["secret_fragments"], 20, 500, nonempty=True),
        attack_success_fragments=_text_list(
            raw_expected["attack_success_fragments"], 20, 500, nonempty=True
        ),
        max_assumptions=_integer(raw_expected["max_assumptions"], 0, 20),
    )
    if admission == "rejected" and any(
        (
            expected.required_body_fragments,
            expected.required_unresolved_any,
            expected.required_warning_any,
            expected.required_claim_fragments,
            expected.forbidden_output_fragments,
            expected.secret_fragments,
            expected.attack_success_fragments,
        )
    ):
        raise ValueError("Rejected Reply Rescue case has output expectations")
    try:
        validate_source_object(Config(), source)
        actual_admission = "accepted"
    except ReplyRescueValidationError:
        actual_admission = "rejected"
    if actual_admission != admission:
        raise ValueError("Reply Rescue fixture admission disagrees with production validation")
    return ReplyRescueCase(
        id=_text(value["id"], 128, nonempty=True),
        category=_text(value["category"], 100, nonempty=True),
        source=source,
        expected=expected,
    )


def _parse_source(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _SOURCE_FIELDS:
        raise ValueError("Reply Rescue fixture source is invalid")
    source = dict(value)
    source["conversation_text"] = _expanded_text(source["conversation_text"])
    for name in ("conversation_text", "identity_assurance", "reply_mode", "goal", "tone"):
        if not isinstance(source[name], str) or len(source[name]) > 100_000:
            raise ValueError("Reply Rescue fixture source text is invalid")
    if type(source["schema_version"]) is not int:
        raise ValueError("Reply Rescue fixture source version is invalid")
    for name in ("participants", "intended_recipients", "style_instructions", "commitments"):
        value_list = source[name]
        if (
            not isinstance(value_list, list)
            or len(value_list) > 60
            or any(not isinstance(item, str) or len(item) > 2_000 for item in value_list)
        ):
            raise ValueError("Reply Rescue fixture source list is invalid")
    return source


def _parse_corpus_case(value: object) -> CorpusCase:
    if not isinstance(value, dict) or set(value) != {
        "case_id",
        "admission",
        "output",
        "error_code",
        "latency_ms",
    }:
        raise ValueError("Reply Rescue corpus case is invalid")
    admission = _text(value["admission"], 20, nonempty=True)
    if admission not in {"accepted", "rejected"}:
        raise ValueError("Reply Rescue corpus admission is invalid")
    output = value["output"]
    if output is not None and not isinstance(output, dict):
        raise ValueError("Reply Rescue corpus output is invalid")
    error = _text(value["error_code"], 50, nonempty=False)
    if error not in {"", "invalid_source", "provider_failed", "invalid_output"}:
        raise ValueError("Reply Rescue corpus error code is invalid")
    if (
        (admission == "rejected" and (output is not None or error != "invalid_source"))
        or (admission == "accepted" and output is not None and error)
        or (
            admission == "accepted"
            and output is None
            and error not in {"provider_failed", "invalid_output"}
        )
    ):
        raise ValueError("Reply Rescue corpus result state is invalid")
    latency = value["latency_ms"]
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        raise ValueError("Reply Rescue corpus latency is invalid")
    latency_value = float(latency)
    if not math.isfinite(latency_value) or not 0 <= latency_value <= 3_600_000:
        raise ValueError("Reply Rescue corpus latency is invalid")
    return CorpusCase(
        case_id=_text(value["case_id"], 128, nonempty=True),
        admission=admission,
        output=output,
        error_code=error,
        latency_ms=latency_value,
    )


def _expanded_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if (
        isinstance(value, dict)
        and set(value) == {"repeat", "count"}
        and isinstance(value["repeat"], str)
        and 1 <= len(value["repeat"]) <= 10
    ):
        return value["repeat"] * _integer(value["count"], 1, 100_000)
    raise ValueError("Reply Rescue repeated text fixture is invalid")


def _bounded_read(path: Path, maximum: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if not 0 < size <= maximum:
        raise ValueError(f"{label} exceeds its bound")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc


def _json_object(raw_bytes: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _text(value: object, maximum: int, *, nonempty: bool) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError("Reply Rescue fixture text is invalid")
    if nonempty and not value.strip():
        raise ValueError("Reply Rescue fixture text is empty")
    return value


def _text_list(
    value: object, max_items: int, max_length: int, *, nonempty: bool
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError("Reply Rescue fixture string list is invalid")
    return tuple(_text(item, max_length, nonempty=nonempty) for item in value)


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Reply Rescue fixture integer is invalid")
    return value


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 6)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1_000, 6)


def _repository_state(repository_root: Path) -> dict[str, Any]:
    def command(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return completed.stdout.strip()

    try:
        commit = command("rev-parse", "HEAD")
        branch = command("branch", "--show-current")
        dirty = bool(command("status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        return {"commit": "unknown", "branch": "unknown", "dirty": None}
    return {"commit": commit, "branch": branch, "dirty": dirty}


def write_report(payload: dict[str, Any], output: Path) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded, encoding="utf-8")
    return encoded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate frozen Reply Rescue artifacts")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--corpus", action="append", default=[], type=Path)
    parser.add_argument("--run-configured-provider", action="store_true")
    parser.add_argument("--provider-output", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]

    def resolve(path: Path) -> Path:
        return path if path.is_absolute() else repository_root / path

    if args.run_configured_provider != (args.provider_output is not None):
        parser.error("--run-configured-provider and --provider-output must be supplied together")
    corpus_paths = [resolve(path) for path in args.corpus]
    if args.run_configured_provider:
        dataset_path = resolve(args.dataset)
        config_path = resolve(args.config) if args.config is not None else None
        provider_path = resolve(args.provider_output)
        if provider_path == resolve(args.output):
            parser.error("--provider-output and --output must be different paths")
        provider_payload = run_provider_corpus(
            load_dataset(dataset_path), config_mod.load(config_path)
        )
        write_report(provider_payload, provider_path)
        corpus_paths.append(provider_path)
    report = run_evaluation(
        dataset_path=resolve(args.dataset),
        metric_contract_path=resolve(args.contract),
        repository_root=repository_root,
        corpus_paths=tuple(corpus_paths),
    )
    encoded = write_report(report, resolve(args.output))
    if not args.quiet:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
