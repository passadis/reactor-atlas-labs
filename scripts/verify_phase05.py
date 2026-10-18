"""Phase 05 verification: the Foundry hosted agent, its versions, identity, guardrail and toolbox.

Against live resources only. Writes: application data (a few chat turns through the web app) and
two endpoint pin flips (rollback, then re-pin forward) - pointer flips, no build, no deploy.

Exit non-zero on any failure. Wrapped by scripts/verify_phase05.sh for `make verify-all`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from atlas_agent.schemas import IncidentTriage
from atlas_common.auth import get_credential
from atlas_common.config import load_local_env
from azure.ai.projects import AIProjectClient

from scripts import hosted_agent as ha
from scripts.sse_probe import CANONICAL, cli_token, run_turn

results: list[tuple[str, bool, str]] = []
state: dict[str, Any] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def guarded(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except BaseException as exc:  # SystemExit from the helper module is a failed check here
        check(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")


def run(*args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, encoding="utf-8",
        timeout=timeout, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def project() -> AIProjectClient:
    return AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
                           credential=get_credential())


# ------------------------------------------------------------ 1 active, pinned, guardrailed --
def versions_and_guardrail() -> None:
    p = project()
    pinned = ha.pinned_version(p)
    versions = {str(v.version): v for v in ha.versions(p)}
    state["pinned"] = pinned
    check("1 hosted agent pinned to an active version",
          bool(pinned) and pinned in versions and ha.status_of(versions[pinned]) == "active",
          f"pinned {pinned}, versions {sorted(versions, key=int)}")
    ok, why = ha.guardrail_ok(versions[pinned])
    check("1 pinned version carries a real, existing RAI policy (not fail-open)", ok, why)


# ----------------------------------------------------------------- 2 invoke, 3 identity --
def invoke_and_identity() -> None:
    p = project()
    started = datetime.now(UTC)
    state["invoked_at"] = started
    t0 = time.perf_counter()
    response = p.get_openai_client(agent_name=ha.AGENT).responses.create(input=CANONICAL,
                                                                        timeout=240)
    seconds = time.perf_counter() - t0
    triage = IncidentTriage.model_validate(json.loads(response.output_text))
    calls = [o.name for o in response.output if o.type == "function_call"]
    state["hosted"] = triage
    check("2 hosted invoke returns a valid IncidentTriage",
          triage.severity == "Sev2" and "CHG-2026-0873" in triage.suspected_change_ids,
          f"{seconds:.1f}s, {len(calls)} tool calls, {triage.severity}, "
          f"{triage.suspected_change_ids}, cites {[c.doc_id for c in triage.citations][:3]}")

    identity = getattr(p.agents.get(ha.AGENT), "instance_identity", None)
    principal = getattr(identity, "principal_id", None)
    state["principal"] = principal
    az = shutil.which("az") or "az"
    roles = subprocess.run(
        [az, "role", "assignment", "list", "--assignee", str(principal), "--all",
         "--query", "[].roleDefinitionName", "-o", "tsv"],
        capture_output=True, text=True, timeout=120,
    ).stdout.split("\n")
    roles = sorted(r for r in roles if r.strip())
    check("3 dedicated Entra agent identity, with ATLAS read roles",
          bool(principal) and "Search Index Data Reader" in roles, f"{principal}: {roles}")


# ------------------------------------------------------------ 4 web: hosted vs this app --
def web_equivalence() -> None:
    url = os.environ["ATLAS_WEB_URL"].rstrip("/")
    with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {cli_token()}"},
                      timeout=60) as client:
        hosted = run_turn(client, CANONICAL, "hosted", None, None)
        app = run_turn(client, CANONICAL, "app", None, None)
    hv = hosted.get("structured", {}).get("value", {})
    av = app.get("structured", {}).get("value", {})
    same = (hv.get("severity") == av.get("severity")
            and set(hv.get("affected_services", [])) & set(av.get("affected_services", [])))
    check("4 web 'Hosted agent' and 'This app' give structurally equivalent triage",
          bool(same) and bool(hv),
          f"hosted {hv.get('severity')} {hv.get('affected_services')} in {hosted['elapsed_s']}s "
          f"({len(hosted['tools'])} tools); app {av.get('severity')} "
          f"{av.get('affected_services')} in {app['elapsed_s']}s")


# ---------------------------------------------------------------------- 5 traces --
def traces() -> None:
    since = state.get("invoked_at", datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    app, rg = os.environ["ATLAS_APPINSIGHTS_NAME"], os.environ["AZURE_RESOURCE_GROUP"]
    query = (f"union requests, dependencies | where timestamp >= datetime({since}) "
             f"and cloud_RoleName == '{ha.AGENT}' | count")
    az = shutil.which("az") or "az"
    deadline, count = time.monotonic() + 300, 0
    while time.monotonic() < deadline:
        proc = subprocess.run(
            [az, "monitor", "app-insights", "query", "--app", app, "-g", rg,
             "--analytics-query", query, "--query", "tables[0].rows[0][0]", "-o", "tsv"],
            capture_output=True, text=True, timeout=90,
        )
        count = int(proc.stdout.strip() or 0) if proc.returncode == 0 else 0
        if count:
            break
        time.sleep(20)
    check("5 hosted-agent spans in Application Insights within 5 minutes", count > 0,
          f"{count} spans from cloud_RoleName={ha.AGENT} since {since}")


# ------------------------------------------------------------------- 6 rollback --
def rollback_and_repin() -> None:
    p = project()
    before = ha.pinned_version(p)
    proc = run("-m", "scripts.hosted_agent", "rollback")
    after = ha.pinned_version(p)
    check("6 rollback_agent moves the pin to an earlier good version",
          proc.returncode == 0 and after is not None and before is not None
          and int(after) < int(before), f"{before} -> {after}")
    ha.pin(p, str(before))
    check("6 re-pinned forward", ha.pinned_version(p) == before, f"pinned {ha.pinned_version(p)}")


# ------------------------------------------------------------- toolbox + prompt agent --
def toolbox_and_prompt_agent() -> None:
    proc = run("-m", "scripts.upsert_toolbox", "--check")
    check("toolbox atlas-tools and its MCP connection match their definitions",
          proc.returncode == 0, " / ".join(proc.stdout.strip().splitlines()))
    proc = run("-m", "scripts.create_prompt_agent", "--check")
    check("prompt agent matches its definition (toolbox attached server-side)",
          proc.returncode == 0, proc.stdout.strip()[-120:])
    proc = run("-m", "scripts.create_prompt_agent", "--ask",
               "Which runbook covers elevated p95 latency on the Checkout API after a "
               "deployment? Give the runbook id.", timeout=240)
    check("prompt agent answers through the toolbox", "RB-0042" in proc.stdout,
          proc.stdout.strip().splitlines()[-1][:120] if proc.stdout.strip() else proc.stderr[-200:])


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    load_local_env()
    guarded("1 versions", versions_and_guardrail)
    guarded("2-3 invoke/identity", invoke_and_identity)
    guarded("4 web", web_equivalence)
    guarded("5 traces", traces)
    guarded("6 rollback", rollback_and_repin)
    guarded("toolbox", toolbox_and_prompt_agent)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
