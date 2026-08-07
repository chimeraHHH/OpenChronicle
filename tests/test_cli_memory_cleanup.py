from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from openchronicle import cli
from openchronicle.store import entries as entries_mod
from openchronicle.store import files as files_mod
from openchronicle.store import fts


def test_clean_memory_serializes_with_concurrent_create(
    ac_root: Path, monkeypatch
) -> None:
    """Clean completes as one store mutation before a waiting writer proceeds."""
    before_name = "topic-before-clean.md"
    after_name = "topic-after-clean.md"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name=before_name, description="before clean", tags=["topic"]
        )
        entries_mod.append_entry(
            conn, name=before_name, content="removed by clean", tags=["topic"]
        )

    real_store_lock = files_mod.store_write_lock
    clean_inside_lock = threading.Event()
    create_lock_attempted = threading.Event()
    release_clean = threading.Event()
    create_done = threading.Event()
    clean_results: list[tuple[int, int]] = []
    errors: list[BaseException] = []

    @contextmanager
    def paused_store_lock():
        is_cleaner = threading.current_thread().name == "clean-memory"
        if not is_cleaner:
            create_lock_attempted.set()
        with real_store_lock():
            if is_cleaner:
                clean_inside_lock.set()
                if not release_clean.wait(timeout=5):
                    raise TimeoutError("test did not release memory cleanup")
            yield

    monkeypatch.setattr(files_mod, "store_write_lock", paused_store_lock)

    def clean_worker() -> None:
        try:
            clean_results.append(cli._clean_memory())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def create_worker() -> None:
        try:
            with fts.cursor() as conn:
                entries_mod.create_file(
                    conn,
                    name=after_name,
                    description="created after clean",
                    tags=["topic"],
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            create_done.set()

    clean_thread = threading.Thread(target=clean_worker, name="clean-memory")
    create_thread = threading.Thread(target=create_worker, name="create-after-clean")
    clean_thread.start()
    assert clean_inside_lock.wait(timeout=5)
    create_thread.start()
    assert create_lock_attempted.wait(timeout=5)
    try:
        assert not create_done.wait(timeout=0.1), "create bypassed memory cleanup lock"
    finally:
        release_clean.set()

    clean_thread.join(timeout=10)
    create_thread.join(timeout=10)
    assert not clean_thread.is_alive()
    assert not create_thread.is_alive()
    assert errors == []
    assert clean_results == [(1, 1)]

    assert not files_mod.memory_path(before_name).exists()
    assert files_mod.memory_path(after_name).exists()
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        rows = conn.execute("SELECT path FROM files").fetchall()
    assert [row["path"] for row in rows] == [after_name]


def test_clean_memory_keeps_markdown_if_index_clear_cannot_start(
    ac_root: Path, monkeypatch
) -> None:
    name = "topic-private-clean.md"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name=name, description="private", tags=["topic"]
        )
        entry_id = entries_mod.append_entry(
            conn, name=name, content="SEARCHABLE_PRIVATE_MARKER", tags=["topic"]
        )
    path = files_mod.memory_path(name)
    real_cursor = fts.cursor

    @contextmanager
    def unavailable_index():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(fts, "cursor", unavailable_index)
    with pytest.raises(RuntimeError, match="database unavailable"):
        cli._clean_memory()
    monkeypatch.setattr(fts, "cursor", real_cursor)

    assert path.exists()
    with fts.cursor() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id=?", (entry_id,)
        ).fetchone()[0] == 1


def test_clean_memory_removes_crash_orphan_temp(ac_root: Path) -> None:
    name = "topic-orphan-temp.md"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name=name, description="private", tags=["topic"]
        )
        entries_mod.append_entry(
            conn, name=name, content="PRIVATE_MEMORY_MARKER", tags=["topic"]
        )
    path = files_mod.memory_path(name)
    orphan = path.parent / f".{path.name}.deadbeef.tmp"
    orphan.write_text("SENSITIVE_CRASH_COPY", encoding="utf-8")

    assert files_mod.is_memory_temp_name(orphan.name) is True
    assert cli._clean_memory() == (2, 1)
    assert not path.exists()
    assert not orphan.exists()
