from __future__ import annotations

import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = (
    REPOSITORY_ROOT / "benchmarks" / "vida-resume-rescue-v1" / "document-extraction"
)


def test_document_extraction_manifest_is_closed_and_bounded() -> None:
    manifest = json.loads((CONTRACT_ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert set(manifest) == {
        "schema_version",
        "suite_id",
        "extractor_version",
        "formats",
        "limits",
        "hard_gates",
        "reference_implementations",
    }
    assert manifest["schema_version"] == 1
    assert manifest["suite_id"] == "OC-Vida-Resume-Document-Extraction-v1"
    assert manifest["extractor_version"] == 1
    assert manifest["formats"] == ["pdf", "docx"]
    assert manifest["limits"] == {
        "source_maximum_bytes": 8 * 1024 * 1024,
        "pdf_maximum_pages": 50,
        "extracted_maximum_chars": 500_000,
        "candidate_maximum_count": 2_000,
        "candidate_maximum_chars": 8_000,
        "docx_maximum_members": 2_000,
        "docx_maximum_uncompressed_bytes": 32 * 1024 * 1024,
        "docx_maximum_member_bytes": 8 * 1024 * 1024,
        "docx_maximum_compression_ratio": 100,
        "worker_timeout_seconds": 15,
    }
    assert set(manifest["hard_gates"].values()) == {True, "none"}
    assert len(manifest["reference_implementations"]) >= 6
    assert len(
        {item["repository"] for item in manifest["reference_implementations"]}
    ) == len(manifest["reference_implementations"])
    assert all(
        len(item["commit"]) == 40 or item["commit"] == "none"
        for item in manifest["reference_implementations"]
    )


def test_document_extraction_cases_freeze_adversarial_surface() -> None:
    dataset = json.loads((CONTRACT_ROOT / "cases.json").read_text(encoding="utf-8"))

    assert set(dataset) == {"schema_version", "dataset_id", "split", "cases"}
    assert dataset["schema_version"] == 1
    assert dataset["dataset_id"] == "OC-Vida-Resume-Document-Extraction-v1"
    assert dataset["split"] == "synthetic-document-ingress-adversarial-dev-v1"
    cases = dataset["cases"]
    assert len(cases) == 20
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["format"] for case in cases} == {"pdf", "docx"}
    assert {case["expected"]["action_capability"] for case in cases} == {"none"}
    assert {case["expected"]["locator"] for case in cases} == {
        "none",
        "page_bbox",
        "part_block",
    }
    ids = {case["id"] for case in cases}
    assert {
        "pdf-two-column-order-uncertain",
        "pdf-image-only",
        "pdf-encrypted",
        "docx-external-relationship",
        "docx-macro-or-embedded-object",
        "docx-zip-expansion-limit",
        "admission-source-tamper",
        "admission-profile-cas-race",
    }.issubset(ids)
