from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

from openchronicle.testing import failpoints


def _invoke(
    *,
    root: Path,
    armed: str,
    called: str | None = None,
    token: str,
    action: str = failpoints.ACTION_SIGKILL_V1,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "OPENCHRONICLE_ROOT": str(root),
            failpoints.FAILPOINT_ENV: armed,
            failpoints.ACTION_ENV: action,
            failpoints.TOKEN_ENV: token,
        }
    )
    script = (
        "from openchronicle.testing import failpoints; "
        "failpoints.hit(" + repr(called or armed) + ")"
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def _authorize(root: Path, token: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    auth = root / failpoints.AUTHORIZATION_FILE
    auth.write_text(token, encoding="utf-8")
    auth.chmod(0o600)


def test_failpoints_are_dormant_without_an_explicit_environment() -> None:
    for name in failpoints.KNOWN_FAILPOINTS:
        failpoints.hit(name)


def test_nonmatching_armed_failpoint_is_a_noop(tmp_path: Path) -> None:
    root = tmp_path / "audit-root"
    token = secrets.token_hex(32)
    result = _invoke(
        root=root,
        armed="capture.json.before_rename",
        called="capture.json.after_rename",
        token=token,
    )
    assert result.returncode == 0
    assert not (root / failpoints.HIT_DIRECTORY).exists()


def test_failpoint_refuses_missing_authorization(tmp_path: Path) -> None:
    root = tmp_path / "audit-root"
    root.mkdir()
    token = secrets.token_hex(32)
    result = _invoke(root=root, armed="capture.json.before_rename", token=token)
    assert result.returncode != -signal.SIGKILL
    assert "authorization file is unavailable" in result.stderr
    assert not (root / failpoints.HIT_DIRECTORY).exists()


def test_failpoint_refuses_non_private_authorization(tmp_path: Path) -> None:
    root = tmp_path / "audit-root"
    token = secrets.token_hex(32)
    _authorize(root, token)
    (root / failpoints.AUTHORIZATION_FILE).chmod(0o644)
    result = _invoke(root=root, armed="capture.json.before_rename", token=token)
    assert result.returncode != -signal.SIGKILL
    assert "must be private" in result.stderr


def test_failpoint_refuses_a_non_temporary_root() -> None:
    token = secrets.token_hex(32)
    result = _invoke(
        root=Path(__file__).resolve().parents[1],
        armed="capture.json.before_rename",
        token=token,
    )
    assert result.returncode != -signal.SIGKILL
    assert "system temp directory" in result.stderr


def test_authorized_failpoint_records_hit_then_sigkills(tmp_path: Path) -> None:
    root = tmp_path / "audit-root"
    token = secrets.token_hex(32)
    name = "capture.json.before_rename"
    _authorize(root, token)

    result = _invoke(root=root, armed=name, token=token)

    assert result.returncode == -signal.SIGKILL
    marker = root / failpoints.HIT_DIRECTORY / f"{name}.hit"
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines[0] == name
    assert int(lines[1]) > 1
    assert len(lines) == 2
    assert marker.stat().st_mode & 0o777 == 0o600
