#!/usr/bin/env bash
# Create the three Entra application registrations ATLAS needs.
#
# Entra objects are not ARM resources, so Bicep cannot create them. This runs alongside
# `azd provision` rather than inside it.
#
# Moved here from Phase 09 deliberately (CLAUDE.md section 7.13): Phase 08 platform auth and
# Phase 09 validate-jwt both depend on these, and a wrong audience or a missing app role surfaces
# as a 401 that looks exactly like a policy bug. Getting them in place early makes that class of
# failure impossible to misdiagnose later.
#
# Idempotent: re-running updates in place and never creates duplicates.
#
#   bash scripts/create_app_registrations.sh            # create or update
#   bash scripts/create_app_registrations.sh --show     # report only, change nothing
set -uo pipefail
export PYTHONIOENCODING=utf-8

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SHOW_ONLY=0
[ "${1:-}" = "--show" ] && SHOW_ONLY=1

TENANT_ID="$(az account show --query tenantId -o tsv 2>/dev/null)"
if [ -z "$TENANT_ID" ]; then echo "not signed in (az login)" >&2; exit 1; fi

echo "ATLAS app registrations  (tenant ${TENANT_ID})"
echo "================================================"
echo

# Stable role ids. Deterministic so re-running never rotates them and so a token issued before a
# re-run stays valid.
ROLE_ENGINEER="6f1d3a41-0b9c-4d5e-9a2f-11c0a7e4b301"
ROLE_APPROVER="7a2e4b52-1c0d-4e6f-8b3a-22d1b8f5c412"
ROLE_READONLY="8b3f5c63-2d1e-4f70-9c4b-33e2c9a6d523"
ROLE_AGENT="9c4a6d74-3e2f-4081-ad5c-44f3dab7e634"

get_app_id() { az ad app list --filter "displayName eq '$1'" --query "[0].appId" -o tsv 2>/dev/null; }

# ---------------------------------------------------------------------------- atlas-web --
# Container Apps Easy Auth for the UI. App roles drive the approval console: Atlas.Approver is what
# the entitlement check reads, and the readonly demo persona must NOT hold it - Episode 4's
# `unauthorized` eval category depends on that being true.
web_manifest() {
  cat <<JSON
[
  {"allowedMemberTypes":["User"],"description":"Raise and triage incidents","displayName":"Atlas Engineer","id":"${ROLE_ENGINEER}","isEnabled":true,"value":"Atlas.Engineer"},
  {"allowedMemberTypes":["User"],"description":"Approve remediation plans","displayName":"Atlas Approver","id":"${ROLE_APPROVER}","isEnabled":true,"value":"Atlas.Approver"},
  {"allowedMemberTypes":["User"],"description":"Read-only access. Deliberately cannot approve.","displayName":"Atlas ReadOnly","id":"${ROLE_READONLY}","isEnabled":true,"value":"Atlas.ReadOnly"}
]
JSON
}

# ---------------------------------------------------------------------------- atlas-gateway --
# The audience APIM validates on inbound calls. Atlas.Agent is assigned to the orchestrator identity
# and to the hosted agent's Entra agent identity.
gateway_manifest() {
  cat <<JSON
[
  {"allowedMemberTypes":["Application"],"description":"Call ATLAS tools through the gateway","displayName":"Atlas Agent","id":"${ROLE_AGENT}","isEnabled":true,"value":"Atlas.Agent"}
]
JSON
}

# All diagnostics go to STDERR. Only the appId goes to stdout, because the caller captures it
# with $(...) - mixing the two silently swallowed a real failure the first time this ran.
say() { printf '%s\n' "$*" >&2; }

ensure_app() { # displayName | identifierUri (or "-") | roles-json-file (or "-")
  local name="$1" uri="$2" roles="$3"
  local appId objId err

  appId="$(get_app_id "$name")"

  if [ "$SHOW_ONLY" = "1" ]; then
    say "  ${name}: ${appId:-NOT CREATED}"
    printf '%s' "$appId"
    return 0
  fi

  if [ -z "$appId" ]; then
    say "creating ${name}"
    appId="$(az ad app create --display-name "$name" --sign-in-audience AzureADMyOrg --query appId -o tsv 2>/dev/null)"
    if [ -z "$appId" ]; then say "  ERROR: could not create ${name}"; return 1; fi
    sleep 5   # Entra is eventually consistent; a create is not immediately visible to a patch.
  else
    say "updating ${name} (${appId})"
  fi

  objId="$(az ad app show --id "$appId" --query id -o tsv 2>/dev/null)"

  if [ "$roles" != "-" ]; then
    if err="$(az ad app update --id "$appId" --app-roles "@${roles}" 2>&1)"; then
      say "  app roles applied"
    else
      say "  ERROR applying app roles: $(printf '%s' "$err" | head -1)"
      return 1
    fi
  fi

  if [ "$uri" != "-" ]; then
    # This tenant's default policy rejects a bare api://<name> identifier URI: "All newly added
    # URIs must contain a tenant verified domain, tenant ID, or app ID." Requesting v2 access
    # tokens lifts that restriction, which keeps the readable audience the design calls for
    # instead of falling back to api://<appId>. v2 tokens also match the v2.0 openid-config URL
    # the Phase 09 validate-jwt policy uses, so the two stay consistent.
    az rest --method PATCH \
      --url "https://graph.microsoft.com/v1.0/applications/${objId}" \
      --headers "Content-Type=application/json" \
      --body '{"api":{"requestedAccessTokenVersion":2}}' >/dev/null 2>&1 \
      && say "  requestedAccessTokenVersion=2" \
      || say "  WARN: could not set requestedAccessTokenVersion"
    sleep 3

    if err="$(az ad app update --id "$appId" --identifier-uris "$uri" 2>&1)"; then
      say "  identifier uri ${uri}"
    else
      say "  ERROR setting identifier uri ${uri}: $(printf '%s' "$err" | head -1)"
      return 1
    fi

    # Verify rather than trust. This is the check that would have caught the silent failure.
    local actual
    actual="$(az ad app show --id "$appId" --query "join(',', identifierUris)" -o tsv 2>/dev/null)"
    if [ "$actual" != "$uri" ]; then
      say "  ERROR: identifier uri did not stick (got '${actual:-none}', wanted '${uri}')"
      return 1
    fi
  fi

  if ! az ad sp show --id "$appId" >/dev/null 2>&1; then
    az ad sp create --id "$appId" >/dev/null 2>&1 && say "  service principal created"
  fi

  printf '%s' "$appId"
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
web_manifest > "$TMP/web-roles.json"
gateway_manifest > "$TMP/gateway-roles.json"

WEB_APP_ID="$(ensure_app 'atlas-web' '-' "$TMP/web-roles.json" | tail -1)"
GATEWAY_APP_ID="$(ensure_app 'atlas-gateway' 'api://atlas-gateway' "$TMP/gateway-roles.json" | tail -1)"
OPSAPI_APP_ID="$(ensure_app 'atlas-ops-api' 'api://atlas-ops-api' '-' | tail -1)"

echo
echo "RESULT"
echo "  atlas-web      ${WEB_APP_ID:-?}"
echo "  atlas-gateway  ${GATEWAY_APP_ID:-?}   audience api://atlas-gateway"
echo "  atlas-ops-api  ${OPSAPI_APP_ID:-?}   audience api://atlas-ops-api"

if [ "$SHOW_ONLY" = "1" ]; then exit 0; fi

# Persist into the azd environment so Phase 08 and Phase 09 read them rather than re-discovering.
if [ -n "${WEB_APP_ID:-}" ];     then azd env set ATLAS_WEB_APP_CLIENT_ID "$WEB_APP_ID" >/dev/null 2>&1; fi
if [ -n "${GATEWAY_APP_ID:-}" ]; then azd env set ATLAS_GATEWAY_APP_CLIENT_ID "$GATEWAY_APP_ID" >/dev/null 2>&1; fi
if [ -n "${OPSAPI_APP_ID:-}" ];  then azd env set ATLAS_OPSAPI_APP_CLIENT_ID "$OPSAPI_APP_ID" >/dev/null 2>&1; fi
echo
echo "Written to the azd environment as ATLAS_*_APP_CLIENT_ID."
echo
echo "Still to do, in Phase 09, once the orchestrator identity exists:"
echo "  - assign Atlas.Agent to the orchestrator UAMI and to the hosted agent identity"
echo "  - assign Atlas.Approver to the demo approver account"
echo "  - do NOT assign Atlas.Approver to the readonly persona; the Episode 4"
echo "    unauthorized eval category depends on that being false"
