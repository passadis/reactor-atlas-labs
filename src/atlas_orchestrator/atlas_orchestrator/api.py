"""The orchestrator's HTTP surface. Internal ingress only - the web app is its only caller.

There is no sign-in here and that is deliberate: the Container App has no external ingress, so the
only thing that can reach it is another app in the same environment. The web app has already
authenticated the user and checked entitlements before it calls; repeating that here would mean
duplicating the entitlement rules in a second place, which is how they drift.

`traceparent` arrives on every call and is carried into the run, so one trace spans browser to web
app to orchestrator to model.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from atlas_agent.factory import IncidentAgentRuntime
from atlas_common.auth import get_async_credential
from atlas_common.config import Settings, load_local_env
from atlas_common.telemetry import configure_telemetry
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .approval import ApprovalResponse
from .checkpoints import probe as probe_checkpoints
from .graph import MODES
from .models import IncidentCase
from .node_ids import WORKFLOW_GRAPH
from .policy import approval_is_valid
from .runner import CaseRunner

log = logging.getLogger("atlas.orchestrator")

SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}


class NewCase(BaseModel):
    report: str = Field(min_length=8, max_length=4000)
    mode: str | None = None
    raised_by: dict[str, str] = Field(default_factory=dict)


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class Decide(BaseModel):
    """An approval or a rejection. The comment is mandatory on both: no unexplained decisions."""

    by: str = Field(min_length=3, max_length=200)
    role: str = Field(min_length=2, max_length=40)
    comment: str = Field(min_length=3, max_length=1000)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    load_local_env()
    configure_telemetry("atlas-orchestrator")
    settings = Settings.from_env()
    credential = get_async_credential()
    runtime = IncidentAgentRuntime(settings, credential)
    runner = CaseRunner(runtime, os.environ.get("ATLAS_ORCHESTRATION_MODE", "graph"))
    app.state.runner = runner
    # Say plainly whether checkpointing works. A broken store is otherwise indistinguishable from
    # a working one until a resume is attempted, which on stage is far too late.
    app.state.checkpoint_status = await probe_checkpoints(runner.checkpoints, WORKFLOW_GRAPH)
    app.state.reference_data = await runner.load_reference_data()
    log.info("orchestrator ready in %s mode | checkpoints: %s | %s",
             runner.default_mode, app.state.checkpoint_status, app.state.reference_data)
    # Finish anything whose approval landed while this service was down.
    app.state.swept = await runner.sweep()
    try:
        yield
    finally:
        # Order matters: stop the runs, then close the clients they were using.
        await app.state.runner.close()
        await runtime.close()


app = FastAPI(title="ATLAS orchestrator", version="0.6.0", lifespan=lifespan)


def runner(request: Request) -> CaseRunner:
    return request.app.state.runner  # type: ignore[no-any-return]


@app.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness plus what this replica is configured to do. The web app probes this."""
    engine = runner(request)
    return {"status": "ok", "service": "atlas-orchestrator",
            "mode": engine.default_mode, "modes": list(MODES),
            "checkpoints": getattr(request.app.state, "checkpoint_status", "unknown"),
            "reference_data": getattr(request.app.state, "reference_data", "not loaded"),
            "policy_gate": "on" if engine.gated else "off",
            "swept_on_start": len(getattr(request.app.state, "swept", []) or [])}


@app.post("/cases", status_code=202)
async def create_case(body: NewCase, request: Request,
                      wait: bool = Query(default=False)) -> dict[str, Any]:
    """Register a case. The run starts when the stream is opened, or now if `wait=true`.

    Deferring the start is what lets a second replica serve the stream: the pending case lives in
    Cosmos, and whichever replica the stream lands on picks it up.
    """
    engine = runner(request)
    traceparent = request.headers.get("traceparent")
    if wait:
        report = await engine.run_sync(body.report, mode=body.mode, traceparent=traceparent)
        return {"case_id": report.case_id, "status": "complete",
                "result": report.model_dump(mode="json", exclude={"case"})}
    doc = await engine.create(body.report, mode=body.mode, traceparent=traceparent,
                              raised_by=body.raised_by)
    return {"case_id": doc["case_id"], "status": "pending", "mode": doc["mode"]}


@app.get("/cases/{case_id}")
async def get_case(case_id: str, request: Request) -> dict[str, Any]:
    """The typed state and the decision log, as they stand."""
    doc = await runner(request).get(case_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    return doc


@app.get("/cases/{case_id}/stream")
async def stream_case(case_id: str, request: Request) -> StreamingResponse:
    """The full event stream for a case. Every event, every time - the client filters.

    Server-side filtering by demo scope would break the Episode 1 encore, which replays a run that
    already happened at a wider scope (CLAUDE.md section 8).
    """
    engine = runner(request)

    async def events() -> AsyncIterator[bytes]:
        try:
            async for event in engine.stream(case_id):
                yield f"event: {event.kind}\ndata: {event.model_dump_json()}\n\n".encode()
        except KeyError:
            payload = json.dumps({"message": f"no case {case_id}"})
            yield f"event: error\ndata: {payload}\n\n".encode()
        except Exception as exc:
            log.exception("stream failed for %s", case_id)
            payload = json.dumps({"message": f"{type(exc).__name__}: {exc}"[:300]})
            yield f"event: error\ndata: {payload}\n\n".encode()
        yield b"event: end\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/cases/{case_id}/ask")
async def ask_case(case_id: str, body: Question, request: Request) -> dict[str, Any]:
    """One handoff turn on an analysed case. Control returns to the engineer after the answer."""
    try:
        return await runner(request).ask(case_id, body.question)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no case {case_id}") from exc


@app.get("/approvals")
async def pending_approvals(request: Request) -> dict[str, Any]:
    """Cases parked on a human. The approval console and Phase 10's Logic App read this."""
    rows = await runner(request).cases.list_pending_approvals()
    return {"pending": rows, "count": len(rows)}


@app.post("/cases/{case_id}/approve")
async def approve_case(case_id: str, body: Decide, request: Request) -> dict[str, Any]:
    return await _decide(case_id, body, request, approved=True)


@app.post("/cases/{case_id}/reject")
async def reject_case(case_id: str, body: Decide, request: Request) -> dict[str, Any]:
    return await _decide(case_id, body, request, approved=False)


async def _decide(case_id: str, body: Decide, request: Request, *,
                  approved: bool) -> dict[str, Any]:
    """Validate the answer server side, record it, then resume the workflow.

    The order matters. The answer is written to Cosmos BEFORE the resume is attempted, so an
    approval given while the orchestrator is being restarted is not lost - the startup sweep
    finds it and finishes the case. That is the whole point of the Episode 2 kill window.
    """
    engine = runner(request)
    doc = await engine.get(case_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    state = doc.get("state") or {}
    case = IncidentCase.model_validate(state) if state else None
    if case is None or case.policy is None:
        raise HTTPException(status_code=409,
                            detail=f"case {case_id} has no policy decision to answer")

    ok, reason, code = approval_is_valid(
        case.policy,
        approver_role=body.role,
        approver_entitlement=engine.entitlements.get(body.role),
        current_plan_id=case.plan.plan_id if case.plan else "",
    )
    if not ok:
        # Refusals are audited but NOT recorded as answers: the case stays pending and askable,
        # and the startup sweep must never resume a case on the strength of a refused approval.
        # An attempt to approve out of role is exactly what Episode 4's unauthorized beat shows,
        # so it has to leave a trace either way.
        await engine.cases.record_refusal(case_id, {
            "approved": approved, "by": body.by, "role": body.role, "comment": body.comment,
            "accepted": False, "refusal_code": code, "refusal_reason": reason,
        })
        status = {"expired": 410, "plan": 409, "blocked": 409}.get(code, 403)
        raise HTTPException(status_code=status, detail=reason)

    answer = ApprovalResponse(approved=approved, by=body.by, role=body.role, comment=body.comment)
    await engine.cases.record_answer(case_id, answer.model_dump(mode="json"))
    outcome = await engine.answer(case_id, answer)
    return {"case_id": case_id, "approved": approved, "by": body.by, "role": body.role, **outcome}


@app.post("/cases/{case_id}/resume")
async def resume_case(case_id: str, request: Request,
                      checkpoint_id: str | None = Query(default=None)) -> dict[str, Any]:
    """Rehydrate a parked case from its checkpoint without answering it.

    Used by `scripts/resume_case.py` and by the restart path: the pending request is re-emitted,
    so the case is live again and the console can answer it.
    """
    try:
        return await runner(request).resume_from_checkpoint(case_id, checkpoint_id=checkpoint_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no case {case_id}") from exc
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/checkpoints")
async def checkpoints(request: Request, case_id: str | None = Query(default=None),
                      mode: str | None = Query(default=None)) -> dict[str, Any]:
    """What is durable right now. This is the listing projected on stage."""
    engine = runner(request)
    if engine.checkpoints is None:
        return {"storage": "disabled", "checkpoints": []}
    from .graph import normalise_mode

    resolved = engine.workflow_name(normalise_mode(mode or engine.default_mode))
    stored = await engine.checkpoints.list_checkpoints(workflow_name=resolved)
    rows: list[dict[str, Any]] = [
        {
            "checkpoint_id": c.checkpoint_id,
            "iteration": c.iteration_count,
            "timestamp": c.timestamp,
            "pending_requests": list(c.pending_request_info_events),
            "graph_signature_hash": c.graph_signature_hash[:16],
        }
        for c in sorted(stored, key=lambda c: (c.iteration_count, c.timestamp))
    ]
    if case_id:
        def mentions(row: dict[str, Any]) -> bool:
            pending: list[str] = row["pending_requests"]
            return not pending or any(case_id in p for p in pending)

        rows = [r for r in rows if mentions(r)]
    return {"storage": "cosmos", "workflow_name": resolved, "count": len(rows),
            "checkpoints": rows}


@app.get("/cases/{case_id}/graph")
async def case_graph(case_id: str, request: Request,
                     mode: str | None = Query(default=None)) -> dict[str, Any]:
    """The workflow topology as JSON and as Mermaid, for the shape this case actually ran in."""
    engine = runner(request)
    if mode is None:
        doc = await engine.get(case_id)
        mode = str(doc.get("mode")) if doc else None
    return engine.graph(mode)


@app.get("/graph")
async def default_graph(request: Request, mode: str | None = Query(default=None)) -> dict[str, Any]:
    """The topology without a case, so the web app can draw the graph before a run starts."""
    return runner(request).graph(mode)


__all__ = ["NewCase", "Question", "app", "lifespan"]
