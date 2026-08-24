"""Fail-closed, deterministic PDF export from the reviewed semantic tree."""

from __future__ import annotations

import hashlib
import hmac
import io
import math
import os
import re
import stat
import threading
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
import pypdf
import reportlab
import uharfbuzz
from pypdf import PdfReader
from pypdf.generic import ContentStream
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfgen.textobject import bidiShapedText

from .native_export import NativeResumeExportError, PdfExportUnavailable, ResumeNativeExport
from .render import ResumeDocumentTree, render_preview_tree

REPORTLAB_VERSION = "5.0.0"
UHARFBUZZ_VERSION = "0.56.0"
PYPDF_VERSION = "6.15.0"
PDF_ENGINE_VERSION = f"ReportLab {REPORTLAB_VERSION} + uharfbuzz {UHARFBUZZ_VERSION}"
FONT_SOURCE_COMMIT = "2d85e20401920891efb7cd6272d6339685df2820"
FONT_PINS = {
    "base": {
        "filename": "NotoSans-Regular.ttf",
        "size_bytes": 626_220,
        "sha256": "82d154854e9c223e853625ae9faf7302fdccc02f6bacfd429d6ff311da93d011",
    },
    "cjk": {
        "filename": "NotoSansSC-Regular.ttf",
        "size_bytes": 10_595_948,
        "sha256": "56b7235feaf4eeeccaa59289ee95d965ed07fb3b2297251981e5e05b4e21ed27",
    },
    "arabic": {
        "filename": "NotoSansArabic-Regular.ttf",
        "size_bytes": 194_336,
        "sha256": "1a4e20719f69a8b17b80a82d732f1f45f88b662b4213817554b06499eef00c68",
    },
}
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 20
PDF_RENDER_TIMEOUT_SECONDS = 30
_HASH_CHUNK_BYTES = 4 * 1024 * 1024
_A4_WIDTH_POINTS = 595.276
_A4_HEIGHT_POINTS = 841.89
_A4_TOLERANCE_POINTS = 2.0
_LEFT_MARGIN = 17 * mm
_RIGHT_MARGIN = 17 * mm
_TOP_MARGIN = 16 * mm
_BOTTOM_MARGIN = 16 * mm
_NAME_SIZE = 25.0
_NAME_LEADING = 29.0
_SECTION_SIZE = 11.0
_SECTION_LEADING = 14.0
_BODY_SIZE = 10.5
_BODY_LEADING = 14.9
_ITEM_GAP = 2.2 * mm
_SECTION_GAP = 6 * mm
_SECTION_AFTER = 2.5 * mm
_BULLET_INDENT = 5 * mm
_BASE_FONT_NAME = "OpenChronicleNotoSans400"
_CJK_FONT_NAME = "OpenChronicleNotoSansSC400"
_ARABIC_FONT_NAME = "OpenChronicleNotoSansArabic400"
_RTL_LOCALES = {"ar", "fa", "he", "ur"}
_FORBIDDEN_PDF_TOKENS = (
    b"/AcroForm",
    b"/EmbeddedFile",
    b"/JavaScript",
    b"/JS ",
    b"/Launch",
    b"/OpenAction",
    b"/RichMedia",
)
_ARABIC_RANGES = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x0870, 0x089F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)
_FONT_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class _FontPaths:
    base: Path
    cjk: Path
    arabic: Path


@dataclass(frozen=True, slots=True)
class _TextRun:
    text: str
    font_name: str
    direction: str
    shaping: bool


class _InvariantCanvas(Canvas):
    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.update(invariant=1, pageCompression=1)
        super().__init__(*args, **kwargs)


def render_pdf_export(
    tree: ResumeDocumentTree,
    *,
    preview_document_digest: str,
    font_directory: Path | None = None,
) -> ResumeNativeExport:
    """Render only reviewed tree bytes with a pinned, bundled, offline engine."""

    if (
        not isinstance(tree, ResumeDocumentTree)
        or re.fullmatch(r"[0-9a-f]{64}", preview_document_digest) is None
    ):
        raise NativeResumeExportError("native export binding is invalid")
    preview = render_preview_tree(tree)
    if not hmac.compare_digest(preview.document_digest, preview_document_digest):
        raise NativeResumeExportError("native export preview binding changed")

    started = time.monotonic()
    try:
        _verify_runtime_versions()
        fonts = _resolve_font_assets(font_directory)
        _register_fonts(fonts)
        content = _render_pdf(tree)
        repeated = _render_pdf(tree)
        if not hmac.compare_digest(content, repeated):
            raise NativeResumeExportError("PDF output is not byte deterministic")
        if time.monotonic() - started > PDF_RENDER_TIMEOUT_SECONDS:
            raise NativeResumeExportError("PDF rendering exceeded the bounded duration")
        _validate_pdf(content, tree=tree)
    except Exception as exc:
        raise PdfExportUnavailable("bundled PDF export is unavailable") from exc

    return ResumeNativeExport(
        projection_id=tree.projection_id,
        artifact_digest=tree.artifact_digest,
        preview_document_digest=preview_document_digest,
        format="pdf",
        media_type="application/pdf",
        extension="pdf",
        content=content,
        content_digest=hashlib.sha256(content).hexdigest(),
    )


def _verify_runtime_versions() -> None:
    versions = (
        (str(reportlab.Version), REPORTLAB_VERSION),
        (str(uharfbuzz.__version__), UHARFBUZZ_VERSION),
        (str(pypdf.__version__), PYPDF_VERSION),
    )
    if any(not hmac.compare_digest(observed, expected) for observed, expected in versions):
        raise NativeResumeExportError("PDF dependency version differs from the accepted pin")


def _resolve_font_assets(font_directory: Path | None = None) -> _FontPaths:
    directory = font_directory or Path(__file__).resolve().parents[1] / "assets" / "pdf_fonts"
    try:
        directory_metadata = directory.lstat()
    except OSError as exc:
        raise NativeResumeExportError("bundled PDF fonts are unavailable") from exc
    if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(directory_metadata.st_mode):
        raise NativeResumeExportError("bundled PDF font directory is invalid")

    resolved: dict[str, Path] = {}
    for identifier, pin in FONT_PINS.items():
        path = directory / str(pin["filename"])
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise NativeResumeExportError("bundled PDF font is unavailable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != pin["size_bytes"]
            or not hmac.compare_digest(
                _file_sha256(path, expected_metadata=metadata), str(pin["sha256"])
            )
        ):
            raise NativeResumeExportError("bundled PDF font differs from the accepted pin")
        resolved[identifier] = path
    return _FontPaths(base=resolved["base"], cjk=resolved["cjk"], arabic=resolved["arabic"])


def _register_fonts(fonts: _FontPaths) -> None:
    with _FONT_LOCK:
        try:
            pdfmetrics.registerFont(TTFont(_BASE_FONT_NAME, fonts.base, validate=1, shapable=False))
            pdfmetrics.registerFont(TTFont(_CJK_FONT_NAME, fonts.cjk, validate=1, shapable=False))
            pdfmetrics.registerFont(
                TTFont(_ARABIC_FONT_NAME, fonts.arabic, validate=1, shapable=True)
            )
        except Exception as exc:
            raise NativeResumeExportError("bundled PDF font could not be registered") from exc


def _render_pdf(tree: ResumeDocumentTree) -> bytes:
    stream = io.BytesIO()
    canvas = _InvariantCanvas(
        stream,
        pagesize=A4,
        title="OpenChronicle Resume",
        author="OpenChronicle",
        creator=PDF_ENGINE_VERSION,
        subject="Reviewed resume projection",
    )
    canvas.setFillColor(colors.HexColor("#17201d"))
    width, height = A4
    usable_width = width - _LEFT_MARGIN - _RIGHT_MARGIN
    page_top = height - _TOP_MARGIN
    y = page_top
    prefer_rtl = _locale_is_rtl(tree.locale)

    def new_page() -> float:
        canvas.showPage()
        canvas.setFillColor(colors.HexColor("#17201d"))
        return page_top

    name_lines = _wrap_text(
        tree.display_name,
        maximum_width=usable_width,
        font_size=_NAME_SIZE,
        prefer_rtl=prefer_rtl,
    )
    for line in name_lines:
        y -= _NAME_SIZE
        _draw_line(
            canvas,
            line,
            left=_LEFT_MARGIN,
            right=width - _RIGHT_MARGIN,
            y=y,
            font_size=_NAME_SIZE,
            prefer_rtl=prefer_rtl,
        )
        y -= _NAME_LEADING - _NAME_SIZE
    y -= 5 * mm
    canvas.setStrokeColor(colors.HexColor("#176b5c"))
    canvas.setLineWidth(0.6 * mm)
    canvas.line(_LEFT_MARGIN, y, width - _RIGHT_MARGIN, y)

    for section in tree.sections:
        heading_lines = _wrap_text(
            section.label,
            maximum_width=usable_width,
            font_size=_SECTION_SIZE,
            prefer_rtl=prefer_rtl,
        )
        first_item_lines = (
            _wrap_text(
                section.items[0].text,
                maximum_width=usable_width - _BULLET_INDENT,
                font_size=_BODY_SIZE,
                prefer_rtl=prefer_rtl,
            )
            if section.items
            else []
        )
        minimum_block = (
            _SECTION_GAP
            + len(heading_lines) * _SECTION_LEADING
            + _SECTION_AFTER
            + min(1, len(first_item_lines)) * _BODY_LEADING
        )
        if y - minimum_block < _BOTTOM_MARGIN:
            y = new_page()
        y -= _SECTION_GAP
        canvas.setFillColor(colors.HexColor("#176b5c"))
        for line in heading_lines:
            y -= _SECTION_SIZE
            _draw_line(
                canvas,
                line,
                left=_LEFT_MARGIN,
                right=width - _RIGHT_MARGIN,
                y=y,
                font_size=_SECTION_SIZE,
                prefer_rtl=prefer_rtl,
            )
            y -= _SECTION_LEADING - _SECTION_SIZE
        y -= _SECTION_AFTER
        canvas.setFillColor(colors.HexColor("#17201d"))

        for item in section.items:
            lines = _wrap_text(
                item.text,
                maximum_width=usable_width - _BULLET_INDENT,
                font_size=_BODY_SIZE,
                prefer_rtl=prefer_rtl,
            )
            item_height = len(lines) * _BODY_LEADING + _ITEM_GAP
            full_page_height = page_top - _BOTTOM_MARGIN
            if item_height <= full_page_height and y - item_height < _BOTTOM_MARGIN:
                y = new_page()
            for index, line in enumerate(lines):
                if y - _BODY_LEADING < _BOTTOM_MARGIN:
                    y = new_page()
                y -= _BODY_SIZE
                if index == 0:
                    bullet_x = width - _RIGHT_MARGIN if prefer_rtl else _LEFT_MARGIN
                    _draw_line(
                        canvas,
                        "-",
                        left=bullet_x - _BULLET_INDENT if prefer_rtl else bullet_x,
                        right=bullet_x if prefer_rtl else bullet_x + _BULLET_INDENT,
                        y=y,
                        font_size=_BODY_SIZE,
                        prefer_rtl=prefer_rtl,
                    )
                _draw_line(
                    canvas,
                    line,
                    left=_LEFT_MARGIN + (0 if prefer_rtl else _BULLET_INDENT),
                    right=width - _RIGHT_MARGIN - (_BULLET_INDENT if prefer_rtl else 0),
                    y=y,
                    font_size=_BODY_SIZE,
                    prefer_rtl=prefer_rtl,
                )
                y -= _BODY_LEADING - _BODY_SIZE
            y -= _ITEM_GAP

    canvas.save()
    return stream.getvalue()


def _wrap_text(
    value: str,
    *,
    maximum_width: float,
    font_size: float,
    prefer_rtl: bool,
) -> list[str]:
    lines: list[str] = []
    for paragraph in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        pending_space = False
        for token in re.findall(r"\S+|\s+", paragraph):
            if token.isspace():
                pending_space = bool(current)
                continue
            separator = " " if pending_space and current else ""
            candidate = current + separator + token
            if (
                _measure_text(candidate, font_size=font_size, prefer_rtl=prefer_rtl)
                <= maximum_width
            ):
                current = candidate
                pending_space = False
                continue
            if current:
                lines.append(current)
                current = ""
            pending_space = False
            if _measure_text(token, font_size=font_size, prefer_rtl=prefer_rtl) <= maximum_width:
                current = token
                continue
            for cluster in _text_clusters(token):
                candidate = current + cluster
                if (
                    current
                    and _measure_text(candidate, font_size=font_size, prefer_rtl=prefer_rtl)
                    > maximum_width
                ):
                    lines.append(current)
                    current = cluster
                else:
                    current = candidate
        if current:
            lines.append(current)
    if not lines:
        raise NativeResumeExportError("PDF text cannot be empty")
    return lines


def _text_clusters(value: str) -> list[str]:
    clusters: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if clusters and (
            category in {"Mn", "Mc", "Me"}
            or character in {"\u200c", "\u200d"}
            or 0xFE00 <= ord(character) <= 0xFE0F
            or clusters[-1].endswith("\u200d")
        ):
            clusters[-1] += character
        else:
            clusters.append(character)
    return clusters


def _measure_text(value: str, *, font_size: float, prefer_rtl: bool) -> float:
    return sum(_run_width(run, font_size) for run in _text_runs(value, prefer_rtl=prefer_rtl))


def _draw_line(
    canvas: Canvas,
    value: str,
    *,
    left: float,
    right: float,
    y: float,
    font_size: float,
    prefer_rtl: bool,
) -> None:
    if not value:
        return
    runs = _text_runs(value, prefer_rtl=prefer_rtl)
    widths = [_run_width(run, font_size) for run in runs]
    x = right - sum(widths) if prefer_rtl else left
    for run, run_width in zip(runs, widths, strict=True):
        _ensure_font_coverage(run)
        canvas.setFont(run.font_name, font_size)
        canvas.drawString(
            x,
            y,
            run.text,
            direction=run.direction,
            shaping=run.shaping,
        )
        x += run_width


def _text_runs(value: str, *, prefer_rtl: bool) -> list[_TextRun]:
    if _contains_arabic(value) and _font_supports(value, _ARABIC_FONT_NAME):
        return [
            _TextRun(
                text=value,
                font_name=_ARABIC_FONT_NAME,
                direction="RTL" if prefer_rtl else "LTR",
                shaping=True,
            )
        ]

    strong = [_strong_script(character) for character in value]
    scripts: list[str] = []
    for index, script in enumerate(strong):
        if script is not None:
            scripts.append(script)
            continue
        previous = next((item for item in reversed(strong[:index]) if item is not None), None)
        following = next((item for item in strong[index + 1 :] if item is not None), None)
        scripts.append("arabic" if previous == "arabic" and following != "base" else "base")

    for index, character in enumerate(value):
        if (
            scripts[index] == "base"
            and not _font_supports(character, _BASE_FONT_NAME)
            and _font_supports(character, _CJK_FONT_NAME)
        ):
            scripts[index] = "cjk"
        elif (
            scripts[index] == "base"
            and not _font_supports(character, _BASE_FONT_NAME)
            and _font_supports(character, _ARABIC_FONT_NAME)
        ):
            scripts[index] = "fallback"

    runs: list[_TextRun] = []
    start = 0
    for index in range(1, len(value) + 1):
        if index < len(value) and scripts[index] == scripts[start]:
            continue
        text = value[start:index]
        arabic = scripts[start] == "arabic"
        fallback = scripts[start] == "fallback"
        cjk = scripts[start] == "cjk"
        runs.append(
            _TextRun(
                text=text,
                font_name=(
                    _ARABIC_FONT_NAME
                    if arabic or fallback
                    else _CJK_FONT_NAME
                    if cjk
                    else _BASE_FONT_NAME
                ),
                direction="RTL" if arabic else "LTR",
                shaping=arabic,
            )
        )
        start = index
    return runs


def _strong_script(character: str) -> str | None:
    if _is_arabic(character):
        return "arabic"
    category = unicodedata.category(character)
    if category[0] in {"L", "N", "S"}:
        return "base"
    return None


def _is_arabic(character: str) -> bool:
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _ARABIC_RANGES)


def _contains_arabic(value: str) -> bool:
    return any(_is_arabic(character) for character in value)


def _font_supports(value: str, font_name: str) -> bool:
    mapping = pdfmetrics.getFont(font_name).face.charToGlyph
    return all(
        character.isspace()
        or unicodedata.category(character) == "Cf"
        or mapping.get(ord(character), 0) != 0
        for character in value
    )


def _ensure_font_coverage(run: _TextRun) -> None:
    if not _font_supports(run.text, run.font_name):
        raise NativeResumeExportError("PDF font coverage is incomplete")
    if any(
        unicodedata.category(character) == "Cc" and character not in {"\t", "\n", "\r"}
        for character in run.text
    ):
        raise NativeResumeExportError("PDF text contains unsupported control characters")


def _run_width(run: _TextRun, font_size: float) -> float:
    _ensure_font_coverage(run)
    if run.shaping:
        _shaped, width = bidiShapedText(
            run.text,
            run.direction,
            fontName=run.font_name,
            fontSize=font_size,
            shaping=True,
        )
        return float(width)
    return float(pdfmetrics.stringWidth(run.text, run.font_name, font_size))


def _locale_is_rtl(locale: str) -> bool:
    return locale.split("-", 1)[0].split("_", 1)[0].casefold() in _RTL_LOCALES


def _validate_pdf(content: bytes, *, tree: ResumeDocumentTree) -> None:
    if (
        not content.startswith(b"%PDF-")
        or not content.rstrip().endswith(b"%%EOF")
        or not 1 <= len(content) <= MAX_PDF_BYTES
        or any(token in content for token in _FORBIDDEN_PDF_TOKENS)
    ):
        raise NativeResumeExportError("PDF output structure is invalid")
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
            raise NativeResumeExportError("PDF document boundary is invalid")
        root = reader.trailer["/Root"]
        if any(key in root for key in ("/AcroForm", "/OpenAction", "/Names", "/AA")):
            raise NativeResumeExportError("PDF catalog contains active content")
        for page in reader.pages:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            if not (
                math.isclose(width, _A4_WIDTH_POINTS, abs_tol=_A4_TOLERANCE_POINTS)
                and math.isclose(height, _A4_HEIGHT_POINTS, abs_tol=_A4_TOLERANCE_POINTS)
            ):
                raise NativeResumeExportError("PDF page geometry is invalid")
            if any(key in page for key in ("/Annots", "/AA")):
                raise NativeResumeExportError("PDF page contains active content")
            _validate_embedded_page_fonts(reader, page)

        with pdfplumber.open(io.BytesIO(content)) as document:
            extracted = "\n".join(page.extract_text() or "" for page in document.pages)
            ordered_segments = [tree.display_name]
            for section in tree.sections:
                ordered_segments.append(section.label)
                ordered_segments.extend(item.text for item in section.items)
            if not _ordered_content_found(extracted, ordered_segments):
                raise NativeResumeExportError("PDF ordered text differs from the semantic tree")
    except NativeResumeExportError:
        raise
    except Exception as exc:
        raise NativeResumeExportError("PDF output could not be parsed") from exc


def _validate_embedded_page_fonts(reader: PdfReader, page: object) -> None:
    content = ContentStream(page.get_contents(), reader)
    selected_font: object | None = None
    used_fonts: set[str] = set()
    for operands, operator in content.operations:
        if operator == b"Tf" and operands:
            selected_font = operands[0]
        elif operator in {b"Tj", b"TJ", b"'", b'"'} and selected_font is not None:
            used_fonts.add(str(selected_font))
    if not used_fonts:
        raise NativeResumeExportError("PDF page has no text fonts")

    resources = page["/Resources"].get_object()
    font_resources = resources.get("/Font")
    if font_resources is None:
        raise NativeResumeExportError("PDF font resources are absent")
    fonts = font_resources.get_object()
    for name in used_fonts:
        reference = fonts.get(name)
        if reference is None:
            raise NativeResumeExportError("PDF used font resource is absent")
        font = reference.get_object()
        descriptor_reference = font.get("/FontDescriptor")
        descriptor = descriptor_reference.get_object() if descriptor_reference is not None else None
        if (
            font.get("/ToUnicode") is None
            or descriptor is None
            or not any(
                descriptor.get(key) is not None for key in ("/FontFile", "/FontFile2", "/FontFile3")
            )
        ):
            raise NativeResumeExportError("PDF used font is not embedded with Unicode mapping")


def _ordered_content_found(extracted: str, segments: Sequence[str]) -> bool:
    haystack = _compact(extracted)
    cursor = 0
    for segment in segments:
        needle = _compact(segment)
        found = haystack.find(needle, cursor)
        if not needle or found < 0:
            return False
        cursor = found + len(needle)
    return True


def _compact(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and unicodedata.category(character) != "Cf"
    )


def _file_sha256(path: Path, *, expected_metadata: os.stat_result) -> str:
    digest = hashlib.sha256()
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_dev != expected_metadata.st_dev
            or opened_metadata.st_ino != expected_metadata.st_ino
            or opened_metadata.st_size != expected_metadata.st_size
        ):
            raise NativeResumeExportError("PDF font changed during verification")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
    except NativeResumeExportError:
        raise
    except OSError as exc:
        raise NativeResumeExportError("PDF font digest is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest()
