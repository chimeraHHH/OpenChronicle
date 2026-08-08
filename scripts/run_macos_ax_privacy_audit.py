#!/usr/bin/env python3
"""Run the opt-in, redaction-safe macOS live AX/privacy audit.

The live fixture and every OpenChronicle sink use a temporary root.  Raw AX
responses are inspected only in memory; the sole retained audit artifact is a
0600 JSON report containing hashes, counts, booleans, and stable reason codes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from live_ax_privacy import (
    REPORT_SCHEMA_VERSION,
    Marker,
    SinkScanner,
    analyze_exact_window_jpeg,
    marker_hit_count,
    verify_report,
    write_private_report,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "live" / "macos_ax_privacy"
DEFAULT_MANIFEST = FIXTURE_ROOT / "manifest.json"
BUILD_ROOT = FIXTURE_ROOT / ".build"
FIXTURE_APP = BUILD_ROOT / "OpenChronicleLiveAXFixture.app"
FIXTURE_EXECUTABLE = FIXTURE_APP / "Contents" / "MacOS" / "LiveAXFixture"
HELPER_EXECUTABLE = BUILD_ROOT / "mac-ax-helper"
BUNDLE_ID = "app.openchronicle.LiveAXFixture"
PUBLIC_TITLE = "OpenChronicle AX Audit — Public"


class AuditFailure(RuntimeError):
    """Failure with a stable, non-sensitive reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Checks:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    def set(
        self,
        check_id: str,
        status: str,
        *,
        observed_count: int = 0,
        reason: str = "",
    ) -> None:
        if status not in {"pass", "fail", "skipped"}:
            raise ValueError("invalid check status")
        item: dict[str, Any] = {
            "id": check_id,
            "status": status,
            "observed_count": max(0, int(observed_count)),
        }
        if reason:
            item["reason"] = reason
        self._items[check_id] = item

    def pass_if(self, check_id: str, condition: bool, *, observed_count: int = 0) -> None:
        self.set(
            check_id,
            "pass" if condition else "fail",
            observed_count=observed_count,
            reason="" if condition else "assertion_failed",
        )

    def skip(self, check_id: str, reason: str) -> None:
        self.set(check_id, "skipped", reason=reason)

    def fill_missing(self, check_ids: Sequence[str], reason: str) -> None:
        for check_id in check_ids:
            if check_id not in self._items:
                self.skip(check_id, reason)

    def report(self) -> list[dict[str, Any]]:
        return [self._items[key] for key in sorted(self._items)]


class FixtureController:
    def __init__(self, executable: Path, markers: Mapping[str, str], ipc_dir: Path) -> None:
        ipc_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        ipc_dir.chmod(0o700)
        self._marker_file = ipc_dir / "markers.json"
        self._command_file = ipc_dir / "commands"
        self._event_file = ipc_dir / "events"
        self._marker_file.write_text(
            json.dumps(markers, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        self._command_file.touch()
        self._event_file.touch()
        for path in (self._marker_file, self._command_file, self._event_file):
            path.chmod(0o600)
        self._marker_values = tuple(markers.values())
        self._event_offset = 0
        self._event_remainder = ""
        self._queued_events: list[dict[str, Any]] = []
        app_path = executable.parents[2]
        self._proc = subprocess.Popen(
            [
                "open",
                "-n",
                "-W",
                "-a",
                str(app_path),
                "-i",
                "/dev/null",
                "-o",
                "/dev/null",
                "--stderr",
                "/dev/null",
                "--env",
                f"OC_LIVE_AX_MARKER_FILE={self._marker_file}",
                "--env",
                f"OC_LIVE_AX_COMMAND_FILE={self._command_file}",
                "--env",
                f"OC_LIVE_AX_EVENT_FILE={self._event_file}",
            ],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )

    def wait_ready(self, timeout: float = 10.0) -> dict[str, Any]:
        return self.wait_event("ready", timeout=timeout)

    def command(self, command: str, expected_event: str = "ack", timeout: float = 5.0) -> dict:
        self.send(command)
        return self.wait_event(expected_event, timeout=timeout)

    def send(self, command: str) -> None:
        if self._proc.poll() is not None:
            raise AuditFailure("fixture_not_running")
        with self._command_file.open("a", encoding="utf-8") as handle:
            handle.write(command + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def wait_event(self, expected: str, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise AuditFailure("fixture_exited")
            self._read_events()
            for index, payload in enumerate(self._queued_events):
                if payload.get("event") == expected:
                    return self._queued_events.pop(index)
            time.sleep(0.02)
        raise AuditFailure(f"fixture_event_timeout_{expected}")

    def _read_events(self) -> None:
        try:
            with self._event_file.open("r", encoding="utf-8") as handle:
                handle.seek(self._event_offset)
                chunk = handle.read()
                self._event_offset = handle.tell()
        except OSError as exc:
            raise AuditFailure("fixture_event_file_unavailable") from exc
        if not chunk:
            return
        if any(marker in chunk for marker in self._marker_values):
            raise AuditFailure("fixture_event_file_contained_marker")
        self._event_remainder += chunk
        lines = self._event_remainder.split("\n")
        self._event_remainder = lines.pop()
        for line in lines:
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditFailure("fixture_event_file_invalid_json") from exc
            if not isinstance(payload, dict):
                raise AuditFailure("fixture_event_file_invalid_payload")
            self._queued_events.append(payload)

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                self.send("quit")
                self._proc.wait(timeout=3.0)
            except (AuditFailure, subprocess.TimeoutExpired):
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=2.0)


class PromptProbe:
    """In-memory LLM substitute that scans prompts and returns safe fixtures."""

    def __init__(self, scanner: SinkScanner) -> None:
        self.scanner = scanner
        self.calls: dict[str, int] = {}

    def __call__(
        self,
        _cfg: Any,
        stage: str,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
    ) -> Any:
        del tools, json_mode
        sink = {
            "timeline": "timeline_prompt",
            "reducer": "session_prompt",
            "classifier": "classifier_prompt",
        }.get(stage, "other_prompt")
        serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        self.scanner.scan_text(sink, serialized)
        self.calls[stage] = self.calls.get(stage, 0) + 1

        if stage == "timeline":
            content = json.dumps(
                {
                    "entries": [
                        "[OpenChronicle Live AX Fixture] exercised public fields, involving —"
                    ]
                }
            )
        elif stage == "reducer":
            content = json.dumps(
                {
                    "summary": "Completed a local AX privacy audit.",
                    "sub_tasks": [
                        "[00:00-00:01, OpenChronicle Live AX Fixture] "
                        "exercised public fields, involving —"
                    ],
                }
            )
        else:
            # No tools: the production classifier exits without mutating
            # memory, after its exact prompt has been inspected in memory.
            content = "Audit prompt observed; no durable mutation requested."
        return _response(content)


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Response:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


def _response(content: str) -> _Response:
    return _Response(content)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_checked(args: Sequence[str], *, code: str) -> None:
    try:
        result = subprocess.run(
            list(args),
            cwd=REPO_ROOT,
            capture_output=True,
            text=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditFailure(code) from exc
    if result.returncode != 0:
        raise AuditFailure(code)


def _build_binaries() -> dict[str, str]:
    BUILD_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    contents = FIXTURE_APP / "Contents"
    macos_dir = contents / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(FIXTURE_ROOT / "Info.plist", contents / "Info.plist")

    _run_checked(
        [
            "swiftc",
            str(FIXTURE_ROOT / "LiveAXFixture.swift"),
            "-o",
            str(FIXTURE_EXECUTABLE),
            "-O",
            "-swift-version",
            "5",
            "-framework",
            "AppKit",
            "-framework",
            "ApplicationServices",
        ],
        code="fixture_compile_failed",
    )
    FIXTURE_EXECUTABLE.chmod(0o700)
    _run_checked(
        ["codesign", "--force", "--sign", "-", str(FIXTURE_APP)],
        code="fixture_codesign_failed",
    )

    helper_source = REPO_ROOT / "resources" / "mac-ax-helper.swift"
    helper_source_hash = _sha256_file(helper_source)
    helper_stamp = BUILD_ROOT / "mac-ax-helper.source.sha256"
    try:
        stamped_hash = helper_stamp.read_text(encoding="ascii").strip()
    except OSError:
        stamped_hash = ""
    helper_stale = (
        not HELPER_EXECUTABLE.is_file()
        or stamped_hash != helper_source_hash
    )
    if helper_stale:
        arch = "arm64" if platform.machine() in {"arm64", "aarch64"} else "x86_64"
        _run_checked(
            [
                "swiftc",
                str(helper_source),
                "-o",
                str(HELPER_EXECUTABLE),
                "-O",
                "-target",
                f"{arch}-apple-macos12.0",
                "-swift-version",
                "5",
            ],
            code="ax_helper_compile_failed",
        )
        HELPER_EXECUTABLE.chmod(0o700)
        _run_checked(
            [
                "codesign",
                "--force",
                "--sign",
                "-",
                "--identifier",
                "app.openchronicle.LiveAXAuditHelper",
                str(HELPER_EXECUTABLE),
            ],
            code="ax_helper_codesign_failed",
        )
        helper_stamp.write_text(helper_source_hash + "\n", encoding="ascii")
        helper_stamp.chmod(0o600)
    return {
        "fixture_source_sha256": _sha256_file(FIXTURE_ROOT / "LiveAXFixture.swift"),
        "fixture_binary_sha256": _sha256_file(FIXTURE_EXECUTABLE),
        "helper_source_sha256": helper_source_hash,
        "helper_binary_sha256": _sha256_file(HELPER_EXECUTABLE),
    }


def _generate_markers() -> tuple[list[Marker], dict[str, str]]:
    normal = f"OC_LIVE_NORMAL_{secrets.token_hex(16)}"
    public_url = f"https://public.invalid/oc-live/{secrets.token_hex(16)}"
    secure = f"OC_LIVE_SECURE_{secrets.token_hex(16)}"
    excluded_title = f"OC_LIVE_EXCLUDED_{secrets.token_hex(16)}"
    private_url = f"https://forbidden.invalid/oc-live/{secrets.token_hex(16)}"
    markers = [
        Marker(normal, "control"),
        Marker(public_url, "control"),
        Marker(secure, "forbidden"),
        Marker(excluded_title, "forbidden"),
        Marker(private_url, "forbidden"),
    ]
    environment = {
        "OC_LIVE_AX_NORMAL": normal,
        "OC_LIVE_AX_PUBLIC_URL": public_url,
        "OC_LIVE_AX_SECURE": secure,
        "OC_LIVE_AX_EXCLUDED_TITLE": excluded_title,
        "OC_LIVE_AX_PRIVATE_URL": private_url,
    }
    return markers, environment


def _wait_for_window(window_meta: Any, expected_title: str, *, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = window_meta.active_window()
        if meta.bundle_id == BUNDLE_ID and meta.title == expected_title:
            return True
        time.sleep(0.05)
    return False


def _helper_call(*args: str) -> tuple[int, dict[str, Any] | None]:
    try:
        proc = subprocess.run(
            [str(HELPER_EXECUTABLE), *args],
            capture_output=True,
            text=True,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, None
    if proc.returncode != 0:
        return proc.returncode, None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return 1, None
    return 0, payload if isinstance(payload, dict) else None


def _serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _all_windows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    apps = payload.get("apps")
    if not isinstance(apps, list) or len(apps) != 1 or not isinstance(apps[0], dict):
        return []
    windows = apps[0].get("windows")
    if not isinstance(windows, list):
        return []
    return [window for window in windows if isinstance(window, dict)]


def _fake_helper(
    path: Path,
    exit_code: int,
    *,
    stderr_marker: str,
    crash: bool = False,
) -> Path:
    escaped = stderr_marker.replace("'", "'\"'\"'")
    suffix = "kill -KILL $$" if crash else f"exit {exit_code}"
    body = f"#!/bin/sh\nprintf '%s\\n' '{escaped}' >&2\n{suffix}\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def _capture_failure_is_content_closed(
    scheduler: Any,
    capture_cfg: Any,
    provider: Any,
    markers: Sequence[Marker],
) -> tuple[bool, Path | None]:
    path = scheduler.capture_once(capture_cfg, provider, trigger=None)
    if path is None:
        return True, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, path
    raw = _serialized(data)
    no_marker = all(marker.value not in raw for marker in markers)
    no_ax = "ax_tree" not in data and "focused_element" not in data
    no_screenshot = "screenshot" not in data
    return no_marker and no_ax and no_screenshot, path


def _derive_sinks(
    cfg: Any,
    capture_path: Path,
    scanner: SinkScanner,
    checks: Checks,
) -> None:
    from openchronicle.store import fts
    from openchronicle.timeline import aggregator
    from openchronicle.writer import classifier, session_reducer
    from openchronicle.writer import llm as llm_mod

    try:
        capture = json.loads(capture_path.read_text(encoding="utf-8"))
        captured_at = datetime.fromisoformat(str(capture["timestamp"]))
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise AuditFailure("captured_fixture_unreadable") from exc

    start = captured_at - timedelta(seconds=1)
    end = captured_at + timedelta(seconds=1)
    probe = PromptProbe(scanner)
    original_call = llm_mod.call_llm
    llm_mod.call_llm = probe
    try:
        with fts.cursor() as conn:
            block = aggregator.produce_block_for_window(
                cfg,
                conn,
                start=start,
                end=end,
                parsed_captures=[(capture_path, capture)],
            )
        if block is None:
            raise AuditFailure("timeline_block_not_produced")

        session_id = f"live-ax-{secrets.token_hex(8)}"
        reduced = session_reducer.reduce_session(
            cfg,
            session_id=session_id,
            start_time=captured_at - timedelta(milliseconds=500),
            end_time=captured_at + timedelta(milliseconds=500),
        )
        if not reduced.written or not reduced.path or not reduced.entry_id:
            raise AuditFailure("session_entry_not_produced")

        classifier.classify_window(
            cfg,
            session_id=session_id,
            event_daily_path=reduced.path,
            start=captured_at - timedelta(minutes=1),
            end=datetime.now().astimezone() + timedelta(minutes=1),
            focus_entry_ids=[reduced.entry_id],
        )
    finally:
        llm_mod.call_llm = original_call

    checks.pass_if(
        "timeline_prompt_observed",
        probe.calls.get("timeline", 0) >= 1,
        observed_count=probe.calls.get("timeline", 0),
    )
    checks.pass_if(
        "session_prompt_observed",
        probe.calls.get("reducer", 0) >= 1,
        observed_count=probe.calls.get("reducer", 0),
    )
    checks.pass_if(
        "classifier_prompt_observed",
        probe.calls.get("classifier", 0) >= 1,
        observed_count=probe.calls.get("classifier", 0),
    )


def _scan_persisted_sinks(root: Path, scanner: SinkScanner) -> dict[str, dict[str, Any]]:
    scanner.scan_files("capture_json", (root / "capture-buffer").glob("*.json"))
    scanner.scan_files("logs", (root / "logs").glob("*.log*"))
    db_path = root / "index.db"
    scanner.scan_sqlite("sqlite_capture_fts", db_path, ["captures", "captures_fts"])
    scanner.scan_sqlite("timeline", db_path, ["timeline_blocks"])
    scanner.scan_sqlite(
        "session",
        db_path,
        ["sessions", "entries", "entries_fts", "classifier_jobs"],
    )
    scanner.scan_files("session", (root / "memory").glob("*.md"))
    for sink in ("timeline_prompt", "session_prompt", "classifier_prompt"):
        # Create an explicit zero-unit record when an earlier prerequisite was
        # unavailable.  The manifest verifier then reports sink_not_observed.
        if sink not in scanner.report():
            scanner.scan_text(sink, "", units=0)
    return scanner.report()


def _skip_ax_dependent_checks(checks: Checks, reason: str) -> None:
    for check_id in (
        "two_visible_windows",
        "focused_window_scoped_ax",
        "scheduler_exact_identity_capture",
        "url_metadata_only_persisted",
        "exact_window_screenshot_verified",
        "exact_window_public_color_observed",
        "exact_window_sibling_color_absent",
        "secure_text_redacted",
        "normal_text_observed",
        "url_like_marker_observed",
        "timeline_prompt_observed",
        "session_prompt_observed",
        "classifier_prompt_observed",
    ):
        checks.skip(check_id, reason)


def _exercise_live(
    root: Path,
    markers: Sequence[Marker],
    environment: Mapping[str, str],
    scanner: SinkScanner,
    checks: Checks,
) -> None:
    # Import only after OPENCHRONICLE_ROOT points at the temporary audit root.
    from openchronicle import logger as logger_mod
    from openchronicle.capture import ax_capture, s1_parser, scheduler, screenshot, window_meta
    from openchronicle.config import CaptureConfig, Config
    from openchronicle.privacy import policy as privacy_policy

    logger_mod.setup(console=False, verbose=True)
    cfg = Config()
    private_title = f"OpenChronicle AX Audit — Private {environment['OC_LIVE_AX_EXCLUDED_TITLE']}"
    cfg.capture = CaptureConfig(
        allowed_bundle_ids=[BUNDLE_ID],
        excluded_window_title_patterns=[environment["OC_LIVE_AX_EXCLUDED_TITLE"]],
        excluded_url_patterns=["forbidden.invalid"],
        deny_unknown_windows=True,
        include_screenshot=False,
        ax_depth=100,
        ax_timeout_seconds=3,
    )
    provider = ax_capture.MacAXHelperProvider(
        helper_path=HELPER_EXECUTABLE,
        depth=cfg.capture.ax_depth,
        timeout=cfg.capture.ax_timeout_seconds,
    )

    fixture_bundle_key = BUNDLE_ID.casefold()
    fixture_was_browser = fixture_bundle_key in s1_parser._BROWSER_BUNDLES
    fixture_address_family = s1_parser._BROWSER_ADDRESS_FAMILY.get(fixture_bundle_key)
    fixture_identifier_rules = s1_parser._ADDRESS_IDENTIFIERS.get("live_fixture")
    fixture_label_rules = s1_parser._ADDRESS_LABELS.get("live_fixture")
    fixture = FixtureController(FIXTURE_EXECUTABLE, environment, root / "fixture-ipc")
    s1_parser._BROWSER_BUNDLES.add(fixture_bundle_key)
    s1_parser._BROWSER_ADDRESS_FAMILY[fixture_bundle_key] = "live_fixture"
    s1_parser._ADDRESS_IDENTIFIERS["live_fixture"] = frozenset(
        {"oc-live-public-url-field"}
    )
    s1_parser._ADDRESS_LABELS["live_fixture"] = frozenset()
    capture_paths: list[Path] = []
    try:
        ready = fixture.wait_ready()
        checks.pass_if(
            "fixture_ready",
            ready.get("window_count") == 2 and ready.get("bundle_id") == BUNDLE_ID,
            observed_count=int(ready.get("window_count") or 0),
        )

        fixture.command("public.normal")
        public_active = _wait_for_window(window_meta, PUBLIC_TITLE)
        if not public_active:
            raise AuditFailure("fixture_window_metadata_unavailable")

        # Helper failures are exercised through the production provider and
        # scheduler before requiring real AX permission.
        stderr_canary = next(
            marker.value for marker in markers if marker.classification == "forbidden"
        )
        denied_helper = _fake_helper(
            root / "deny-helper",
            2,
            stderr_marker=stderr_canary,
        )
        denied_provider = ax_capture.MacAXHelperProvider(
            helper_path=denied_helper, depth=8, timeout=1
        )
        denied_ok, denied_path = _capture_failure_is_content_closed(
            scheduler, cfg.capture, denied_provider, markers
        )
        if denied_path is not None:
            capture_paths.append(denied_path)
        checks.pass_if(
            "helper_permission_denial_content_fail_closed",
            denied_ok,
            observed_count=1,
        )

        crash_helper = _fake_helper(
            root / "crash-helper",
            1,
            stderr_marker=stderr_canary,
            crash=True,
        )
        crash_provider = ax_capture.MacAXHelperProvider(
            helper_path=crash_helper, depth=8, timeout=1
        )
        crash_ok, crash_path = _capture_failure_is_content_closed(
            scheduler, cfg.capture, crash_provider, markers
        )
        if crash_path is not None:
            capture_paths.append(crash_path)
        checks.pass_if("helper_crash_content_fail_closed", crash_ok, observed_count=1)

        # Title policy must reject the private window before the helper is
        # invoked. URL policy is exercised separately in the allowed window.
        fixture.command("private.secure")
        private_active = _wait_for_window(window_meta, private_title)
        before = len(list((root / "capture-buffer").glob("*.json")))
        denied_title_path = scheduler.capture_once(cfg.capture, provider, trigger=None)
        after = len(list((root / "capture-buffer").glob("*.json")))
        checks.pass_if(
            "excluded_title_denied_before_capture",
            private_active and denied_title_path is None and before == after,
            observed_count=1,
        )

        # Keep the title allowed and replace the public address field with the
        # forbidden URL. Temporarily registering this dedicated fixture's exact
        # address-field identifier exercises the same stable-ID adapter and
        # parser -> URL policy boundary as a supported browser. The mutation is
        # process-local and restored in ``finally`` below.
        fixture.command("public.forbidden-url")
        private_url_active = _wait_for_window(window_meta, PUBLIC_TITLE)
        before = len(list((root / "capture-buffer").glob("*.json")))
        url_policy_observations: list[str] = []
        original_evaluate_url_candidate = privacy_policy.evaluate_url_candidate

        def observe_url_policy(
            capture_cfg: Any,
            *,
            url: object,
            scheme_known: bool,
        ):
            decision = original_evaluate_url_candidate(
                capture_cfg,
                url=url,
                scheme_known=scheme_known,
            )
            if url == environment["OC_LIVE_AX_PRIVATE_URL"]:
                url_policy_observations.append(decision.reason)
            return decision

        privacy_policy.evaluate_url_candidate = observe_url_policy
        try:
            denied_url_path = scheduler.capture_once(cfg.capture, provider, trigger=None)
        finally:
            privacy_policy.evaluate_url_candidate = original_evaluate_url_candidate
        after = len(list((root / "capture-buffer").glob("*.json")))
        checks.pass_if(
            "excluded_url_denied_before_capture",
            private_url_active
            and denied_url_path is None
            and before == after
            and url_policy_observations == ["excluded_url"],
            observed_count=len(url_policy_observations),
        )

        fixture.command("public.url")
        fixture.command("public.normal")
        if not _wait_for_window(window_meta, PUBLIC_TITLE):
            raise AuditFailure("fixture_public_refocus_failed")

        rc, all_payload = _helper_call("--app-name", BUNDLE_ID, "--depth", "100")
        if rc == 2:
            _skip_ax_dependent_checks(checks, "accessibility_permission_denied")
        elif rc != 0 or all_payload is None:
            _skip_ax_dependent_checks(checks, "ax_helper_failed")
        else:
            all_text = _serialized(all_payload)
            scanner.scan_text("ephemeral_ax", all_text)
            windows = _all_windows(all_payload)
            checks.pass_if("two_visible_windows", len(windows) == 2, observed_count=len(windows))

            secure = environment["OC_LIVE_AX_SECURE"]
            redacted_count = all_text.count("[REDACTED]")
            checks.pass_if(
                "secure_text_redacted",
                secure not in all_text and redacted_count >= 1,
                observed_count=redacted_count,
            )

            fixture.command("public.normal")
            if not _wait_for_window(window_meta, PUBLIC_TITLE):
                raise AuditFailure("fixture_public_refocus_failed")
            focused_rc, focused_payload = _helper_call(
                "--app-name",
                BUNDLE_ID,
                "--focused-window-only",
                "--depth",
                "100",
            )
            focused_text = _serialized(focused_payload) if focused_payload is not None else ""
            scanner.scan_text("ephemeral_ax", focused_text)
            focused_windows = _all_windows(focused_payload)
            forbidden_absent = all(
                marker.value not in focused_text
                for marker in markers
                if marker.classification == "forbidden"
            )
            checks.pass_if(
                "focused_window_scoped_ax",
                focused_rc == 0
                and len(focused_windows) == 1
                and focused_windows[0].get("title") == PUBLIC_TITLE
                and forbidden_absent,
                observed_count=len(focused_windows),
            )

            normal = environment["OC_LIVE_AX_NORMAL"]
            public_url = environment["OC_LIVE_AX_PUBLIC_URL"]
            checks.pass_if(
                "normal_text_observed",
                normal in focused_text,
                observed_count=focused_text.count(normal),
            )
            checks.pass_if(
                "url_like_marker_observed",
                public_url in focused_text,
                observed_count=focused_text.count(public_url),
            )

            screenshot_meta = window_meta.active_window()
            shot = (
                screenshot.grab(
                    target=screenshot_meta,
                    max_width=cfg.capture.screenshot_max_width,
                    jpeg_quality=cfg.capture.screenshot_jpeg_quality,
                )
                if screenshot_meta.capture_ready
                else None
            )
            screenshot_verified = (
                shot is not None
                and shot.width > 0
                and shot.height > 0
                and screenshot_meta.same_capture_target(shot.window_meta)
            )
            pixel_metrics = analyze_exact_window_jpeg(shot, screenshot_meta)
            pixel_count = int(pixel_metrics["pixel_count"])
            public_pixels = int(pixel_metrics["public_pixels"])
            private_pixels = int(pixel_metrics["private_pixels"])
            public_color_verified = (
                pixel_count > 0
                and public_pixels >= max(1, int(pixel_count * 0.08))
                and int(pixel_metrics["public_samples"]) >= 4
            )
            sibling_color_absent = (
                pixel_count > 0
                and private_pixels <= max(1, int(pixel_count * 0.001))
                and int(pixel_metrics["private_samples"]) == 0
            )
            checks.pass_if(
                "exact_window_public_color_observed",
                public_color_verified,
                observed_count=public_pixels,
            )
            checks.pass_if(
                "exact_window_sibling_color_absent",
                sibling_color_absent,
                observed_count=private_pixels,
            )
            checks.pass_if(
                "exact_window_screenshot_verified",
                screenshot_verified
                and bool(pixel_metrics["jpeg_valid"])
                and bool(pixel_metrics["aspect_matches"])
                and public_color_verified
                and sibling_color_absent,
                observed_count=(shot.width * shot.height if shot is not None else 0),
            )
            # Drop the in-memory base64 immediately. It is never attached to a
            # capture and is absent from every scanned/retained artifact.
            shot = None

            exact_meta = window_meta.active_window()
            capture_path = scheduler.capture_once(cfg.capture, provider, trigger=None)
            if capture_path is None:
                raise AuditFailure("allowed_capture_not_persisted")
            capture_paths.append(capture_path)
            persisted_text = capture_path.read_text(encoding="utf-8")
            persisted_capture = json.loads(persisted_text)
            persisted_meta = persisted_capture.get("window_meta") or {}
            privacy_meta = persisted_capture.get("privacy") or {}
            metadata_only = (
                normal not in persisted_text
                and public_url in persisted_text
                and "ax_tree" not in persisted_capture
                and "focused_element" not in persisted_capture
                and "screenshot" not in persisted_capture
                and persisted_capture.get("visible_text") == ""
                and persisted_capture.get("url") == public_url
                and persisted_meta.get("title") == ""
                and privacy_meta.get("content_mode") == "url_metadata_only"
            )
            checks.pass_if(
                "url_metadata_only_persisted",
                metadata_only,
                observed_count=1 if metadata_only else 0,
            )
            checks.pass_if(
                "scheduler_exact_identity_capture",
                exact_meta.capture_ready
                and persisted_meta.get("pid") == exact_meta.pid
                and persisted_meta.get("window_id") == exact_meta.window_id
                and persisted_meta.get("bounds") == exact_meta.bounds.to_dict()
                and persisted_meta.get("title") == "",
                observed_count=1,
            )
            _derive_sinks(cfg, capture_path, scanner, checks)

        # Race production metadata/policy/helper checks against 48 fast focus
        # changes.  Any resulting capture must contain no forbidden marker.
        fixture.send("rapid")
        fixture.wait_event("rapid_started", timeout=3.0)
        rapid_paths: list[Path] = []
        attempts = 0
        deadline = time.monotonic() + 2.2
        while time.monotonic() < deadline and attempts < 8:
            attempts += 1
            path = scheduler.capture_once(cfg.capture, provider, trigger=None)
            if path is not None:
                rapid_paths.append(path)
        fixture.wait_event("rapid_finished", timeout=4.0)
        capture_paths.extend(rapid_paths)
        rapid_closed = True
        for path in rapid_paths:
            raw = path.read_text(encoding="utf-8")
            if any(
                marker.value in raw for marker in markers if marker.classification == "forbidden"
            ):
                rapid_closed = False
                break
        checks.pass_if(
            "rapid_focus_fail_closed",
            attempts >= 1 and rapid_closed,
            observed_count=attempts,
        )

        screenshots_absent = True
        for path in capture_paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                screenshots_absent = False
                break
            if "screenshot" in data:
                screenshots_absent = False
                break
        checks.pass_if(
            "screenshots_remained_disabled",
            screenshots_absent and cfg.capture.include_screenshot is False,
            observed_count=len(capture_paths),
        )
    finally:
        fixture.close()
        if not fixture_was_browser:
            s1_parser._BROWSER_BUNDLES.discard(fixture_bundle_key)
        if fixture_address_family is None:
            s1_parser._BROWSER_ADDRESS_FAMILY.pop(fixture_bundle_key, None)
        else:
            s1_parser._BROWSER_ADDRESS_FAMILY[fixture_bundle_key] = fixture_address_family
        if fixture_identifier_rules is None:
            s1_parser._ADDRESS_IDENTIFIERS.pop("live_fixture", None)
        else:
            s1_parser._ADDRESS_IDENTIFIERS["live_fixture"] = fixture_identifier_rules
        if fixture_label_rules is None:
            s1_parser._ADDRESS_LABELS.pop("live_fixture", None)
        else:
            s1_parser._ADDRESS_LABELS["live_fixture"] = fixture_label_rules
        logging.shutdown()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditFailure("manifest_unreadable") from exc
    if not isinstance(data, dict):
        raise AuditFailure("manifest_not_object")
    return data


def run(report_path: Path, manifest_path: Path) -> tuple[dict[str, Any], list[Marker]]:
    manifest = _load_json(manifest_path)
    markers, environment = _generate_markers()
    scanner = SinkScanner(markers)
    checks = Checks()
    build_hashes: dict[str, str] = {}
    fatal_code = ""
    sinks: dict[str, dict[str, Any]] = {}
    temp_removed = False

    try:
        build_hashes = _build_binaries()
    except AuditFailure as exc:
        fatal_code = exc.code

    temporary = tempfile.TemporaryDirectory(prefix="openchronicle-live-ax-")
    root = Path(temporary.name) / "root"
    root.mkdir(mode=0o700)
    original_root = os.environ.get("OPENCHRONICLE_ROOT")
    original_helper = os.environ.get("OPENCHRONICLE_AX_HELPER")
    os.environ["OPENCHRONICLE_ROOT"] = str(root)
    os.environ["OPENCHRONICLE_AX_HELPER"] = str(HELPER_EXECUTABLE)
    try:
        if not fatal_code:
            try:
                _exercise_live(root, markers, environment, scanner, checks)
            except AuditFailure as exc:
                fatal_code = exc.code
            except Exception as exc:  # noqa: BLE001
                fatal_code = f"unexpected_{type(exc).__name__}"
        logging.shutdown()
        sinks = _scan_persisted_sinks(root, scanner)
    finally:
        if original_root is None:
            os.environ.pop("OPENCHRONICLE_ROOT", None)
        else:
            os.environ["OPENCHRONICLE_ROOT"] = original_root
        if original_helper is None:
            os.environ.pop("OPENCHRONICLE_AX_HELPER", None)
        else:
            os.environ["OPENCHRONICLE_AX_HELPER"] = original_helper
        temporary.cleanup()
        temp_removed = not Path(temporary.name).exists()

    forbidden_zero = all(
        marker_hit_count(sinks, sink, classification="forbidden") == 0
        for sink in manifest.get("forbidden_zero_sinks", [])
        if isinstance(sink, str)
    )
    checks.pass_if(
        "forbidden_markers_absent_from_sinks",
        forbidden_zero,
        observed_count=sum(
            marker_hit_count(sinks, sink, classification="forbidden")
            for sink in manifest.get("forbidden_zero_sinks", [])
            if isinstance(sink, str)
        ),
    )
    if fatal_code:
        checks.set("fatal", "fail", reason=fatal_code)
    checks.fill_missing(
        [check_id for check_id in manifest.get("required_checks", []) if isinstance(check_id, str)],
        fatal_code or "prerequisite_unavailable",
    )

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "complete",
        "opt_in": True,
        "fixture": build_hashes,
        "marker_digests": sorted(marker.sha256 for marker in markers),
        "checks": checks.report(),
        "sinks": sinks,
        "artifact_policy": {
            "plaintext_markers_retained": 0,
            "raw_ax_payloads_retained": 0,
            "temporary_root_removed": temp_removed,
            "report_mode": "sha256_count_redacted_only",
        },
    }
    violations = verify_report(report, manifest)
    if fatal_code or violations:
        report["status"] = "incomplete"
        report["verification_codes"] = sorted(set(violations))
    # The path is intentionally absent from the report; only the caller sees it.
    write_private_report(report_path, report, markers)
    return report, markers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--acknowledge-live-ax", action="store_true")
    parser.add_argument("--acknowledge-production-capture-paused", action="store_true")
    args = parser.parse_args()

    if platform.system() != "Darwin":
        parser.error("the live AX audit requires macOS")
    if not args.acknowledge_live_ax:
        parser.error("pass --acknowledge-live-ax to allow fixture windows and AX access")
    if not args.acknowledge_production_capture_paused:
        parser.error(
            "pause/stop the real OpenChronicle daemon, then pass "
            "--acknowledge-production-capture-paused"
        )

    report, _markers = run(args.report.resolve(), args.manifest.resolve())
    print(f"live AX privacy audit: {report['status']}; redacted report written")
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
