#!/usr/bin/env bash
# Phase 02 verification: seed data in Cosmos and AI Search, against deployed resources.
# Idempotent. Check 5 re-runs the seeder and proves it changes nothing.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec uv run python -m scripts.verify_phase02
