from __future__ import annotations

import sys

import pytest

from openchronicle import packaged_entry


def test_worker_command_uses_source_python_when_not_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert packaged_entry.worker_command(
        packaged_entry.DOCUMENT_WORKER, ("python", "-m", "worker")
    ) == ("python", "-m", "worker")


def test_worker_command_uses_exact_frozen_self_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert packaged_entry.worker_command(
        packaged_entry.PROVIDER_WORKER, ("python", "-c", "secret")
    ) == (sys.executable, "--openchronicle-worker=provider")
    with pytest.raises(ValueError, match="allowlisted"):
        packaged_entry.worker_command("../../other", ("python",))


def test_packaged_entry_rejects_unknown_and_extra_arguments() -> None:
    assert packaged_entry.run(["--version"]) == 2
    assert packaged_entry.run(["--openchronicle-worker=provider", "extra"]) == 2
