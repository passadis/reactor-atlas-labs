"""Run each staged scenario through the deployed workflow and report what the agents and gate did.

    uv run python -m scripts.probe_scenarios                     # every scenario
    uv run python -m scripts.probe_scenarios reporting-ttl       # one
    uv run python -m scripts.probe_scenarios --json

For each scenario (data/generators/scenarios.py, published in data/seed/manifest.json) it raises the
scenario's prompt through the web app, as the presenter, waits for the policy gate, and checks the
things the scenario is built to show:

- triage found the scenario's change id (the thread to pull),
- the knowledge agent cited the scenario's runbook,
- the gate reached the intended outcome (auto-approved / approval required, and by whom).

An auto-approved case dispatches on its own; the probe follows it to the end. A parked case is left
awaiting approval (`make demo-reset` clears it) - approving it is the demo, not the probe.

Writes application data only. Exit non-zero if any scenario misses its intended gate outcome.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from atlas_common.config import load_local_env

from scripts.e2e_remediation import follow, to_gate, web
from scripts.sse_probe import CANONICAL

MANIFEST = Path(__file__).resolve().parent.parent / "data" / "seed" / "manifest.json"
# What each scenario must show at the gate. Kept beside the probe, not in the generator, because it
# is a statement about the deployed system's behaviour, checked live.
EXPECT: dict[str, dict[str, Any]] = {
    # The canonical incident, re-checked so a planner or gate change cannot quietly move it.
    "canonical": {"approval_required": True, "roles_exactly": ["director"]},
    "reporting-ttl": {"auto_approved": True},
    "search-flag": {"approval_required": True, "first_role": "sre"},
    "orders-stale": {"approval_required": True, "first_role": "manager", "reason_has": "RB-0015"},
}


def probe(client: Any, key: str, sc: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    gate = to_gate(client, sc["prompt"])
    case_id = gate["case_id"]
    t_gate = time.perf_counter() - t0
    doc = client.get(f"/api/workflow/{case_id}").raise_for_status().json()
    state = doc.get("state") or {}
    policy = state.get("policy") or {}
    triage = state.get("triage") or {}
    knowledge = state.get("knowledge") or {}
    plan = state.get("plan") or {}
    runbooks = [r.get("doc_id") for r in knowledge.get("runbooks") or []]
    outcome: dict[str, Any] = {}
    if policy.get("auto_approved"):
        followed = follow(client, case_id)
        rm = (followed["doc"] or {}).get("remediation") or {}
        outcome = {"remediation": rm.get("status"), "steps": rm.get("steps_total"),
                   "after_gate_s": followed["marks"].get("finished")}
    expect = EXPECT.get(key, {})
    roles = policy.get("required_approver_roles") or []
    checks = {
        "change found": sc["change_id"] in (triage.get("suspected_change_ids") or []),
        "runbook cited": sc["runbook_id"] in runbooks,
        "gate outcome": (
            (not expect.get("auto_approved") or bool(policy.get("auto_approved")))
            and (not expect.get("approval_required") or bool(policy.get("approval_required")))
            and (not expect.get("first_role") or (roles[:1] == [expect["first_role"]]))
            and (not expect.get("roles_exactly") or roles == expect["roles_exactly"])
            and (not expect.get("reason_has")
                 or any(expect["reason_has"] in r for r in policy.get("reasons") or []))),
    }
    if policy.get("auto_approved"):
        checks["auto-approved case remediated"] = outcome.get("remediation") == "succeeded"
    return {
        "scenario": key, "case_id": case_id, "to_gate_s": round(t_gate, 1),
        "severity": triage.get("severity"), "suspected_changes": triage.get("suspected_change_ids"),
        "runbooks": runbooks, "injection_noticed": knowledge.get("embedded_instructions_found"),
        "plan_risk": plan.get("risk"),
        "destructive_steps": sum(1 for s in plan.get("steps") or [] if s.get("destructive")),
        "gate": {"auto_approved": policy.get("auto_approved"),
                 "approval_required": policy.get("approval_required"),
                 "blocked": policy.get("blocked"), "roles": roles,
                 "reasons": policy.get("reasons")},
        **outcome, "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keys", nargs="*", help="scenario keys (default: all)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    load_local_env()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    scenarios = dict(manifest["scenarios"])
    scenarios["canonical"] = {"prompt": CANONICAL, **manifest["canonical"]}
    keys = args.keys or sorted(scenarios)
    client = web()
    reports = [probe(client, k, scenarios[k]) for k in keys]
    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        for r in reports:
            print(f"\n== {r['scenario']}  {r['case_id']}  gate in {r['to_gate_s']} s")
            print(f"   severity {r['severity']}, changes {r['suspected_changes']}, "
                  f"runbooks {r['runbooks']}, injection noticed: {r['injection_noticed']}")
            print(f"   plan risk {r['plan_risk']}, destructive steps {r['destructive_steps']}")
            g = r["gate"]
            print(f"   gate: auto={g['auto_approved']} approval={g['approval_required']} "
                  f"blocked={g['blocked']} roles={g['roles']}")
            for reason in g["reasons"] or []:
                print(f"     - {reason}")
            if "remediation" in r:
                print(f"   remediation {r['remediation']} ({r['steps']} steps, "
                      f"{r['after_gate_s']} s after the gate)")
            for name, ok in r["checks"].items():
                print(f"   {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(r["checks"]["gate outcome"] for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
