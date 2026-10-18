#!/usr/bin/env bash
# Phase 06 verification: the multi-agent workflow, through the deployed web app and orchestrator.
# Idempotent. Writes application data only (cases); cleared by make demo-reset.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec timeout 1800 uv run python -m scripts.verify_phase06
