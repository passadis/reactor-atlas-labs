"""Running a case: one workflow per mode, one event stream per run.

The event stream is deliberately complete. CLAUDE.md section 8 makes demo scope a *view* filter, so
the server emits every event for every run and the web app decides what to reveal. Filtering here
would mean the Episode 1 encore could not show Episode 2's graph for a run that already happened.

Replica handoff: `POST /cases` writes a pending case to Cosmos and whichever replica serves the
stream runs it. A run is owned by exactly one replica - the one holding the queue - and the case
document is the only thing that crosses.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from agent_framework import Workflow
from atlas_agent.factory import IncidentAgentRuntime
from atlas_common.telemetry import continued_from, current_traceparent, span
from pydantic import BaseModel

from .agents import FRONT_DESK, IMPACT, KNOWLEDGE, PLANNER, TRIAGE, SpecialistFactory
from .executors import (
    AgentNode,
    IntakeRequest,
    NodeProgress,
    apply_merge_rules,
    build_report,
    new_case_id,
    prompt_for,
    record,
    resolve_intake,
    usable,
)
from .graph import (
    CaseRecorder,
    Mode,
    build_handoff_workflow,
    build_workflow,
    normalise_mode,
    topology,
)
from .models import (
    Dimension,
    ImpactResult,
    IncidentCase,
    IncidentReport,
    KnowledgeResult,
    RemediationPlan,
    TriageResult,
)
from .store import CaseRepository

log = logging.getLogger("atlas.orchestrator")

# The orchestrator makes four agent calls per case, each with its own tool budget, so it needs more
# headroom than the single Episode 1 agent. Per process, not shared with the web app.
ORCHESTRATOR_INVOCATION_LIMITS: dict[str, Any] = {
    "max_iterations": 12,
    "max_function_calls": 32,
    "max_duration_seconds": 120.0,
}
EVENT_BUFFER = 400
RUN_TIMEOUT_S = 420.0

# Fixed order, because the planner needs the other three. Used only to fill gaps the handoff mesh
# left behind; the graph shapes get their order from the edges.
SPECIALIST_ORDER: tuple[tuple[str, Dimension, type[BaseModel]], ...] = (
    (TRIAGE, "triage", TriageResult),
    (KNOWLEDGE, "knowledge", KnowledgeResult),
    (IMPACT, "impact", ImpactResult),
    (PLANNER, "plan", RemediationPlan),
)


class CaseEvent(BaseModel):
    """One line of the SSE stream. `kind` is what the client filters on."""

    kind: str
    case_id: str
    at: float
    node: str | None = None
    data: dict[str, Any] = {}


class Run:
    """One in-flight case on this replica: a queue, a replay buffer and the task driving it."""

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self.started = time.perf_counter()
        self.buffer: list[CaseEvent] = []
        self.subscribers: list[asyncio.Queue[CaseEvent | None]] = []
        self.done = False
        self.case: IncidentCase | None = None
        self.report: IncidentReport | None = None
        self.task: asyncio.Task[None] | None = None

    def publish(self, event: CaseEvent) -> None:
        self.buffer.append(event)
        del self.buffer[:-EVENT_BUFFER]
        for queue in self.subscribers:
            queue.put_nowait(event)

    def finish(self) -> None:
        self.done = True
        for queue in self.subscribers:
            queue.put_nowait(None)

    async def subscribe(self) -> AsyncIterator[CaseEvent]:
        queue: asyncio.Queue[CaseEvent | None] = asyncio.Queue()
        replay = list(self.buffer)
        self.subscribers.append(queue)
        try:
            for event in replay:
                yield event
            if self.done:
                return
            while True:
                nxt: CaseEvent | None = await queue.get()
                if nxt is None:
                    return
                if nxt not in replay:
                    yield nxt
        finally:
            with contextlib.suppress(ValueError):
                self.subscribers.remove(queue)


class CaseRunner:
    """Owns the runtime, the per-mode workflows and the live runs."""

    def __init__(self, runtime: IncidentAgentRuntime, default_mode: str = "graph") -> None:
        self.runtime = runtime
        runtime.client.function_invocation_configuration.update(ORCHESTRATOR_INVOCATION_LIMITS)
        self.factory = SpecialistFactory(runtime)
        self.default_mode: Mode = normalise_mode(default_mode)
        self.cases = CaseRepository(runtime.store)
        self._workflows: dict[Mode, Workflow] = {}
        self._runs: dict[str, Run] = {}

    def _event(self, run: Run, kind: str, *, node: str | None = None,
               data: dict[str, Any] | None = None) -> CaseEvent:
        return CaseEvent(kind=kind, case_id=run.case_id,
                         at=round(time.perf_counter() - run.started, 2), node=node,
                         data=data or {})

    # ----------------------------------------------------------------------- topology --

    def workflow(self, mode: Mode) -> Workflow:
        """Built once per mode and reused: agents are stateless, the graph is not per-request."""
        if mode not in self._workflows:
            self._workflows[mode] = build_workflow(self.factory, mode)
        return self._workflows[mode]

    def graph(self, mode: str | None = None) -> dict[str, Any]:
        resolved = normalise_mode(mode or self.default_mode)
        if resolved == "handoff":
            workflow, _agents = build_handoff_workflow(self.factory, CaseRecorder(_blank_case()))
            return topology(workflow, resolved)
        return topology(self.workflow(resolved), resolved)

    # --------------------------------------------------------------------------- runs --

    async def create(self, report: str, *, mode: str | None = None,
                     traceparent: str | None = None,
                     raised_by: dict[str, str] | None = None) -> dict[str, Any]:
        """Register a case. The run itself starts when someone opens the stream."""
        case_id = new_case_id()
        resolved = normalise_mode(mode or self.default_mode)
        return await self.cases.create_pending(
            case_id, report, resolved, traceparent=traceparent, raised_by=raised_by,
        )

    async def get(self, case_id: str) -> dict[str, Any] | None:
        run = self._runs.get(case_id)
        if run is not None and run.case is not None:
            doc: dict[str, Any] = {
                "id": case_id, "case_id": case_id, "status": run.case.status,
                "mode": run.case.mode, "state": run.case.model_dump(mode="json"),
                "live": not run.done,
            }
            if run.report is not None:
                doc["result"] = run.report.model_dump(mode="json", exclude={"case"})
            return doc
        return await self.cases.get(case_id)

    async def stream(self, case_id: str) -> AsyncIterator[CaseEvent]:
        """Subscribe to a case, starting the run if this replica has not started it yet."""
        run = self._runs.get(case_id)
        if run is None:
            doc = await self.cases.get(case_id)
            if doc is None:
                raise KeyError(case_id)
            if doc.get("status") not in (None, "pending"):
                # It ran on another replica, or earlier. Replay the outcome, not the events.
                yield CaseEvent(kind="restored", case_id=case_id, at=0.0, data=doc)
                return
            run = self._start(case_id, doc)
        async for event in run.subscribe():
            yield event

    def _start(self, case_id: str, doc: dict[str, Any]) -> Run:
        run = Run(case_id)
        self._runs[case_id] = run
        request = IntakeRequest(
            report=str(doc["report"]), case_id=case_id, mode=str(doc.get("mode") or "graph"),
            traceparent=doc.get("traceparent"),
            raised_by=dict(doc.get("raised_by") or {}),
        )
        run.task = asyncio.create_task(self._drive(run, request))
        return run

    async def _drive(self, run: Run, request: IntakeRequest) -> None:
        try:
            async with asyncio.timeout(RUN_TIMEOUT_S):
                await self._execute(run, request)
        except TimeoutError:
            run.publish(self._event(run, "error", data={"message": "the run exceeded its budget",
                                                        "seconds": RUN_TIMEOUT_S}))
        except Exception as exc:
            log.exception("case %s failed", run.case_id)
            run.publish(self._event(run, "error",
                                    data={"message": f"{type(exc).__name__}: {exc}"[:400]}))
        finally:
            run.finish()

    async def _execute(self, run: Run, request: IntakeRequest) -> None:
        mode = normalise_mode(request.mode)
        with continued_from({"traceparent": request.traceparent} if request.traceparent else None):
            with span("atlas.case", case_id=run.case_id, mode=mode) as case_span:
                run.publish(self._event(run, "started", data={
                    "mode": mode, "report": request.report,
                    "traceparent": current_traceparent(),
                }))
                if mode == "handoff":
                    await self._run_handoff(run, request)
                else:
                    await self._run_graph(run, request, mode)
                if run.report is not None:
                    case_span.set_attribute("atlas.case.severity", run.report.severity)
                    case_span.set_attribute("atlas.case.needs_human", run.report.needs_human)

    async def _run_graph(self, run: Run, request: IntakeRequest, mode: Mode) -> None:
        workflow = self.workflow(mode)
        async for event in workflow.run(request, stream=True):
            payload = event.data
            if event.type == "intermediate" and isinstance(payload, NodeProgress):
                run.publish(self._event(run, "node", node=payload.node,
                                        data=payload.model_dump(mode="json")))
            elif event.type == "output" and isinstance(payload, IncidentReport):
                run.case, run.report = payload.case, payload
                run.publish(self._event(run, "report", data=payload.model_dump(
                    mode="json", exclude={"case"})))
            elif event.type in ("executor_invoked", "executor_completed"):
                run.publish(self._event(run, event.type, node=event.executor_id))
            elif event.type == "status":
                run.publish(self._event(run, "status", data={"state": str(event.state)}))
        if run.case is not None:
            await self.cases.save(run.case, run.report)

    async def _run_handoff(self, run: Run, request: IntakeRequest) -> None:
        """The handoff shape as a case run: the deterministic bookends, an agent-routed middle."""
        case = await resolve_intake(self.runtime.tools.services, request)
        run.case = case
        run.publish(self._event(run, "node", node="intake", data=NodeProgress(
            node="intake", case_id=case.case_id, seconds=0.0, ok=True,
            detail=f"services {case.services}").model_dump(mode="json")))
        recorder = CaseRecorder(case)
        workflow, _agents = build_handoff_workflow(self.factory, recorder)
        seen: set[str] = set()
        async for event in workflow.run(
            f"New incident to analyse. Report: {case.report}", stream=True,
        ):
            if event.type in ("executor_invoked", "executor_completed"):
                run.publish(self._event(run, event.type, node=event.executor_id))
            for name in list(recorder.recorded):
                if name not in seen:
                    seen.add(name)
                    run.publish(self._event(run, "node", node=name, data=NodeProgress(
                        node=name, case_id=case.case_id, seconds=round(
                            time.perf_counter() - run.started, 2),
                        ok=True, detail="recorded through the handoff mesh",
                    ).model_dump(mode="json")))
        await self._fill_gaps(run, case, recorder.recorded)
        apply_merge_rules(case)
        run.publish(self._event(run, "node", node="aggregate", data=NodeProgress(
            node="aggregate", case_id=case.case_id, seconds=0.0, ok=True,
            detail=f"{len(case.decisions)} decisions").model_dump(mode="json")))
        run.report = build_report(case)
        run.publish(self._event(run, "report",
                                data=run.report.model_dump(mode="json", exclude={"case"})))
        await self.cases.save(case, run.report)

    async def _fill_gaps(self, run: Run, case: IncidentCase, recorded: list[str]) -> None:
        """Run any specialist the handoff mesh never got to, directly.

        The mesh is the least predictable of the three shapes: a coordinator can burn its turn
        limit before every specialist has reported, and it did - a live run finished with triage
        and knowledge recorded and no impact analysis at all. Rather than ship a half-analysed
        case, deterministic code finishes the job and writes down that it had to. The agentic part
        is best effort; the state contract is not.
        """
        for name, dimension, model in SPECIALIST_ORDER:
            if name in recorded:
                continue
            node = AgentNode(id=name, agent=self.factory.build(name), output_model=model,
                             dimension=dimension)
            value, reason, detail, calls, seconds = await node.invoke(
                prompt_for(dimension, case), usable(dimension, case))
            if value is None:
                case.degrade(dimension, reason or "error", detail)
                case.decide(name, "degrade", f"{dimension}: {reason} ({detail})")
            else:
                record(case, value, by=name)
                case.decide(name, "handoff-gap-filled",
                            f"the handoff never recorded {dimension}; ran the specialist directly")
            run.publish(self._event(run, "node", node=name, data=NodeProgress(
                node=name, case_id=case.case_id, seconds=seconds, ok=value is not None,
                detail=detail or "filled in after the handoff", tool_calls=calls,
            ).model_dump(mode="json")))

    async def run_sync(self, report: str, *, mode: str | None = None,
                       traceparent: str | None = None) -> IncidentReport:
        """Run a case to completion in one request. Used by tests, verification and `?wait=true`."""
        doc = await self.create(report, mode=mode, traceparent=traceparent)
        async for _event in self.stream(str(doc["case_id"])):
            pass
        run = self._runs[str(doc["case_id"])]
        # The subscriber is released by `finish()` inside the driver's `finally`, so the task can
        # still be finalising here. Returning now would let the caller close the runtime out from
        # under an in-flight connection, which surfaces later as "Event loop is closed".
        if run.task is not None:
            await run.task
        if run.report is None:
            raise RuntimeError(f"case {doc['case_id']} produced no report")
        return run.report

    async def close(self) -> None:
        """Stop driving any case still in flight. Called on shutdown, before the clients close."""
        tasks = [r.task for r in self._runs.values() if r.task is not None and not r.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task

    # ---------------------------------------------------------------------- follow-up --

    async def ask(self, case_id: str, question: str) -> dict[str, Any]:
        """One handoff turn on an analysed case: the front desk answers or routes."""
        doc = await self.get(case_id)
        if doc is None:
            raise KeyError(case_id)
        workflow, _agents = build_handoff_workflow(
            self.factory, None, participants=(KNOWLEDGE, IMPACT), autonomous=False,
        )
        prompt = f"{_case_brief(doc)}\n\nThe engineer asks: {question}"
        said: dict[str, str] = {}
        routed: list[str] = []
        with span("atlas.ask", case_id=case_id):
            async for event in workflow.run(prompt, stream=True):
                if event.type not in ("output", "intermediate"):
                    continue
                who = event.executor_id or "unknown"
                said[who] = said.get(who, "") + (getattr(event.data, "text", "") or "")
                for content in getattr(event.data, "contents", None) or []:
                    # Every participant gets an `executor_invoked` when the mesh fans a message
                    # out, so only a real handoff tool call proves a specialist was consulted.
                    name = getattr(content, "name", "") or ""
                    if content.type == "function_call" and name.startswith("handoff_to_"):
                        target = name.removeprefix("handoff_to_")
                        if target != FRONT_DESK:
                            routed.append(target)
        # Non-autonomous handoff returns control to the user as soon as an agent answers without
        # handing on, so the last word is often the specialist's, not the front desk's. Either is
        # the answer to this turn; preferring the front desk only when it actually spoke.
        notes = {k: v.strip() for k, v in said.items() if k != FRONT_DESK and v.strip()}
        answer = said.get(FRONT_DESK, "").strip() or (list(notes.values())[-1] if notes else "")
        return {"case_id": case_id, "answer": answer, "handoffs": sorted(set(routed)),
                "answered_by": FRONT_DESK if said.get(FRONT_DESK, "").strip()
                else (list(notes)[-1] if notes else None),
                "specialist_notes": notes}


def _case_brief(doc: dict[str, Any]) -> str:
    """The case, flattened for the front desk. Built from stored state, never from a model call."""
    state = doc.get("state") or {}
    result = doc.get("result") or {}
    lines = [f"Case {doc.get('case_id')} (status {doc.get('status')}).",
             f"Report: {state.get('report') or doc.get('report')}"]
    if result:
        lines.append(f"Severity {result.get('severity')}. {result.get('summary', '')}")
    triage = state.get("triage") or {}
    if triage:
        lines.append(f"Triage: {triage.get('severity')} {triage.get('category')}, affected "
                     f"{triage.get('affected_services')}, suspected change "
                     f"{triage.get('suspected_change_ids')}. {triage.get('likely_cause', '')}")
    knowledge = state.get("knowledge") or {}
    if knowledge:
        books = [f"{r.get('doc_id')} {r.get('title')}" for r in knowledge.get("runbooks", [])]
        lines.append(f"Knowledge: runbooks {books}, similar incidents "
                     f"{knowledge.get('similar_incidents')}")
    impact = state.get("impact") or {}
    if impact:
        lines.append(f"Impact: blast radius {impact.get('blast_radius')}, Tier 1 "
                     f"{impact.get('tier1_affected')}. {impact.get('customer_impact', '')}")
    plan = state.get("plan") or {}
    if plan:
        steps = [f"{s.get('order')}. {s.get('action')}" for s in plan.get("steps", [])]
        lines.append(f"Draft plan {plan.get('plan_id')} (risk {plan.get('risk')}): {steps}")
    decisions = state.get("decisions") or []
    if decisions:
        lines.append("Decision log: " + "; ".join(
            f"{d.get('by')} {d.get('rule')}: {d.get('detail')}" for d in decisions))
    return "\n".join(lines)


def _blank_case() -> IncidentCase:
    return IncidentCase(case_id="CASE-TOPOLOGY", report="topology only")


__all__ = [
    "EVENT_BUFFER",
    "ORCHESTRATOR_INVOCATION_LIMITS",
    "RUN_TIMEOUT_S",
    "CaseEvent",
    "CaseRunner",
    "Run",
]
