from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "benchmarks" / "vida-resume-rescue-v1" / "native-export"


def test_native_export_contract_freezes_formats_references_and_hard_gates() -> None:
    manifest = json.loads((CONTRACT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["formats"] == ["pdf", "docx"]
    assert manifest["pdf_engine"]["release_boundary"] == "development_only_until_bundled"
    assert manifest["docx_engine"] == {
        "repository": "python-openxml/python-docx",
        "commit": "e45454602b53e8e572b179ccf1c91093ec9f4ed7",
        "version": "1.2.0",
        "license": "MIT",
    }
    assert {item["repository"] for item in manifest["comparators"]} == {
        "AmruthPillai/Reactive-Resume",
        "Kozea/WeasyPrint",
        "MrBitBucket/reportlab-mirror",
        "typst/typst",
    }
    assert all(
        value is True for key, value in manifest["hard_gates"].items() if key != "action_capability"
    )
    assert manifest["hard_gates"]["action_capability"] == "none"


def test_native_export_cases_cover_fidelity_tamper_and_native_save() -> None:
    payload = json.loads((CONTRACT / "cases.json").read_text(encoding="utf-8"))
    cases = payload["cases"]
    identifiers = {case["id"] for case in cases}

    assert len(cases) == 14
    assert {case["format"] for case in cases} == {"pdf", "docx"}
    assert {
        "pdf-multilingual-cjk-rtl",
        "pdf-unpinned-engine",
        "docx-repeat-bytes",
        "docx-hostile-markup-is-text",
        "stale-preview-digest",
        "stale-projection-binding",
        "existing-target-path",
        "webview-byte-injection",
    } <= identifiers
