#!/usr/bin/env bash
# Assign the atlas-gateway app role Atlas.Agent to every workload identity that calls tools.
#
# APIM's validate-jwt requires `roles` to contain Atlas.Agent (apim/policies/fragments/
# atlas-agent-auth.xml). App-role assignments are Entra objects, so Bicep cannot make them; this
# runs from postprovision once the identities exist. Idempotent: an existing assignment is skipped.
#
# Who holds it, and why:
#   web, orchestrator   run the agent tools in-process (Phase 03/06)
#   func                the MCP server's write tools call the ops API through the gateway
#   worker              Phase 10 remediation executor
#   foundry project MI  the toolbox's connection to the APIM MCP servers (prompt agent)
#   hosted agent        its Entra agent identity runs the same tools (Phase 05)
#
# Who deliberately does NOT: the signed-in operator. Governance proof 3 is exactly that - a valid
# token and a valid subscription key, refused 403 because a person is not an agent.
#
#   bash scripts/assign_gateway_roles.sh           # assign
#   bash scripts/assign_gateway_roles.sh --check   # exit non-zero if any is missing
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

# An exported value wins (verify_phase09 runs this with .env.local already loaded, and azd is not
# always reachable from a child shell on Windows); otherwise ask the azd environment.
env_get() { local v="${!1:-}"; [ -n "$v" ] && { echo "$v"; return; }; azd env get-value "$1" 2>/dev/null || true; }

RG="$(env_get AZURE_RESOURCE_GROUP)"
GATEWAY_APP_ID="$(env_get ATLAS_GATEWAY_APP_CLIENT_ID)"
ROLE_AGENT="9c4a6d74-3e2f-4081-ad5c-44f3dab7e634"   # stable id, scripts/create_app_registrations.sh
if [ -z "$RG" ] || [ -z "$GATEWAY_APP_ID" ]; then
  echo "assign_gateway_roles: AZURE_RESOURCE_GROUP or ATLAS_GATEWAY_APP_CLIENT_ID not set" >&2
  exit 1
fi

RESOURCE_SP="$(az ad sp show --id "$GATEWAY_APP_ID" --query id -o tsv 2>/dev/null)"
if [ -z "$RESOURCE_SP" ]; then
  # An app registration has no service principal until something creates one; tokens for its
  # audience cannot carry roles without it.
  RESOURCE_SP="$(az ad sp create --id "$GATEWAY_APP_ID" --query id -o tsv)"
fi
GRAPH="https://graph.microsoft.com/v1.0/servicePrincipals/${RESOURCE_SP}/appRoleAssignedTo"

principal_of_identity() {
  az identity list -g "$RG" --query "[?contains(name, '$1')].principalId | [0]" -o tsv 2>/dev/null
}

project_principal() {
  local account project sub
  account="$(env_get ATLAS_FOUNDRY_ACCOUNT_NAME)"
  project="$(env_get ATLAS_FOUNDRY_PROJECT_NAME)"
  sub="$(az account show --query id -o tsv)"
  az rest --method get --url "https://management.azure.com/subscriptions/${sub}/resourceGroups/${RG}/providers/Microsoft.CognitiveServices/accounts/${account}/projects/${project}?api-version=2025-06-01" \
    --query identity.principalId -o tsv 2>/dev/null
}

declare -a NAMES PRINCIPALS
add() { if [ -n "$2" ]; then NAMES+=("$1"); PRINCIPALS+=("$2"); else echo "  SKIP $1 (no principal yet)"; fi; }
add "web"          "$(principal_of_identity atlas-web)"
add "orchestrator" "$(principal_of_identity atlas-orch)"
add "func"         "$(principal_of_identity atlas-func)"
add "worker"       "$(principal_of_identity atlas-worker)"
add "foundry-project" "$(project_principal)"
add "hosted-agent" "$(env_get ATLAS_HOSTED_AGENT_PRINCIPAL_ID)"

existing="$(az rest --method get --url "$GRAPH" \
  --query "value[?appRoleId=='${ROLE_AGENT}'].principalId" -o tsv 2>/dev/null)"

missing=0
for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"; pid="${PRINCIPALS[$i]}"
  if grep -q "$pid" <<<"$existing"; then
    echo "  ok   Atlas.Agent -> ${name} (${pid})"
    continue
  fi
  if [ "$CHECK" = "1" ]; then
    echo "  MISSING Atlas.Agent -> ${name} (${pid})"; missing=1; continue
  fi
  if az rest --method post --url "$GRAPH" --headers "Content-Type=application/json" \
       --body "{\"principalId\":\"${pid}\",\"resourceId\":\"${RESOURCE_SP}\",\"appRoleId\":\"${ROLE_AGENT}\"}" \
       >/dev/null 2>&1; then
    echo "  new  Atlas.Agent -> ${name} (${pid})"
  else
    echo "  FAIL Atlas.Agent -> ${name} (${pid})"; missing=1
  fi
done

# Tokens are cached for up to 24 h by managed identity endpoints; a role added after a token was
# minted appears only on the next token. Say so rather than letting a 403 look like a policy bug.
[ "$CHECK" = "0" ] && echo "note: running workloads pick up a new role on their next token (restart to force)"
exit "$missing"
