"""Phase 09 verification: APIM is the only way from an agent to a backend, and it governs.

    uv run python -m scripts.verify_phase09
    ATLAS_SKIP_DESCRIPTION_CHANGE=1 uv run python -m scripts.verify_phase09   # skip check 6

1. all four APIs exist with the expected operations; both MCP servers list and call their tools
2. scripts/prove_governance.py passes all eight proofs
3. every agent surface reaches tools only through APIM (configuration, and the platform lock)
4. llm-emit-token-metric metrics reach Application Insights with the CaseId dimension
5. APIM writes a request row with gen_ai.* dimensions per MCP tool call
6. one tool description changed in APIM reaches all four agent surfaces (then restored)
7. every workload identity holds Atlas.Agent; the operator does not

Check 6 is a control-plane write to one APIM tool resource, always restored. Exit non-zero on any
failure. Wrapped by scripts/verify_phase09.sh for `make verify-all`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from atlas_common.config import load_local_env

from scripts.sse_probe import cli_token

API_VERSION = "2025-09-01-preview"
results: list[tuple[str, bool, str]] = []
ctx: dict[str, Any] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def guarded(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except BaseException as exc:
        check(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")


def az(*args: str) -> str:
    return subprocess.run([shutil.which("az") or "az", *args], capture_output=True, text=True,
                          timeout=240, check=True).stdout.strip()


def kql(query: str) -> str:
    return az("monitor", "app-insights", "query", "--app", os.environ["ATLAS_APPINSIGHTS_NAME"],
              "-g", os.environ["AZURE_RESOURCE_GROUP"], "--analytics-query", query,
              "--query", "tables[0].rows[0][0]", "-o", "tsv")


def poll_kql(query: str, budget_s: float = 300) -> int:
    deadline, value = time.monotonic() + budget_s, 0
    while time.monotonic() < deadline:
        try:
            value = int(kql(query) or 0)
        except (subprocess.CalledProcessError, ValueError):
            value = 0
        if value:
            return value
        time.sleep(20)
    return value


def apim_base() -> str:
    sub = az("account", "show", "--query", "id", "-o", "tsv")
    return (f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
            f"{os.environ['AZURE_RESOURCE_GROUP']}/providers/Microsoft.ApiManagement/service/"
            f"{os.environ['ATLAS_APIM_NAME']}")


def arm(path: str) -> dict[str, Any]:
    return dict(json.loads(az("rest", "--method", "get", "--url",
                              f"{apim_base()}{path}?api-version={API_VERSION}")))


def web() -> httpx.Client:
    if "web" not in ctx:
        ctx["web"] = httpx.Client(base_url=os.environ["ATLAS_WEB_URL"].rstrip("/"), timeout=180,
                                  headers={"Authorization": f"Bearer {cli_token()}"})
    client: httpx.Client = ctx["web"]
    return client


# ------------------------------------------------------------------------ 1 APIs --
def apis_exist() -> None:
    governed = json.loads((Path(__file__).resolve().parents[1] / "apim/mcp/"
                           "atlas-ops-mcp-tools.json").read_text(encoding="utf-8"))["tools"]
    expected = {"atlas-ops-api": ("http", 11), "atlas-ops-mcp": ("mcp", len(governed)),
                "atlas-tools-mcp": ("mcp", 0), "atlas-models": ("http", 2)}
    for api, (kind, count) in expected.items():
        props = arm(f"/apis/{api}")["properties"]
        actual_kind = props.get("type") or "http"
        if kind == "mcp" and count:
            n = len(arm(f"/apis/{api}/tools").get("value", []))
            what = "tools"
        elif kind == "http":
            n = len(arm(f"/apis/{api}/operations").get("value", []))
            what = "operations"
        else:
            n, what = 0, "passthrough (tools come from the backend)"
        check(f"1 {api} exists at /{props.get('path')}",
              actual_kind == kind and (n == count or not count),
              f"type {actual_kind}, {n} {what}")

    probe = web().post("/api/gateway/probe", json={"kind": "mcp_tools"}).raise_for_status().json()
    ctx["probe"] = probe
    for server, want in (("ops-mcp", len(governed)), ("tools-mcp", 6)):
        info = probe["servers"].get(server, {})
        tools = [t["name"] for t in info.get("tools", [])]
        call = info.get("sample_call", {})
        retried = ""
        if info.get("attempts", 1) > 1:
            retried = f"; attempt {info.get('attempts')} after {info.get('errors')}"
        check(f"1 {server} lists its tools and answers a tools/call through the gateway",
              info.get("status") == "ok" and len(tools) == want and call.get("ok"),
              f"{len(tools)} tools: {', '.join(tools)}; {call.get('tool')} ok={call.get('ok')}"
              + (retried or (f"; {info.get('detail')}" if info.get("detail") else "")))


# ------------------------------------------------------------------------ 2 governance --
def governance() -> None:
    proc = subprocess.run([sys.executable, "-m", "scripts.prove_governance"],
                          capture_output=True, text=True, timeout=900)
    ctx["governance_output"] = proc.stdout
    summary = [ln.strip() for ln in proc.stdout.splitlines() if "governance proofs passed" in ln]
    failed = [ln.strip() for ln in proc.stdout.splitlines() if ln.rstrip().endswith("FAIL")]
    check("2 prove_governance.py passes every proof", proc.returncode == 0,
          (summary[0] if summary else proc.stderr[-300:]) + (f"; failed: {failed}" if failed
                                                              else ""))


# ------------------------------------------------------------------------ 3 only via APIM --
def only_via_apim() -> None:
    rg, gateway = os.environ["AZURE_RESOURCE_GROUP"], os.environ["ATLAS_APIM_GATEWAY_URL"]
    apim_principal = os.environ["ATLAS_APIM_PRINCIPAL_ID"]
    for fragment in ("atlas-api", "atlas-mcp"):
        name = az("functionapp", "list", "-g", rg, "--query",
                  f"[?contains(name, '{fragment}')].name | [0]", "-o", "tsv")
        auth = json.loads(az("rest", "--method", "get", "--url",
                             f"{apim_base().split('/providers/')[0]}/providers/Microsoft.Web/"
                             f"sites/{name}/config/authsettingsV2?api-version=2024-04-01"))
        aad = auth["properties"]["identityProviders"]["azureActiveDirectory"]
        allowed = aad["validation"]["defaultAuthorizationPolicy"]["allowedPrincipals"]["identities"]
        check(f"3 {name} accepts only the gateway's identity",
              auth["properties"]["globalValidation"]["requireAuthentication"]
              and allowed == [apim_principal], f"allowedPrincipals {allowed}")

    for fragment in ("atlas-web", "atlas-orch"):
        name = az("containerapp", "list", "-g", rg, "--query",
                  f"[?contains(name, '{fragment}')].name | [0]", "-o", "tsv")
        envs = json.loads(az("containerapp", "show", "-g", rg, "-n", name, "--query",
                             "properties.template.containers[0].env", "-o", "json"))
        values = {e["name"]: e.get("value", "") for e in envs}
        check(f"3 {name} calls tools through the gateway",
              values.get("ATLAS_OPS_API_URL", "").startswith(gateway)
              and values.get("ATLAS_OPS_API_SCOPE") == "api://atlas-gateway/.default"
              and "/secrets/" in values.get("ATLAS_APIM_KEY_SECRET_URI", ""),
              f"ATLAS_OPS_API_URL={values.get('ATLAS_OPS_API_URL')}")

    from scripts import hosted_agent as ha
    p = ha.project()
    pinned = ha.pinned_version(p)
    version = next(v for v in ha.versions(p) if str(v.version) == str(pinned))
    definition = version.as_dict().get("definition", {}) if hasattr(version, "as_dict") else {}
    hosted_env = definition.get("environment_variables") or {}
    check("3 the hosted agent calls tools through the gateway",
          str(hosted_env.get("ATLAS_OPS_API_URL", "")).startswith(gateway),
          f"version {pinned}: ATLAS_OPS_API_URL={hosted_env.get('ATLAS_OPS_API_URL')}")

    toolbox = p.toolboxes.get("atlas-tools")
    latest = next(iter(p.toolboxes.list_versions("atlas-tools", limit=1, order="desc")))
    urls = [t.get("server_url") for t in latest.as_dict().get("tools", [])
            if t.get("type") == "mcp"]
    check("3 the toolbox (prompt agent) reaches both MCP servers through the gateway",
          len(urls) == 2 and all(str(u).startswith(gateway) for u in urls),
          f"default v{toolbox.default_version}, latest v{latest.version}: {urls}")

    health = web().get("/api/health").json()
    probe = (health.get("checks") or {}).get("ops_api") or {}
    check("3 the web app's live tools report the gateway backend",
          probe.get("status") == "ok" and "ops-api" in str(probe.get("detail")),
          f"{probe.get('status')}: {probe.get('detail')}")


# ------------------------------------------------------------------------ 4 token metric --
def token_metric() -> None:
    import asyncio

    from atlas_common.secrets import read_secret
    from azure.identity.aio import AzureCliCredential

    async def key() -> str:
        async with AzureCliCredential() as cred:
            return await read_secret(os.environ["ATLAS_APIM_READONLY_KEY_SECRET_URI"], cred)

    case = f"CASE-V09{uuid.uuid4().hex[:6].upper()}"
    r = httpx.post(f"{os.environ['ATLAS_APIM_GATEWAY_URL']}/models/responses", timeout=120,
                   headers={"Ocp-Apim-Subscription-Key": asyncio.run(key()),
                            "x-atlas-case-id": case, "x-atlas-agent": "verify_phase09"},
                   json={"model": "gpt-main", "input": "Say OK.", "max_output_tokens": 16})
    ctx["metric_case"] = case
    query = (f"customMetrics | where timestamp > ago(1h) | "
             f"where tostring(customDimensions.CaseId) == '{case}' | count")
    n = poll_kql(query)
    ctx["metric_query"] = (
        "customMetrics | where timestamp > ago(1d) and name in ('Total Tokens', "
        "'Prompt Tokens', 'Completion Tokens') | extend CaseId = tostring(customDimensions.CaseId),"
        " AgentName = tostring(customDimensions.AgentName) | summarize tokens = sum(valueSum) "
        "by bin(timestamp, 5m), name, AgentName | render timechart")
    check("4 token metrics reach Application Insights with the CaseId dimension",
          r.status_code == 200 and n > 0,
          f"/models HTTP {r.status_code}; {n} customMetrics rows for CaseId {case}")


# ------------------------------------------------------------------------ 5 gen_ai rows --
def mcp_rows() -> None:
    query = ("requests | where timestamp > ago(1h) | where tostring(customDimensions) has "
             "'gen_ai' | count")
    n = poll_kql(query)
    check("5 APIM writes request rows with gen_ai.* dimensions for MCP tool calls", n > 0,
          f"{n} rows in the last hour")


# ------------------------------------------------------------------------ 6 four surfaces --
def description_change() -> None:
    if os.environ.get("ATLAS_SKIP_DESCRIPTION_CHANGE") == "1":
        check("6 one description in APIM reaches all four surfaces", True,
              "SKIPPED by ATLAS_SKIP_DESCRIPTION_CHANGE")
        return
    proc = subprocess.run([sys.executable, "-m", "scripts.tool_description_change"],
                          capture_output=True, text=True, timeout=900)
    lines = [ln.strip() for ln in proc.stdout.splitlines() if "in force" in ln or "NOT SEEN" in ln]
    ctx["description_output"] = proc.stdout
    check("6 one description changed in APIM reaches all four agent surfaces",
          proc.returncode == 0, "; ".join(lines) or proc.stderr[-300:])


# ------------------------------------------------------------------------ 7 roles --
def roles() -> None:
    # shutil.which, not a bare "bash": Windows CreateProcess searches System32 first and would
    # start WSL's bash, which inherits none of this environment.
    proc = subprocess.run([shutil.which("bash") or "bash", "scripts/assign_gateway_roles.sh",
                           "--check"],
                          capture_output=True, text=True, timeout=300)
    ok_lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("ok")]
    check("7 every workload identity holds Atlas.Agent", proc.returncode == 0,
          f"{len(ok_lines)} assignments" + ("" if proc.returncode == 0 else f": {proc.stdout}"))
    me = az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv")
    sp = az("ad", "sp", "show", "--id", os.environ["ATLAS_GATEWAY_APP_CLIENT_ID"],
            "--query", "id", "-o", "tsv")
    # Filtered in Python: a JMESPath `==` does not survive az.cmd's quoting on Windows.
    assigned = json.loads(az("rest", "--method", "get", "--url",
                             f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp}/"
                             "appRoleAssignedTo"))
    mine = [a for a in assigned.get("value", []) if a.get("principalId") == me]
    check("7 the operator deliberately does NOT hold Atlas.Agent", not mine,
          "proof 3 depends on it")


def main() -> int:
    load_local_env()
    # Order matters: the token metric uses the read-only model budget, which proof 7 in
    # prove_governance deliberately exhausts for a minute.
    for name, fn in [("1 apis", apis_exist), ("4 token metric", token_metric),
                     ("7 roles", roles), ("3 only via apim", only_via_apim),
                     ("2 governance", governance), ("5 gen_ai rows", mcp_rows),
                     ("6 four surfaces", description_change)]:
        guarded(name, fn)

    print("\n--- report back ---")
    print(f"gateway: {os.environ.get('ATLAS_APIM_GATEWAY_URL')}")
    print(f"toolbox: atlas-tools ({os.environ.get('FOUNDRY_PROJECT_ENDPOINT', '').rstrip('/')}"
          "/toolboxes/atlas-tools/mcp)")
    print(f"token-metric chart query:\n  {ctx.get('metric_query', '(check 4 did not run)')}")
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
