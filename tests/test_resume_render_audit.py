from __future__ import annotations

import contextlib
import copy
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openchronicle.evaluation import resume_render


def test_frozen_resume_render_manifest_cases_and_production_previews() -> None:
    manifest = resume_render.load_manifest()
    cases = resume_render.load_cases()

    assert manifest["suite_id"] == resume_render.SUITE_ID
    assert manifest["renderer_version"] == 1
    assert manifest["template_id"] == "openchronicle-classic-v1"
    assert [case["id"] for case in cases] == [
        "single-page-hostile-markup",
        "unicode-and-directionality",
        "long-unbroken-token",
        "automatic-multipage-flow",
    ]
    for case in cases:
        first, first_segments = resume_render.build_case_preview(case)
        second, second_segments = resume_render.build_case_preview(case)

        assert first == second
        assert first_segments == second_segments
        assert first.renderer_version == manifest["renderer_version"]
        assert first.template_id == manifest["template_id"]
        assert first.html.startswith("<!doctype html>")
        assert "default-src 'none'" in first.html
        assert "<script" not in first.html.lower()
        assert "<img" not in first.html.lower()
        assert first.to_dict()["action_capability"] == "none"


def test_render_manifest_and_case_fixtures_are_closed(tmp_path: Path) -> None:
    manifest = copy.deepcopy(resume_render.load_manifest())
    manifest["unreviewed"] = True
    bad_manifest = tmp_path / "manifest.json"
    bad_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(resume_render.RenderAuditError, match="closed schema"):
        resume_render.load_manifest(bad_manifest)

    cases = {
        "schema_version": 1,
        "suite_id": resume_render.SUITE_ID,
        "cases": [
            {
                **resume_render.load_cases()[0],
                "expected_pages": {"minimum": 2, "maximum": 1},
            }
        ],
    }
    bad_cases = tmp_path / "cases.json"
    bad_cases.write_text(json.dumps(cases), encoding="utf-8")

    with pytest.raises(resume_render.RenderAuditError, match="page range"):
        resume_render.load_cases(bad_cases)


def test_pdfinfo_font_and_order_parsers_enforce_acceptance_contract() -> None:
    info = "Pages:           3\nPage size:       595.92 x 841.92 pts (A4)\n"
    assert resume_render._parse_pdfinfo(info) == (3, 595.92, 841.92)
    with pytest.raises(resume_render.RenderAuditError):
        resume_render._parse_pdfinfo("Pages: 1\n")

    fonts = (
        "name                                 type              encoding         emb sub uni object ID\n"
        "------------------------------------ ----------------- ---------------- --- --- --- ---------\n"
        "AAAAAA+ArialMT                       TrueType          WinAnsi          yes yes yes      5  0\n"
    )
    assert resume_render._parse_font_report(fonts) == (1, True)
    assert resume_render._ordered_content_found(
        "林玥 · Zoë\nSUMMARY\nA very\nlong line", ["林玥 · Zoë", "Summary", "A very long line"]
    )
    assert not resume_render._ordered_content_found("Ada\nSkills", ["Skills", "Ada"])


def test_bbox_parser_reports_out_of_page_words(tmp_path: Path) -> None:
    bbox = tmp_path / "bbox.html"
    bbox.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>
<page width="595.920000" height="841.920000">
<flow><block><line>
<word xMin="10" yMin="20" xMax="30" yMax="40">Ada</word>
<word xMin="590" yMin="20" xMax="610" yMax="40">overflow</word>
</line></block></flow>
</page></doc></body></html>
""",
        encoding="utf-8",
    )

    result = resume_render._parse_bbox(bbox, tolerance=1.0)

    assert result["word_count"] == 2
    assert result["out_of_bounds_word_count"] == 1
    assert len(result["digest"]) == 64


def test_browser_group_cleanup_kills_term_resistant_descendant(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    leader_code = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8'); "
        "time.sleep(30)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader_code, str(child_pid_path), child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not child_pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid_path.is_file()
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))

        resume_render._terminate_owned_browser_group(process)

        assert process.returncode == -signal.SIGTERM
        assert resume_render._owned_process_group_members(process.pid) == []
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_browser_group_cleanup_drains_residual_after_successful_leader_exit(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "residual.pid"
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    leader_code = (
        "import pathlib,subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8')"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader_code, str(child_pid_path), child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert process.wait(timeout=5) == 0
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        assert child_pid in resume_render._owned_process_group_members(process.pid)

        resume_render._terminate_owned_browser_group(process)

        assert resume_render._owned_process_group_members(process.pid) == []
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(child_pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        assert not state or state.startswith("Z")
    finally:
        for pid in resume_render._owned_process_group_members(process.pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
