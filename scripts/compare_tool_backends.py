"""Run the canonical incident twice - tools on Cosmos, then tools on the ops API - and compare.

    uv run python -m scripts.compare_tool_backends

The two runs differ in exactly one value, `ATLAS_OPS_API_URL` (PHASE-08 section C), and the
script prints both so that is visible. The analysis is a model run, so prose differs run to run;
what must NOT differ are the facts the tools supply and the deterministic decisions built on
them: the suspected change, the blast radius, the Tier 1 exposure, the severity, and the gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from typing import Any

from atlas_agent.factory import IncidentAgentRuntime
from atlas_common.config import Settings, load_local_env
from atlas_orchestrator.runner import CaseRunner

CANONICAL = ("Checkout is timing out for about 20% of EU customers since 14:05, "
             "started right after the payments deploy.")


async def run_once(settings: Settings) -> dict[str, Any]:
    async with IncidentAgentRuntime(settings) as runtime:
        runner = CaseRunner(runtime, "graph", checkpoints=False)
        await runner.load_reference_data()
        started = time.perf_counter()
        doc = await runner.create(CANONICAL, raised_by={"persona": "sre"})
        case_id = str(doc["case_id"])
        async for event in runner.stream(case_id):
            if event.kind in ("approval_required", "report", "error"):
                break
        state = ((await runner.get(case_id)) or {}).get("state") or {}
        await runner.close()
    triage, impact = state.get("triage") or {}, state.get("impact") or {}
    policy = state.get("policy") or {}
    return {
        "backend": runtime.tools.backend,
        "seconds": round(time.perf_counter() - started, 1),
        "severity": triage.get("severity"),
        "suspected_changes": sorted(triage.get("suspected_change_ids") or []),
        "blast_radius": impact.get("blast_radius"),
        "tier1_affected": sorted(impact.get("tier1_affected") or []),
        "status": state.get("status"),
        "approver_roles": policy.get("required_approver_roles"),
        "plan_steps": len((state.get("plan") or {}).get("steps") or []),
    }


async def main() -> int:
    load_local_env()
    base = Settings.from_env()
    url = os.environ.get("ATLAS_OPS_API_URL", "")
    if not url:
        print("set ATLAS_OPS_API_URL (azd env get-value ATLAS_APIM_OPS_URL)", file=sys.stderr)
        return 2
    direct = await run_once(replace(base, ops_api_url=""))
    http = await run_once(replace(base, ops_api_url=url))

    print(f"ATLAS_OPS_API_URL: ''  vs  '{url}'   (the only difference)\n")
    keys = ["backend", "seconds", "severity", "suspected_changes", "blast_radius",
            "tier1_affected", "status", "approver_roles", "plan_steps"]
    print(f"{'':20s} {'direct (cosmos)':32s} http (ops API)")
    for key in keys:
        print(f"{key:20s} {json.dumps(direct[key]):32s} {json.dumps(http[key])}")

    # Approver roles and plan length are NOT compared: both follow from the plan, which is a model
    # output that varies run to run (a plan that touches a frozen service needs a director). The
    # facts the tools supply, and the decisions computed from those facts, must not vary.
    must_match = ["severity", "suspected_changes", "blast_radius", "tier1_affected", "status"]
    diverged = [k for k in must_match if direct[k] != http[k]]
    print("\nfacts and decisions match" if not diverged else f"\nDIVERGED: {diverged}")
    return 1 if diverged or http["backend"] != "ops-api" else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(asyncio.run(main()))
