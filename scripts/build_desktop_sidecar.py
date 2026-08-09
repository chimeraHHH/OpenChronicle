#!/usr/bin/env python3
"""Build and smoke-test the target-suffixed Tauri desktop sidecar."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

BRIDGE_NAME = "openchronicle-desktop-bridge"
PROTOCOL_VERSION = 14
PYINSTALLER_VERSION = "6.22.0"
SUPPORTED_TARGETS = {
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
}
ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINT = ROOT / "resources" / "desktop_bridge_entry.py"
BINARIES_DIR = ROOT / "apps" / "desktop" / "src-tauri" / "binaries"
SCRATCH_ROOT = ROOT / "scratch" / "desktop-sidecar"


def target_triple(*, system: str | None = None, machine: str | None = None) -> str:
    identity = (system or platform.system(), machine or platform.machine())
    try:
        return SUPPORTED_TARGETS[identity]
    except KeyError as exc:
        raise ValueError(
            f"unsupported desktop sidecar target: {identity[0]} {identity[1]}"
        ) from exc


def output_path(triple: str) -> Path:
    if triple not in SUPPORTED_TARGETS.values():
        raise ValueError("desktop sidecar target triple is not allowlisted")
    return BINARIES_DIR / f"{BRIDGE_NAME}-{triple}"


def pyinstaller_command(triple: str, *, codesign_identity: str = "") -> list[str]:
    target = output_path(triple)
    work = SCRATCH_ROOT / triple / "work"
    spec = SCRATCH_ROOT / triple / "spec"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--noupx",
        "--name",
        target.name,
        "--paths",
        str(ROOT / "src"),
        "--collect-data",
        "openchronicle",
        "--distpath",
        str(BINARIES_DIR),
        "--workpath",
        str(work),
        "--specpath",
        str(spec),
    ]
    if codesign_identity:
        if len(codesign_identity) > 256 or "\x00" in codesign_identity:
            raise ValueError("desktop sidecar signing identity is invalid")
        command.extend(["--codesign-identity", codesign_identity])
    command.append(str(ENTRY_POINT))
    return command


def build(*, triple: str, keep_work: bool = False, codesign_identity: str = "") -> dict[str, Any]:
    target = output_path(triple)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = SCRATCH_ROOT / triple
    if scratch.exists():
        shutil.rmtree(scratch)
    subprocess.run(
        pyinstaller_command(triple, codesign_identity=codesign_identity), cwd=ROOT, check=True
    )
    if not target.is_file() or target.is_symlink():
        raise RuntimeError("desktop sidecar build did not produce a regular file")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    _verify_architecture(target, triple)
    smoke = smoke_test(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "bridge_name": BRIDGE_NAME,
        "protocol_version": PROTOCOL_VERSION,
        "target_triple": triple,
        "pyinstaller_version": PYINSTALLER_VERSION,
        "signing_mode": "developer_id" if codesign_identity else "pyinstaller_adhoc",
        "binary": target.name,
        "sha256": digest,
        "size_bytes": target.stat().st_size,
        "smoke": smoke,
    }
    manifest_path = target.with_name(f"{target.name}.manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not keep_work:
        shutil.rmtree(scratch, ignore_errors=True)
    return manifest


def smoke_test(executable: Path) -> dict[str, Any]:
    if not executable.is_absolute() or not executable.is_file() or executable.is_symlink():
        raise ValueError("desktop sidecar smoke target is invalid")
    request = {
        "version": PROTOCOL_VERSION,
        "operation": "resume_rescue.state",
        "params": {},
    }
    with tempfile.TemporaryDirectory(prefix="openchronicle-sidecar-smoke-") as temporary:
        temporary_path = Path(temporary)
        environment = {
            "HOME": str(temporary_path / "home"),
            "OPENCHRONICLE_ROOT": str(temporary_path / "data"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(temporary_path),
            "LANG": "en_US.UTF-8",
        }
        Path(environment["HOME"]).mkdir(mode=0o700)
        completed = subprocess.run(
            [str(executable)],
            input=json.dumps(request, separators=(",", ":")) + "\n",
            capture_output=True,
            text=True,
            env=environment,
            timeout=45,
            check=False,
        )
    if completed.returncode != 0 or completed.stderr:
        raise RuntimeError("desktop sidecar smoke process failed")
    if completed.stdout.count("\n") > 1:
        raise RuntimeError("desktop sidecar smoke response was not one line")
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("desktop sidecar smoke response was malformed") from exc
    if (
        not isinstance(response, dict)
        or set(response) != {"version", "ok", "result"}
        or response["version"] != PROTOCOL_VERSION
        or response["ok"] is not True
        or not isinstance(response["result"], dict)
    ):
        raise RuntimeError("desktop sidecar smoke response used the wrong protocol")
    result = response["result"]
    if set(result) != {
        "enabled",
        "rewrite_enabled",
        "rewrite_provider",
        "profiles",
        "opportunities",
        "projections",
        "rewrites",
    }:
        raise RuntimeError("desktop sidecar smoke response used the wrong state schema")
    return {"operation": request["operation"], "passed": True}


def _verify_architecture(executable: Path, triple: str) -> None:
    expected = "arm64" if triple.startswith("aarch64-") else "x86_64"
    completed = subprocess.run(
        ["/usr/bin/lipo", "-archs", str(executable)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    architectures = completed.stdout.split()
    if architectures != [expected]:
        raise RuntimeError("desktop sidecar architecture differs from its target suffix")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the bundled OpenChronicle bridge")
    parser.add_argument("--target", choices=sorted(SUPPORTED_TARGETS.values()))
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args(argv)
    try:
        triple = args.target or target_triple()
    except ValueError as exc:
        parser.error(str(exc))
    manifest = build(
        triple=triple,
        keep_work=args.keep_work,
        codesign_identity=os.environ.get("APPLE_SIGNING_IDENTITY", ""),
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
