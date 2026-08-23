from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from openchronicle.evaluation import memoryagentbench_factconsolidation as mab


def _sample() -> mab.SourceSample:
    return mab.SourceSample(
        context="\n".join(
            [
                "Here is a list of facts:",
                "0. The capital of France is Paris.",
                "1. The capital of Germany is Berlin.",
                "2. The capital of France is Harare.",
                "3. The capital of France is Lyon.",
            ]
        ),
        questions=(
            "What is the capital of France?",
            "What is the capital of Germany?",
        ),
        answers=(("Lyon",), ("Berlin",)),
        qa_pair_ids=("synthetic-no0", "synthetic-no1"),
        source="synthetic",
    )


def test_parser_maps_statement_and_question_to_the_same_slot() -> None:
    statement = mab.parse_statement(
        "The headquarters of University of California, Berkeley is located "
        "in the city of Berkeley."
    )
    question = mab.parse_question(
        "Which city is the headquarter of University of California, Berkeley located in?"
    )

    assert statement.slot == question.slot
    assert statement.subject_key == question.subject_key
    assert statement.value == "Berkeley"


def test_synthetic_tier_uses_reviewed_lifecycle_and_keeps_second_update_current() -> None:
    report = mab.evaluate_sample(_sample())

    assert report["ingest"] == {
        **report["ingest"],
        "operation_count": 4,
        "accepted_count": 4,
        "append_count": 2,
        "supersede_count": 2,
        "current_fact_count": 2,
        "unique_current_slot_count": 2,
        "duplicate_current_slot_count": 0,
        "history_entry_count": 4,
        "current_slot_consistency": 1.0,
    }
    assert report["variants"]["no_memory"]["accuracy"] == 0.0
    assert report["variants"]["bm25_top1_current_only"]["accuracy"] == 1.0
    assert report["variants"]["bm25_top20_plus_slot_oracle"]["accuracy"] == 1.0
    assert report["variants"]["typed_slot_oracle"]["accuracy"] == 1.0
    assert report["variants"]["typed_slot_oracle"]["stale_value_rate"] == 0.0


def test_duplicate_current_slots_are_counted_instead_of_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_list_current_facts = mab.list_current_facts

    def duplicate_one(*args, **kwargs):
        facts = real_list_current_facts(*args, **kwargs)
        return [*facts, facts[0]]

    monkeypatch.setattr(mab, "list_current_facts", duplicate_one)

    report = mab.evaluate_sample(_sample())

    assert report["ingest"]["current_fact_count"] == 3
    assert report["ingest"]["unique_current_slot_count"] == 2
    assert report["ingest"]["duplicate_current_slot_count"] == 1
    assert report["ingest"]["current_slot_consistency"] < 1.0


def test_prediction_with_current_and_historical_values_is_stale() -> None:
    sample = _sample()
    questions = tuple(mab.parse_question(question) for question in sample.questions)
    facts = mab.parse_context(sample.context)
    histories: dict[tuple[str, str], list[str]] = {}
    for fact in facts:
        histories.setdefault(fact.slot, []).append(fact.value)

    result = mab._evaluate_variant(  # noqa: SLF001 - metric regression test
        sample,
        questions,
        histories,
        answer=lambda question: (
            "Paris and Lyon" if question.endswith("France?") else "Berlin"
        ),
    )

    assert result["accuracy"] == 1.0
    assert result["stale_value_rate"] == 0.5
    assert result["contradiction_rate"] == 0.5
    assert result["contradiction_free_accuracy"] == 0.5


def test_bm25_top1_does_not_use_the_slot_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [
        mab.fts.EntryHit(
            id="germany",
            path="project-benchmark-facts-capital.md",
            prefix="project-",
            timestamp="2026-08-24T00:00",
            tags="fact",
            content="The capital of Germany is Berlin.",
            superseded=0,
            rank=0.1,
        ),
        mab.fts.EntryHit(
            id="france",
            path="project-benchmark-facts-capital.md",
            prefix="project-",
            timestamp="2026-08-24T00:01",
            tags="fact",
            content="The capital of France is Lyon.",
            superseded=0,
            rank=0.2,
        ),
    ]
    requested_top_k: list[int] = []

    def fake_search(*_args, **kwargs):
        requested_top_k.append(kwargs["top_k"])
        return hits[: kwargs["top_k"]]

    monkeypatch.setattr(mab.fts, "search", fake_search)
    current = {(hit.path, hit.id): object() for hit in hits}

    assert mab._bm25_top1_answer(  # type: ignore[arg-type]  # noqa: SLF001
        None,
        "What is the capital of France?",
        current,
    ) == "Berlin"
    assert mab._bm25_slot_oracle_answer(  # type: ignore[arg-type]  # noqa: SLF001
        None,
        "What is the capital of France?",
        current,
    ) == "Lyon"
    assert requested_top_k == [1, 20]


def test_frozen_manifest_pins_code_data_and_all_query_ids() -> None:
    repository = Path(__file__).resolve().parents[1]
    path = (
        repository
        / "benchmarks"
        / "memoryagentbench-factconsolidation-v1"
        / "json"
        / "manifest.json"
    )
    manifest = mab.load_manifest(path)

    assert manifest["upstream"]["code_revision"] == (
        "fe1735de8cf8b9908e1e3d3b5612afc815698062"
    )
    assert manifest["data"]["revision"] == (
        "7ea066982b140a19337e17e60d45d4076e042faf"
    )
    assert manifest["data"]["parquet_sha256"] == (
        "24d5c3f09ce0ce15625cb9f8a98f44f0d864ca6c94d7b4ad04eb697ca3a5ff45"
    )
    contract_path = path.with_name("metric_contract.json")
    assert manifest["metric_contract"]["path"] == "json/metric_contract.json"
    assert manifest["metric_contract"]["sha256"] == hashlib.sha256(
        contract_path.read_bytes()
    ).hexdigest()
    assert manifest["qa_pair_ids"] == [
        f"factconsolidation_sh_6k_no{index}" for index in range(100)
    ]


def test_parquet_hash_is_checked_before_optional_reader_import(tmp_path: Path) -> None:
    manifest = {
        "data": {"parquet_sha256": "0" * 64},
        "selection": {"metadata_source": "synthetic", "matching_row_count": 1},
    }
    wrong = tmp_path / "wrong.parquet"
    wrong.write_bytes(b"not the frozen data")

    with pytest.raises(ValueError, match="SHA-256 differs"):
        mab.load_parquet_sample(wrong, manifest)


def test_official_substring_metric_normalizes_articles_and_punctuation() -> None:
    assert mab.substring_exact_match("Answer: The Beatles.", "The Beatles")
    assert not mab.substring_exact_match("Harare", "Paris")


def test_report_encoder_round_trips_json(tmp_path: Path) -> None:
    report = {"result": {"accuracy": 1.0}}
    output = tmp_path / "report.json"

    encoded = mab.write_report(report, output)

    assert json.loads(encoded) == report
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_metric_contract_rejects_changed_gate_values() -> None:
    repository = Path(__file__).resolve().parents[1]
    benchmark_json = (
        repository / "benchmarks" / "memoryagentbench-factconsolidation-v1" / "json"
    )
    manifest = mab.load_manifest(benchmark_json / "manifest.json")
    contract_bytes = (benchmark_json / "metric_contract.json").read_bytes()
    contract = json.loads(contract_bytes)
    contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
    mab._validate_contract(  # noqa: SLF001 - frozen trust-boundary regression
        contract,
        manifest,
        contract_sha256=contract_sha256,
    )
    contract["gates"]["typed_slot_oracle_accuracy_min"] = 0.0

    with pytest.raises(ValueError, match="does not match"):
        mab._validate_contract(  # noqa: SLF001 - frozen trust-boundary regression
            contract,
            manifest,
            contract_sha256=hashlib.sha256(
                json.dumps(contract, sort_keys=True).encode()
            ).hexdigest(),
        )

    contract = json.loads(contract_bytes)
    contract["gates"].pop("repository_clean")
    with pytest.raises(ValueError, match="does not match"):
        mab._validate_contract(  # noqa: SLF001 - frozen schema regression
            contract,
            manifest,
            contract_sha256=contract_sha256,
        )
