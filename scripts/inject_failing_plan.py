"""Stage the RB-0099 failure: an approved case whose plan has a step that fails on purpose.

    uv run python -m scripts.inject_failing_plan            # prints the operation id

The DLQ beat (PHASE-10 B) needs a plan that fails reliably, and the planner will not write one on
request - nor should it. So this builds a small, clearly labelled synthetic case directly:
three steps, step 2 on runbook RB-0099, approved by the presenter. Then it takes the REAL path:
mint the single-use token, submit through the web app's gateway probe (agent identity -> APIM ->
ops API -> Service Bus), and let the worker fail step 2 five times into the DLQ.

    make dlq-inspect                         # see it there, with the reason
    make dlq-replay OP=OP-XXXX               # --fix clears the fault, replays, case completes

Writes application data only. `make demo-reset` removes the case and re-arms the fault.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from datetime import UTC, datetime

import httpx
from atlas_common.config import Settings, load_local_env
from atlas_common.repositories.base import CosmosStore
from atlas_orchestrator.approval import ApprovalRecord
from atlas_orchestrator.models import IncidentCase, RemediationPlan, RemediationStep
from atlas_orchestrator.store import CaseRepository

from atlas_common import approval_tokens
from scripts.sse_probe import cli_token

STEPS = [
    ("Drain traffic from the unhealthy payments pool", "svc-payments", "RB-0060#1"),
    ("Recycle the payment gateway connection pool", "svc-payments", "RB-0099#1"),
    ("Confirm checkout p95 is back under SLO", "svc-checkout-api", "RB-0042#4"),
]


async def stage() -> tuple[str, str, str]:
    settings = Settings.from_env()
    case_id = f"CASE-RB0099{secrets.token_hex(3).upper()}"
    plan_id = secrets.token_hex(8)
    plan = RemediationPlan(
        plan_id=plan_id, risk="medium", estimated_minutes=2,
        justification="Synthetic plan for the retry, dead-letter and replay demonstration.",
        steps=[RemediationStep(order=i, action=a, target_service=s, command_ref=ref,
                               destructive=False, expected_result="step converges",
                               rollback="none needed") for i, (a, s, ref) in enumerate(STEPS, 1)])
    case = IncidentCase(case_id=case_id, report="Synthetic: RB-0099 retry and dead-letter demo.",
                        status="executing", services=["svc-payments", "svc-checkout-api"],
                        plan=plan, raised_by={"persona": "director", "source": "inject"},
                        approval=ApprovalRecord(approved=True, by="presenter", role="director",
                                                comment="staged for the DLQ demo",
                                                at=datetime.now(UTC), plan_id=plan_id,
                                                accepted=True))
    case.decide("inject", "synthetic-case", "staged by scripts/inject_failing_plan.py")
    async with CosmosStore(settings) as store:
        # Arm the fault: a previous replay (--fix) may have turned it off, and then the "failing"
        # plan would quietly succeed.
        await store.upsert("control", {"id": "fault-rb-0099", "enabled": True,
                                       "set_by": "scripts/inject_failing_plan.py"})
        cases = CaseRepository(store)
        await cases.save(case)
        minted = await approval_tokens.mint(store, case_id=case_id, plan_id=plan_id,
                                            approved_by="presenter", approver_role="director")
    return case_id, plan_id, minted.token


def main() -> int:
    load_local_env()
    case_id, plan_id, token = asyncio.run(stage())
    web = httpx.Client(base_url=os.environ["ATLAS_WEB_URL"].rstrip("/"), timeout=120,
                       headers={"Authorization": f"Bearer {cli_token()}"})
    r = web.post("/api/gateway/probe", json={"kind": "remediation", "case_id": case_id,
                                            "plan_id": plan_id, "approval_token": token})
    r.raise_for_status()
    body = r.json()
    submitted = body.get("body") if isinstance(body.get("body"), dict) else {}
    op = submitted.get("operation_id")
    if body.get("status") != 202 or not op:
        print(f"submission refused: HTTP {body.get('status')} {body.get('body')}")
        return 1
    asyncio.run(_record(case_id, plan_id, op))
    print(f"staged {case_id} plan {plan_id}: operation {op} queued; step 2 (RB-0099) will fail "
          "5 times and dead-letter in about a minute")
    print(op)
    return 0


async def _record(case_id: str, plan_id: str, operation_id: str) -> None:
    async with CosmosStore(Settings.from_env()) as store:
        await CaseRepository(store).set_fields(case_id, {"remediation": {
            "status": "queued", "operation_id": operation_id, "plan_id": plan_id,
            "steps_total": len(STEPS), "steps": []}})


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
