from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openchronicle.evaluation import lecs
from openchronicle.evaluation.memops50_evidence_answer import Candidate, CandidateTurn

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "benchmarks" / "memops50-lecs-v1" / "json" / "model_artifact_manifest.json"


class FixtureScorer:
    model_id = "fixture:cross-encoder-v1"

    def __init__(self, scores: list[float], *, token_count: int = 10) -> None:
        self._scores = scores
        self._token_count = token_count
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def score(
        self,
        query: str,
        documents: tuple[str, ...],
    ) -> tuple[lecs.EvidenceScore, ...]:
        self.calls.append((query, tuple(documents)))
        return tuple(lecs.EvidenceScore(score, self._token_count) for score in self._scores)


class _FakeEncoding:
    def __init__(self, count: int) -> None:
        self.ids = list(range(count))


class _FakeTokenizer:
    def __init__(self, count: int) -> None:
        self._count = count

    def encode(self, _query: str, _document: str) -> _FakeEncoding:
        return _FakeEncoding(self._count)


class _NeverEncoder:
    def rerank(self, *_args: object, **_kwargs: object) -> list[float]:
        raise AssertionError("overlength input must fail before ONNX inference")


def _candidates(count: int) -> tuple[Candidate, ...]:
    return tuple(
        Candidate(
            segment_id=index,
            rank=index,
            turns=(
                CandidateTurn(
                    ref=f"R{index:02d}-T01",
                    segment_id=index,
                    turn_index=1,
                    role="user" if index % 2 else "assistant",
                    content=f"turn {index}",
                ),
            ),
            distractor=False,
            query_mode="strict_and",
        )
        for index in range(1, count + 1)
    )


def test_lecs_strict_zero_threshold_and_presentation_order() -> None:
    scorer = FixtureScorer([-1.0, 0.0, 0.1, 2.0])

    result = lecs.select_evidence(
        query="question",
        candidates=_candidates(4),
        scorer=scorer,
    )

    assert result.selection_status == "selected"
    assert result.selected_evidence_refs == ("R03-T01", "R04-T01")
    assert [trace.score_rank for trace in result.traces] == [4, 3, 2, 1]
    assert [trace.raw_score_bits for trace in result.traces] == [
        "0xbf800000",
        "0x00000000",
        "0x3dcccccd",
        "0x40000000",
    ]
    assert scorer.calls == [
        (
            "question",
            (
                "user\nturn 1",
                "assistant\nturn 2",
                "user\nturn 3",
                "assistant\nturn 4",
            ),
        )
    ]


def test_lecs_cap_ties_and_empty_selection_are_deterministic() -> None:
    candidates = _candidates(9)
    first = lecs.select_evidence(
        query="question",
        candidates=candidates,
        scorer=FixtureScorer([1.0] * 9),
    )
    second = lecs.select_evidence(
        query="question",
        candidates=candidates,
        scorer=FixtureScorer([1.0] * 9),
    )

    assert first.selected_evidence_refs == tuple(f"R{index:02d}-T01" for index in range(1, 8))
    assert first.capped_positive_refs == ("R08-T01", "R09-T01")
    assert first.traces == second.traces
    assert lecs.selection_trace_sha256(first) == lecs.selection_trace_sha256(second)

    empty = lecs.select_evidence(
        query="question",
        candidates=candidates[:2],
        scorer=FixtureScorer([-0.0, -1.0]),
    )
    assert empty.selection_status == "insufficient"
    assert empty.selected_evidence_refs == ()


@pytest.mark.parametrize("scores", [[float("nan")], [float("inf")], []])
def test_lecs_rejects_invalid_scores(scores: list[float]) -> None:
    with pytest.raises(lecs.LecsUnavailable):
        lecs.select_evidence(
            query="question",
            candidates=_candidates(1),
            scorer=FixtureScorer(scores),
        )


def test_lecs_rejects_policy_changes_and_duplicate_refs() -> None:
    with pytest.raises(ValueError, match="frozen contract"):
        lecs.select_evidence(
            query="question",
            candidates=_candidates(1),
            scorer=FixtureScorer([1.0]),
            max_selected=6,
        )
    candidate = _candidates(1)[0]
    duplicate = Candidate(
        segment_id=2,
        rank=2,
        turns=(candidate.turns[0],),
        distractor=False,
        query_mode="strict_and",
    )
    with pytest.raises(ValueError, match="identity|not unique"):
        lecs.select_evidence(
            query="question",
            candidates=(candidate, duplicate),
            scorer=FixtureScorer([1.0, 1.0]),
        )


def test_model_manifest_matches_provisioned_snapshot(tmp_path: Path) -> None:
    manifest = lecs.load_artifact_manifest(MANIFEST_PATH)
    for item in manifest["files"]:
        path = tmp_path / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"wrong")

    with pytest.raises(lecs.LecsUnavailable, match="digest changed"):
        lecs.verify_model_artifacts(tmp_path, manifest)

    malformed = json.loads(MANIFEST_PATH.read_bytes())
    malformed["execution"]["batch_size"] = 32
    with pytest.raises(lecs.LecsUnavailable, match="execution contract"):
        lecs.verify_model_artifacts(tmp_path, malformed)


def test_frozen_artifact_manifest_is_anchored(tmp_path: Path) -> None:
    contract_path = MANIFEST_PATH.with_name("selection_metric_contract.json")
    assert lecs.load_frozen_artifact_manifest(
        artifact_manifest_path=MANIFEST_PATH,
        selection_contract_path=contract_path,
    )["files_aggregate_sha256"] == (
        "ac180b887cbdf361a4e13b332a61e5695eb37fc4d4249333a778eb18c9714a96"
    )

    changed = tmp_path / "model.json"
    changed.write_bytes(MANIFEST_PATH.read_bytes() + b"\n")
    with pytest.raises(lecs.LecsUnavailable, match="not contract-anchored"):
        lecs.load_frozen_artifact_manifest(
            artifact_manifest_path=changed,
            selection_contract_path=contract_path,
        )


def test_fastembed_adapter_rejects_overlength_before_inference() -> None:
    adapter = object.__new__(lecs.FastEmbedCrossEncoder)
    adapter._tokenizer = _FakeTokenizer(513)
    adapter._encoder = _NeverEncoder()
    adapter._batch_size = 64
    adapter._max_pair_tokens = 512

    with pytest.raises(lecs.LecsUnavailable, match="token limit"):
        adapter.score("question", ("user\ncontent",))


def test_fastembed_constructor_freezes_offline_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = lecs.load_artifact_manifest(MANIFEST_PATH)
    calls: list[dict[str, object]] = []

    class FakePreflightTokenizer:
        def no_truncation(self) -> None:
            calls.append({"preflight_no_truncation": True})

        def no_padding(self) -> None:
            calls.append({"preflight_no_padding": True})

        def encode(self, _query: str, _document: str) -> _FakeEncoding:
            return _FakeEncoding(10)

    class FakeTokenizerFactory:
        @staticmethod
        def from_file(path: str) -> FakePreflightTokenizer:
            calls.append({"tokenizer_path": path})
            return FakePreflightTokenizer()

    class FakeOutput:
        name = "logits"
        shape = ["batch_size", 1]
        type = "tensor(float)"

    class FakeSession:
        def get_outputs(self) -> list[FakeOutput]:
            return [FakeOutput()]

        def get_providers(self) -> list[str]:
            return ["CPUExecutionProvider"]

    class FakeTextCrossEncoder:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            self.model = SimpleNamespace(
                model=FakeSession(),
                tokenizer=SimpleNamespace(truncation={"max_length": 512}),
            )

    import fastembed.rerank.cross_encoder as cross_encoder

    monkeypatch.setattr(
        lecs,
        "load_frozen_artifact_manifest",
        lambda **_kwargs: manifest,
    )
    monkeypatch.setattr(lecs, "verify_model_artifacts", lambda *_args: "a" * 64)
    monkeypatch.setattr(lecs, "verify_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cross_encoder, "TextCrossEncoder", FakeTextCrossEncoder)
    monkeypatch.setitem(
        sys.modules,
        "tokenizers",
        SimpleNamespace(Tokenizer=FakeTokenizerFactory),
    )

    lecs.FastEmbedCrossEncoder(
        model_dir=tmp_path,
        artifact_manifest_path=MANIFEST_PATH,
        selection_contract_path=MANIFEST_PATH.with_name("selection_metric_contract.json"),
        dependency_lock=ROOT / "uv.lock",
    )

    constructor = next(call for call in calls if "model_name" in call)
    assert constructor == {
        "model_name": "Xenova/ms-marco-MiniLM-L-6-v2",
        "cache_dir": str(tmp_path.parent),
        "threads": 1,
        "providers": ("CPUExecutionProvider",),
        "cuda": False,
        "lazy_load": False,
        "local_files_only": True,
        "specific_model_path": str(tmp_path.resolve()),
    }
    assert {"preflight_no_truncation": True} in calls
    assert {"preflight_no_padding": True} in calls


def test_runtime_platform_drift_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = lecs.load_artifact_manifest(MANIFEST_PATH)
    monkeypatch.setattr(lecs.platform, "python_version", lambda: "0.0.0")

    with pytest.raises(lecs.LecsUnavailable, match="frozen runtime changed"):
        lecs.verify_runtime(manifest, dependency_lock=ROOT / "uv.lock")


@pytest.mark.parametrize(
    ("providers", "shape", "truncation"),
    [
        (["CoreMLExecutionProvider"], ["batch_size", 1], {"max_length": 512}),
        (["CPUExecutionProvider"], ["batch_size", 2], {"max_length": 512}),
        (["CPUExecutionProvider"], ["batch_size", 1], {"max_length": 256}),
    ],
)
def test_loaded_encoder_runtime_drift_is_rejected(
    providers: list[str],
    shape: list[object],
    truncation: dict[str, int],
) -> None:
    execution = lecs.load_artifact_manifest(MANIFEST_PATH)["execution"]
    output = SimpleNamespace(name="logits", shape=shape, type="tensor(float)")
    session = SimpleNamespace(
        get_outputs=lambda: [output],
        get_providers=lambda: providers,
    )
    encoder = SimpleNamespace(
        model=SimpleNamespace(
            model=session,
            tokenizer=SimpleNamespace(truncation=truncation),
        )
    )

    with pytest.raises(lecs.LecsUnavailable, match="loaded encoder differs"):
        lecs._validate_loaded_encoder(encoder, execution)


def test_lecs_canonicalizes_shuffled_candidates_and_turns() -> None:
    rank_one = Candidate(
        segment_id=10,
        rank=1,
        turns=(
            CandidateTurn("R01-T02", 10, 2, "assistant", "second"),
            CandidateTurn("R01-T01", 10, 1, "user", "first"),
        ),
        distractor=False,
        query_mode="strict_and",
    )
    rank_two = Candidate(
        segment_id=20,
        rank=2,
        turns=(CandidateTurn("R02-T01", 20, 1, "user", "third"),),
        distractor=False,
        query_mode="strict_and",
    )
    scorer = FixtureScorer([0.5, 0.7, 0.9])

    result = lecs.select_evidence(
        query="question",
        candidates=(rank_two, rank_one),
        scorer=scorer,
    )

    assert result.selected_evidence_refs == (
        "R01-T01",
        "R01-T02",
        "R02-T01",
    )
    assert scorer.calls[0][1] == (
        "user\nfirst",
        "assistant\nsecond",
        "user\nthird",
    )


def test_selector_trace_contains_no_candidate_annotations() -> None:
    candidate = Candidate(
        segment_id=1,
        rank=1,
        turns=(CandidateTurn("R01-T01", 1, 1, "user", "visible content"),),
        distractor=True,
        query_mode="relaxed_or_after_zero_hits",
    )
    result = lecs.select_evidence(
        query="visible question",
        candidates=(candidate,),
        scorer=FixtureScorer([1.0]),
    )
    payload = json.dumps(
        result.traces[0].__dict__
        if hasattr(result.traces[0], "__dict__")
        else {
            field: getattr(result.traces[0], field)
            for field in lecs.LecsTurnTrace.__dataclass_fields__
        }
    )

    assert "distractor" not in payload
    assert "gold" not in payload
    assert "segment_id" not in payload
    assert "visible content" not in payload
    assert "visible question" not in payload
