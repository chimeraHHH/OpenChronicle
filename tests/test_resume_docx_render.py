from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle.evaluation.resume_docx_render import (
    DocxRenderAuditError,
    _ordered_segments_present,
    load_manifest,
)
from openchronicle.evaluation.resume_render import (
    build_case_document_tree,
    build_case_preview,
    load_cases,
)
from openchronicle.resume_rescue.render import render_preview_tree

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    REPOSITORY_ROOT / "benchmarks" / "vida-resume-rescue-v1" / "native-export" / "manifest.json"
)
CASES_PATH = (
    REPOSITORY_ROOT / "benchmarks" / "vida-resume-rescue-v1" / "render" / "cases.json"
)


def test_docx_audit_manifest_pins_independent_renderer_and_poppler() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    assert manifest["docx_qa"]["kind"] == "libreoffice-headless-to-pdf"
    assert manifest["docx_qa"]["version"].startswith("LibreOffice 26.2.3.2")
    assert set(manifest["docx_qa"]["tools"]) == {
        "pdfinfo_version",
        "pdftotext_version",
        "pdftoppm_version",
    }


def test_docx_and_html_cases_share_one_semantic_document_tree() -> None:
    for case in load_cases(CASES_PATH):
        tree, tree_segments = build_case_document_tree(case)
        html_preview, html_segments = build_case_preview(case)
        tree_preview = render_preview_tree(tree)

        assert tree_segments == html_segments
        assert tree_preview == html_preview


def test_ordered_text_check_tolerates_layout_whitespace_but_not_loss_or_reorder() -> None:
    segments = ["林玥 · Zoë", "EXPERIENCE", "Built local-first software."]

    assert _ordered_segments_present(
        "林玥   · Zoë\n\nEXPERIENCE\n• Built local-first\nsoftware.", segments
    )
    assert not _ordered_segments_present("EXPERIENCE\n林玥 · Zoë\nBuilt local-first software.", segments)
    assert not _ordered_segments_present("林玥 · Zoë\nEXPERIENCE", segments)

    long_token = "artifact-" + ("x" * 240) + "-end"
    wrapped_token = long_token[:90] + "\n" + long_token[90:180] + "\n" + long_token[180:]
    assert _ordered_segments_present(wrapped_token, [long_token])
    assert not _ordered_segments_present(wrapped_token.replace("x", "y", 1), [long_token])


def test_docx_audit_manifest_rejects_unpinned_qa_fields(tmp_path: Path) -> None:
    value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    value["docx_qa"]["unexpected"] = True
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(DocxRenderAuditError, match="manifest"):
        load_manifest(path)
