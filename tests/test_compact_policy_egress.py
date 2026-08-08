from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openchronicle import config as config_mod
from openchronicle.store import entries as entries_store
from openchronicle.store import files as files_store
from openchronicle.store import fts
from openchronicle.writer import compact as compact_mod


def _response(text: str) -> SimpleNamespace:
    message = SimpleNamespace(content=text)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.mark.parametrize("legacy_unmarked", [False, True])
def test_compactor_never_sends_automation_or_legacy_unmarked_entries(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_unmarked: bool,
) -> None:
    name = "topic-hidden-compact.md"
    marker = "LEGACY_AUTOMATION_REMOTE_MARKER"
    calls = 0

    def forbidden_provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("hidden entry reached compact provider")

    monkeypatch.setattr(compact_mod.llm_mod, "call_llm", forbidden_provider)
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name=name,
            description="compact privacy fixture",
            tags=["topic"],
        )
        entries_store.append_entry(
            conn,
            name=name,
            content=marker,
            tags=["automation"],
        )
        if legacy_unmarked:
            path = files_store.memory_path(name)
            files_store.atomic_write_text(
                path,
                path.read_text(encoding="utf-8").replace(
                    " #oc-origin:automation-v1", ""
                ),
            )

        result = compact_mod.compact_file(config_mod.Config(), conn, name=name)

    assert calls == 0
    assert result.accepted is False
    assert "manual-v1" in result.note


def test_compactor_may_send_explicit_manual_entry(ac_root: Path, monkeypatch) -> None:
    name = "topic-manual-compact.md"
    marker = "EXPLICIT_MANUAL_COMPACTION_MARKER"
    payloads: list[str] = []

    def provider(_cfg, _stage, *, messages, **_kwargs):
        payloads.append(str(messages[-1]["content"]))
        path = files_store.memory_path(name)
        return _response(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(compact_mod.llm_mod, "call_llm", provider)
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name=name,
            description="manual compact fixture",
            tags=["topic"],
        )
        entries_store.append_entry(
            conn,
            name=name,
            content=marker,
            tags=["manual"],
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )

        result = compact_mod.compact_file(config_mod.Config(), conn, name=name)

    assert result.accepted is True
    assert len(payloads) == 1
    assert marker in payloads[0]


def test_compactor_cannot_rewrite_trust_markers_or_frontmatter(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "topic-manual-compact-guard.md"
    original_description = "LOCAL_FRONTMATTER_AUTHORITY"

    def provider(_cfg, _stage, *, messages, **_kwargs):
        original = files_store.memory_path(name).read_text(encoding="utf-8")
        tampered = original.replace(
            original_description, "MODEL_FRONTMATTER_INJECTION"
        ).replace(" #oc-origin:manual-v1", " #oc-origin:automation-v1")
        return _response(tampered)

    monkeypatch.setattr(compact_mod.llm_mod, "call_llm", provider)
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name=name,
            description=original_description,
            tags=["topic"],
        )
        entries_store.append_entry(
            conn,
            name=name,
            content="ManualAlpha ManualBeta ManualGamma ManualDelta",
            tags=["manual"],
            origin=files_store.MANUAL_ENTRY_ORIGIN,
        )

        result = compact_mod.compact_file(config_mod.Config(), conn, name=name)
        parsed = files_store.read_file(files_store.memory_path(name))

    assert result.accepted is False
    assert "trust markers" in result.note
    assert parsed.description == original_description
    assert parsed.entries[0].origin == files_store.MANUAL_ENTRY_ORIGIN
