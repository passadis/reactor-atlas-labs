"""Phase 08 verification: the operations API and the MCP tool server, against the deployed apps.

    uv run python -m scripts.verify_phase08

Seven checks, one per PHASE-08 acceptance criterion plus liveness:

1. both function apps are Running and answer their health route;
2. the hand-written OpenAPI spec is valid and matches the deployed routes exactly;
3. the ops API refuses an anonymous call (401) and serves an Entra-authenticated one (200);
4. a ticket submitted twice with one Idempotency-Key is created once and replayed once;
5. the MCP server lists its six tools, each saying when NOT to use it;
6. a forged approval token is refused (403, typed) and audited; a genuine one is accepted once
   and refused on reuse;
7. the deployed agents read through the ops API, and the switch is one configuration value.

Writes application data only (a ticket, a verification approval record, one queued command that
names a verification case). No control-plane writes. Exit non-zero on any failure. Wrapped by
scripts/verify_phase08.sh for `make verify-all`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from atlas_common.config import Settings, load_local_env
from atlas_common.repositories.base import CosmosStore
from azure.identity import AzureCliCredential

from atlas_common import approval_tokens
from scripts.mcp_probe import McpSession, mcp_app

ROOT = Path(__file__).resolve().parents[1]
OPS_SCOPE = "api://atlas-ops-api/.default"
EXPECTED_TOOLS = {"search_ops_knowledge", "get_service_topology", "get_oncall", "open_ticket",
                  "submit_remediation", "get_remediation_status"}

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
                          timeout=180, check=True).stdout.strip()


def function_app(fragment: str) -> str:
    return az("functionapp", "list", "-g", os.environ["AZURE_RESOURCE_GROUP"], "--query",
              f"[?contains(name, '{fragment}')].name | [0]", "-o", "tsv")


def ops() -> httpx.Client:
    if "ops" not in ctx:
        token = AzureCliCredential().get_token(OPS_SCOPE).token
        ctx["ops"] = httpx.Client(base_url=ctx["api_url"], timeout=90,
                                  headers={"Authorization": f"Bearer {token}"})
    client: httpx.Client = ctx["ops"]
    return client


# ------------------------------------------------------------------------ 1 liveness --
def apps_are_live() -> None:
    for fragment in ("atlas-api", "atlas-mcp"):
        name = function_app(fragment)
        state = az("functionapp", "show", "-g", os.environ["AZURE_RESOURCE_GROUP"], "-n", name,
                   # Flex Consumption apps report state under `properties`, not at the top.
                   "--query", "properties.state", "-o", "tsv")
        health = httpx.get(f"https://{name}.azurewebsites.net/api/health", timeout=90)
        check(f"1 {name} is Running and healthy", state == "Running" and health.status_code == 200,
              f"state {state}, /api/health HTTP {health.status_code}")
        if fragment == "atlas-api":
            ctx["api_url"] = f"https://{name}.azurewebsites.net/api"


# --------------------------------------------------------------- 2 spec matches deploy --
def spec_matches() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         "tests/test_ops_api.py", "-k", "spec or when_not_to_use or llm_facing"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    tail = (proc.stdout.strip().splitlines() or [""])[-1]
    import yaml
    spec = yaml.safe_load((ROOT / "apim/openapi/atlas-ops-api.yaml").read_text(encoding="utf-8"))
    ops_ids = [op["operationId"] for item in spec["paths"].values() for op in item.values()]
    ctx["operations"] = ops_ids
    check("2 the OpenAPI spec is valid, LLM-readable and matches the deployed routes",
          proc.returncode == 0, f"{tail}; {len(ops_ids)} operations: {', '.join(ops_ids)}")


# ------------------------------------------------------------------------ 3 auth --
def auth_is_enforced() -> None:
    anon = httpx.get(f"{ctx['api_url']}/services/svc-checkout-api", timeout=90)
    authed = ops().get("/services/svc-checkout-api")
    name = authed.json().get("name") if authed.status_code == 200 else authed.text[:120]
    check("3 anonymous calls get 401, Entra-authenticated calls get 200",
          anon.status_code == 401 and authed.status_code == 200,
          f"anonymous HTTP {anon.status_code}, bearer HTTP {authed.status_code} ({name})")


# ------------------------------------------------------------------ 4 idempotency --
def idempotent_ticket() -> None:
    key = f"verify-p08-{uuid.uuid4().hex[:12]}"
    body = {"case_id": "CASE-VERIFYP08", "severity": "Sev3",
            "summary": "Phase 08 verification ticket, safe to ignore.",
            "service_ids": ["svc-checkout-api"]}
    first = ops().post("/tickets", json=body, headers={"Idempotency-Key": key})
    second = ops().post("/tickets", json=body, headers={"Idempotency-Key": key})
    one, two = first.json(), second.json()
    ctx["idempotency"] = (f"key {key}: HTTP {first.status_code} {one.get('ticket_id')} then "
                          f"HTTP {second.status_code} {two.get('ticket_id')} "
                          f"Idempotency-Replayed={second.headers.get('Idempotency-Replayed')}")
    check("4 one Idempotency-Key creates one ticket and replays it",
          first.status_code == 201 and second.status_code == 200
          and one.get("ticket_id") == two.get("ticket_id")
          and second.headers.get("Idempotency-Replayed") == "true", ctx["idempotency"])


# ------------------------------------------------------------------------ 5 MCP --
def mcp_tools() -> None:
    base, key = mcp_app()
    session = McpSession(base, key)
    session.open()
    listed = (session.rpc("tools/list").get("result") or {}).get("tools", [])
    names = {t["name"] for t in listed}
    silent = [t["name"] for t in listed if "NOT" not in t.get("description", "")]
    ctx["tools"] = sorted(names)
    check("5 the MCP server lists its six tools, each saying when not to use it",
          names == EXPECTED_TOOLS and not silent,
          f"{len(listed)} tools: {', '.join(sorted(names))}"
          + (f"; no negative guidance: {silent}" if silent else ""))


# ------------------------------------------------------------------ 6 approval tokens --
async def _audit_rows(settings: Settings, case_id: str, cid: str) -> list[dict[str, Any]]:
    async with CosmosStore(settings) as store:
        return await store.query(
            approval_tokens.APPROVALS,
            "SELECT * FROM c WHERE c.kind = 'approval-refusal' AND c.correlation_id = @cid",
            [{"name": "@cid", "value": cid}], partition_key=case_id)


async def _mint(settings: Settings, case_id: str, plan_id: str) -> str:
    async with CosmosStore(settings) as store:
        minted = await approval_tokens.mint(store, case_id=case_id, plan_id=plan_id,
                                            approved_by="verify_phase08",
                                            approver_role="incident-commander")
    return minted.token


def submit(case_id: str, plan_id: str, token: str) -> httpx.Response:
    return ops().post("/remediation/commands",
                      json={"case_id": case_id, "plan_id": plan_id, "approval_token": token},
                      headers={"Idempotency-Key": f"verify-p08-{uuid.uuid4().hex[:12]}"})


def tokens_cannot_be_forged() -> None:
    settings = Settings.from_env()
    case_id = f"CASE-VERIFY{uuid.uuid4().hex[:8].upper()}"
    plan_id = uuid.uuid4().hex[:16]  # the API accepts plan ids in the planner's shape only

    forged = submit(case_id, plan_id, approval_tokens.TOKEN_PREFIX + "forged" + "x" * 37)
    body = forged.json()
    cid = str(body.get("correlation_id", ""))
    audit = asyncio.run(_audit_rows(settings, case_id, cid)) if cid else []
    check("6 a forged approval token is refused with a typed 403 and audited",
          forged.status_code == 403 and body.get("code") == "approval_invalid" and len(audit) == 1,
          f"HTTP {forged.status_code} {body.get('code')}, correlation {cid}, "
          f"{len(audit)} audit record(s)" + (f" code={audit[0].get('code')}" if audit else ""))

    token = asyncio.run(_mint(settings, case_id, plan_id))
    first = submit(case_id, plan_id, token)
    reuse = submit(case_id, plan_id, token)
    check("6 a genuine token is accepted once and refused on reuse",
          first.status_code == 202 and reuse.status_code == 403,
          f"first HTTP {first.status_code} {first.json().get('operation_id')}, "
          f"reuse HTTP {reuse.status_code} {reuse.json().get('message', '')[:70]}")


# ------------------------------------------------------------------ 7 the one switch --
def one_value_switch() -> None:
    rg = os.environ["AZURE_RESOURCE_GROUP"]
    seen: dict[str, str] = {}
    for fragment in ("atlas-web", "atlas-orch"):
        name = az("containerapp", "list", "-g", rg, "--query",
                  f"[?contains(name, '{fragment}')].name | [0]", "-o", "tsv")
        env = json.loads(az("containerapp", "show", "-g", rg, "-n", name, "--query",
                            "properties.template.containers[0].env", "-o", "json"))
        seen[name] = next((e.get("value", "") for e in env if e["name"] == "ATLAS_OPS_API_URL"), "")
    check("7 web and orchestrator carry ATLAS_OPS_API_URL pointing at the ops API",
          all(v.rstrip("/") == ctx["api_url"] for v in seen.values()),
          "; ".join(f"{k}={v or '(unset)'}" for k, v in seen.items()))

    from scripts.sse_probe import cli_token
    web = httpx.Client(base_url=os.environ["ATLAS_WEB_URL"].rstrip("/"), timeout=120,
                       headers={"Authorization": f"Bearer {cli_token()}"})
    health = web.get("/api/health").json()
    probe = (health.get("checks") or {}).get("ops_api") or {}
    check("7 the live agent tools report the ops-api backend",
          probe.get("status") == "ok" and "ops-api" in str(probe.get("detail")),
          f"web /api/health ops_api: {probe.get('status')} {probe.get('detail')}")


def main() -> int:
    load_local_env()
    for name, fn in [("1 liveness", apps_are_live), ("2 spec", spec_matches),
                     ("3 auth", auth_is_enforced), ("4 idempotency", idempotent_ticket),
                     ("5 mcp", mcp_tools), ("6 approval tokens", tokens_cannot_be_forged),
                     ("7 switch", one_value_switch)]:
        if name != "1 liveness" and "api_url" not in ctx:
            check(name, False, "skipped: the ops API was not reachable")
            continue
        guarded(name, fn)

    print("\n--- report back ---")
    print(f"tools/list: {', '.join(ctx.get('tools', []))}")
    print(f"operations: {', '.join(ctx.get('operations', []))}")
    print(f"idempotency: {ctx.get('idempotency', 'not run')}")
    passed = sum(ok for _, ok, _ in results)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
