"""Local extractive cross-encoder selection over exact dialogue turns."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import platform
import struct
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import memops50
from .memops50_evidence_answer import Candidate, CandidateTurn

FROZEN_ARTIFACT_MANIFEST_SHA256 = "83442e51ef942743cbe7880da5ddc2ab01bdffc96560f7662201651ac1295c93"
FROZEN_SELECTION_CONTRACT_SHA256 = (
    "e7517e3f92b70789f9bd8194f5c2655391a68c408740e2aa60ee9b4eec27146f"
)
_EXPECTED_UPSTREAM = {
    "repository": "https://huggingface.co/Xenova/ms-marco-MiniLM-L-6-v2",
    "revision": "a09144355adeed5f58c8ed011d209bf8ee5a1fec",
    "base_model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "license_spdx": "Apache-2.0",
    "license_evidence": "fastembed-0.8.0-supported-model-registry",
}
_EXPECTED_FILES_AGGREGATE = "ac180b887cbdf361a4e13b332a61e5695eb37fc4d4249333a778eb18c9714a96"


class LecsUnavailable(RuntimeError):
    """The frozen local selector cannot be loaded or executed."""


@dataclass(frozen=True, slots=True)
class EvidenceScore:
    raw_score: float
    token_count: int


class EvidenceScorer(Protocol):
    @property
    def model_id(self) -> str: ...

    def score(
        self,
        query: str,
        documents: Sequence[str],
    ) -> tuple[EvidenceScore, ...]: ...


@dataclass(frozen=True, slots=True)
class LecsTurnTrace:
    candidate_ref: str
    bm25_rank: int
    query_mode: str
    turn_ref: str
    role: str
    content_sha256: str
    content_utf8_bytes: int
    input_sha256: str
    token_count: int
    truncated: bool
    raw_score_bits: str
    raw_score_decimal: str
    passes_threshold: bool
    score_rank: int
    selected: bool
    answer_presentation_rank: int | None


@dataclass(frozen=True, slots=True)
class LecsSelection:
    selection_status: str
    selected_evidence_refs: tuple[str, ...]
    capped_positive_refs: tuple[str, ...]
    positive_count: int
    selector_latency_ms: int
    scorer_model_id: str
    traces: tuple[LecsTurnTrace, ...]


@dataclass(frozen=True, slots=True)
class _ScoredTurn:
    candidate: Candidate
    turn: CandidateTurn
    raw_score: float
    token_count: int
    input_sha256: str


def load_artifact_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise LecsUnavailable("LECS model artifact manifest is unavailable.") from exc
    _validate_artifact_manifest(payload)
    return payload


def load_frozen_artifact_manifest(
    *,
    artifact_manifest_path: Path,
    selection_contract_path: Path,
) -> dict[str, Any]:
    try:
        contract_bytes = selection_contract_path.read_bytes()
        artifact_bytes = artifact_manifest_path.read_bytes()
        contract = json.loads(contract_bytes)
        artifact = json.loads(artifact_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise LecsUnavailable("LECS frozen contracts are unavailable.") from exc
    if hashlib.sha256(contract_bytes).hexdigest() != FROZEN_SELECTION_CONTRACT_SHA256:
        raise LecsUnavailable("LECS selection contract digest changed.")
    _validate_selection_contract(contract)
    artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
    if (
        artifact_digest != FROZEN_ARTIFACT_MANIFEST_SHA256
        or artifact_digest != contract["selector"]["model_artifact_manifest_sha256"]
    ):
        raise LecsUnavailable("LECS artifact manifest is not contract-anchored.")
    _validate_artifact_manifest(artifact)
    return artifact


def verify_model_artifacts(model_dir: Path, manifest: Mapping[str, Any]) -> str:
    """Verify every frozen model artifact before importing FastEmbed."""
    _validate_artifact_manifest(manifest)
    root = model_dir.resolve()
    expected_files = manifest["files"]
    expected_paths = [item["path"] for item in expected_files]
    actual_paths = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and ".cache" not in path.relative_to(root).parts
    )
    if actual_paths != expected_paths:
        raise LecsUnavailable("LECS model artifact file set changed.")
    actual: list[dict[str, Any]] = []
    for expected in expected_files:
        path = root / expected["path"]
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LecsUnavailable("LECS model artifact is missing.") from exc
        item = {
            "path": expected["path"],
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if item != expected:
            raise LecsUnavailable("LECS model artifact digest changed.")
        actual.append(item)
    aggregate = memops50.sha256_bytes(memops50.canonical_json(actual))
    if aggregate != manifest["files_aggregate_sha256"]:
        raise LecsUnavailable("LECS model artifact aggregate changed.")
    return aggregate


def verify_runtime(manifest: Mapping[str, Any], *, dependency_lock: Path) -> None:
    _validate_artifact_manifest(manifest)
    execution = manifest["execution"]
    packages = {
        "fastembed": (
            execution["library_version"],
            execution["library_distribution_sha256"],
        ),
        "onnxruntime": (
            execution["onnxruntime_version"],
            execution["onnxruntime_distribution_sha256"],
        ),
        "tokenizers": (
            execution["tokenizers_version"],
            execution["tokenizers_distribution_sha256"],
        ),
        "numpy": (
            execution["numpy_version"],
            execution["numpy_distribution_sha256"],
        ),
    }
    try:
        installed = {
            package: (
                importlib.metadata.version(package),
                _distribution_sha256(package),
            )
            for package in packages
        }
        lock_digest = hashlib.sha256(dependency_lock.read_bytes()).hexdigest()
    except (importlib.metadata.PackageNotFoundError, OSError) as exc:
        raise LecsUnavailable("LECS frozen runtime is unavailable.") from exc
    if (
        installed != packages
        or lock_digest != execution["dependency_lock_sha256"]
        or platform.python_version() != execution["python_version"]
        or platform.system() != execution["platform_system"]
        or platform.machine() != execution["platform_machine"]
    ):
        raise LecsUnavailable("LECS frozen runtime changed.")


class FastEmbedCrossEncoder:
    """Pinned offline FastEmbed cross-encoder with no fallback path."""

    def __init__(
        self,
        *,
        model_dir: Path,
        artifact_manifest_path: Path,
        selection_contract_path: Path,
        dependency_lock: Path,
    ) -> None:
        manifest = load_frozen_artifact_manifest(
            artifact_manifest_path=artifact_manifest_path,
            selection_contract_path=selection_contract_path,
        )
        aggregate = verify_model_artifacts(model_dir, manifest)
        verify_runtime(manifest, dependency_lock=dependency_lock)
        execution = manifest["execution"]
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
            tokenizer.no_truncation()
            tokenizer.no_padding()
            encoder = TextCrossEncoder(
                model_name="Xenova/ms-marco-MiniLM-L-6-v2",
                cache_dir=str(model_dir.parent),
                threads=execution["threads"],
                providers=(execution["provider"],),
                cuda=False,
                lazy_load=False,
                local_files_only=True,
                specific_model_path=str(model_dir.resolve()),
            )
            _validate_loaded_encoder(encoder, execution)
        except Exception as exc:
            raise LecsUnavailable("LECS local cross-encoder could not be loaded.") from exc
        self._encoder = encoder
        self._tokenizer = tokenizer
        self._batch_size = int(execution["batch_size"])
        self._max_pair_tokens = int(execution["max_pair_tokens"])
        self._model_id = f"fastembed-cross-encoder:Xenova/ms-marco-MiniLM-L-6-v2@sha256:{aggregate}"

    @property
    def model_id(self) -> str:
        return self._model_id

    def score(
        self,
        query: str,
        documents: Sequence[str],
    ) -> tuple[EvidenceScore, ...]:
        token_counts = tuple(
            len(self._tokenizer.encode(query, document).ids) for document in documents
        )
        if any(count > self._max_pair_tokens for count in token_counts):
            raise LecsUnavailable("LECS input exceeds the frozen token limit.")
        try:
            values = tuple(
                _float32(value)
                for value in self._encoder.rerank(
                    query,
                    list(documents),
                    batch_size=self._batch_size,
                )
            )
        except Exception as exc:
            raise LecsUnavailable("LECS local scoring failed.") from exc
        if len(values) != len(documents):
            raise LecsUnavailable("LECS returned the wrong score count.")
        if any(not math.isfinite(value) for value in values):
            raise LecsUnavailable("LECS returned a non-finite score.")
        return tuple(
            EvidenceScore(raw_score=value, token_count=token_count)
            for value, token_count in zip(values, token_counts, strict=True)
        )


def select_evidence(
    *,
    query: str,
    candidates: Sequence[Candidate],
    scorer: EvidenceScorer,
    threshold: float = 0.0,
    max_selected: int = 7,
) -> LecsSelection:
    if threshold != 0.0 or isinstance(max_selected, bool) or max_selected != 7:
        raise ValueError("LECS selection policy differs from the frozen contract")
    flattened = _canonical_candidate_turns(candidates)
    refs = [turn.ref for _candidate, turn in flattened]
    if len(refs) != len(set(refs)):
        raise ValueError("LECS candidate refs are not unique")
    documents = tuple(_right_input(turn) for _candidate, turn in flattened)
    started = time.monotonic()
    scored = scorer.score(query, documents)
    latency_ms = round((time.monotonic() - started) * 1000)
    if len(scored) != len(flattened):
        raise LecsUnavailable("LECS returned the wrong score count.")
    values: list[_ScoredTurn] = []
    for (candidate, turn), document, item in zip(
        flattened,
        documents,
        scored,
        strict=True,
    ):
        value = _float32(item.raw_score)
        if not math.isfinite(value):
            raise LecsUnavailable("LECS returned a non-finite score.")
        if type(item.token_count) is not int or item.token_count < 1:
            raise LecsUnavailable("LECS returned an invalid token count.")
        values.append(
            _ScoredTurn(
                candidate=candidate,
                turn=turn,
                raw_score=value,
                token_count=item.token_count,
                input_sha256=hashlib.sha256(
                    memops50.canonical_json({"left": query, "right": document})
                ).hexdigest(),
            )
        )
    ranked = sorted(
        values,
        key=lambda item: (
            -item.raw_score,
            item.candidate.rank,
            item.turn.turn_index,
            item.turn.ref,
        ),
    )
    positive = [item for item in ranked if item.raw_score > threshold]
    chosen = positive[:max_selected]
    selected_refs = {item.turn.ref for item in chosen}
    presentation = sorted(
        (item for item in values if item.turn.ref in selected_refs),
        key=lambda item: (
            item.candidate.rank,
            item.turn.turn_index,
            item.turn.ref,
        ),
    )
    presentation_rank = {item.turn.ref: index for index, item in enumerate(presentation, start=1)}
    score_rank = {item.turn.ref: index for index, item in enumerate(ranked, start=1)}
    traces = tuple(
        LecsTurnTrace(
            candidate_ref=f"R{item.candidate.rank:02d}",
            bm25_rank=item.candidate.rank,
            query_mode=item.candidate.query_mode,
            turn_ref=item.turn.ref,
            role=item.turn.role,
            content_sha256=hashlib.sha256(item.turn.content.encode()).hexdigest(),
            content_utf8_bytes=len(item.turn.content.encode()),
            input_sha256=item.input_sha256,
            token_count=item.token_count,
            truncated=False,
            raw_score_bits=_float32_bits(item.raw_score),
            raw_score_decimal=format(item.raw_score, ".9g"),
            passes_threshold=item.raw_score > threshold,
            score_rank=score_rank[item.turn.ref],
            selected=item.turn.ref in selected_refs,
            answer_presentation_rank=presentation_rank.get(item.turn.ref),
        )
        for item in values
    )
    selected = tuple(item.turn.ref for item in presentation)
    return LecsSelection(
        selection_status="selected" if selected else "insufficient",
        selected_evidence_refs=selected,
        capped_positive_refs=tuple(item.turn.ref for item in positive[max_selected:]),
        positive_count=len(positive),
        selector_latency_ms=latency_ms,
        scorer_model_id=scorer.model_id,
        traces=traces,
    )


def selection_trace_sha256(selection: LecsSelection) -> str:
    payload = [
        {field: getattr(trace, field) for field in LecsTurnTrace.__dataclass_fields__}
        for trace in selection.traces
    ]
    return memops50.sha256_bytes(memops50.canonical_json(payload))


def _right_input(turn: CandidateTurn) -> str:
    return f"{turn.role}\n{turn.content}"


def _float32(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LecsUnavailable("LECS returned a non-numeric score.")
    try:
        return struct.unpack("!f", struct.pack("!f", float(value)))[0]
    except (OverflowError, struct.error) as exc:
        raise LecsUnavailable("LECS returned an invalid float32 score.") from exc


def _float32_bits(value: float) -> str:
    bits = struct.unpack("!I", struct.pack("!f", value))[0]
    return f"0x{bits:08x}"


def _canonical_candidate_turns(
    candidates: Sequence[Candidate],
) -> tuple[tuple[Candidate, CandidateTurn], ...]:
    ordered = sorted(candidates, key=lambda candidate: candidate.rank)
    ranks = [candidate.rank for candidate in ordered]
    if ranks != list(range(1, len(ordered) + 1)):
        raise ValueError("LECS candidate ranks are not contiguous")
    flattened: list[tuple[Candidate, CandidateTurn]] = []
    for candidate in ordered:
        turns = sorted(candidate.turns, key=lambda turn: (turn.turn_index, turn.ref))
        indexes = [turn.turn_index for turn in turns]
        if len(indexes) != len(set(indexes)):
            raise ValueError("LECS candidate turn indexes are not unique")
        for turn in turns:
            expected_ref = f"R{candidate.rank:02d}-T{turn.turn_index:02d}"
            if turn.ref != expected_ref or turn.segment_id != candidate.segment_id:
                raise ValueError("LECS candidate turn identity is invalid")
            flattened.append((candidate, turn))
    return tuple(flattened)


def _distribution_sha256(package: str) -> str:
    distribution = importlib.metadata.distribution(package)
    files: list[dict[str, Any]] = []
    for item in distribution.files or ():
        path = Path(distribution.locate_file(item))
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": str(item),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    files.sort(key=lambda item: item["path"])
    return memops50.sha256_bytes(memops50.canonical_json(files))


def _validate_loaded_encoder(encoder: object, execution: Mapping[str, Any]) -> None:
    try:
        onnx_session = encoder.model.model
        outputs = onnx_session.get_outputs()
        providers = onnx_session.get_providers()
        truncation = encoder.model.tokenizer.truncation
    except (AttributeError, TypeError) as exc:
        raise LecsUnavailable("LECS loaded encoder shape is unavailable.") from exc
    if (
        providers != [execution["provider"]]
        or len(outputs) != 1
        or outputs[0].name != execution["expected_logits_name"]
        or outputs[0].shape != execution["expected_logits_shape"]
        or outputs[0].type != execution["expected_logits_type"]
        or not isinstance(truncation, dict)
        or truncation.get("max_length") != execution["max_pair_tokens"]
    ):
        raise LecsUnavailable("LECS loaded encoder differs from the frozen runtime.")


def _validate_artifact_manifest(manifest: object) -> None:
    if not isinstance(manifest, Mapping) or set(manifest) != {
        "schema_version",
        "artifact_id",
        "upstream",
        "execution",
        "files",
        "files_aggregate_sha256",
    }:
        raise LecsUnavailable("LECS model artifact manifest is invalid.")
    if manifest["schema_version"] != 1 or manifest["artifact_id"] != (
        "xenova-ms-marco-minilm-l6-v2-onnx-fp32-a0914435"
    ):
        raise LecsUnavailable("LECS model artifact identity changed.")
    upstream = manifest["upstream"]
    if upstream != _EXPECTED_UPSTREAM:
        raise LecsUnavailable("LECS upstream revision changed.")
    execution = manifest["execution"]
    expected_execution_keys = {
        "library",
        "library_version",
        "library_wheel_sha256",
        "library_distribution_sha256",
        "onnxruntime_version",
        "onnxruntime_distribution_sha256",
        "tokenizers_version",
        "tokenizers_distribution_sha256",
        "numpy_version",
        "numpy_distribution_sha256",
        "python_version",
        "platform_system",
        "platform_machine",
        "dependency_lock_sha256",
        "provider",
        "threads",
        "batch_size",
        "dtype",
        "local_files_only",
        "trust_remote_code",
        "model_file",
        "expected_logits_shape",
        "expected_logits_name",
        "expected_logits_type",
        "score_extraction",
        "activation",
        "max_pair_tokens",
        "truncation_allowed",
    }
    if (
        not isinstance(execution, Mapping)
        or set(execution) != expected_execution_keys
        or execution.get("library") != "fastembed"
        or execution.get("library_version") != "0.8.0"
        or execution.get("library_wheel_sha256")
        != "40bee672657574a1009e35ec50030a55f2b426842cb011845379817641bbbbd0"
        or execution.get("provider") != "CPUExecutionProvider"
        or execution.get("threads") != 1
        or execution.get("batch_size") != 64
        or execution.get("dtype") != "float32"
        or execution.get("local_files_only") is not True
        or execution.get("trust_remote_code") is not False
        or execution.get("max_pair_tokens") != 512
        or execution.get("truncation_allowed") is not False
        or execution.get("model_file") != "onnx/model.onnx"
        or execution.get("expected_logits_shape") != ["batch_size", 1]
        or execution.get("expected_logits_name") != "logits"
        or execution.get("expected_logits_type") != "tensor(float)"
        or execution.get("score_extraction") != "logits[i,0]"
        or execution.get("activation") != "identity"
    ):
        raise LecsUnavailable("LECS execution contract changed.")
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != 7:
        raise LecsUnavailable("LECS model artifact file manifest is invalid.")
    paths: list[str] = []
    for item in files:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"path", "size", "sha256"}
            or not isinstance(item["path"], str)
            or type(item["size"]) is not int
            or item["size"] < 1
            or not isinstance(item["sha256"], str)
            or len(item["sha256"]) != 64
        ):
            raise LecsUnavailable("LECS model artifact file entry is invalid.")
        paths.append(item["path"])
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise LecsUnavailable("LECS model artifact paths are invalid.")
    if manifest["files_aggregate_sha256"] != _EXPECTED_FILES_AGGREGATE:
        raise LecsUnavailable("LECS model artifact aggregate is invalid.")


def _validate_selection_contract(contract: object) -> None:
    if not isinstance(contract, Mapping) or set(contract) != {
        "schema_version",
        "architecture_id",
        "dataset_policy",
        "retrieval",
        "selector",
        "gates",
    }:
        raise LecsUnavailable("LECS selection contract is invalid.")
    selector = contract["selector"]
    if (
        contract["schema_version"] != 1
        or contract["architecture_id"] != "memops50-lecs-selection-v1"
        or not isinstance(selector, Mapping)
        or selector.get("model_artifact_manifest_sha256") != FROZEN_ARTIFACT_MANIFEST_SHA256
        or selector.get("provider_calls") != 0
        or selector.get("remote_io") != 0
        or selector.get("fallback_calls") != 0
        or selector.get("score_type") != "native_single_relevance_logit_float32"
        or selector.get("score_operator") != ">"
        or selector.get("score_threshold") != 0.0
        or selector.get("max_selected_turns") != 7
        or selector.get("model_left_input") != "exact_question"
        or selector.get("model_right_input") != "exact_role_newline_exact_turn_content"
    ):
        raise LecsUnavailable("LECS selection policy changed.")
