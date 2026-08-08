from __future__ import annotations

import base64
import json
import logging
import sqlite3
import stat
import sys
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

_LIB_PATH = Path(__file__).parents[1] / "scripts" / "live_ax_privacy.py"
_SPEC = spec_from_file_location("live_ax_privacy_test_support", _LIB_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_LIB = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _LIB
_SPEC.loader.exec_module(_LIB)

REPORT_SCHEMA_VERSION = _LIB.REPORT_SCHEMA_VERSION
Marker = _LIB.Marker
SinkScanner = _LIB.SinkScanner
safe_report_bytes = _LIB.safe_report_bytes
verify_report = _LIB.verify_report
write_private_report = _LIB.write_private_report
analyze_exact_window_jpeg = _LIB.analyze_exact_window_jpeg
PUBLIC_VISUAL_CANARY = _LIB.PUBLIC_VISUAL_CANARY
PRIVATE_VISUAL_CANARY = _LIB.PRIVATE_VISUAL_CANARY


def _sink(marker: Marker, *, hits: int = 0, units: int = 1) -> dict:
    return {
        "units_scanned": units,
        "bytes_scanned": 12,
        "unavailable_units": 0,
        "markers": [
            {
                "sha256": marker.sha256,
                "classification": marker.classification,
                "hits": hits,
            }
        ],
    }


def _report(marker: Marker, *, hits: int = 0) -> dict:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "complete",
        "marker_digests": [marker.sha256],
        "checks": [{"id": "fixture_ready", "status": "pass", "observed_count": 2}],
        "sinks": {"capture_json": _sink(marker, hits=hits)},
        "artifact_policy": {
            "plaintext_markers_retained": 0,
            "raw_ax_payloads_retained": 0,
            "temporary_root_removed": True,
        },
    }


def _manifest() -> dict:
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "required_checks": ["fixture_ready"],
        "required_sinks": ["capture_json"],
        "forbidden_zero_sinks": ["capture_json"],
    }


def test_sink_scanner_reports_only_digest_count_and_classification(tmp_path: Path) -> None:
    control = Marker("CONTROL_CANARY_NOT_FOR_REPORT", "control")
    forbidden = Marker("FORBIDDEN_CANARY_NOT_FOR_REPORT", "forbidden")
    source = tmp_path / "capture.json"
    source.write_text(
        f"{control.value} {forbidden.value} {forbidden.value}",
        encoding="utf-8",
    )

    scanner = SinkScanner([control, forbidden])
    scanner.scan_files("capture_json", [source])
    report = scanner.report()
    serialized = json.dumps(report)

    assert control.value not in serialized
    assert forbidden.value not in serialized
    markers = {item["sha256"]: item for item in report["capture_json"]["markers"]}
    assert markers[control.sha256]["hits"] == 1
    assert markers[forbidden.sha256]["hits"] == 2
    assert report["capture_json"]["units_scanned"] == 1


def test_sqlite_scanner_reads_plain_and_fts_projection(tmp_path: Path) -> None:
    marker = Marker("FORBIDDEN_SQLITE_CANARY", "forbidden")
    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE captures(id TEXT PRIMARY KEY, visible_text TEXT)")
    conn.execute("CREATE VIRTUAL TABLE captures_fts USING fts5(visible_text)")
    conn.execute("INSERT INTO captures VALUES ('one', ?)", (marker.value,))
    conn.execute("INSERT INTO captures_fts VALUES (?)", (marker.value,))
    conn.commit()
    conn.close()

    scanner = SinkScanner([marker])
    scanner.scan_sqlite("sqlite_capture_fts", db, ["captures", "captures_fts"])
    result = scanner.report()["sqlite_capture_fts"]

    assert result["units_scanned"] == 2
    assert result["unavailable_units"] == 0
    assert result["markers"][0]["hits"] == 2


def test_report_serialization_rejects_plaintext_marker() -> None:
    marker = Marker("NEVER_RETAIN_THIS_VALUE", "forbidden")
    with pytest.raises(ValueError, match="plaintext audit marker"):
        safe_report_bytes({"unsafe": marker.value}, [marker])


def test_private_report_is_owner_only_and_redacted(tmp_path: Path) -> None:
    marker = Marker("PRIVATE_REPORT_CANARY", "forbidden")
    path = tmp_path / "reports" / "audit.json"

    write_private_report(path, _report(marker), [marker])

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert marker.value not in path.read_text(encoding="utf-8")
    assert marker.sha256 in path.read_text(encoding="utf-8")


def test_manifest_verifier_accepts_clean_complete_report() -> None:
    marker = Marker("CLEAN_FORBIDDEN_CANARY", "forbidden")
    assert verify_report(_report(marker), _manifest()) == []


def test_manifest_verifier_rejects_forbidden_sink_hit_and_raw_retention() -> None:
    marker = Marker("LEAKED_FORBIDDEN_CANARY", "forbidden")
    report = _report(marker, hits=1)
    report["artifact_policy"]["raw_ax_payloads_retained"] = 1

    violations = verify_report(report, _manifest())

    assert "forbidden_marker_hit:capture_json" in violations
    assert "raw_ax_payloads_retained" in violations


def test_manifest_verifier_rejects_unavailable_or_missing_marker_coverage() -> None:
    marker = Marker("COVERAGE_CANARY", "forbidden")
    unavailable = _report(marker)
    unavailable["sinks"]["capture_json"]["unavailable_units"] = 99
    missing_markers = _report(marker)
    missing_markers["sinks"]["capture_json"]["markers"] = []

    assert "sink_unavailable:capture_json" in verify_report(
        unavailable, _manifest()
    )
    assert "sink_marker_set_mismatch:capture_json" in verify_report(
        missing_markers, _manifest()
    )


def test_manifest_verifier_rejects_duplicate_check_overwrite() -> None:
    marker = Marker("DUPLICATE-CHECK-CANARY", "forbidden")
    report = _report(marker)
    report["checks"] = [
        {"id": "fixture_ready", "status": "fail", "observed_count": 0},
        {"id": "fixture_ready", "status": "pass", "observed_count": 2},
    ]

    violations = verify_report(report, _manifest())

    assert "duplicate_check:fixture_ready" in violations
    assert "check_not_passed:fixture_ready" in violations


def test_allow_incomplete_still_checks_redaction_without_claiming_live_coverage() -> None:
    marker = Marker("INCOMPLETE_REPORT_CANARY", "forbidden")
    report = _report(marker)
    report["status"] = "incomplete"
    report["checks"] = []
    report["sinks"] = {}

    assert verify_report(report, _manifest(), allow_incomplete=True) == []


def test_pixel_probe_rejects_full_screen_with_forged_public_metadata() -> None:
    from PIL import Image, ImageDraw

    def shot_for(image: Image.Image):
        out = BytesIO()
        image.save(out, format="JPEG", quality=90)
        return SimpleNamespace(image_base64=base64.b64encode(out.getvalue()).decode("ascii"))

    target = SimpleNamespace(bounds=SimpleNamespace(width=520, height=260))
    exact = Image.new("RGB", (520, 260), PUBLIC_VISUAL_CANARY)
    exact_metrics = analyze_exact_window_jpeg(shot_for(exact), target)
    assert exact_metrics["jpeg_valid"] is True
    assert exact_metrics["aspect_matches"] is True
    assert exact_metrics["public_samples"] == 5
    assert exact_metrics["private_pixels"] == 0

    screen = Image.new("RGB", (1200, 600), (96, 96, 96))
    draw = ImageDraw.Draw(screen)
    draw.rectangle((80, 180, 279, 329), fill=PUBLIC_VISUAL_CANARY)
    draw.rectangle((900, 180, 1099, 329), fill=PRIVATE_VISUAL_CANARY)
    screen_metrics = analyze_exact_window_jpeg(shot_for(screen), target)
    pixel_count = int(screen_metrics["pixel_count"])
    would_pass_color_gate = (
        int(screen_metrics["public_pixels"]) >= int(pixel_count * 0.08)
        and int(screen_metrics["public_samples"]) >= 4
        and int(screen_metrics["private_pixels"]) <= max(1, int(pixel_count * 0.001))
        and int(screen_metrics["private_samples"]) == 0
    )
    assert screen_metrics["aspect_matches"] is True
    assert int(screen_metrics["private_pixels"]) > 0
    assert would_pass_color_gate is False


def test_live_manifest_requires_every_requested_sink_and_fault_case() -> None:
    manifest_path = (
        Path(__file__).parent / "live" / "macos_ax_privacy" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert {
        "capture_json",
        "logs",
        "sqlite_capture_fts",
        "timeline",
        "session",
        "classifier_prompt",
    }.issubset(manifest["required_sinks"])
    assert {
        "two_visible_windows",
        "scheduler_exact_identity_capture",
        "exact_window_screenshot_verified",
        "exact_window_public_color_observed",
        "exact_window_sibling_color_absent",
        "secure_text_redacted",
        "normal_text_observed",
        "url_like_marker_observed",
        "rapid_focus_fail_closed",
        "helper_permission_denial_content_fail_closed",
        "helper_crash_content_fail_closed",
    }.issubset(manifest["required_checks"])
    assert manifest["minimum_marker_counts"] == {"control": 2, "forbidden": 3}


def test_production_scheduler_prevents_denied_window_markers_reaching_sinks(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from openchronicle.capture import scheduler
    from openchronicle.capture.window_meta import WindowBounds, WindowMeta
    from openchronicle.config import CaptureConfig
    from openchronicle.store import fts

    title_marker = Marker("OC_LIVE_DENIED_TITLE_UNIT_CANARY", "forbidden")
    url_marker = Marker("https://forbidden.invalid/unit/canary", "forbidden")
    cfg = CaptureConfig(excluded_window_title_patterns=[title_marker.value])

    monkeypatch.setattr(
        scheduler.window_meta,
        "active_window",
        lambda: WindowMeta(
            app_name="Live Fixture",
            bundle_id="app.openchronicle.LiveAXFixture",
            title=f"Private {title_marker.value}",
            pid=123,
            window_id=456,
            bounds=WindowBounds(x=0, y=0, width=640, height=480),
        ),
    )

    class ProviderThatWouldLeak:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool, **_kwargs):
            del focused_window_only
            raise AssertionError(f"provider exposed {url_marker.value}")

    with caplog.at_level(logging.INFO):
        path = scheduler.capture_once(cfg, ProviderThatWouldLeak(), trigger=None)

    assert path is None
    assert not list((ac_root / "capture-buffer").glob("*.json"))
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0

    scanner = SinkScanner([title_marker, url_marker])
    scanner.scan_files("capture_json", (ac_root / "capture-buffer").glob("*.json"))
    scanner.scan_text("logs", caplog.text)
    scanner.scan_sqlite(
        "sqlite_capture_fts",
        ac_root / "index.db",
        ["captures", "captures_fts"],
    )
    for sink in scanner.report().values():
        assert all(marker["hits"] == 0 for marker in sink["markers"])


def test_production_scheduler_denies_fixture_browser_url_before_screenshot_or_sinks(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openchronicle.capture import s1_parser, scheduler
    from openchronicle.capture.ax_models import AXCaptureResult
    from openchronicle.capture.window_meta import WindowBounds, WindowMeta
    from openchronicle.config import CaptureConfig
    from openchronicle.store import fts

    marker = Marker("https://forbidden.invalid/live-fixture-unit", "forbidden")
    bundle_id = "app.openchronicle.LiveAXFixture"
    identity = WindowMeta(
        app_name="Live Fixture",
        bundle_id=bundle_id,
        title="Allowed public title",
        pid=123,
        window_id=456,
        bounds=WindowBounds(x=0, y=0, width=640, height=480),
    )
    raw = {
        "timestamp": "2026-08-08T00:00:00Z",
        "window_meta": identity.to_capture_request(),
        "apps": [
            {
                "pid": 123,
                "name": "Live Fixture",
                "bundle_id": bundle_id,
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "Allowed public title",
                        "focused": True,
                        "elements": [
                            {
                                "role": "AXTextField",
                                "identifier": "oc-live-public-url-field",
                                "value": marker.value,
                            }
                        ],
                    }
                ],
            }
        ],
    }

    class Provider:
        available = True

        def capture_frontmost(self, *, focused_window_only: bool, **_kwargs):
            assert focused_window_only is True
            return AXCaptureResult(
                raw,
                raw["timestamp"],
                raw["apps"],
                {},
                tree_complete_verified=True,
                effective_max_depth=100,
            )

    monkeypatch.setattr(scheduler.window_meta, "active_window", lambda: identity)
    monkeypatch.setattr(
        scheduler.screenshot,
        "grab",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("URL exclusion must run before screenshot capture")
        ),
    )
    monkeypatch.setattr(
        s1_parser,
        "_BROWSER_BUNDLES",
        {*s1_parser._BROWSER_BUNDLES, bundle_id.casefold()},
    )
    monkeypatch.setitem(
        s1_parser._BROWSER_ADDRESS_FAMILY,
        bundle_id.casefold(),
        "live_fixture",
    )
    monkeypatch.setitem(
        s1_parser._ADDRESS_IDENTIFIERS,
        "live_fixture",
        frozenset({"oc-live-public-url-field"}),
    )
    monkeypatch.setitem(
        s1_parser._ADDRESS_LABELS,
        "live_fixture",
        frozenset(),
    )
    cfg = CaptureConfig(
        excluded_url_patterns=["forbidden.invalid"],
        include_screenshot=True,
    )
    observed_url_decisions: list[str] = []
    original_evaluate_url_candidate = scheduler.privacy_policy.evaluate_url_candidate

    def observe_url_policy(
        capture_cfg: CaptureConfig,
        *,
        url: object,
        scheme_known: bool,
    ):
        decision = original_evaluate_url_candidate(
            capture_cfg,
            url=url,
            scheme_known=scheme_known,
        )
        if url == marker.value:
            observed_url_decisions.append(decision.reason)
        return decision

    monkeypatch.setattr(
        scheduler.privacy_policy,
        "evaluate_url_candidate",
        observe_url_policy,
    )
    records: list[logging.LogRecord] = []

    class RecordHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = RecordHandler()
    old_level = scheduler.logger.level
    scheduler.logger.setLevel(logging.DEBUG)
    scheduler.logger.addHandler(handler)
    try:
        path = scheduler.capture_once(cfg, Provider(), trigger=None)
    finally:
        scheduler.logger.removeHandler(handler)
        scheduler.logger.setLevel(old_level)

    log_text = "\n".join(record.getMessage() for record in records)
    assert path is None
    assert observed_url_decisions == ["excluded_url"]
    assert "excluded_url" in log_text
    assert marker.value not in log_text
    assert not list((ac_root / "capture-buffer").glob("*.json"))
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0
