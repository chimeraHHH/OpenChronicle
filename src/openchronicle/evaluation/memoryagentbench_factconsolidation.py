"""Frozen MemoryAgentBench FactConsolidation adapter for reviewed local memory."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import string
import subprocess
import tempfile
import time
import tracemalloc
import unicodedata
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths
from ..config import Config
from ..provenance.models import EvidenceRef, content_digest
from ..services.current_facts import CurrentFact, list_current_facts
from ..services.memory import MemoryService
from ..store import entries as entries_store
from ..store import files as files_store
from ..store import fts

_FROZEN_CONTRACT_SHA256 = "e576c9fda1ac259ac7053483ebb7f2bb820fa5e16d7c4fedccf268c36c53b27d"
_FROZEN_GATE_KEYS = frozenset(
    {
        "accepted_count",
        "current_fact_count",
        "unique_current_slot_count",
        "duplicate_current_slot_count_max",
        "history_entry_count",
        "current_slot_consistency_min",
        "parser_coverage_min",
        "no_memory_accuracy_max",
        "bm25_top1_accuracy_min",
        "bm25_slot_oracle_accuracy_min",
        "typed_slot_oracle_accuracy_min",
        "typed_slot_oracle_contradiction_free_accuracy_min",
        "typed_slot_oracle_stale_value_rate_max",
        "typed_slot_oracle_contradiction_rate_max",
        "repository_clean",
    }
)


@dataclass(frozen=True, slots=True)
class SourceSample:
    context: str
    questions: tuple[str, ...]
    answers: tuple[tuple[str, ...], ...]
    qa_pair_ids: tuple[str, ...]
    source: str = "factconsolidation_sh_6k"


@dataclass(frozen=True, slots=True)
class ParsedFact:
    index: int
    predicate: str
    subject: str
    value: str
    statement: str

    @property
    def slot(self) -> tuple[str, str]:
        return self.predicate, _canonical_subject(self.subject)

    @property
    def subject_key(self) -> str:
        digest = hashlib.sha256(self.slot[1].encode()).hexdigest()[:20]
        return f"mab.{self.predicate}.{digest}"


@dataclass(frozen=True, slots=True)
class ParsedQuestion:
    predicate: str
    subject: str

    @property
    def slot(self) -> tuple[str, str]:
        return self.predicate, _canonical_subject(self.subject)

    @property
    def subject_key(self) -> str:
        digest = hashlib.sha256(self.slot[1].encode()).hexdigest()[:20]
        return f"mab.{self.predicate}.{digest}"


_STATEMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (predicate, re.compile(pattern))
    for predicate, pattern in (
        ("head_of_state", r"The name of the current head of state in (?P<subject>.+) is (?P<value>.+)\."),
        ("head_of_government", r"The name of the current head of (?:the )?(?P<subject>.+) government is (?P<value>.+)\."),
        ("prime_minister", r"The Prime Minister of (?P<subject>.+) is (?P<value>.+)\."),
        ("chairperson", r"The chairperson of (?P<subject>.+) is (?P<value>.+)\."),
        ("director", r"The director of (?P<subject>.+) is (?P<value>.+)\."),
        ("headquarters", r"The headquarters of (?P<subject>.+) is located in the city of (?P<value>.+)\."),
        ("author", r"The author of (?P<subject>.+) is (?P<value>.+)\."),
        ("ceo", r"The chief executive officer of (?P<subject>.+) is (?P<value>.+)\."),
        ("educated_at", r"The univeristy where (?P<subject>.+) was educated is (?P<value>.+)\."),
        ("music_type", r"The type of music that (?P<subject>.+) plays is (?P<value>.+)\."),
        ("producer", r"The company that produced (?P<subject>.+) is (?P<value>.+)\."),
        ("original_broadcaster", r"The origianl broadcaster of (?P<subject>.+) is (?P<value>.+)\."),
        ("official_language", r"The official language of (?P<subject>.+) is (?P<value>.+)\."),
        ("capital", r"The capital of (?P<subject>.+) is (?P<value>.+)\."),
        ("child", r"(?P<subject>.+)'s child is (?P<value>.+)\."),
        ("born_city", r"(?P<subject>.+) was born in the city of (?P<value>.+)\."),
        ("died_city", r"(?P<subject>.+) died in the city of (?P<value>.+)\."),
        ("position", r"(?P<subject>.+) plays the position of (?P<value>.+)\."),
        ("continent", r"(?P<subject>.+) is located in the continent of (?P<value>.+)\."),
        ("worked_city", r"(?P<subject>.+) worked in the city of (?P<value>.+)\."),
        ("married_to", r"(?P<subject>.+) is married to (?P<value>.+)\."),
        ("founded_city", r"(?P<subject>.+) was founded in the city of (?P<value>.+)\."),
        ("founded_by", r"(?P<subject>.+) was founded by (?P<value>.+)\."),
        ("sport", r"(?P<subject>.+) is associated with the sport of (?P<value>.+)\."),
        ("citizenship", r"(?P<subject>.+) is a citizen of (?P<value>.+)\."),
        ("performed_by", r"(?P<subject>.+) was performed by (?P<value>.+)\."),
        ("speaks_language", r"(?P<subject>.+) speaks the language of (?P<value>.+)\."),
        ("famous_for", r"(?P<subject>.+) is famous for (?P<value>.+)\."),
        ("employed_by", r"(?P<subject>.+) is employed by (?P<value>.+)\."),
        ("created_country", r"(?P<subject>.+) was created in the country of (?P<value>.+)\."),
        ("religion", r"(?P<subject>.+) is affiliated with the religion of (?P<value>.+)\."),
        ("field", r"(?P<subject>.+) works in the field of (?P<value>.+)\."),
        ("developed_by", r"(?P<subject>.+) was developed by (?P<value>.+)\."),
        ("created_by", r"(?P<subject>.+) was created by (?P<value>.+)\."),
        ("written_language", r"(?P<subject>.+) was written in the language of (?P<value>.+)\."),
    )
)

_QUESTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (predicate, re.compile(pattern))
    for predicate, pattern in (
        ("sport", r"Which sport is (?P<subject>.+) associated with\?"),
        ("famous_for", r"What is (?P<subject>.+) famous for\?"),
        ("created_country", r"Which country was (?P<subject>.+) created in\?"),
        ("educated_at", r"Which university was (?P<subject>.+) educated at\?"),
        ("author", r"Who is the author of (?P<subject>.+)\?"),
        ("official_language", r"What is the official language of (?P<subject>.+)\?"),
        ("head_of_government", r"What is the name of the current head of the (?P<subject>.+) government\?"),
        ("head_of_state", r"What is the name of the current head of state in (?P<subject>.+)\?"),
        ("performed_by", r"Who performed (?P<subject>.+)\?"),
        ("position", r"What position does (?P<subject>.+) play\?"),
        ("citizenship", r"What is the country of citizenship of (?P<subject>.+)\?"),
        ("continent", r"Which continent is (?P<subject>.+) located in\?"),
        ("religion", r"Which religion is (?P<subject>.+) affiliated with\?"),
        ("developed_by", r"Who is the developer of (?P<subject>.+)\?"),
        ("ceo", r"Who is the chief executive officer of (?P<subject>.+)\?"),
        ("chairperson", r"Who is the chairperson of (?P<subject>.+)\?"),
        ("founded_by", r"Who founded (?P<subject>.+)\?"),
        ("director", r"Who is the director of (?P<subject>.+)\?"),
        ("died_city", r"Which city did (?P<subject>.+) die in\?"),
        ("capital", r"What is the capital of (?P<subject>.+)\?"),
        ("music_type", r"What type of music does (?P<subject>.+) play\?"),
        ("headquarters", r"Which city is the headquarter of (?P<subject>.+) located in\?"),
        ("founded_city", r"Where was (?P<subject>.+) founded\?"),
        ("married_to", r"Who is (?P<subject>.+) married to\?"),
        ("speaks_language", r"What language does (?P<subject>.+) speak\?"),
        ("field", r"What kind of work does (?P<subject>.+) do\?"),
        ("employed_by", r"Who is the employer of (?P<subject>.+)\?"),
        ("producer", r"Which company is (?P<subject>.+) produced by\?"),
        ("created_by", r"Who was (?P<subject>.+) created by\?"),
        ("worked_city", r"Which city did (?P<subject>.+) work in\?"),
        ("child", r"Who is (?P<subject>.+)'s child\?"),
    )
)


def parse_context(context: str) -> tuple[ParsedFact, ...]:
    lines = context.splitlines()
    if not lines or lines[0].strip() != "Here is a list of facts:":
        raise ValueError("FactConsolidation context header is invalid")
    facts: list[ParsedFact] = []
    for expected_index, raw in enumerate(lines[1:]):
        match = re.fullmatch(r"(\d+)\.\s+(.+)", raw)
        if match is None or int(match.group(1)) != expected_index:
            raise ValueError(f"FactConsolidation statement index {expected_index} is invalid")
        statement = match.group(2)
        facts.append(parse_statement(statement, index=expected_index))
    if not facts:
        raise ValueError("FactConsolidation context contains no facts")
    return tuple(facts)


def parse_statement(statement: str, *, index: int = -1) -> ParsedFact:
    for predicate, pattern in _STATEMENT_PATTERNS:
        match = pattern.fullmatch(statement)
        if match is not None:
            return ParsedFact(
                index=index,
                predicate=predicate,
                subject=match.group("subject"),
                value=match.group("value"),
                statement=statement,
            )
    raise ValueError(f"unsupported FactConsolidation statement: {statement}")


def parse_question(question: str) -> ParsedQuestion:
    for predicate, pattern in _QUESTION_PATTERNS:
        match = pattern.fullmatch(question)
        if match is not None:
            return ParsedQuestion(predicate=predicate, subject=match.group("subject"))
    raise ValueError(f"unsupported FactConsolidation question: {question}")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("FactConsolidation manifest is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("benchmark_id") != "mab-cr-factconsolidation-sh-6k-v1"
    ):
        raise ValueError("FactConsolidation manifest is invalid")
    return manifest


def load_parquet_sample(parquet_path: Path, manifest: dict[str, Any]) -> SourceSample:
    expected_sha = manifest["data"]["parquet_sha256"]
    actual_sha = _file_sha256(parquet_path)
    if actual_sha != expected_sha:
        raise ValueError(f"FactConsolidation Parquet SHA-256 differs: {actual_sha}")
    try:
        import pyarrow.parquet as parquet  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "pyarrow is required only for this benchmark; run it with "
            "`uv run --with pyarrow==21.0.0 ...`"
        ) from exc
    source = manifest["selection"]["metadata_source"]
    rows = [
        row
        for row in parquet.read_table(parquet_path).to_pylist()
        if row.get("metadata", {}).get("source") == source
    ]
    if len(rows) != manifest["selection"]["matching_row_count"]:
        raise ValueError("FactConsolidation source selection changed")
    row = rows[0]
    metadata = row["metadata"]
    sample = SourceSample(
        context=row["context"],
        questions=tuple(row["questions"]),
        answers=tuple(tuple(answer) for answer in row["answers"]),
        qa_pair_ids=tuple(metadata["qa_pair_ids"]),
        source=metadata["source"],
    )
    validate_sample(sample, manifest)
    return sample


def validate_sample(sample: SourceSample, manifest: dict[str, Any]) -> None:
    expected = manifest["expected"]
    facts = parse_context(sample.context)
    parsed_questions = tuple(parse_question(question) for question in sample.questions)
    if (
        sample.source != manifest["selection"]["metadata_source"]
        or len(facts) != expected["fact_count"]
        or len({fact.slot for fact in facts}) != expected["unique_slot_count"]
        or len(parsed_questions) != expected["question_count"]
        or len(sample.answers) != expected["question_count"]
        or list(sample.qa_pair_ids) != manifest["qa_pair_ids"]
    ):
        raise ValueError("FactConsolidation sample shape differs from the frozen manifest")
    hashes = {
        "context_sha256": hashlib.sha256(sample.context.encode()).hexdigest(),
        "questions_sha256": _canonical_json_sha(list(sample.questions)),
        "answers_sha256": _canonical_json_sha([list(answer) for answer in sample.answers]),
        "qa_pair_ids_sha256": _canonical_json_sha(list(sample.qa_pair_ids)),
    }
    if hashes != manifest["hashes"]:
        raise ValueError("FactConsolidation sample content differs from the frozen manifest")


def run_evaluation(
    *,
    parquet_path: Path,
    manifest_path: Path,
    metric_contract_path: Path,
    repository_root: Path,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    sample = load_parquet_sample(parquet_path, manifest)
    contract_bytes = metric_contract_path.read_bytes()
    contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
    if (
        manifest.get("metric_contract")
        != {
            "path": "json/metric_contract.json",
            "sha256": _FROZEN_CONTRACT_SHA256,
        }
        or contract_sha256 != _FROZEN_CONTRACT_SHA256
    ):
        raise ValueError("FactConsolidation metric contract does not match the manifest")
    try:
        contract = json.loads(contract_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("FactConsolidation metric contract is invalid JSON") from exc
    _validate_contract(contract, manifest, contract_sha256=contract_sha256)
    repository = _repository_state(repository_root)
    result = evaluate_sample(sample)
    result["gate_verdict"] = _gate_verdict(
        result,
        contract["gates"],
        repository_clean=not repository["dirty"],
    )
    return {
        "schema_version": 1,
        "evaluation_id": manifest["benchmark_id"],
        "scope": "official-data deterministic OpenChronicle adapter; not an official leaderboard run",
        "repository": repository,
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "dataset": {
            "code_revision": manifest["upstream"]["code_revision"],
            "data_revision": manifest["data"]["revision"],
            "parquet_sha256": manifest["data"]["parquet_sha256"],
            "source": sample.source,
            "fact_count": len(parse_context(sample.context)),
            "question_count": len(sample.questions),
            "qa_pair_ids_sha256": manifest["hashes"]["qa_pair_ids_sha256"],
        },
        "manifest": {
            "path": str(manifest_path.relative_to(repository_root)),
            "sha256": _file_sha256(manifest_path),
        },
        "metric_contract": {
            "path": str(metric_contract_path.relative_to(repository_root)),
            "sha256": contract_sha256,
        },
        "result": result,
    }


def evaluate_sample(sample: SourceSample) -> dict[str, Any]:
    facts = parse_context(sample.context)
    questions = tuple(parse_question(question) for question in sample.questions)
    histories: dict[tuple[str, str], list[str]] = {}
    for fact in facts:
        histories.setdefault(fact.slot, []).append(fact.value)

    tracemalloc.start()
    with _isolated_root(), fts.cursor() as conn:
        cfg = Config()
        service = MemoryService(conn, cfg=cfg)
        ingest_started = time.perf_counter()
        applied = _ingest_facts(conn, service, facts)
        ingest_ms = (time.perf_counter() - ingest_started) * 1000
        current_facts = list_current_facts(conn, cfg, limit=10_000)
        current_by_subject: dict[str, list[CurrentFact]] = {}
        for fact in current_facts:
            current_by_subject.setdefault(fact.subject_key, []).append(fact)
        current_by_identity = {(fact.path, fact.id): fact for fact in current_facts}

        variants = {
            "no_memory": _evaluate_variant(
                sample,
                questions,
                histories,
                answer=lambda _text: "",
            ),
            "bm25_top1_current_only": _evaluate_variant(
                sample,
                questions,
                histories,
                answer=lambda text: _bm25_top1_answer(
                    conn,
                    text,
                    current_by_identity,
                ),
            ),
            "bm25_top20_plus_slot_oracle": _evaluate_variant(
                sample,
                questions,
                histories,
                answer=lambda text: _bm25_slot_oracle_answer(
                    conn,
                    text,
                    current_by_identity,
                ),
            ),
            "typed_slot_oracle": _evaluate_variant(
                sample,
                questions,
                histories,
                answer=lambda text: _typed_answer(
                    parse_question(text),
                    current_by_subject,
                ),
            ),
        }
        consistency = _current_slot_consistency(facts, current_by_subject)
        history_count = sum(
            len(files_store.read_file(files_store.memory_path(path)).entries)
            for path in sorted({fact.target_path for fact in applied})
        )
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "ingest": {
            "review_mode": "fixture_auto_review_in_isolated_temporary_root",
            "operation_count": len(applied),
            "accepted_count": sum(fact.accepted for fact in applied),
            "append_count": sum(fact.operation == "append" for fact in applied),
            "supersede_count": sum(fact.operation == "supersede" for fact in applied),
            "current_fact_count": len(current_facts),
            "unique_current_slot_count": len(current_by_subject),
            "duplicate_current_slot_count": sum(
                max(0, len(group) - 1) for group in current_by_subject.values()
            ),
            "history_entry_count": history_count,
            "current_slot_consistency": consistency,
            "elapsed_ms": round(ingest_ms, 3),
            "peak_tracemalloc_mb": round(peak_bytes / 1_048_576, 3),
        },
        "parser": {
            "fact_coverage": _ratio(len(facts), len(sample.context.splitlines()) - 1),
            "question_coverage": _ratio(len(questions), len(sample.questions)),
            "unique_slot_count": len(histories),
        },
        "variants": variants,
    }


@dataclass(frozen=True, slots=True)
class AppliedFact:
    target_path: str
    operation: str
    accepted: bool


def _ingest_facts(
    conn,
    service: MemoryService,
    facts: tuple[ParsedFact, ...],
) -> list[AppliedFact]:
    source_path = "event-memoryagentbench-factconsolidation.md"
    entries_store.create_file(
        conn,
        name=source_path,
        description="Frozen MemoryAgentBench FactConsolidation evidence.",
        tags=["event", "evaluation"],
    )
    current_entry_ids: dict[tuple[str, str], str] = {}
    applied: list[AppliedFact] = []
    for fact in facts:
        source_id = f"mab-source-{fact.index:04d}"
        entries_store.append_entry_once(
            conn,
            name=source_path,
            content=fact.statement,
            tags=["event", "memoryagentbench"],
            entry_id=source_id,
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )
        source_ref = EvidenceRef(
            kind="memory_entry",
            id=source_id,
            path=source_path,
            content_hash=content_digest(fact.statement),
        )
        target_path = f"project-benchmark-facts-{fact.predicate.replace('_', '-')}.md"
        target_entry_id = current_entry_ids.get(fact.slot, "")
        operation = "supersede" if target_entry_id else "append"
        candidate = service.propose_candidate(
            kind="fact",
            operation=operation,
            target_path=target_path,
            target_entry_id=target_entry_id,
            content=fact.statement,
            tags=["fact", "memoryagentbench", fact.predicate.replace("_", "-")],
            evidence=[source_ref],
            claim_evidence=[source_ref],
            confidence=1.0,
            subject_key=fact.subject_key,
            assertion_kind="observed",
            producer_run_key=f"mab-factconsolidation:{fact.index}",
        )
        if candidate.status != "pending":
            raise RuntimeError(
                f"FactConsolidation fact {fact.index} entered {candidate.status}, not pending"
            )
        accepted = service.approve_candidate(candidate.id, expected_version=candidate.version)
        if accepted.status != "accepted" or not accepted.applied_entry_id:
            raise RuntimeError(f"FactConsolidation fact {fact.index} was not published")
        current_entry_ids[fact.slot] = accepted.applied_entry_id
        applied.append(
            AppliedFact(
                target_path=target_path,
                operation=operation,
                accepted=True,
            )
        )
    return applied


def _typed_answer(
    question: ParsedQuestion,
    current_by_subject: dict[str, list[CurrentFact]],
) -> str:
    facts = current_by_subject.get(question.subject_key, [])
    if len(facts) != 1:
        return ""
    fact = facts[0]
    parsed = parse_statement(fact.content)
    return parsed.value if parsed.slot == question.slot else ""


def _bm25_top1_answer(
    conn,
    question_text: str,
    current_by_identity: dict[tuple[str, str], CurrentFact],
) -> str:
    hits = fts.search(
        conn,
        query=question_text,
        path_patterns=["project-benchmark-facts-*.md"],
        top_k=1,
        include_superseded=False,
        match_any_terms=True,
    )
    if not hits:
        return ""
    hit = hits[0]
    if (hit.path, hit.id) not in current_by_identity:
        return ""
    return parse_statement(hit.content).value


def _bm25_slot_oracle_answer(
    conn,
    question_text: str,
    current_by_identity: dict[tuple[str, str], CurrentFact],
) -> str:
    question = parse_question(question_text)
    for hit in _current_bm25_hits(conn, question_text, current_by_identity):
        parsed = parse_statement(hit.content)
        if parsed.slot == question.slot:
            return parsed.value
    return ""


def _current_bm25_hits(
    conn,
    question_text: str,
    current_by_identity: dict[tuple[str, str], CurrentFact],
) -> list[fts.EntryHit]:
    hits = fts.search(
        conn,
        query=question_text,
        path_patterns=["project-benchmark-facts-*.md"],
        top_k=20,
        include_superseded=False,
        match_any_terms=True,
    )
    current: list[fts.EntryHit] = []
    for hit in hits:
        if (hit.path, hit.id) not in current_by_identity:
            continue
        current.append(hit)
    return current


def _evaluate_variant(
    sample: SourceSample,
    questions: tuple[ParsedQuestion, ...],
    histories: dict[tuple[str, str], list[str]],
    *,
    answer: Callable[[str], str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    for qa_id, question_text, question, golds in zip(
        sample.qa_pair_ids,
        sample.questions,
        questions,
        sample.answers,
        strict=True,
    ):
        started = time.perf_counter()
        prediction = answer(question_text)
        latencies.append((time.perf_counter() - started) * 1000)
        correct = any(substring_exact_match(prediction, gold) for gold in golds)
        stale_values = histories[question.slot][:-1]
        normalized_golds = {normalize_answer(gold) for gold in golds}
        stale = any(
            not any(
                f" {normalized_value} " in f" {normalized_gold} "
                for normalized_gold in normalized_golds
            )
            and _normalized_phrase_in_prediction(prediction, normalized_value)
            for value in stale_values
            if (normalized_value := normalize_answer(value))
        )
        contradiction_free = correct and not stale
        contradiction = correct and stale
        rows.append(
            {
                "qa_pair_id": qa_id,
                "prediction": prediction,
                "correct": correct,
                "stale": stale,
                "contradiction": contradiction,
                "contradiction_free": contradiction_free,
                "unanswered": not prediction.strip(),
            }
        )
    return {
        "accuracy": _ratio(sum(row["correct"] for row in rows), len(rows)),
        "contradiction_free_accuracy": _ratio(
            sum(row["contradiction_free"] for row in rows), len(rows)
        ),
        "stale_value_rate": _ratio(sum(row["stale"] for row in rows), len(rows)),
        "contradiction_rate": _ratio(
            sum(row["contradiction"] for row in rows), len(rows)
        ),
        "unanswered_rate": _ratio(sum(row["unanswered"] for row in rows), len(rows)),
        "query_latency_p50_ms": round(_percentile(latencies, 0.50), 3),
        "query_latency_p95_ms": round(_percentile(latencies, 0.95), 3),
        "rows": rows,
    }


def normalize_answer(value: str) -> str:
    text = value.lower()
    text = "".join(char for char in text if char not in string.punctuation)
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    return " ".join(text.split())


def substring_exact_match(prediction: str, ground_truth: str) -> bool:
    return normalize_answer(ground_truth) in normalize_answer(prediction)


def _normalized_phrase_in_prediction(prediction: str, normalized_phrase: str) -> bool:
    normalized_prediction = normalize_answer(prediction)
    return f" {normalized_phrase} " in f" {normalized_prediction} "


def _current_slot_consistency(
    facts: tuple[ParsedFact, ...],
    current_by_subject: dict[str, list[CurrentFact]],
) -> float:
    expected: dict[str, ParsedFact] = {}
    for fact in facts:
        expected[fact.subject_key] = fact
    matches = 0
    for subject_key, expected_fact in expected.items():
        current = current_by_subject.get(subject_key, [])
        if len(current) != 1:
            continue
        actual = parse_statement(current[0].content)
        matches += actual.slot == expected_fact.slot and actual.value == expected_fact.value
    return _ratio(matches, len(expected))


def _gate_verdict(
    result: dict[str, Any],
    gates: dict[str, Any],
    *,
    repository_clean: bool,
) -> dict[str, Any]:
    variants = result["variants"]
    ingest = result["ingest"]
    parser = result["parser"]
    checks = {
        "accepted_count": ingest["accepted_count"] == gates["accepted_count"],
        "current_fact_count": ingest["current_fact_count"] == gates["current_fact_count"],
        "unique_current_slot_count": ingest["unique_current_slot_count"]
        == gates["unique_current_slot_count"],
        "duplicate_current_slot_count": ingest["duplicate_current_slot_count"]
        <= gates["duplicate_current_slot_count_max"],
        "history_entry_count": ingest["history_entry_count"] == gates["history_entry_count"],
        "current_slot_consistency": ingest["current_slot_consistency"]
        >= gates["current_slot_consistency_min"],
        "fact_parser_coverage": parser["fact_coverage"] >= gates["parser_coverage_min"],
        "question_parser_coverage": parser["question_coverage"]
        >= gates["parser_coverage_min"],
        "no_memory_accuracy": variants["no_memory"]["accuracy"]
        <= gates["no_memory_accuracy_max"],
        "bm25_top1_accuracy": variants["bm25_top1_current_only"]["accuracy"]
        >= gates["bm25_top1_accuracy_min"],
        "bm25_slot_oracle_accuracy": variants["bm25_top20_plus_slot_oracle"]["accuracy"]
        >= gates["bm25_slot_oracle_accuracy_min"],
        "typed_slot_oracle_accuracy": variants["typed_slot_oracle"]["accuracy"]
        >= gates["typed_slot_oracle_accuracy_min"],
        "typed_slot_oracle_contradiction_free_accuracy": variants[
            "typed_slot_oracle"
        ]["contradiction_free_accuracy"]
        >= gates["typed_slot_oracle_contradiction_free_accuracy_min"],
        "typed_slot_oracle_stale_value_rate": variants["typed_slot_oracle"][
            "stale_value_rate"
        ]
        <= gates["typed_slot_oracle_stale_value_rate_max"],
        "typed_slot_oracle_contradiction_rate": variants["typed_slot_oracle"][
            "contradiction_rate"
        ]
        <= gates["typed_slot_oracle_contradiction_rate_max"],
        "repository_clean": repository_clean is gates["repository_clean"],
    }
    return {"passed": all(checks.values()), "checks": checks}


def _validate_contract(
    contract: object,
    manifest: dict[str, Any],
    *,
    contract_sha256: str,
) -> None:
    if (
        not isinstance(contract, dict)
        or set(contract) != {
            "schema_version",
            "benchmark_id",
            "primary_metric",
            "official_metric",
            "gates",
        }
        or contract.get("schema_version") != 1
        or contract.get("benchmark_id") != manifest["benchmark_id"]
        or contract.get("primary_metric")
        != "typed_slot_oracle_contradiction_free_accuracy"
        or contract.get("official_metric") != "substring_exact_match"
        or not isinstance(contract.get("gates"), dict)
        or set(contract["gates"]) != _FROZEN_GATE_KEYS
        or manifest.get("metric_contract")
        != {
            "path": "json/metric_contract.json",
            "sha256": _FROZEN_CONTRACT_SHA256,
        }
        or contract_sha256 != _FROZEN_CONTRACT_SHA256
    ):
        raise ValueError("FactConsolidation metric contract does not match the manifest")
    gates = contract["gates"]
    count_keys = {
        "accepted_count",
        "current_fact_count",
        "unique_current_slot_count",
        "duplicate_current_slot_count_max",
        "history_entry_count",
    }
    if any(type(gates[key]) is not int or gates[key] < 0 for key in count_keys):
        raise ValueError("FactConsolidation metric contract has invalid count gates")
    rate_keys = _FROZEN_GATE_KEYS - count_keys - {"repository_clean"}
    if any(
        isinstance(gates[key], bool)
        or not isinstance(gates[key], (int, float))
        or not math.isfinite(gates[key])
        or not 0 <= gates[key] <= 1
        for key in rate_keys
    ):
        raise ValueError("FactConsolidation metric contract has invalid rate gates")
    if type(gates["repository_clean"]) is not bool:
        raise ValueError("FactConsolidation metric contract has invalid repository gate")


def _canonical_subject(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return " ".join(normalized.split())


def _canonical_json_sha(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * quantile) + 0.999999) - 1))
    return ordered[index]


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


@contextmanager
def _isolated_root():
    previous = os.environ.get("OPENCHRONICLE_ROOT")
    with tempfile.TemporaryDirectory(prefix="oc-memoryagentbench-") as temp_dir:
        os.environ["OPENCHRONICLE_ROOT"] = temp_dir
        paths.ensure_dirs()
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("OPENCHRONICLE_ROOT", None)
            else:
                os.environ["OPENCHRONICLE_ROOT"] = previous


def _repository_state(repository_root: Path) -> dict[str, Any]:
    commit = _git(repository_root, "rev-parse", "HEAD")
    status = _git(repository_root, "status", "--porcelain")
    return {"commit": commit, "dirty": bool(status), "status_lines": len(status.splitlines())}


def _git(repository_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError("unable to record repository identity")
    return completed.stdout.strip()


def write_report(report: dict[str, Any], output: Path | None = None) -> str:
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    return encoded


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    benchmark_root = repository_root / "benchmarks" / "memoryagentbench-factconsolidation-v1"
    manifest_path = args.manifest or benchmark_root / "json" / "manifest.json"
    contract_path = args.contract or benchmark_root / "json" / "metric_contract.json"
    report = run_evaluation(
        parquet_path=args.parquet.resolve(),
        manifest_path=manifest_path.resolve(),
        metric_contract_path=contract_path.resolve(),
        repository_root=repository_root,
    )
    encoded = write_report(report, args.output)
    if not args.quiet:
        print(encoded, end="")
    return 0 if report["result"]["gate_verdict"]["passed"] else 1
