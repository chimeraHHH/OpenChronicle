"""PyInstaller entry point for the bundled one-request desktop bridge."""

from __future__ import annotations

from openchronicle.packaged_entry import run

if __name__ == "__main__":
    raise SystemExit(run())
