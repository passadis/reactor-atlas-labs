#!/usr/bin/env bash
# T-10 routine: warm every surface the episode touches (read-only). See scripts/warm.py.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8
uv run python -m scripts.warm
