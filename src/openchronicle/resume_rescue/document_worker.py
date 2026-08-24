"""Capability-minimized subprocess for untrusted résumé document parsing."""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any

from .document_extract import (
    MAX_SOURCE_BYTES,
    DocumentExtractionError,
    _extract_document_in_process,
    _strict_json_object,
)

MAX_REQUEST_BYTES = ((MAX_SOURCE_BYTES + 2) // 3 * 4) + 1_024


def main() -> int:
    _apply_resource_limits()
    _install_capability_audit_hook()
    source = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not 1 <= len(source) <= MAX_REQUEST_BYTES:
        _write_error("document extraction worker request size is invalid")
        return 0
    try:
        request = _strict_json_object(source)
        if set(request) != {"schema_version", "format", "source_base64"}:
            raise DocumentExtractionError("document extraction worker request is invalid")
        source_format = request.get("format")
        encoded = request.get("source_base64")
        if (
            request.get("schema_version") != 1
            or source_format not in {"pdf", "docx"}
            or not isinstance(encoded, str)
            or not encoded
        ):
            raise DocumentExtractionError("document extraction worker request is invalid")
        try:
            document_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise DocumentExtractionError("document extraction worker source is invalid") from exc
        review = _extract_document_in_process(document_bytes, source_format=source_format)
        _write_json({"schema_version": 1, "ok": True, "review": review.to_dict()})
    except DocumentExtractionError as exc:
        _write_error(str(exc))
    except Exception:
        _write_error("document extraction failed")
    return 0


def _install_capability_audit_hook() -> None:
    blocked = {
        "os.posix_spawn",
        "os.posix_spawnp",
        "os.spawn",
        "os.system",
        "subprocess.Popen",
    }

    def audit(event: str, _args: tuple[Any, ...]) -> None:
        if event.startswith("socket.") or event in blocked:
            raise PermissionError("document extraction worker capability denied")

    sys.addaudithook(audit)


def _apply_resource_limits() -> None:
    if os.name != "posix":
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (15, 16))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
    except (ImportError, OSError, ValueError):
        pass


def _write_error(message: str) -> None:
    safe = message if isinstance(message, str) and 1 <= len(message) <= 256 else "invalid document"
    _write_json({"schema_version": 1, "ok": False, "error": safe})


def _write_json(value: dict[str, Any]) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    raise SystemExit(main())
