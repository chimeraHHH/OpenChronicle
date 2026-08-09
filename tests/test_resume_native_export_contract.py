from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "benchmarks" / "vida-resume-rescue-v1" / "native-export"


def test_native_export_contract_freezes_formats_references_and_hard_gates() -> None:
    manifest = json.loads((CONTRACT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["formats"] == ["pdf", "docx"]
    assert manifest["pdf_engine"]["release_boundary"] == "bundled_offline"
    assert manifest["pdf_engine"]["kind"] == "reportlab-harfbuzz-offline"
    assert manifest["docx_engine"] == {
        "repository": "python-openxml/python-docx",
        "commit": "e45454602b53e8e572b179ccf1c91093ec9f4ed7",
        "version": "1.2.0",
        "license": "MIT",
    }
    assert manifest["docx_qa"] == {
        "kind": "libreoffice-headless-to-pdf",
        "version": "LibreOffice 26.2.3.2 70e089b17412e4cb7773e41413306b17a2328c34",
        "tools": {
            "pdfinfo_version": "pdfinfo version 26.04.0",
            "pdftotext_version": "pdftotext version 26.04.0",
            "pdftoppm_version": "pdftoppm version 26.04.0",
        },
        "page_width_points": 595.276,
        "page_height_points": 841.89,
        "page_size_tolerance_points": 2.0,
        "maximum_pdf_bytes": 10_485_760,
        "maximum_png_bytes": 20_971_520,
        "expected_pages": {
            "single-page-hostile-markup": [1, 1],
            "unicode-and-directionality": [1, 1],
            "long-unbroken-token": [1, 1],
            "automatic-multipage-flow": [2, 4],
        },
    }
    assert {item["repository"] for item in manifest["comparators"]} == {
        "AmruthPillai/Reactive-Resume",
        "Kozea/WeasyPrint",
        "MrBitBucket/reportlab-mirror",
        "py-pdf/fpdf2",
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
