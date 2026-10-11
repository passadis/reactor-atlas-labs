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

from agent_framework import CheckpointStorage, Workflow
from atlas_agent.factory import IncidentAgentRuntime
from atlas_common.models import Entitlement, Service
from atlas_common.repositories.cosmos_repos import EntitlementRepository, ServiceRepository
from atlas_common.telemetry import bound, continued_from, current_traceparent, span
from pydantic import BaseModel

from .agents import FRONT_DESK, IMPACT, KNOWLEDGE, PLANNER, TRIAGE, SpecialistFactory
from .approval import ApprovalExecutor, ApprovalResponse
from .checkpoints import build_storage
from .executors import (
    ChainNode,
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
    build_approval_tail,
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
from .node_ids import (
    APPROVAL,
    WORKFLOW_APPROVAL_TAIL,
    WORKFLOW_GRAPH,
    WORKFLOW_SEQUENTIAL,
)
from .remediation import ApprovalWorkflow, Dispatcher
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
# How long a live SSE stream waits on a human before letting go. The case is NOT lost at this
# point - it stays `awaiting_approval` in Cosmos with its checkpoint, and approving later resumes
# it cold. This only bounds how long one browser connection is held open.
APPROVAL_WAIT_S = 300.0
# While a case waits on a human, how often its holding replica checks whether ANOTHER replica has
# already answered it (the cold path). Without this, the console - streaming from the holding
# replica - kept saying "waiting on a human" for up to APPROVAL_WAIT_S after the case was decided
# elsewhere (2026-10-11, API-NOTES 23.3).
RESOLVED_ELSEWHERE_POLL_S = 5.0
WAITING_STATUSES = (None, "pending", "awaiting_approval")

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
        # Set while the workflow is parked on a human. `answers` is how an approval arriving on
        # this replica rejoins the live stream instead of starting a cold resume.
        self.request_id: str | None = None
        self.checkpoint_id: str | None = None
        self.answers: asyncio.Queue[ApprovalResponse] = asyncio.Queue()

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

    def __init__(self, runtime: IncidentAgentRuntime, default_mode: str = "graph",
                 *, checkpoints: bool = True) -> None:
        self.runtime = runtime
        runtime.client.function_invocation_configuration.update(ORCHESTRATOR_INVOCATION_LIMITS)
        self.factory = SpecialistFactory(runtime)
        self.default_mode: Mode = normalise_mode(default_mode)
        self.cases = CaseRepository(runtime.store)
        self.checkpoints: CheckpointStorage | None = (
            build_storage(runtime.settings) if checkpoints else None)
        # The policy gate needs both tables. Loaded once at startup, not per case: they are
        # reference data, and a gate that re-reads Cosmos per decision is a gate that can time out.
        self.catalogue: dict[str, Service] = {}
        self.entitlements: dict[str, Entitlement] = {}
        self._workflows: dict[Mode, Workflow] = {}
        self._runs: dict[str, Run] = {}
        # Phase 10: after the human. See remediation.py.
        self.dispatcher = Dispatcher(self.cases, runtime.store, runtime.ops)
        self.approval_workflow = ApprovalWorkflow(
            self.cases, runtime.store, runtime.workflows, runtime.settings.console_url)
        self._tasks: set[asyncio.Task[Any]] = set()

    def _background(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._background_done)

    def _background_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        # A failed background task must say so: unretrieved exceptions otherwise vanish.
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed: %r", task.exception())

    async def _after_decision(self, run: Run) -> None:
        """Once a human (or the policy) has decided: close the Logic App's wait, then dispatch.

        Deterministic, idempotent and never fatal to the case: every outcome is recorded on the
        case document, and a failed dispatch can be retried with POST /cases/{id}/dispatch.
        """
        case = run.case
        if case is None:
            return
        if case.approval is not None and case.status in ("approved", "rejected"):
            decision = {"case_id": case.case_id, "approved": case.approval.approved,
                        "by": case.approval.by, "role": case.approval.role,
                        "comment": case.approval.comment, "status": case.status}
            # Background: the run may not have registered its callback yet, and dispatch must
            # not wait on a notification workflow.
            self._background(self.approval_workflow.complete_eventually(case.case_id, decision))
        if case.status == "approved":
            try:
                # Always in the CASE's trace, whichever replica and whichever path (warm, or cold
                # from a checkpoint inside the approval request) gets here. Otherwise a cold
                # resume dispatched under the approval request's trace, and the remediation hung
                # off a different waterfall from the incident (API-NOTES 17.8).
                carrier = {"traceparent": case.traceparent} if case.traceparent else None
                with (
                    continued_from(carrier),
                    bound(case_id=case.case_id,
                          plan_id=case.plan.plan_id if case.plan else None, node="dispatch"),
                    span("atlas.dispatch", case_id=case.case_id),
                ):
                    record = await self.dispatcher.dispatch(case)
            except Exception as exc:
                log.exception("dispatch of %s failed", case.case_id)
                record = {"status": "dispatch_failed", "detail": type(exc).__name__}
            run.publish(self._event(run, "dispatched", data=record))

    async def load_reference_data(self) -> str:
        """Read the catalogue and entitlements. Until this runs, the gate is not wired in."""
        services = await ServiceRepository(self.runtime.store).list_all()
        entitlements = await EntitlementRepository(self.runtime.store).list_all()
        self.catalogue = {s.service_id: s for s in services}
        self.entitlements = {e.role: e for e in entitlements}
        self._workflows.clear()  # rebuild with the gate attached
        return (f"{len(self.catalogue)} services "
                f"({sum(1 for s in self.catalogue.values() if s.change_freeze)} frozen), "
                f"{len(self.entitlements)} entitlement roles")

    @property
    def gated(self) -> bool:
        return bool(self.catalogue and self.entitlements)

    def _event(self, run: Run, kind: str, *, node: str | None = None,
               data: dict[str, Any] | None = None) -> CaseEvent:
        return CaseEvent(kind=kind, case_id=run.case_id,
                         at=round(time.perf_counter() - run.started, 2), node=node,
                         data=data or {})

    # ----------------------------------------------------------------------- topology --

    def workflow(self, mode: Mode) -> Workflow:
        """One cached instance per mode, for the TOPOLOGY only (`GET /graph`). Never run it.

        Runs and resumes each take `new_workflow(mode)`: Agent Framework refuses two concurrent
        runs on one instance ("Workflow is already running"), which failed a cold resume that
        landed on a replica busy with another case (2026-10-06, API-NOTES 22.5).
        """
        if mode not in self._workflows:
            self._workflows[mode] = self.new_workflow(mode)
        return self._workflows[mode]

    def new_workflow(self, mode: Mode) -> Workflow:
        """A fresh instance for one run. Checkpoints stay compatible: the graph is built from the
        fixed ids in `node_ids.py`, so `graph_signature_hash` is the same for every build
        (tests/test_checkpoints.py::test_the_graph_signature_is_stable_across_builds)."""
        if mode == "handoff":
            # Handoff's typed case comes out of a conversational mesh; the durable, gated part of
            # its run is the approval tail. That is what a resume restores into.
            return build_approval_tail(
                catalogue=self.catalogue, entitlements=self.entitlements,
                checkpoint_storage=self.checkpoints)
        return build_workflow(
            self.factory, mode,
            catalogue=self.catalogue or None, entitlements=self.entitlements or None,
            checkpoint_storage=self.checkpoints,
        )

    def workflow_name(self, mode: Mode) -> str:
        if mode == "handoff":
            return WORKFLOW_APPROVAL_TAIL
        return WORKFLOW_SEQUENTIAL if mode == "sequential_only" else WORKFLOW_GRAPH

    def graph(self, mode: str | None = None) -> dict[str, Any]:
        resolved = normalise_mode(mode or self.default_mode)
        if resolved == "handoff":
            workflow, _agents = build_handoff_workflow(self.factory, CaseRecorder(_blank_case()))
            return topology(workflow, resolved)
        return topology(self.workflow(resolved), resolved)

    # --------------------------------------------------------------------------- runs --

    async def create(self, report: str, *, mode: str | None = None,
                     traceparent: str | None = None,
                     raised_by: dict[str, str] | None = None,
                     approval_ttl_minutes: int | None = None) -> dict[str, Any]:
        """Register a case. The run itself starts when someone opens the stream."""
        case_id = new_case_id()
        resolved = normalise_mode(mode or self.default_mode)
        return await self.cases.create_pending(
            case_id, report, resolved, traceparent=traceparent, raised_by=raised_by,
            approval_ttl_minutes=approval_ttl_minutes,
        )

    async def get(self, case_id: str) -> dict[str, Any] | None:
        """The case as it stands. Memory only while the run is live, Cosmos otherwise.

        A finished run's cached case goes stale the moment anything else moves the case on - an
        operator running `scripts/resume_case.py`, or another replica answering the approval. The
        cache is an optimisation for a run in flight, never the source of truth.
        """
        run = self._runs.get(case_id)
        if run is not None and run.case is not None and not run.done:
            if run.case.status == "awaiting_approval":
                # Another replica may have decided it (the cold path): Cosmos is the truth.
                stored = await self.cases.get(case_id)
                if stored is not None and stored.get("status") not in WAITING_STATUSES:
                    return stored
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
            approval_ttl_minutes=doc.get("approval_ttl_minutes"),
        )
        run.task = asyncio.create_task(self._drive(run, request))
        return run

    async def _drive(self, run: Run, request: IntakeRequest) -> None:
        # Every run builds its workflow from the shared tool catalogue; this refreshes the
        # catalogue's descriptions if APIM changed one (atlas_common.tool_catalog).
        await self.runtime.refresh_tool_catalog()
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
            with (
                bound(case_id=run.case_id, mode=mode),
                span("atlas.case", case_id=run.case_id, mode=mode) as case_span,
            ):
                run.publish(self._event(run, "started", data={
                    "mode": mode, "report": request.report,
                    "traceparent": current_traceparent(),
                }))
                if mode == "handoff":
                    await self._run_handoff(run, request)
                else:
                    await self._run_graph(run, request, mode)
                # Inside the case span on purpose: the dispatch's calls through APIM, and the
                # Service Bus message after them, stay in this trace - one waterfall to the worker.
                await self._after_decision(run)
                if run.report is not None:
                    case_span.set_attribute("atlas.case.severity", run.report.severity)
                    case_span.set_attribute("atlas.case.needs_human", run.report.needs_human)

    async def _pump(self, run: Run, stream: Any) -> bool:
        """Relay one workflow run into the case's event stream. True if it parked on a human."""
        suspended = False
        async for event in stream:
            payload = event.data
            if event.type == "intermediate" and isinstance(payload, NodeProgress):
                run.publish(self._event(run, "node", node=payload.node,
                                        data=payload.model_dump(mode="json")))
            elif event.type == "intermediate" and isinstance(payload, IncidentCase):
                # The gate nodes hand the case out so it can be persisted mid-flight; a suspended
                # workflow never produces a report, so this is the only place it comes from.
                run.case = payload
                run.publish(self._event(run, "state", node=event.executor_id,
                                        data={"status": payload.status}))
            elif event.type == "output" and isinstance(payload, IncidentReport):
                run.case, run.report = payload.case, payload
                run.publish(self._event(run, "report", data=payload.model_dump(
                    mode="json", exclude={"case"})))
            elif event.type == "request_info":
                suspended = True
                run.request_id = event.request_id
                await self._persist_pending_approval(run)
                if run.case is not None:
                    # The Logic App collects and notifies; it does not decide. Not awaited: a slow
                    # or dead workflow must never hold up the case (the console works without it).
                    self._background(self.approval_workflow.start(run.case))
                run.publish(self._event(run, "approval_required", node=APPROVAL,
                                        data=payload.model_dump(mode="json")))
            elif event.type in ("executor_invoked", "executor_completed"):
                run.publish(self._event(run, event.type, node=event.executor_id))
            elif event.type == "status":
                run.publish(self._event(run, "status", data={"state": str(event.state)}))
        return suspended

    async def _run_graph(self, run: Run, request: IntakeRequest, mode: Mode) -> None:
        """Run to completion, or to the approval gate and then wait for a human on this stream.

        The suspension ends the workflow run: `request_info` parks the case and the stream goes
        idle. Rather than close the browser's stream there, the case waits here for an answer so
        the demo is one continuous timeline. Nothing is lost if it gives up - the checkpoint is
        already written, and approving later resumes the case cold (`resume_from_checkpoint`).
        """
        workflow = self.new_workflow(mode)
        suspended = await self._pump(run, workflow.run(request, stream=True))
        await self._await_human(run, workflow, suspended)

    async def _await_human(self, run: Run, workflow: Workflow, suspended: bool) -> None:
        """Hold the stream while the case waits on a human, then finish it. Every shape."""
        while suspended:
            if run.case is None:
                run.case = await self._case_from_store(run.case_id)
            answer = await self._wait_for_answer(run)
            if answer is None:
                return
            # No `checkpoint_id` here, deliberately. This workflow object is still holding the
            # pending request, and restoring a checkpoint over it overwrites that cycle:
            #   "received a checkpoint restore while 1 request_info event(s) are still pending
            #    ... those pending requests will be overwritten by the checkpoint's state"
            # Restoring belongs to the cold path, where the object is new.
            run.publish(self._event(run, "resuming", node=APPROVAL, data={
                "by": answer.by, "role": answer.role, "approved": answer.approved,
                "path": "warm"}))
            suspended = await self._pump(run, workflow.run(
                responses={run.request_id: answer} if run.request_id else None, stream=True))

        if run.case is not None:
            await self.cases.save(run.case, run.report)
            await self.cases.close_approval(run.case)

    async def _wait_for_answer(self, run: Run) -> ApprovalResponse | None:
        """An answer for this replica's live run - or None: decided elsewhere, or the wait ended.

        Never saves anything on the way out: the replica that decided the case owns its state.
        """
        deadline = time.monotonic() + APPROVAL_WAIT_S
        while True:
            try:
                return await asyncio.wait_for(run.answers.get(), RESOLVED_ELSEWHERE_POLL_S)
            except TimeoutError:
                pass
            stored = await self.cases.get(run.case_id)
            if stored is not None and stored.get("status") not in WAITING_STATUSES:
                run.publish(self._event(run, "restored", node=APPROVAL, data=stored))
                return None
            if time.monotonic() >= deadline:
                run.publish(self._event(run, "awaiting_approval", node=APPROVAL, data={
                    "message": "still waiting on a human; the case is checkpointed and resumable",
                    "waited_s": APPROVAL_WAIT_S}))
                return None

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

        if not self.gated:
            run.report = build_report(case)
            run.publish(self._event(run, "report",
                                    data=run.report.model_dump(mode="json", exclude={"case"})))
            await self.cases.save(case, run.report)
            return

        # Same gate, same suspension, same cold resume as the graph. Building the report by hand
        # here - as the Phase 06 handoff path did - skipped the policy gate entirely.
        tail = self.new_workflow("handoff")
        suspended = await self._pump(run, tail.run(case, stream=True))
        await self._await_human(run, tail, suspended)

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
            # ChainNode, not the AgentNode base: the framework refuses to construct an executor
            # with no @handler, and this path only runs when the mesh missed a specialist - so the
            # bug hid until a live run needed it (docs/API-NOTES.md 14.11).
            node = ChainNode(id=name, agent=self.factory.build(name), output_model=model,
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
        # CosmosCheckpointStorage owns its Cosmos client when built from an endpoint, so it has to
        # be closed or the process exits complaining about an unclosed session.
        closer = getattr(self.checkpoints, "close", None)
        if closer is not None:
            with contextlib.suppress(Exception):
                await closer()

    # ---------------------------------------------------------------------- approvals --

    async def _case_from_store(self, case_id: str) -> IncidentCase | None:
        doc = await self.cases.get(case_id)
        state = (doc or {}).get("state")
        return IncidentCase.model_validate(state) if state else None

    async def _persist_pending_approval(self, run: Run) -> None:
        """Make a parked case fully durable: the case state, then the approval row.

        Both are needed, and forgetting the first cost a verification cycle. With two replicas, the
        case parks on replica A while the approval arrives at replica B - which has nothing in
        memory and reads Cosmos. If only the approval row were written, B would find the original
        `pending` document with no state, no policy decision, and would answer
        "case X has no policy decision to answer" instead of validating the approver.

        The workflow's checkpoint remains the source of truth for *resuming*; these two documents
        are the source of truth for *what is being asked, and of whom*. Phase 10's Logic App reads
        the same approval row.
        """
        if run.case is None:
            return
        await self.cases.save(run.case)
        await self.cases.save_approval(run.case, request_id=run.request_id or "")

    async def latest_checkpoint_id(self, mode: Mode, *,
                                   request_id: str | None = None) -> str | None:
        """The newest checkpoint for a mode, or the newest one holding a SPECIFIC pending request.

        `request_id` is not optional in spirit. The store holds every checkpoint of every case, so
        "the newest checkpoint with any pending request" is usually some *other* case's - which
        surfaces as `ValueError: Response provided for unknown request ID` at the worst moment.
        `pending_request_info_events` is keyed by request id, so the match is exact.
        """
        if self.checkpoints is None:
            return None
        checkpoints = await self.checkpoints.list_checkpoints(
            workflow_name=self.workflow_name(mode))
        if request_id is not None:
            checkpoints = [c for c in checkpoints if request_id in c.pending_request_info_events]
        if not checkpoints:
            return None
        newest = max(checkpoints, key=lambda c: (c.iteration_count, c.timestamp))
        return str(newest.checkpoint_id)

    async def answer(self, case_id: str, response: ApprovalResponse) -> dict[str, Any]:
        """Approve or reject a case, whether or not this replica is the one running it.

        Two paths, and the second is the Episode 2 set piece:

        - **warm**: the case is parked on this replica's live stream, so the answer joins it and
          the browser sees the run continue in the same timeline.
        - **cold**: nobody is holding the case - the process was killed, or the approval arrived
          on another replica. The workflow is rebuilt from `node_ids`, restored from the newest
          checkpoint carrying the pending request, and completed here.
        """
        doc = await self.cases.get(case_id)
        if doc is None:
            raise KeyError(case_id)
        mode = normalise_mode(str(doc.get("mode") or self.default_mode))

        run = self._runs.get(case_id)
        if run is not None and not run.done and run.request_id:
            run.answers.put_nowait(response)
            return {"case_id": case_id, "path": "warm", "streaming": True}

        return await self.resume_from_checkpoint(case_id, mode, response=response)

    async def resume_from_checkpoint(
        self, case_id: str, mode: Mode | None = None, *,
        response: ApprovalResponse | None = None, checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        """Rebuild the workflow and finish a parked case. The cold half of kill/resume."""
        doc = await self.cases.get(case_id)
        if doc is None:
            raise KeyError(case_id)
        resolved = normalise_mode(mode or str(doc.get("mode") or self.default_mode))
        request_id = ApprovalExecutor.request_id_for(case_id)
        target = checkpoint_id or await self.latest_checkpoint_id(
            resolved, request_id=request_id)
        if target is None:
            raise LookupError(f"no checkpoint holds a pending request for {case_id}")

        run = self._runs.get(case_id) or Run(case_id)
        self._runs[case_id] = run
        run.done = False
        run.checkpoint_id = target
        run.request_id = run.request_id or ApprovalExecutor.request_id_for(case_id)
        workflow = self.new_workflow(resolved)
        started = time.perf_counter()
        with (
            bound(case_id=case_id),
            span("atlas.resume", case_id=case_id, checkpoint_id=target),
        ):
            responses = {run.request_id: response} if response is not None else None
            suspended = await self._pump(
                run, workflow.run(checkpoint_id=target, responses=responses, stream=True))
        if run.case is not None:
            await self.cases.save(run.case, run.report)
            await self.cases.close_approval(run.case)
        await self._after_decision(run)
        run.finish()
        return {
            "case_id": case_id, "path": "cold", "checkpoint_id": target,
            "seconds": round(time.perf_counter() - started, 2),
            "status": run.case.status if run.case else doc.get("status"),
            "still_suspended": suspended,
        }

    async def sweep(self) -> list[dict[str, Any]]:
        """On startup, finish any case whose approval landed while this service was down.

        Without this, killing the orchestrator between the approval and the resume would leave the
        case parked forever - which is exactly the window the Episode 2 demo opens on purpose.
        """
        answered = await self.cases.answered_while_away()
        results: list[dict[str, Any]] = []
        for doc in answered:
            case_id = str(doc["case_id"])
            answer = doc.get("answer") or {}
            # Only the response's own fields: older rows also carry the audit fields
            # (accepted, refusal_code, ...) and ApprovalResponse forbids extras.
            known = {k: v for k, v in answer.items() if k in ApprovalResponse.model_fields}
            try:
                outcome = await self.resume_from_checkpoint(
                    case_id, response=ApprovalResponse.model_validate(known))
            except Exception as exc:
                log.exception("sweep could not resume %s", case_id)
                outcome = {"case_id": case_id, "error": f"{type(exc).__name__}: {exc}"[:200]}
                # Do not fail on the same row at every restart: park it for a human, visibly.
                await self.cases.mark_approval(case_id, "abandoned", outcome["error"])
            results.append(outcome)
        if results:
            log.info("startup sweep resumed %d case(s): %s", len(results), results)
        return results

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
