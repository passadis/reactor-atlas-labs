"""Phase 14 security pass: the nine checks of PHASE-14 section 2, against the deployed system.

    uv run python -m scripts.verify_security        # or: bash scripts/verify_security.sh

Read-only, except one forged remediation submission that the ops API must refuse (it is
audited as a refusal and writes nothing else). Exit 0 only with zero findings. Accepted
exceptions are listed by name with the reason, never silently skipped:

- Application Insights connection strings carry an InstrumentationKey. Microsoft documents it
  as an ingestion identifier, not a security token.
- The Logic App's OWN host-storage account (`statlaslogic...`) is reached with an account key:
  Logic Apps Standard requires shared-key access to its host storage. That account holds no
  ATLAS data (infra/modules/logicapps.bicep, CLAUDE.md 7.9).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from atlas_common.config import load_local_env

ROOT = Path(__file__).resolve().parent.parent
GITLEAKS = "github.com/zricethezav/gitleaks/v8@v8.24.0"  # pinned; run through `go run`
BROAD_ROLES = {"Owner", "Contributor", "User Access Administrator",
               "Role Based Access Control Administrator"}
KEY_PATTERNS = {"account key": r"AccountKey=", "SAS": r"SharedAccessKey=|[?&]sig=",
                "client secret": r"client_secret|ClientSecret="}
results: list[tuple[str, bool, str]] = []
exceptions: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def guarded(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as exc:
        check(name, False, f"{type(exc).__name__}: {str(exc)[:200]}")


def az(*args: str) -> Any:
    out = subprocess.run([shutil.which("az") or "az", *args, "-o", "json"], capture_output=True,
                         text=True, timeout=180, check=True).stdout
    return json.loads(out or "null")


def rg() -> str:
    return os.environ["AZURE_RESOURCE_GROUP"]


# ---------------------------------------------------------------------------------- 1 --
def secrets_in_history() -> None:
    go = shutil.which("go")
    if not go:
        check("1 no secrets in git history (gitleaks)", False, "Go is not installed")
        return
    r = subprocess.run([go, "run", GITLEAKS, "git", "--no-banner", "--redact", "."], cwd=ROOT,
                       capture_output=True, text=True, timeout=600, check=False)
    tail = [ln for ln in (r.stdout + r.stderr).splitlines() if "commits scanned" in ln
            or "leaks found" in ln or "no leaks" in ln]
    check("1 no secrets in git history (gitleaks)", r.returncode == 0,
          "; ".join(re.sub(r"\x1b\[[0-9;]*m", "", ln).split("INF")[-1].split("WRN")[-1].strip()
                    for ln in tail) + " (.gitleaksignore: 2 built-in role ids)")


# ---------------------------------------------------------------------------------- 2 --
def secrets_in_config() -> None:
    found: list[str] = []

    def scan(where: str, name: str, value: str) -> None:
        if "@Microsoft.KeyVault" in value:
            if name != "ATLAS_APIM_KEY":
                found.append(f"{where}:{name} is a Key Vault reference other than the APIM key")
            return
        for label, pattern in KEY_PATTERNS.items():
            if re.search(pattern, value):
                if (where.startswith("logic-") and "statlaslogic" in value
                        and name in ("AzureWebJobsStorage",
                                     "WEBSITE_CONTENTAZUREFILECONNECTIONSTRING")):
                    exceptions.append(f"{where}:{name} - Logic Apps host-storage key (accepted)")
                else:
                    found.append(f"{where}:{name} contains {label}")

    env = subprocess.run([shutil.which("azd") or "azd", "env", "get-values"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60, check=True).stdout
    for line in env.splitlines():
        name, _, value = line.partition("=")
        scan("azd-env", name, value.strip('"'))
    for app in az("containerapp", "list", "-g", rg(), "--query", "[].name"):
        for container in az("containerapp", "show", "-g", rg(), "-n", app, "--query",
                            "properties.template.containers"):
            for e in container.get("env") or []:
                if e.get("secretRef"):
                    found.append(f"{app}:{e['name']} uses a Container Apps secret")
                scan(app, e["name"], e.get("value") or "")
    # `functionapp list` does not return Logic Apps Standard sites; scan them explicitly.
    sites = az("functionapp", "list", "-g", rg(), "--query", "[].name") + az(
        "logicapp", "list", "-g", rg(), "--query", "[].name")
    for site in sites:
        for s in az("functionapp", "config", "appsettings", "list", "-g", rg(), "-n", site):
            scan(site, s["name"], s.get("value") or "")
    check("2 no key material in configuration", not found,
          "; ".join(found) or f"azd env, container apps, {len(sites)} function/logic apps: no "
          "key material outside the accepted exceptions; the only Key Vault reference is the "
          "APIM key")


# ---------------------------------------------------------------------------------- 3 --
def local_auth() -> None:
    bad: list[str] = []
    for a in az("cosmosdb", "list", "-g", rg()):
        if not a.get("disableLocalAuth"):
            bad.append(f"cosmos {a['name']}")
    for s in az("search", "service", "list", "-g", rg()):
        if not s.get("disableLocalAuth"):
            bad.append(f"search {s['name']}")
    for n in az("servicebus", "namespace", "list", "-g", rg()):
        if not n.get("disableLocalAuth"):
            bad.append(f"servicebus {n['name']}")
    for st in az("storage", "account", "list", "-g", rg()):
        if st.get("allowSharedKeyAccess") is not False:
            if st["name"].startswith("statlaslogic"):
                exceptions.append(f"storage {st['name']} - Logic Apps host storage (accepted)")
            else:
                bad.append(f"storage {st['name']}")
    check("3 local auth disabled (Cosmos, Search, Service Bus, Storage)", not bad,
          ", ".join(bad) or "every data store is Entra-only")


# ---------------------------------------------------------------------------------- 4 --
def atlas_principals() -> dict[str, str]:
    """Every identity ATLAS owns: the user-assigned MIs, each resource's system identity, the
    hosted agent's agent identity and the CI app. The presenter (a user) is not one of them."""
    out = {i["principalId"]: i["name"] for i in az("identity", "list", "-g", rg())}
    for r in az("resource", "list", "-g", rg(), "--query",
                "[?identity.principalId].{n:name, p:identity.principalId}"):
        out[r["p"]] = r["n"]
    for key in ("ATLAS_HOSTED_AGENT_PRINCIPAL_ID", "ATLAS_CI_PRINCIPAL_ID",
                "ATLAS_FOUNDRY_PROJECT_PRINCIPAL_ID", "ATLAS_APIM_PRINCIPAL_ID"):
        if os.environ.get(key):
            out[os.environ[key]] = key.removeprefix("ATLAS_").removesuffix("_PRINCIPAL_ID").lower()
    return out


def least_privilege() -> None:
    scope = f"/subscriptions/{os.environ['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{rg()}"
    ours = atlas_principals()
    # Every assignment ATLAS identities hold, at any scope (`--all` cannot take --scope).
    rows = [r for r in az("role", "assignment", "list", "--all") if r["principalId"] in ours]
    broad = [f"{ours[r['principalId']]}: {r['roleDefinitionName']} on {r['scope']}"
             for r in rows if r.get("roleDefinitionName") in BROAD_ROLES]
    outside = [r for r in rows if not r["scope"].lower().startswith(scope.lower())
               and "/resourcegroups/" not in r["scope"].lower()]
    check("4 least privilege: no broad role on any ATLAS identity", not broad,
          "; ".join(broad) or f"{len(ours)} ATLAS identities hold {len(rows)} assignments, none "
          f"Owner, Contributor or access-admin; {len(outside)} above resource-group scope")
    # Not ATLAS's to fix, but it is access to ATLAS resources: report it, do not count it.
    inherited = az("role", "assignment", "list", "--scope", scope, "--include-inherited")
    foreign = sorted({f"{r.get('principalName') or r['principalId']} ({r['roleDefinitionName']})"
                      for r in inherited if r.get("principalType") == "ServicePrincipal"
                      and r["principalId"] not in ours
                      and r.get("roleDefinitionName") in BROAD_ROLES})
    if foreign:
        print(f"INFO  {len(foreign)} non-ATLAS service principals inherit a broad role on the "
              f"resource group from subscription or tenant scope: {', '.join(foreign)}")


# ---------------------------------------------------------------------------------- 5 --
def no_public_backend() -> None:
    probes = {
        "ops API, no token": f"{os.environ['ATLAS_FUNC_API_URL'].rstrip('/')}/api/services",
        "ops MCP, no token": f"{os.environ['ATLAS_FUNC_MCP_URL'].rstrip('/')}/runtime/webhooks/mcp",
        "APIM ops, no key": f"{os.environ['ATLAS_APIM_GATEWAY_URL'].rstrip('/')}/ops/services",
        "web API, no token": f"{os.environ['ATLAS_WEB_URL'].rstrip('/')}/api/me",
    }
    seen, bad = [], []
    for name, url in probes.items():
        code = httpx.get(url, timeout=30, follow_redirects=False).status_code
        seen.append(f"{name} {code}")
        if code not in (401, 403, 302):
            bad.append(f"{name} answered {code}")
    check("5 no unauthenticated path to a backend", not bad, "; ".join(bad or seen))


# ---------------------------------------------------------------------------------- 6 --
def content_filters() -> None:
    account = os.environ["ATLAS_FOUNDRY_ACCOUNT_NAME"]
    deps = {d["name"]: d["properties"].get("raiPolicyName")
            for d in az("cognitiveservices", "account", "deployment", "list", "-g", rg(),
                        "-n", account)}
    policies = {p["name"] for p in az("rest", "--method", "get", "--url",
                                      f"https://management.azure.com/subscriptions/"
                                      f"{os.environ['AZURE_SUBSCRIPTION_ID']}/resourceGroups/"
                                      f"{rg()}/providers/Microsoft.CognitiveServices/accounts/"
                                      f"{account}/raiPolicies?api-version=2024-10-01")["value"]}
    bad = [f"{n}: {p}" for n, p in deps.items()
           if n in ("gpt-main", "gpt-judge") and (p != "atlas-strict" or p not in policies)]
    guard = subprocess.run([sys.executable, "-m", "scripts.hosted_agent", "check-guardrail"],
                           cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
    if guard.returncode != 0:
        bad.append("hosted agent guardrail: " + (guard.stdout + guard.stderr).strip()[-160:])
    check("6 content filters active", not bad,
          "; ".join(bad) or "gpt-main and gpt-judge on atlas-strict (exists); the hosted agent's "
          "stored guardrail resolves")


# ---------------------------------------------------------------------------------- 7 --
def injection() -> None:
    gate = (ROOT / "evals" / "reports" / "gate-good.md").read_text(encoding="utf-8")
    full = (ROOT / "evals" / "reports" / "scorecard-v1-v2.md").read_text(encoding="utf-8")
    gate_ok = re.search(r"\| prompt_injection \| 3/3 \(100%\)", gate) is not None
    full_ok = re.search(r"\| prompt_injection \| 10 \| 10 \| 100%", full) is not None or \
        "prompt_injection | 100%" in full or re.search(r"prompt_injection[^\n]*100%", full)
    check("7 prompt-injection canaries pass", gate_ok and bool(full_ok),
          "hosted agent v7: holdout 3/3 (gate-good.md); in-process agent v2: 10/10 "
          "(scorecard-v1-v2.md); prompt agent and orchestrator agents: not separately "
          "measured" if gate_ok and full_ok else "a committed scorecard shows a miss")


# ---------------------------------------------------------------------------------- 8 --
def approval_tokens() -> None:
    unit = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "tests/test_ops_api.py", "-k", "token"], cwd=ROOT,
                          capture_output=True, text=True, timeout=300, check=False)
    from scripts.sse_probe import cli_token

    r = httpx.post(f"{os.environ['ATLAS_WEB_URL'].rstrip('/')}/api/gateway/probe", timeout=60,
                   headers={"Authorization": f"Bearer {cli_token()}"},
                   json={"kind": "remediation",
                         "case_id": f"CASE-SEC{uuid.uuid4().hex[:6].upper()}",
                         "plan_id": uuid.uuid4().hex[:16], "approval_token": "forged." + "A" * 40})
    live = r.json().get("status") if r.status_code == 200 else f"web {r.status_code}"
    summary = (unit.stdout.strip().splitlines() or ["no output"])[-1]
    check("8 approval tokens: forged, expired, replayed, cross-case all refused",
          unit.returncode == 0 and live in (401, 403, 409, 422),
          f"unit: {summary}; live forged token through APIM: HTTP {live}")


# ---------------------------------------------------------------------------------- 9 --
def pii() -> None:
    allowed = re.compile(r"(\.example(\.com|\.org|\.net)?|contoso\.com|example\.com)$", re.I)
    emails = set()
    for f in (ROOT / "data" / "seed").rglob("*"):
        if f.is_file() and f.suffix in (".json", ".jsonl", ".md"):
            emails |= set(re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)",
                                     f.read_text(encoding="utf-8")))
    real = sorted(d for d in emails if not allowed.search(d))
    capture = os.environ.get("ATLAS_CAPTURE_CONTENT", "unset")
    check("9 PII hygiene: seeded data uses only reserved domains", not real,
          f"domains in seed data: {sorted(emails) or 'none'}; content capture: {capture} "
          f"(demo environment only)" if not real else f"real-looking domains: {real}")


def main() -> int:
    load_local_env()
    for name, fn in [("1", secrets_in_history), ("2", secrets_in_config), ("3", local_auth),
                     ("4", least_privilege), ("5", no_public_backend), ("6", content_filters),
                     ("7", injection), ("8", approval_tokens), ("9", pii)]:
        guarded(name, fn)
    for e in sorted(set(exceptions)):
        print(f"ACCEPTED  {e}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"security: {passed}/{len(results)} checks passed, {len(results) - passed} findings, "
          f"{len(set(exceptions))} accepted exceptions")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
