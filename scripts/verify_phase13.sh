#!/usr/bin/env bash
# Phase 13 verification: CI, the evaluate-only gate, rollback, the eval-required rule, nightly.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec timeout 900 uv run python -m scripts.verify_phase13
