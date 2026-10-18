#!/usr/bin/env bash
# Phase 05 verification: hosted agent versions, guardrail, identity, web equivalence, traces,
# rollback (then re-pin forward), toolbox and prompt agent. Idempotent: ends pinned where it began.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec timeout 1500 uv run python -m scripts.verify_phase05
