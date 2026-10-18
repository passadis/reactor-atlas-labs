#!/usr/bin/env bash
# Pin the hosted agent endpoint to one version (100% of traffic). A pointer flip on the agent
# endpoint, not a build or a deploy (CLAUDE.md 9.2): safe to run on stage, takes seconds.
#
#   bash scripts/pin_agent_version.sh 4
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[ $# -eq 1 ] || { echo "usage: $0 <version>" >&2; exit 2; }
export PYTHONIOENCODING=utf-8
exec uv run python -m scripts.hosted_agent pin "$1"
