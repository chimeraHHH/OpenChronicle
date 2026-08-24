#!/usr/bin/env python3
"""Run OpenChronicle through a pinned LongMemEval-V2 checkout."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from openchronicle.evaluation.longmemeval_v2 import (
    UPSTREAM_COMMIT,
    default_memory_config,
    register_upstream_backend,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--upstream-root", type=Path, required=True)
    parser.add_argument("--help", action="store_true", dest="wrapper_help")
    args, upstream_args = parser.parse_known_args(argv)
    upstream_root = args.upstream_root.expanduser().resolve()
    commit = _git(upstream_root, "rev-parse", "HEAD")
    if commit != UPSTREAM_COMMIT:
        raise SystemExit(
            f"LongMemEval-V2 checkout must be pinned to {UPSTREAM_COMMIT}; got {commit}"
        )

    if str(upstream_root) not in sys.path:
        sys.path.insert(0, str(upstream_root))
    from evaluation import run_eval

    run_eval.METHODS.add("openchronicle")
    if args.wrapper_help and not upstream_args:
        previous = sys.argv
        try:
            sys.argv = ["evaluation.run_eval", "--help"]
            run_eval.main()
        finally:
            sys.argv = previous
        return 0

    try:
        register_upstream_backend(upstream_root)
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "LongMemEval-V2 dependencies are incomplete; activate its documented "
            f"Python 3.11 environment first (missing module: {exc.name})."
        ) from None
    original_builder = run_eval.build_memory_config

    def build_memory_config(parsed_args, data_root):
        if parsed_args.method == "openchronicle":
            return default_memory_config()
        return original_builder(parsed_args, data_root)

    run_eval.build_memory_config = build_memory_config
    previous = sys.argv
    try:
        sys.argv = ["evaluation.run_eval", *upstream_args]
        run_eval.main()
    finally:
        sys.argv = previous
    return 0


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise SystemExit(f"unable to inspect LongMemEval-V2 checkout: {root}")
    return completed.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
