from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from xml.sax.saxutils import escape

import pytest

from openchronicle.resume_rescue.document_extract import (
    MAX_PDF_PAGES,
    MAX_SOURCE_BYTES,
    DocumentExtractionError,
    admit_document_candidates,
    extract_document,
)


def _pdf(pages: list[list[tuple[float, float, str]]]) -> bytes:
    font_id = 3 + (2 * len(pages))
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{3 + (2 * index)} 0 R".encode() for index in range(len(pages)))
            + f"] /Count {len(pages)} >>".encode()
        ),
        font_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, text_items in enumerate(pages):
        page_id = 3 + (2 * index)
        content_id = page_id + 1
        stream = b"\n".join(
            (
                f"BT /F1 12 Tf {x} {y} Td (".encode()
                + text.replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)")
                .encode("ascii")
                + b") Tj ET"
            )
            for x, y, text in text_items
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode()
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (font_id + 1)
    for object_id in range(1, font_id + 1):
        offsets[object_id] = len(output)
        output.extend(f"{object_id} 0 obj\n".encode())
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {font_id + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {font_id + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def _docx(
    *,
    body: str,
    external_relationship: bool = False,
    extras: dict[str, bytes] | None = None,
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    )
    relationship = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + (
            '<Relationship Id="rId1" Type="http://example.test/hyperlink" '
            'Target="https://127.0.0.1/private" TargetMode="External"/>'
            if external_relationship
            else ""
        )
        + "</Relationships>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("word/document.xml", document)
        package.writestr("word/_rels/document.xml.rels", relationship)
        for name, value in (extras or {}).items():
            package.writestr(name, value)
    return output.getvalue()


def test_pdf_review_preserves_line_geometry_and_splits_columns() -> None:
    source = _pdf(
        [
            [
                (50, 720, "Senior Engineer"),
                (360, 720, "Python SQL"),
                (50, 690, "Built reliable systems"),
            ]
        ]
    )

    review = extract_document(source, source_format="pdf")
    repeated = extract_document(source, source_format="pdf")

    assert review == repeated
    assert review.to_dict()["action_capability"] == "none"
    assert [item["text"] for item in review.candidates] == [
        "Senior Engineer",
        "Python SQL",
        "Built reliable systems",
    ]
    assert all(item["locator"]["kind"] == "page_bbox" for item in review.candidates)
    assert all(item["locator"]["page"] == 1 for item in review.candidates)
    assert all(len(item["locator"]["bbox"]) == 4 for item in review.candidates)
    assert {item["code"] for item in review.warnings} == {
        "reading_order_requires_review",
        "untrusted_document_text",
    }


def test_pdf_page_size_and_parse_failures_are_closed() -> None:
    with pytest.raises(DocumentExtractionError, match="signature"):
        extract_document(b"not a pdf", source_format="pdf")
    with pytest.raises(DocumentExtractionError, match="page count"):
        extract_document(_pdf([[] for _ in range(MAX_PDF_PAGES + 1)]), source_format="pdf")
    with pytest.raises(DocumentExtractionError, match="source size"):
        extract_document(b"%PDF-" + (b"x" * MAX_SOURCE_BYTES), source_format="pdf")


def test_empty_pdf_requires_no_silent_candidate() -> None:
    review = extract_document(_pdf([[]]), source_format="pdf")

    assert review.candidates == ()
    assert {item["code"] for item in review.warnings} == {
        "no_extractable_text",
        "reading_order_requires_review",
        "untrusted_document_text",
    }


def test_docx_review_preserves_paragraph_table_and_unicode_blocks() -> None:
    table = (
        "<w:tbl><w:tr>"
        f"<w:tc>{_paragraph('Python')}</w:tc>"
        f"<w:tc>{_paragraph('高级')}</w:tc>"
        "</w:tr></w:tbl>"
    )
    source = _docx(body=_paragraph("Résumé 工程师") + table + _paragraph("مرحبا"))

    review = extract_document(source, source_format="docx")

    assert [item["text"] for item in review.candidates] == [
        "Résumé 工程师",
        "Python | 高级",
        "مرحبا",
    ]
    assert all(item["locator"]["kind"] == "part_block" for item in review.candidates)
    assert all(item["locator"]["page"] == 0 for item in review.candidates)
    assert [item["locator"]["block"] for item in review.candidates] == [0, 1, 2]
    assert {item["code"] for item in review.warnings} == {
        "docx_pagination_unavailable",
        "untrusted_document_text",
    }


def test_docx_external_relationship_is_ledgered_without_fetch() -> None:
    review = extract_document(
        _docx(body=_paragraph("Local text"), external_relationship=True),
        source_format="docx",
    )

    assert [item["text"] for item in review.candidates] == ["Local text"]
    assert "external_relationship_ignored" in {item["code"] for item in review.warnings}
    assert "127.0.0.1" not in str(review.to_dict())


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (_docx(body=_paragraph("text"), extras={"word/vbaProject.bin": b"macro"}), "active"),
        (
            _docx(
                body=_paragraph("text"),
                extras={"custom/high-ratio.bin": b"x" * 100_000},
                compression=zipfile.ZIP_DEFLATED,
            ),
            "compression ratio",
        ),
        (_docx(body="<w:p>"), "could not be parsed"),
    ],
)
def test_docx_active_expansion_and_xml_failures_are_closed(source: bytes, message: str) -> None:
    with pytest.raises(DocumentExtractionError, match=message):
        extract_document(source, source_format="docx")


def test_admission_reextracts_source_and_builds_exact_document_provenance() -> None:
    source = _docx(body=_paragraph("Built exact evidence"))
    review = extract_document(source, source_format="docx")
    selection = {
        "candidate_id": review.candidates[0]["id"],
        "fact_id": "fact.document.1",
        "section": "experience",
        "confidentiality": "private",
        "ownership_scope": "individual",
    }

    facts = admit_document_candidates(
        review,
        source,
        [selection],
        reviewed_at="2026-08-09T20:30:00+08:00",
    )

    assert facts[0]["text"] == "Built exact evidence"
    assert facts[0]["provenance"] == [
        {
            "kind": "document_excerpt",
            "reviewed_at": "2026-08-09T20:30:00+08:00",
            "source_id": review.source_id,
            "source_digest": review.source_digest,
            "page": 0,
            "section": "word/document.xml#block=0;kind=paragraph",
            "start": 0,
            "end": len("Built exact evidence"),
            "extraction_method": "ooxml-bounded-blocks-v1",
        }
    ]
    with pytest.raises(DocumentExtractionError, match="source binding"):
        admit_document_candidates(
            review,
            _docx(body=_paragraph("Replacement content")),
            [selection],
            reviewed_at="2026-08-09T20:30:00+08:00",
        )
    tampered = replace(
        review,
        candidates=({**review.candidates[0], "text": "Tampered"},),
    )
    with pytest.raises(DocumentExtractionError, match="review changed"):
        admit_document_candidates(
            tampered,
            source,
            [selection],
            reviewed_at="2026-08-09T20:30:00+08:00",
        )


def test_admission_rejects_duplicate_selection_and_naive_timestamp() -> None:
    source = _docx(body=_paragraph("One candidate"))
    review = extract_document(source, source_format="docx")
    selection = {
        "candidate_id": review.candidates[0]["id"],
        "fact_id": "fact.document.1",
        "section": "other",
        "confidentiality": "public",
        "ownership_scope": "individual",
    }

    with pytest.raises(DocumentExtractionError, match="binding"):
        admit_document_candidates(
            review,
            source,
            [selection, {**selection, "fact_id": "fact.document.2"}],
            reviewed_at="2026-08-09T20:30:00+08:00",
        )
    with pytest.raises(DocumentExtractionError, match="timezone-aware"):
        admit_document_candidates(
            review,
            source,
            [selection],
            reviewed_at="2026-08-09T20:30:00",
        )
