#!/usr/bin/env bash
# Phase 09 verification: APIM as the AI gateway - APIs, governance proofs, the only-via-APIM
# claim, token metrics, MCP telemetry and one-description-four-surfaces.
#
# Check 6 changes one APIM tool description and restores it (a control-plane write, rehearsal
# only). ATLAS_SKIP_DESCRIPTION_CHANGE=1 skips just that and says so.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec timeout 2400 uv run python -m scripts.verify_phase09
