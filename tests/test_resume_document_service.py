from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from openchronicle import config as config_mod
from openchronicle.resume_rescue import ResumeRescueConflict, ResumeRescueService
from openchronicle.resume_rescue.document_extract import DocumentExtractionError
from openchronicle.store import fts


def _cfg() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.resume_rescue.enabled = True
    return cfg


def _docx(*paragraphs: str) -> bytes:
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    body = "".join(
        f"<w:p><w:r><w:t>{escape(paragraph)}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("word/document.xml", document)
    return output.getvalue()


def _manual_fact(fact_id: str, text: str) -> dict[str, object]:
    return {
        "id": fact_id,
        "section": "summary",
        "text": text,
        "confidentiality": "public",
        "ownership_scope": "individual",
        "provenance": [{"kind": "manual_reviewed", "reviewed_at": "2026-08-09T00:00:00Z"}],
    }


def test_document_service_reextracts_and_persists_only_selected_fact(ac_root: Path) -> None:
    source = _docx("Selected exact evidence", "UNSELECTED_PRIVATE_SOURCE_TEXT")
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        review = service.review_document(source, source_format="docx")
        candidate = review.candidates[0]

        profile, created = service.admit_document(
            source=source,
            source_format="docx",
            expected_review_digest=review.review_digest,
            profile_id="primary-profile",
            display_name="Ada Example",
            locale="en",
            selections=[
                {
                    "candidate_id": candidate["id"],
                    "fact_id": "fact-document-1",
                    "section": "experience",
                    "confidentiality": "private",
                    "ownership_scope": "individual",
                }
            ],
        )

        assert created is True
        assert [fact["text"] for fact in profile.profile["facts"]] == [
            "Selected exact evidence"
        ]
        provenance = profile.profile["facts"][0]["provenance"][0]
        assert provenance["kind"] == "document_excerpt"
        assert provenance["source_digest"] == review.source_digest
        assert provenance["page"] == 0
        assert provenance["section"] == "word/document.xml#block=0;kind=paragraph"
        stored_json = conn.execute(
            "SELECT profile_json FROM resume_profiles WHERE profile_id=? AND version=?",
            (profile.profile_id, profile.version),
        ).fetchone()["profile_json"]
        assert "UNSELECTED_PRIVATE_SOURCE_TEXT" not in stored_json
        assert review.source_digest in stored_json


def test_document_service_preserves_profile_identity_conflicts_and_cas(ac_root: Path) -> None:
    source = _docx("Imported evidence")
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        current, _ = service.save_profile(
            profile_id="primary-profile",
            display_name="Existing Name",
            locale="en",
            facts=[_manual_fact("existing-1", "First"), _manual_fact("existing-2", "Second")],
            conflicts=[
                {
                    "id": "conflict-1",
                    "fact_ids": ["existing-1", "existing-2"],
                    "description": "Needs review.",
                }
            ],
        )
        review = service.review_document(source, source_format="docx")
        selection = {
            "candidate_id": review.candidates[0]["id"],
            "fact_id": "imported-1",
            "section": "other",
            "confidentiality": "public",
            "ownership_scope": "individual",
        }

        with pytest.raises(ResumeRescueConflict, match="identity changed"):
            service.admit_document(
                source=source,
                source_format="docx",
                expected_review_digest=review.review_digest,
                profile_id=current.profile_id,
                display_name="Injected Rename",
                locale="en",
                selections=[selection],
                expected_version=current.version,
            )
        admitted, changed = service.admit_document(
            source=source,
            source_format="docx",
            expected_review_digest=review.review_digest,
            profile_id=current.profile_id,
            display_name="Existing Name",
            locale="en",
            selections=[selection],
            expected_version=current.version,
        )
        assert changed is True
        assert admitted.version == 2
        assert admitted.profile["display_name"] == "Existing Name"
        assert admitted.profile["conflicts"] == current.profile["conflicts"]
        with pytest.raises(ResumeRescueConflict, match="profile changed"):
            service.admit_document(
                source=source,
                source_format="docx",
                expected_review_digest=review.review_digest,
                profile_id=current.profile_id,
                display_name="Existing Name",
                locale="en",
                selections=[{**selection, "fact_id": "imported-2"}],
                expected_version=current.version,
            )


def test_document_service_rejects_stale_review_digest_and_fact_collision(ac_root: Path) -> None:
    source = _docx("Imported evidence")
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        current, _ = service.save_profile(
            profile_id="primary-profile",
            display_name="Existing Name",
            locale="en",
            facts=[_manual_fact("existing-1", "Existing")],
        )
        review = service.review_document(source, source_format="docx")
        selection = {
            "candidate_id": review.candidates[0]["id"],
            "fact_id": "existing-1",
            "section": "other",
            "confidentiality": "public",
            "ownership_scope": "individual",
        }

        with pytest.raises(ResumeRescueConflict, match="review changed"):
            service.admit_document(
                source=_docx("Replacement"),
                source_format="docx",
                expected_review_digest=review.review_digest,
                profile_id=current.profile_id,
                display_name="Existing Name",
                locale="en",
                selections=[selection],
                expected_version=current.version,
            )
        with pytest.raises(DocumentExtractionError, match="expected review digest"):
            service.admit_document(
                source=source,
                source_format="docx",
                expected_review_digest="invalid",
                profile_id=current.profile_id,
                display_name="Existing Name",
                locale="en",
                selections=[selection],
                expected_version=current.version,
            )
        with pytest.raises(DocumentExtractionError, match="already exists"):
            service.admit_document(
                source=source,
                source_format="docx",
                expected_review_digest=review.review_digest,
                profile_id=current.profile_id,
                display_name="Existing Name",
                locale="en",
                selections=[selection],
                expected_version=current.version,
            )
        assert json.loads(
            conn.execute(
                "SELECT profile_json FROM resume_profiles WHERE profile_id=?",
                (current.profile_id,),
            ).fetchone()["profile_json"]
        ) == current.profile
