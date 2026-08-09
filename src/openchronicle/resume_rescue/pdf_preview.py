"""Bounded PNG preview pages rendered from the exact audited PDF bytes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any

import pypdfium2 as pdfium
from PIL import Image

from .native_export import NativeResumeExportError, ResumeNativeExport

PDF_PREVIEW_VERSION = 1
PDFIUM_VERSION = "5.12.1"
PDF_PREVIEW_SCALE = 1.5
MAX_PREVIEW_PAGES = 20
MAX_PREVIEW_PAGE_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_TOTAL_BYTES = 20 * 1024 * 1024
MAX_PREVIEW_DIMENSION = 2_048
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_END = b"IEND\xaeB`\x82"


@dataclass(frozen=True, slots=True)
class ResumePdfPreviewPage:
    page_number: int
    width_pixels: int
    height_pixels: int
    content: bytes
    content_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "width_pixels": self.width_pixels,
            "height_pixels": self.height_pixels,
            "media_type": "image/png",
            "byte_count": len(self.content),
            "content_digest": self.content_digest,
            "content_base64": base64.b64encode(self.content).decode("ascii"),
        }


@dataclass(frozen=True, slots=True)
class ResumePdfPreview:
    projection_id: str
    artifact_digest: str
    preview_document_digest: str
    pdf_content_digest: str
    pdf_byte_count: int
    pages: tuple[ResumePdfPreviewPage, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "pdf_preview_version": PDF_PREVIEW_VERSION,
            "projection_id": self.projection_id,
            "artifact_digest": self.artifact_digest,
            "preview_document_digest": self.preview_document_digest,
            "pdf_content_digest": self.pdf_content_digest,
            "pdf_byte_count": self.pdf_byte_count,
            "renderer": f"pypdfium2-{PDFIUM_VERSION}-scale-{PDF_PREVIEW_SCALE}",
            "page_count": len(self.pages),
            "pages": [page.to_dict() for page in self.pages],
            "action_capability": "none",
        }


def render_pdf_preview(exported: ResumeNativeExport) -> ResumePdfPreview:
    """Rasterize only an already audited native PDF export."""

    if (
        not isinstance(exported, ResumeNativeExport)
        or exported.format != "pdf"
        or exported.media_type != "application/pdf"
        or exported.extension != "pdf"
        or not hmac.compare_digest(
            hashlib.sha256(exported.content).hexdigest(), exported.content_digest
        )
    ):
        raise NativeResumeExportError("PDF preview binding is invalid")
    if not hmac.compare_digest(version("pypdfium2"), PDFIUM_VERSION):
        raise NativeResumeExportError("PDF preview renderer version differs")

    try:
        document = pdfium.PdfDocument(exported.content)
    except Exception as exc:
        raise NativeResumeExportError("PDF preview source could not be opened") from exc
    pages: list[ResumePdfPreviewPage] = []
    total_bytes = 0
    try:
        if not 1 <= len(document) <= MAX_PREVIEW_PAGES:
            raise NativeResumeExportError("PDF preview page count is invalid")
        for index in range(len(document)):
            page = document[index]
            try:
                bitmap = page.render(scale=PDF_PREVIEW_SCALE, rotation=0)
                try:
                    image = bitmap.to_pil()
                    try:
                        content, width, height = _encode_png(image)
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
            total_bytes += len(content)
            if (
                not 1 <= width <= MAX_PREVIEW_DIMENSION
                or not 1 <= height <= MAX_PREVIEW_DIMENSION
                or not 1 <= len(content) <= MAX_PREVIEW_PAGE_BYTES
                or total_bytes > MAX_PREVIEW_TOTAL_BYTES
                or not content.startswith(_PNG_SIGNATURE)
                or not content.endswith(_PNG_END)
            ):
                raise NativeResumeExportError("PDF preview page is invalid")
            pages.append(
                ResumePdfPreviewPage(
                    page_number=index + 1,
                    width_pixels=width,
                    height_pixels=height,
                    content=content,
                    content_digest=hashlib.sha256(content).hexdigest(),
                )
            )
    except NativeResumeExportError:
        raise
    except Exception as exc:
        raise NativeResumeExportError("PDF preview could not be rendered") from exc
    finally:
        document.close()
    return ResumePdfPreview(
        projection_id=exported.projection_id,
        artifact_digest=exported.artifact_digest,
        preview_document_digest=exported.preview_document_digest,
        pdf_content_digest=exported.content_digest,
        pdf_byte_count=len(exported.content),
        pages=tuple(pages),
    )


def _encode_png(image: Image.Image) -> tuple[bytes, int, int]:
    normalized = image if image.mode == "RGB" else image.convert("RGB")
    try:
        width, height = normalized.size
        output = io.BytesIO()
        normalized.save(output, format="PNG", optimize=False, compress_level=9)
        return output.getvalue(), width, height
    finally:
        if normalized is not image:
            normalized.close()
