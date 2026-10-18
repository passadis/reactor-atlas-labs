#!/usr/bin/env bash
# Episode 3, Beat 4: take away the ops API's right to send to the remediation queue - and show that
# dispatch fails CLOSED: the plan is not marked executed, nothing reaches the worker.
#
#   bash scripts/chaos/revoke_servicebus_role.sh --apply     # remove the role assignment
#   bash scripts/chaos/revoke_servicebus_role.sh --revert    # put it back (same deterministic name)
#   bash scripts/chaos/revoke_servicebus_role.sh --status
#
# A CONTROL-PLANE write. CLAUDE.md 9.4: never live in a broadcast - a revoked role reaches a
# running workload only when its cached token is renewed, and propagation takes minutes. It is
# recorded on a free day and SHOWN from docs/assets/fallbacks/ep3-4-revoke-servicebus-role.txt.
# The identity is the ops API Function's (it is the one that enqueues); the scope is the one queue.
set -euo pipefail
# Git Bash rewrites "/subscriptions/..." arguments into Windows paths before az sees them; the
# delete then fails with MissingSubscription (2026-10-11). Arguments here are Azure ids, never paths.
export MSYS_NO_PATHCONV=1
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

ACTION="${1:---status}"
RG="${AZURE_RESOURCE_GROUP:-$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null | tr -d '\r')}"
NS=$(azd env get-value ATLAS_SERVICEBUS_NAMESPACE 2>/dev/null | tr -d '\r')
QUEUE=$(azd env get-value ATLAS_COMMANDS_QUEUE 2>/dev/null | tr -d '\r')
FUNC_MI=$(az identity list -g "$RG" --query "[?contains(name, 'func')].principalId | [0]" -o tsv | tr -d '\r')
SCOPE=$(az servicebus queue show -g "$RG" --namespace-name "$NS" -n "$QUEUE" --query id -o tsv | tr -d '\r')
ROLE="Azure Service Bus Data Sender"

current() {
  az role assignment list --assignee "$FUNC_MI" --scope "$SCOPE" --role "$ROLE" \
    --query "length(@)" -o tsv 2>/dev/null | tr -d '\r'
}

case "$ACTION" in
  --apply)
    az role assignment delete --assignee "$FUNC_MI" --scope "$SCOPE" --role "$ROLE" -o none
    echo "removed '$ROLE' from the ops API identity on $QUEUE at $(date -u +%H:%M:%S)Z"
    ;;
  --revert)
    if [[ "$(current)" == "0" ]]; then
      # The same assignment Bicep makes, so the next `azd provision` sees no drift.
      az role assignment create --assignee-object-id "$FUNC_MI" --assignee-principal-type \
        ServicePrincipal --scope "$SCOPE" --role "$ROLE" -o none
    fi
    echo "'$ROLE' present on $QUEUE for the ops API identity ($(date -u +%H:%M:%S)Z)"
    ;;
  --status)
    [[ "$(current)" == "0" ]] && echo "REVOKED: the ops API may not send to $QUEUE" \
      || echo "baseline: the ops API may send to $QUEUE"
    ;;
  *) echo "usage: $0 --apply | --revert | --status" >&2; exit 2 ;;
esac
