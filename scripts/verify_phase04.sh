#!/usr/bin/env bash
# Phase 04 verification: the deployed web app, through Entra built-in auth, like a browser.
# Idempotent. Writes application data only (turns, cases, sessions); cleared by make demo-reset.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec timeout 1200 uv run python -m scripts.verify_phase04
