"""Episode 1: the ATLAS incident agent, running locally, streaming, multi-turn.

    uv run python -m scripts.demo_agent_local                   # canonical incident, then follow-up
    uv run python -m scripts.demo_agent_local --version v0      # the vague instructions
    uv run python -m scripts.demo_agent_local --resume <id>     # continue a session from Cosmos
    uv run python -m scripts.demo_agent_local --json            # machine-readable, for verification

What the audience sees: every tool call as it happens, the validated `IncidentTriage`, then a
follow-up question that refers to "the service that caused this" without naming it. The session is
saved to Cosmos between turns, so `--resume` in a brand-new process continues the same conversation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid

from agent_framework import AgentResponseUpdate
from atlas_agent.factory import IncidentAgentRuntime, answer, triage
from atlas_agent.schemas import IncidentTriage
from atlas_common.config import Settings
from atlas_common.telemetry import configure_telemetry, span, trace_id

CANONICAL_REPORT = (
    "Checkout is timing out for about 20% of EU customers since 14:05, "
    "started right after the payments deploy."
)
FOLLOW_UP = "Is the service that caused this under a change freeze, and which team owns it?"


def _console_utf8() -> None:
    """Windows consoles default to cp1252 and print an em dash as a replacement glyph."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


class ActivityPrinter:
    """Prints tool calls and results as they stream in, once per call."""

    def __init__(self, show_text: bool) -> None:
        self._named: set[str] = set()
        self._show_text = show_text
        self.tool_calls: list[str] = []

    def __call__(self, update: AgentResponseUpdate) -> None:
        for content in update.contents:
            if content.type == "function_call" and content.name and content.call_id:
                if content.call_id not in self._named:
                    self._named.add(content.call_id)
                    self.tool_calls.append(content.name)
                    print(f"  -> {content.name}", flush=True)
            elif content.type == "text" and self._show_text and content.text:
                print(content.text, end="", flush=True)


def _print_triage(triage_out: IncidentTriage) -> None:
    print(f"\n  severity     {triage_out.severity}   category {triage_out.category}")
    print(f"  affected     {', '.join(triage_out.affected_services)}")
    print(f"  suspected    {', '.join(triage_out.suspected_change_ids) or 'none'}")
    print(f"  confidence   {triage_out.confidence:.2f}   needs_human {triage_out.needs_human}")
    print(f"  citations    {', '.join(c.doc_id for c in triage_out.citations)}")
    print(f"\n  {triage_out.summary}\n")


async def main(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    exporting = configure_telemetry("atlas-agent-local")
    report: dict[str, object] = {}
    with span("demo_agent_local", session=args.resume, instructions=args.version) as root:
        code = await _main(args, settings, report)
        root.set_attribute("atlas.exit_code", code)
        report["trace_id"] = trace_id()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    elif code == 0:
        where = "Application Insights" if exporting else "nowhere (no connection string)"
        print(f"  trace {report['trace_id']} -> {where}")
    return code


async def _main(args: argparse.Namespace, settings: Settings, report: dict[str, object]) -> int:
    async with IncidentAgentRuntime(settings) as runtime:
        agent, loaded = await runtime.agent(args.version)
        session_id = args.resume or f"demo-{uuid.uuid4().hex[:12]}"
        if not args.json:
            print(f"ATLAS incident agent  instructions={loaded.version} ({loaded.source})")
            print(f"session {session_id}\n")

        session = await runtime.sessions.load(session_id) if args.resume else None
        if args.resume and session is None:
            print(f"no session '{session_id}' in Cosmos (expired or never saved)", file=sys.stderr)
            return 2
        session = session or agent.create_session()

        if not args.resume:
            if not args.json:
                print(f"REPORT  {args.report}\n")
            printer = ActivityPrinter(show_text=False)
            started = time.perf_counter()
            result = await triage(
                agent, args.report, session, on_update=None if args.json else printer
            )
            elapsed = time.perf_counter() - started
            await runtime.sessions.save(session_id, session)
            report["triage"] = result.output.model_dump(mode="json")
            report["triage_seconds"] = round(elapsed, 1)
            report["tool_calls"] = result.tool_calls
            report["tokens"] = {"input": result.input_tokens, "output": result.output_tokens}
            if not args.json:
                if isinstance(result.output, IncidentTriage):
                    _print_triage(result.output)
                else:
                    print(f"\n  triage failed: {result.output.reason} - {result.output.detail}\n")
                print(f"  {elapsed:.1f}s, {len(result.tool_calls)} tool calls, "
                      f"{result.input_tokens} input tokens\n")

        if args.follow_up:
            # Rebuild the session from Cosmos first: proves the conversation survives a restart.
            restored = await runtime.sessions.load(session_id)
            if restored is None:
                print("session was not persisted", file=sys.stderr)
                return 2
            if not args.json:
                print(f"FOLLOW-UP  {args.follow_up}\n")
            printer = ActivityPrinter(show_text=not args.json)
            turn = await answer(
                agent, args.follow_up, restored, on_update=None if args.json else printer
            )
            await runtime.sessions.save(session_id, restored)
            report["follow_up"] = turn.text
            report["follow_up_tool_calls"] = turn.tool_calls
            report["follow_up_failure"] = turn.failure
            if not args.json:
                print(f"\n  [{turn.failure}] {turn.text}\n" if turn.failure else "\n")

        report["session_id"] = session_id
        report["instructions"] = {"version": loaded.version, "source": loaded.source}
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--report", default=CANONICAL_REPORT)
    parser.add_argument("--follow-up", default=FOLLOW_UP, help="empty string to skip")
    parser.add_argument("--version", choices=["v0", "v1"], default=None)
    parser.add_argument("--resume", metavar="SESSION_ID", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--scenario", choices=["canonical"], default="canonical")
    return parser.parse_args(argv)


if __name__ == "__main__":
    _console_utf8()
    sys.exit(asyncio.run(main(parse_args())))
