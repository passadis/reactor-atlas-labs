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

from .graph import MODES
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    load_local_env()
    configure_telemetry("atlas-orchestrator")
    settings = Settings.from_env()
    credential = get_async_credential()
    runtime = IncidentAgentRuntime(settings, credential)
    app.state.runner = CaseRunner(runtime, os.environ.get("ATLAS_ORCHESTRATION_MODE", "graph"))
    log.info("orchestrator ready in %s mode", app.state.runner.default_mode)
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
    return {"status": "ok", "service": "atlas-orchestrator",
            "mode": runner(request).default_mode, "modes": list(MODES)}


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
