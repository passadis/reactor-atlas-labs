#!/usr/bin/env bash
# Episode 2 set piece: kill the orchestrator mid-approval and bring it back.
#
# Restarts the active revision, which terminates every replica and starts fresh ones. Every case
# held in memory is gone - which is the whole point. The parked case lives in its Cosmos
# checkpoint, so approving after the restart still completes it.
#
# `--scale-zero` instead drops min-replicas to 0 and waits for ACA to retire the replicas. It is a
# more literal "scaled to nothing", but it waits on the scale-down cooldown (minutes, not
# seconds) and `--max-replicas 0` is rejected outright, so restart is the one the demo uses.
#
# This is a CONTROL-PLANE write and is NOT permitted mid-broadcast (CLAUDE.md section 9) except as
# this rehearsed beat, whose measured wall clock belongs in docs/DEMO-NOTES.md (section 9.6).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

MODE="${1:-restart}"
RG="${AZURE_RESOURCE_GROUP:-$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null | tr -d '\r')}"
APP="${ATLAS_ORCHESTRATOR_APP:-$(az containerapp list -g "$RG" \
  --query "[?tags.\"azd-service-name\"=='orchestrator'].name | [0]" -o tsv 2>/dev/null)}"

if [[ -z "$RG" || -z "$APP" ]]; then
  echo "could not resolve the resource group or the orchestrator app" >&2
  exit 1
fi

REV=$(az containerapp show -g "$RG" -n "$APP" \
  --query "properties.latestReadyRevisionName" -o tsv 2>/dev/null)
BEFORE=$(az containerapp replica list -g "$RG" -n "$APP" --revision "$REV" \
  --query "length(@)" -o tsv 2>/dev/null || echo "?")

# The replicas alive now. A restart retires them one by one, not at once: a case started while an
# old one is still serving can die with it (2026-10-07). "Back" is when the first new replica
# answers; "replaced" is when no old one is left and the minimum is running again.
OLD=$(az containerapp replica list -g "$RG" -n "$APP" --revision "$REV" --query "[].name" -o tsv   2>/dev/null | tr -d '' | tr '
' ' ')
MIN=$(az containerapp show -g "$RG" -n "$APP" --query "properties.template.scale.minReplicas"   -o tsv 2>/dev/null | tr -d '')
echo "== killing $APP (revision $REV, $BEFORE replica(s)) =="
START=$(date +%s)

if [[ "$MODE" == "--scale-zero" ]]; then
  # Restore whatever range was set before (the scale profile, API-NOTES 19.5), not a hard-coded one.
  read -r MIN0 MAX0 < <(az containerapp show -g "$RG" -n "$APP" \
    --query "[properties.template.scale.minReplicas, properties.template.scale.maxReplicas]" \
    -o tsv 2>/dev/null | tr '\r\n' '  ')
  az containerapp update -g "$RG" -n "$APP" --min-replicas 0 --max-replicas 1 -o none
  echo "min-replicas set to 0 at $(date -u +%H:%M:%S); waiting for the scale-down cooldown"
  for _ in $(seq 1 90); do
    LEFT=$(az containerapp replica list -g "$RG" -n "$APP" --revision "$REV" \
      --query "length(@)" -o tsv 2>/dev/null || echo 0)
    [[ "${LEFT:-0}" == "0" ]] && break
    sleep 5
  done
  az containerapp update -g "$RG" -n "$APP" --min-replicas "${MIN0:-1}" --max-replicas "${MAX0:-3}" -o none
else
  az containerapp revision restart -g "$RG" -n "$APP" --revision "$REV" -o none
  echo "revision restart requested at $(date -u +%H:%M:%S); every replica is being replaced (in-memory state is lost)"
fi

for _ in $(seq 1 90); do
  READY=$(az containerapp replica list -g "$RG" -n "$APP" --revision "$REV" \
    --query "length([?properties.runningState=='Running'])" -o tsv 2>/dev/null || echo 0)
  [[ "${READY:-0}" -ge 1 ]] && break
  sleep 3
done

# Ready replicas are not the same as a ready app: wait until it actually answers.
HEALTHY="no"
for _ in $(seq 1 60); do
  STATE=$(az containerapp show -g "$RG" -n "$APP" --query "properties.runningStatus" -o tsv 2>/dev/null || echo "")
  if [[ "$STATE" == "Running" ]]; then HEALTHY="yes"; break; fi
  sleep 3
done

TOTAL=$(( $(date +%s) - START ))
echo "orchestrator back with ${READY:-0} running replica(s) after ${TOTAL}s (healthy: $HEALTHY)"

# Wait for the old replicas to be gone. An approval or a new case that lands on an old replica
# while it is being retired can die with it (2026-10-07). On stage: narrate `make checkpoints`
# (Cosmos, works while it is down) until the "replaced" line, then approve.
for _ in $(seq 1 100); do
  NOW=$(az containerapp replica list -g "$RG" -n "$APP" --revision "$REV"     --query "[?properties.runningState=='Running'].name" -o tsv 2>/dev/null | tr -d '')
  LEFT=0; COUNT=0
  for r in $NOW; do
    COUNT=$((COUNT + 1))
    for o in $OLD; do [[ "$r" == "$o" ]] && LEFT=$((LEFT + 1)); done
  done
  [[ "$LEFT" == "0" && "$COUNT" -ge "${MIN:-1}" ]] && break
  sleep 3
done
REPLACED=$(( $(date +%s) - START ))
echo "every old replica replaced after ${REPLACED}s ($COUNT running, min ${MIN:-1}) - approve and start cases from here"
echo
echo "The startup sweep finishes any case whose approval arrived while it was down."
echo "Record this wall clock in docs/DEMO-NOTES.md (CLAUDE.md section 9.6)."
