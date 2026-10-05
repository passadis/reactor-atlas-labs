#!/usr/bin/env bash
# Episode 3/4 chaos beat: the ops API goes down - as DATA, never by stopping the Function App
# (CLAUDE.md 9.3). Restore with `bash scripts/chaos/ops_api_down.sh off`.
#
#   bash scripts/chaos/ops_api_down.sh on [seconds]   # default 120, self-expiring
#   bash scripts/chaos/ops_api_down.sh off
#   --apply / --revert / --status are accepted as aliases (the Phase 14 drill convention).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
export PYTHONIOENCODING=utf-8
action="${1:-on}"; seconds="${2:-120}"
case "$action" in --apply) action=on ;; --revert) action=off ;; --status) action=status ;; esac
exec uv run python -m scripts.chaos_flag "$action" --seconds "$seconds"
