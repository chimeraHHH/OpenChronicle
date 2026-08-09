"""Fail-closed PDF export through the exact audited development engine."""

from __future__ import annotations

import hashlib
import hmac
import io
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path

import pdfplumber

from .native_export import NativeResumeExportError, ResumeNativeExport
from .pinned_pdf import PinnedPdfProcessError, print_pdf
from .render import ResumeDocumentTree, render_preview_tree

DEFAULT_CHROME_MACOS = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PDF_ENGINE_VERSION = "Google Chrome 151.0.7922.77"
PDF_ENGINE_SHA256 = "696e4277196f9bc4d6655285f617efb068a84d4c7fc498948f10d8a4e03f9663"
POPPLER_VERSIONS = {
    "pdfinfo": "pdfinfo version 26.04.0",
    "pdffonts": "pdffonts version 26.04.0",
    "pdftotext": "pdftotext version 26.04.0",
}
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 20
PDF_RENDER_TIMEOUT_SECONDS = 30
_COMMAND_OUTPUT_LIMIT = 1024 * 1024
_HASH_CHUNK_BYTES = 4 * 1024 * 1024
_A4_WIDTH_POINTS = 595.276
_A4_HEIGHT_POINTS = 841.89
_A4_TOLERANCE_POINTS = 2.0
_PAGE_COUNT = re.compile(r"^Pages:\s+([0-9]+)$", re.MULTILINE)
_PAGE_SIZE = re.compile(
    r"^(?:Page size:|Page\s+\d+\s+size:)\s+([0-9.]+) x ([0-9.]+) pts",
    re.MULTILINE,
)
_FORBIDDEN_PDF_TOKENS = (
    b"/AcroForm",
    b"/EmbeddedFile",
    b"/JavaScript",
    b"/JS ",
    b"/Launch",
    b"/OpenAction",
    b"/RichMedia",
)


def render_pdf_export(
    tree: ResumeDocumentTree,
    *,
    preview_document_digest: str,
    chrome_path: Path | None = None,
) -> ResumeNativeExport:
    """Render the reviewed tree only when every development-engine pin matches."""

    if not isinstance(tree, ResumeDocumentTree) or re.fullmatch(
        r"[0-9a-f]{64}", preview_document_digest
    ) is None:
        raise NativeResumeExportError("native export binding is invalid")
    preview = render_preview_tree(tree)
    if not hmac.compare_digest(preview.document_digest, preview_document_digest):
        raise NativeResumeExportError("native export preview binding changed")
    chrome = _resolve_pinned_chrome(chrome_path)
    commands = {name: _resolve_pinned_command(name) for name in POPPLER_VERSIONS}

    with tempfile.TemporaryDirectory(prefix="openchronicle-native-pdf-") as raw_directory:
        directory = Path(raw_directory)
        directory.chmod(0o700)
        html_path = directory / "preview.html"
        pdf_path = directory / "resume.pdf"
        html_path.write_text(preview.html, encoding="utf-8")
        html_path.chmod(0o600)
        try:
            print_pdf(chrome, html_path, pdf_path, timeout=PDF_RENDER_TIMEOUT_SECONDS)
        except PinnedPdfProcessError as exc:
            raise NativeResumeExportError("pinned PDF renderer failed") from exc
        if not pdf_path.is_file() or pdf_path.is_symlink():
            raise NativeResumeExportError("pinned PDF renderer output is invalid")
        pdf_path.chmod(0o600)
        content = pdf_path.read_bytes()
        _validate_pdf(
            content,
            tree=tree,
            pdf_path=pdf_path,
            commands=commands,
        )

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


def _resolve_pinned_chrome(chrome_path: Path | None) -> Path:
    raw_candidate = (chrome_path or DEFAULT_CHROME_MACOS).expanduser()
    try:
        metadata = raw_candidate.lstat()
    except OSError as exc:
        raise NativeResumeExportError("pinned PDF engine is unavailable") from exc
    if raw_candidate.is_symlink():
        raise NativeResumeExportError("pinned PDF engine is unavailable")
    candidate = raw_candidate.resolve()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise NativeResumeExportError("pinned PDF engine is unavailable")
    if _command_version([str(candidate), "--version"]) != PDF_ENGINE_VERSION:
        raise NativeResumeExportError("PDF engine version differs from the accepted pin")
    if metadata.st_size < 1 or not hmac.compare_digest(_file_sha256(candidate), PDF_ENGINE_SHA256):
        raise NativeResumeExportError("PDF engine digest differs from the accepted pin")
    return candidate


def _resolve_pinned_command(name: str) -> str:
    command = shutil.which(name)
    if command is None or _command_version([command, "-v"]) != POPPLER_VERSIONS[name]:
        raise NativeResumeExportError(f"pinned {name} inspector is unavailable")
    return command


def _validate_pdf(
    content: bytes,
    *,
    tree: ResumeDocumentTree,
    pdf_path: Path,
    commands: Mapping[str, str],
) -> None:
    if (
        not content.startswith(b"%PDF-")
        or not content.rstrip().endswith(b"%%EOF")
        or not 1 <= len(content) <= MAX_PDF_BYTES
        or any(token in content for token in _FORBIDDEN_PDF_TOKENS)
    ):
        raise NativeResumeExportError("PDF output structure is invalid")
    try:
        with pdfplumber.open(io.BytesIO(content)) as document:
            page_count = len(document.pages)
            if not 1 <= page_count <= MAX_PDF_PAGES:
                raise NativeResumeExportError("PDF page count is invalid")
            for page in document.pages:
                if not (
                    math.isclose(float(page.width), _A4_WIDTH_POINTS, abs_tol=_A4_TOLERANCE_POINTS)
                    and math.isclose(
                        float(page.height), _A4_HEIGHT_POINTS, abs_tol=_A4_TOLERANCE_POINTS
                    )
                ):
                    raise NativeResumeExportError("PDF page geometry is invalid")
    except NativeResumeExportError:
        raise
    except Exception as exc:
        raise NativeResumeExportError("PDF output could not be parsed") from exc

    info = _run_capture(
        [commands["pdfinfo"], "-f", "1", "-l", str(page_count), str(pdf_path)]
    )
    count_match = _PAGE_COUNT.search(info)
    sizes = _PAGE_SIZE.findall(info)
    if (
        count_match is None
        or int(count_match.group(1)) != page_count
        or len(sizes) != page_count
        or any(
            not math.isclose(float(width), _A4_WIDTH_POINTS, abs_tol=_A4_TOLERANCE_POINTS)
            or not math.isclose(float(height), _A4_HEIGHT_POINTS, abs_tol=_A4_TOLERANCE_POINTS)
            for width, height in sizes
        )
        or not all(
            marker in info
            for marker in ("Encrypted:       no", "Form:            none", "JavaScript:      no")
        )
    ):
        raise NativeResumeExportError("PDF inspector rejected the output")

    extracted = _run_capture(
        [commands["pdftotext"], "-raw", "-enc", "UTF-8", str(pdf_path), "-"]
    )
    ordered_segments = [tree.display_name]
    for section in tree.sections:
        ordered_segments.append(section.label)
        ordered_segments.extend(item.text for item in section.items)
    if not _ordered_content_found(extracted, ordered_segments):
        raise NativeResumeExportError("PDF ordered text differs from the semantic tree")

    font_report = _run_capture([commands["pdffonts"], str(pdf_path)])
    if not _font_report_embedded(font_report):
        raise NativeResumeExportError("PDF fonts are not fully embedded")


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


def _font_report_embedded(value: str) -> bool:
    lines = value.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.startswith("---")) + 1
    except StopIteration:
        return False
    fonts = [line.split() for line in lines[start:] if line.strip()]
    return bool(fonts) and all(len(parts) >= 8 and parts[-5] == "yes" for parts in fonts)


def _run_capture(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            timeout=PDF_RENDER_TIMEOUT_SECONDS,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeResumeExportError("PDF inspector failed") from exc
    output = result.stdout + result.stderr
    if result.returncode != 0 or len(output) > _COMMAND_OUTPUT_LIMIT:
        raise NativeResumeExportError("PDF inspector returned an invalid result")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NativeResumeExportError("PDF inspector output is invalid") from exc


def _command_version(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            timeout=PDF_RENDER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeResumeExportError("PDF dependency version is unavailable") from exc
    output = (result.stdout + result.stderr).decode("utf-8", errors="strict").strip()
    first_line = output.splitlines()[0] if output else ""
    if result.returncode != 0 or len(first_line) > 128:
        raise NativeResumeExportError("PDF dependency version is unavailable")
    return first_line


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        raise NativeResumeExportError("PDF engine digest is unavailable") from exc
    return digest.hexdigest()
