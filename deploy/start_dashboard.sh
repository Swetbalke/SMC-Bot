#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

exec "$PROJECT_DIR/venv/bin/python" -m streamlit run "$PROJECT_DIR/dashboard.py" \
  --server.address 127.0.0.1 \
  --server.port 8501 \
  --server.headless true \
  --browser.gatherUsageStats false
