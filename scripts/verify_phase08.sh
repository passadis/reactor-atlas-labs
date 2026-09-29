#!/usr/bin/env bash
# Phase 08 verification: the operations API and MCP tool server on Functions Flex Consumption.
#
# Application-data writes only (a ticket, a verification approval, one queued command).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec timeout 900 uv run python -m scripts.verify_phase08
