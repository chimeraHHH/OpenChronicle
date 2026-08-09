from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from docx import Document
from docx.shared import Mm

from openchronicle import config as config_mod
from openchronicle.resume_rescue import ResumeRescueConflict, ResumeRescueService, extract_document
from openchronicle.resume_rescue.native_export import (
    MAX_DOCX_BYTES,
    NativeResumeExportError,
    render_docx_export,
)
from openchronicle.resume_rescue.render import build_document_tree
from openchronicle.store import fts


def _cfg() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.resume_rescue.enabled = True
    return cfg


def _fact(identifier: str, section: str, text: str) -> dict[str, object]:
    return {
        "id": identifier,
        "section": section,
        "text": text,
        "confidentiality": "private",
        "ownership_scope": "individual",
        "provenance": [{"kind": "manual_reviewed", "reviewed_at": "2026-08-09T08:00:00+08:00"}],
    }


def _sources(ac_root: Path):
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        profile, _ = service.save_profile(
            profile_id="native-export-profile",
            display_name="林玥 · Zoë <resume>",
            locale="zh-CN",
            facts=[
                _fact(
                    "fact-experience",
                    "experience",
                    "构建本地优先的软件，并把 <script> 当作普通文本。",
                ),
                _fact(
                    "fact-language",
                    "language",
                    "English, français, 日本語, and العربية.",
                ),
            ],
        )
        opportunity, _ = service.save_opportunity(
            employer="Example Labs",
            title="Engineer",
            source_text="Build local-first software.",
            captured_at="2026-08-09T09:00:00+08:00",
        )
        projection, _ = service.compose_exact(
            profile_id=profile.profile_id,
            opportunity_id=opportunity.id,
            sections=[
                {"kind": "experience", "fact_ids": ["fact-experience"]},
                {"kind": "language", "fact_ids": ["fact-language"]},
            ],
        )
        preview = service.preview(projection.id)
    return profile, projection, preview


def test_docx_export_is_repeatable_bounded_and_semantic_tree_exact(ac_root: Path) -> None:
    profile, projection, preview = _sources(ac_root)
    tree = build_document_tree(profile=profile, projection=projection)

    first = render_docx_export(tree, preview_document_digest=preview.document_digest)
    second = render_docx_export(tree, preview_document_digest=preview.document_digest)

    assert first == second
    assert first.content == second.content
    assert first.content.startswith(b"PK")
    assert len(first.content) < MAX_DOCX_BYTES
    assert first.metadata() == {
        "schema_version": 1,
        "projection_id": projection.id,
        "artifact_digest": projection.artifact_digest,
        "preview_document_digest": preview.document_digest,
        "renderer_version": 1,
        "native_export_version": 1,
        "template_id": "openchronicle-classic-v1",
        "format": "docx",
        "media_type": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "extension": "docx",
        "byte_count": len(first.content),
        "content_digest": first.content_digest,
        "action_capability": "none",
    }

    document = Document(io.BytesIO(first.content))
    assert [paragraph.text for paragraph in document.paragraphs if paragraph.text] == [
        "林玥 · Zoë <resume>",
        "EXPERIENCE",
        "构建本地优先的软件，并把 <script> 当作普通文本。",
        "LANGUAGES",
        "English, français, 日本語, and العربية.",
    ]
    assert abs(document.sections[0].page_width - Mm(210)) < 500
    assert abs(document.sections[0].page_height - Mm(297)) < 500
    assert document.core_properties.author == "OpenChronicle"
    expected_created = (
        datetime.fromisoformat(projection.created_at).astimezone(UTC).replace(microsecond=0)
    )
    assert document.core_properties.created == expected_created

    extracted = extract_document(first.content, source_format="docx")
    assert [candidate["text"] for candidate in extracted.candidates] == [
        "林玥 · Zoë <resume>",
        "EXPERIENCE",
        "构建本地优先的软件，并把 <script> 当作普通文本。",
        "LANGUAGES",
        "English, français, 日本語, and العربية.",
    ]


def test_docx_export_zip_is_canonical_and_contains_no_active_or_external_content(
    ac_root: Path,
) -> None:
    profile, projection, preview = _sources(ac_root)
    exported = render_docx_export(
        build_document_tree(profile=profile, projection=projection),
        preview_document_digest=preview.document_digest,
    )

    with zipfile.ZipFile(io.BytesIO(exported.content)) as package:
        infos = package.infolist()
        assert [info.filename for info in infos] == sorted(info.filename for info in infos)
        assert {info.date_time for info in infos} == {(1980, 1, 1, 0, 0, 0)}
        assert all(not (info.flag_bits & 0x1) for info in infos)
        joined_names = " ".join(info.filename.casefold() for info in infos)
        assert "vbaproject" not in joined_names
        assert "activex" not in joined_names
        assert "embeddings" not in joined_names
        relationship_text = "\n".join(
            package.read(info).decode("utf-8") for info in infos if info.filename.endswith(".rels")
        )
        assert 'TargetMode="External"' not in relationship_text


def test_docx_export_rejects_invalid_preview_binding(ac_root: Path) -> None:
    profile, projection, _preview = _sources(ac_root)
    tree = build_document_tree(profile=profile, projection=projection)

    with pytest.raises(NativeResumeExportError, match="binding"):
        render_docx_export(tree, preview_document_digest="not-a-digest")


def test_docx_service_refetches_current_projection_and_binds_reviewed_preview(
    ac_root: Path,
) -> None:
    profile, projection, preview = _sources(ac_root)
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        exported = service.export_docx(
            projection.id,
            expected_preview_document_digest=preview.document_digest,
        )
        assert exported.projection_id == projection.id
        assert exported.preview_document_digest == preview.document_digest

        with pytest.raises(ResumeRescueConflict, match="preview changed"):
            service.export_docx(
                projection.id,
                expected_preview_document_digest="f" * 64,
            )

        service.save_profile(
            profile_id=profile.profile_id,
            display_name=profile.profile["display_name"],
            locale=profile.profile["locale"],
            facts=[
                *profile.profile["facts"],
                _fact("fact-new", "skill", "A newly reviewed fact."),
            ],
            expected_version=profile.version,
        )
        with pytest.raises(ResumeRescueConflict, match="projection changed"):
            service.export_docx(
                projection.id,
                expected_preview_document_digest=preview.document_digest,
            )
