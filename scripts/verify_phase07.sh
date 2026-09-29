#!/usr/bin/env bash
# Phase 07 verification: checkpoints, the policy gate, approval and kill/resume recovery.
#
# Check 2 scales the orchestrator to 0 and back (several minutes, a control-plane write).
# ATLAS_SKIP_KILL=1 skips only that step and says so in the output.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec timeout 2400 uv run python -m scripts.verify_phase07
