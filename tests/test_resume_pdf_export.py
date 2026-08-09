from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path

import pdfplumber
import pytest

from openchronicle.evaluation.resume_render import build_case_document_tree, load_cases
from openchronicle.resume_rescue import pdf_export
from openchronicle.resume_rescue.native_export import NativeResumeExportError
from openchronicle.resume_rescue.render import (
    ResumeDocumentItem,
    ResumeDocumentSection,
    render_preview_tree,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    REPOSITORY_ROOT / "benchmarks" / "vida-resume-rescue-v1" / "native-export" / "manifest.json"
)
FONT_DIRECTORY = REPOSITORY_ROOT / "src" / "openchronicle" / "assets" / "pdf_fonts"


def test_pdf_export_pins_match_the_frozen_native_manifest() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    engine = manifest["pdf_engine"]

    assert engine["version"] == pdf_export.PDF_ENGINE_VERSION
    assert engine["reportlab_version"] == pdf_export.REPORTLAB_VERSION
    assert engine["uharfbuzz_version"] == pdf_export.UHARFBUZZ_VERSION
    assert engine["pypdf_version"] == pdf_export.PYPDF_VERSION
    assert engine["font_source"]["commit"] == pdf_export.FONT_SOURCE_COMMIT
    assert engine["font_source"]["assets"] == {
        pin["filename"]: pin["sha256"] for pin in pdf_export.FONT_PINS.values()
    }
    assert manifest["limits"]["maximum_pdf_bytes"] == pdf_export.MAX_PDF_BYTES
    assert manifest["limits"]["renderer_timeout_seconds"] == pdf_export.PDF_RENDER_TIMEOUT_SECONDS


def test_pdf_export_reuses_reviewed_tree_and_returns_only_validated_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree, _segments = build_case_document_tree(load_cases()[0])
    preview = render_preview_tree(tree)
    fonts = pdf_export._FontPaths(
        base=tmp_path / "base.ttf",
        cjk=tmp_path / "cjk.ttf",
        arabic=tmp_path / "arabic.ttf",
    )
    observed: dict[str, object] = {"renders": 0}
    content = b"%PDF-1.7\nreviewed fixture\n%%EOF\n"

    monkeypatch.setattr(pdf_export, "_verify_runtime_versions", lambda: None)
    monkeypatch.setattr(pdf_export, "_resolve_font_assets", lambda _path: fonts)
    monkeypatch.setattr(pdf_export, "_register_fonts", lambda value: observed.update(fonts=value))

    def fake_render(rendered_tree: object) -> bytes:
        observed["tree"] = rendered_tree
        observed["renders"] = int(observed["renders"]) + 1
        return content

    def fake_validate(value: bytes, *, tree: object) -> None:
        observed["content"] = value
        observed["validated_tree"] = tree

    monkeypatch.setattr(pdf_export, "_render_pdf", fake_render)
    monkeypatch.setattr(pdf_export, "_validate_pdf", fake_validate)

    exported = pdf_export.render_pdf_export(
        tree,
        preview_document_digest=preview.document_digest,
        font_directory=tmp_path,
    )

    assert exported.metadata() == {
        "schema_version": 1,
        "projection_id": tree.projection_id,
        "artifact_digest": tree.artifact_digest,
        "preview_document_digest": preview.document_digest,
        "renderer_version": 1,
        "native_export_version": 1,
        "template_id": "openchronicle-classic-v1",
        "format": "pdf",
        "media_type": "application/pdf",
        "extension": "pdf",
        "byte_count": len(exported.content),
        "content_digest": exported.content_digest,
        "action_capability": "none",
    }
    assert observed == {
        "renders": 2,
        "fonts": fonts,
        "tree": tree,
        "content": content,
        "validated_tree": tree,
    }


def test_pdf_export_rejects_stale_preview_before_engine_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree, _segments = build_case_document_tree(load_cases()[0])
    monkeypatch.setattr(
        pdf_export,
        "_verify_runtime_versions",
        lambda: pytest.fail("stale preview must fail before engine access"),
    )

    with pytest.raises(NativeResumeExportError, match="preview binding"):
        pdf_export.render_pdf_export(tree, preview_document_digest="f" * 64)


def test_pdf_export_rejects_symlink_or_changed_font_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(NativeResumeExportError, match="directory is invalid"):
        pdf_export._resolve_font_assets(linked_directory)

    monkeypatch.setattr(
        pdf_export,
        "FONT_PINS",
        {
            "base": {"filename": "base.ttf", "size_bytes": 4, "sha256": "0" * 64},
            "cjk": {"filename": "cjk.ttf", "size_bytes": 4, "sha256": "0" * 64},
            "arabic": {"filename": "arabic.ttf", "size_bytes": 4, "sha256": "0" * 64},
        },
    )
    target = real_directory / "base.ttf"
    target.write_bytes(b"font")
    (real_directory / "cjk.ttf").symlink_to(target)
    (real_directory / "arabic.ttf").write_bytes(b"font")
    with pytest.raises(NativeResumeExportError, match="differs"):
        pdf_export._resolve_font_assets(real_directory)


@pytest.mark.skipif(
    not all((FONT_DIRECTORY / pin["filename"]).is_file() for pin in pdf_export.FONT_PINS.values()),
    reason="deterministically generated release font assets are absent",
)
def test_real_bundled_pdf_export_is_deterministic_searchable_and_bounded() -> None:
    expected_pages = {
        "single-page-hostile-markup": 1,
        "unicode-and-directionality": 1,
        "long-unbroken-token": 1,
        "automatic-multipage-flow": 3,
    }

    for case in load_cases():
        tree, ordered_segments = build_case_document_tree(case)
        preview = render_preview_tree(tree)
        first = pdf_export.render_pdf_export(tree, preview_document_digest=preview.document_digest)
        second = pdf_export.render_pdf_export(tree, preview_document_digest=preview.document_digest)

        assert first.content == second.content
        with pdfplumber.open(io.BytesIO(first.content)) as document:
            assert len(document.pages) == expected_pages[case["id"]]
            extracted = "\n".join(page.extract_text() or "" for page in document.pages)
        assert pdf_export._ordered_content_found(extracted, ordered_segments)


@pytest.mark.skipif(
    not all((FONT_DIRECTORY / pin["filename"]).is_file() for pin in pdf_export.FONT_PINS.values()),
    reason="deterministically generated release font assets are absent",
)
def test_real_bundled_pdf_export_preserves_arabic_locale_logical_text() -> None:
    base, _segments = build_case_document_tree(load_cases()[0])
    tree = replace(
        base,
        display_name="ليلى حسن",
        locale="ar",
        sections=(
            ResumeDocumentSection(
                kind="summary",
                label="الملخص",
                items=(
                    ResumeDocumentItem(
                        fact_id="fact-arabic-001",
                        text="مهندسة موثوقية تبني أنظمة محلية أولاً.",
                    ),
                ),
            ),
        ),
    )
    preview = render_preview_tree(tree)
    exported = pdf_export.render_pdf_export(tree, preview_document_digest=preview.document_digest)

    with pdfplumber.open(io.BytesIO(exported.content)) as document:
        extracted = "\n".join(page.extract_text() or "" for page in document.pages)
    assert pdf_export._ordered_content_found(
        extracted,
        [tree.display_name, tree.sections[0].label, tree.sections[0].items[0].text],
    )
