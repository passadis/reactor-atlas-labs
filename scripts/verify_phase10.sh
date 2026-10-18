#!/usr/bin/env bash
# Phase 10 verification: approvals, asynchronous remediation, DLQ and replay, one trace.
#
# Application data only. ATLAS_SKIP_TIMEOUT=1 skips the ~3 minute timeout path.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec timeout 2400 uv run python -m scripts.verify_phase10
