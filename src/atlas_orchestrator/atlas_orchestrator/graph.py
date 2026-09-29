"""The three orchestration shapes, over one set of specialists and one typed state contract.

| `ATLAS_ORCHESTRATION_MODE` | Shape |
|---|---|
| `graph` (default) | intake, triage, knowledge and impact in parallel, aggregate, plan, report |
| `sequential_only` | the same nodes in a straight chain: the degraded mode, and the A/B test |
| `handoff` | intake, a front desk routes to the specialists autonomously, aggregate, report |

The point of building all three is that the *state contract does not move*. Every shape produces the
same `IncidentCase`, because every shape writes through `executors.record()`. In handoff mode the
specialists have no `response_format` - they are mid-conversation and must be free to emit handoff
tool calls - so they write their typed result through a `record_*` tool instead. Same model, same
validation, different shape.

`HandoffBuilder` also backs `POST /cases/{case_id}/ask`, where control genuinely returns to the
user between turns. That is the shape's real job; running a whole case through it is the comparison.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Literal

from agent_framework import (
    Agent,
    CheckpointStorage,
    FunctionTool,
    Workflow,
    WorkflowBuilder,
    WorkflowViz,
    tool,
)
from agent_framework_orchestrations import HandoffBuilder
from atlas_common.models import Entitlement, Service
from pydantic import BaseModel, ConfigDict, ValidationError

from .agents import (
    COORDINATOR,
    FRONT_DESK,
    IMPACT,
    KNOWLEDGE,
    PLANNER,
    TRIAGE,
    SpecialistFactory,
)
from .approval import ApprovalExecutor, PolicyGateExecutor
from .executors import (
    AggregateExecutor,
    BranchNode,
    ChainNode,
    IntakeExecutor,
    ReportExecutor,
    record,
)
from .models import ImpactResult, IncidentCase, KnowledgeResult, RemediationPlan, TriageResult
from .node_ids import (
    AGGREGATE,
    INTAKE,
    REPORT,
    WORKFLOW_APPROVAL_TAIL,
    WORKFLOW_GRAPH,
    WORKFLOW_SEQUENTIAL,
)

log = logging.getLogger("atlas.orchestrator")

Mode = Literal["graph", "sequential_only", "handoff"]
MODES: tuple[Mode, ...] = ("graph", "sequential_only", "handoff")
HANDOFF_TURN_LIMIT = 12


def normalise_mode(value: str | None) -> Mode:
    mode = (value or "graph").strip().lower()
    for known in MODES:
        if mode == known:
            return known
    return "graph"


# ----------------------------------------------------------------- the deterministic graph --


def build_workflow(
    factory: SpecialistFactory,
    mode: Mode = "graph",
    *,
    catalogue: dict[str, Service] | None = None,
    entitlements: dict[str, Entitlement] | None = None,
    checkpoint_storage: CheckpointStorage | None = None,
) -> Workflow:
    """The `graph` and `sequential_only` shapes, both ending at the same report executor.

    Node ids come from `node_ids.py` and never vary, because the framework binds a checkpoint to
    the graph that wrote it and refuses a restore into a different topology.

    `catalogue` and `entitlements` feed the Phase 07 policy gate. Without them the gate and the
    approval node are left out entirely and the workflow runs straight from the planner to the
    report - which is what the Phase 06 tests and the topology endpoint use.
    """
    runtime = factory.runtime
    intake = IntakeExecutor(id=INTAKE, services=runtime.tools.services)
    triage = ChainNode(id=TRIAGE, agent=factory.build(TRIAGE), output_model=TriageResult,
                       dimension="triage")
    planner = ChainNode(id=PLANNER, agent=factory.build(PLANNER), output_model=RemediationPlan,
                        dimension="plan")
    aggregate = AggregateExecutor(id=AGGREGATE)
    report = ReportExecutor(id=REPORT)

    gated = catalogue is not None and entitlements is not None
    tail: list[Any] = [planner]
    if gated:
        tail.append(PolicyGateExecutor(catalogue=catalogue or {}, entitlements=entitlements or {}))
        tail.append(ApprovalExecutor(entitlements=entitlements or {}))
    tail.append(report)

    if mode == "sequential_only":
        knowledge = ChainNode(id=KNOWLEDGE, agent=factory.build(KNOWLEDGE),
                              output_model=KnowledgeResult, dimension="knowledge")
        impact = ChainNode(id=IMPACT, agent=factory.build(IMPACT), output_model=ImpactResult,
                           dimension="impact")
        head = [intake, triage, knowledge, impact, aggregate]
        builder = WorkflowBuilder(
            name=WORKFLOW_SEQUENTIAL, start_executor=intake,
            checkpoint_storage=checkpoint_storage,
            output_from=[report],
            intermediate_output_from=[*head, *tail[:-1]],
        )
        builder.add_chain([*head, *tail])
        return builder.build()

    knowledge_b = BranchNode(id=KNOWLEDGE, agent=factory.build(KNOWLEDGE),
                             output_model=KnowledgeResult, dimension="knowledge")
    impact_b = BranchNode(id=IMPACT, agent=factory.build(IMPACT), output_model=ImpactResult,
                          dimension="impact")
    builder = WorkflowBuilder(
        name=WORKFLOW_GRAPH, start_executor=intake,
        checkpoint_storage=checkpoint_storage,
        output_from=[report],
        intermediate_output_from=[intake, triage, knowledge_b, impact_b, aggregate, *tail[:-1]],
    )
    builder.add_edge(intake, triage)
    builder.add_fan_out_edges(triage, [knowledge_b, impact_b])
    builder.add_fan_in_edges([knowledge_b, impact_b], aggregate)
    builder.add_chain([aggregate, *tail])
    return builder.build()


def build_approval_tail(
    *,
    catalogue: dict[str, Service],
    entitlements: dict[str, Entitlement],
    checkpoint_storage: CheckpointStorage | None = None,
) -> Workflow:
    """`policy_gate -> approval -> report`, as a workflow of its own.

    The handoff shape builds its typed case in a conversational mesh, which has no gated graph to
    suspend in. Without this, a handoff case skipped the policy gate entirely and went straight to
    a report - caught by `verify_phase06` after Phase 07 landed. Feeding the finished case through
    the same three nodes gives the handoff shape the same authorization, the same durable
    suspension and the same cold resume as the graph.
    """
    gate = PolicyGateExecutor(catalogue=catalogue, entitlements=entitlements)
    approval = ApprovalExecutor(entitlements=entitlements)
    report = ReportExecutor(id=REPORT)
    builder = WorkflowBuilder(
        name=WORKFLOW_APPROVAL_TAIL, start_executor=gate,
        checkpoint_storage=checkpoint_storage,
        output_from=[report], intermediate_output_from=[gate, approval],
    )
    builder.add_chain([gate, approval, report])
    return builder.build()


# ------------------------------------------------------------------------ handoff shape --


class RecordTriageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: TriageResult


class RecordKnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: KnowledgeResult


class RecordImpactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: ImpactResult


class RecordPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: RemediationPlan


class CaseRecorder:
    """The typed sink for handoff mode: one `record_*` tool per specialist, bound to one case.

    Without this the handoff shape would produce a transcript and nothing else, and Episode 2 could
    not compare it against the graph. With it, a handoff run fills the same `IncidentCase` - and the
    tool schema does the same Pydantic validation the `response_format` path does.
    """

    def __init__(self, case: IncidentCase, order: Sequence[str] = ()) -> None:
        self.case = case
        self.order = list(order)
        self.recorded: list[str] = []

    def _next_step(self) -> str:
        """Deterministic routing advice, returned in the tool result the coordinator then reads.

        Left to itself the coordinator re-routes to a specialist that has already finished, because
        all it has is the prose of the last message. The recorder knows exactly what is done, so it
        says so. The model still drives the handoff; it just is not guessing about state.
        """
        remaining = [n for n in self.order if n not in self.recorded]
        if not remaining:
            return ("Every specialist has now recorded. Coordinator: summarise in two sentences "
                    "and stop - do not hand off again.")
        return f"Coordinator: hand off to {remaining[0]} next. Still outstanding: {remaining}."

    async def _write(self, raw: Any, model: type[BaseModel], by: str) -> str:
        # Write once. A coordinator that re-routes to a specialist which has already finished gets
        # a second, thinner answer - observed live, and it silently replaced a good result with an
        # empty one. The model does not get to clobber recorded state; the executor decides.
        if by in self.recorded:
            return (f"{by} has already recorded for this case and was NOT overwritten. "
                    f"{self._next_step()}")
        # The framework hands the tool its arguments as parsed JSON, so `result` arrives as a dict
        # even though the schema names a model. Validate here: this is the handoff path's
        # equivalent of `response.value` raising, and it must not fail silently.
        try:
            value = model.model_validate(raw)
        except ValidationError as exc:
            log.warning("%s rejected a malformed result: %s", by, exc)
            return (f"That did not match the {model.__name__} schema and was NOT recorded: "
                    f"{exc}. Correct it and call the tool again.")
        detail = record(self.case, value, by=by)
        self.recorded.append(by)
        return f"{detail}. Hand back to the coordinator now. {self._next_step()}"

    def tools_for(self, name: str) -> list[FunctionTool]:
        """The `record_*` tool for one specialist, or nothing for an agent that records nothing."""
        specs: dict[str, tuple[Any, type[BaseModel], str]] = {
            TRIAGE: (self.record_triage, RecordTriageInput,
                     "Record the completed triage as structured state. Call this once, last."),
            KNOWLEDGE: (self.record_knowledge, RecordKnowledgeInput,
                        "Record the retrieved runbooks and similar incidents as structured "
                        "state. Call this once, last."),
            IMPACT: (self.record_impact, RecordImpactInput,
                     "Record the impact assessment as structured state. Call this once, last."),
            PLANNER: (self.record_plan, RecordPlanInput,
                      "Record the drafted remediation plan as structured state. Call this once, "
                      "last. Recording a plan does not execute it."),
        }
        if name not in specs:
            return []
        func, schema, description = specs[name]
        return [tool(func, name=f"record_{name.removesuffix('_agent')}",
                     description=description, schema=schema)]

    async def record_triage(self, result: Any) -> str:
        return await self._write(result, TriageResult, TRIAGE)

    async def record_knowledge(self, result: Any) -> str:
        return await self._write(result, KnowledgeResult, KNOWLEDGE)

    async def record_impact(self, result: Any) -> str:
        return await self._write(result, ImpactResult, IMPACT)

    async def record_plan(self, result: Any) -> str:
        return await self._write(result, RemediationPlan, PLANNER)


HANDOFF_ADDENDUM = """

## In handoff mode

You are in a conversation with a coordinator rather than answering a single request, so the
"return the model and nothing else" rule above does not apply to your message text.

Do your analysis with your tools exactly as described, then:

1. Call `{record_tool}` once, with the completed result object as its `result` argument. That is
   what writes your finding into the case; a result you only describe in prose is lost.
2. Hand back to `coordinator_agent` with a one-line summary of what you recorded.

Do not paste the result object into your message text, and do not hand off to any other specialist.
"""


ASK_ADDENDUM = """

## In conversation

You have been handed a question from an on-call engineer by `front_desk_agent`, not a request to
produce a report. The "return the model and nothing else" rule above does not apply here: answering
with a JSON object puts raw JSON in front of the engineer.

Use your tools as usual, then reply in two or three plain sentences, naming the ids you relied on,
and hand back to `front_desk_agent`.
"""


def handoff_addendum(name: str) -> str:
    return HANDOFF_ADDENDUM.format(record_tool=f"record_{name.removesuffix('_agent')}")


def build_handoff_workflow(
    factory: SpecialistFactory,
    recorder: CaseRecorder | None = None,
    *,
    participants: Sequence[str] = (TRIAGE, KNOWLEDGE, IMPACT, PLANNER),
    autonomous: bool = True,
) -> tuple[Workflow, dict[str, Agent]]:
    """Front desk plus specialists, wired as a handoff mesh.

    With `recorder`, the hub is `coordinator_agent`, the specialists gain their `record_*` tool and
    a full case run fills typed state (`ATLAS_ORCHESTRATION_MODE=handoff`). Without it, and with
    `autonomous=False`, the hub is `front_desk_agent` and this is the interactive shape behind
    `POST /cases/{case_id}/ask`: control comes back to the user after any answer that does not hand
    off.
    """
    hub_name = COORDINATOR if recorder else FRONT_DESK
    hub = factory.build(hub_name, for_handoff=True)
    specialists = {
        name: factory.build(
            name,
            extra_tools=recorder.tools_for(name) if recorder else None,
            extra_instructions=handoff_addendum(name) if recorder else ASK_ADDENDUM,
            for_handoff=True,
        )
        for name in participants
    }
    agents = {hub_name: hub, **specialists}
    builder = HandoffBuilder(
        name="atlas-incident-handoff", participants=list(agents.values()),
        output_from=[hub], intermediate_output_from="all_other",
    ).with_start_agent(hub)
    builder.add_handoff(hub, list(specialists.values()))
    for agent in specialists.values():
        builder.add_handoff(agent, [hub])
    if autonomous:
        builder.with_autonomous_mode(turn_limits={name: HANDOFF_TURN_LIMIT for name in agents})
    if recorder is not None:
        recorder.order = list(participants)
        # Deterministic stop: the run is over when every dimension is in the case. Leaving the
        # decision to the coordinator costs a full extra lap of the mesh when it hesitates.
        expected = set(participants)
        builder.with_termination_condition(lambda _messages: expected <= set(recorder.recorded))
    return builder.build(), agents


# -------------------------------------------------------------------------- topology --


def mermaid(workflow: Workflow) -> str:
    """The live graph, from the built workflow. Episode 2 opens on this, not on a slide."""
    return WorkflowViz(workflow).to_mermaid()


def topology(workflow: Workflow, mode: Mode) -> dict[str, Any]:
    """Nodes and edges as JSON, for the web app's graph panel."""
    nodes: list[dict[str, Any]] = []
    for executor_id in sorted(workflow.executors):
        nodes.append({
            "id": executor_id,
            "kind": "agent" if executor_id.endswith("_agent") else "deterministic",
            "start": executor_id == workflow.start_executor_id,
        })
    edges: list[dict[str, str]] = []
    for group in workflow.edge_groups:
        kind = type(group).__name__.removesuffix("EdgeGroup").lower()
        if kind == "internal":  # the runner's own delivery edges, not part of the design
            continue
        for source in group.source_executor_ids:
            for target in group.target_executor_ids:
                edges.append({"from": source, "to": target, "kind": kind})
    return {"mode": mode, "name": workflow.name, "nodes": nodes, "edges": edges,
            "mermaid": mermaid(workflow)}


__all__ = [
    "HANDOFF_TURN_LIMIT",
    "MODES",
    "CaseRecorder",
    "Mode",
    "build_approval_tail",
    "build_handoff_workflow",
    "build_workflow",
    "mermaid",
    "normalise_mode",
    "topology",
]
