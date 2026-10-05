#!/usr/bin/env bash
# Smoke check: ATLAS at its known-good baseline and serving. Read-only.
# ATLAS_SMOKE_FULL=1 adds the canonical case end to end (about 2 minutes).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec timeout 900 uv run python -m scripts.verify_smoke
