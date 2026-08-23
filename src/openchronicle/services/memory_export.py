"""Deterministic local exports of the authorized current-fact projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from ..config import Config
from .current_facts import CurrentFact, list_current_facts

_MAX_EXPORT_FACTS = 10_000
_MAX_EXPORT_BYTES = 32 * 1024 * 1024


def build_current_memory_export(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    format: str,
    now: datetime | None = None,
) -> dict[str, object]:
    clean_format = format.strip().casefold()
    if clean_format not in {"json", "markdown"}:
        raise ValueError("memory export format must be json or markdown")
    exported_at = (now or datetime.now().astimezone()).isoformat()
    facts = list_current_facts(conn, cfg, as_of=now, limit=_MAX_EXPORT_FACTS)
    if clean_format == "json":
        content = _json_export(facts, exported_at=exported_at)
        media_type = "application/json"
        extension = "json"
    else:
        content = _markdown_export(facts, exported_at=exported_at)
        media_type = "text/markdown"
        extension = "md"
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_EXPORT_BYTES:
        raise ValueError("current memory export exceeds 32 MiB")
    date_stamp = exported_at[:10] if len(exported_at) >= 10 else "current"
    return {
        "schema_version": 1,
        "format": f"openchronicle_current_memory_{clean_format}_v1",
        "media_type": media_type,
        "extension": extension,
        "file_name": f"openchronicle-memory-{date_stamp}.{extension}",
        "byte_count": len(encoded),
        "content_digest": hashlib.sha256(encoded).hexdigest(),
        "fact_count": len(facts),
        "content": content,
        "action_capability": "save_local_copy",
    }


def _json_export(facts: list[CurrentFact], *, exported_at: str) -> str:
    payload = {
        "schema_version": 1,
        "format": "openchronicle_current_memory_v1",
        "exported_at": exported_at,
        "fact_count": len(facts),
        "facts": [_fact_payload(fact) for fact in facts],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _markdown_export(facts: list[CurrentFact], *, exported_at: str) -> str:
    lines = [
        "# OpenChronicle Current Memory",
        "",
        f"Exported: {exported_at}",
        f"Current facts: {len(facts)}",
        "",
    ]
    for fact in facts:
        label = fact.subject_key or f"{fact.path}#{fact.id}"
        lines.extend(
            [
                f"## {label}",
                "",
                f"- File: `{fact.path}`",
                f"- Entry: `{fact.id}`",
                f"- Revision: `{fact.revision}`",
                f"- Recorded: {fact.recorded_at}",
                f"- Assertion basis: {fact.assertion_kind or 'unspecified'}",
                f"- Valid from: {fact.valid_from or 'open'}",
                f"- Valid to: {fact.valid_to or 'open'}",
                f"- Tags: {', '.join(fact.tags) if fact.tags else 'none'}",
                f"- Direct sources: {fact.source_count}",
                "",
                fact.content,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _fact_payload(fact: CurrentFact) -> dict[str, object]:
    return {
        "id": fact.id,
        "path": fact.path,
        "state": "current",
        "subject_key": fact.subject_key,
        "assertion_kind": fact.assertion_kind,
        "recorded_at": fact.recorded_at,
        "valid_from": fact.valid_from,
        "valid_to": fact.valid_to,
        "revision": fact.revision,
        "content": fact.content,
        "tags": list(fact.tags),
        "origin": fact.origin,
        "source_count": fact.source_count,
    }
