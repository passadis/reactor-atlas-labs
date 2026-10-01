"""The whole loop, through the front door: incident -> plan -> approval -> remediation -> report.

    uv run python -m scripts.e2e_remediation
    uv run python -m scripts.e2e_remediation --json

Every step goes through the web app, as the signed-in presenter (Atlas.Director since Phase 10):
raise the canonical incident, wait for the policy gate to park it, approve it in the console API,
then follow the remediation the orchestrator dispatched - ticket, operation, each worker step - to
the end. Prints the wall-clock breakdown PHASE-10 asks for and the trace id that spans every hop.

Writes application data only (a case, a ticket, a remediation). Exit non-zero if the loop did not
close or took longer than the 3-minute budget.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx
from atlas_common.config import load_local_env

from scripts.sse_probe import CANONICAL, cli_token

BUDGET_S = 180.0
TERMINAL = ("succeeded", "dead_lettered", "dispatch_failed", "failed")


def web() -> httpx.Client:
    return httpx.Client(base_url=os.environ["ATLAS_WEB_URL"].rstrip("/"), timeout=180,
                        headers={"Authorization": f"Bearer {cli_token()}"})


def to_gate(client: httpx.Client, message: str, ttl: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"message": message, "mode": "graph"}
    if ttl:
        body["approval_ttl_minutes"] = ttl
    case_id = str(client.post("/api/workflow", json=body).raise_for_status().json()["case_id"])
    parked: dict[str, Any] = {}
    traceparent = ""
    event = "message"
    with client.stream("GET", f"/api/workflow/{case_id}/stream", timeout=300) as stream:
        for line in stream.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1].strip() or "{}")
                if event == "started":
                    traceparent = str((payload.get("data") or {}).get("traceparent") or "")
                elif event == "approval_required":
                    parked = payload.get("data") or {}
                    break
                elif event in ("report", "end", "error"):
                    break
    return {"case_id": case_id, "parked": parked, "traceparent": traceparent}


def follow(client: httpx.Client, case_id: str, budget_s: float = 150) -> dict[str, Any]:
    marks: dict[str, float] = {}
    started = time.perf_counter()
    doc: dict[str, Any] = {}
    while time.perf_counter() - started < budget_s:
        doc = client.get(f"/api/workflow/{case_id}").raise_for_status().json()
        rm = doc.get("remediation") or {}
        now = round(time.perf_counter() - started, 1)
        if rm.get("operation_id") and "dispatched" not in marks:
            marks["dispatched"] = now
        if any(s.get("status") == "succeeded" for s in rm.get("steps") or []) \
                and "first_step" not in marks:
            marks["first_step"] = now
        if rm.get("status") in TERMINAL:
            marks["finished"] = now
            break
        time.sleep(2)
    return {"doc": doc, "marks": marks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    load_local_env()
    client = web()
    me = client.get("/api/me").raise_for_status().json()
    persona = (me.get("persona") or {}).get("role")
    t0 = time.perf_counter()
    gate = to_gate(client, CANONICAL)
    t_gate = time.perf_counter() - t0
    case_id = gate["case_id"]
    roles = gate["parked"].get("required_approver_roles") or []
    decision = client.post(f"/api/workflow/{case_id}/decision", json={
        "decision": "approve", "comment": "Phase 10 end-to-end: approved by the presenter"})
    t_approved = time.perf_counter() - t0
    outcome = follow(client, case_id) if decision.status_code < 300 else {"doc": {}, "marks": {}}
    total = time.perf_counter() - t0
    doc = outcome["doc"]
    rm = doc.get("remediation") or {}
    result = {
        "case_id": case_id, "persona": persona, "required_roles": roles,
        "approval_http": decision.status_code,
        "approval_detail": decision.json().get("detail") if decision.status_code >= 300 else "",
        "case_status": doc.get("status"), "ticket_id": (doc.get("ticket") or {}).get("ticket_id"),
        "operation_id": rm.get("operation_id"), "remediation_status": rm.get("status"),
        "steps": f"{sum(1 for s in rm.get('steps') or [] if s.get('status') == 'succeeded')}/"
                 f"{rm.get('steps_total')}",
        "trace_id": [*gate["traceparent"].split("-"), "", ""][1],
        "timing_s": {"analysis_to_gate": round(t_gate, 1),
                     "approval_accepted": round(t_approved - t_gate, 1),
                     **{f"after_approval_{k}": v for k, v in outcome["marks"].items()},
                     "total": round(total, 1)},
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"case {case_id}  persona {persona}  needs {roles}")
        print(f"  approval        HTTP {result['approval_http']} {result['approval_detail'] or ''}")
        print(f"  ticket          {result['ticket_id']}")
        print(f"  operation       {result['operation_id']}  {result['remediation_status']} "
              f"({result['steps']} steps)")
        print(f"  case status     {result['case_status']}")
        print(f"  trace id        {result['trace_id']}")
        print("  timing (s)      " + "  ".join(f"{k}={v}" for k, v in result["timing_s"].items()))
    ok = (result["remediation_status"] == "succeeded" and result["case_status"] == "completed"
          and bool(result["ticket_id"]) and total <= BUDGET_S)
    print("CLOSED THE LOOP" if ok else "DID NOT CLOSE THE LOOP")
    return 0 if ok else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
