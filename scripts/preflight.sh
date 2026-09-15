#!/usr/bin/env bash
# Local toolchain preflight. Prints a pass/fail table and exits non-zero if anything required
# is missing. Safe to re-run; --fix installs the pieces that can be installed unattended.
#
# Usage: bash scripts/preflight.sh [--fix]
set -uo pipefail

FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

PASS=0; FAIL=0; WARN=0
rows=()

row() { # status | item | found | note
  rows+=("$1|$2|$3|$4")
  case "$1" in PASS) PASS=$((PASS+1));; FAIL) FAIL=$((FAIL+1));; WARN) WARN=$((WARN+1));; esac
}

# Compare dotted versions: returns 0 if $1 >= $2
vge() {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

check_ver() { # item | command | min | extract-regex
  local item="$1" cmd="$2" min="$3" rx="$4" out found
  if ! out=$(eval "$cmd" 2>&1); then
    row FAIL "$item" "not found" "install it"
    return
  fi
  found=$(printf '%s' "$out" | grep -oE "$rx" | head -n1)
  if [ -z "$found" ]; then
    row WARN "$item" "unparsed" "present but version not parsed"
  elif vge "$found" "$min"; then
    row PASS "$item" "$found" "min $min"
  else
    row FAIL "$item" "$found" "need >= $min"
  fi
}

echo "ATLAS preflight"
echo "==============="
echo

# ---- core toolchain -------------------------------------------------------
check_ver "azure cli"        "az version --output tsv --query '\"azure-cli\"'" "2.77"    '[0-9]+\.[0-9]+\.[0-9]+'
check_ver "azd"              "azd version"                                     "1.27.1"  '[0-9]+\.[0-9]+\.[0-9]+'
check_ver "func core tools"  "func --version"                                  "4.0.7030" '[0-9]+\.[0-9]+\.[0-9]+'
check_ver "uv"               "uv --version"                                    "0.5"     '[0-9]+\.[0-9]+\.[0-9]+'
check_ver "github cli"       "gh --version"                                    "2.60"    '[0-9]+\.[0-9]+\.[0-9]+'
check_ver "node"             "node --version"                                  "20"      '[0-9]+\.[0-9]+\.[0-9]+'
check_ver "git"              "git --version"                                   "2.40"    '[0-9]+\.[0-9]+\.[0-9]+'

# GNU Make: the episode runbooks call `make demo-reset` and `make verify-all` live on stage.
# Windows needs it installed separately; ezwinports ships 4.4.1 (GnuWin32's 3.81 is too old
# for .SHELLFLAGS / pipefail).
if command -v make >/dev/null 2>&1; then
  check_ver "gnu make" "make --version" "4.0" '[0-9]+\.[0-9]+(\.[0-9]+)?'
elif [ "$FIX" = "1" ] && command -v winget >/dev/null 2>&1 \
     && winget install --id ezwinports.make --exact --silent \
        --accept-package-agreements --accept-source-agreements >/dev/null 2>&1; then
  row PASS "gnu make" "4.4.1" "installed by --fix; restart your shell for PATH"
else
  row FAIL "gnu make" "missing" "winget install --id ezwinports.make (then restart the shell)"
fi

# ---- python 3.13 (see docs/API-NOTES.md 4.2) ------------------------------
PYV=$(uv run python -c 'import sys;print(".".join(map(str,sys.version_info[:3])))' 2>/dev/null)
if [ -z "$PYV" ]; then
  if [ "$FIX" = "1" ]; then uv python install 3.13 >/dev/null 2>&1 && uv sync >/dev/null 2>&1; fi
  PYV=$(uv run python -c 'import sys;print(".".join(map(str,sys.version_info[:3])))' 2>/dev/null)
fi
case "$PYV" in
  3.13.*) row PASS "python (venv)" "$PYV" "3.13 required: hosted agent has no 3.12 runtime" ;;
  "")     row FAIL "python (venv)" "none" "run: uv python install 3.13 && uv sync" ;;
  *)      row FAIL "python (venv)" "$PYV" "must be 3.13.x - see docs/API-NOTES.md 4.2" ;;
esac

# ---- az extensions --------------------------------------------------------
for ext in containerapp application-insights logic; do
  v=$(az extension show -n "$ext" --query version -o tsv 2>/dev/null)
  if [ -n "$v" ]; then
    row PASS "az ext: $ext" "$v" ""
  elif [ "$FIX" = "1" ] && az extension add --name "$ext" --only-show-errors >/dev/null 2>&1; then
    row PASS "az ext: $ext" "installed" "installed by --fix"
  else
    row FAIL "az ext: $ext" "missing" "az extension add --name $ext"
  fi
done
# az apim is built into the CLI core, not an extension.
if az apim --help >/dev/null 2>&1; then
  row PASS "az apim" "core" "built in, no extension needed"
else
  row FAIL "az apim" "missing" "unexpected: az apim should be core"
fi

# ---- azd Foundry extensions ----------------------------------------------
azd_ext_ver() { azd extension list -o json 2>/dev/null | python -c "
import json,sys
want=sys.argv[1]
for e in json.load(sys.stdin):
    if e.get('id')==want:
        print(e.get('installedVersion') or '', e.get('version') or '')
        break
" "$1"; }

for ext in azure.ai.agents azure.ai.projects azure.ai.toolboxes azure.ai.connections; do
  read -r inst latest <<<"$(azd_ext_ver "$ext")"
  if [ -z "${inst:-}" ]; then
    if [ "$FIX" = "1" ] && azd ext install "$ext" >/dev/null 2>&1; then
      row PASS "azd ext: $ext" "installed" "installed by --fix"
    else
      row FAIL "azd ext: $ext" "missing" "azd ext install $ext"
    fi
  elif [ "$inst" != "$latest" ]; then
    if [ "$FIX" = "1" ] && azd ext upgrade "$ext" >/dev/null 2>&1; then
      row PASS "azd ext: $ext" "$latest" "upgraded by --fix"
    else
      row WARN "azd ext: $ext" "$inst" "newer available: $latest (azd ext upgrade $ext)"
    fi
  else
    row PASS "azd ext: $ext" "$inst" ""
  fi
done

# ---- docker (optional) ----------------------------------------------------
if docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
  row PASS "docker daemon" "$(docker info --format '{{.ServerVersion}}' 2>/dev/null)" "local agent loop available"
else
  row WARN "docker daemon" "not running" "only needed for 'azd ai agent run'; azd deploy builds in ACR"
fi

# ---- azure session --------------------------------------------------------
if acct=$(az account show -o json 2>/dev/null); then
  sub=$(printf '%s' "$acct" | python -c 'import json,sys;print(json.load(sys.stdin)["id"])')
  ten=$(printf '%s' "$acct" | python -c 'import json,sys;print(json.load(sys.stdin)["tenantId"])')
  row PASS "az login" "${sub:0:8}..." "tenant ${ten:0:8}..."
  echo "subscription : $sub"
  echo "tenant       : $ten"
  echo
else
  row FAIL "az login" "not signed in" "run: az login"
fi

# ---- resource providers ---------------------------------------------------
PROVIDERS="Microsoft.CognitiveServices Microsoft.App Microsoft.ApiManagement Microsoft.ServiceBus
Microsoft.DocumentDB Microsoft.Search Microsoft.Web Microsoft.Logic Microsoft.OperationalInsights
Microsoft.Insights Microsoft.KeyVault Microsoft.ContainerRegistry Microsoft.ManagedIdentity
Microsoft.Storage"
for p in $PROVIDERS; do
  state=$(az provider show -n "$p" --query registrationState -o tsv 2>/dev/null)
  if [ "$state" = "Registered" ]; then
    row PASS "provider: $p" "Registered" ""
  elif [ "$FIX" = "1" ]; then
    az provider register -n "$p" --only-show-errors >/dev/null 2>&1
    row WARN "provider: $p" "${state:-unknown}" "registration started; re-run to confirm"
  else
    row FAIL "provider: $p" "${state:-unknown}" "az provider register -n $p"
  fi
done

# ---- render ---------------------------------------------------------------
printf '%-6s %-34s %-18s %s\n' "STATUS" "ITEM" "FOUND" "NOTE"
printf '%-6s %-34s %-18s %s\n' "------" "----------------------------------" "------------------" "----"
for r in "${rows[@]}"; do
  IFS='|' read -r s i f n <<<"$r"
  printf '%-6s %-34s %-18s %s\n' "$s" "$i" "$f" "$n"
done

echo
echo "pass=$PASS  warn=$WARN  fail=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo
  echo "Preflight FAILED. Re-run with --fix to install what can be installed unattended."
  exit 1
fi
echo "Preflight OK."
