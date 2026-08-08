#!/usr/bin/env python3
"""Verify a redacted macOS live AX/privacy audit report against its manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from live_ax_privacy import verify_report

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "tests" / "live" / "macos_ax_privacy" / "manifest.json"


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"unreadable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"expected a JSON object: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="validate shape/redaction while permitting skipped live prerequisites",
    )
    args = parser.parse_args()

    violations = verify_report(
        _object(args.report.resolve()),
        _object(args.manifest.resolve()),
        allow_incomplete=args.allow_incomplete,
    )
    if violations:
        print("live AX privacy report verification: FAIL")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("live AX privacy report verification: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
