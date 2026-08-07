"""Markdown memory file I/O — read, write, parse frontmatter, parse entries."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import threading
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import frontmatter

from .. import paths
from ..provenance.models import EvidenceRef


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    A crash between writing the temp file and the rename leaves the
    original file untouched; a crash after the rename leaves the new
    file fully written. ``os.replace`` is atomic on POSIX (and on
    Windows for files on the same volume). The temp file lives in the
    target's parent directory so the rename is a same-filesystem move.

    Without this, a daemon SIGKILL / OOM / power loss in the middle of
    ``Path.write_text`` truncates the file — frontmatter or entry text
    half-written, the next read fails to parse.

    Permissions are preserved when overwriting an existing file.
    Newly created files keep ``mkstemp``'s 0o600 default — which is
    appropriate for private memory data; the previous code path
    inherited the umask default (typically 0o644).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # Preserve permissions of the existing file so updates don't
        # silently flip group/other-read bits set by the user.
        with contextlib.suppress(FileNotFoundError):
            os.chmod(tmp_path, path.stat().st_mode & 0o7777)
        os.replace(tmp_path, path)
        # Persist the directory entry so a power loss right after the
        # rename can't leave the dir pointing at neither old nor new.
        # macOS APFS sometimes returns EINVAL on directory fsync; the
        # call is best-effort — failure here is strictly less safe than
        # success, never less safe than skipping.
        with contextlib.suppress(OSError):
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise

VALID_PREFIXES = ("user-", "project-", "tool-", "topic-", "person-", "org-", "event-")


# Per-path mutex registry. The reducer fires from a daemon thread per
# session, and the daily-tick + on-demand classifiers can fire in
# parallel, so two threads can both call ``append_entry`` (or supersede)
# on the same memory file. Without serialization the read-modify-write
# in those functions silently loses one of the writes — both threads
# read the same base, both write a "+1 entry" version, and the second
# write wins. The FTS index, written outside the file, ends up holding
# rows for entries that don't exist on disk.
#
# Each thread mutex is paired with a cross-process BSD lock below so daemon
# callbacks and CLI recovery commands cannot race. Per-path locks protect the
# read-modify-write itself; store mutations additionally take one global lock
# so a full FTS rebuild cannot interleave with any Markdown/index update.
_lock_registry_lock = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}
_STORE_WRITE_LOCK_NAME = ".store-write.lock"
_REVIEW_OPERATION_LOCK_NAME = ".memory-review-operation"
_review_thread_lock = threading.RLock()
_review_lock_state = threading.local()
_MEMORY_TEMP_RE = re.compile(
    r"^\.[^/]+\.md\.[A-Za-z0-9_-]+\.tmp$"
)


def _lock_for(path: Path) -> threading.Lock:
    """Return the threading.Lock that guards write access to ``path``.

    Resolves the path before keying so a relative and an absolute spelling
    of the same file map to the same lock. ``resolve(strict=False)`` works
    on non-existent paths (the file may not be created yet) and is called
    outside the registry lock so a slow stat doesn't stall other threads.
    """
    key = str(path.resolve())
    with _lock_registry_lock:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


@contextlib.contextmanager
def _cross_process_lock(lock_path: Path) -> Iterator[None]:
    """Hold one private BSD advisory-lock sidecar until the context exits."""
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def store_write_lock() -> Iterator[None]:
    """Serialize Markdown/FTS mutations, including full index rebuilds.

    Operations that also mutate one Markdown file must acquire this global
    lock before :func:`file_lock`. Keeping one order across threads and
    processes prevents a rebuild from deleting FTS rows while a file writer is
    between its Markdown rename and matching index update.
    """
    lock_path = paths.root() / _STORE_WRITE_LOCK_NAME
    with _lock_for(lock_path), _cross_process_lock(lock_path):
        yield


@contextlib.contextmanager
def review_operation_lock() -> Iterator[None]:
    """Serialize provenance-producing writes with closure-based purge.

    The lock is re-entrant within one thread because candidate approval calls
    the generic provenance-bearing append path while already holding this
    fence. Only the outermost acquisition takes the cross-process BSD lock.
    """
    lock_path = paths.root() / _REVIEW_OPERATION_LOCK_NAME
    with _review_thread_lock:
        depth = int(getattr(_review_lock_state, "depth", 0))
        _review_lock_state.depth = depth + 1
        try:
            if depth:
                yield
            else:
                with _cross_process_lock(lock_path):
                    yield
        finally:
            _review_lock_state.depth = depth


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Serialize writers to one logical path across threads and processes.

    The stable sidecar lock coordinates CLI recovery commands with the daemon,
    and the kernel releases it automatically if either process crashes.
    """
    with _lock_for(path):
        lock_path = path.parent / f".{path.name}.lock"
        with _cross_process_lock(lock_path):
            yield


def is_memory_temp_name(name: str) -> bool:
    """Recognize temp files created by :func:`atomic_write_text`."""
    return _MEMORY_TEMP_RE.fullmatch(name) is not None


def cleanup_orphan_memory_temps() -> int:
    """Remove crash-left temp copies while excluding every live writer."""
    removed = 0
    with store_write_lock():
        memory = paths.memory_dir()
        if not memory.exists():
            return 0
        for path in memory.rglob("*"):
            if not path.is_file() or not is_memory_temp_name(path.name):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed

ENTRY_HEADING_RE = re.compile(
    r"^##\s*\[(?P<ts>[^\]]+)\]\s*\{id:\s*(?P<id>[a-zA-Z0-9\-]+)\}(?P<tags>[^\n]*)$",
    re.MULTILINE,
)
PROVENANCE_MARKER_RE = re.compile(r"<!--\s*oc-provenance:", re.IGNORECASE)
PROVENANCE_COMMENT_RE = re.compile(
    r"(?:^|\n)<!--\s*oc-provenance:\s*(?P<payload>\{[^\r\n]*\})\s*-->\s*\Z",
    re.IGNORECASE,
)


@dataclass
class ParsedEntry:
    id: str
    timestamp: str
    tags: list[str]
    heading_line: str
    body: str
    superseded_by: str | None = None
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    provenance_present: bool = False
    provenance_valid: bool = True
    provenance_error: str = ""


@dataclass
class ParsedFile:
    path: Path
    description: str
    tags: list[str]
    status: str
    created: str
    updated: str
    entry_count: int
    needs_compact: bool
    entries: list[ParsedEntry] = field(default_factory=list)
    raw_frontmatter: dict[str, Any] = field(default_factory=dict)


def memory_path(name: str) -> Path:
    """Resolve a logical memory filename to an absolute path inside memory_dir()."""
    if "/" in name or "\\" in name:
        raise ValueError(f"memory path must not contain slashes: {name!r}")
    if not name.endswith(".md"):
        name = name + ".md"
    parent = paths.memory_dir()
    if parent.exists():
        requested_key = unicodedata.normalize("NFC", name).casefold()
        matches = [
            child
            for child in parent.iterdir()
            if unicodedata.normalize("NFC", child.name).casefold() == requested_key
        ]
        if matches and (len(matches) != 1 or matches[0].name != name):
            raise ValueError(
                "memory path spelling must exactly match its canonical on-disk name"
            )
    return parent / name


def validate_prefix(name: str) -> str:
    stem = name.removesuffix(".md")
    for p in VALID_PREFIXES:
        if stem.startswith(p) and len(stem) > len(p):
            return p.rstrip("-")
    raise ValueError(
        f"filename {name!r} must start with one of: {', '.join(VALID_PREFIXES)}"
    )


def today() -> str:
    return date.today().isoformat()


def default_frontmatter(*, description: str, tags: list[str]) -> dict[str, Any]:
    return {
        "description": description,
        "tags": tags,
        "status": "active",
        "created": today(),
        "updated": today(),
        "entry_count": 0,
        "needs_compact": False,
    }


def write_file(path: Path, fm: dict[str, Any], body: str) -> None:
    post = frontmatter.Post(content=body, **fm)
    text = frontmatter.dumps(post) + ("\n" if not body.endswith("\n") else "")
    atomic_write_text(path, text)


def read_file(path: Path) -> ParsedFile:
    if not path.exists():
        raise FileNotFoundError(path)
    post = frontmatter.load(path)
    fm = dict(post.metadata)
    body = post.content
    entries = _parse_entries(body)
    return ParsedFile(
        path=path,
        description=str(fm.get("description", "")),
        tags=list(fm.get("tags", []) or []),
        status=str(fm.get("status", "active")),
        created=str(fm.get("created", "")),
        updated=str(fm.get("updated", "")),
        entry_count=int(fm.get("entry_count", len(entries)) or 0),
        needs_compact=bool(fm.get("needs_compact", False)),
        entries=entries,
        raw_frontmatter=fm,
    )


def _parse_entries(body: str) -> list[ParsedEntry]:
    entries: list[ParsedEntry] = []
    matches = list(ENTRY_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        tag_str = m.group("tags") or ""
        raw_tags = [t.strip() for t in tag_str.split() if t.strip().startswith("#")]
        tags = [t[1:] for t in raw_tags]  # strip leading #
        superseded_by = None
        for t in tags:
            if t.startswith("superseded-by:"):
                superseded_by = t.split(":", 1)[1]
                break
        entry_body = body[start:end].strip("\n")
        evidence_refs: list[EvidenceRef] = []
        provenance_present = bool(PROVENANCE_MARKER_RE.search(entry_body))
        provenance_valid = True
        provenance_error = ""
        provenance_match = PROVENANCE_COMMENT_RE.search(entry_body)
        marker_count = len(PROVENANCE_MARKER_RE.findall(entry_body))
        if provenance_present and (marker_count != 1 or provenance_match is None):
            provenance_valid = False
            provenance_error = "provenance marker must be one valid final-line frame"
        elif provenance_match:
            try:
                payload = json.loads(provenance_match.group("payload"))
                if not isinstance(payload, dict) or payload.get("v") != 1:
                    raise ValueError("unsupported provenance frame version")
                raw_sources = payload.get("sources")
                if not isinstance(raw_sources, list) or not all(
                    isinstance(item, dict) for item in raw_sources
                ):
                    raise ValueError("provenance sources must be an array of objects")
                evidence_refs = [EvidenceRef.from_dict(item) for item in raw_sources]
            except (json.JSONDecodeError, ValueError, TypeError):
                provenance_valid = False
                provenance_error = "malformed provenance frame"
                evidence_refs = []
            if provenance_valid:
                entry_body = entry_body[: provenance_match.start()].rstrip("\n")
        entries.append(
            ParsedEntry(
                id=m.group("id"),
                timestamp=m.group("ts"),
                tags=tags,
                heading_line=m.group(0),
                body=entry_body,
                superseded_by=superseded_by,
                evidence_refs=evidence_refs,
                provenance_present=provenance_present,
                provenance_valid=provenance_valid,
                provenance_error=provenance_error,
            )
        )
    return entries


def render_heading(*, timestamp: str, entry_id: str, tags: list[str]) -> str:
    tag_part = "".join(f" #{t}" for t in tags) if tags else ""
    return f"## [{timestamp}] {{id: {entry_id}}}{tag_part}"


def validate_entry_body(body: str) -> None:
    """Reserve parser control lines so one logical write stays one entry."""
    if PROVENANCE_MARKER_RE.search(body):
        raise ValueError("entry content contains the reserved oc-provenance marker")
    if ENTRY_HEADING_RE.search(body):
        raise ValueError("entry content contains a reserved canonical entry heading")


def render_file(
    *, fm: dict[str, Any], entries: list[ParsedEntry], header_lines: list[str] | None = None
) -> str:
    parts: list[str] = []
    if header_lines:
        parts.extend(header_lines)
        parts.append("")
    for e in entries:
        parts.append(e.heading_line)
        if e.body:
            parts.append(e.body)
        if e.evidence_refs or e.provenance_present:
            payload = {
                "v": 1,
                "sources": [source.to_dict() for source in e.evidence_refs],
            }
            parts.append(
                "<!-- oc-provenance: "
                + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + " -->"
            )
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _update_frontmatter_unlocked(path: Path, updates: dict[str, Any]) -> None:
    """Update frontmatter after the caller has acquired global→path locks."""
    post = frontmatter.load(path)
    post.metadata.update(updates)
    atomic_write_text(path, frontmatter.dumps(post) + "\n")


def update_frontmatter(path: Path, updates: dict[str, Any]) -> None:
    with store_write_lock(), file_lock(path):
        _update_frontmatter_unlocked(path, updates)


def list_memory_files() -> list[Path]:
    if not paths.memory_dir().exists():
        return []
    return sorted(p for p in paths.memory_dir().iterdir() if p.suffix == ".md" and p.name != "index.md")
