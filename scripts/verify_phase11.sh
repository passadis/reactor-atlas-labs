#!/usr/bin/env bash
# Phase 11 verification: one trace, the workbook, real alerts, content capture, availability.
#
# Application data only. ATLAS_SKIP_DLQ=1 skips the ~6 minute alert-firing path.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec timeout 3000 uv run python -m scripts.verify_phase11
