#!/usr/bin/env python3
"""Sign and execute the bundled macOS app without re-signing an ad-hoc sidecar."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import zipfile
from html import escape
from pathlib import Path
from typing import Any

import build_desktop_sidecar as sidecar_builder

ROOT = Path(__file__).resolve().parents[1]
APP = (
    ROOT
    / "apps"
    / "desktop"
    / "src-tauri"
    / "target"
    / "release"
    / "bundle"
    / "macos"
    / "OpenChronicle.app"
)
MAIN_NAME = "openchronicle-desktop"
FINAL_MANIFEST = APP.parent / "OpenChronicle.bundle-manifest.json"


class BridgeOperationError(RuntimeError):
    """Closed packaged-bridge failure with a machine-readable public code."""

    def __init__(self, operation: str, code: str) -> None:
        self.operation = operation
        self.code = code
        super().__init__(f"packaged bridge operation {operation} failed closed: {code}")


def finalize(app: Path = APP) -> dict[str, Any]:
    app = app.resolve()
    if app != APP.resolve() or not app.is_dir() or app.is_symlink():
        raise ValueError("desktop bundle path is invalid")
    executable_dir = app / "Contents" / "MacOS"
    main = executable_dir / MAIN_NAME
    sidecar = executable_dir / sidecar_builder.BRIDGE_NAME
    if not _regular_executable(main) or not _regular_executable(sidecar):
        raise RuntimeError("desktop bundle executable layout is invalid")

    triple = sidecar_builder.target_triple()
    source_sidecar = sidecar_builder.output_path(triple)
    source_manifest_path = source_sidecar.with_name(f"{source_sidecar.name}.manifest.json")
    source_manifest = _json_file(source_manifest_path)
    source_digest = _sha256(source_sidecar)
    if (
        source_manifest.get("sha256") != source_digest
        or source_manifest.get("target_triple") != triple
        or source_manifest.get("protocol_version") != sidecar_builder.PROTOCOL_VERSION
    ):
        raise RuntimeError("desktop sidecar build manifest is stale")

    identity = os.environ.get("APPLE_SIGNING_IDENTITY", "")
    signing_mode = "developer_id" if identity else "development_adhoc"
    bundled_before_sign = _sha256(sidecar)
    if not identity:
        if bundled_before_sign != source_digest:
            raise RuntimeError("Tauri changed the ad-hoc sidecar before finalization")
        _run(["/usr/bin/codesign", "--force", "--sign", "-", str(main)])
        _run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)])

    _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=4", str(app)])
    _verify_architecture(main, triple)
    _verify_architecture(sidecar, triple)
    sidecar_smoke = sidecar_builder.smoke_test(sidecar)
    resume_rescue_e2e = _resume_rescue_e2e(sidecar)
    app_smoke = _launch_smoke(main)
    bundled_digest = _sha256(sidecar)
    if signing_mode == "development_adhoc" and bundled_digest != source_digest:
        raise RuntimeError("development finalization re-signed the PyInstaller sidecar")

    manifest = {
        "schema_version": 1,
        "app": app.name,
        "bundle_identifier": "app.openchronicle.desktop",
        "target_triple": triple,
        "signing_mode": signing_mode,
        "notarization": "not_run",
        "gatekeeper": "not_applicable_adhoc" if not identity else "not_run",
        "sidecar": {
            "protocol_version": sidecar_builder.PROTOCOL_VERSION,
            "source_sha256": source_digest,
            "bundled_sha256": bundled_digest,
            "size_bytes": sidecar.stat().st_size,
            "smoke": sidecar_smoke,
        },
        "resume_rescue_e2e": resume_rescue_e2e,
        "development_verification_passed": resume_rescue_e2e["development_path_passed"],
        "release_gate_passed": False,
        "app_size_bytes": _directory_size(app),
        "app_smoke": app_smoke,
        "codesign_deep_strict": True,
    }
    FINAL_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _resume_rescue_e2e(sidecar: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="openchronicle-bundle-resume-") as temporary:
        root = Path(temporary)
        home = root / "home"
        data = root / "data"
        home.mkdir(mode=0o700)
        state = _bridge_request(sidecar, root, "resume_rescue.state")
        if state["enabled"] is not False:
            raise RuntimeError("packaged resume workflow did not start disabled")
        config_path = data / "config.toml"
        config_text = config_path.read_text(encoding="utf-8")
        marker = "[resume_rescue]\nenabled = false"
        if config_text.count(marker) != 1:
            raise RuntimeError("packaged resume config marker differs")
        config_path.write_text(
            config_text.replace(marker, "[resume_rescue]\nenabled = true", 1),
            encoding="utf-8",
        )

        source = json.dumps(
            {
                "basics": {"name": "Ada Example", "summary": "Reliability engineer."},
                "skills": [{"name": "Python", "keywords": ["SQLite"]}],
            },
            separators=(",", ":"),
        )
        review = _bridge_request(
            sidecar, root, "resume_rescue.review_json", {"source_text": source}
        )["review"]
        selections = [
            {
                "candidate_id": candidate["id"],
                "fact_id": f"packaged-fact-{index}",
                "section": candidate["suggested_section"],
                "confidentiality": "private",
                "ownership_scope": "individual",
            }
            for index, candidate in enumerate(review["candidates"])
        ]
        profile = _bridge_request(
            sidecar,
            root,
            "resume_rescue.admit_json",
            {
                "source_text": source,
                "expected_review_digest": review["review_digest"],
                "profile_id": "packaged-profile",
                "display_name": review["display_name_candidate"],
                "locale": "en-US",
                "selections": selections,
            },
        )["profile"]
        opportunity = _bridge_request(
            sidecar,
            root,
            "resume_rescue.save_opportunity",
            {
                "employer": "Example Labs",
                "title": "Reliability Engineer",
                "source_text": "Build reliable services.",
                "source_url": "",
                "priorities": [],
                "locale": "en-US",
            },
        )["opportunity"]
        facts = profile["profile"]["facts"]
        sections = [
            {
                "kind": section,
                "fact_ids": [fact["id"] for fact in facts if fact["section"] == section],
            }
            for section in dict.fromkeys(fact["section"] for fact in facts)
        ]
        projection = _bridge_request(
            sidecar,
            root,
            "resume_rescue.compose_exact",
            {
                "profile_id": profile["id"],
                "opportunity_id": opportunity["id"],
                "sections": sections,
                "requirements": [],
            },
        )["projection"]
        preview = _bridge_request(
            sidecar,
            root,
            "resume_rescue.preview",
            {"projection_id": projection["id"]},
        )["preview"]
        json_export = _bridge_request(
            sidecar,
            root,
            "resume_rescue.export_json",
            {"projection_id": projection["id"]},
        )["export"]
        native_exports: dict[str, int] = {}
        pdf_export = "passed"
        for source_format, magic in (("docx", b"PK"), ("pdf", b"%PDF")):
            try:
                exported = _bridge_request(
                    sidecar,
                    root,
                    f"resume_rescue.export_{source_format}",
                    {
                        "projection_id": projection["id"],
                        "expected_preview_document_digest": preview["document_digest"],
                    },
                    timeout=90,
                )["export"]
            except BridgeOperationError as exc:
                if source_format != "pdf" or exc.code != "EXPORT_UNAVAILABLE":
                    raise
                pdf_export = "blocked_unbundled_engine"
                continue
            content = base64.b64decode(exported["content_base64"], validate=True)
            if not content.startswith(magic) or exported["action_capability"] != "none":
                raise RuntimeError("packaged native resume export is invalid")
            native_exports[source_format] = len(content)

        document = _resume_docx("Selected packaged document evidence")
        document_review = _bridge_request(
            sidecar,
            root,
            "resume_rescue.review_document",
            {
                "source_base64": base64.b64encode(document).decode("ascii"),
                "source_format": "docx",
            },
            timeout=90,
        )["review"]
        if (
            json_export["action_capability"] != "none"
            or json.loads(json_export["json_text"]) != json_export["document"]
            or [item["text"] for item in document_review["candidates"]]
            != ["Selected packaged document evidence"]
        ):
            raise RuntimeError("packaged resume workflow output is invalid")
    return {
        "passed": pdf_export == "passed",
        "development_path_passed": True,
        "operations": 10,
        "json_candidates": len(review["candidates"]),
        "selected_facts": len(facts),
        "docx_bytes": native_exports["docx"],
        "pdf_export": pdf_export,
        "pdf_bytes": native_exports.get("pdf"),
        "document_candidates": len(document_review["candidates"]),
        "action_capability": "none",
    }


def _bridge_request(
    executable: Path,
    root: Path,
    operation: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = 45,
) -> dict[str, Any]:
    environment = {
        "HOME": str(root / "home"),
        "OPENCHRONICLE_ROOT": str(root / "data"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(root),
        "LANG": "en_US.UTF-8",
    }
    request = {
        "version": sidecar_builder.PROTOCOL_VERSION,
        "operation": operation,
        "params": params or {},
    }
    completed = subprocess.run(
        [str(executable)],
        input=json.dumps(request, separators=(",", ":")) + "\n",
        capture_output=True,
        text=True,
        env=environment,
        timeout=timeout,
        check=False,
    )
    if completed.stderr:
        raise RuntimeError("packaged bridge emitted diagnostics")
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("packaged bridge response is malformed") from exc
    if (
        completed.returncode != 0
        or not isinstance(response, dict)
        or set(response) != {"version", "ok", "result"}
        or response["version"] != sidecar_builder.PROTOCOL_VERSION
        or response["ok"] is not True
        or not isinstance(response["result"], dict)
    ):
        error = response.get("error") if isinstance(response, dict) else None
        code = error.get("code", "invalid_envelope") if isinstance(error, dict) else "failed"
        raise BridgeOperationError(operation, code)
    return response["result"]


def _resume_docx(*paragraphs: str) -> bytes:
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document.main+xml"/></Types>'
    )
    body = "".join(
        f"<w:p><w:r><w:t>{escape(paragraph)}</w:t></w:r></w:p>" for paragraph in paragraphs
    )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("word/document.xml", document)
    return output.getvalue()


def _launch_smoke(main: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="openchronicle-app-smoke-") as temporary:
        root = Path(temporary)
        home = root / "home"
        data = root / "data"
        home.mkdir(mode=0o700)
        environment = {
            "HOME": str(home),
            "OPENCHRONICLE_ROOT": str(data),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(root),
            "LANG": "en_US.UTF-8",
        }
        process = subprocess.Popen(
            [str(main)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        try:
            time.sleep(5)
            if process.poll() is not None:
                raise RuntimeError("desktop bundle app exited during launch smoke")
            if not (data / "config.toml").is_file() or not (data / "index.db").is_file():
                raise RuntimeError("desktop bundle app did not complete its bridge startup")
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=5)
        if stdout or stderr:
            raise RuntimeError("desktop bundle app emitted startup diagnostics")
    return {"duration_seconds": 5, "isolated_data_root": True, "passed": True}


def _verify_architecture(executable: Path, triple: str) -> None:
    expected = "arm64" if triple.startswith("aarch64-") else "x86_64"
    completed = _run(["/usr/bin/lipo", "-archs", str(executable)])
    if completed.stdout.split() != [expected]:
        raise RuntimeError("desktop bundle architecture differs from its target")


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_file(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 64 * 1024:
        raise ValueError("desktop build manifest is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("desktop build manifest is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("desktop build manifest is invalid")
    return value


def _regular_executable(path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and os.access(path, os.X_OK)


def _directory_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def main() -> int:
    print(json.dumps(finalize(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
