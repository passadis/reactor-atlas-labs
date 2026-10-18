#!/usr/bin/env bash
# Deploy the Logic Apps Standard project (logicapps/) to the provisioned Logic App. Phase 10.
#
# A zip deploy of the workflow definitions - a control-plane write, so pre-broadcast only
# (CLAUDE.md section 9). Idempotent: the same zip deploys the same workflows.
#
#   bash scripts/deploy_logicapps.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONIOENCODING=utf-8

RG="${AZURE_RESOURCE_GROUP:-$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null)}"
SITE="$(az logicapp list -g "$RG" --query "[0].name" -o tsv)"
[ -n "$SITE" ] || { echo "no Logic App in $RG" >&2; exit 1; }

ZIP="$(mktemp -d)/logicapps.zip"
uv run python - "$ZIP" <<'PY'
import sys, zipfile, pathlib
root = pathlib.Path("logicapps")
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix == ".json":
            z.write(path, path.relative_to(root).as_posix())
            print("  +", path.relative_to(root).as_posix())
PY
echo "deploying to $SITE"
az logicapp deployment source config-zip -g "$RG" -n "$SITE" --src "$ZIP" -o none
az rest --method get --url "https://management.azure.com$(az logicapp show -g "$RG" -n "$SITE" --query id -o tsv)/hostruntime/runtime/webhooks/workflow/api/management/workflows?api-version=2023-12-01" \
  --query "[].{name:name, state:properties.state}" -o table 2>/dev/null || true
