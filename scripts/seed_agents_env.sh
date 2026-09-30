#!/usr/bin/env bash
# Seed agents/.azure/<env>/.env from the platform's .env.local, so the second azd project
# (agents/azure.yaml, microsoft.foundry) resolves ${...} against the project Bicep created.
#
# Called by scripts/postprovision.sh; safe to run on its own after any provision. Idempotent.
#
# The hosted agent runs the same atlas_agent tools as the web app, so its environment carries the
# same data endpoints. FOUNDRY_* names are written here only for ${...} substitution in
# agents/azure.yaml; the platform injects the real FOUNDRY_* values into the running agent and they
# must never be declared in the service `env:` map (CLAUDE.md 7.4).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ENV_NAME="${AZURE_ENV_NAME:-$(grep -E '^AZURE_ENV_NAME=' .env.local | cut -d= -f2-)}"
ENV_NAME="${ENV_NAME:-atlas}"
get() { grep -E "^$1=" .env.local | head -1 | cut -d= -f2-; }

SUB="$(get AZURE_SUBSCRIPTION_ID)"
RG="$(get AZURE_RESOURCE_GROUP)"
ACCOUNT="$(get ATLAS_FOUNDRY_ACCOUNT_NAME)"
PROJECT="$(get ATLAS_FOUNDRY_PROJECT_NAME)"

AGENTS_ENV_DIR="agents/.azure/${ENV_NAME}"
mkdir -p "$AGENTS_ENV_DIR"
SEEDED="$(mktemp)"
{
  echo "AZURE_ENV_NAME=${ENV_NAME}"
  echo "AZURE_LOCATION=$(get AZURE_LOCATION)"
  echo "AZURE_SUBSCRIPTION_ID=${SUB}"
  echo "AZURE_RESOURCE_GROUP=${RG}"
  echo "AZURE_AI_PROJECT_ID=/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.CognitiveServices/accounts/${ACCOUNT}/projects/${PROJECT}"
  echo "FOUNDRY_PROJECT_ENDPOINT=$(get FOUNDRY_PROJECT_ENDPOINT)"
  echo "AZURE_AI_MODEL_DEPLOYMENT_NAME=$(get FOUNDRY_MODEL_DEPLOYMENT)"
  echo "APPLICATIONINSIGHTS_CONNECTION_STRING=$(get APPLICATIONINSIGHTS_CONNECTION_STRING)"
  echo "ATLAS_COSMOS_ENDPOINT=$(get ATLAS_COSMOS_ENDPOINT)"
  echo "ATLAS_COSMOS_DATABASE=$(get ATLAS_COSMOS_DATABASE)"
  echo "ATLAS_SEARCH_ENDPOINT=$(get ATLAS_SEARCH_ENDPOINT)"
  echo "ATLAS_FOUNDRY_ACCOUNT_NAME=${ACCOUNT}"
  # Phase 09: the hosted agent's tools go through the gateway like every other surface. The key
  # is a Key Vault URI, never a value - the agent reads it with its own Entra agent identity.
  echo "ATLAS_OPS_API_URL=$(get ATLAS_APIM_OPS_URL)"
  echo "ATLAS_OPS_API_SCOPE=api://atlas-gateway/.default"
  echo "ATLAS_APIM_KEY_SECRET_URI=$(get ATLAS_APIM_KEY_SECRET_URI)"
  echo "ATLAS_TOOL_CATALOG_URL=$(get ATLAS_APIM_OPS_MCP_URL)"
  echo "ATLAS_RAI_POLICY_ID=/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.CognitiveServices/accounts/${ACCOUNT}/raiPolicies/$(get ATLAS_RAI_POLICY_NAME)"
  # What `azd ai agent init --project-id` writes for an existing project (captured 2026-09-19,
  # azure.ai.agents 1.0.0-beta.15): use the project Bicep made, code deploy needs no ACR.
  echo "USE_EXISTING_AI_PROJECT=true"
  echo "AZD_AGENT_SKIP_ACR=true"
  echo "AZD_FOUNDRY_ACR_MODE=none"
  echo "AZURE_AI_ACCOUNT_NAME=${ACCOUNT}"
  echo "AZURE_AI_PROJECT_NAME=${PROJECT}"
  echo "AZURE_AI_DEPLOYMENTS_LOCATION=$(get AZURE_LOCATION)"
  echo "AZURE_OPENAI_ENDPOINT=https://${ACCOUNT}.openai.azure.com/"
  echo "AZURE_TENANT_ID=$(get AZURE_TENANT_ID)"
} > "$SEEDED"

# MERGE, never overwrite: after each deploy azd records AGENT_<SERVICE>_* values in this file, and
# `azd ai agent monitor/show/invoke` resolve the agent through them. Seeded keys win; every other
# key is kept.
ENV_FILE="${AGENTS_ENV_DIR}/.env"
touch "$ENV_FILE"
python - "$ENV_FILE" "$SEEDED" <<'PY'
import sys
from pathlib import Path
env_path, seeded_path = Path(sys.argv[1]), Path(sys.argv[2])
def parse(text):
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v
    return out
merged = parse(env_path.read_text(encoding="utf-8"))
merged.update(parse(seeded_path.read_text(encoding="utf-8")))
env_path.write_text("".join(f"{k}={v}\n" for k, v in merged.items()), encoding="utf-8")
PY
rm -f "$SEEDED"
printf '{"version":1,"defaultEnvironment":"%s"}\n' "$ENV_NAME" > agents/.azure/config.json
echo "seed_agents_env: wrote ${AGENTS_ENV_DIR}/.env ($(wc -l < "${AGENTS_ENV_DIR}/.env") values)"
