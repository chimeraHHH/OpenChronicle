#!/usr/bin/env python3
"""Fetch, verify, and freeze the release PDF fonts at their audited axes."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import os
import stat
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSET_DIR = ROOT / "src" / "openchronicle" / "assets" / "pdf_fonts"
MANIFEST_PATH = DEFAULT_ASSET_DIR / "manifest.json"
RAW_BASE_URL = "https://raw.githubusercontent.com"
MAX_SOURCE_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60
_HASH_CHUNK_BYTES = 4 * 1024 * 1024
_FONT_FIELDS = {
    "id",
    "filename",
    "source_path",
    "source_size_bytes",
    "source_sha256",
    "axes",
    "size_bytes",
    "sha256",
    "license",
    "license_file",
}


class FontBuildError(RuntimeError):
    """A pinned font source or generated asset failed closed."""


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FontBuildError("PDF font manifest is unavailable") from exc
    if set(value) != {
        "schema_version",
        "source_repository",
        "source_commit",
        "build_tool",
        "fonts",
    }:
        raise FontBuildError("PDF font manifest schema differs")
    build_tool = value.get("build_tool")
    fonts = value.get("fonts")
    if (
        value.get("schema_version") != 1
        or value.get("source_repository") != "google/fonts"
        or not _hex_digest(value.get("source_commit"), length=40)
        or build_tool != {"name": "fonttools", "version": "4.63.0"}
        or not isinstance(fonts, list)
        or len(fonts) != 3
    ):
        raise FontBuildError("PDF font manifest identity differs")
    identifiers: set[str] = set()
    filenames: set[str] = set()
    for font in fonts:
        if not isinstance(font, dict) or set(font) != _FONT_FIELDS:
            raise FontBuildError("PDF font entry schema differs")
        identifier = font.get("id")
        filename = font.get("filename")
        source_path = font.get("source_path")
        axes = font.get("axes")
        if (
            identifier not in {"base", "cjk", "arabic"}
            or identifier in identifiers
            or not isinstance(filename, str)
            or filename in filenames
            or Path(filename).name != filename
            or not filename.endswith(".ttf")
            or not isinstance(source_path, str)
            or not source_path.startswith("ofl/")
            or ".." in source_path
            or not isinstance(axes, dict)
            or not axes
            or any(
                not isinstance(axis, str) or len(axis) != 4 or type(position) not in {int, float}
                for axis, position in axes.items()
            )
            or font.get("license") != "OFL-1.1"
            or Path(str(font.get("license_file"))).name != font.get("license_file")
        ):
            raise FontBuildError("PDF font entry identity differs")
        for size_key in ("source_size_bytes", "size_bytes"):
            size = font.get(size_key)
            if type(size) is not int or not 1 <= size <= MAX_SOURCE_BYTES:
                raise FontBuildError("PDF font size pin is invalid")
        if not _hex_digest(font.get("source_sha256")) or not _hex_digest(font.get("sha256")):
            raise FontBuildError("PDF font digest pin is invalid")
        identifiers.add(identifier)
        filenames.add(filename)
    if identifiers != {"base", "cjk", "arabic"}:
        raise FontBuildError("PDF font roles differ")
    return value


def ensure_fonts(destination: Path = DEFAULT_ASSET_DIR) -> list[Path]:
    """Create only missing assets; reject any unexpected existing bytes."""

    manifest = load_manifest()
    try:
        destination.mkdir(parents=True, exist_ok=True)
        metadata = destination.lstat()
    except OSError as exc:
        raise FontBuildError("PDF font destination is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FontBuildError("PDF font destination must be a real directory")
    destination.chmod(0o700)

    generated: list[Path] = []
    for font in manifest["fonts"]:
        target = destination / font["filename"]
        if target.exists() or target.is_symlink():
            _verify_regular_file(
                target,
                size=font["size_bytes"],
                digest=font["sha256"],
            )
            generated.append(target)
            continue
        source = _download_source(manifest, font)
        built = _instantiate_font(source, axes=font["axes"])
        if len(built) != font["size_bytes"] or not _digest_matches(built, font["sha256"]):
            raise FontBuildError("generated PDF font differs from the accepted pin")
        _atomic_create(target, built)
        _verify_regular_file(target, size=font["size_bytes"], digest=font["sha256"])
        generated.append(target)
    return generated


def _download_source(manifest: dict[str, Any], font: dict[str, Any]) -> bytes:
    url = (
        f"{RAW_BASE_URL}/{manifest['source_repository']}/"
        f"{manifest['source_commit']}/{font['source_path']}"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "OpenChronicle-font-builder/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            if not final_url.startswith(f"{RAW_BASE_URL}/"):
                raise FontBuildError("PDF font download left the accepted origin")
            source = response.read(font["source_size_bytes"] + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise FontBuildError("pinned PDF font source is unavailable") from exc
    if len(source) != font["source_size_bytes"] or not _digest_matches(
        source, font["source_sha256"]
    ):
        raise FontBuildError("pinned PDF font source differs")
    return source


def _instantiate_font(source: bytes, *, axes: dict[str, int | float]) -> bytes:
    try:
        font = TTFont(io.BytesIO(source), recalcTimestamp=False)
        instantiateVariableFont(font, axes, inplace=True, optimize=True)
        font.recalcTimestamp = False
        output = io.BytesIO()
        font.save(output, reorderTables=False)
        return output.getvalue()
    except Exception as exc:
        raise FontBuildError("pinned PDF font could not be instantiated") from exc


def _atomic_create(target: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise FontBuildError("PDF font target appeared during the build") from exc
    except OSError as exc:
        raise FontBuildError("PDF font asset could not be created") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _verify_regular_file(path: Path, *, size: int, digest: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FontBuildError("PDF font asset is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise FontBuildError("PDF font asset differs from the accepted pin")
    try:
        digest_matches = _file_digest_matches(path, digest, expected_metadata=metadata)
    except OSError as exc:
        raise FontBuildError("PDF font asset is unavailable") from exc
    if metadata.st_size != size or not digest_matches:
        raise FontBuildError("PDF font asset differs from the accepted pin")


def _file_digest_matches(path: Path, expected: str, *, expected_metadata: os.stat_result) -> bool:
    digest = hashlib.sha256()
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_dev != expected_metadata.st_dev
            or opened_metadata.st_ino != expected_metadata.st_ino
            or opened_metadata.st_size != expected_metadata.st_size
        ):
            return False
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return hmac.compare_digest(digest.hexdigest(), expected)


def _digest_matches(value: bytes, expected: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(value).hexdigest(), expected)


def _hex_digest(value: Any, *, length: int = 64) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_ASSET_DIR)
    arguments = parser.parse_args()
    try:
        paths = ensure_fonts(arguments.destination)
    except FontBuildError as exc:
        parser.exit(1, f"PDF font build failed: {exc}\n")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
