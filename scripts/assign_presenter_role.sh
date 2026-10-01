#!/usr/bin/env bash
# Give the signed-in presenter the atlas-web app role Atlas.Director (Phase 10 decision, 2026-10-01).
#
# Why: the canonical plan breaks glass on the frozen payments service, which only a director may
# approve. Without this the presenter resolves to `manager` and every console approval of that plan
# is refused - so the end-to-end run could never be approved through the console. The presenter can
# still NARROW in the UI (act as manager: the Episode 2 refusal; act as readonly: the Episode 4
# unauthorized beat). Narrowing only ever lowers privilege.
#
# Idempotent. Remove the assignment to undo.
#
#   bash scripts/assign_presenter_role.sh           # assign
#   bash scripts/assign_presenter_role.sh --check   # exit non-zero if missing
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

env_get() { local v="${!1:-}"; [ -n "$v" ] && { echo "$v"; return; }; azd env get-value "$1" 2>/dev/null || true; }

WEB_APP_ID="$(env_get ATLAS_WEB_APP_CLIENT_ID)"
ROLE_DIRECTOR="ad5b7e85-4f30-4192-be6d-55a4ebc8f745"   # stable id, scripts/create_app_registrations.sh
[ -n "$WEB_APP_ID" ] || { echo "ATLAS_WEB_APP_CLIENT_ID not set" >&2; exit 1; }

SP="$(az ad sp show --id "$WEB_APP_ID" --query id -o tsv 2>/dev/null)"
ME="$(az ad signed-in-user show --query id -o tsv)"
GRAPH="https://graph.microsoft.com/v1.0/servicePrincipals/${SP}/appRoleAssignedTo"

existing="$(az rest --method get --url "$GRAPH" -o json 2>/dev/null)"
if grep -q "\"appRoleId\": \"${ROLE_DIRECTOR}\"" <<<"$(echo "$existing" | tr -d '\r')" \
   && grep -q "$ME" <<<"$existing"; then
  # Both appear; confirm they are the same assignment.
  if uv run python -c "
import json,sys
d=json.loads(sys.stdin.read())
sys.exit(0 if any(a['principalId']=='$ME' and a['appRoleId']=='$ROLE_DIRECTOR' for a in d.get('value',[])) else 1)
" <<<"$existing"; then
    echo "  ok   Atlas.Director -> presenter ($ME)"
    exit 0
  fi
fi
if [ "$CHECK" = "1" ]; then
  echo "  MISSING Atlas.Director -> presenter ($ME)"
  exit 1
fi
az rest --method post --url "$GRAPH" --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"${ME}\",\"resourceId\":\"${SP}\",\"appRoleId\":\"${ROLE_DIRECTOR}\"}" \
  -o none && echo "  new  Atlas.Director -> presenter ($ME)"
echo "note: the role appears in the web app after the next sign-in (the token carries it)"
