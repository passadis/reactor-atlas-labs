#!/usr/bin/env bash
# GitHub Actions -> Azure with OIDC: an Entra app, a federated credential, no secret (Phase 13).
#
#   bash scripts/github_oidc.sh                    # idempotent; re-run any time
#
# 1. Entra app `atlas-github-ci` and its service principal (created once, reused after).
# 2. ONE federated credential: tokens from jobs in this repo's `atlas` GitHub environment only.
#    A workflow that does not declare `environment: atlas` cannot authenticate at all.
# 3. The GitHub environment `atlas`.
# 4. GitHub variables (not secrets - nothing here is one): the ids azure/login needs, and an
#    allow-listed copy of the azd environment the scripts read (ATLAS_ENV_FILE).
# 5. `azd env set ATLAS_CI_PRINCIPAL_ID` - the next `azd provision` grants the narrow roles in
#    infra/modules/rbac.bicep ("CI identity"). Nothing is granted by this script itself.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="${GITHUB_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
APP_NAME="atlas-github-ci"
SUBJECT="repo:${REPO}:environment:atlas"
TENANT=$(az account show --query tenantId -o tsv)
SUB=$(az account show --query id -o tsv)

APP_ID=$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv)
if [[ -z "$APP_ID" ]]; then
  APP_ID=$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)
  echo "created app $APP_NAME ($APP_ID)"
fi
SP_ID=$(az ad sp list --filter "appId eq '$APP_ID'" --query "[0].id" -o tsv)
if [[ -z "$SP_ID" ]]; then
  SP_ID=$(az ad sp create --id "$APP_ID" --query id -o tsv)
  echo "created service principal $SP_ID"
fi
HAS=$(az ad app federated-credential list --id "$APP_ID" --query "[?subject=='$SUBJECT'] | length(@)" -o tsv)
if [[ "$HAS" == "0" ]]; then
  BODY=$(mktemp)
  printf '{"name":"github-atlas-environment","issuer":"https://token.actions.githubusercontent.com","subject":"%s","audiences":["api://AzureADTokenExchange"],"description":"GitHub Actions, environment atlas only"}' "$SUBJECT" > "$BODY"
  az ad app federated-credential create --id "$APP_ID" --parameters "@$BODY" -o none
  rm -f "$BODY"
  echo "federated credential added for $SUBJECT"
fi

gh api --method PUT "repos/${REPO}/environments/atlas" >/dev/null
echo "GitHub environment 'atlas' ready"

gh variable set AZURE_CLIENT_ID --body "$APP_ID" --repo "$REPO"
gh variable set AZURE_TENANT_ID --body "$TENANT" --repo "$REPO"
gh variable set AZURE_SUBSCRIPTION_ID --body "$SUB" --repo "$REPO"
# Allow-list: identifiers and endpoints the scripts read. Nothing secret lives in the azd env
# (keys stay in Key Vault, referenced by URI), and this list keeps it that way.
KEEP='^(AZURE_ENV_NAME|AZURE_LOCATION|AZURE_RESOURCE_GROUP|AZURE_SUBSCRIPTION_ID|AZURE_TENANT_ID|FOUNDRY_PROJECT_ENDPOINT|FOUNDRY_MODEL_DEPLOYMENT|ATLAS_MODEL_JUDGE|ATLAS_MODEL_EMBED|ATLAS_FOUNDRY_ACCOUNT_NAME|ATLAS_FOUNDRY_PROJECT_NAME|ATLAS_COSMOS_ENDPOINT|ATLAS_COSMOS_DATABASE|ATLAS_SEARCH_ENDPOINT|ATLAS_SEARCH_SERVICE_NAME|ATLAS_APIM_NAME|ATLAS_WEB_URL|ATLAS_SERVICEBUS_NAMESPACE|ATLAS_SERVICEBUS_FQDN|ATLAS_APPINSIGHTS_NAME|APPLICATIONINSIGHTS_CONNECTION_STRING)='
ENV_FILE=$(azd env get-values | tr -d '\r' | grep -E "$KEEP" | sed 's/"//g')
gh variable set ATLAS_ENV_FILE --body "$ENV_FILE" --repo "$REPO"
echo "GitHub variables set: AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID, ATLAS_ENV_FILE ($(echo "$ENV_FILE" | wc -l | tr -d ' ') keys)"

azd env set ATLAS_CI_PRINCIPAL_ID "$SP_ID" >/dev/null
echo "azd env ATLAS_CI_PRINCIPAL_ID=$SP_ID - run 'azd provision' to grant the CI roles"
