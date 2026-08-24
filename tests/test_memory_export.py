from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from openchronicle import config as config_mod
from openchronicle.provenance.models import EvidenceRef, content_digest
from openchronicle.services.memory_export import build_current_memory_export
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.store.facts import make_fact_metadata


def _seed_export_facts(conn) -> None:
    source_body = "Grounded export source."
    entries_store.create_file(
        conn,
        name="event-2026-08-20.md",
        description="export evidence",
        tags=["event"],
    )
    entries_store.append_entry_once(
        conn,
        name="event-2026-08-20.md",
        content=source_body,
        tags=["source"],
        entry_id="export-source",
        origin=files_store.MANUAL_ENTRY_ORIGIN,
    )
    source = EvidenceRef(
        kind="memory_entry",
        id="export-source",
        path="event-2026-08-20.md",
        content_hash=content_digest(source_body),
    )
    entries_store.create_file(
        conn,
        name="user-preferences.md",
        description="exported preferences",
        tags=["preference"],
    )
    entries_store.append_entry_once(
        conn,
        name="user-preferences.md",
        content="User prefers local-first tools.",
        tags=["preference", "local-first"],
        entry_id="export-current",
        evidence_refs=[source],
        fact_metadata=make_fact_metadata(
            subject_key="user.tools.storage",
            assertion_kind="user_asserted",
            valid_from="2026-08-01",
        ),
    )
    entries_store.append_entry_once(
        conn,
        name="user-preferences.md",
        content="Expired fact must not export.",
        tags=["expired"],
        entry_id="export-expired",
        evidence_refs=[source],
        fact_metadata=make_fact_metadata(
            subject_key="user.expired",
            assertion_kind="observed",
            valid_to="2020-01-01",
        ),
    )


def test_json_current_memory_export_is_complete_and_digest_bound(ac_root: Path) -> None:
    cfg = config_mod.Config()
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    with fts.cursor() as conn:
        _seed_export_facts(conn)
        exported = build_current_memory_export(conn, cfg, format="json", now=now)

    content = str(exported["content"])
    payload = json.loads(content)
    assert payload["exported_at"] == now.isoformat()
    assert payload["fact_count"] == 1
    assert payload["facts"] == [
        {
            "assertion_kind": "user_asserted",
            "content": "User prefers local-first tools.",
            "id": "export-current",
            "origin": "derived-v1",
            "path": "user-preferences.md",
            "recorded_at": payload["facts"][0]["recorded_at"],
            "revision": payload["facts"][0]["revision"],
            "source_count": 1,
            "state": "current",
            "subject_key": "user.tools.storage",
            "tags": ["preference", "local-first"],
            "valid_from": "2026-08-01",
            "valid_to": "",
        }
    ]
    assert len(payload["facts"][0]["revision"]) == 64
    encoded = content.encode()
    assert exported["byte_count"] == len(encoded)
    assert exported["content_digest"] == hashlib.sha256(encoded).hexdigest()
    assert exported["file_name"] == "openchronicle-memory-2026-08-23.json"


def test_markdown_current_memory_export_is_human_readable(ac_root: Path) -> None:
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_export_facts(conn)
        exported = build_current_memory_export(
            conn,
            cfg,
            format="markdown",
            now=datetime(2026, 8, 23, 12, tzinfo=UTC),
        )

    content = str(exported["content"])
    assert content.startswith("# OpenChronicle Current Memory\n")
    assert "## user.tools.storage" in content
    assert "User prefers local-first tools." in content
    assert "Expired fact must not export." not in content
    assert exported["media_type"] == "text/markdown"
    assert exported["extension"] == "md"
