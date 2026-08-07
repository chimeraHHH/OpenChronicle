from contextlib import contextmanager
from pathlib import Path

from openchronicle.store import entries as entries_mod
from openchronicle.store import files as files_mod
from openchronicle.store import fts
from openchronicle.writer import tools as wtools


def test_dispatch_append_and_commit(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="project-foo.md", description="Foo project", tags=["project"]
        )
        state = wtools.CommitState()
        r1 = wtools.dispatch(
            "append",
            {"path": "project-foo.md", "content": "Bar happened.", "tags": ["bar"]},
            conn=conn, soft_limit_tokens=20000, state=state,
        )
        assert r1["ok"]
        r2 = wtools.dispatch("commit", {"summary": "wrote 1"}, conn=conn, soft_limit_tokens=20000, state=state)
        assert r2["ok"]
        assert state.committed
        assert state.summary == "wrote 1"
        assert len(state.written_ids) == 1


def test_dispatch_read_memory(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="tool-cursor.md", description="Cursor editor", tags=["tool"]
        )
        state = wtools.CommitState()
        wtools.dispatch(
            "append",
            {"path": "tool-cursor.md", "content": "User uses Cursor.", "tags": ["editor"]},
            conn=conn, soft_limit_tokens=20000, state=state,
        )
        r = wtools.dispatch(
            "read_memory", {"path": "tool-cursor.md"},
            conn=conn, soft_limit_tokens=20000, state=state,
        )
        assert r["path"] == "tool-cursor.md"
        assert len(r["entries"]) == 1


def test_dispatch_search(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="topic-rust.md", description="Rust learning", tags=["topic"]
        )
        state = wtools.CommitState()
        wtools.dispatch(
            "append",
            {"path": "topic-rust.md", "content": "User learning async Rust.", "tags": ["rust"]},
            conn=conn, soft_limit_tokens=20000, state=state,
        )
        r = wtools.dispatch(
            "search_memory", {"query": "async", "top_k": 3},
            conn=conn, soft_limit_tokens=20000, state=state,
        )
        assert len(r["results"]) == 1
        assert r["results"][0]["path"] == "topic-rust.md"


def test_flag_compact_updates_markdown_and_fts_together(
    ac_root: Path, monkeypatch
) -> None:
    name = "topic-compact-flag.md"
    real_store_lock = files_mod.store_write_lock
    real_file_lock = files_mod.file_lock
    real_update = files_mod._update_frontmatter_unlocked
    real_set_flag = fts.set_needs_compact
    global_depth = 0
    path_depth = 0

    @contextmanager
    def tracked_store_lock():
        nonlocal global_depth
        with real_store_lock():
            global_depth += 1
            try:
                yield
            finally:
                global_depth -= 1

    @contextmanager
    def tracked_file_lock(path: Path):
        nonlocal path_depth
        assert global_depth > 0
        with real_file_lock(path):
            path_depth += 1
            try:
                yield
            finally:
                path_depth -= 1

    def asserted_update(path: Path, updates: dict) -> None:
        assert global_depth > 0 and path_depth > 0
        real_update(path, updates)

    def asserted_set_flag(conn, path: str, value: bool) -> None:
        assert global_depth > 0 and path_depth > 0
        real_set_flag(conn, path, value)

    monkeypatch.setattr(files_mod, "store_write_lock", tracked_store_lock)
    monkeypatch.setattr(files_mod, "file_lock", tracked_file_lock)
    monkeypatch.setattr(files_mod, "_update_frontmatter_unlocked", asserted_update)
    monkeypatch.setattr(fts, "set_needs_compact", asserted_set_flag)

    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name=name, description="compact flag", tags=["topic"]
        )
        state = wtools.CommitState()

        result = wtools.tool_flag_compact(
            conn,
            path=name,
            reason="test threshold",
            state=state,
        )

        assert result == {"ok": True}
        assert state.flagged_compact == [name]
        assert files_mod.read_file(files_mod.memory_path(name)).needs_compact is True
        assert fts.get_file(conn, name).needs_compact == 1
