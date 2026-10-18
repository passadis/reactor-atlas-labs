"""One case's trace told as a story: one line per step a person cares about, in plain words.

    uv run python -m scripts.trace_story <case_id>        # or: make trace-story CASE=<id>

`make trace-check` prints every span (about 230 lines for one case) and asserts the hops - the
engineer's view. This prints the same trace for an audience: who did what, how long it took, what
the code decided and why, and where the work went next. Built from the same telemetry: the case's
spans (timings, model calls, tool calls) and its business events (`policy.decision`,
`approval.answered`, `remediation.dispatched`, `remediation.outcome`).

Read-only. Telemetry lands 1-3 minutes after the fact: run the case BEFORE the beat.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from typing import Any

from atlas_common.config import load_local_env

from scripts.trace_check import AppInsights, Span, _quote, build_tree, find_operation, load_spans

EVENTS = ("policy.decision", "approval.answered", "approval.expired", "remediation.dispatched",
          "remediation.outcome", "tool.failure", "guardrail.block", "injection.canary_retrieved")
AGENTS = {"triage_agent": "TRIAGE", "knowledge_agent": "KNOWLEDGE", "impact_agent": "IMPACT",
          "planner_agent": "PLANNER"}
SAYS = {
    "TRIAGE": "what is broken, how bad, what changed",
    "KNOWLEDGE": "which runbook and past incidents apply",
    "IMPACT": "what else this takes down",
    "PLANNER": "drafts the remediation plan",
}


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _descendants(span: Span) -> list[Span]:
    out: list[Span] = []
    stack = list(span.children)
    while stack:
        s = stack.pop()
        out.append(s)
        stack.extend(s.children)
    return out


def _tools(span: Span) -> str:
    calls = Counter(s.name.removeprefix("execute_tool ") for s in _descendants(span)
                    if s.name.startswith("execute_tool "))
    return ", ".join(f"{n} x{c}" if c > 1 else n for n, c in calls.most_common()) or "none"


def _models(span: Span) -> int:
    return sum(1 for s in _descendants(span) if s.name.startswith("chat "))


def _first(spans: list[Span], name: str) -> Span | None:
    return next((s for s in spans if s.name == name), None)


def story(case_id: str) -> int:
    load_local_env()
    ai = AppInsights()
    op = find_operation(ai, case_id)
    if not op:
        print(f"no trace found for {case_id} (telemetry lands 1-3 minutes after the run)")
        return 1
    spans = load_spans(ai, op)
    build_tree(spans)
    t0 = _ts(str(ai.query(f"union requests, dependencies | where operation_Id == {_quote(op)} "
                          "| summarize t0 = min(timestamp)")[0]["t0"]))
    events = ai.query(f"""traces
        | where tostring(customDimensions["atlas.case_id"]) == {_quote(case_id)}
        | where message in ({", ".join(_quote(e) for e in EVENTS)})
        | project timestamp, message, customDimensions | order by timestamp asc""")

    def ev(name: str) -> tuple[float, dict[str, Any]] | None:
        for e in events:
            if e["message"] == name:
                cd = e["customDimensions"]
                cd = json.loads(cd) if isinstance(cd, str) else cd
                return _ts(str(e["timestamp"])) - t0, {k.removeprefix("atlas."): v
                                                       for k, v in cd.items()}
        return None

    lines: list[tuple[float, str, str]] = []

    def add(at: float, what: str, detail: str) -> None:
        lines.append((at, what, detail))

    web = next((s for s in spans if s.role.endswith("atlas-web")), None)
    add(0.0, "REPORT", "the engineer's report enters through the web app"
        + (f" ({web.name})" if web else ""))
    intake = _first(spans, "executor.process intake")
    if intake:
        add(intake.start_ms / 1000, "INTAKE (code)",
            f"resolves the services named in the report - {intake.duration_ms / 1000:.1f} s")
    nodes = {s.name.removeprefix("atlas.node."): s for s in spans
             if s.name.startswith("atlas.node.")}
    k, i = nodes.get("knowledge_agent"), nodes.get("impact_agent")
    overlap = k is not None and i is not None and abs(k.start_ms - i.start_ms) < 2000
    for key, label in AGENTS.items():
        s = nodes.get(key)
        if s is None:
            continue
        tag = " [parallel]" if overlap and key in ("knowledge_agent", "impact_agent") else ""
        add(s.start_ms / 1000, f"{label} (agent){tag}",
            f"{SAYS[label]} - {s.duration_ms / 1000:.1f} s, {_models(s)} model calls, "
            f"tools: {_tools(s)}")
        if key == "impact_agent" and overlap and k is not None:
            saved = (k.duration_ms + s.duration_ms - max(k.duration_ms, s.duration_ms)) / 1000
            add(s.start_ms / 1000, "", f"knowledge and impact ran side by side: {saved:.1f} s "
                "saved over running them one after the other")
    agg = _first(spans, "executor.process aggregate")
    if agg:
        add(agg.start_ms / 1000, "AGGREGATE (code)",
            "merges the three analyses by fixed rules (severity = max of triage and impact)")
    gate = ev("policy.decision")
    gate_span = _first(spans, "executor.process policy_gate")
    if gate or gate_span:
        at = gate[0] if gate else (gate_span.start_ms / 1000 if gate_span else 0)
        d = gate[1] if gate else {}
        decision = str(d.get("decision", "decided")).replace("_", " ")
        roles = f" - {d['roles']} must approve" if d.get("roles") else ""
        add(at, "POLICY GATE (code)", f"{decision}{roles}. No model call: rules in policy.py")
    logic = next((s for s in spans if s.role.startswith("logic-")), None)
    if logic:
        add(logic.start_ms / 1000, "LOGIC APP", "approval workflow starts: notifies approvers, "
            "posts the console link, waits for the decision (and expires it after 30 min)")
    answered = ev("approval.answered")
    if answered:
        at, d = answered
        verdict = "APPROVED" if str(d.get("approved")).lower() == "true" else "REJECTED"
        add(at, "HUMAN", f"{verdict} by a {d.get('role')}, {d.get('latency_s')} s after the gate "
            "asked - checked against role, plan id and expiry before it counts")
    expired = ev("approval.expired")
    if expired:
        add(expired[0], "HUMAN", "nobody answered in time - the case expired, nothing ran")
    dispatched = ev("remediation.dispatched")
    disp = _first(spans, "atlas.dispatch")
    if dispatched or disp:
        # The span's start, not the event's: the event is logged once the submit has returned.
        at = disp.start_ms / 1000 if disp else (dispatched[0] if dispatched else 0)
        d = dispatched[1] if dispatched else {}
        add(at, "DISPATCH (code)", f"opens ticket {d.get('ticket_id', '?')}, mints a single-use "
            f"approval token, submits operation {d.get('operation_id', '?')} through APIM")
    enqueue = _first(spans, "atlas.remediation.enqueue")
    if enqueue:
        add(enqueue.start_ms / 1000, "OPS API (Function)",
            "checks the token is genuine and unspent, spends it, puts the command on Service Bus")
    worker = _first(spans, "atlas.worker.remediation")
    progress = sum(1 for s in spans if s.role.endswith("atlas-worker")
                   and s.name == "ServiceBus.send")
    outcome = ev("remediation.outcome")
    if worker:
        d = outcome[1] if outcome else {}
        add(worker.start_ms / 1000, "WORKER",
            f"takes the command off the queue, runs {d.get('steps', '?')} steps one by one, "
            f"publishes {progress} progress events - {worker.duration_ms / 1000:.1f} s")
    if outcome:
        at, d = outcome
        add(at, "DONE", f"remediation {d.get('status')} after {d.get('deliveries')} delivery "
            "attempt(s); the case is closed with its ticket and every step's result")
    for name, label in (("tool.failure", "a tool failed (typed failure, the run continued)"),
                        ("guardrail.block", "a guardrail blocked content"),
                        ("injection.canary_retrieved", "a planted instruction was retrieved - "
                                                       "flagged, not obeyed")):
        hit = ev(name)
        if hit:
            add(hit[0], "NOTE", label)

    roles = sorted({s.role.split(" ")[0].removeprefix("atlas.") for s in spans if s.role})
    print(f"{case_id} - the whole journey is ONE trace ({op}), {len(spans)} spans, "
          f"{len(roles)} components\n")
    for at, what, detail in sorted(lines, key=lambda x: x[0]):
        print(f"  t+{at:6.1f}s  {what:28} {detail}")
    print(f"\n  components on this one trace: {', '.join(roles)}")
    print("  the full waterfall: make trace-check CASE=" + case_id)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("case_id")
    args = parser.parse_args()
    return story(args.case_id)


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
