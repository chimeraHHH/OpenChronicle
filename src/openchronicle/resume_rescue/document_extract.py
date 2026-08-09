"""Bounded, review-only PDF and DOCX extraction for Résumé Rescue."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import os
import re
import signal
import subprocess
import sys
import zipfile
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

import pdfplumber
from defusedxml import ElementTree as DefusedElementTree
from pdfminer.pdfdocument import PDFPasswordIncorrect

from ..packaged_entry import DOCUMENT_WORKER, worker_command
from ..provenance.models import canonical_digest
from .models import (
    VALID_CONFIDENTIALITY,
    VALID_OWNERSHIP,
    VALID_SECTIONS,
    validate_profile,
)

EXTRACTOR_VERSION = 1
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_EXTRACTED_CHARS = 500_000
MAX_CANDIDATES = 2_000
MAX_CANDIDATE_CHARS = 8_000
MAX_DOCX_MEMBERS = 2_000
MAX_DOCX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_DOCX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 100
WORKER_TIMEOUT_SECONDS = 15
MAX_WORKER_OUTPUT_BYTES = 4 * 1024 * 1024

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_RELATIONSHIP_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPE_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"
_W = f"{{{_WORD_NAMESPACE}}}"
_REL = f"{{{_RELATIONSHIP_NAMESPACE}}}"
_CT = f"{{{_CONTENT_TYPE_NAMESPACE}}}"
_ACTIVE_DOCX_MARKERS = (
    "vbaproject",
    "word/activex/",
    "word/embeddings/",
    "word/oleobject",
)
_WARNING_CODES = {
    "docx_pagination_unavailable",
    "duplicate_candidate_text",
    "external_relationship_ignored",
    "no_extractable_text",
    "ocr_required",
    "reading_order_requires_review",
    "untrusted_document_text",
}


class DocumentExtractionError(ValueError):
    """A source, extraction review, or admission binding is invalid."""


@dataclass(frozen=True, slots=True)
class DocumentImportReview:
    source_id: str
    source_digest: str
    source_byte_count: int
    source_format: str
    extraction_method: str
    candidates: tuple[dict[str, Any], ...]
    omissions: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, str], ...]
    review_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "format": self.source_format,
            "extractor": {
                "version": EXTRACTOR_VERSION,
                "method": self.extraction_method,
            },
            "source": {
                "id": self.source_id,
                "digest": self.source_digest,
                "byte_count": self.source_byte_count,
            },
            "candidates": copy.deepcopy(list(self.candidates)),
            "omissions": copy.deepcopy(list(self.omissions)),
            "warnings": copy.deepcopy(list(self.warnings)),
            "action_capability": "none",
            "review_digest": self.review_digest,
        }


def extract_document(source: bytes, *, source_format: str) -> DocumentImportReview:
    """Extract exact, unadmitted review candidates from bounded source bytes."""

    if not isinstance(source, bytes) or not 1 <= len(source) <= MAX_SOURCE_BYTES:
        raise DocumentExtractionError("document source size is invalid")
    if source_format not in {"pdf", "docx"}:
        raise DocumentExtractionError("document format is unsupported")
    return _run_extraction_worker(source, source_format=source_format)


def _extract_document_in_process(source: bytes, *, source_format: str) -> DocumentImportReview:
    """Worker-only extraction entry point; callers use :func:`extract_document`."""

    if not isinstance(source, bytes) or not 1 <= len(source) <= MAX_SOURCE_BYTES:
        raise DocumentExtractionError("document source size is invalid")
    if source_format not in {"pdf", "docx"}:
        raise DocumentExtractionError("document format is unsupported")
    source_digest = hashlib.sha256(source).hexdigest()
    source_id = f"resume-document-{source_digest[:32]}"
    if source_format == "pdf":
        candidates, omissions, warnings, method = _extract_pdf(
            source, source_id=source_id, source_digest=source_digest
        )
    else:
        candidates, omissions, warnings, method = _extract_docx(
            source, source_id=source_id, source_digest=source_digest
        )
    _validate_extraction_totals(candidates)
    payload = _review_payload(
        source_id=source_id,
        source_digest=source_digest,
        source_byte_count=len(source),
        source_format=source_format,
        extraction_method=method,
        candidates=candidates,
        omissions=omissions,
        warnings=warnings,
    )
    review_digest = canonical_digest(
        {"schema": "resume-document-import-review-v1", "review": payload}
    )
    return DocumentImportReview(
        source_id=source_id,
        source_digest=source_digest,
        source_byte_count=len(source),
        source_format=source_format,
        extraction_method=method,
        candidates=tuple(copy.deepcopy(candidates)),
        omissions=tuple(copy.deepcopy(omissions)),
        warnings=tuple(copy.deepcopy(warnings)),
        review_digest=review_digest,
    )


def _run_extraction_worker(source: bytes, *, source_format: str) -> DocumentImportReview:
    request = json.dumps(
        {
            "schema_version": 1,
            "format": source_format,
            "source_base64": base64.b64encode(source).decode("ascii"),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    process = subprocess.Popen(
        _document_worker_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_worker_environment(),
        start_new_session=os.name == "posix",
    )
    try:
        try:
            stdout, _ = process.communicate(input=request, timeout=WORKER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            _terminate_worker_process(process)
            raise DocumentExtractionError("document extraction timed out") from exc
        _terminate_residual_worker_group(process)
        if process.returncode != 0:
            raise DocumentExtractionError("document extraction worker failed")
        if not 1 <= len(stdout) <= MAX_WORKER_OUTPUT_BYTES:
            raise DocumentExtractionError("document extraction worker output size is invalid")
        response = _strict_json_object(stdout)
        if response.get("schema_version") != 1 or response.get("ok") is not True:
            if set(response) == {"schema_version", "ok", "error"}:
                error = response.get("error")
                if isinstance(error, str) and 1 <= len(error) <= 256:
                    raise DocumentExtractionError(error)
            raise DocumentExtractionError("document extraction worker response is invalid")
        if set(response) != {"schema_version", "ok", "review"}:
            raise DocumentExtractionError("document extraction worker response is invalid")
        return _parse_worker_review(response["review"], source=source, source_format=source_format)
    finally:
        if process.poll() is None:
            _terminate_worker_process(process)


def _document_worker_command() -> list[str]:
    return list(
        worker_command(
            DOCUMENT_WORKER,
            [sys.executable, "-m", "openchronicle.resume_rescue.document_worker"],
        )
    )


def _worker_environment() -> dict[str, str]:
    environment = {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
    }
    if os.name == "nt" and "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return environment


def _terminate_worker_process(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        for worker_signal in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, worker_signal)
            except ProcessLookupError:
                break
    elif process.poll() is None:
        process.kill()
    try:
        process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def _terminate_residual_worker_group(process: subprocess.Popen[bytes]) -> None:
    if os.name != "posix":
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def _strict_json_object(source: bytes) -> dict[str, Any]:
    try:
        decoded = source.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DocumentExtractionError("document extraction worker response is invalid") from exc
    if not isinstance(value, dict):
        raise DocumentExtractionError("document extraction worker response is invalid")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate key")
        output[key] = value
    return output


def _parse_worker_review(
    value: object, *, source: bytes, source_format: str
) -> DocumentImportReview:
    expected_keys = {
        "schema_version",
        "format",
        "extractor",
        "source",
        "candidates",
        "omissions",
        "warnings",
        "action_capability",
        "review_digest",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise DocumentExtractionError("document extraction review is invalid")
    if (
        value.get("schema_version") != 1
        or value.get("format") != source_format
        or value.get("action_capability") != "none"
    ):
        raise DocumentExtractionError("document extraction review is invalid")
    extractor = value.get("extractor")
    if not isinstance(extractor, dict) or set(extractor) != {"version", "method"}:
        raise DocumentExtractionError("document extraction review is invalid")
    method = _bounded_string(extractor.get("method"), maximum=128)
    if extractor.get("version") != EXTRACTOR_VERSION:
        raise DocumentExtractionError("document extraction review is invalid")

    source_digest = hashlib.sha256(source).hexdigest()
    source_id = f"resume-document-{source_digest[:32]}"
    source_binding = value.get("source")
    if source_binding != {
        "id": source_id,
        "digest": source_digest,
        "byte_count": len(source),
    }:
        raise DocumentExtractionError("document extraction source binding is invalid")

    raw_candidates = value.get("candidates")
    if not isinstance(raw_candidates, list) or len(raw_candidates) > MAX_CANDIDATES:
        raise DocumentExtractionError("document extraction candidates are invalid")
    candidates: list[dict[str, Any]] = []
    for raw in raw_candidates:
        if not isinstance(raw, dict) or set(raw) != {
            "id",
            "text",
            "text_digest",
            "locator",
            "extraction_method",
            "candidate_digest",
        }:
            raise DocumentExtractionError("document extraction candidate is invalid")
        text = _bounded_string(raw.get("text"), maximum=MAX_CANDIDATE_CHARS)
        locator = _parse_worker_locator(raw.get("locator"), source_format=source_format)
        expected = _candidate(
            source_id=source_id,
            source_digest=source_digest,
            text=text,
            locator=locator,
            extraction_method=method,
        )
        if raw != expected:
            raise DocumentExtractionError("document extraction candidate binding is invalid")
        candidates.append(expected)
    _validate_extraction_totals(candidates)

    raw_omissions = value.get("omissions")
    if not isinstance(raw_omissions, list) or len(raw_omissions) > 100:
        raise DocumentExtractionError("document extraction omissions are invalid")
    omissions = [_parse_worker_omission(item) for item in raw_omissions]
    raw_warnings = value.get("warnings")
    if not isinstance(raw_warnings, list) or len(raw_warnings) > 20:
        raise DocumentExtractionError("document extraction warnings are invalid")
    warnings = [_parse_worker_warning(item) for item in raw_warnings]
    if len({item["code"] for item in warnings}) != len(warnings):
        raise DocumentExtractionError("document extraction warnings are duplicated")

    payload = _review_payload(
        source_id=source_id,
        source_digest=source_digest,
        source_byte_count=len(source),
        source_format=source_format,
        extraction_method=method,
        candidates=candidates,
        omissions=omissions,
        warnings=warnings,
    )
    review_digest = value.get("review_digest")
    expected_digest = canonical_digest(
        {"schema": "resume-document-import-review-v1", "review": payload}
    )
    if review_digest != expected_digest:
        raise DocumentExtractionError("document extraction review digest is invalid")
    return DocumentImportReview(
        source_id=source_id,
        source_digest=source_digest,
        source_byte_count=len(source),
        source_format=source_format,
        extraction_method=method,
        candidates=tuple(candidates),
        omissions=tuple(omissions),
        warnings=tuple(warnings),
        review_digest=expected_digest,
    )


def _parse_worker_locator(value: object, *, source_format: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DocumentExtractionError("document extraction locator is invalid")
    if source_format == "pdf":
        if set(value) != {"kind", "page", "section", "start", "end", "bbox"}:
            raise DocumentExtractionError("document extraction PDF locator is invalid")
        page = value.get("page")
        bbox = value.get("bbox")
        if (
            value.get("kind") != "page_bbox"
            or type(page) is not int
            or not 1 <= page <= MAX_PDF_PAGES
            or not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(
                type(item) in {int, float} and math.isfinite(float(item)) for item in bbox
            )
        ):
            raise DocumentExtractionError("document extraction PDF locator is invalid")
        normalized_bbox = [round(float(item), 3) for item in bbox]
        if (
            any(item < 0 or item > 100_000 for item in normalized_bbox)
            or normalized_bbox[0] > normalized_bbox[2]
            or normalized_bbox[1] > normalized_bbox[3]
        ):
            raise DocumentExtractionError("document extraction PDF locator is invalid")
        return {
            "kind": "page_bbox",
            "page": page,
            "section": _bounded_string(value.get("section"), maximum=512),
            **_parse_worker_span(value),
            "bbox": normalized_bbox,
        }
    if set(value) != {
        "kind",
        "page",
        "section",
        "start",
        "end",
        "part",
        "block",
        "block_kind",
    }:
        raise DocumentExtractionError("document extraction DOCX locator is invalid")
    part = _bounded_string(value.get("part"), maximum=256)
    block = value.get("block")
    block_kind = _bounded_string(value.get("block_kind"), maximum=128)
    if (
        value.get("kind") != "part_block"
        or value.get("page") != 0
        or re.fullmatch(r"word/(?:document|header\d+|footer\d+)\.xml", part) is None
        or type(block) is not int
        or not 0 <= block <= 100_000
        or (
            block_kind != "paragraph"
            and re.fullmatch(r"table_row_\d+", block_kind) is None
        )
    ):
        raise DocumentExtractionError("document extraction DOCX locator is invalid")
    return {
        "kind": "part_block",
        "page": 0,
        "section": _bounded_string(value.get("section"), maximum=512),
        **_parse_worker_span(value),
        "part": part,
        "block": block,
        "block_kind": block_kind,
    }


def _parse_worker_span(value: dict[str, Any]) -> dict[str, int]:
    start = value.get("start")
    end = value.get("end")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= 10_000_000
    ):
        raise DocumentExtractionError("document extraction span is invalid")
    return {"start": start, "end": end}


def _parse_worker_omission(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DocumentExtractionError("document extraction omission is invalid")
    if set(value) == {"code", "count"} and value.get("code") == "images_not_extracted":
        count = value.get("count")
        if type(count) is int and 1 <= count <= 100_000:
            return {"code": "images_not_extracted", "count": count}
    if (
        set(value) == {"code", "parts"}
        and value.get("code") == "supplementary_parts_not_extracted"
    ):
        parts = value.get("parts")
        allowed = {"word/comments.xml", "word/footnotes.xml", "word/endnotes.xml"}
        if (
            isinstance(parts, list)
            and 1 <= len(parts) <= len(allowed)
            and len(set(parts)) == len(parts)
            and all(isinstance(item, str) and item in allowed for item in parts)
        ):
            return {"code": "supplementary_parts_not_extracted", "parts": list(parts)}
    raise DocumentExtractionError("document extraction omission is invalid")


def _parse_worker_warning(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"code", "message"}:
        raise DocumentExtractionError("document extraction warning is invalid")
    code = value.get("code")
    if not isinstance(code, str) or code not in _WARNING_CODES:
        raise DocumentExtractionError("document extraction warning is invalid")
    return {"code": code, "message": _bounded_string(value.get("message"), maximum=512)}


def _bounded_string(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise DocumentExtractionError("document extraction text is invalid")
    return value


def admit_document_candidates(
    review: DocumentImportReview,
    source: bytes,
    selections: list[dict[str, Any]],
    *,
    reviewed_at: str,
) -> list[dict[str, Any]]:
    """Turn explicitly selected document candidates into profile facts."""

    if not isinstance(review, DocumentImportReview):
        raise DocumentExtractionError("document review is invalid")
    expected_review_digest = canonical_digest(
        {
            "schema": "resume-document-import-review-v1",
            "review": _review_payload(
                source_id=review.source_id,
                source_digest=review.source_digest,
                source_byte_count=review.source_byte_count,
                source_format=review.source_format,
                extraction_method=review.extraction_method,
                candidates=list(review.candidates),
                omissions=list(review.omissions),
                warnings=list(review.warnings),
            ),
        }
    )
    if expected_review_digest != review.review_digest:
        raise DocumentExtractionError("document review changed since extraction")
    fresh_review = extract_document(source, source_format=review.source_format)
    if fresh_review.review_digest != review.review_digest:
        raise DocumentExtractionError("document source binding differs from the review")
    _reviewed_timestamp(reviewed_at)
    if not isinstance(selections, list) or len(selections) > MAX_CANDIDATES:
        raise DocumentExtractionError("document selections must be a bounded list")

    candidates = {item["id"]: item for item in review.candidates}
    seen_candidates: set[str] = set()
    seen_facts: set[str] = set()
    facts: list[dict[str, Any]] = []
    for selection in selections:
        expected = {
            "candidate_id",
            "fact_id",
            "section",
            "confidentiality",
            "ownership_scope",
        }
        if not isinstance(selection, dict) or set(selection) != expected:
            raise DocumentExtractionError("document selection must use the closed schema")
        candidate_id = selection.get("candidate_id")
        fact_id = selection.get("fact_id")
        section = selection.get("section")
        confidentiality = selection.get("confidentiality")
        ownership = selection.get("ownership_scope")
        if (
            not isinstance(candidate_id, str)
            or candidate_id not in candidates
            or candidate_id in seen_candidates
            or not isinstance(fact_id, str)
            or _ID.fullmatch(fact_id) is None
            or fact_id in seen_facts
            or section not in VALID_SECTIONS
            or confidentiality not in VALID_CONFIDENTIALITY
            or ownership not in VALID_OWNERSHIP
        ):
            raise DocumentExtractionError("document selection binding is invalid")
        candidate = candidates[candidate_id]
        locator = candidate["locator"]
        facts.append(
            {
                "id": fact_id,
                "section": section,
                "text": candidate["text"],
                "confidentiality": confidentiality,
                "ownership_scope": ownership,
                "provenance": [
                    {
                        "kind": "document_excerpt",
                        "reviewed_at": reviewed_at,
                        "source_id": review.source_id,
                        "source_digest": review.source_digest,
                        "page": locator["page"],
                        "section": locator["section"],
                        "start": locator["start"],
                        "end": locator["end"],
                        "extraction_method": review.extraction_method,
                    }
                ],
            }
        )
        seen_candidates.add(candidate_id)
        seen_facts.add(fact_id)
    validate_profile(
        {
            "schema_version": 1,
            "profile_id": "document-admission-check",
            "display_name": "Document admission check",
            "locale": "",
            "facts": facts,
            "conflicts": [],
        }
    )
    return facts


def _extract_pdf(
    source: bytes, *, source_id: str, source_digest: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]], str]:
    if not source.startswith(b"%PDF-"):
        raise DocumentExtractionError("PDF signature is invalid")
    method = f"pdfplumber-{pdfplumber.__version__}-geometry-v1"
    candidates: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    warnings = [
        _warning(
            "untrusted_document_text",
            "Document text is untrusted data; embedded instructions were not executed.",
        ),
        _warning(
            "reading_order_requires_review",
            "PDF reading order is inferred from geometry and requires review.",
        ),
    ]
    image_count = 0
    try:
        with pdfplumber.open(io.BytesIO(source)) as document:
            if len(document.pages) > MAX_PDF_PAGES:
                raise DocumentExtractionError("PDF page count exceeds limit")
            if not document.pages:
                raise DocumentExtractionError("PDF contains no pages")
            for page_number, page in enumerate(document.pages, start=1):
                words = page.extract_words(
                    x_tolerance=2,
                    y_tolerance=3,
                    keep_blank_chars=False,
                    use_text_flow=False,
                    split_at_punctuation=False,
                    expand_ligatures=True,
                )
                image_count += len(page.images)
                page_cursor = 0
                for segment in _pdf_line_segments(words, float(page.width)):
                    text = segment["text"]
                    for chunk in _text_chunks(text):
                        start = page_cursor
                        end = start + len(chunk)
                        bbox = [round(float(value), 3) for value in segment["bbox"]]
                        locator = {
                            "kind": "page_bbox",
                            "page": page_number,
                            "section": (
                                f"pdf/page/{page_number}/bbox/"
                                + ",".join(f"{value:.3f}" for value in bbox)
                            ),
                            "start": start,
                            "end": end,
                            "bbox": bbox,
                        }
                        candidates.append(
                            _candidate(
                                source_id=source_id,
                                source_digest=source_digest,
                                text=chunk,
                                locator=locator,
                                extraction_method=method,
                            )
                        )
                        page_cursor = end + 1
    except PDFPasswordIncorrect as exc:
        raise DocumentExtractionError("encrypted PDF is unsupported") from exc
    except DocumentExtractionError:
        raise
    except Exception as exc:
        raise DocumentExtractionError("PDF source could not be parsed") from exc
    if image_count:
        omissions.append({"code": "images_not_extracted", "count": image_count})
    if not candidates:
        warnings.append(
            _warning(
                "ocr_required" if image_count else "no_extractable_text",
                (
                    "The PDF contains images but no extractable text; reviewed offline OCR is required."
                    if image_count
                    else "The PDF contains no extractable text."
                ),
            )
        )
    _append_duplicate_warning(candidates, warnings)
    return candidates, omissions, warnings, method


def _pdf_line_segments(words: Iterable[dict[str, Any]], page_width: float) -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        if not isinstance(word.get("text"), str) or not word["text"].strip():
            continue
        for row in rows:
            if abs(float(row[0]["top"]) - float(word["top"])) <= 3:
                row.append(word)
                break
        else:
            rows.append([word])
    output: list[dict[str, Any]] = []
    horizontal_gap = max(36.0, page_width * 0.08)
    for row in rows:
        ordered = sorted(row, key=lambda item: float(item["x0"]))
        segments: list[list[dict[str, Any]]] = []
        for word in ordered:
            if (
                not segments
                or float(word["x0"]) - float(segments[-1][-1]["x1"]) > horizontal_gap
            ):
                segments.append([word])
            else:
                segments[-1].append(word)
        for segment in segments:
            text = " ".join(str(item["text"]).strip() for item in segment).strip()
            if not text:
                continue
            output.append(
                {
                    "text": text,
                    "bbox": [
                        min(float(item["x0"]) for item in segment),
                        min(float(item["top"]) for item in segment),
                        max(float(item["x1"]) for item in segment),
                        max(float(item["bottom"]) for item in segment),
                    ],
                }
            )
    return output


def _extract_docx(
    source: bytes, *, source_id: str, source_digest: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]], str]:
    if not source.startswith(b"PK"):
        raise DocumentExtractionError("DOCX signature is invalid")
    method = "ooxml-bounded-blocks-v1"
    candidates: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    warnings = [
        _warning(
            "untrusted_document_text",
            "Document text is untrusted data; embedded instructions were not executed.",
        ),
        _warning(
            "docx_pagination_unavailable",
            "DOCX source locations use package parts and blocks; page numbers were not invented.",
        ),
    ]
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as package:
            infos = _validate_docx_package(package)
            names = {info.filename for info in infos}
            _validate_docx_content_types(package.read("[Content_Types].xml"))
            if _has_external_relationship(package, infos):
                warnings.append(
                    _warning(
                        "external_relationship_ignored",
                        "External DOCX relationships were ignored and were not fetched.",
                    )
                )
            media_count = sum(name.lower().startswith("word/media/") for name in names)
            if media_count:
                omissions.append({"code": "images_not_extracted", "count": media_count})
            omitted_parts = [
                name
                for name in names
                if name in {"word/comments.xml", "word/footnotes.xml", "word/endnotes.xml"}
            ]
            if omitted_parts:
                omissions.append({"code": "supplementary_parts_not_extracted", "parts": omitted_parts})
            parts = ["word/document.xml"] + sorted(
                name
                for name in names
                if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
            )
            for part in parts:
                candidates.extend(
                    _docx_part_candidates(
                        package.read(part),
                        part=part,
                        source_id=source_id,
                        source_digest=source_digest,
                        extraction_method=method,
                    )
                )
    except DocumentExtractionError:
        raise
    except (zipfile.BadZipFile, KeyError, DefusedElementTree.ParseError) as exc:
        raise DocumentExtractionError("DOCX source could not be parsed") from exc
    except Exception as exc:
        raise DocumentExtractionError("DOCX source could not be parsed") from exc
    if not candidates:
        warnings.append(_warning("no_extractable_text", "The DOCX contains no extractable text."))
    _append_duplicate_warning(candidates, warnings)
    return candidates, omissions, warnings, method


def _validate_docx_package(package: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = package.infolist()
    if not infos or len(infos) > MAX_DOCX_MEMBERS:
        raise DocumentExtractionError("DOCX member count is invalid")
    names: set[str] = set()
    total_size = 0
    for info in infos:
        name = info.filename
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
            or name in names
        ):
            raise DocumentExtractionError("DOCX member path is invalid")
        names.add(name)
        if info.flag_bits & 0x1:
            raise DocumentExtractionError("encrypted DOCX members are unsupported")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise DocumentExtractionError("DOCX compression method is unsupported")
        if info.file_size > MAX_DOCX_MEMBER_BYTES:
            raise DocumentExtractionError("DOCX member size exceeds limit")
        total_size += info.file_size
        if total_size > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise DocumentExtractionError("DOCX expanded size exceeds limit")
        if info.file_size and info.file_size / max(info.compress_size, 1) > MAX_DOCX_COMPRESSION_RATIO:
            raise DocumentExtractionError("DOCX compression ratio exceeds limit")
        lowered = name.lower()
        if any(marker in lowered for marker in _ACTIVE_DOCX_MARKERS):
            raise DocumentExtractionError("DOCX active or embedded content is unsupported")
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise DocumentExtractionError("DOCX required parts are missing")
    return infos


def _validate_docx_content_types(source: bytes) -> None:
    root = DefusedElementTree.fromstring(source)
    if root.tag != f"{_CT}Types":
        raise DocumentExtractionError("DOCX content types are invalid")
    document_types = {
        item.attrib.get("ContentType", "")
        for item in root.findall(f"{_CT}Override")
        if item.attrib.get("PartName") == "/word/document.xml"
    }
    if document_types != {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    }:
        raise DocumentExtractionError("DOCX main document content type is unsupported")


def _has_external_relationship(
    package: zipfile.ZipFile, infos: Iterable[zipfile.ZipInfo]
) -> bool:
    external = False
    for info in infos:
        if not info.filename.endswith(".rels"):
            continue
        root = DefusedElementTree.fromstring(package.read(info.filename))
        if root.tag != f"{_REL}Relationships":
            raise DocumentExtractionError("DOCX relationships are invalid")
        for relationship in root.findall(f"{_REL}Relationship"):
            if relationship.attrib.get("TargetMode") == "External":
                external = True
    return external


def _docx_part_candidates(
    source: bytes,
    *,
    part: str,
    source_id: str,
    source_digest: str,
    extraction_method: str,
) -> list[dict[str, Any]]:
    root = DefusedElementTree.fromstring(source)
    container = root.find(f"{_W}body") if part == "word/document.xml" else root
    if container is None:
        raise DocumentExtractionError("DOCX text part is invalid")
    output: list[dict[str, Any]] = []
    cursor = 0
    block_index = 0
    for child in list(container):
        if child.tag == f"{_W}p":
            values = [("paragraph", _docx_text(child))]
        elif child.tag == f"{_W}tbl":
            values = [
                (f"table_row_{row_index}", _docx_table_row_text(row))
                for row_index, row in enumerate(child.findall(f"{_W}tr"))
            ]
        else:
            continue
        for block_kind, text in values:
            normalized = text.strip()
            if not normalized:
                block_index += 1
                continue
            for chunk in _text_chunks(normalized):
                start = cursor
                end = start + len(chunk)
                locator = {
                    "kind": "part_block",
                    "page": 0,
                    "section": f"{part}#block={block_index};kind={block_kind}",
                    "start": start,
                    "end": end,
                    "part": part,
                    "block": block_index,
                    "block_kind": block_kind,
                }
                output.append(
                    _candidate(
                        source_id=source_id,
                        source_digest=source_digest,
                        text=chunk,
                        locator=locator,
                        extraction_method=extraction_method,
                    )
                )
                cursor = end + 1
            block_index += 1
    return output


def _docx_text(element: Any) -> str:
    fragments: list[str] = []
    for node in element.iter():
        if node.tag == f"{_W}t" and node.text:
            fragments.append(node.text)
        elif node.tag == f"{_W}tab":
            fragments.append("\t")
        elif node.tag in {f"{_W}br", f"{_W}cr"}:
            fragments.append("\n")
    return "".join(fragments)


def _docx_table_row_text(row: Any) -> str:
    cells = []
    for cell in row.findall(f"{_W}tc"):
        paragraphs = [_docx_text(item).strip() for item in cell.findall(f".//{_W}p")]
        cells.append("\n".join(value for value in paragraphs if value))
    return " | ".join(cells)


def _text_chunks(text: str) -> Iterable[str]:
    for start in range(0, len(text), MAX_CANDIDATE_CHARS):
        chunk = text[start : start + MAX_CANDIDATE_CHARS]
        if chunk:
            yield chunk


def _candidate(
    *,
    source_id: str,
    source_digest: str,
    text: str,
    locator: dict[str, Any],
    extraction_method: str,
) -> dict[str, Any]:
    text_digest = canonical_digest({"schema": "resume-document-text-v1", "text": text})
    binding = {
        "source_id": source_id,
        "source_digest": source_digest,
        "text": text,
        "text_digest": text_digest,
        "locator": locator,
        "extraction_method": extraction_method,
    }
    candidate_digest = canonical_digest(
        {"schema": "resume-document-candidate-v1", "candidate": binding}
    )
    return {
        "id": f"document-candidate-{candidate_digest[:32]}",
        "text": text,
        "text_digest": text_digest,
        "locator": copy.deepcopy(locator),
        "extraction_method": extraction_method,
        "candidate_digest": candidate_digest,
    }


def _review_payload(
    *,
    source_id: str,
    source_digest: str,
    source_byte_count: int,
    source_format: str,
    extraction_method: str,
    candidates: list[dict[str, Any]],
    omissions: list[dict[str, Any]],
    warnings: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "format": source_format,
        "extractor": {"version": EXTRACTOR_VERSION, "method": extraction_method},
        "source": {
            "id": source_id,
            "digest": source_digest,
            "byte_count": source_byte_count,
        },
        "candidates": copy.deepcopy(candidates),
        "omissions": copy.deepcopy(omissions),
        "warnings": copy.deepcopy(warnings),
        "action_capability": "none",
    }


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _append_duplicate_warning(
    candidates: list[dict[str, Any]], warnings: list[dict[str, str]]
) -> None:
    texts = [item["text"] for item in candidates]
    if len(texts) != len(set(texts)):
        warnings.append(
            _warning(
                "duplicate_candidate_text",
                "Duplicate extracted text is present at distinct source locations.",
            )
        )


def _validate_extraction_totals(candidates: list[dict[str, Any]]) -> None:
    if len(candidates) > MAX_CANDIDATES:
        raise DocumentExtractionError("document candidate count exceeds limit")
    if sum(len(item["text"]) for item in candidates) > MAX_EXTRACTED_CHARS:
        raise DocumentExtractionError("document extracted text exceeds limit")
    if len({item["id"] for item in candidates}) != len(candidates):
        raise DocumentExtractionError("document candidate bindings are duplicated")


def _reviewed_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise DocumentExtractionError("reviewed_at is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DocumentExtractionError("reviewed_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DocumentExtractionError("reviewed_at must be timezone-aware")
    return value
