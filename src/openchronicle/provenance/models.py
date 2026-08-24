"""Small, serializable provenance value objects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A stable reference to one local source record.

    ``path`` is empty for database-native sources such as timeline blocks and
    sessions. ``content_hash`` captures the exact source revision used by a
    derived object without copying the source text into the provenance graph.
    """

    kind: str
    id: str
    path: str = ""
    timestamp: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str)
            for value in (
                self.kind,
                self.id,
                self.path,
                self.timestamp,
                self.content_hash,
            )
        ):
            raise ValueError("evidence reference fields must be strings")
        if not self.kind.strip():
            raise ValueError("evidence kind is required")
        if not self.id.strip():
            raise ValueError("evidence id is required")
        if "\x00" in self.kind or "\x00" in self.id or "\x00" in self.path:
            raise ValueError("evidence identifiers cannot contain NUL")

    @property
    def key(self) -> str:
        """Opaque prompt-safe token source; it never embeds captured text."""
        raw = f"{self.kind}\0{self.path}\0{self.id}".encode()
        return "ev-" + hashlib.sha256(raw).hexdigest()[:20]

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "id": self.id,
            "path": self.path,
            "timestamp": self.timestamp,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvidenceRef:
        if not isinstance(value, dict):
            raise ValueError("evidence reference must be an object")
        return cls(
            kind=str(value.get("kind") or ""),
            id=str(value.get("id") or ""),
            path=str(value.get("path") or ""),
            timestamp=str(value.get("timestamp") or ""),
            content_hash=str(value.get("content_hash") or ""),
        )


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_digest(value: Any) -> str:
    return content_digest(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def observation_digest(data: dict[str, Any]) -> str:
    """Hash the v2 semantic observation, excluding only pixel storage.

    Policy decisions can depend on fields outside the rendered text slice, so
    every persisted field is bound. Screenshot bytes and their storage-only
    strip marker remain deliberately mutable under tiered retention.
    """
    excluded = {
        "screenshot",
        "screenshot_stripped",
        "__openchronicle_dropped_screenshot",
        "__openchronicle_source_digest",
    }
    return canonical_digest(
        {
            "version": 2,
            "observation": {key: value for key, value in data.items() if key not in excluded},
        }
    )


def legacy_observation_digest(data: dict[str, Any]) -> str:
    """Pre-v2 digest, used only by the one-time trusted local migration."""
    return canonical_digest(
        {
            key: data.get(key)
            for key in (
                "timestamp",
                "window_meta",
                "trigger",
                "focused_element",
                "visible_text",
                "url",
                "ax_tree",
            )
        }
    )


def timeline_block_digest(*, start: str, end: str, entries: list[Any], apps: list[Any]) -> str:
    return canonical_digest({"start": start, "end": end, "entries": entries, "apps": apps})


def timeline_capture_window_digest(
    bindings: list[tuple[str, str, str, str]],
) -> str:
    """Bind the complete, order-independent semantic capture snapshot."""
    return canonical_digest(
        {
            "version": 1,
            "captures": sorted(
                (
                    {
                        "path": path,
                        "observation_id": observation_id,
                        "source_hash": source_hash,
                        "capture_time": capture_time,
                    }
                    for path, observation_id, source_hash, capture_time in bindings
                ),
                key=lambda value: (
                    value["path"],
                    value["observation_id"],
                    value["capture_time"],
                    value["source_hash"],
                ),
            ),
        }
    )


def timeline_window_receipt_digest(
    *,
    window_start: str,
    window_end: str,
    capture_count: int,
    capture_digest: str,
    policy_digest: str,
    outcome: str,
    block_id: str,
    block_projection_digest: str,
    block_source_digest: str,
    raw_state: str,
) -> str:
    """Bind one durable window outcome; ``inspected_at`` is intentionally mutable."""
    return canonical_digest(
        {
            "version": 2,
            "window_start": window_start,
            "window_end": window_end,
            "capture_count": capture_count,
            "capture_digest": capture_digest,
            "policy_digest": policy_digest,
            "outcome": outcome,
            "block_id": block_id,
            "block_projection_digest": block_projection_digest,
            "block_source_digest": block_source_digest,
            "raw_state": raw_state,
        }
    )


def timeline_block_projection_digest(
    *,
    block_id: str,
    start: str,
    end: str,
    timezone: str,
    entries: list[Any],
    apps: list[Any],
    capture_count: int,
    created_at: str,
    source_digest: str,
) -> str:
    """Bind every persisted field in an immutable timeline-block row.

    ``timeline_block_digest`` remains the compact evidence-content binding used
    by historical provenance references.  This wider digest protects the
    SQLite projection itself, including metadata that is returned by public
    context surfaces but is not part of that historical evidence digest.
    """
    return canonical_digest(
        {
            "schema": "timeline-block-projection-v1",
            "id": block_id,
            "start": start,
            "end": end,
            "timezone": timezone,
            "entries": entries,
            "apps": apps,
            "capture_count": capture_count,
            "created_at": created_at,
            "source_digest": source_digest,
        }
    )


def timeline_block_sources_digest(sources: list[EvidenceRef]) -> str:
    """Bind a timeline block to its complete, order-independent source set."""
    return _evidence_sources_digest("timeline-block-sources-v1", sources)


def daily_wrap_sources_digest(sources: list[EvidenceRef]) -> str:
    """Bind a Daily Wrap revision to its complete persisted source set."""
    return _evidence_sources_digest("daily-wrap-sources-v1", sources)


def _evidence_sources_digest(schema: str, sources: list[EvidenceRef]) -> str:
    """Return an order-independent digest over fully bound evidence refs."""
    normalized = sorted(
        (
            {
                "kind": source.kind,
                "id": source.id,
                "path": source.path,
                "timestamp": source.timestamp,
                "content_hash": source.content_hash,
            }
            for source in sources
        ),
        key=lambda value: (
            value["kind"],
            value["path"],
            value["id"],
            value["timestamp"],
            value["content_hash"],
        ),
    )
    return canonical_digest(
        {
            "schema": schema,
            "sources": normalized,
        }
    )
