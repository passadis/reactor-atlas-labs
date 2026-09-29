"""ATLAS web: the face of the system for all four episodes.

Routes (Phase 04 contract):

    GET  /                          chat console + activity panel
    GET  /approvals                 approval console (plans arrive in Phase 07/10)
    GET  /kb/{doc_id}               knowledge page (citation target)
    POST /api/chat                  start a turn -> {turn_id, session_id}
    GET  /api/chat/{turn_id}/stream SSE: meta, token, tool_start, tool_end, structured, error, done
    GET  /api/cases/{case_id}       case detail
    POST /api/workflow              start a multi-agent case -> {case_id}     (Phase 06)
    GET  /api/workflow/{id}/stream  SSE: node, executor_*, report, error, end (Phase 06)
    GET  /api/workflow/{id}         typed workflow state + decision log       (Phase 06)
    GET  /api/workflow/graph        workflow topology as JSON and Mermaid     (Phase 06)
    GET  /api/workflow/approvals    cases parked on a human                   (Phase 07)
    POST /api/workflow/{id}/decision approve or reject, as the signed-in user (Phase 07)
    GET  /api/workflow/{id}/checkpoints  what is durable for this case        (Phase 07)
    POST /api/workflow/{id}/ask     handoff follow-up turn                    (Phase 06)
    GET  /api/approvals             plans awaiting approval
    POST /api/approvals/{case_id}   approve / reject - Entra role AND entitlement, server side
    GET  /api/health                liveness + dependency probe (the only unauthenticated route)
    GET  /api/trace/{turn_id}       traceparent + Application Insights deep link
    GET  /api/me                    who you are, which persona, what you may do
    GET  /api/kb/{doc_id}           knowledge document
    GET  /api/instructions          instruction versions and the active one

The scope selector (`?scope=ep1..ep4|full`) is a VIEW filter implemented entirely in the page. The
server always streams the full event set (CLAUDE.md section 8).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib import resources
from typing import Annotated, Any, Literal

import httpx
from atlas_agent.factory import IncidentAgentRuntime
from atlas_agent.instructions import KNOWN_VERSIONS
from atlas_agent.streaming import UiEvent
from atlas_common.auth import SCOPE_FOUNDRY
from atlas_common.config import Settings, load_local_env
from atlas_common.entitlements import can_approve, can_chat
from atlas_common.repositories import EntitlementRepository, RepositoryUnavailableError
from atlas_common.telemetry import configure_telemetry, current_traceparent, span, trace_id
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import get_bearer_token_provider
from azure.search.documents.aio import SearchClient
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sse_starlette.event import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from .auth import Principal, check_bypass_at_startup, current_principal
from .personas import Actor, PersonaResolver
from .shapes import HOSTED_AGENT_NAME, MODES, ShapeRunner
from .stores import CaseStore, TurnStore

log = logging.getLogger("atlas.web")
STATIC = resources.files("atlas_web") / "static"
AGENT_VERSION = os.environ.get("ATLAS_VERSION", "dev")
HEALTH_TIMEOUT_S = 5.0
# The orchestrator runs four agents per case, so its client gets a long connect-and-read budget and
# no read timeout at all while streaming: an SSE stream is idle by design between nodes.
ORCHESTRATOR_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)
ORCHESTRATOR_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
CANCEL_POLL_S = 1.5


class TurnCancelledError(Exception):
    """The user asked to stop this turn (Stop button, or the tab closed)."""


async def until_cancelled(
    source: AsyncIterator[UiEvent], is_cancelled: Callable[[], Awaitable[bool]]
) -> AsyncIterator[UiEvent]:
    """Relay `source` until a cancel request lands, then stop it mid-await.

    Container Apps ingress does not propagate a browser disconnect on a streaming response to the
    app (measured 2026-09-19: the run completed after the client had gone), so a closed tab cannot
    be detected from the socket. The page sends an explicit cancel instead (`sendBeacon` on
    `pagehide`, or the Stop button); it lands on any replica as a flag on the turn document, and
    this loop polls for it. Cancelling the pending `__anext__` raises inside the agent run, whose
    cleanup cancels the model call.
    """
    stop = asyncio.Event()

    async def watch() -> None:
        while not stop.is_set():
            await asyncio.sleep(CANCEL_POLL_S)
            with contextlib.suppress(Exception):
                if await is_cancelled():
                    stop.set()

    watcher = asyncio.create_task(watch())
    stopped = asyncio.create_task(stop.wait())
    try:
        while True:
            pending = asyncio.ensure_future(anext(source))
            done, _ = await asyncio.wait({pending, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if pending in done:
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                yield event
                continue
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
            raise TurnCancelledError
    finally:
        watcher.cancel()
        stopped.cancel()


class State:
    """Process-wide clients, created once in the lifespan."""

    runtime: IncidentAgentRuntime
    turns: TurnStore
    cases: CaseStore
    personas: PersonaResolver
    shapes: ShapeRunner
    search: SearchClient
    project: AIProjectClient
    http: httpx.AsyncClient
    orchestrator: httpx.AsyncClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    check_bypass_at_startup()
    settings = Settings.from_env()
    state = State()
    state.runtime = IncidentAgentRuntime(settings)
    credential = state.runtime.credential
    state.turns = TurnStore(state.runtime.store)
    state.cases = CaseStore(state.runtime.store)
    state.personas = PersonaResolver(EntitlementRepository(state.runtime.store))
    state.search = SearchClient(settings.search_endpoint, settings.knowledge_index, credential)
    state.project = AIProjectClient(endpoint=settings.foundry_project_endpoint,
                                    credential=credential)
    foundry_openai = AsyncOpenAI(
        base_url=settings.foundry_openai_base_url,
        api_key=get_bearer_token_provider(credential, SCOPE_FOUNDRY),
        timeout=60,
        max_retries=1,
    )
    state.shapes = ShapeRunner(
        state.runtime, state.turns, state.cases, foundry_openai,
        state.project.get_openai_client(),
        # The hosted agent is invoked on its own endpoint (/agents/<name>/endpoint/...).
        state.project.get_openai_client(agent_name=HOSTED_AGENT_NAME),
    )
    state.http = httpx.AsyncClient(timeout=HEALTH_TIMEOUT_S)
    state.orchestrator = httpx.AsyncClient(
        base_url=os.environ.get("ATLAS_ORCHESTRATOR_URL", ""), timeout=ORCHESTRATOR_TIMEOUT,
    )
    app.state.atlas = state
    try:
        yield
    finally:
        await state.http.aclose()
        await state.orchestrator.aclose()
        await state.search.close()
        await state.project.close()
        await state.runtime.close()


def create_app() -> FastAPI:
    load_local_env()  # local runs only; in Azure every value is already in the environment
    configure_telemetry("atlas-web", service_version=AGENT_VERSION)
    app = FastAPI(title="ATLAS", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    _routes(app)
    return app


def atlas(request: Request) -> State:
    return request.app.state.atlas  # type: ignore[no-any-return]


async def actor_for(
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    act_as: Annotated[str | None, Header(alias="x-atlas-act-as")] = None,
) -> Actor:
    try:
        return await atlas(request).personas.resolve(principal, act_as)
    except RepositoryUnavailableError as exc:
        raise HTTPException(503, "I could not reach the people directory.") from exc


ActorDep = Annotated[Actor, Depends(actor_for)]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{6,64}$")
    mode: Literal["app", "model", "prompt", "hosted"] = "app"
    instructions: Literal["v0", "v1"] | None = None


class WorkflowRequest(BaseModel):
    """Start a multi-agent case. `mode` picks the orchestration shape, not the system's ability."""

    message: str = Field(min_length=8, max_length=4000)
    mode: Literal["graph", "sequential_only", "handoff"] = "graph"


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class WorkflowDecision(BaseModel):
    """Approve or reject a parked case. Who is deciding comes from the token, not from here."""

    decision: Literal["approve", "reject"]
    comment: str = Field(min_length=3, max_length=1000)


class ApprovalRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=3, max_length=1000)


def _sse(event: UiEvent) -> ServerSentEvent:
    return ServerSentEvent(data=json.dumps(event.data, default=str), event=event.event)


def _page(name: str) -> FileResponse:
    return FileResponse(str(STATIC / name), headers={"Cache-Control": "no-store"})


def _routes(app: FastAPI) -> None:
    # -- pages -------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index(_: Annotated[Principal, Depends(current_principal)]) -> FileResponse:
        return _page("index.html")

    @app.get("/approvals", include_in_schema=False)
    async def approvals_page(_: Annotated[Principal, Depends(current_principal)]) -> FileResponse:
        return _page("approvals.html")

    @app.get("/kb/{doc_id}", include_in_schema=False)
    async def kb_page(doc_id: str,
                      _: Annotated[Principal, Depends(current_principal)]) -> FileResponse:
        return _page("kb.html")

    # -- identity ------------------------------------------------------------------------------
    @app.get("/api/me")
    async def me(actor: ActorDep) -> dict[str, Any]:
        body = actor.public()
        body["may_chat"] = can_chat(actor.principal.app_roles).allowed
        body["agent_version"] = AGENT_VERSION
        return body

    @app.get("/api/instructions")
    async def instructions(request: Request, _: ActorDep) -> dict[str, Any]:
        store = atlas(request).runtime.instructions
        try:
            active = await store.active_version()
        except RepositoryUnavailableError:
            active = atlas(request).runtime.settings.instructions_version
        return {"versions": list(KNOWN_VERSIONS), "active": active}

    # -- chat ----------------------------------------------------------------------------------
    @app.post("/api/chat")
    async def start_turn(body: ChatRequest, request: Request, actor: ActorDep) -> dict[str, Any]:
        decision = can_chat(actor.principal.app_roles)
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        state = atlas(request)
        session_id = body.session_id or f"s-{uuid.uuid4().hex[:16]}"
        try:
            turn = await state.turns.create(
                session_id,
                message=body.message,
                mode=body.mode,
                instructions=body.instructions,
                user={"email": actor.principal.email, "persona": actor.persona_role,
                      "app_roles": list(actor.principal.app_roles)},
            )
        except RepositoryUnavailableError as exc:
            raise HTTPException(503, "I could not reach the operations database.") from exc
        return {"turn_id": turn["id"], "session_id": session_id, "mode": body.mode}

    @app.get("/api/chat/{turn_id}/stream")
    async def stream_turn(turn_id: str, request: Request, actor: ActorDep) -> EventSourceResponse:
        state = atlas(request)
        try:
            turn = await state.turns.get(turn_id)
        except RepositoryUnavailableError as exc:
            raise HTTPException(503, "I could not reach the operations database.") from exc
        if turn is None or turn.get("user", {}).get("email") != actor.principal.email:
            raise HTTPException(404, "No such turn.")

        async def events() -> AsyncIterator[ServerSentEvent]:
            if not await state.turns.claim(turn):
                # EventSource reconnect after the turn already ran: end it, do not re-run it.
                yield _sse(UiEvent("done", {"replayed": True, "status": turn.get("status")}))
                return
            started = time.perf_counter()
            summary: dict[str, Any] = {"mode": turn["mode"]}
            first_event_ms: int | None = None
            with span(
                "atlas.chat.turn",
                turn_id=turn_id,
                session_id=turn["session_id"],
                mode=turn["mode"],
                user_role=actor.persona_role,
                agent_version=AGENT_VERSION,
            ) as root:
                turn["trace_id"] = trace_id()
                turn["timeline"] = []
                yield _sse(UiEvent("meta", {
                    "turn_id": turn_id,
                    "session_id": turn["session_id"],
                    "mode": turn["mode"],
                    "traceparent": current_traceparent(),
                    "trace_id": turn["trace_id"],
                    "agent_version": AGENT_VERSION,
                }))
                status = "done"
                try:
                    runner = contextlib.aclosing(state.shapes.run(turn, summary))
                    async with runner as source:
                        async for event in until_cancelled(
                            source,
                            lambda: state.turns.cancel_requested(turn_id, turn["session_id"]),
                        ):
                            if first_event_ms is None and event.event in ("token", "tool_start"):
                                first_event_ms = round((time.perf_counter() - started) * 1000)
                            if event.event == "tool_end":
                                turn["timeline"].append(event.data)
                            if event.event == "structured" and event.data.get("case_id"):
                                root.set_attribute("atlas.case_id", event.data["case_id"])
                            yield _sse(event)
                except TurnCancelledError:
                    status = "cancelled"
                    summary["cancelled"] = True
                    log.warning("turn %s cancelled on request", turn_id)
                    yield _sse(UiEvent("error", {"reason": "cancelled", "message": "Stopped.",
                                                 "retry_after": None}))
                except asyncio.CancelledError:
                    status = "cancelled"
                    log.warning("client disconnected; turn %s cancelled", turn_id)
                    raise
                finally:
                    root.set_attribute("atlas.instructions_version",
                                       str(summary.get("instructions_version", "")))
                    root.set_attribute("atlas.status", status)
                    with contextlib.suppress(Exception):
                        await state.turns.update(
                            turn, status=status, summary=summary,
                            first_event_ms=first_event_ms, trace_id=turn["trace_id"],
                        )
            yield _sse(UiEvent("done", {
                **summary,
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "first_event_ms": first_event_ms,
            }))

        return EventSourceResponse(events(), ping=15, headers={"X-Accel-Buffering": "no"})

    @app.post("/api/chat/{turn_id}/cancel", status_code=202)
    async def cancel_turn(turn_id: str, request: Request, actor: ActorDep) -> dict[str, Any]:
        """Stop a running turn. Sent by the Stop button and by `sendBeacon` when the tab closes."""
        state = atlas(request)
        turn = await state.turns.get(turn_id)
        if turn is None or turn.get("user", {}).get("email") != actor.principal.email:
            raise HTTPException(404, "No such turn.")
        if turn.get("status") in ("pending", "streaming"):
            await state.turns.update(turn, cancel_requested=True)
        return {"turn_id": turn_id, "status": turn.get("status"), "cancel_requested": True}

    # -- workflow (Episode 2) ------------------------------------------------------------------
    #
    # A thin proxy. The orchestrator has no external ingress and no sign-in of its own, so the web
    # app is the only way in: it authenticates the user, checks the same entitlement that gates
    # chat, then forwards. The full event set is passed through untouched - scope is a view filter
    # in the browser (CLAUDE.md section 8), so filtering here would break the Episode 1 encore.
    def orchestrator_url() -> str:
        url = os.environ.get("ATLAS_ORCHESTRATOR_URL", "")
        if not url:
            raise HTTPException(503, "The workflow orchestrator is not configured.")
        return url

    @app.post("/api/workflow", status_code=202)
    async def start_case(body: WorkflowRequest, request: Request,
                         actor: ActorDep) -> dict[str, Any]:
        decision = can_chat(actor.principal.app_roles)
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        orchestrator_url()
        try:
            response = await atlas(request).orchestrator.post(
                "/cases",
                json={"report": body.message, "mode": body.mode,
                      "raised_by": {"email": actor.principal.email, "persona": actor.persona_role}},
                headers={"traceparent": current_traceparent() or ""},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("orchestrator refused a case: %s", exc)
            raise HTTPException(503, "I could not reach the workflow orchestrator.") from exc
        return dict(response.json())

    @app.get("/api/workflow/{case_id}/stream")
    async def stream_case(case_id: str, request: Request, _: ActorDep) -> EventSourceResponse:
        orchestrator_url()
        state = atlas(request)

        async def events() -> AsyncIterator[ServerSentEvent]:
            try:
                async with state.orchestrator.stream(
                    "GET", f"/cases/{case_id}/stream",
                    timeout=ORCHESTRATOR_STREAM_TIMEOUT,
                    headers={"traceparent": current_traceparent() or ""},
                ) as upstream:
                    upstream.raise_for_status()
                    event = ""
                    async for line in upstream.aiter_lines():
                        if line.startswith("event: "):
                            event = line.removeprefix("event: ").strip()
                        elif line.startswith("data: "):
                            yield ServerSentEvent(event=event or "message",
                                                  data=line.removeprefix("data: "))
            except httpx.HTTPError as exc:
                log.warning("workflow stream failed for %s: %s", case_id, exc)
                yield _sse(UiEvent("error", {
                    "message": "I lost the connection to the workflow orchestrator.",
                    "reason": "degraded",
                }))

        return EventSourceResponse(events())

    # Declared before /api/workflow/{case_id}: FastAPI matches in declaration
    # order, so a parameterised route above this one would swallow "approvals"
    # and answer "No case approvals." - which it did.
    @app.get("/api/workflow/approvals")
    async def workflow_approvals(request: Request, _: ActorDep) -> dict[str, Any]:
        orchestrator_url()
        try:
            response = await atlas(request).orchestrator.get("/approvals")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(503, "I could not reach the workflow orchestrator.") from exc
        return dict(response.json())

    @app.get("/api/workflow/graph")
    async def workflow_graph(request: Request, _: ActorDep,
                             mode: str | None = None) -> dict[str, Any]:
        orchestrator_url()
        try:
            response = await atlas(request).orchestrator.get(
                "/graph", params={"mode": mode} if mode else None)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(503, "I could not reach the workflow orchestrator.") from exc
        return dict(response.json())

    @app.get("/api/workflow/{case_id}")
    async def workflow_case(case_id: str, request: Request, _: ActorDep) -> dict[str, Any]:
        orchestrator_url()
        try:
            response = await atlas(request).orchestrator.get(f"/cases/{case_id}")
            if response.status_code == 404:
                raise HTTPException(404, f"No case {case_id}.")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(503, "I could not reach the workflow orchestrator.") from exc
        return dict(response.json())

    @app.post("/api/workflow/{case_id}/ask")
    async def workflow_ask(case_id: str, body: AskRequest, request: Request,
                           actor: ActorDep) -> dict[str, Any]:
        decision = can_chat(actor.principal.app_roles)
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        orchestrator_url()
        try:
            response = await atlas(request).orchestrator.post(
                f"/cases/{case_id}/ask", json={"question": body.question},
                headers={"traceparent": current_traceparent() or ""},
            )
            if response.status_code == 404:
                raise HTTPException(404, f"No case {case_id}.")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(503, "I could not reach the workflow orchestrator.") from exc
        return dict(response.json())

    # -- workflow approvals (Episode 2) --------------------------------------------------------
    #
    # The orchestrator checks the approval again on its side; this layer exists to bind the
    # decision to a signed-in human. `can_approve` is the same check the Phase 04 console uses,
    # so the two approval paths cannot drift apart.

    @app.post("/api/workflow/{case_id}/decision")
    async def workflow_decision(case_id: str, body: WorkflowDecision, request: Request,
                                actor: ActorDep) -> dict[str, Any]:
        """Approve or reject a parked case, as the signed-in user.

        The caller does not get to say who they are: `by` and `role` come from the token and the
        resolved persona, never from the request body. That is what stops the Episode 4
        unauthorized beat from being a matter of typing a different role.
        """
        orchestrator_url()
        state = atlas(request)
        try:
            pending = (await state.orchestrator.get("/approvals")).json()
        except httpx.HTTPError as exc:
            raise HTTPException(503, "I could not reach the workflow orchestrator.") from exc
        row = next((p for p in pending.get("pending", []) if p.get("case_id") == case_id), None)
        if row is None:
            raise HTTPException(404, f"No approval is pending for {case_id}.")

        decision = can_approve(
            actor.principal.app_roles, actor.entitlement,
            risk=str(row.get("risk") or "high"),
            tier=1 if row.get("required_roles") else 3,
            blast_radius=int(row.get("blast_radius") or 0),
        )
        if not decision.allowed:
            log.warning("approval refused for %s by %s: %s", case_id,
                        actor.principal.email, decision.reason)
            raise HTTPException(403, decision.reason)

        path = "approve" if body.decision == "approve" else "reject"
        try:
            response = await state.orchestrator.post(
                f"/cases/{case_id}/{path}",
                json={"by": actor.principal.email, "role": actor.persona_role,
                      "comment": body.comment},
                headers={"traceparent": current_traceparent() or ""},
                timeout=ORCHESTRATOR_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(503, "I could not reach the workflow orchestrator.") from exc
        if response.status_code >= 400:
            raise HTTPException(response.status_code,
                                response.json().get("detail", "The orchestrator refused."))
        return dict(response.json())

    @app.get("/api/workflow/{case_id}/checkpoints")
    async def workflow_checkpoints(case_id: str, request: Request, _: ActorDep) -> dict[str, Any]:
        """What is durable for this case. Projected on stage during Episode 2."""
        orchestrator_url()
        try:
            response = await atlas(request).orchestrator.get(
                "/checkpoints", params={"case_id": case_id})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(503, "I could not reach the workflow orchestrator.") from exc
        return dict(response.json())

    # -- cases ---------------------------------------------------------------------------------
    @app.get("/api/cases/{case_id}")
    async def get_case(case_id: str, request: Request, _: ActorDep) -> dict[str, Any]:
        try:
            case = await atlas(request).cases.get(case_id)
        except RepositoryUnavailableError as exc:
            raise HTTPException(503, "I could not reach the case store.") from exc
        if case is None:
            raise HTTPException(404, f"No case {case_id}.")
        return case

    # -- approvals -----------------------------------------------------------------------------
    @app.get("/api/approvals")
    async def list_approvals(request: Request, actor: ActorDep) -> dict[str, Any]:
        try:
            pending = await atlas(request).cases.list_awaiting_approval()
        except RepositoryUnavailableError as exc:
            raise HTTPException(503, "I could not reach the case store.") from exc
        for item in pending:
            plan = item.get("plan") or {}
            item["decision"] = can_approve(
                actor.principal.app_roles, actor.entitlement,
                risk=str(plan.get("risk", "high")), tier=int(plan.get("tier", 1)),
                blast_radius=int(plan.get("blast_radius", 99)),
            ).__dict__
        return {"pending": pending}

    @app.post("/api/approvals/{case_id}")
    async def decide(case_id: str, body: ApprovalRequest, request: Request,
                     actor: ActorDep) -> dict[str, Any]:
        state = atlas(request)
        case = await state.cases.get(case_id)
        if case is None:
            raise HTTPException(404, f"No case {case_id}.")
        plan = case.get("plan")
        if not plan or plan.get("status") != "awaiting_approval":
            raise HTTPException(409, "This case has no plan awaiting approval.")
        decision = can_approve(
            actor.principal.app_roles, actor.entitlement,
            risk=str(plan["risk"]), tier=int(plan["tier"]),
            blast_radius=int(plan["blast_radius"]),
        )
        if not decision.allowed:
            log.warning("approval refused for %s on %s: %s", actor.principal.email, case_id,
                        decision.reason)
            raise HTTPException(403, decision.reason)
        plan["status"] = "approved" if body.decision == "approve" else "rejected"
        plan["decided_by"] = {"email": actor.principal.email, "persona": actor.persona_role}
        plan["decision_reason"] = body.reason
        case["plan"] = plan
        await state.cases.save(case)
        return {"case_id": case_id, "status": plan["status"], "reason": decision.reason}

    # -- knowledge -----------------------------------------------------------------------------
    @app.get("/api/kb/{doc_id}")
    async def kb(doc_id: str, request: Request, _: ActorDep) -> dict[str, Any]:
        if not doc_id.replace("-", "").isalnum() or len(doc_id) > 40:
            raise HTTPException(400, "Bad document id.")
        results = await atlas(request).search.search(
            search_text="*",
            filter=f"doc_id eq '{doc_id}'",
            select=["id", "doc_id", "doc_type", "title", "chunk", "services",
                    "last_reviewed", "owner_team", "risk"],
            top=20,
        )
        chunks = sorted([r async for r in results], key=lambda r: str(r["id"]))
        if not chunks:
            raise HTTPException(404, f"No document {doc_id}.")
        head = chunks[0]
        return {
            "doc_id": head["doc_id"], "doc_type": head["doc_type"], "title": head["title"],
            "services": head.get("services") or [], "last_reviewed": head.get("last_reviewed"),
            "owner_team": head.get("owner_team"), "risk": head.get("risk"),
            "text": "\n\n".join(str(c["chunk"]) for c in chunks),
        }

    # -- trace ---------------------------------------------------------------------------------
    @app.get("/api/trace/{turn_id}")
    async def trace_link(turn_id: str, request: Request, _: ActorDep) -> dict[str, Any]:
        turn = await atlas(request).turns.get(turn_id)
        if turn is None or not turn.get("trace_id"):
            raise HTTPException(404, "No trace recorded for that turn.")
        tid = turn["trace_id"]
        component = os.environ.get("ATLAS_APPINSIGHTS_ID", "")
        link = None
        if component:
            parts = component.strip("/").split("/")
            component_id = {"SubscriptionId": parts[1], "ResourceGroup": parts[3],
                            "Name": parts[-1]}
            model = {"eventId": tid, "timestamp": turn.get("created_at")}
            link = ("https://portal.azure.com/#blade/AppInsightsExtension/DetailsV2Blade/"
                    f"ComponentId/{urllib.parse.quote(json.dumps(component_id))}/"
                    f"DataModel/{urllib.parse.quote(json.dumps(model))}")
        return {"turn_id": turn_id, "trace_id": tid,
                "traceparent": f"00-{tid}-0000000000000000-01", "app_insights": link}

    # -- health --------------------------------------------------------------------------------
    @app.get("/api/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        """Container probes. Deliberately checks nothing downstream."""
        return {"status": "live"}

    @app.get("/api/health")
    async def health(request: Request) -> JSONResponse:
        state = atlas(request)
        checks: dict[str, dict[str, Any]] = {}

        async def probe(name: str, required: bool, coro: Any) -> None:
            t0 = time.perf_counter()
            try:
                async with asyncio.timeout(HEALTH_TIMEOUT_S):
                    detail = await coro
                checks[name] = {"status": "ok", "required": required, "detail": detail}
            except Exception as exc:
                checks[name] = {"status": "unreachable", "required": required,
                                "detail": type(exc).__name__}
            checks[name]["ms"] = round((time.perf_counter() - t0) * 1000)

        async def cosmos() -> str:
            return f"instructions active={await state.runtime.instructions.active_version()}"

        async def search() -> str:
            results = await state.search.search(search_text="*", top=0, include_total_count=True)
            return f"{await results.get_count()} documents"

        async def foundry() -> str:
            agent = await state.project.agents.get("atlas-triage-prompt")
            return f"prompt agent {agent.name} reachable"

        async def orchestrator() -> str:
            url = os.environ.get("ATLAS_ORCHESTRATOR_URL", "")
            if not url:
                return "not configured"
            response = await state.http.get(f"{url}/api/health")
            body = response.json()
            return (f"HTTP {response.status_code}, default mode "
                    f"{body.get('mode')}, modes {body.get('modes')}")

        async def ops_api() -> str:
            # Which door the agent tools use (PHASE-08 section C). Not required: the tools degrade
            # to a typed "could not reach" answer rather than taking the web app down.
            url = state.runtime.settings.ops_api_url
            if not url:
                return "not configured, tools read Cosmos directly"
            response = await state.http.get(f"{url.rstrip('/')}/health")
            return f"HTTP {response.status_code}, tools on {state.runtime.tools.backend}"

        await asyncio.gather(
            probe("cosmos", True, cosmos()),
            probe("search", True, search()),
            probe("foundry", True, foundry()),
            probe("orchestrator", False, orchestrator()),
            probe("ops_api", False, ops_api()),
        )
        healthy = all(c["status"] == "ok" for c in checks.values() if c["required"])
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "version": AGENT_VERSION,
             "modes": list(MODES), "checks": checks},
            status_code=200 if healthy else 503,
        )


app = create_app()
