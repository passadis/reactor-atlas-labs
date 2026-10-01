"""Phase 10: how the orchestrator applies what the worker reports, and the dispatch guard rails."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from atlas_common.remediation import ExecutionRecord, ProgressEvent, StepResult
from atlas_orchestrator.models import IncidentCase, RemediationPlan, RemediationStep
from atlas_orchestrator.remediation import Dispatcher, RemediationListener, _iso_duration


class Cases:
    """In-memory CaseRepository: just the methods the remediation code uses."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    async def get(self, case_id: str) -> dict[str, Any] | None:
        return self.docs.get(case_id)

    async def save(self, case: IncidentCase, report: Any = None) -> None:
        doc = self.docs.setdefault(case.case_id, {"id": case.case_id})
        doc.update({"status": case.status, "state": case.model_dump(mode="json")})

    async def patch_remediation(self, case_id: str, record: dict[str, Any], *,
                                only_if_open: bool = True) -> bool:
        current = (self.docs.get(case_id) or {}).get("remediation") or {}
        if only_if_open and current.get("status") in ("succeeded", "failed", "dead_lettered"):
            return False
        self.docs.setdefault(case_id, {"id": case_id})["remediation"] = record
        return True

    async def set_fields(self, case_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self.docs.setdefault(case_id, {"id": case_id}).update(fields)
        return self.docs[case_id]


def executing_case(cases: Cases) -> IncidentCase:
    plan = RemediationPlan(plan_id="0123456789abcdef", steps=[RemediationStep(
        order=1, action="restart", target_service="svc-payments", command_ref="RB-0042#1",
        destructive=False, expected_result="ok", rollback="none")])
    case = IncidentCase(case_id="CASE-T1", report="test report text", status="executing",
                        plan=plan)
    asyncio.run(cases.save(case))
    asyncio.run(cases.set_fields("CASE-T1", {"remediation": {
        "status": "queued", "operation_id": "OP-1", "steps": []}}))
    return case


def record(status: str) -> ExecutionRecord:
    return ExecutionRecord(operation_id="OP-1", case_id="CASE-T1", plan_id="0123456789abcdef",
                           status=status, steps_total=1, detail=f"{status} detail")  # type: ignore[arg-type]


def test_a_successful_result_completes_the_case() -> None:
    cases = Cases()
    executing_case(cases)
    asyncio.run(RemediationListener(cases, None).apply_result(record("succeeded")))
    assert cases.docs["CASE-T1"]["status"] == "completed"
    assert cases.docs["CASE-T1"]["remediation"]["status"] == "succeeded"


def test_a_dead_lettered_result_parks_the_case_instead_of_failing_it() -> None:
    """Terminal `failed` would make a replay unable to complete the case."""
    cases = Cases()
    executing_case(cases)
    asyncio.run(RemediationListener(cases, None).apply_result(record("failed")))
    assert cases.docs["CASE-T1"]["status"] == "executing"
    assert cases.docs["CASE-T1"]["remediation"]["status"] == "dead_lettered"


def test_progress_for_another_operation_is_ignored() -> None:
    cases = Cases()
    executing_case(cases)
    stray = ProgressEvent(operation_id="OP-OTHER", case_id="CASE-T1", plan_id="x", steps_total=1,
                          step=StepResult(order=1, action="a", target_service="s",
                                          command_ref="RB-0042#1", status="succeeded"))
    asyncio.run(RemediationListener(cases, None).apply_progress(stray))
    assert cases.docs["CASE-T1"]["remediation"]["steps"] == []


def test_a_result_for_an_unknown_case_is_ignored() -> None:
    asyncio.run(RemediationListener(Cases(), None).apply_result(record("succeeded")))


def test_dispatch_refuses_a_case_that_is_not_approved() -> None:
    cases = Cases()
    case = executing_case(cases)
    cases.docs["CASE-T1"].pop("remediation")
    out = asyncio.run(Dispatcher(cases, None, ops=None).dispatch(case))
    assert out["status"] == "not_dispatched"


def test_the_workflow_timeout_is_the_rest_of_the_window_never_under_a_minute() -> None:
    soon = datetime.now(UTC) + timedelta(seconds=20)
    later = datetime.now(UTC) + timedelta(minutes=10)
    assert _iso_duration(soon) == "PT60S"
    assert 590 <= int(_iso_duration(later)[2:-1]) <= 600
    assert _iso_duration(None) == "PT30M"


def test_a_late_progress_event_cannot_undo_a_finished_remediation() -> None:
    """The lost update found in verification: progress racing the result (API-NOTES 17.9)."""
    cases = Cases()
    executing_case(cases)
    listener = RemediationListener(cases, None)
    asyncio.run(listener.apply_result(record("succeeded")))
    late = ProgressEvent(operation_id="OP-1", case_id="CASE-T1", plan_id="x", steps_total=1,
                         step=StepResult(order=1, action="a", target_service="s",
                                         command_ref="RB-0042#1", status="started"))
    asyncio.run(listener.apply_progress(late))
    assert cases.docs["CASE-T1"]["status"] == "completed"
    assert cases.docs["CASE-T1"]["remediation"]["status"] == "succeeded"
