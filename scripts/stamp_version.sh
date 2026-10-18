#!/usr/bin/env bash
# Stamp the git commit into atlas_common before any service is packaged (azd project prepackage).
#
# Every span carries it as atlas.agent_version / service.version (Phase 11), so a trace says exactly
# which build produced it. "-dirty" marks uncommitted changes - honest about rehearsal builds.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
sha="$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
git diff --quiet HEAD 2>/dev/null || sha="${sha}-dirty"
printf '"""Written by scripts/stamp_version.sh at package time. Not in source control."""\n\nVERSION = "%s"\n' "$sha" \
  > src/atlas_common/atlas_common/_version.py
echo "stamped version ${sha}"
