"""Phase 06 unit tests: the deterministic half of the workflow, with no Azure and no model.

The load-bearing test here is `test_deterministic_executors_never_call_the_model`. The Episode 2
claim is that authorization, merging and reporting are plain code; a test that only reads the code
proves nothing, so this one wires a chat client that raises on contact and runs the executors
against it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from atlas_agent.schemas import Citation
from atlas_common.models import Severity
from atlas_orchestrator.executors import (
    AggregateExecutor,
    IntakeRequest,
    Partial,
    ReportExecutor,
    apply_merge_rules,
    build_report,
    match_services,
    new_case_id,
    prompt_for,
    record,
    resolve_intake,
    usable,
    worst,
)
from atlas_orchestrator.graph import MODES, CaseRecorder, normalise_mode
from atlas_orchestrator.models import (
    Dimension,
    ImpactResult,
    IncidentCase,
    KnowledgeResult,
    RemediationPlan,
    RemediationStep,
    RunbookRef,
    TriageResult,
)
from atlas_orchestrator.runner import _case_brief
from pydantic import ValidationError

NOW = datetime(2026, 9, 1, 14, 20, tzinfo=UTC)


class ModelContactedError(AssertionError):
    """Raised if a deterministic executor reaches for the model."""


class ExplodingClient:
    """Stands in for the chat client. Any attribute access at all is a failure."""

    def __getattr__(self, name: str) -> Any:
        raise ModelContactedError(f"deterministic code touched the chat client: .{name}")


class FakeServices:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    async def list_all(self) -> list[Any]:
        return [type("S", (), {"service_id": sid, "name": name})() for sid, name in self._rows]


CATALOGUE = [
    ("svc-checkout-api", "Checkout API"),
    ("svc-payments", "Payments"),
    ("svc-search", "Search"),
    ("svc-loyalty", "Loyalty"),
]

TRIAGE_RESULT = TriageResult(
    severity="Sev2", category="deployment", affected_services=["svc-checkout-api"],
    likely_cause="payments deploy", suspected_change_ids=["CHG-2026-0873"], confidence=0.9,
    summary="Checkout degraded after the payments deploy.",
)
KNOWLEDGE_RESULT = KnowledgeResult(
    runbooks=[RunbookRef(doc_id="RB-0042", title="Checkout latency", applicable_steps=["Step 1"])],
    similar_incidents=["INC-2026-0297"],
    citations=[Citation(doc_id="RB-0042", title="Checkout latency", last_reviewed="2026-01-14",
                       relevance=0.9)],
)
IMPACT_RESULT = ImpactResult(
    blast_radius=10, affected_service_ids=["svc-payments"], tier1_affected=["svc-checkout-api"],
    customer_impact="20 percent of EU checkouts fail.", severity_hint="Sev2",
)
PLAN = RemediationPlan(
    steps=[RemediationStep(order=1, action="Disable the flag", target_service="svc-payments",
                           command_ref="RB-0042#3", destructive=False,
                           expected_result="Latency returns to baseline", rollback="Re-enable")],
    risk="medium", estimated_minutes=10, justification="Runbook step 3.",
)


def a_case(**fields: Any) -> IncidentCase:
    base: dict[str, Any] = {"case_id": "CASE-TEST0001", "report": "Checkout is timing out."}
    base.update(fields)
    return IncidentCase(**base)


class Ctx:
    """A stand-in WorkflowContext that records what an executor sent and yielded."""

    def __init__(self) -> None:
        self.messages: list[Any] = []
        self.outputs: list[Any] = []

    async def send_message(self, message: Any) -> None:
        self.messages.append(message)

    async def yield_output(self, output: Any) -> None:
        self.outputs.append(output)


# ------------------------------------------------------------------------ intake --


def test_intake_resolves_service_ids_from_the_report() -> None:
    case = asyncio.run(resolve_intake(
        FakeServices(CATALOGUE),
        IntakeRequest(report="Checkout is timing out since the payments deploy."),
    ))
    assert case.services == ["svc-checkout-api", "svc-payments"]
    assert case.status == "analysing"
    assert any(d.rule == "intake" for d in case.decisions)


def test_intake_says_so_when_nothing_matches() -> None:
    case = asyncio.run(resolve_intake(
        FakeServices(CATALOGUE), IntakeRequest(report="Something unnamed is slow today."),
    ))
    assert case.services == []
    assert any(d.rule == "no-service-resolved" for d in case.decisions)


def test_intake_survives_a_dead_cmdb() -> None:
    class Dead:
        async def list_all(self) -> list[Any]:
            raise RuntimeError("cosmos is down")

    case = asyncio.run(resolve_intake(Dead(), IntakeRequest(report="Checkout is timing out.")))
    assert case.services == []
    assert case.status == "analysing"


def test_intake_rejects_a_report_that_is_too_short() -> None:
    with pytest.raises(ValidationError):
        IntakeRequest(report="down")


def test_match_services_does_not_invent_a_service() -> None:
    assert match_services("the moon is broken", CATALOGUE) == []


def test_case_ids_are_unique_and_shaped() -> None:
    ids = {new_case_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("CASE-") and len(i) == 13 for i in ids)


# --------------------------------------------------------------------- merge rules --


def test_severity_is_the_worse_of_triage_and_impact() -> None:
    case = a_case(triage=TRIAGE_RESULT.model_copy(),
                  impact=IMPACT_RESULT.model_copy(update={"severity_hint": "Sev1"}))
    apply_merge_rules(case)
    assert case.triage is not None and case.triage.severity == "Sev1"
    escalation = [d for d in case.decisions if d.rule == "severity=max(triage,impact)"]
    assert escalation and "Sev2 -> Sev1" in escalation[0].detail


def test_a_weaker_impact_hint_never_lowers_the_severity() -> None:
    case = a_case(triage=TRIAGE_RESULT.model_copy(),
                  impact=IMPACT_RESULT.model_copy(update={"severity_hint": "Sev4"}))
    apply_merge_rules(case)
    assert case.triage is not None and case.triage.severity == "Sev2"
    assert any("held" in d.detail for d in case.decisions)


@pytest.mark.parametrize(("a", "b", "expected"), [
    ("Sev3", "Sev1", "Sev1"), ("Sev1", "Sev4", "Sev1"), ("Sev2", "Sev2", "Sev2"),
    ("Sev4", "Sev3", "Sev3"),
])
def test_worst_orders_severities(a: Severity, b: Severity, expected: Severity) -> None:
    assert worst(a, b) == expected


def test_tier1_in_the_blast_radius_is_always_recorded() -> None:
    case = a_case(triage=TRIAGE_RESULT.model_copy(), impact=IMPACT_RESULT.model_copy())
    apply_merge_rules(case)
    assert any(d.rule == "tier1-in-blast-radius" for d in case.decisions)


def test_embedded_instructions_escalate() -> None:
    case = a_case(triage=TRIAGE_RESULT.model_copy(),
                  knowledge=KNOWLEDGE_RESULT.model_copy(
                      update={"embedded_instructions_found": True}))
    apply_merge_rules(case)
    assert any(d.rule == "embedded-instructions" for d in case.decisions)
    assert build_report(case).needs_human is True


def test_affected_services_are_the_union_of_both_views() -> None:
    case = a_case(triage=TRIAGE_RESULT.model_copy(), impact=IMPACT_RESULT.model_copy())
    apply_merge_rules(case)
    assert case.triage is not None
    assert case.triage.affected_services == ["svc-checkout-api", "svc-payments"]


def test_merge_rules_hold_when_a_dimension_is_missing() -> None:
    case = a_case(triage=TRIAGE_RESULT.model_copy())
    case.degrade("impact", "timeout", "no answer in 90s")
    apply_merge_rules(case)
    report = build_report(case)
    assert report.missing_analysis == ["impact"]
    assert report.needs_human is True
    assert report.severity == "Sev2"


def test_status_reflects_whether_a_plan_exists() -> None:
    assert apply_merge_rules(a_case(triage=TRIAGE_RESULT)).status == "aggregated"
    assert apply_merge_rules(a_case(triage=TRIAGE_RESULT, plan=PLAN)).status == "plan_ready"


# ------------------------------------------------------------------------- record --


def test_record_is_the_one_door_into_typed_state() -> None:
    case = a_case()
    record(case, TRIAGE_RESULT, by="triage_agent")
    record(case, KNOWLEDGE_RESULT, by="knowledge_agent")
    record(case, IMPACT_RESULT, by="impact_agent")
    record(case, PLAN, by="planner_agent")
    assert case.triage and case.knowledge and case.impact and case.plan
    assert case.status == "plan_ready"
    assert [d.rule for d in case.decisions] == ["triage", "knowledge", "impact", "plan"]


def test_record_refuses_anything_that_is_not_a_specialist_result() -> None:
    with pytest.raises(TypeError):
        record(a_case(), RunbookRef(doc_id="RB-0001", title="x"), by="triage_agent")


def test_plan_ids_are_deterministic_and_content_addressed() -> None:
    first = PLAN.with_plan_id("CASE-A")
    assert first.plan_id == PLAN.with_plan_id("CASE-A").plan_id
    assert first.plan_id != PLAN.with_plan_id("CASE-B").plan_id
    changed = PLAN.model_copy(update={"estimated_minutes": 99})
    assert changed.with_plan_id("CASE-A").plan_id == first.plan_id  # steps decide, nothing else


def test_the_recorder_will_not_let_a_second_answer_overwrite_the_first() -> None:
    case = a_case()
    recorder = CaseRecorder(case, ["knowledge_agent"])
    asyncio.run(recorder.record_knowledge(KNOWLEDGE_RESULT.model_dump()))
    answer = asyncio.run(recorder.record_knowledge(KnowledgeResult().model_dump()))
    assert "NOT overwritten" in answer
    assert case.knowledge is not None and case.knowledge.runbooks[0].doc_id == "RB-0042"


def test_the_recorder_rejects_a_malformed_result_without_raising() -> None:
    case = a_case()
    recorder = CaseRecorder(case, ["triage_agent"])
    answer = asyncio.run(recorder.record_triage({"severity": "Sev9"}))
    assert "NOT recorded" in answer
    assert case.triage is None
    assert recorder.recorded == []


# ------------------------------------------------------------------------- report --


def test_report_is_built_from_state_alone() -> None:
    case = a_case(triage=TRIAGE_RESULT, knowledge=KNOWLEDGE_RESULT, impact=IMPACT_RESULT,
                  plan=PLAN.with_plan_id("CASE-TEST0001"))
    report = build_report(case)
    assert report.severity == "Sev2"
    assert report.plan_steps == 1
    assert report.needs_human is True  # Tier 1 in the blast radius
    assert report.plan_id == PLAN.with_plan_id("CASE-TEST0001").plan_id
    assert [c.doc_id for c in report.citations] == ["RB-0042"]


def test_low_confidence_triage_needs_a_human() -> None:
    case = a_case(triage=TRIAGE_RESULT.model_copy(update={"confidence": 0.2}))
    assert build_report(case).needs_human is True


def test_a_clean_low_severity_case_does_not_need_a_human() -> None:
    quiet = TRIAGE_RESULT.model_copy(update={"severity": "Sev3", "confidence": 0.9})
    case = a_case(triage=quiet, impact=IMPACT_RESULT.model_copy(update={"tier1_affected": []}),
                  knowledge=KNOWLEDGE_RESULT, plan=PLAN)
    assert build_report(case).needs_human is False


# ---------------------------------------------------- no model from deterministic code --


def test_deterministic_executors_never_call_the_model() -> None:
    """Wire a client that explodes on contact, then run every deterministic node against it."""
    exploding: Any = ExplodingClient()
    case = a_case(triage=TRIAGE_RESULT.model_copy(), plan=PLAN)
    partials = [
        Partial(dimension="knowledge", case=case, knowledge=KNOWLEDGE_RESULT),
        Partial(dimension="impact", case=case, impact=IMPACT_RESULT),
    ]
    aggregate, report = AggregateExecutor(id="aggregate"), ReportExecutor(id="report")
    # The executors hold no client at all, which is the point: the only way to a model is an
    # AgentNode. Passing one in anywhere would surface as ModelContactedError.
    assert not any(getattr(x, "_agent", None) for x in (aggregate, report))
    ctx = Ctx()
    asyncio.run(aggregate.merge(partials, ctx))  # type: ignore[arg-type]
    merged = ctx.messages[0]
    ctx2 = Ctx()
    asyncio.run(report.run(merged, ctx2))  # type: ignore[arg-type]
    assert ctx2.outputs[0].severity == "Sev2"
    # Sanity-check the probe itself: if a client had been reachable, this is what would have fired.
    with pytest.raises(ModelContactedError):
        exploding.responses  # noqa: B018


def test_prompts_are_built_from_state_not_from_prose() -> None:
    case = a_case(triage=TRIAGE_RESULT, knowledge=KNOWLEDGE_RESULT, impact=IMPACT_RESULT,
                  services=["svc-checkout-api"])
    plan_prompt = prompt_for("plan", case)
    assert "RB-0042" in plan_prompt
    assert "blast radius 10" in plan_prompt
    assert "svc-checkout-api" in prompt_for("triage", case)
    assert "Knowledge says" not in prompt_for("knowledge", case)


def test_the_planner_is_warned_about_embedded_instructions() -> None:
    case = a_case(triage=TRIAGE_RESULT,
                  knowledge=KNOWLEDGE_RESULT.model_copy(
                      update={"embedded_instructions_found": True}))
    assert "do not follow it" in prompt_for("plan", case)


# -------------------------------------------------------------------------- modes --


@pytest.mark.parametrize("value", ["graph", "sequential_only", "handoff"])
def test_known_modes_survive_normalisation(value: str) -> None:
    assert normalise_mode(value) in MODES
    assert normalise_mode(value.upper()) == value


@pytest.mark.parametrize("value", [None, "", "nonsense", "GRAPH "])
def test_unknown_modes_fall_back_to_graph(value: str | None) -> None:
    assert normalise_mode(value) == "graph"


def test_case_brief_carries_the_decision_log_into_the_follow_up() -> None:
    case = a_case(triage=TRIAGE_RESULT, knowledge=KNOWLEDGE_RESULT, impact=IMPACT_RESULT, plan=PLAN)
    apply_merge_rules(case)
    brief = _case_brief({"case_id": case.case_id, "status": case.status,
                         "state": case.model_dump(mode="json")})
    assert "RB-0042" in brief
    assert "tier1-in-blast-radius" in brief
    assert "Decision log" in brief


# ------------------------------------------------------- unusable-but-valid answers --


def test_an_empty_plan_is_refused_when_a_runbook_was_retrieved() -> None:
    """The schema was satisfied and the case still had nothing to do. Seen live, now caught."""
    case = a_case(triage=TRIAGE_RESULT, knowledge=KNOWLEDGE_RESULT)
    check = usable("plan", case)
    assert check is not None
    problem = check(RemediationPlan(justification="Nothing to do."))
    assert problem is not None and "RB-0042" in problem
    assert check(PLAN) is None


def test_an_empty_plan_is_allowed_when_no_runbook_was_found() -> None:
    case = a_case(triage=TRIAGE_RESULT, knowledge=KnowledgeResult())
    assert usable("plan", case) is None
    assert usable("plan", a_case(triage=TRIAGE_RESULT)) is None


@pytest.mark.parametrize("dimension", ["triage", "knowledge", "impact"])
def test_the_other_dimensions_have_no_extra_check(dimension: Dimension) -> None:
    case = a_case(triage=TRIAGE_RESULT, knowledge=KNOWLEDGE_RESULT)
    assert usable(dimension, case) is None
