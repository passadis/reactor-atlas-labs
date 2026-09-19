#!/usr/bin/env bash
# Phase 03 verification: the incident agent against the deployed model, Cosmos, Search and Foundry.
# Idempotent. Writes one demo session (removed at the end) and a prompt-agent version only on drift.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
# Hard ceiling: a stalled credential or network call fails the phase instead of hanging make.
exec timeout 1500 uv run python -m scripts.verify_phase03
