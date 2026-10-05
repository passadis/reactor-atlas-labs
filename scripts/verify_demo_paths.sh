#!/usr/bin/env bash
# Phase 14: every on-stage demo path, each from a cold demo-reset. See scripts/verify_demo_paths.py.
#   bash scripts/verify_demo_paths.sh                 # all five paths
#   bash scripts/verify_demo_paths.sh --path ep1 encore
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
uv run python -m scripts.verify_demo_paths "$@"
