from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_desktop_sidecar.py"
SPEC = importlib.util.spec_from_file_location("desktop_sidecar_builder", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
FINALIZER = ROOT / "scripts" / "finalize_desktop_bundle.py"


def test_target_triples_and_output_names_are_closed() -> None:
    assert builder.target_triple(system="Darwin", machine="arm64") == "aarch64-apple-darwin"
    assert builder.target_triple(system="Darwin", machine="x86_64") == "x86_64-apple-darwin"
    with pytest.raises(ValueError, match="unsupported"):
        builder.target_triple(system="Linux", machine="x86_64")
    with pytest.raises(ValueError, match="allowlisted"):
        builder.output_path("../../untrusted")


def test_pyinstaller_command_is_pinned_onefile_console_and_target_suffixed() -> None:
    command = builder.pyinstaller_command("aarch64-apple-darwin")

    assert command[:3] == [os.sys.executable, "-m", "PyInstaller"]
    assert {"--onefile", "--console", "--noupx", "--clean", "--noconfirm"} <= set(command)
    name = command[command.index("--name") + 1]
    assert name == "openchronicle-desktop-bridge-aarch64-apple-darwin"
    assert command[command.index("--collect-data") + 1] == "openchronicle"
    assert Path(command[-1]) == ROOT / "resources" / "desktop_bridge_entry.py"

    signed = builder.pyinstaller_command(
        "aarch64-apple-darwin", codesign_identity="Developer ID Application: Example"
    )
    identity_index = signed.index("--codesign-identity")
    assert signed[identity_index + 1] == "Developer ID Application: Example"
    with pytest.raises(ValueError, match="identity is invalid"):
        builder.pyinstaller_command("aarch64-apple-darwin", codesign_identity="x\x00y")


def test_tauri_bundle_config_uses_external_binary_basename() -> None:
    payload = json.loads(
        (ROOT / "apps/desktop/src-tauri/tauri.bundle.conf.json").read_text(encoding="utf-8")
    )

    assert payload == {
        "$schema": "https://schema.tauri.app/config/2",
        "bundle": {"externalBin": ["binaries/openchronicle-desktop-bridge"]},
    }


def test_npm_bundle_pipeline_builds_app_then_runs_finalizer() -> None:
    package = json.loads((ROOT / "apps/desktop/package.json").read_text(encoding="utf-8"))
    command = package["scripts"]["tauri:bundle"]

    assert command.startswith("npm run sidecar:build && tauri build")
    assert "--config src-tauri/tauri.bundle.conf.json --bundles app" in command
    assert command.endswith("python ../../scripts/finalize_desktop_bundle.py")
    assert FINALIZER.is_file()
