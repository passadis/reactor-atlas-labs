#!/usr/bin/env bash
# Phase 12 verification: evaluation scorecards, thresholds, local checks, red team, comparison.
#
# Verifies the recorded runs (`make eval`, `make redteam` produce them). ATLAS_EVAL_LIVE=1 adds a
# fresh four-case live run.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec timeout 1800 uv run python -m scripts.verify_phase12
