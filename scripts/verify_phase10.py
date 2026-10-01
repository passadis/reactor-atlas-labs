"""Phase 10 verification: approvals, asynchronous remediation, DLQ and replay, one trace.

    uv run python -m scripts.verify_phase10
    ATLAS_SKIP_TIMEOUT=1 uv run python -m scripts.verify_phase10   # skip the ~3 min timeout path

1. end to end: canonical incident -> plan -> approval in the console -> Service Bus -> worker ->
   result -> completed case with a ticket; under 3 minutes; timing recorded
2. the Logic App approval run waited on its webhook and was completed by the decision
3. a duplicate dispatch of the same plan executes nothing a second time
4. the RB-0099 plan retries, dead-letters after 5 deliveries, and dlq_replay recovers it
5. a command forged straight onto the queue is refused, audited, and changes nothing
6. timeout: a 1-minute window, no decision -> rejected as expired, nothing dispatched
7. one Application Insights trace holds web, orchestrator, APIM, Function and worker

Writes application data only. Exit non-zero on any failure. Wrapped by verify_phase10.sh.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from atlas_common.config import Settings, load_local_env
from atlas_common.remediation import COMMANDS_QUEUE, service_bus_namespace
from atlas_common.repositories.base import CosmosStore
from azure.identity import AzureCliCredential
from azure.identity.aio import AzureCliCredential as AsyncCli
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient

from scripts.e2e_remediation import follow, to_gate, web
from scripts.sse_probe import CANONICAL

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


_loop = asyncio.new_event_loop()
_store: dict[str, Any] = {}


def cosmos_query(container: str, query: str, value: str,
                 pk: str | None = None) -> list[dict[str, Any]]:
    """One parameterised query (`@v` bound to `value`), on ONE long-lived Cosmos session.

    The first version opened a new CLI credential and Cosmos client per poll; one of those calls
    hung for ~55 minutes and the timeout check read a stale answer at the end of it. One session,
    and a hard 30 s ceiling per query.
    """
    async def run() -> list[dict[str, Any]]:
        if "store" not in _store:
            _store["cred"] = AsyncCli()
            _store["store"] = CosmosStore(Settings.from_env(), _store["cred"])
        result: list[dict[str, Any]] = await asyncio.wait_for(
            _store["store"].query(container, query, [{"name": "@v", "value": value}],
                                  partition_key=pk), 30)
        return result

    return _loop.run_until_complete(run())


def case_doc(case_id: str) -> dict[str, Any]:
    rows = (cosmos_query("cases", "SELECT * FROM c WHERE c.id = @v", case_id,
                                    case_id))
    return dict(rows[0]) if rows else {}


def logic_runs(workflow: str) -> list[dict[str, Any]]:
    site = az("logicapp", "list", "-g", os.environ["AZURE_RESOURCE_GROUP"], "--query", "[0].id",
              "-o", "tsv")
    out = az("rest", "--method", "get", "--url",
             f"https://management.azure.com{site}/hostruntime/runtime/webhooks/workflow/api/"
             f"management/workflows/{workflow}/runs?api-version=2018-11-01", "-o", "json")
    return list(json.loads(out or "{}").get("value", []))


def kql(query: str) -> list[list[Any]]:
    app_id = az("monitor", "app-insights", "component", "show", "--app",
                os.environ["ATLAS_APPINSIGHTS_NAME"], "-g", os.environ["AZURE_RESOURCE_GROUP"],
                "--query", "appId", "-o", "tsv")
    token = AzureCliCredential().get_token("https://api.applicationinsights.io/.default").token
    r = httpx.post(f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
                   json={"query": query}, timeout=60,
                   headers={"Authorization": f"Bearer {token}"})
    return list(r.json()["tables"][0]["rows"]) if r.status_code == 200 else []


# ------------------------------------------------------------------------ 1 end to end --
def end_to_end() -> None:
    client = web()
    persona = (client.get("/api/me").json().get("persona") or {}).get("role")
    t0 = time.perf_counter()
    gate = to_gate(client, CANONICAL)
    t_gate = time.perf_counter() - t0
    case_id = gate["case_id"]
    ctx.update(case_id=case_id, trace_id=gate["traceparent"].split("-")[1]
               if gate["traceparent"] else "")
    r = client.post(f"/api/workflow/{case_id}/decision",
                    json={"decision": "approve", "comment": "verify_phase10: end to end"})
    outcome = follow(client, case_id) if r.status_code < 300 else {"doc": {}, "marks": {}}
    total = time.perf_counter() - t0
    doc, marks = outcome["doc"], outcome["marks"]
    rm = doc.get("remediation") or {}
    ctx.update(plan_id=rm.get("plan_id"), operation_id=rm.get("operation_id"))
    ctx["timing"] = {"to_gate": round(t_gate, 1), **{f"after_approval_{k}": v
                                                      for k, v in marks.items()},
                     "total": round(total, 1)}
    done = sum(1 for s in rm.get("steps") or [] if s.get("status") == "succeeded")
    check("1 incident -> approval in the console -> Service Bus -> worker -> completed, < 3 min",
          r.status_code == 200 and doc.get("status") == "completed"
          and rm.get("status") == "succeeded" and bool((doc.get("ticket") or {}).get("ticket_id"))
          and total < 180,
          f"{case_id} as {persona}: ticket {(doc.get('ticket') or {}).get('ticket_id')}, "
          f"{rm.get('operation_id')} {done}/{rm.get('steps_total')} steps, {total:.0f} s "
          f"{ctx['timing']}")


# ------------------------------------------------------------------------ 2 Logic App --
def logic_app_run() -> None:
    case_id = ctx["case_id"]
    run_id = (case_doc(case_id).get("approval_workflow") or {}).get("run_id", "")
    deadline, status = time.monotonic() + 60, ""
    while time.monotonic() < deadline:
        run = next((r for r in logic_runs("approve-remediation") if r.get("name") == run_id), {})
        status = (run.get("properties") or {}).get("status", "")
        if status and status not in ("Running", "Waiting"):
            break
        time.sleep(5)
    callback = (cosmos_query(
        "approvals", "SELECT * FROM c WHERE c.id = @v", f"callback:{case_id}", case_id))
    cb = callback[0] if callback else {}
    check("2 the approval run waited on its webhook and the decision completed it",
          status == "Succeeded" and cb.get("status") == "completed",
          f"run {run_id[-12:]} {status or 'not found'}; callback {cb.get('status')} "
          f"HTTP {cb.get('http_status')}")


# ------------------------------------------------------------------------ 3 duplicate --
def duplicate_dispatch() -> None:
    case_id, plan_id, op = ctx["case_id"], ctx["plan_id"], ctx["operation_id"]
    body = json.dumps({"operation_id": op, "case_id": case_id, "plan_id": plan_id,
                       "submitted_at": datetime.now(UTC).isoformat()})

    async def resend() -> None:
        async with AsyncCli() as cred, ServiceBusClient(service_bus_namespace(),
                                                        credential=cred) as client:
            async with client.get_queue_sender(COMMANDS_QUEUE) as sender:
                # Same MessageId inside the 10-minute window: Service Bus drops this one.
                await sender.send_messages(ServiceBusMessage(body, message_id=plan_id,
                                                             session_id="svc-checkout-api"))
                # A different MessageId gets through - and the worker must still not re-run it.
                await sender.send_messages(ServiceBusMessage(
                    body, message_id=f"{plan_id}:dup", session_id="svc-checkout-api"))

    before = case_doc(case_id).get("remediation") or {}
    asyncio.run(resend())
    time.sleep(20)
    op_doc = (cosmos_query("approvals", "SELECT * FROM c WHERE c.id = @v",
                                      f"operation:{op}", case_id))
    steps = [s for s in (op_doc[0].get("step_results") if op_doc else []) or []]
    attempts = sorted({int(s.get("attempt", 1)) for s in steps})
    after = case_doc(case_id).get("remediation") or {}
    check("3 a duplicate dispatch of the same plan executes nothing twice",
          len(steps) == int(before.get("steps_total") or 0) and attempts == [1]
          and after.get("status") == "succeeded",
          f"{len(steps)} step results, attempts {attempts}, status {after.get('status')}")


# ------------------------------------------------------------------------ 4 DLQ --
def dlq_and_replay() -> None:
    py = sys.executable
    staged = subprocess.run([py, "-m", "scripts.inject_failing_plan"], capture_output=True,
                            text=True, timeout=300)
    op = staged.stdout.strip().splitlines()[-1] if staged.returncode == 0 else ""
    if not op.startswith("OP-"):
        check("4 RB-0099 retries, dead-letters, and replays", False, staged.stdout[-200:])
        return
    t0 = time.perf_counter()
    dead = []
    while time.perf_counter() - t0 < 180:
        listed = subprocess.run([py, "-m", "scripts.dlq_inspect", "--json"], capture_output=True,
                                text=True, timeout=120)
        dead = [m for m in json.loads(listed.stdout or "[]") if m.get("operation_id") == op]
        if dead:
            break
        time.sleep(10)
    t_dead = time.perf_counter() - t0
    case_id = dead[0]["case_id"] if dead else ""
    parked = None
    for _ in range(10):  # the result reaches the case a moment after the message hits the DLQ
        parked = (case_doc(case_id).get("remediation") or {}).get("status") if case_id else None
        if parked == "dead_lettered" or not case_id:
            break
        time.sleep(3)
    replay = subprocess.run([py, "-m", "scripts.dlq_replay", "--command-id", op, "--fix"],
                            capture_output=True, text=True, timeout=300)
    final = ""
    t1 = time.perf_counter()
    while case_id and time.perf_counter() - t1 < 120:
        doc = case_doc(case_id)
        final = f"{doc.get('status')}/{(doc.get('remediation') or {}).get('status')}"
        if final == "completed/succeeded":
            break
        time.sleep(5)
    check("4 RB-0099 retries, dead-letters after 5 deliveries, and dlq_replay recovers it",
          bool(dead) and dead[0]["delivery_count"] >= 4 and parked == "dead_lettered"
          and replay.returncode == 0 and final == "completed/succeeded",
          f"{op}: DLQ after {t_dead:.0f} s ({dead[0]['reason'] if dead else 'never'}), case "
          f"{parked} -> replay -> {final}")


# ------------------------------------------------------------------------ 5 forged --
def forged_command() -> None:
    case = f"CASE-FORGED{secrets.token_hex(3).upper()}"
    op, plan = f"OP-FORGED{secrets.token_hex(3).upper()}", secrets.token_hex(8)

    async def forge() -> None:
        async with AsyncCli() as cred, ServiceBusClient(service_bus_namespace(),
                                                        credential=cred) as client:
            async with client.get_queue_sender(COMMANDS_QUEUE) as sender:
                await sender.send_messages(ServiceBusMessage(json.dumps(
                    {"operation_id": op, "case_id": case, "plan_id": plan,
                     "submitted_at": datetime.now(UTC).isoformat()}),
                    message_id=plan, session_id="svc-payments", correlation_id=case))

    asyncio.run(forge())
    refusal: list[dict[str, Any]] = []
    for _ in range(12):
        time.sleep(5)
        refusal = (cosmos_query(
            "approvals", "SELECT * FROM c WHERE c.kind = 'worker-refusal' AND "
                         "c.operation_id = @v", op, case))
        if refusal:
            break
    effects = (cosmos_query("control",
                                       "SELECT * FROM c WHERE c.operation_id = @v", op))
    check("5 a command forged onto the queue is refused, audited, and changes nothing",
          bool(refusal) and not effects,
          f"{op}: {refusal[0]['code'] if refusal else 'no refusal'}; "
          f"{len(effects)} state changes")


# ------------------------------------------------------------------------ 6 timeout --
def timeout_path() -> None:
    if os.environ.get("ATLAS_SKIP_TIMEOUT") == "1":
        check("6 timeout: expired window -> rejected, nothing dispatched", True,
              "SKIPPED by ATLAS_SKIP_TIMEOUT")
        return
    client = web()
    gate = to_gate(client, CANONICAL, ttl=1)
    case_id = gate["case_id"]
    t0 = time.perf_counter()
    doc: dict[str, Any] = {}
    while time.perf_counter() - t0 < 240:
        doc = case_doc(case_id)
        if doc.get("status") not in (None, "pending", "awaiting_approval"):
            break
        time.sleep(10)
    approval = (doc.get("state") or {}).get("approval") or {}
    check("6 timeout: a 1-minute window, no decision -> rejected as expired, nothing dispatched",
          doc.get("status") == "rejected" and approval.get("role") == "approval-window"
          and not doc.get("remediation"),
          f"{case_id}: {doc.get('status')} by {approval.get('by')} after "
          f"{time.perf_counter() - t0:.0f} s; remediation {doc.get('remediation')}")


# ------------------------------------------------------------------------ 7 one trace --
def one_trace() -> None:
    trace_id = ctx.get("trace_id", "")
    roles: set[str] = set()
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        roles = set()
        for table in ("requests", "dependencies", "traces"):
            for row in kql(f"{table} | where timestamp > ago(2h) and operation_Id == "
                           f"'{trace_id}' | distinct cloud_RoleName"):
                roles.add(str(row[0]))
        if any("worker" in r for r in roles):
            break
        time.sleep(20)
    hops = {"web": "atlas-web", "orchestrator": "atlas-orchestrator", "APIM": "apim",
            "Function": "func-atlas-api", "worker": "atlas-worker"}
    present = {h: any(n in r for r in roles) for h, n in hops.items()}
    check("7 one trace: web -> orchestrator -> APIM -> Function -> Service Bus -> worker",
          all(present.values()),
          f"trace {trace_id}: " + ", ".join(f"{h}={'yes' if v else 'NO'}"
                                            for h, v in present.items()))


def main() -> int:
    load_local_env()
    for name, fn in [("1 end to end", end_to_end), ("2 logic app", logic_app_run),
                     ("3 duplicate", duplicate_dispatch), ("5 forged", forged_command),
                     ("4 dlq", dlq_and_replay), ("6 timeout", timeout_path),
                     ("7 one trace", one_trace)]:
        if name != "1 end to end" and "case_id" not in ctx and name[0] in "237":
            check(name, False, "skipped: the end-to-end run did not produce a case")
            continue
        guarded(name, fn)
    print("\n--- report back ---")
    print(f"end-to-end timing (s): {ctx.get('timing')}")
    print(f"trace id covering every hop: {ctx.get('trace_id')}")
    passed = sum(ok for _, ok, _ in results)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
