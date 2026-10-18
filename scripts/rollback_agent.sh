#!/usr/bin/env bash
# Roll the hosted agent back to the previous good version: the highest active version below the
# pinned one whose guardrail resolves to a real policy. One command, no build, no deploy.
#
#   bash scripts/rollback_agent.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
exec uv run python -m scripts.hosted_agent rollback
