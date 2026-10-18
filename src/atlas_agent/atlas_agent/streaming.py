"""Turn the framework's stream into the lifecycle events an audience can read.

The right-hand activity panel in the web app (Phase 04) shows `lookup_service started -> 142 ms ->
ok`. The framework streams `function_call` fragments (arguments arrive in pieces) and later a
`function_result` with the same `call_id`; this module pairs them, times them, and classifies the
outcome from the typed tool result (`ToolFailure`, `NotFound`, or data).

Every hosting shape emits the same event vocabulary, so the UI never needs to know which one ran:

| event        | data                                                        |
|--------------|-------------------------------------------------------------|
| `token`      | `{"text": ...}` - a prose delta                             |
| `tool_start` | `{"call_id", "name"}`                                       |
| `tool_end`   | `{"call_id", "name", "ms", "outcome", "detail"}`            |
| `structured` | `{"kind": "incident_triage" \\| "triage_failure", "value"}` |
| `error`      | `{"reason", "message", "retry_after"}`                      |
| `meta`       | `{"traceparent", "trace_id", "mode", ...}`                  |
| `done`       | `{"elapsed_ms", "tool_calls", "input_tokens", ...}`         |
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_framework import (
    AgentResponseUpdate,
    FunctionInvocationContext,
    FunctionMiddleware,
)
from pydantic import BaseModel

Outcome = Literal["ok", "not_found", "failed"]


@dataclass
class UiEvent:
    event: str
    data: dict[str, Any]


def classify_result(result: Any, exception: str | None) -> tuple[Outcome, str]:
    """Outcome of one tool call, from the typed result the tool returned."""
    if exception:
        return "failed", exception
    payload: Any = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except ValueError:
            return "ok", ""
    if isinstance(payload, dict) and payload.get("ok") is False:
        if "backend" in payload:
            return "failed", str(payload.get("message", ""))
        return "not_found", str(payload.get("message", ""))
    return "ok", ""


def _as_dict(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, BaseModel):
        return arguments.model_dump()
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class ToolTimer(FunctionMiddleware):
    """Function middleware that measures each tool's own execution time.

    The model issues tool calls in parallel and their results stream back together, so timing from
    the stream alone shows the batch, not the tool. The invocation context carries no `call_id`, so
    timings are matched to streamed calls by tool name plus the arguments the model sent.
    Pass it per run: `agent.run(..., middleware=[timer])`.
    """

    def __init__(self) -> None:
        self._done: list[tuple[str, dict[str, Any], int]] = []

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        started = time.perf_counter()
        try:
            await call_next()
        finally:
            self._done.append((
                context.function.name,
                _as_dict(context.arguments),
                round((time.perf_counter() - started) * 1000),
            ))

    def pop(self, name: str, raw_arguments: dict[str, Any]) -> int | None:
        """Duration of the recorded call matching this streamed call, consumed once."""
        for index, (done_name, args, ms) in enumerate(self._done):
            if done_name == name and all(args.get(k) == v for k, v in raw_arguments.items()):
                del self._done[index]
                return ms
        return None


@dataclass
class ActivityTracker:
    """Stateful translator: feed it updates, it yields UI events. One instance per turn."""

    stream_text: bool = True
    timer: ToolTimer | None = None
    _started: dict[str, tuple[str, float]] = field(default_factory=dict)
    _arguments: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[str] = field(default_factory=list)
    outcomes: dict[str, Outcome] = field(default_factory=dict)

    def feed(self, update: AgentResponseUpdate) -> list[UiEvent]:
        events: list[UiEvent] = []
        for content in update.contents:
            if content.type == "function_call" and content.call_id:
                self._collect_arguments(content.call_id, content.arguments)
            if content.type == "function_call" and content.call_id and content.name:
                if content.call_id not in self._started:
                    self._started[content.call_id] = (content.name, time.perf_counter())
                    self.tool_calls.append(content.name)
                    events.append(
                        UiEvent("tool_start", {"call_id": content.call_id, "name": content.name})
                    )
            elif content.type == "function_result" and content.call_id:
                name, started = self._started.get(content.call_id, ("tool", time.perf_counter()))
                outcome, detail = classify_result(content.result, content.exception)
                self.outcomes[content.call_id] = outcome
                measured = None
                if self.timer is not None:
                    raw = _as_dict(self._arguments.get(content.call_id))
                    measured = self.timer.pop(name, raw)
                events.append(
                    UiEvent(
                        "tool_end",
                        {
                            "call_id": content.call_id,
                            "name": name,
                            "ms": measured
                            if measured is not None
                            else round((time.perf_counter() - started) * 1000),
                            "measured": measured is not None,
                            "outcome": outcome,
                            "detail": detail[:200],
                        },
                    )
                )
            elif content.type == "text" and self.stream_text and content.text:
                events.append(UiEvent("token", {"text": content.text}))
        return events

    def _collect_arguments(self, call_id: str, arguments: Any) -> None:
        """Arguments stream in fragments (strings) or arrive whole (a mapping)."""
        if arguments is None:
            return
        if isinstance(arguments, str):
            previous = self._arguments.get(call_id)
            self._arguments[call_id] = (previous if isinstance(previous, str) else "") + arguments
        else:
            self._arguments[call_id] = arguments
