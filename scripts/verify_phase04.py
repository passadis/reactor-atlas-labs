"""Phase 04 verification: the web app on Azure Container Apps, driven like a browser.

Every check hits the PUBLIC deployed URL. Authenticated calls use a real Entra token for the
signed-in Azure CLI user (`api://atlas-web/user_impersonation`), through Container Apps built-in
auth - no bypass anywhere.

Writes: a few chat turns, cases and sessions in Cosmos (application data, cleared by
`make demo-reset`). No control-plane writes.

Exit non-zero on any failure. Wrapped by scripts/verify_phase04.sh for `make verify-all`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

import httpx
from atlas_common.config import Settings, load_local_env
from atlas_common.repositories import CosmosStore

from scripts.sse_probe import CANONICAL, cli_token, run_turn

FIRST_EVENT_TARGET_S = 3.0
WARM_TRIAGE_TARGET_S = 15.0
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def guarded(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as exc:
        check(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")


def az(*args: str, timeout: int = 90) -> str:
    exe = shutil.which("az") or "az"
    proc = subprocess.run(
        [exe, *args], capture_output=True, text=True, timeout=timeout, check=True
    )
    return proc.stdout.strip()


state: dict[str, Any] = {}


# ------------------------------------------------------------------------- 1 the app --
def app_running() -> None:
    rg, name = os.environ["AZURE_RESOURCE_GROUP"], os.environ["ATLAS_WEB_APP_NAME"]
    app = json.loads(az("containerapp", "show", "-g", rg, "-n", name, "-o", "json"))
    props = app["properties"]
    image = props["template"]["containers"][0]["image"]
    check("1 web image is the ATLAS build, not the placeholder",
          "azurecr.io" in image and "quickstart" not in image, image.split("/")[-1])
    check("1 latest revision is the ready one",
          props["latestRevisionName"] == props["latestReadyRevisionName"],
          props["latestReadyRevisionName"])
    check("1 minReplicas >= 2", props["template"]["scale"]["minReplicas"] >= 2,
          str(props["template"]["scale"]["minReplicas"]))
    env = {e["name"]: e.get("value") for e in props["template"]["containers"][0].get("env", [])}
    check("4 ATLAS_DEMO_AUTH_BYPASS is not set in the deployed revision",
          "ATLAS_DEMO_AUTH_BYPASS" not in env)
    auth = json.loads(az("containerapp", "auth", "show", "-g", rg, "-n", name, "-o", "json"))
    enabled = auth.get("platform", {}).get("enabled") is True
    action = auth.get("globalValidation", {}).get("unauthenticatedClientAction")
    check("4 built-in auth enabled, unauthenticated -> login",
          enabled and action == "RedirectToLoginPage", f"enabled={enabled} action={action}")


# ------------------------------------------------------------------ 2 health, 4 auth --
def health_and_challenge(url: str) -> None:
    r = httpx.get(f"{url}/api/health", timeout=30)
    body = r.json()
    required = {k: v["status"] for k, v in body["checks"].items() if v["required"]}
    check("2 /api/health 200, every required dependency ok",
          r.status_code == 200 and all(s == "ok" for s in required.values()), str(required))
    # Built-in auth redirects browsers and answers 401 to non-browser clients, so ask like one.
    browser = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edg/140.0",
               "Accept": "text/html"}
    r = httpx.get(f"{url}/", timeout=30, follow_redirects=False, headers=browser)
    location = r.headers.get("location", "")
    check("4 unauthenticated page request is challenged (302 to Entra)",
          r.status_code == 302 and "login" in location, f"{r.status_code} {location[:60]}")
    r = httpx.get(f"{url}/api/me", timeout=30, follow_redirects=False,
                  headers={"X-Requested-With": "XMLHttpRequest"})
    check("4 unauthenticated API call is refused", r.status_code in (401, 403), str(r.status_code))


# ------------------------------------------------------------ 3 stream, 6 follow-up --
def streams(url: str) -> None:
    headers = {"Authorization": f"Bearer {cli_token()}"}
    with httpx.Client(base_url=url, headers=headers, timeout=60) as client:
        me = client.get("/api/me").json()
        check("4 bearer token accepted; identity and persona resolved",
              bool(me.get("email")) and me.get("auth") == "easyauth",
              f"{me.get('email')} as {me['persona']['role']} roles={me.get('app_roles')}")

        cold = run_turn(client, CANONICAL, "app", None, None)
        warms = [run_turn(client, CANONICAL, "app", None, None) for _ in range(3)]
        # Gate on the median of three warm runs: a single sample at the boundary is noise.
        warm = sorted(warms, key=lambda t: t["elapsed_s"])[1]
        state["warm_all"] = [t["elapsed_s"] for t in warms]
        for label, t in (("cold", cold), ("warm", warm)):
            state[label] = t
        s = warm.get("structured", {})
        value = s.get("value", {})
        tools = warm.get("tools", [])
        check("3 structured IncidentTriage at the end of the stream",
              s.get("kind") == "incident_triage" and value.get("severity") == "Sev2"
              and "CHG-2026-0873" in value.get("suspected_change_ids", []),
              f"{s.get('case_id')} {value.get('severity')} {value.get('suspected_change_ids')}")
        check("3 at least 4 tool lifecycle events with duration and outcome", len(tools) >= 4,
              "; ".join(tools[:5]))
        check(f"3 first event < {FIRST_EVENT_TARGET_S:.0f} s (warm)",
              (warm["first_event_s"] or 99) < FIRST_EVENT_TARGET_S,
              f"cold {cold['first_event_s']} s, warm {warm['first_event_s']} s")
        check(f"AC full triage < {WARM_TRIAGE_TARGET_S:.0f} s (warm, median of 3)",
              warm["elapsed_s"] < WARM_TRIAGE_TARGET_S,
              f"cold {cold['elapsed_s']} s, warm {state['warm_all']} s")
        check("AC traceparent delivered in the stream", bool(warm.get("traceparent")),
              str(warm.get("traceparent")))
        state["trace_id"] = (warm.get("traceparent") or "--").split("-")[1]
        state["case_id"] = s.get("case_id")

        follow = run_turn(client, "Is the service that caused this under a change freeze, and "
                          "which team owns it?", "app", warm["session_id"], None)
        text = follow["text"].lower()
        check("6 second turn resolves 'the service that caused this'",
              "team-payments" in text and "freeze" in text, follow["text"][:100])

        case = client.get(f"/api/cases/{state['case_id']}")
        check("case persisted and readable", case.status_code == 200
              and case.json().get("triage", {}).get("severity") == "Sev2", str(case.status_code))
        kb = client.get("/api/kb/RB-0042")
        check("citation target /kb/RB-0042 resolves", kb.status_code == 200
              and kb.json().get("doc_id") == "RB-0042", str(kb.status_code))
        refused = client.post(f"/api/approvals/{state['case_id']}",
                              json={"decision": "approve", "reason": "verify"})
        check("approval API refuses a case with no plan (server side)", refused.status_code == 409,
              str(refused.status_code))

        cut = run_turn(client, CANONICAL, "app", None, cancel_after=5.0)
        state["cancelled_turn"] = cut["turn_id"]


def cancellation() -> None:
    turn_id = state.get("cancelled_turn")
    if not turn_id:
        check("5 closing the stream cancels the server-side run", False, "no turn")
        return

    async def status() -> str:
        async with CosmosStore(Settings.from_env()) as store:
            for _ in range(10):
                rows = await store.query("sessions", "SELECT c.status FROM c WHERE c.id = @id",
                                         [{"name": "@id", "value": turn_id}])
                if rows and rows[0].get("status") != "streaming":
                    return str(rows[0].get("status"))
                await asyncio.sleep(3)
        return "streaming"

    final = asyncio.run(status())
    check("5 closing the stream cancels the server-side run", final == "cancelled", final)


def trace_in_app_insights() -> None:
    tid = state.get("trace_id")
    app, rg = os.environ.get("ATLAS_APPINSIGHTS_NAME"), os.environ.get("AZURE_RESOURCE_GROUP")
    query = (f"union requests, dependencies | where timestamp > ago(1h) "
             f"and operation_Id == '{tid}' | count")
    deadline, count = time.monotonic() + 240, 0
    while time.monotonic() < deadline:
        try:
            count = int(az("monitor", "app-insights", "query", "--app", str(app), "-g", str(rg),
                           "--analytics-query", query, "--query", "tables[0].rows[0][0]",
                           "-o", "tsv") or 0)
        except subprocess.CalledProcessError:
            count = 0
        if count:
            break
        time.sleep(20)
    check("AC browser-turn traceparent visible in Application Insights", count > 0,
          f"{count} spans for {tid}")


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    load_local_env()
    url = os.environ["ATLAS_WEB_URL"].rstrip("/")
    guarded("1 app", app_running)
    guarded("2 health", lambda: health_and_challenge(url))
    guarded("3 streams", lambda: streams(url))
    guarded("5 cancel", cancellation)
    guarded("AC trace", trace_in_app_insights)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for label in ("cold", "warm"):
        t = state.get(label)
        if t:
            print(f"  {label}: first event {t['first_event_s']} s, full triage {t['elapsed_s']} s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
