#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

exec "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/paper_engine.py"
