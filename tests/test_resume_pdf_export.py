from __future__ import annotations

import json
from pathlib import Path

import pytest

from openchronicle.evaluation.resume_render import build_case_document_tree, load_cases
from openchronicle.resume_rescue import pdf_export
from openchronicle.resume_rescue.native_export import NativeResumeExportError
from openchronicle.resume_rescue.render import render_preview_tree

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    REPOSITORY_ROOT / "benchmarks" / "vida-resume-rescue-v1" / "native-export" / "manifest.json"
)


def test_pdf_export_pins_match_the_frozen_native_manifest() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["pdf_engine"]["version"] == pdf_export.PDF_ENGINE_VERSION
    assert manifest["pdf_engine"]["executable_sha256"] == pdf_export.PDF_ENGINE_SHA256
    assert manifest["limits"]["maximum_pdf_bytes"] == pdf_export.MAX_PDF_BYTES
    assert (
        manifest["limits"]["renderer_timeout_seconds"]
        == pdf_export.PDF_RENDER_TIMEOUT_SECONDS
    )


def test_pdf_export_reuses_reviewed_tree_and_returns_only_validated_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree, _segments = build_case_document_tree(load_cases()[0])
    preview = render_preview_tree(tree)
    chrome = tmp_path / "chrome"
    chrome.write_bytes(b"pinned")
    chrome.chmod(0o700)
    observed: dict[str, object] = {}

    monkeypatch.setattr(pdf_export, "_resolve_pinned_chrome", lambda _path: chrome)
    monkeypatch.setattr(
        pdf_export,
        "_resolve_pinned_command",
        lambda name: f"/pinned/{name}",
    )

    def fake_print(
        resolved_chrome: Path, html_path: Path, pdf_path: Path, *, timeout: int
    ) -> str:
        observed["chrome"] = resolved_chrome
        observed["html"] = html_path.read_text(encoding="utf-8")
        observed["timeout"] = timeout
        pdf_path.write_bytes(b"%PDF-1.7\nreviewed fixture\n%%EOF\n")
        return "exited_after_output_ready"

    def fake_validate(
        content: bytes, *, tree: object, pdf_path: Path, commands: object
    ) -> None:
        observed["content"] = content
        observed["tree"] = tree
        observed["pdf_path"] = pdf_path
        observed["commands"] = commands

    monkeypatch.setattr(pdf_export, "print_pdf", fake_print)
    monkeypatch.setattr(pdf_export, "_validate_pdf", fake_validate)

    exported = pdf_export.render_pdf_export(
        tree,
        preview_document_digest=preview.document_digest,
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
    assert observed["chrome"] == chrome
    assert str(observed["html"]).startswith("<!doctype html>\n")
    assert "default-src 'none'" in str(observed["html"])
    assert observed["tree"] is tree
    assert observed["commands"] == {
        "pdfinfo": "/pinned/pdfinfo",
        "pdffonts": "/pinned/pdffonts",
        "pdftotext": "/pinned/pdftotext",
    }


def test_pdf_export_rejects_stale_preview_before_engine_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree, _segments = build_case_document_tree(load_cases()[0])
    monkeypatch.setattr(
        pdf_export,
        "_resolve_pinned_chrome",
        lambda _path: pytest.fail("stale preview must fail before engine access"),
    )

    with pytest.raises(NativeResumeExportError, match="preview binding"):
        pdf_export.render_pdf_export(tree, preview_document_digest="f" * 64)


def test_pdf_export_rejects_symlink_engine_and_unembedded_fonts(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.write_bytes(b"browser")
    engine.chmod(0o700)
    link = tmp_path / "linked-engine"
    link.symlink_to(engine)

    with pytest.raises(NativeResumeExportError, match="unavailable"):
        pdf_export._resolve_pinned_chrome(link)

    embedded = (
        "name type encoding emb sub uni object ID\n"
        "------------------------------------------\n"
        "AAAAAA+Arial TrueType WinAnsi yes yes yes 4 0\n"
    )
    assert pdf_export._font_report_embedded(embedded)
    assert not pdf_export._font_report_embedded(embedded.replace(" yes yes yes ", " no yes yes "))
