#!/usr/bin/env bash
# Copy the atlas_common package into a function app folder before azd zips it.
#
# atlas_common is a uv workspace member, not a PyPI package, so a Flex Consumption remote build
# cannot `pip install` it. Vendoring the source keeps one copy in git (src/atlas_common) and puts a
# second, gitignored copy exactly where the Functions worker imports from. Run by the prepackage
# hook of each function service in azure.yaml; safe to run repeatedly.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:?usage: vendor_atlas_common.sh <function app folder, relative to the repo root>}"
DEST="$ROOT/$TARGET/atlas_common"

rm -rf "$DEST"
cp -R "$ROOT/src/atlas_common/atlas_common" "$DEST"
find "$DEST" -name "__pycache__" -type d -prune -exec rm -rf {} +
echo "vendored atlas_common into $TARGET ($(find "$DEST" -name '*.py' | wc -l | tr -d ' ') modules)"
