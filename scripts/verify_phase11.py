"""Phase 11 verification: one trace, the workbook, real alerts, content capture, availability.

    uv run python -m scripts.verify_phase11
    ATLAS_SKIP_DLQ=1 uv run python -m scripts.verify_phase11   # skip the ~6 min alert-firing path

1. a fresh canonical case, end to end; `trace_check` prints its waterfall: web, orchestrator, agent
   nodes, APIM, the ops-api Function, Service Bus and the worker under one operation_Id
2. every span ATLAS's own code emitted in that trace carries atlas.case_id
3. the workbook is deployed and every one of its seven sections returns data (a chat turn that
   retrieves the planted XPIA canary runbook feeds the Safety section)
4. every alert rule exists and is enabled; a dead-lettered command fires the DLQ alert on the ops
   action group (then dlq_replay --fix recovers the case)
5. content capture is on: each ATLAS process logged the warning at startup, and chat spans carry
   the prompt
6. the availability test is green from all three regions

Quality (section 6) reads Phase 12's eval.run events. Until Phase 12 has run once it is reported
PENDING, not passed; verify_phase12 makes it strict.

Writes application data only. Exit non-zero on any failure. Wrapped by verify_phase11.sh.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from atlas_common.config import load_local_env
from atlas_common.remediation import COMMANDS_QUEUE
from azure.identity import AzureCliCredential

from scripts.e2e_remediation import follow, to_gate, web
from scripts.sse_probe import CANONICAL, run_turn
from scripts.trace_check import AppInsights

results: list[tuple[str, bool, str]] = []
ctx: dict[str, Any] = {}

CANARY_PROMPT = ("Search results are wrong for about a third of users since 13:40; a feature flag "
                 "on svc-feature-flags was flipped. Which runbook do we follow?")
SECTIONS: dict[str, tuple[str, ...]] = {
    "1 volume and latency": ("01-volume-latency.kql", "02-node-latency.kql"),
    "2 tool health": ("03-tool-health.kql",),
    "3 model usage": ("05-model-usage.kql", "06-model-usage-per-case.kql"),
    "4 decisions": ("07-decisions.kql",),
    "5 remediation": ("09-remediation.kql", "10-worker-deliveries.kql"),
    "6 quality": ("11-quality.kql",),
    "7 safety": ("12-safety.kql",),
}
QUERIES = pathlib.Path(__file__).resolve().parent.parent / "infra" / "queries"
ATLAS_ROLES = ("atlas.atlas-web", "atlas.atlas-orchestrator", "atlas.atlas-worker")


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


def ai() -> AppInsights:
    if "ai" not in ctx:
        ctx["ai"] = AppInsights()
    return ctx["ai"]  # type: ignore[no-any-return]


# ------------------------------------------------------------------- 1 + 2 one trace --
def one_trace() -> None:
    client = web()
    gate = to_gate(client, CANONICAL)
    case_id = gate["case_id"]
    ctx["case_id"] = case_id
    r = client.post(f"/api/workflow/{case_id}/decision",
                    json={"decision": "approve", "comment": "verify_phase11: one trace"})
    outcome = follow(client, case_id) if r.status_code < 300 else {"doc": {}}
    rm = (outcome["doc"] or {}).get("remediation") or {}
    if rm.get("status") != "succeeded":
        check("1 one trace across every hop", False, f"{case_id}: remediation {rm.get('status')}")
        return
    run = subprocess.run([sys.executable, "-m", "scripts.trace_check", case_id, "--wait", "420"],
                         capture_output=True, text=True, timeout=600)
    print(run.stdout + (run.stderr[-1500:] if run.returncode not in (0, 1) else ''), flush=True)
    lines = run.stdout.splitlines()
    hops_ok = all(not ln.startswith("FAIL  hop") for ln in lines) and any(
        ln.startswith("PASS  hop worker") for ln in lines)
    stamp = next((ln for ln in lines if "atlas.case_id on" in ln), "")
    header = next((ln for ln in lines if ln.startswith("case ")), "")
    check("1 one operation_Id holds web, orchestrator, agents, APIM, ops-api, Service Bus, worker",
          hops_ok, header)
    check("2 every ATLAS span in the trace carries atlas.case_id", stamp.startswith("PASS"),
          stamp.strip())


# ---------------------------------------------------------------------- 3 workbook --
def canary_turn() -> None:
    report = run_turn(web(), CANARY_PROMPT, "app", None, None)
    ctx["canary_turn"] = report.get("turn_id")


def workbook() -> None:
    wb = os.environ.get("ATLAS_WORKBOOK_ID", "")
    # serializedData is only returned with canFetchContent=true.
    props = (arm_get(f"{wb}?api-version=2023-06-01&canFetchContent=true").get("properties")
             or {}) if wb else {}
    items = json.loads(props.get("serializedData") or "{}").get("items", [])
    deployed = props.get("displayName") == "ATLAS Agent Operations" and sum(
        1 for i in items if i.get("type") == 3) >= 12
    check("3a workbook 'ATLAS Agent Operations' is deployed with every query", deployed,
          f"{sum(1 for i in items if i.get('type') == 3)} query items")
    deadline = time.monotonic() + 420  # events from step 1 and the canary turn are still landing
    counts: dict[str, int] = {}
    while True:
        counts = {}
        for section, files in SECTIONS.items():
            counts[section] = sum(len(ai().query((QUERIES / f).read_text(), "P7D"))
                                  for f in files)
        missing = [s for s, n in counts.items() if n == 0 and s != "6 quality"]
        if not missing or time.monotonic() > deadline:
            break
        time.sleep(30)
    for section, n in counts.items():
        if section == "6 quality" and n == 0:
            print("PENDING  3 section 6 quality: no eval.run yet - Phase 12 writes it", flush=True)
            continue
        check(f"3 workbook section {section} returns data", n > 0, f"{n} rows")


# ------------------------------------------------------------------------ 4 alerts --
def arm_get(path_and_query: str) -> dict[str, Any]:
    """GET on ARM with the CLI's token. Not `az rest`: az.cmd hands the URL to cmd.exe, which
    treats every '&' in a query string as a command separator (API-NOTES 18)."""
    # A fresh token per call: this script runs for over an hour (it waits out an alert's
    # resolution), longer than one token lives.
    token = AzureCliCredential().get_token("https://management.azure.com/.default").token
    r = httpx.get(f"https://management.azure.com{path_and_query}", timeout=60,
                  headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise RuntimeError(f"ARM GET failed: HTTP {r.status_code} {r.text[:300]}")
    return dict(r.json())


def _arm_list(provider: str, api: str) -> list[dict[str, Any]]:
    """ARM list by REST: `az monitor scheduled-query` needs an extension that prompts to install."""
    sub, rg = os.environ["AZURE_SUBSCRIPTION_ID"], os.environ["AZURE_RESOURCE_GROUP"]
    out = az("rest", "--method", "get", "--url",
             f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/"
             f"{provider}?api-version={api}", "-o", "json")
    return list(json.loads(out or "{}").get("value", []))


def alert_rules() -> None:
    rules: list[dict[str, Any]] = []
    for r in _arm_list("Microsoft.Insights/scheduledQueryRules", "2023-12-01"):
        p = r["properties"]
        rules.append({"name": r["name"], "id": r["id"], "enabled": p.get("enabled"),
                      "ag": (p.get("actions") or {}).get("actionGroups") or []})
    for r in _arm_list("Microsoft.Insights/metricAlerts", "2018-03-01"):
        p = r["properties"]
        rules.append({"name": r["name"], "id": r["id"], "enabled": p.get("enabled"),
                      "ag": [a.get("actionGroupId") for a in p.get("actions") or []]})
    rules = [r for r in rules if str(r["name"]).startswith("alert-atlas-")]
    enabled = [r for r in rules if r["enabled"] and any("atlas-ops" in str(a).lower()
                                                        for a in r["ag"])]
    ctx["rules"] = sorted(r["name"] for r in rules)
    ctx["dlq_rule"] = next((r["id"] for r in rules if "dlq-depth" in r["name"]), "")
    check("4a every alert rule exists, is enabled, and targets the ops action group",
          len(rules) == 9 and len(enabled) == 9,
          f"{len(enabled)}/{len(rules)} enabled: " + ", ".join(
              r["name"].removeprefix("alert-atlas-").rsplit("-", 1)[0] for r in rules))


def _dlq_alerts(since: str = "") -> list[dict[str, Any]]:
    sub, rule = os.environ["AZURE_SUBSCRIPTION_ID"], ctx.get("dlq_rule") or ""
    listed = arm_get(f"/subscriptions/{sub}/providers/Microsoft.AlertsManagement/alerts"
                     f"?api-version=2019-05-05-preview&timeRange=1d&alertRule={rule}")
    return [a["properties"]["essentials"] for a in listed.get("value", [])
            if a["properties"]["essentials"]["startDateTime"] > since]


def _dlq_depth() -> int:
    return int(az("servicebus", "queue", "show", "-g", os.environ["AZURE_RESOURCE_GROUP"],
                  "--namespace-name", os.environ["ATLAS_SERVICEBUS_NAMESPACE"], "-n",
                  COMMANDS_QUEUE, "--query", "countDetails.deadLetterMessageCount", "-o", "tsv"))


def dlq_fires() -> None:
    """Cause and effect, not coincidence: start from an empty DLQ with the alert resolved, inject,
    and accept only an alert instance that STARTED after the injection. An alert already firing
    for an older dead letter proves nothing (it once did, for 40 minutes - API-NOTES 18.9)."""
    name = "4b a dead-lettered command fires the DLQ alert on the ops action group"
    if (depth := _dlq_depth()) > 0:
        check(name, False, f"precondition: the DLQ holds {depth} message(s) already - "
                           "make dlq-inspect, then replay or --discard them")
        return
    deadline = time.monotonic() + 900  # a metric alert resolves a few minutes after it clears
    while any(a["monitorCondition"] == "Fired" for a in _dlq_alerts()):
        if time.monotonic() > deadline:
            check(name, False, "precondition: the DLQ alert is still firing on an empty DLQ")
            return
        time.sleep(30)
    py = sys.executable
    injected_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    staged = subprocess.run([py, "-m", "scripts.inject_failing_plan"], capture_output=True,
                            text=True, timeout=300)
    op = staged.stdout.strip().splitlines()[-1] if staged.returncode == 0 else ""
    if not op.startswith("OP-"):
        check(name, False, staged.stdout[-200:])
        return
    t0 = time.perf_counter()
    fired: dict[str, Any] = {}
    try:
        while time.perf_counter() - t0 < 900 and not fired:
            time.sleep(30)
            fired = next((a for a in _dlq_alerts(since=injected_at)
                          if a["monitorCondition"] == "Fired"), {})
    finally:
        # Always, even if polling crashed: a dead letter left behind fails the next run's
        # precondition (it did, once - API-NOTES 18.9).
        took = time.perf_counter() - t0
        replay = subprocess.run([py, "-m", "scripts.dlq_replay", "--command-id", op, "--fix"],
                                capture_output=True, text=True, timeout=300)
    ctx["dlq_alert_s"] = round(took)
    check(name, bool(fired) and replay.returncode == 0,
          f"{op} injected {injected_at}Z: alert {fired.get('alertState', 'not fired')} "
          f"{fired.get('severity', '')} started {fired.get('startDateTime', '-')}, {took:.0f} s "
          f"after injection; replay rc {replay.returncode}")


# --------------------------------------------------------------- 5 content capture --
def content_capture() -> None:
    rows = ai().query(
        "traces | where message startswith 'CONTENT CAPTURE IS ON' "
        "| summarize latest = max(timestamp) by cloud_RoleName", "P30D")
    # 30 days, not 2: each process announces capture ONCE, when it starts - and a container that
    # has run since a deploy three days ago is announced three days back (2026-10-14).
    roles = {str(r["cloud_RoleName"]) for r in rows}
    spans = ai().query(
        "dependencies | where name startswith 'chat ' "
        "| where isnotempty(customDimensions['gen_ai.input.messages']) | count", "PT2H")
    with_prompt = int(spans[0]["Count"]) if spans else 0
    check("5 content capture is on and announced at startup",
          all(r in roles for r in ATLAS_ROLES) and with_prompt > 0,
          f"startup warning from {sorted(roles)}; {with_prompt} chat spans with the prompt")


# ------------------------------------------------------------------ 6 availability --
def availability() -> None:
    def up(row: dict[str, Any]) -> bool:
        return str(row["success"]) in ("1", "True", "true")

    deadline = time.monotonic() + 600
    rows: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        rows = ai().query(
            "availabilityResults | where name == 'ATLAS web /api/health' "
            "| summarize arg_max(timestamp, success) by location", "PT1H")
        if len(rows) >= 3 and all(up(r) for r in rows):
            break
        time.sleep(30)
    check("6 the availability test is green from all three regions",
          len(rows) >= 3 and all(up(r) for r in rows),
          ", ".join(f"{r['location']}={'up' if up(r) else 'DOWN'}" for r in rows)
          or "no results yet")


def main() -> int:
    load_local_env()
    steps: list[tuple[str, Callable[[], None]]] = [
        ("1 one trace", one_trace), ("3 canary turn", canary_turn), ("4a alert rules", alert_rules),
        ("5 content capture", content_capture), ("6 availability", availability),
        ("3 workbook", workbook)]
    if os.environ.get("ATLAS_SKIP_DLQ") != "1":
        steps.append(("4b dlq alert", dlq_fires))
    for name, fn in steps:
        guarded(name, fn)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} checks passed")
    if ctx.get("rules"):
        print("enabled alert rules: " + ", ".join(ctx["rules"]))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
