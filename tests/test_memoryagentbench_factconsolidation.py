from __future__ import annotations

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
        "history_entry_count": 4,
        "current_slot_consistency": 1.0,
    }
    assert report["variants"]["no_memory"]["accuracy"] == 0.0
    assert report["variants"]["bm25_current_only"]["accuracy"] == 1.0
    assert report["variants"]["typed_current_fact"]["accuracy"] == 1.0
    assert report["variants"]["typed_current_fact"]["stale_value_rate"] == 0.0


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
