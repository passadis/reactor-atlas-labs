"""Run the Episode 1 incident agent over an evaluation dataset and record what it did.

    uv run python -m evals.harness --dataset evals/datasets/atlas-eval-v1.jsonl --instructions v1

For each case: one structured triage (exactly the code path the web app's *This app* shape runs),
with a recording middleware that captures every tool call - name, arguments, result - and, for
`tool_failure` cases, replaces one tool's result with a typed failure. Nothing deployed is touched
to simulate the failure: the chaos flag would break the live ops API for everyone.

Output, one row per case, in `evals/results/<run_id>/responses.jsonl`:
`id, category, query, output, failure, tool_calls[], response (OpenAI-style message list for the
cloud evaluators), response_text, latency_s, attempts, tokens`.

This is "dataset mode" input: generate once, evaluate as many times as you like (CLAUDE.md 7.6:
the ACA orchestrator streams and suspends, so it is evaluated from traces, not as a target).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_framework import FunctionInvocationContext, FunctionMiddleware
from atlas_agent.factory import IncidentAgentRuntime, triage
from atlas_agent.schemas import IncidentTriage
from atlas_agent.tools import ToolFailure
from atlas_common.config import Settings, load_local_env
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"
FAULT_MESSAGES = {
    "unavailable": "I could not reach the {backend}.",
    "timeout": "I could not reach the {backend}: no response within 10 seconds.",
    "error": "The {backend} returned an error (HTTPStatusError).",
}
BACKEND_OF = {"get_recent_changes": "operations API", "get_telemetry": "operations API",
              "lookup_service": "operations API", "get_incident": "operations API",
              "search_knowledge": "knowledge index", "compute_blast_radius": "operations API"}


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class Recorder(FunctionMiddleware):
    """Records every tool call; optionally fails one tool with a typed ToolFailure."""

    def __init__(self, fault: dict[str, str] | None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fault = fault

    async def process(self, context: FunctionInvocationContext,
                      call_next: Callable[[], Awaitable[None]]) -> None:
        name = context.function.name
        started = time.perf_counter()
        if self._fault and self._fault["tool"] == name:
            backend = BACKEND_OF.get(name, "backend")
            context.result = ToolFailure(
                backend=backend,
                message=FAULT_MESSAGES[self._fault["mode"]].format(backend=backend))
            injected = True
        else:
            await call_next()
            injected = False
        self.calls.append({
            "name": name, "arguments": _jsonable(context.arguments),
            "result": _jsonable(context.result), "injected_fault": injected,
            "ms": round((time.perf_counter() - started) * 1000),
        })


def response_messages(calls: list[dict[str, Any]], final_text: str) -> list[dict[str, Any]]:
    """The agent's turn as the message list Foundry's agent evaluators read (API-NOTES 20.2)."""
    messages: list[dict[str, Any]] = []
    for n, call in enumerate(calls, 1):
        cid = f"call_{n}"
        messages.append({"role": "assistant", "content": [{
            "type": "tool_call", "tool_call_id": cid, "name": call["name"],
            "arguments": call["arguments"]}]})
        messages.append({"role": "tool", "tool_call_id": cid, "content": [{
            "type": "tool_result", "tool_result": call["result"]}]})
    messages.append({"role": "assistant", "content": [{"type": "text", "text": final_text}]})
    return messages


async def run_case(runtime: IncidentAgentRuntime, version: str | None,
                   row: dict[str, Any]) -> dict[str, Any]:
    agent, loaded = await runtime.agent(version)
    started = time.perf_counter()
    error = None
    infra_retries = 0
    while True:
        recorder = Recorder(row.get("fault"))
        try:
            result = await triage(agent, row["query"], agent.create_session(),
                                  middleware=[recorder])
            output = result.output
        except Exception as exc:  # a crash is a result too; the checks fail it
            error = f"{type(exc).__name__}: {exc}"[:500]
            output, result = None, None
        # An unreachable or throttled model is infrastructure noise, not agent behaviour: retry
        # the case once and say so in the record (API-NOTES 20.3).
        reason = getattr(output, "reason", None)
        if reason in ("model_unavailable", "throttled") and infra_retries == 0:
            infra_retries += 1
            await asyncio.sleep(10)
            continue
        break
    latency = round(time.perf_counter() - started, 2)
    if isinstance(output, IncidentTriage):
        value: dict[str, Any] | None = output.model_dump(mode="json")
        failure = None
        text = json.dumps(value, ensure_ascii=False)
    else:
        value = None
        failure = output.model_dump(mode="json") if output is not None else {
            "reason": "harness_error", "detail": error}
        text = f"[triage failed: {failure.get('reason')}] {failure.get('detail') or ''}".strip()
    return {
        "id": row["id"], "category": row["category"], "query": row["query"],
        "instructions_version": loaded.version, "output": value, "failure": failure,
        "tool_calls": recorder.calls, "response": response_messages(recorder.calls, text),
        "response_text": text, "latency_s": latency,
        "attempts": result.attempts if result else 0, "infra_retries": infra_retries,
        "input_tokens": result.input_tokens if result else None,
        "output_tokens": result.output_tokens if result else None,
    }


async def run(rows: list[dict[str, Any]], version: str | None,
              concurrency: int) -> list[dict[str, Any]]:
    gate = asyncio.Semaphore(concurrency)
    done: list[dict[str, Any]] = []
    async with IncidentAgentRuntime(Settings.from_env()) as runtime:

        async def one(row: dict[str, Any]) -> None:
            async with gate:
                rec = await run_case(runtime, version, row)
                done.append(rec)
                status = "ok" if rec["output"] else (rec["failure"] or {}).get("reason")
                print(f"  {rec['id']:<28} {status:<16} {rec['latency_s']:>6.1f}s "
                      f"tools={len(rec['tool_calls'])}", flush=True)

        await asyncio.gather(*(one(r) for r in rows))
    order = {r["id"]: i for i, r in enumerate(rows)}
    done.sort(key=lambda r: order[r["id"]])
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", type=Path,
                        default=ROOT / "evals" / "datasets" / "atlas-eval-v1.jsonl")
    parser.add_argument("--instructions", default=None, help="v0 | v1 (default: active)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--only", nargs="*", help="case ids or categories")
    args = parser.parse_args()
    load_local_env()
    run_id = args.run_id or (f"{datetime.now(UTC):%Y%m%d-%H%M%S}-"
                             f"{args.instructions or 'active'}-{uuid.uuid4().hex[:4]}")
    rows = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if args.only:
        rows = [r for r in rows if r["id"] in args.only or r["category"] in args.only]
    (RESULTS / run_id).mkdir(parents=True, exist_ok=True)
    done = asyncio.run(run(rows, args.instructions, args.concurrency))
    path = RESULTS / run_id / "responses.jsonl"
    path.write_bytes("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                             for r in done).encode())
    print(f"responses: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
