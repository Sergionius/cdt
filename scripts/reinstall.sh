#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

pipx uninstall cdt || true
pipx install -e .

echo "✅ cdt reinstalled via pipx"
