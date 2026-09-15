#!/usr/bin/env bash
# Phase 01 verification. Idempotent, read-only, exits non-zero on any failure.
#
# Reads .env.local (written by scripts/postprovision.sh). Run it directly any time:
#   bash scripts/verify_phase01.sh
set -uo pipefail
export PYTHONIOENCODING=utf-8

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env.local ]; then
  echo "verify_phase01: .env.local missing. Run 'azd provision' or 'bash scripts/postprovision.sh'." >&2
  exit 1
fi

get() { grep -E "^$1=" .env.local | head -1 | cut -d= -f2-; }

RG="$(get AZURE_RESOURCE_GROUP)"
PASS=0; FAIL=0
rows=()
ok()   { rows+=("PASS|$1|$2"); PASS=$((PASS+1)); }
bad()  { rows+=("FAIL|$1|$2"); FAIL=$((FAIL+1)); }
check(){ if [ "$1" = "$2" ]; then ok "$3" "$4"; else bad "$3" "expected '$2', got '$1'"; fi; }

echo "ATLAS Phase 01 verification  (rg=${RG})"
echo "==============================================="
echo

# 1 -- resource group and inventory ------------------------------------------
COUNT=$(az resource list -g "$RG" --query "length(@)" -o tsv 2>/dev/null || echo 0)
if [ "${COUNT:-0}" -ge 15 ]; then ok "1 resource group" "$COUNT resources"; else bad "1 resource group" "only ${COUNT:-0} resources"; fi

# 2 -- Foundry project endpoint answers an authenticated call -----------------
PROJECT_EP="$(get FOUNDRY_PROJECT_ENDPOINT)"
TOKEN=$(az account get-access-token --scope "https://ai.azure.com/.default" --query accessToken -o tsv 2>/dev/null)
if [ -n "$TOKEN" ] && [ -n "$PROJECT_EP" ]; then
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" \
         "${PROJECT_EP}/openai/v1/models?api-version=v1" --max-time 30 2>/dev/null)
  if [ "$CODE" = "200" ]; then ok "2 foundry project endpoint" "HTTP 200"; else bad "2 foundry project endpoint" "HTTP ${CODE:-none}"; fi
else
  bad "2 foundry project endpoint" "no token or endpoint"
fi

# 3 -- the three model aliases, all Succeeded --------------------------------
ACCT="$(get ATLAS_FOUNDRY_ACCOUNT_NAME)"
for alias in gpt-main gpt-judge embed-main; do
  STATE=$(az cognitiveservices account deployment show -n "$ACCT" -g "$RG" --deployment-name "$alias" \
          --query "properties.provisioningState" -o tsv 2>/dev/null)
  check "${STATE:-missing}" "Succeeded" "3 model ${alias}" "Succeeded"
done

# 4 -- Cosmos: containers present, local auth disabled -----------------------
COSMOS="$(get ATLAS_COSMOS_ACCOUNT_NAME)"
DBN="$(get ATLAS_COSMOS_DATABASE)"
LOCAL_AUTH=$(az cosmosdb show -n "$COSMOS" -g "$RG" --query "disableLocalAuth" -o tsv 2>/dev/null)
check "${LOCAL_AUTH:-unknown}" "true" "4 cosmos disableLocalAuth" "true"
for c in incidents tickets approvals workflow-checkpoints idempotency sessions eval-runs; do
  PK=$(az cosmosdb sql container show -a "$COSMOS" -g "$RG" -d "$DBN" -n "$c" \
       --query "resource.partitionKey.paths[0]" -o tsv 2>/dev/null)
  if [ -n "$PK" ]; then ok "4 cosmos container ${c}" "pk ${PK}"; else bad "4 cosmos container ${c}" "missing"; fi
done

# 5 -- AI Search: running and RBAC-only --------------------------------------
SRCH="$(get ATLAS_SEARCH_SERVICE_NAME)"
SSTATE=$(az search service show -n "$SRCH" -g "$RG" --query "status" -o tsv 2>/dev/null)
SAUTH=$(az rest --method GET --url "https://management.azure.com/subscriptions/$(get AZURE_SUBSCRIPTION_ID)/resourceGroups/${RG}/providers/Microsoft.Search/searchServices/${SRCH}?api-version=2025-05-01" \
        --query "properties.disableLocalAuth" -o tsv 2>/dev/null)
if [ "${SSTATE,,}" = "running" ]; then ok "5 search status" "running"; else bad "5 search status" "${SSTATE:-unknown}"; fi
check "${SAUTH:-unknown}" "true" "5 search disableLocalAuth" "true"

# 6 -- Service Bus entities with the settings the demo depends on ------------
SBNS="$(get ATLAS_SERVICEBUS_NAMESPACE)"
SESS=$(az servicebus queue show --namespace-name "$SBNS" -g "$RG" -n remediation-commands --query "requiresSession" -o tsv 2>/dev/null)
DEDUP=$(az servicebus queue show --namespace-name "$SBNS" -g "$RG" -n remediation-commands --query "requiresDuplicateDetection" -o tsv 2>/dev/null)
SKU=$(az servicebus namespace show -n "$SBNS" -g "$RG" --query "sku.name" -o tsv 2>/dev/null)
check "${SKU:-unknown}" "Premium" "6 servicebus sku" "Premium"
check "${SESS:-unknown}" "true" "6 commands requiresSession" "true"
check "${DEDUP:-unknown}" "true" "6 commands dedup" "true"
for sub in web-progress audit; do
  S=$(az servicebus topic subscription show --namespace-name "$SBNS" -g "$RG" --topic-name remediation-events -n "$sub" --query "name" -o tsv 2>/dev/null)
  if [ -n "$S" ]; then ok "6 subscription ${sub}" "present"; else bad "6 subscription ${sub}" "missing"; fi
done

# 7 -- Container apps running with the right identity ------------------------
for pair in "ATLAS_WEB_APP_NAME:web" "ATLAS_ORCHESTRATOR_APP_NAME:orchestrator" "ATLAS_WORKER_APP_NAME:worker"; do
  VAR="${pair%%:*}"; LABEL="${pair##*:}"
  APP="$(get "$VAR")"
  ST=$(az containerapp show -n "$APP" -g "$RG" --query "properties.provisioningState" -o tsv 2>/dev/null)
  UAMI=$(az containerapp show -n "$APP" -g "$RG" --query "length(identity.userAssignedIdentities)" -o tsv 2>/dev/null)
  if [ "$ST" = "Succeeded" ] && [ "${UAMI:-0}" -ge 1 ]; then
    ok "7 containerapp ${LABEL}" "Succeeded, ${UAMI} UAMI"
  else
    bad "7 containerapp ${LABEL}" "state=${ST:-none} uami=${UAMI:-0}"
  fi
done

# 8 -- function apps running --------------------------------------------------
for pair in "ATLAS_FUNC_API_NAME:ops-api" "ATLAS_FUNC_MCP_NAME:ops-mcp"; do
  VAR="${pair%%:*}"; LABEL="${pair##*:}"
  APP="$(get "$VAR")"
  ST=$(az functionapp show -n "$APP" -g "$RG" --query "state" -o tsv 2>/dev/null)
  check "${ST:-unknown}" "Running" "8 functionapp ${LABEL}" "Running"
done

# 9 -- APIM provisioned, logger attached -------------------------------------
APIM="$(get ATLAS_APIM_NAME)"
AST=$(az apim show -n "$APIM" -g "$RG" --query "provisioningState" -o tsv 2>/dev/null)
check "${AST:-unknown}" "Succeeded" "9 apim provisioning" "Succeeded"
LOGGER=$(az rest --method GET --url "https://management.azure.com/subscriptions/$(get AZURE_SUBSCRIPTION_ID)/resourceGroups/${RG}/providers/Microsoft.ApiManagement/service/${APIM}/loggers/atlas-appinsights?api-version=2024-05-01" \
         --query "properties.loggerType" -o tsv 2>/dev/null)
check "${LOGGER:-missing}" "applicationInsights" "9 apim logger" "applicationInsights"

# 10 -- Logic Apps host running ----------------------------------------------
LOGIC="$(get ATLAS_LOGICAPP_NAME)"
LST=$(az functionapp show -n "$LOGIC" -g "$RG" --query "state" -o tsv 2>/dev/null)
check "${LST:-unknown}" "Running" "10 logic app host" "Running"

# 11 -- role assignments ------------------------------------------------------
# Cosmos data-plane assignments are the ones that silently break everything.
CRA=$(az cosmosdb sql role assignment list --account-name "$COSMOS" -g "$RG" --query "length(@)" -o tsv 2>/dev/null)
if [ "${CRA:-0}" -ge 5 ]; then ok "11 cosmos sql role assignments" "${CRA} found"; else bad "11 cosmos sql role assignments" "only ${CRA:-0}, expected >= 5"; fi

ARA=$(az role assignment list --scope "/subscriptions/$(get AZURE_SUBSCRIPTION_ID)/resourceGroups/${RG}" --query "length(@)" -o tsv 2>/dev/null)
if [ "${ARA:-0}" -ge 20 ]; then ok "11 arm role assignments" "${ARA} found"; else bad "11 arm role assignments" "only ${ARA:-0}, expected >= 20"; fi

# 12 -- no secrets leaked into azd outputs ------------------------------------
LEAKS=$(grep -icE '(AccountKey=|SharedAccessKey=|"key":|password)' .env.local 2>/dev/null || echo 0)
# APPLICATIONINSIGHTS_CONNECTION_STRING carries an InstrumentationKey by design; excluded.
if [ "${LEAKS:-0}" -eq 0 ]; then ok "12 no key material in .env.local" "clean"; else bad "12 no key material in .env.local" "${LEAKS} suspicious lines"; fi

# -- render -------------------------------------------------------------------
printf '%-6s %-38s %s\n' "STATUS" "CHECK" "DETAIL"
printf '%-6s %-38s %s\n' "------" "--------------------------------------" "------"
for r in "${rows[@]}"; do
  IFS='|' read -r s c d <<<"$r"
  printf '%-6s %-38s %s\n' "$s" "$c" "$d"
done

echo
echo "pass=$PASS  fail=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo
  echo "Phase 01 verification FAILED."
  echo "A 403 or a missing role immediately after provisioning is usually propagation."
  echo "Wait 5 minutes and re-run before investigating."
  exit 1
fi
echo "Phase 01 verification PASSED."
