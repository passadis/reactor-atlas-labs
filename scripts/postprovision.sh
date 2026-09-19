#!/usr/bin/env bash
# azd postprovision hook.
#
#   1. Writes .env.local with every endpoint the application code needs (gitignored).
#   2. Seeds agents/.azure/<env>/.env so the second azd project resolves ${...} substitutions
#      against the project this deployment just created (CLAUDE.md section 7.3).
#   3. Waits for role-assignment propagation, which is up to 5 minutes and is the usual cause of a
#      403 straight after provisioning.
#   4. Runs scripts/verify_phase01.sh.
#
# Idempotent. Safe to re-run against an existing environment.
set -euo pipefail

ENV_NAME="${AZURE_ENV_NAME:-atlas}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "postprovision: environment ${ENV_NAME}"

# --- 1. capture azd outputs -------------------------------------------------
VALUES="$(azd env get-values --environment "$ENV_NAME" 2>/dev/null)"
if [ -z "$VALUES" ]; then
  echo "postprovision: azd env get-values returned nothing" >&2
  exit 1
fi

# Strip surrounding quotes azd adds, and keep only ATLAS_/AZURE_/FOUNDRY_/APPLICATIONINSIGHTS_ keys.
printf '%s\n' "$VALUES" \
  | grep -E '^(ATLAS_|AZURE_|FOUNDRY_|APPLICATIONINSIGHTS_)' \
  | sed 's/="/=/; s/"$//' \
  > .env.local

echo "postprovision: wrote .env.local ($(wc -l < .env.local) values)"

get() { grep -E "^$1=" .env.local | head -1 | cut -d= -f2-; }

PROJECT_ENDPOINT="$(get FOUNDRY_PROJECT_ENDPOINT)"
RG="$(get AZURE_RESOURCE_GROUP)"

# --- 2. seed the agents project --------------------------------------------
# The agents azd project references the ALREADY PROVISIONED Foundry project by endpoint rather than
# creating its own. This is the seam that lets Bicep own the project while microsoft.foundry owns
# the agent. See agents/README.md.
AZURE_ENV_NAME="$ENV_NAME" bash scripts/seed_agents_env.sh

# --- 3. wait for role-assignment propagation --------------------------------
# Poll something that actually needs a data-plane role rather than sleeping blindly. Cosmos is the
# strictest: local auth is disabled, so a token without the SQL role assignment fails outright.
COSMOS_EP="$(get ATLAS_COSMOS_ENDPOINT)"
if [ -n "$COSMOS_EP" ]; then
  echo -n "postprovision: waiting for Cosmos data-plane role propagation "
  for i in $(seq 1 30); do
    if az cosmosdb sql role assignment list \
         --account-name "$(get ATLAS_COSMOS_ACCOUNT_NAME)" \
         --resource-group "$RG" -o tsv --query "length(@)" 2>/dev/null | grep -qE '^[1-9]'; then
      echo " ok (${i}0s)"
      break
    fi
    echo -n "."
    sleep 10
  done
  echo
fi

# --- 4. verify ---------------------------------------------------------------
if [ -x scripts/verify_phase01.sh ] || [ -f scripts/verify_phase01.sh ]; then
  bash scripts/verify_phase01.sh
else
  echo "postprovision: scripts/verify_phase01.sh not present yet, skipping"
fi

echo "postprovision: done"
echo
echo "  Foundry project : ${PROJECT_ENDPOINT}"
echo "  Web app         : $(get ATLAS_WEB_URL)"
echo "  APIM gateway    : $(get ATLAS_APIM_GATEWAY_URL)"
