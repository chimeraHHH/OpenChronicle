from __future__ import annotations

import contextlib
import copy
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_LIBRARY_PATH = Path(__file__).resolve().parents[3] / "scripts" / "runtime_reliability_lib.py"
_SPEC = importlib.util.spec_from_file_location("runtime_reliability_lib", _LIBRARY_PATH)
assert _SPEC is not None and _SPEC.loader is not None
reliability = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = reliability
_SPEC.loader.exec_module(reliability)


def test_replay_repairs_faults_and_is_idempotent(tmp_path: Path) -> None:
    result = reliability.run_replay_audit(tmp_path / "replay", count=32)

    assert result["run_status"] == "passed"
    assert result["model_calls"] == 0
    assert result["visible_capture_count"] == 31
    assert result["fts_row_count"] == 31
    assert result["actual_visible_set_digest"] == result["expected_visible_set_digest"]
    assert result["faults"] == {
        "missing_projection_rows": 2,
        "stale_projection_rows": 1,
        "orphan_capture_temps": 1,
        "tombstoned_capture_files": 1,
    }
    assert all(result["checks"].values())
    assert result["metrics"]["final"]["owned_temp_file_count"] == 0


def test_short_soak_passes_functionally_but_cannot_meet_24h(tmp_path: Path) -> None:
    manifest = reliability.load_manifest()
    result = reliability.run_soak_audit(
        tmp_path / "soak",
        duration_seconds=0.4,
        sample_interval_seconds=0.05,
        work_interval_seconds=0.02,
        manifest=manifest,
    )

    assert result["run_status"] == "passed"
    assert result["worker"]["completed_requested_duration"] is True
    assert result["worker"]["model_calls"] == 0
    assert result["resource_summary"]["coverage_complete"] is True
    assert result["resource_summary"]["within_limits"] is True
    assert result["resource_summary"]["metrics"]["child_process_count"]["maximum"] == 0
    assert result["sample_timeline"]["complete"] is True


def test_soak_stops_its_owned_worker_on_resource_limit_breach(tmp_path: Path) -> None:
    manifest = copy.deepcopy(reliability.load_manifest())
    manifest["limits"]["max_rss_bytes"] = 1

    result = reliability.run_soak_audit(
        tmp_path / "bounded-soak",
        duration_seconds=5,
        sample_interval_seconds=0.05,
        work_interval_seconds=0.02,
        manifest=manifest,
    )

    assert result["run_status"] == "failed"
    assert "rss_bytes" in result["process"]["resource_limit_breaches"]
    assert result["process"]["stop_disposition"] in {"terminated", "killed"}
    assert result["process"]["timed_out"] is False


def test_stop_owned_process_kills_term_resistant_descendant_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); "
        "time.sleep(5)"
    )
    leader_code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]],"
        "stdout=subprocess.PIPE,text=True); "
        "assert child.stdout.readline().strip() == 'ready'; "
        "print(child.pid, flush=True); "
        "time.sleep(5)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader_code, child_code],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    owned_pgid = process.pid
    signals: list[int] = []
    real_signal_owned_group = reliability._signal_owned_group

    def signal_while_leader_is_unreaped(pgid: int, signum: int) -> None:
        assert process.returncode is None
        signals.append(signum)
        real_signal_owned_group(pgid, signum)

    monkeypatch.setattr(
        reliability,
        "_signal_owned_group",
        signal_while_leader_is_unreaped,
    )
    try:
        disposition = reliability._stop_owned_process(
            process,
            owned_pgid=owned_pgid,
            grace_seconds=0.1,
        )

        assert disposition == "killed"
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert process.returncode == -signal.SIGTERM
        table = reliability._process_table()
        assert table is not None
        assert child_pid not in table
        assert all(pgid != owned_pgid for pgid, _state in table.values())
    finally:
        # If an assertion or implementation error interrupts the protocol,
        # the unreaped leader still pins this exact test-owned PGID.
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(owned_pgid, signal.SIGKILL)
            process.wait(timeout=5)


def test_unexpected_worker_exit_still_drains_its_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "unexpected-worker-child.pid"
    child_code = "import time; print('ready', flush=True); time.sleep(5)"
    leader_code = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]],"
        "stdout=subprocess.PIPE,text=True); "
        "assert child.stdout.readline().strip() == 'ready'; "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8'); "
        "time.sleep(0.2); "
        "raise SystemExit(7)"
    )
    command = [
        sys.executable,
        "-c",
        leader_code,
        str(child_pid_path),
        child_code,
    ]
    monkeypatch.setattr(
        reliability,
        "_soak_worker_command",
        lambda **_kwargs: command,
    )
    manifest = copy.deepcopy(reliability.load_manifest())
    # This test targets unexpected-exit cleanup, not the sampled no-child gate.
    manifest["limits"]["max_child_process_count"] = 1

    result = reliability.run_soak_audit(
        tmp_path / "unexpected-exit-soak",
        duration_seconds=2,
        sample_interval_seconds=0.05,
        work_interval_seconds=0.02,
        manifest=manifest,
    )

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    assert result["run_status"] == "failed"
    assert result["process"]["returncode"] == 7
    assert result["process"]["stop_disposition"] == "terminated"
    table = reliability._process_table()
    assert table is not None
    assert child_pid not in table


def test_report_verifier_recomputes_incomplete_status_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    manifest = reliability.load_manifest()
    report = reliability.run_full_audit(
        tmp_path / "workspace",
        replay_count=12,
        soak_duration_seconds=0.3,
        sample_interval_seconds=0.05,
        work_interval_seconds=0.02,
        manifest=manifest,
    )

    verified = reliability.verify_report(report, manifest)
    assert verified["valid"] is True
    assert verified["evaluation"]["status"] == "incomplete"
    assert report["status"] == "incomplete"
    assert report["acceptance"] == {
        "complete": False,
        "storage_harness_complete": False,
        "replay_meets_10000_minimum": False,
        "soak_meets_24h_minimum": False,
        "production_daemon_queue_measured": False,
    }
    encoded = json.dumps(report, sort_keys=True)
    assert "deterministic-replay-record" not in encoded
    assert "deterministic-focused-record" not in encoded

    claimed_complete = copy.deepcopy(report)
    claimed_complete["status"] = "complete"
    claimed_complete["acceptance"]["complete"] = True
    assert reliability.verify_report(claimed_complete, manifest)["valid"] is False

    forbidden = copy.deepcopy(report)
    forbidden["diagnostic"] = {"visible_text": "must never be reportable"}
    forbidden_result = reliability.verify_report(forbidden, manifest)
    assert forbidden_result["valid"] is False
    assert forbidden_result["forbidden_key_paths"] == ["$.diagnostic.visible_text"]

    unknown = copy.deepcopy(report)
    unknown["diagnostic_code"] = 7
    unknown_result = reliability.verify_report(unknown, manifest)
    assert unknown_result["valid"] is False
    assert unknown_result["unknown_key_paths"] == ["$.diagnostic_code"]

    typed_payload_channel = copy.deepcopy(report)
    typed_payload_channel["soak"]["samples"][0]["shm_bytes"] = "SECRET_PAYLOAD"
    typed_result = reliability.verify_report(typed_payload_channel, manifest)
    assert typed_result["valid"] is False
    assert typed_result["fixed_values_valid"] is False

    malformed = copy.deepcopy(report)
    malformed["soak"]["observed_duration_ns"] = "not-a-number"
    assert reliability.verify_report(malformed, manifest)["valid"] is False
    assert reliability.verify_report([], manifest)["valid"] is False

    forged_24h = copy.deepcopy(report)
    duration_ns = 86_400 * 1_000_000_000
    forged_24h["soak"]["requested_duration_ns"] = duration_ns
    forged_24h["soak"]["observed_duration_ns"] = duration_ns
    forged_24h["soak"]["worker"]["requested_duration_ns"] = duration_ns
    forged_24h["soak"]["worker"]["observed_duration_ns"] = duration_ns
    forged_24h["soak"]["sample_timeline"]["complete"] = True
    forged_result = reliability.verify_report(forged_24h, manifest)
    assert forged_result["valid"] is False
    assert forged_result["evaluation"]["recalculated_sample_timeline"]["complete"] is False

    forged_replay = copy.deepcopy(report)
    forged_replay["replay"]["source_set_digest"] = "a" * 64
    forged_replay["replay"]["expected_visible_set_digest"] = "b" * 64
    forged_replay["replay"]["actual_visible_set_digest"] = "b" * 64
    replay_result = reliability.verify_report(forged_replay, manifest)
    assert replay_result["valid"] is False
    assert replay_result["evaluation"]["replay_functional"] is False

    forged_reconcile = copy.deepcopy(report)
    forged_reconcile["replay"]["reconcile"]["recovery"]["removed"] = 1
    reconcile_result = reliability.verify_report(forged_reconcile, manifest)
    assert reconcile_result["valid"] is False
    assert reconcile_result["evaluation"]["replay_functional"] is False

    forged_storage = copy.deepcopy(report)
    forged_storage["replay"]["metrics"]["final"]["capture_file_count"] += 1
    forged_storage["replay"]["sqlite"]["checkpoint"] = [0, 1, 0]
    storage_result = reliability.verify_report(forged_storage, manifest)
    assert storage_result["valid"] is False
    assert storage_result["evaluation"]["replay_functional"] is False

    forged_resources = copy.deepcopy(report)
    forged_resources["replay"]["metrics"]["final"]["db_bytes"] = (
        manifest["limits"]["max_db_bytes"] + 1
    )
    resources_result = reliability.verify_report(forged_resources, manifest)
    assert resources_result["valid"] is False
    assert resources_result["evaluation"]["replay_functional"] is False

    forged_worker = copy.deepcopy(report)
    forged_worker["soak"]["worker"]["requested_duration_ns"] += 1
    forged_worker["soak"]["worker"]["indexed_capture_count"] += 1
    forged_worker["soak"]["worker"]["observed_duration_ns"] = (
        forged_worker["soak"]["observed_duration_ns"] + 1
    )
    worker_result = reliability.verify_report(forged_worker, manifest)
    assert worker_result["valid"] is False
    assert worker_result["evaluation"]["soak_functional"] is False

    output = tmp_path / "report.json"
    reliability.write_json_atomic(output, report)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "incomplete"


def test_single_sample_cannot_claim_a_24h_timeline() -> None:
    manifest = reliability.load_manifest()
    duration_ns = 86_400 * 1_000_000_000

    summary = reliability.summarize_sample_timeline(
        [{"elapsed_monotonic_ns": 0}],
        observed_duration_ns=duration_ns,
        manifest=manifest,
    )

    assert summary["complete"] is False
    assert summary["terminal_sample_gap_ns"] == duration_ns


def test_custom_manifest_cannot_weaken_issue_2_hard_floors(tmp_path: Path) -> None:
    manifest = reliability.load_manifest()
    manifest["minimum_replay_capture_count"] = 4
    manifest["minimum_soak_duration_seconds"] = 1
    manifest["maximum_sample_interval_seconds"] = 3600
    manifest["limits"]["max_rss_bytes"] *= 10
    path = tmp_path / "weak-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(reliability.AuditError):
        reliability.load_manifest(path)
