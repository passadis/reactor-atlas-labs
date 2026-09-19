"""The four hosting shapes behind one event stream (the Episode 1 mode switch).

| Mode     | What answers                               | Tools           | State             |
|----------|--------------------------------------------|-----------------|-------------------|
| `model`  | `gpt-main`, a bare Responses call          | none            | none              |
| `prompt` | Foundry prompt agent `atlas-triage-prompt` | knowledge index | Foundry responses |
| `hosted` | Foundry hosted agent (Phase 05)            | server-side     | Foundry           |
| `app`    | `atlas_agent` in this process              | six typed tools | Cosmos session    |

Every shape yields the same `UiEvent` vocabulary (see `atlas_agent.streaming`), so the activity
panel, the contract view and the error handling are identical whichever one ran. A shape that is
not deployed yet says so as a typed `not_deployed` event; it lights up the moment it exists,
without a redeploy of this app.

Nothing here raises into the page. Model-side failures become `error` events with the same
degraded messages the agent uses (content filter, throttling with a retry countdown, unreachable
model); anything unexpected becomes a generic `error` event and a logged exception.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncGenerator
from typing import Any, Literal

from atlas_agent.factory import (
    DEGRADED_MESSAGES,
    ChatTurn,
    IncidentAgentRuntime,
    TriageResult,
    answer,
    model_failure_reason,
    retry_after_seconds,
    triage,
)
from atlas_agent.schemas import IncidentTriage
from atlas_agent.streaming import ActivityTracker, ToolTimer, UiEvent
from atlas_common.repositories import RepositoryUnavailableError
from openai import AsyncOpenAI, NotFoundError

from .stores import CaseStore, TurnStore

log = logging.getLogger("atlas.web.shapes")

Mode = Literal["app", "model", "prompt", "hosted"]
MODES: tuple[Mode, ...] = ("app", "model", "prompt", "hosted")

PROMPT_AGENT_NAME = "atlas-triage-prompt"
HOSTED_AGENT_NAME = os.environ.get("ATLAS_HOSTED_AGENT_NAME", "atlas-incident-hosted")
SHAPE_TIMEOUT_S = 150.0

RAW_MODEL_PREAMBLE = (
    "You are the on-call assistant for Contoso Cloud Operations. Help the engineer with this."
)


def _error(reason: str, message: str | None = None, retry_after: int | None = None) -> UiEvent:
    return UiEvent(
        "error",
        {
            "reason": reason,
            "message": message or DEGRADED_MESSAGES.get(reason, "Something went wrong."),
            "retry_after": retry_after,
        },
    )


def _model_error(exc: BaseException) -> UiEvent | None:
    reason = model_failure_reason(exc)
    if reason is None:
        return None
    return _error(reason, retry_after=retry_after_seconds(exc))


class ShapeRunner:
    def __init__(
        self,
        runtime: IncidentAgentRuntime,
        turns: TurnStore,
        cases: CaseStore,
        foundry_openai: AsyncOpenAI,
        project_openai: AsyncOpenAI,
    ) -> None:
        self.runtime = runtime
        self.turns = turns
        self.cases = cases
        self.foundry_openai = foundry_openai
        self.project_openai = project_openai

    async def run(self, turn: dict[str, Any], summary: dict[str, Any]) -> AsyncGenerator[UiEvent]:
        """Run one turn in its shape. `summary` is filled in for the caller's `done` event."""
        mode: Mode = turn.get("mode", "app")
        try:
            async with asyncio.timeout(SHAPE_TIMEOUT_S):
                if mode == "app":
                    source = self._app(turn, summary)
                elif mode == "model":
                    source = self._model(turn)
                else:
                    source = self._foundry_agent(turn, mode)
                async with contextlib.aclosing(source) as events:
                    async for event in events:
                        yield event
        except TimeoutError:
            yield _error("timeout")
        except Exception as exc:
            failure = _model_error(exc)
            if failure is None:
                log.exception("turn %s failed in mode %s", turn.get("id"), mode)
                failure = _error("internal", "Something went wrong on our side. It is logged.")
            yield failure

    # -- app: this process, atlas_agent ------------------------------------------------------
    async def _app(self, turn: dict[str, Any], summary: dict[str, Any]) -> AsyncGenerator[UiEvent]:
        runtime = self.runtime
        agent, loaded = await runtime.agent(turn.get("instructions"))
        summary["instructions_version"] = loaded.version
        yield UiEvent("meta", {"instructions": {"version": loaded.version,
                                                "source": loaded.source}})
        session_id = turn["session_id"]
        try:
            session = await runtime.sessions.load(session_id)
        except RepositoryUnavailableError:
            session = None
            yield _error("degraded", "I could not reach the conversation store. This turn will "
                         "work, but it will not be remembered.")
        first_turn = session is None
        session = session or agent.create_session()

        queue: asyncio.Queue[UiEvent] = asyncio.Queue()
        timer = ToolTimer()
        tracker = ActivityTracker(stream_text=not first_turn, timer=timer)

        def on_update(update: Any) -> None:
            for event in tracker.feed(update):
                queue.put_nowait(event)

        async def work() -> TriageResult | ChatTurn:
            if first_turn:
                return await triage(agent, turn["message"], session, on_update=on_update,
                                    middleware=[timer])
            return await answer(agent, turn["message"], session, on_update=on_update,
                                middleware=[timer])

        task = asyncio.create_task(work())
        try:
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    continue
                getter.cancel()
                while not queue.empty():
                    yield queue.get_nowait()
                break
            result = task.result()
        finally:
            # Client gone (tab closed) or timeout: stop the model run, do not leave it burning.
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                summary["cancelled"] = True
                log.warning("turn %s cancelled mid-run", turn.get("id"))

        summary["tool_calls"] = tracker.tool_calls
        with contextlib.suppress(RepositoryUnavailableError):
            await runtime.sessions.save(session_id, session)

        if isinstance(result, ChatTurn):
            if result.failure:
                yield _error(result.failure, result.text, result.retry_after)
            return

        summary["input_tokens"] = result.input_tokens
        summary["output_tokens"] = result.output_tokens
        output = result.output
        if not isinstance(output, IncidentTriage):
            yield UiEvent("structured", {"kind": "triage_failure", "value": output.model_dump()})
            yield _error(output.reason, retry_after=output.retry_after)
            return

        case_id = self.cases.new_id()
        summary["case_id"] = case_id
        yield UiEvent("structured", {"kind": "incident_triage", "case_id": case_id,
                                     "value": output.model_dump(mode="json")})
        with contextlib.suppress(RepositoryUnavailableError):
            await self.cases.save({
                "id": case_id,
                "case_id": case_id,
                "session_id": session_id,
                "title": turn["message"][:160],
                "severity": output.severity,
                "triage": output.model_dump(mode="json"),
                "citations": [c.model_dump(mode="json") for c in output.citations],
                "timeline": turn.get("timeline", []),
                "tool_calls": tracker.tool_calls,
                "instructions_version": loaded.version,
                "raised_by": turn.get("user", {}),
                "trace_id": turn.get("trace_id"),
            })

    # -- model: a bare Responses call --------------------------------------------------------
    async def _model(self, turn: dict[str, Any]) -> AsyncGenerator[UiEvent]:
        stream = await self.foundry_openai.responses.create(
            model=self.runtime.settings.model_main,
            input=f"{RAW_MODEL_PREAMBLE}\n\n{turn['message']}",
            store=False,
            stream=True,
        )
        async for event in stream:
            if event.type == "response.output_text.delta":
                yield UiEvent("token", {"text": event.delta})

    # -- prompt / hosted: server-side Foundry agents ----------------------------------------
    async def _foundry_agent(self, turn: dict[str, Any], mode: Mode) -> AsyncGenerator[UiEvent]:
        name = PROMPT_AGENT_NAME if mode == "prompt" else HOSTED_AGENT_NAME
        state = await self.turns.shape_state(turn["session_id"], mode)
        extra: dict[str, Any] = {"agent_reference": {"name": name, "type": "agent_reference"}}
        kwargs: dict[str, Any] = {}
        if state.get("previous_response_id"):
            kwargs["previous_response_id"] = state["previous_response_id"]
        try:
            stream = await self.project_openai.responses.create(
                input=turn["message"], stream=True, extra_body=extra, **kwargs
            )
        except NotFoundError:
            yield _error("not_deployed", f"The {mode} agent '{name}' is not deployed yet."
                         + (" It arrives in Phase 05." if mode == "hosted" else ""))
            return

        started: dict[str, tuple[str, float]] = {}
        response_id: str | None = None
        async for event in stream:
            kind = event.type
            if kind == "response.created":
                response_id = event.response.id
            elif kind == "response.output_text.delta":
                yield UiEvent("token", {"text": event.delta})
            elif kind == "response.output_item.added" and event.item.type != "message":
                item_id = getattr(event.item, "id", None) or str(len(started))
                started[item_id] = (event.item.type, time.perf_counter())
                yield UiEvent("tool_start", {"call_id": item_id, "name": event.item.type})
            elif kind == "response.output_item.done" and event.item.type != "message":
                item_id = getattr(event.item, "id", None) or ""
                name_, t0 = started.get(item_id, (event.item.type, time.perf_counter()))
                status = getattr(event.item, "status", "completed")
                yield UiEvent("tool_end", {
                    "call_id": item_id, "name": name_,
                    "ms": round((time.perf_counter() - t0) * 1000),
                    "outcome": "ok" if status in (None, "completed") else "failed",
                    "detail": "",
                })
            elif kind in ("response.failed", "response.incomplete"):
                yield _error("model_unavailable")
        if response_id:
            await self.turns.save_shape_state(
                turn["session_id"], mode, previous_response_id=response_id
            )
