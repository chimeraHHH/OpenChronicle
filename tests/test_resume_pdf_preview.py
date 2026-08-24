from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openchronicle.evaluation.resume_render import build_case_document_tree, load_cases
from openchronicle.resume_rescue import pdf_export, pdf_preview
from openchronicle.resume_rescue.native_export import (
    NativeResumeExportError,
    ResumeNativeExport,
)
from openchronicle.resume_rescue.render import render_preview_tree

ROOT = Path(__file__).resolve().parents[1]
FONT_DIRECTORY = ROOT / "src" / "openchronicle" / "assets" / "pdf_fonts"
MANIFEST = ROOT / "benchmarks" / "vida-resume-rescue-v1" / "native-export" / "manifest.json"


def test_pdf_preview_pins_match_native_export_contract() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["pdf_preview"] == {
        "kind": "backend-rasterized-png-pages",
        "pypdfium2_version": pdf_preview.PDFIUM_VERSION,
        "scale": pdf_preview.PDF_PREVIEW_SCALE,
        "maximum_pages": pdf_preview.MAX_PREVIEW_PAGES,
        "maximum_page_bytes": pdf_preview.MAX_PREVIEW_PAGE_BYTES,
        "maximum_total_bytes": pdf_preview.MAX_PREVIEW_TOTAL_BYTES,
        "webview_pdf_bytes": False,
        "webview_pdf_path": False,
    }


def test_pdf_preview_rejects_changed_export_binding() -> None:
    tree, _segments = build_case_document_tree(load_cases()[0])
    preview = render_preview_tree(tree)
    exported = ResumeNativeExport(
        projection_id=tree.projection_id,
        artifact_digest=tree.artifact_digest,
        preview_document_digest=preview.document_digest,
        format="pdf",
        media_type="application/pdf",
        extension="pdf",
        content=b"changed",
        content_digest="0" * 64,
    )

    with pytest.raises(NativeResumeExportError, match="binding is invalid"):
        pdf_preview.render_pdf_preview(exported)


@pytest.mark.skipif(
    not all((FONT_DIRECTORY / pin["filename"]).is_file() for pin in pdf_export.FONT_PINS.values()),
    reason="deterministically generated release font assets are absent",
)
def test_real_pdf_preview_uses_exact_pdf_digest_and_deterministic_png_pages() -> None:
    expected_pages = [1, 1, 1, 3]

    for case, page_count in zip(load_cases(), expected_pages, strict=True):
        tree, _segments = build_case_document_tree(case)
        semantic_preview = render_preview_tree(tree)
        exported = pdf_export.render_pdf_export(
            tree, preview_document_digest=semantic_preview.document_digest
        )
        first = pdf_preview.render_pdf_preview(exported)
        second = pdf_preview.render_pdf_preview(exported)

        assert first == second
        assert first.pdf_content_digest == exported.content_digest
        assert first.preview_document_digest == semantic_preview.document_digest
        assert len(first.pages) == page_count
        payload = first.to_dict()
        for page in payload["pages"]:
            content = base64.b64decode(page["content_base64"], validate=True)
            assert content.startswith(b"\x89PNG\r\n\x1a\n")
            assert content.endswith(b"IEND\xaeB`\x82")
            assert len(content) == page["byte_count"]
