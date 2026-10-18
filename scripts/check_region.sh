#!/usr/bin/env bash
# Verify a region can host the whole ATLAS footprint before Phase 01 commits to it.
#
# Usage: bash scripts/check_region.sh [location]      # default: eastus2
#
# Exits non-zero if any REQUIRED capability is missing. Capabilities that only affect a single
# demo beat are reported as WARN so you can make the trade-off knowingly.
#
# Region choice is recorded in docs/API-NOTES.md section 5. Short version: AI red teaming is
# available only in East US 2 and North Central US, which is why eastus2 is primary.
set -uo pipefail
export PYTHONIOENCODING=utf-8

LOC="${1:-eastus2}"
PASS=0; FAIL=0; WARN=0
rows=()
row() { rows+=("$1|$2|$3"); case "$1" in PASS) PASS=$((PASS+1));; FAIL) FAIL=$((FAIL+1));; WARN) WARN=$((WARN+1));; esac; }

SUB=$(az account show --query id -o tsv 2>/dev/null)
if [ -z "$SUB" ]; then echo "error: not signed in (az login)" >&2; exit 1; fi

echo "ATLAS region check: $LOC"
echo "=========================="
echo

# --- model catalogue -------------------------------------------------------
MODELS_JSON=$(az cognitiveservices model list -l "$LOC" -o json 2>/dev/null)
have_model() { # name | sku
  printf '%s' "$MODELS_JSON" | python -c '
import json,sys
name,sku=sys.argv[1],sys.argv[2]
for m in json.load(sys.stdin):
    mm=m.get("model") or {}
    if mm.get("name")==name and any(s.get("name")==sku for s in (mm.get("skus") or [])):
        print(mm.get("version","")); break
' "$1" "$2"
}

for spec in "gpt-5.4:GlobalStandard:gpt-main" "gpt-5.4-mini:GlobalStandard:gpt-judge" \
            "text-embedding-3-large:Standard:embed-main"; do
  IFS=: read -r model sku alias <<<"$spec"
  ver=$(have_model "$model" "$sku")
  if [ -n "$ver" ]; then
    row PASS "model $alias ($model $sku)" "version $ver"
  else
    row FAIL "model $alias ($model $sku)" "not offered in $LOC"
  fi
done

# --- quota headroom --------------------------------------------------------
# Units are sku.capacity units == thousands of TPM. See docs/API-NOTES.md section 5.
USAGE_JSON=$(az cognitiveservices usage list -l "$LOC" -o json 2>/dev/null)
quota_check() { # family | needed | alias
  printf '%s' "$USAGE_JSON" | python -c '
import json,sys
fam,need,alias=sys.argv[1],int(sys.argv[2]),sys.argv[3]
for u in json.load(sys.stdin):
    if (u.get("name") or {}).get("value")==fam:
        lim=int(u.get("limit") or 0); cur=int(u.get("currentValue") or 0); free=lim-cur
        status="PASS" if free>=need else "FAIL"
        print(f"{status}|{alias} quota ({fam})|free {free} / need {need} units")
        break
else:
    print(f"WARN|{alias} quota ({fam})|family not reported in {sys.argv[4]}")
' "$1" "$2" "$3" "$LOC"
}
while IFS='|' read -r s i d; do [ -n "$s" ] && row "$s" "$i" "$d"; done <<EOF
$(quota_check "OpenAI.GlobalStandard.gpt-5.4" 200 "gpt-main")
$(quota_check "OpenAI.GlobalStandard.gpt-5.4-mini" 100 "gpt-judge")
$(quota_check "OpenAI.Standard.text-embedding-3-large" 120 "embed-main")
EOF

# --- APIM StandardV2 -------------------------------------------------------
APIM_SKUS=$(az rest --method GET --url \
  "https://management.azure.com/subscriptions/$SUB/providers/Microsoft.ApiManagement/skus?api-version=2023-05-01-preview" \
  2>/dev/null | python -c '
import json,sys
loc=sys.argv[1].lower()
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
out=set()
for v in d.get("value",[]):
    if loc in {x.lower().replace(" ","") for x in v.get("locations",[])}:
        out.add(v.get("name"))
print(",".join(sorted(out)))
' "$LOC")
if printf '%s' "$APIM_SKUS" | grep -q "StandardV2"; then
  row PASS "APIM StandardV2" "available"
else
  row FAIL "APIM StandardV2" "not available (${APIM_SKUS:-none reported})"
fi

# --- Functions Flex Consumption --------------------------------------------
if az functionapp list-flexconsumption-locations -o tsv 2>/dev/null | tr -d '\r' | grep -qix "$LOC"; then
  PYVERS=$(az functionapp list-flexconsumption-runtimes --location "$LOC" --runtime python -o json 2>/dev/null \
           | python -c 'import json,sys;print(",".join(str(r.get("version")) for r in json.load(sys.stdin)))' 2>/dev/null)
  row PASS "Functions Flex Consumption" "python: ${PYVERS:-unknown}"
  if printf '%s' "$PYVERS" | grep -q "3.13"; then
    row PASS "Functions python 3.13" "supported"
  else
    row FAIL "Functions python 3.13" "not offered (have: ${PYVERS:-none})"
  fi
else
  row FAIL "Functions Flex Consumption" "not available in $LOC"
fi

# --- provider-level regional availability ----------------------------------
prov_has() { # provider | resourceType
  az provider show -n "$1" --query "resourceTypes[?resourceType=='$2'].locations[]" -o tsv 2>/dev/null \
    | tr -d '\r' | tr 'A-Z' 'a-z' | tr -d ' ' | grep -qx "$LOC"
}
prov_has Microsoft.App managedEnvironments      && row PASS "Container Apps environments" "available" || row FAIL "Container Apps environments" "not available"
prov_has Microsoft.ServiceBus namespaces        && row PASS "Service Bus (Premium at provision)" "available" || row FAIL "Service Bus" "not available"
# NOTE: this proves the service is OFFERED here, NOT that the region has capacity right now.
# eastus2 passed this check on 2026-09-15 and then failed provisioning with
# InsufficientResourcesAvailable for every Search SKU. Only a real create attempt proves capacity.
prov_has Microsoft.Search searchServices        && row WARN "AI Search (offered, capacity unproven)" "see CLAUDE.md 7.14" || row FAIL "AI Search" "not available"
prov_has Microsoft.DocumentDB databaseAccounts  && row PASS "Cosmos DB" "available" || row FAIL "Cosmos DB" "not available"
prov_has Microsoft.ContainerRegistry registries && row PASS "Container Registry" "available" || row FAIL "Container Registry" "not available"

# --- ACA dedicated workload profile needs regional vCPU --------------------
VCPU=$(az vm list-usage -l "$LOC" -o json 2>/dev/null | python -c '
import json,sys
for r in json.load(sys.stdin):
    if r["name"]["localizedValue"]=="Total Regional vCPUs":
        print(int(r["limit"])-int(r["currentValue"])); break
' 2>/dev/null)
if [ -n "${VCPU:-}" ] && [ "$VCPU" -ge 20 ]; then
  row PASS "regional vCPU (ACA D4 profile)" "$VCPU free / need 20"
else
  row FAIL "regional vCPU (ACA D4 profile)" "${VCPU:-unknown} free / need 20"
fi

# --- Foundry agent + evaluation region support -----------------------------
# Static lists from Microsoft Learn, captured 2026-09-15. Re-verify before Episode 1:
#   scripts/mslearn.sh fetch https://learn.microsoft.com/azure/foundry/agents/concepts/limits-quotas-regions
#   scripts/mslearn.sh fetch https://learn.microsoft.com/azure/foundry/concepts/evaluation-regions-limits-virtual-network
AGENTS_REGIONS="australiaeast brazilsouth canadacentral canadaeast centralus eastus eastus2 francecentral germanywestcentral italynorth japaneast japanwest koreacentral northcentralus norwayeast polandcentral southafricanorth southcentralus southeastasia southindia swedencentral"
REDTEAM_REGIONS="eastus2 northcentralus"
BATCHEVAL_REGIONS="brazilsouth canadacentral canadaeast centralus eastus eastus2 northcentralus southcentralus westcentralus westus westus2 westus3 francecentral germanywestcentral italynorth northeurope norwayeast polandcentral spaincentral swedencentral switzerlandnorth uksouth westeurope australiaeast centralindia eastasia japaneast japanwest koreacentral southindia southeastasia southafricanorth uaenorth"

in_list() { printf '%s' "$2" | tr ' ' '\n' | grep -qx "$1"; }

in_list "$LOC" "$AGENTS_REGIONS"    && row PASS "Foundry Agents + Responses API" "supported" || row FAIL "Foundry Agents + Responses API" "NOT supported in $LOC"
in_list "$LOC" "$BATCHEVAL_REGIONS" && row PASS "Batch evaluations (Phase 12)" "supported"   || row FAIL "Batch evaluations (Phase 12)" "NOT supported in $LOC"
if in_list "$LOC" "$REDTEAM_REGIONS"; then
  row PASS "AI Red Teaming Agent (Phase 12.4)" "supported"
else
  row WARN "AI Red Teaming Agent (Phase 12.4)" "NOT supported in $LOC - only: $REDTEAM_REGIONS"
fi

# --- render ----------------------------------------------------------------
printf '%-6s %-42s %s\n' "STATUS" "CAPABILITY" "DETAIL"
printf '%-6s %-42s %s\n' "------" "------------------------------------------" "------"
for r in "${rows[@]}"; do
  IFS='|' read -r s i d <<<"$r"
  printf '%-6s %-42s %s\n' "$s" "$i" "$d"
done

echo
echo "pass=$PASS  warn=$WARN  fail=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo
  echo "$LOC cannot host the full ATLAS footprint. Try: bash scripts/check_region.sh eastus2"
  exit 1
fi
if [ "$WARN" -gt 0 ]; then
  echo "$LOC is usable, but read the WARN rows - each one costs a demo beat."
fi
echo "Region $LOC OK."
