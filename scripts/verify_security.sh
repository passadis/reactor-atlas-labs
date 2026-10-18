#!/usr/bin/env bash
# Phase 14 security pass (PHASE-14 section 2). See scripts/verify_security.py.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
uv run python -m scripts.verify_security
