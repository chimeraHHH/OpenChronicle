"""Logging surface regression tests."""

from __future__ import annotations

import logging

from openchronicle import logger as logger_mod


def test_setup_registers_daily_wrap_file_sink(ac_root, monkeypatch) -> None:
    calls: list[tuple[str, str, int]] = []
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)

    def record_sink(name: str, filename: str, *, level: int) -> logging.Logger:
        calls.append((name, filename, level))
        return logging.getLogger(name)

    monkeypatch.setattr(logger_mod, "_INITIALIZED", False)
    monkeypatch.setattr(logger_mod, "_sink", record_sink)
    try:
        logger_mod.setup(console=False)
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    assert ("openchronicle.daily_wrap", "daily-wrap.log", logging.INFO) in calls
