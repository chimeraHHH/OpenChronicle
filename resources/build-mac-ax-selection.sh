#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCH="$(uname -m)"
TARGET="${ARCH}-apple-macos12.0"

swiftc "$SCRIPT_DIR/mac-ax-selection.swift" \
  -o "$SCRIPT_DIR/mac-ax-selection" \
  -O \
  -target "$TARGET" \
  -swift-version 5

echo "Built $SCRIPT_DIR/mac-ax-selection"
