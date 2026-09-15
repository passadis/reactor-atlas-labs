# PHASE 06 — Multi-agent workflow (Episode 2)

> Depends on: Phase 04.
> Episode 2 topics: orchestration patterns, sequential/concurrent/handoff, agentic vs deterministic.

---

Build `src/atlas_orchestrator`: the Agent Framework **workflow** that turns one agent into a
coordinated incident-management system. Phase 07 adds state durability, checkpoints and the human
approval gate; this phase gets the graph running.

## Before you write code

Query MS Learn MCP for the current Agent Framework workflow pages: `WorkflowBuilder`, executors and
message handlers, the execution model (supersteps), and the three orchestration builders
(sequential, concurrent, handoff). Confirm the exact class and decorator names against the installed
package and update `docs/API-NOTES.md`.

## 1. Specialist agents

Each gets its own instructions file under `src/atlas_orchestrator/instructions/` and its own
structured output. Keep them narrow — that is the lesson.

| Agent | Responsibility | Output model | Tools |
|---|---|---|---|
| `triage_agent` | Severity, category, affected services, confidence | `TriageResult` | `lookup_service`, `get_recent_changes`, `get_telemetry` |
| `knowledge_agent` | Retrieve runbooks + similar past incidents, extract applicable steps, flag staleness | `KnowledgeResult` | `search_knowledge`, `get_incident` |
| `impact_agent` | Blast radius, customer impact, SLO/error-budget burn, business tier | `ImpactResult` | `compute_blast_radius`, `get_telemetry`, `lookup_service` |
| `planner_agent` | Draft an ordered remediation plan with risk, duration, rollback | `RemediationPlan` | read-only tools only — **it may not execute anything** |

`RemediationPlan`:

```python
class RemediationStep(BaseModel):
    order: int; action: str; target_service: str
    command_ref: str                    # references a runbook step id, never free-form shell
    destructive: bool; expected_result: str; rollback: str

class RemediationPlan(BaseModel):
    plan_id: str                        # deterministic: sha256(case_id + normalized steps)[:16]
    steps: list[RemediationStep]
    risk: Literal["low","medium","high"]
    estimated_minutes: int
    requires_approval: bool             # the agent's OPINION; the policy gate decides
    justification: str
    citations: list[Citation]
```

Note the split explicitly in code comments: `requires_approval` from the planner is advisory.
`policy_gate` (Phase 07) is authoritative.

## 2. Deterministic executors

Plain code, no model:

- `IntakeExecutor` — validate input, extract/resolve candidate service ids against the CMDB
  (fuzzy match with a confidence threshold; below threshold, ask the user), stamp `case_id`,
  load initial context. Rejects malformed input before a single token is spent.
- `AggregateExecutor` — fan-in. Merge `TriageResult`, `KnowledgeResult`, `ImpactResult` into the
  typed `IncidentCase` state. Resolve conflicts with explicit rules (e.g. severity = max of triage
  severity and impact-derived severity) and record *why* in `state.decisions`.
- `ReportExecutor` — build the final `IncidentReport` from state. No model call; the prose fields
  were already produced by the agents.

## 3. The three orchestration patterns — all three, switchable

Build one workflow that contains all three so you can show them without redeploying:

```
intake → triage_agent                       [SEQUENTIAL]
       → { knowledge_agent ‖ impact_agent } [CONCURRENT fan-out/fan-in]
       → aggregate
       → planner_agent
       → policy_gate  (Phase 07)
       → request_info (Phase 07)
       → dispatch     (Phase 10)
       → report
```

Plus a **handoff** orchestration used for interactive follow-up: a `front_desk` agent that can hand
off to `knowledge_agent` or `impact_agent` for multi-turn Q&A on an existing case, returning control
to the user between turns. Expose it at `POST /cases/{case_id}/ask`.

Config flag `ATLAS_ORCHESTRATION_MODE = graph | sequential_only | handoff` so you can degrade live
if something misbehaves, and so Episode 2 can run the same incident through each shape.

## 4. Service surface

`src/atlas_orchestrator/api.py` (FastAPI, internal ingress only):

| Route | Purpose |
|---|---|
| `POST /cases` | Start a run. Returns `case_id`. Accepts a `traceparent`. |
| `GET /cases/{case_id}/stream` | SSE of workflow events: executor started/completed, agent deltas, state transitions, request-info raised. |
| `GET /cases/{case_id}` | Current typed state + decision log. |
| `POST /cases/{case_id}/ask` | Handoff orchestration turn. |
| `GET /cases/{case_id}/graph` | Workflow topology as JSON **and** as Mermaid, for the on-stage diagram. |
| `GET /api/health` | |

`GET /cases/{case_id}/graph` returning live Mermaid is worth the effort: render it in the web app so
Episode 2 opens with the real graph, not a slide.

## 5. Failure boundaries

- Every agent node has a timeout and a **fallback**: on failure, the aggregate executor proceeds
  with a `Degraded` marker for that dimension and the report says which analysis is missing.
  Losing the impact agent must not lose the case.
- Model 429s: bounded retry with jitter, then degrade. Surface retry counts as span attributes.
- Every executor is pure with respect to external state except through repositories, so it can be
  replayed.

## 6. Tests

- Unit: each executor with frozen inputs.
- Contract: every agent's structured output validates against its schema for 10 varied inputs.
- Integration: the full graph against deployed Azure resources for the canonical incident, asserting
  the frozen expectations from `tests/fixtures/`.
- Chaos: kill `knowledge_agent` (point it at a bad endpoint) and assert the run still completes with
  `Degraded`.

## Verification — `scripts/verify_phase06.sh`

1. `azd deploy orchestrator` green; ACA app `Running`.
2. `POST /cases` with the canonical incident returns a case that reaches `plan_ready` in < 60 s.
3. `GET /cases/{id}` shows populated `TriageResult`, `KnowledgeResult`, `ImpactResult`,
   `RemediationPlan`, and a non-empty `decisions` log.
4. The concurrent branch actually runs concurrently — assert wall time < sum of the two branch
   durations, using span timings.
5. All three orchestration modes complete for the canonical incident.
6. Chaos test passes (degraded, not failed).
7. `GET /cases/{id}/graph` returns valid Mermaid that renders.

## Acceptance criteria

- [ ] Four specialist agents, each with narrow instructions and a typed output.
- [ ] Sequential, concurrent and handoff all demonstrably working against the same case.
- [ ] Deterministic executors contain zero model calls, and this is asserted by a test that patches
      the chat client and fails if it is called.
- [ ] A dead specialist degrades the run rather than killing it.
- [ ] `docs/DEMO-NOTES.md` has the Episode 2 first half: the graph, the pattern switch, the
      agentic/deterministic split.

## Report back

The Mermaid graph, wall-clock timings per node, and the decision log for the canonical incident.
