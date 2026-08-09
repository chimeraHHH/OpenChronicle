"""Independent LibreOffice/Poppler acceptance for native Résumé Rescue DOCX.

The suite uses only committed synthetic fixtures. LibreOffice is a QA
comparator, never a runtime dependency or a source of product document bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..provenance.models import canonical_digest
from ..resume_rescue.native_export import render_docx_export
from ..resume_rescue.render import RENDERER_VERSION, TEMPLATE_ID, render_preview_tree
from .resume_render import DEFAULT_CASES, build_case_document_tree, load_cases

REPORT_SCHEMA_VERSION = 1
SUITE_ID = "OC-Vida-Resume-DOCX-Render-v1"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = REPO_ROOT / "benchmarks/vida-resume-rescue-v1/native-export/manifest.json"
_COMMAND_OUTPUT_LIMIT = 1024 * 1024
_PAGE_COUNT = re.compile(r"^Pages:\s+([0-9]+)$", re.MULTILINE)
_PAGE_SIZE = re.compile(
    r"^(?:Page size:|Page\s+\d+\s+size:)\s+([0-9.]+) x ([0-9.]+) pts",
    re.MULTILINE,
)


class DocxRenderAuditError(RuntimeError):
    """The independent DOCX render audit could not produce trusted evidence."""


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DocxRenderAuditError("native export manifest is unreadable") from exc
    if not isinstance(value, dict) or value.get("suite_id") != "OC-Vida-Resume-Native-Export-v1":
        raise DocxRenderAuditError("native export manifest identity is invalid")
    qa = value.get("docx_qa")
    expected_fields = {
        "kind",
        "version",
        "tools",
        "page_width_points",
        "page_height_points",
        "page_size_tolerance_points",
        "maximum_pdf_bytes",
        "maximum_png_bytes",
        "expected_pages",
    }
    if (
        not isinstance(qa, dict)
        or set(qa) != expected_fields
        or qa.get("kind") != "libreoffice-headless-to-pdf"
        or not isinstance(qa.get("version"), str)
        or not qa["version"]
        or not isinstance(qa.get("tools"), dict)
        or set(qa["tools"])
        != {"pdfinfo_version", "pdftotext_version", "pdftoppm_version"}
        or not isinstance(qa.get("expected_pages"), dict)
    ):
        raise DocxRenderAuditError("DOCX QA manifest is invalid")
    for field in ("page_width_points", "page_height_points", "page_size_tolerance_points"):
        if type(qa.get(field)) not in (int, float) or qa[field] <= 0:
            raise DocxRenderAuditError("DOCX QA page geometry is invalid")
    for field in ("maximum_pdf_bytes", "maximum_png_bytes"):
        if type(qa.get(field)) is not int or not 1 <= qa[field] <= 100_000_000:
            raise DocxRenderAuditError("DOCX QA artifact limit is invalid")
    if any(not isinstance(version, str) or not version for version in qa["tools"].values()):
        raise DocxRenderAuditError("DOCX QA tool version is invalid")
    for case_id, page_range in qa["expected_pages"].items():
        if (
            not isinstance(case_id, str)
            or not isinstance(page_range, list)
            or len(page_range) != 2
            or any(type(value) is not int or value < 1 for value in page_range)
            or page_range[0] > page_range[1]
        ):
            raise DocxRenderAuditError("DOCX QA expected page range is invalid")
    return value


def run_audit(
    *,
    output_dir: Path,
    manifest_path: Path = DEFAULT_MANIFEST,
    cases_path: Path = DEFAULT_CASES,
    soffice_path: Path | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    cases = load_cases(cases_path)
    qa = manifest["docx_qa"]
    commands = {
        "soffice": _required_command("soffice", soffice_path),
        "pdfinfo": _required_command("pdfinfo"),
        "pdftotext": _required_command("pdftotext"),
        "pdftoppm": _required_command("pdftoppm"),
    }
    timeout = manifest["limits"]["renderer_timeout_seconds"]
    versions = {
        "soffice": _command_version([commands["soffice"], "--version"], timeout),
        "pdfinfo_version": _command_version([commands["pdfinfo"], "-v"], timeout),
        "pdftotext_version": _command_version([commands["pdftotext"], "-v"], timeout),
        "pdftoppm_version": _command_version([commands["pdftoppm"], "-v"], timeout),
    }
    if versions["soffice"] != qa["version"]:
        raise DocxRenderAuditError("LibreOffice version differs from the pinned manifest")
    for field, expected in qa["tools"].items():
        if versions[field] != expected:
            raise DocxRenderAuditError(f"{field} differs from the pinned manifest")

    destination = output_dir.expanduser().resolve()
    try:
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError as exc:
        raise DocxRenderAuditError("DOCX audit output directory already exists") from exc
    destination.chmod(0o700)

    reports = [
        _run_case(
            case=case,
            destination=destination,
            commands=commands,
            manifest=manifest,
        )
        for case in cases
    ]
    if set(qa["expected_pages"]) != {str(case["id"]) for case in cases}:
        raise DocxRenderAuditError("DOCX QA page ranges differ from the fixture cases")
    passed = all(report["automated_status"] == "passed" for report in reports)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "suite_id": SUITE_ID,
        "manifest_digest": canonical_digest(manifest),
        "fixture_digest": canonical_digest({"schema": "resume-render-cases-v1", "cases": cases}),
        "renderer": {"version": RENDERER_VERSION, "template_id": TEMPLATE_ID},
        "docx_engine": manifest["docx_engine"],
        "qa_engine": {"kind": qa["kind"], "version": versions["soffice"]},
        "tools": {field: versions[field] for field in sorted(qa["tools"])},
        "cases": reports,
        "automated_status": "passed" if passed else "failed",
        "release_gate_status": "pending_visual_review" if passed else "failed",
        "visual_review": {
            "required": True,
            "status": "pending",
            "scope": "inspect every PNG for clipping, overlap, missing glyphs, spacing, and page transitions",
        },
        "verification_scope": "synthetic-docx-libreoffice-poppler-text-geometry-and-raster",
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
    commands: Mapping[str, str],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    case_id = str(case["id"])
    case_dir = destination / case_id
    case_dir.mkdir(mode=0o700)
    tree, _preview_segments = build_case_document_tree(case)
    ordered_segments = [tree.display_name]
    for section in tree.sections:
        ordered_segments.append(section.label.upper())
        ordered_segments.extend(item.text for item in section.items)
    preview = render_preview_tree(tree)
    first = render_docx_export(tree, preview_document_digest=preview.document_digest)
    second = render_docx_export(tree, preview_document_digest=preview.document_digest)
    if first.content != second.content:
        raise DocxRenderAuditError(f"{case_id}: DOCX bytes are not repeatable")

    docx_path = case_dir / f"{case_id}.docx"
    docx_path.write_bytes(first.content)
    docx_path.chmod(0o600)
    profile_dir = case_dir / "libreoffice-profile"
    profile_dir.mkdir(mode=0o700)
    _run_command(
        [
            commands["soffice"],
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--norestore",
            "--invisible",
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(case_dir),
            str(docx_path),
        ],
        timeout=manifest["limits"]["renderer_timeout_seconds"],
        environment={"SAL_USE_VCLPLUGIN": "svp"},
    )
    pdf_path = case_dir / f"{case_id}.pdf"
    if not pdf_path.is_file():
        raise DocxRenderAuditError(f"{case_id}: LibreOffice did not create a PDF")
    pdf_path.chmod(0o600)
    qa = manifest["docx_qa"]
    pdf_bytes = pdf_path.read_bytes()
    if not pdf_bytes.startswith(b"%PDF-") or not 1 <= len(pdf_bytes) <= qa["maximum_pdf_bytes"]:
        raise DocxRenderAuditError(f"{case_id}: converted PDF is invalid or oversized")

    info = _run_command(
        [commands["pdfinfo"], str(pdf_path)],
        timeout=manifest["limits"]["renderer_timeout_seconds"],
    )
    page_match = _PAGE_COUNT.search(info)
    if page_match is None:
        raise DocxRenderAuditError(f"{case_id}: Poppler page metadata is incomplete")
    page_count = int(page_match.group(1))
    page_info = _run_command(
        [commands["pdfinfo"], "-f", "1", "-l", str(page_count), str(pdf_path)],
        timeout=manifest["limits"]["renderer_timeout_seconds"],
    )
    size_matches = _PAGE_SIZE.findall(page_info)
    if len(size_matches) != page_count:
        raise DocxRenderAuditError(f"{case_id}: Poppler page geometry is incomplete")
    minimum_pages, maximum_pages = qa["expected_pages"][case_id]
    page_count_ok = minimum_pages <= page_count <= maximum_pages
    page_size_ok = all(
        abs(float(width) - qa["page_width_points"]) <= qa["page_size_tolerance_points"]
        and abs(float(height) - qa["page_height_points"]) <= qa["page_size_tolerance_points"]
        for width, height in size_matches
    )

    extracted_text = _run_command(
        [commands["pdftotext"], "-raw", "-enc", "UTF-8", str(pdf_path), "-"],
        timeout=manifest["limits"]["renderer_timeout_seconds"],
    )
    ordered_text_ok = _ordered_segments_present(extracted_text, ordered_segments)
    png_prefix = case_dir / "page"
    _run_command(
        [commands["pdftoppm"], "-png", "-r", "144", str(pdf_path), str(png_prefix)],
        timeout=manifest["limits"]["renderer_timeout_seconds"],
    )
    png_paths = sorted(case_dir.glob("page-*.png"))
    pngs_ok = len(png_paths) == page_count and all(
        1 <= path.stat().st_size <= qa["maximum_png_bytes"] for path in png_paths
    )
    checks = {
        "a4_page_size": page_size_ok,
        "bounded_artifacts": len(first.content) <= manifest["limits"]["maximum_docx_bytes"]
        and len(pdf_bytes) <= qa["maximum_pdf_bytes"]
        and pngs_ok,
        "docx_repeat_bytes": first.content == second.content,
        "ordered_text_complete": ordered_text_ok,
        "page_count_expected": page_count_ok,
        "parseable_pdf": True,
        "raster_pages_complete": pngs_ok,
    }
    automated_status = "passed" if all(checks.values()) else "failed"
    return {
        "id": case_id,
        "checks": checks,
        "page_count": page_count,
        "docx": {"byte_count": len(first.content), "sha256": first.content_digest},
        "pdf": {"byte_count": len(pdf_bytes), "sha256": _sha256(pdf_bytes)},
        "text_sha256": _sha256(extracted_text.encode("utf-8")),
        "pngs": [
            {"file": path.name, "byte_count": path.stat().st_size, "sha256": _sha256(path.read_bytes())}
            for path in png_paths
        ],
        "automated_status": automated_status,
    }


def _ordered_segments_present(text: str, ordered_segments: Sequence[str]) -> bool:
    normalized = _normalize_text(text)
    cursor = 0
    for segment in ordered_segments:
        needle = _normalize_text(segment)
        if not needle:
            return False
        tokens = needle.split()
        if any(len(token) > 80 for token in tokens):
            token_patterns = [
                r"\s*".join(re.escape(character) for character in token)
                if len(token) > 80
                else re.escape(token)
                for token in tokens
            ]
            match = re.search(r"\s+".join(token_patterns), normalized[cursor:])
            if match is None:
                return False
            cursor += match.end()
        else:
            position = normalized.find(needle, cursor)
            if position < 0:
                return False
            cursor = position + len(needle)
    return True


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(character for character in normalized if unicodedata.category(character) != "Cf")
    normalized = re.sub(r"(?<=-)\s+", "", normalized)
    return " ".join(normalized.split())


def _required_command(name: str, explicit: Path | None = None) -> str:
    if explicit is not None:
        resolved = explicit.expanduser().resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise DocxRenderAuditError(f"required {name} executable is unavailable")
        return str(resolved)
    found = shutil.which(name)
    if found is None:
        raise DocxRenderAuditError(f"required {name} executable is unavailable")
    return found


def _command_version(command: Sequence[str], timeout: int) -> str:
    return _run_command(command, timeout=timeout).splitlines()[0].strip()


def _run_command(
    command: Sequence[str],
    *,
    timeout: int,
    environment: Mapping[str, str] | None = None,
) -> str:
    env = os.environ.copy()
    if environment:
        env.update(environment)
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocxRenderAuditError("DOCX audit command failed to complete") from exc
    output = completed.stdout + completed.stderr
    if len(output) > _COMMAND_OUTPUT_LIMIT or completed.returncode != 0:
        raise DocxRenderAuditError("DOCX audit command returned an invalid result")
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocxRenderAuditError("DOCX audit command output is not UTF-8") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--soffice", type=Path)
    args = parser.parse_args(argv)
    report = run_audit(
        output_dir=args.output_dir,
        manifest_path=args.manifest,
        cases_path=args.cases,
        soffice_path=args.soffice,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["automated_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
