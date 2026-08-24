"""Pinned-engine render acceptance for deterministic Resume Rescue previews.

This is an opt-in development audit. It never enables product PDF export: the
generated report deliberately remains pending until its rendered PNG pages are
reviewed by a person.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..provenance.models import canonical_digest
from ..resume_rescue.models import (
    VALID_SECTIONS,
    build_exact_artifact,
    opportunity_digest,
    profile_digest,
    validate_profile,
)
from ..resume_rescue.pinned_pdf import (
    PinnedPdfProcessError,
    drain_owned_process_group,
    owned_process_group_members,
    print_pdf,
)
from ..resume_rescue.render import (
    RENDERER_VERSION,
    SECTION_LABELS,
    TEMPLATE_ID,
    ResumeDocumentTree,
    ResumePreview,
    build_document_tree,
    render_preview,
)
from ..resume_rescue.store import ProfileVersion, ResumeProjection

REPORT_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
FIXTURE_SCHEMA_VERSION = 1
SUITE_ID = "OC-Vida-Resume-Render-v1"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = REPO_ROOT / "benchmarks/vida-resume-rescue-v1/render/manifest.json"
DEFAULT_CASES = REPO_ROOT / "benchmarks/vida-resume-rescue-v1/render/cases.json"
DEFAULT_CHROME_MACOS = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_COMMAND_OUTPUT_LIMIT = 1024 * 1024
_CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_PAGE_SIZE = re.compile(r"^Page size:\s+([0-9.]+) x ([0-9.]+) pts(?:\s+\(.*\))?$", re.MULTILINE)
_PAGES = re.compile(r"^Pages:\s+([0-9]+)$", re.MULTILINE)

_MANIFEST_FIELDS = {
    "schema_version",
    "suite_id",
    "renderer_version",
    "template_id",
    "engine",
    "tools",
    "limits",
    "required_checks",
}
_ENGINE_FIELDS = {"kind", "version"}
_TOOL_FIELDS = {
    "pdfinfo_version",
    "pdftotext_version",
    "pdftoppm_version",
    "pdffonts_version",
}
_LIMIT_FIELDS = {
    "command_timeout_seconds",
    "maximum_pdf_bytes",
    "maximum_png_bytes",
    "page_width_points",
    "page_height_points",
    "page_size_tolerance_points",
    "bbox_tolerance_points",
}
_REQUIRED_CHECKS = {
    "a4_page_size",
    "bounded_artifacts",
    "embedded_fonts",
    "in_bounds_text_boxes",
    "ordered_text_complete",
    "page_count_expected",
    "parseable_pdf",
    "repeat_layout_stable",
    "repeat_raster_stable",
    "repeat_text_stable",
}


class RenderAuditError(RuntimeError):
    """The audit could not produce trustworthy render evidence."""


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    value = _read_object(path)
    if set(value) != _MANIFEST_FIELDS:
        raise RenderAuditError("render manifest must use the closed schema")
    if (
        value.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or value.get("suite_id") != SUITE_ID
        or value.get("renderer_version") != RENDERER_VERSION
        or value.get("template_id") != TEMPLATE_ID
    ):
        raise RenderAuditError("render manifest identity differs from production")
    engine = value.get("engine")
    tools = value.get("tools")
    limits = value.get("limits")
    checks = value.get("required_checks")
    if (
        not isinstance(engine, dict)
        or set(engine) != _ENGINE_FIELDS
        or engine.get("kind") != "google-chrome-headless"
        or not _bounded_string(engine.get("version"), maximum=128)
    ):
        raise RenderAuditError("render engine manifest is invalid")
    if (
        not isinstance(tools, dict)
        or set(tools) != _TOOL_FIELDS
        or any(not _bounded_string(tools.get(key), maximum=128) for key in _TOOL_FIELDS)
    ):
        raise RenderAuditError("render tool manifest is invalid")
    if not isinstance(limits, dict) or set(limits) != _LIMIT_FIELDS:
        raise RenderAuditError("render limits manifest is invalid")
    integer_limits = ("command_timeout_seconds", "maximum_pdf_bytes", "maximum_png_bytes")
    if any(
        type(limits.get(key)) is not int or not 1 <= limits[key] <= 100_000_000
        for key in integer_limits
    ):
        raise RenderAuditError("render integer limit is invalid")
    point_limits = (
        "page_width_points",
        "page_height_points",
        "page_size_tolerance_points",
        "bbox_tolerance_points",
    )
    if any(_positive_number(limits.get(key)) is None for key in point_limits):
        raise RenderAuditError("render point limit is invalid")
    if (
        not isinstance(checks, list)
        or set(checks) != _REQUIRED_CHECKS
        or len(checks) != len(_REQUIRED_CHECKS)
    ):
        raise RenderAuditError("render required checks differ")
    return value


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    value = _read_object(path)
    if set(value) != {"schema_version", "suite_id", "cases"}:
        raise RenderAuditError("render fixture must use the closed schema")
    if value.get("schema_version") != FIXTURE_SCHEMA_VERSION or value.get("suite_id") != SUITE_ID:
        raise RenderAuditError("render fixture identity is invalid")
    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= 20:
        raise RenderAuditError("render fixture cases must be a bounded list")
    cases = [_validate_case(item) for item in raw_cases]
    if len({item["id"] for item in cases}) != len(cases):
        raise RenderAuditError("render fixture case IDs must be unique")
    return cases


def _build_case_sources(
    case: Mapping[str, Any],
) -> tuple[ProfileVersion, ResumeProjection, list[str]]:
    """Build shared production sources from one already validated synthetic case."""

    profile_facts: list[dict[str, Any]] = []
    request_sections: list[dict[str, Any]] = []
    ordered_segments = [str(case["display_name"])]
    fact_index = 0
    for section in case["sections"]:
        fact_ids = []
        ordered_segments.append(SECTION_LABELS[section["kind"]])
        for text in section["items"]:
            fact_index += 1
            fact_id = f"fact-{case['id']}-{fact_index:03d}"
            fact_ids.append(fact_id)
            ordered_segments.append(text)
            profile_facts.append(
                {
                    "id": fact_id,
                    "section": section["kind"],
                    "text": text,
                    "confidentiality": "public",
                    "ownership_scope": "individual",
                    "provenance": [
                        {
                            "kind": "manual_reviewed",
                            "reviewed_at": "2026-08-09T00:00:00Z",
                        }
                    ],
                }
            )
        request_sections.append({"kind": section["kind"], "fact_ids": fact_ids})

    profile_value = validate_profile(
        {
            "schema_version": 1,
            "profile_id": f"render-{case['id']}",
            "display_name": case["display_name"],
            "locale": case["locale"],
            "facts": profile_facts,
            "conflicts": [],
        }
    )
    profile_hash = profile_digest(profile_value)
    profile = ProfileVersion(
        profile_id=profile_value["profile_id"],
        version=1,
        profile=profile_value,
        digest=profile_hash,
        created_at="2026-08-09T00:00:00Z",
    )
    opportunity = {
        "schema_version": 1,
        "employer": "Synthetic Render Fixture",
        "title": "Render Acceptance",
        "source_url": "",
        "source_text": "Synthetic render acceptance only.",
        "priorities": [],
        "locale": case["locale"],
        "captured_at": "2026-08-09T00:00:00Z",
    }
    opportunity_hash = opportunity_digest(opportunity)
    opportunity_id = f"resume-opportunity-{opportunity_hash[:32]}"
    request = {"schema_version": 1, "sections": request_sections, "requirements": []}
    artifact = build_exact_artifact(
        profile=profile_value,
        profile_version=1,
        profile_digest_value=profile_hash,
        opportunity=opportunity,
        opportunity_id=opportunity_id,
        opportunity_digest_value=opportunity_hash,
        request=request,
    )
    request_hash = canonical_digest({"schema": "resume-projection-request-v1", "request": request})
    artifact_hash = canonical_digest({"schema": "resume-artifact-v1", "artifact": artifact})
    idempotency_key = canonical_digest(
        {
            "schema": "resume-render-audit-projection-v1",
            "case": case["id"],
            "profile_digest": profile_hash,
            "opportunity_digest": opportunity_hash,
            "request_digest": request_hash,
        }
    )
    projection = ResumeProjection(
        id=f"resume-render-{idempotency_key[:32]}",
        idempotency_key=idempotency_key,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        profile_digest=profile.digest,
        opportunity_id=opportunity_id,
        opportunity_digest=opportunity_hash,
        request=request,
        request_digest=request_hash,
        artifact=artifact,
        artifact_digest=artifact_hash,
        created_at="2026-08-09T00:00:00Z",
    )
    return profile, projection, ordered_segments


def build_case_preview(case: Mapping[str, Any]) -> tuple[ResumePreview, list[str]]:
    """Build a production preview from one already validated synthetic case."""

    profile, projection, ordered_segments = _build_case_sources(case)
    return render_preview(profile=profile, projection=projection), ordered_segments


def build_case_document_tree(case: Mapping[str, Any]) -> tuple[ResumeDocumentTree, list[str]]:
    """Build the same closed semantic tree used by native export renderers."""

    profile, projection, ordered_segments = _build_case_sources(case)
    return build_document_tree(profile=profile, projection=projection), ordered_segments


def run_audit(
    *,
    output_dir: Path,
    manifest_path: Path = DEFAULT_MANIFEST,
    cases_path: Path = DEFAULT_CASES,
    chrome_path: Path | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    cases = load_cases(cases_path)
    chrome = _resolve_chrome(chrome_path)
    commands = {
        "pdfinfo": _required_command("pdfinfo"),
        "pdftotext": _required_command("pdftotext"),
        "pdftoppm": _required_command("pdftoppm"),
        "pdffonts": _required_command("pdffonts"),
    }
    timeout = manifest["limits"]["command_timeout_seconds"]
    observed_versions = {
        "engine": _command_version([str(chrome), "--version"], timeout=timeout),
        "pdfinfo_version": _command_version([commands["pdfinfo"], "-v"], timeout=timeout),
        "pdftotext_version": _command_version([commands["pdftotext"], "-v"], timeout=timeout),
        "pdftoppm_version": _command_version([commands["pdftoppm"], "-v"], timeout=timeout),
        "pdffonts_version": _command_version([commands["pdffonts"], "-v"], timeout=timeout),
    }
    if observed_versions["engine"] != manifest["engine"]["version"]:
        raise RenderAuditError("Chrome version differs from pinned render manifest")
    for key in _TOOL_FIELDS:
        if observed_versions[key] != manifest["tools"][key]:
            raise RenderAuditError(f"{key} differs from pinned render manifest")

    destination = output_dir.expanduser().resolve()
    try:
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError as exc:
        raise RenderAuditError("render audit output directory already exists") from exc
    destination.chmod(0o700)

    case_reports = []
    for case in cases:
        case_reports.append(
            _run_case(
                case=case,
                destination=destination,
                chrome=chrome,
                commands=commands,
                manifest=manifest,
            )
        )
    automated_passed = all(item["automated_status"] == "passed" for item in case_reports)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "suite_id": SUITE_ID,
        "manifest_digest": canonical_digest(manifest),
        "fixture_digest": canonical_digest({"schema": "resume-render-cases-v1", "cases": cases}),
        "renderer": {"version": RENDERER_VERSION, "template_id": TEMPLATE_ID},
        "engine": {
            "kind": manifest["engine"]["kind"],
            "version": observed_versions["engine"],
            "executable_sha256": _file_sha256(chrome),
        },
        "tools": {key: observed_versions[key] for key in sorted(_TOOL_FIELDS)},
        "cases": case_reports,
        "automated_status": "passed" if automated_passed else "failed",
        "release_gate_status": "pending_visual_review" if automated_passed else "failed",
        "visual_review": {
            "required": True,
            "status": "pending",
            "scope": "inspect every committed report PNG for clipping, overlap, glyph, spacing, and page-transition defects",
        },
        "verification_scope": "pinned-local-engine-structural-text-layout-and-raster-repeatability",
        "authenticity_verified": False,
        "action_capability": "none",
    }
    report_path = destination / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_path.chmod(0o600)
    return report


def _run_case(
    *,
    case: Mapping[str, Any],
    destination: Path,
    chrome: Path,
    commands: Mapping[str, str],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    case_dir = destination / str(case["id"])
    case_dir.mkdir(mode=0o700)
    preview, ordered_segments = build_case_preview(case)
    html_path = case_dir / "preview.html"
    pdf_path = case_dir / "preview.pdf"
    html_path.write_text(preview.html, encoding="utf-8")
    html_path.chmod(0o600)
    timeout = int(manifest["limits"]["command_timeout_seconds"])
    first_process_disposition = _print_pdf(chrome, html_path, pdf_path, timeout=timeout)
    pdf_path.chmod(0o600)
    first = _inspect_pdf(
        pdf_path=pdf_path,
        png_prefix=case_dir / "preview",
        ordered_segments=ordered_segments,
        commands=commands,
        timeout=timeout,
        limits=manifest["limits"],
    )
    with tempfile.TemporaryDirectory(prefix="openchronicle-render-repeat-") as raw_repeat:
        repeat_dir = Path(raw_repeat)
        repeat_html = repeat_dir / "preview.html"
        repeat_pdf = repeat_dir / "preview.pdf"
        repeat_html.write_text(preview.html, encoding="utf-8")
        repeat_html.chmod(0o600)
        repeat_process_disposition = _print_pdf(chrome, repeat_html, repeat_pdf, timeout=timeout)
        repeat_pdf.chmod(0o600)
        repeat = _inspect_pdf(
            pdf_path=repeat_pdf,
            png_prefix=repeat_dir / "preview",
            ordered_segments=ordered_segments,
            commands=commands,
            timeout=timeout,
            limits=manifest["limits"],
        )

    minimum = case["expected_pages"]["minimum"]
    maximum = case["expected_pages"]["maximum"]
    checks = {
        "a4_page_size": first["a4_page_size"],
        "bounded_artifacts": first["bounded_artifacts"],
        "embedded_fonts": first["embedded_fonts"],
        "in_bounds_text_boxes": first["out_of_bounds_word_count"] == 0,
        "ordered_text_complete": first["ordered_text_complete"],
        "page_count_expected": minimum <= first["page_count"] <= maximum,
        "parseable_pdf": first["parseable_pdf"],
        "repeat_layout_stable": first["layout_digest"] == repeat["layout_digest"],
        "repeat_raster_stable": first["png_sha256"] == repeat["png_sha256"],
        "repeat_text_stable": first["text_digest"] == repeat["text_digest"],
    }
    passed = set(checks) == _REQUIRED_CHECKS and all(checks.values())
    return {
        "id": case["id"],
        "projection_id": preview.projection_id,
        "document_digest": preview.document_digest,
        "expected_pages": {"minimum": minimum, "maximum": maximum},
        "observed": {
            "page_count": first["page_count"],
            "page_width_points": first["page_width_points"],
            "page_height_points": first["page_height_points"],
            "pdf_bytes": first["pdf_bytes"],
            "pdf_sha256": first["pdf_sha256"],
            "repeat_pdf_bytes_equal": first["pdf_sha256"] == repeat["pdf_sha256"],
            "text_digest": first["text_digest"],
            "layout_digest": first["layout_digest"],
            "word_count": first["word_count"],
            "out_of_bounds_word_count": first["out_of_bounds_word_count"],
            "font_count": first["font_count"],
            "png_sha256": first["png_sha256"],
            "png_files": first["png_files"],
            "process_disposition": first_process_disposition,
            "repeat_process_disposition": repeat_process_disposition,
        },
        "checks": checks,
        "automated_status": "passed" if passed else "failed",
    }


def _inspect_pdf(
    *,
    pdf_path: Path,
    png_prefix: Path,
    ordered_segments: Sequence[str],
    commands: Mapping[str, str],
    timeout: int,
    limits: Mapping[str, Any],
) -> dict[str, Any]:
    info = _run_capture([commands["pdfinfo"], str(pdf_path)], timeout=timeout)
    page_count, page_width, page_height = _parse_pdfinfo(info)
    extracted = _run_capture(
        [commands["pdftotext"], "-raw", "-enc", "UTF-8", str(pdf_path), "-"],
        timeout=timeout,
    )
    bbox_path = png_prefix.with_suffix(".bbox.html")
    _run_capture(
        [commands["pdftotext"], "-bbox-layout", str(pdf_path), str(bbox_path)],
        timeout=timeout,
    )
    bbox_path.chmod(0o600)
    bbox = _parse_bbox(
        bbox_path,
        tolerance=float(limits["bbox_tolerance_points"]),
    )
    font_report = _run_capture([commands["pdffonts"], str(pdf_path)], timeout=timeout)
    font_count, embedded_fonts = _parse_font_report(font_report)
    _run_capture(
        [commands["pdftoppm"], "-png", "-r", "144", str(pdf_path), str(png_prefix)],
        timeout=timeout,
    )
    png_paths = sorted(png_prefix.parent.glob(f"{png_prefix.name}-*.png"))
    for path in png_paths:
        path.chmod(0o600)
    pdf_bytes = pdf_path.stat().st_size
    png_bytes = sum(path.stat().st_size for path in png_paths)
    width_matches = math.isclose(
        page_width,
        float(limits["page_width_points"]),
        abs_tol=float(limits["page_size_tolerance_points"]),
    )
    height_matches = math.isclose(
        page_height,
        float(limits["page_height_points"]),
        abs_tol=float(limits["page_size_tolerance_points"]),
    )
    png_hashes = [_file_sha256(path) for path in png_paths]
    return {
        "parseable_pdf": pdf_path.read_bytes()[:5] == b"%PDF-" and page_count > 0,
        "page_count": page_count,
        "page_width_points": page_width,
        "page_height_points": page_height,
        "a4_page_size": width_matches and height_matches,
        "ordered_text_complete": _ordered_content_found(extracted, ordered_segments),
        "text_digest": canonical_digest(
            {"schema": "resume-render-extracted-text-v1", "text": _compact(extracted)}
        ),
        "layout_digest": bbox["digest"],
        "word_count": bbox["word_count"],
        "out_of_bounds_word_count": bbox["out_of_bounds_word_count"],
        "font_count": font_count,
        "embedded_fonts": font_count > 0 and embedded_fonts,
        "pdf_bytes": pdf_bytes,
        "pdf_sha256": _file_sha256(pdf_path),
        "png_files": [path.name for path in png_paths],
        "png_sha256": png_hashes,
        "bounded_artifacts": (
            0 < pdf_bytes <= int(limits["maximum_pdf_bytes"])
            and 0 < png_bytes <= int(limits["maximum_png_bytes"])
            and len(png_paths) == page_count
        ),
    }


def _print_pdf(chrome: Path, html_path: Path, pdf_path: Path, *, timeout: int) -> str:
    try:
        return print_pdf(chrome, html_path, pdf_path, timeout=timeout)
    except PinnedPdfProcessError as exc:
        raise RenderAuditError(str(exc)) from exc


def _terminate_owned_browser_group(process: subprocess.Popen[bytes]) -> None:
    try:
        drain_owned_process_group(process)
    except PinnedPdfProcessError as exc:
        raise RenderAuditError(str(exc)) from exc


def _owned_process_group_members(pgid: int) -> list[int]:
    try:
        return owned_process_group_members(pgid)
    except PinnedPdfProcessError as exc:
        raise RenderAuditError(str(exc)) from exc


def _run_capture(command: Sequence[str], *, timeout: int) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderAuditError(f"command timed out: {Path(command[0]).name}") from exc
    output = result.stdout + result.stderr
    if len(output) > _COMMAND_OUTPUT_LIMIT:
        raise RenderAuditError(f"command output is oversized: {Path(command[0]).name}")
    if result.returncode != 0:
        raise RenderAuditError(
            f"command failed with exit code {result.returncode}: {Path(command[0]).name}"
        )
    return result.stdout.decode("utf-8", errors="strict")


def _command_version(command: Sequence[str], *, timeout: int) -> str:
    try:
        result = subprocess.run(command, check=False, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RenderAuditError("version command timed out") from exc
    output = (result.stdout + result.stderr).decode("utf-8", errors="strict").strip()
    first_line = output.splitlines()[0] if output else ""
    if result.returncode != 0 or not _bounded_string(first_line, maximum=128):
        raise RenderAuditError("could not identify render dependency version")
    return first_line


def _parse_pdfinfo(value: str) -> tuple[int, float, float]:
    pages = _PAGES.search(value)
    size = _PAGE_SIZE.search(value)
    if pages is None or size is None:
        raise RenderAuditError("pdfinfo did not report pages and page size")
    count = int(pages.group(1))
    width = float(size.group(1))
    height = float(size.group(2))
    if count < 1 or not all(math.isfinite(item) and item > 0 for item in (width, height)):
        raise RenderAuditError("pdfinfo reported invalid geometry")
    return count, width, height


def _parse_bbox(path: Path, *, tolerance: float) -> dict[str, Any]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise RenderAuditError("pdftotext bbox output is invalid") from exc
    pages = []
    out_of_bounds = 0
    word_count = 0
    for page in (item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "page"):
        try:
            width = float(page.attrib["width"])
            height = float(page.attrib["height"])
        except (KeyError, ValueError) as exc:
            raise RenderAuditError("bbox page geometry is invalid") from exc
        words = []
        for word in (item for item in page.iter() if item.tag.rsplit("}", 1)[-1] == "word"):
            try:
                coords = tuple(float(word.attrib[key]) for key in ("xMin", "yMin", "xMax", "yMax"))
            except (KeyError, ValueError) as exc:
                raise RenderAuditError("bbox word geometry is invalid") from exc
            x_min, y_min, x_max, y_max = coords
            if (
                any(not math.isfinite(item) for item in coords)
                or x_min < -tolerance
                or y_min < -tolerance
                or x_max > width + tolerance
                or y_max > height + tolerance
                or x_min > x_max
                or y_min > y_max
            ):
                out_of_bounds += 1
            words.append([word.text or "", *(round(item, 3) for item in coords)])
            word_count += 1
        pages.append([round(width, 3), round(height, 3), words])
    if not pages or word_count == 0:
        raise RenderAuditError("bbox output contains no rendered text")
    return {
        "word_count": word_count,
        "out_of_bounds_word_count": out_of_bounds,
        "digest": canonical_digest({"schema": "resume-render-bbox-v1", "pages": pages}),
    }


def _parse_font_report(value: str) -> tuple[int, bool]:
    lines = value.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.startswith("---")) + 1
    except StopIteration as exc:
        raise RenderAuditError("pdffonts output is invalid") from exc
    fonts = [line.split() for line in lines[start:] if line.strip()]
    # The font type may itself contain a space (for example, "CID TrueType"),
    # while the final five fields are always emb/sub/uni/object-id/generation.
    if any(len(parts) < 8 for parts in fonts):
        raise RenderAuditError("pdffonts row is invalid")
    return len(fonts), bool(fonts) and all(parts[-5] == "yes" for parts in fonts)


def _ordered_content_found(extracted: str, segments: Sequence[str]) -> bool:
    haystack = _compact(extracted)
    cursor = 0
    for segment in segments:
        needle = _compact(segment)
        if not needle:
            return False
        found = haystack.find(needle, cursor)
        if found < 0:
            return False
        cursor = found + len(needle)
    return True


def _compact(value: str) -> str:
    # Section headings use CSS text-transform, so extracted text can differ in
    # case while preserving the exact source characters and reading order.
    # Poppler also inserts bidi-format controls around right-to-left runs; those
    # controls carry layout direction rather than source résumé content.
    normalized = unicodedata.normalize("NFC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and unicodedata.category(character) != "Cf"
    )


def _validate_case(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "display_name",
        "locale",
        "expected_pages",
        "sections",
    }:
        raise RenderAuditError("render case must use the closed schema")
    case_id = value.get("id")
    display_name = value.get("display_name")
    locale = value.get("locale")
    expected = value.get("expected_pages")
    sections = value.get("sections")
    if not isinstance(case_id, str) or _CASE_ID.fullmatch(case_id) is None:
        raise RenderAuditError("render case ID is invalid")
    if not _bounded_string(display_name, maximum=512) or not _bounded_string(locale, maximum=64):
        raise RenderAuditError("render case identity text is invalid")
    if not isinstance(expected, dict) or set(expected) != {"minimum", "maximum"}:
        raise RenderAuditError("render case page expectation is invalid")
    minimum = expected.get("minimum")
    maximum = expected.get("maximum")
    if type(minimum) is not int or type(maximum) is not int or not 1 <= minimum <= maximum <= 20:
        raise RenderAuditError("render case page range is invalid")
    if not isinstance(sections, list) or not 1 <= len(sections) <= len(VALID_SECTIONS):
        raise RenderAuditError("render case sections must be a bounded list")
    normalized_sections = []
    seen = set()
    total_items = 0
    for raw in sections:
        if not isinstance(raw, dict) or set(raw) != {"kind", "items"}:
            raise RenderAuditError("render case section must use the closed schema")
        kind = raw.get("kind")
        items = raw.get("items")
        if kind not in VALID_SECTIONS or kind in seen:
            raise RenderAuditError("render case section kind is invalid or duplicated")
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise RenderAuditError("render case items must be a bounded list")
        if any(not _bounded_string(item, maximum=8_000) for item in items):
            raise RenderAuditError("render case item text is invalid")
        total_items += len(items)
        seen.add(kind)
        normalized_sections.append({"kind": kind, "items": list(items)})
    if total_items > 200:
        raise RenderAuditError("render case has too many items")
    return {
        "id": case_id,
        "display_name": display_name,
        "locale": locale,
        "expected_pages": {"minimum": minimum, "maximum": maximum},
        "sections": normalized_sections,
    }


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderAuditError(f"unreadable render JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RenderAuditError("render JSON must contain an object")
    return value


def _resolve_chrome(value: Path | None) -> Path:
    if value is not None:
        path = value.expanduser().resolve()
    elif DEFAULT_CHROME_MACOS.is_file():
        path = DEFAULT_CHROME_MACOS.resolve()
    else:
        candidate = shutil.which("google-chrome") or shutil.which("chromium")
        if candidate is None:
            raise RenderAuditError("Chrome executable is unavailable")
        path = Path(candidate).resolve()
    if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
        raise RenderAuditError("Chrome executable must be an executable regular file")
    return path


def _required_command(name: str) -> str:
    value = shutil.which(name)
    if value is None:
        raise RenderAuditError(f"required PDF command is unavailable: {name}")
    return str(Path(value).resolve())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _bounded_string(value: object, *, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and "\x00" not in value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--chrome", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_audit(
            output_dir=args.output_dir,
            manifest_path=args.manifest,
            cases_path=args.cases,
            chrome_path=args.chrome,
        )
    except RenderAuditError as exc:
        print(json.dumps({"automated_status": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    summary = {
        "automated_status": report["automated_status"],
        "release_gate_status": report["release_gate_status"],
        "case_count": len(report["cases"]),
        "output_dir": str(args.output_dir.expanduser().resolve()),
        "action_capability": "none",
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if report["automated_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
