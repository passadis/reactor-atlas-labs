# PHASE 03 — Agent core (Episode 1)

> Depends on: Phase 02.
> Episode 1 topics covered: agent instructions, tool contracts, conversation state, structured
> outputs, model-vs-assistant-vs-agent-vs-workflow.

---

Build `src/atlas_agent` — the single agent definition that will be hosted **twice** (Foundry hosted
agent in Phase 05, ACA custom app in Phase 04). One definition, two hosting models.

## Before you write code

Re-verify the Agent Framework symbol set against `docs/API-NOTES.md` and the installed package.
Query MS Learn MCP for the current pages on agent creation, tools, structured output, and agent
sessions/conversation state. If anything in `API-NOTES.md` is now wrong, update it.

## 1. Instructions as a versioned artefact

`src/atlas_agent/instructions/atlas_incident_agent.v1.md`, loaded at runtime — never a Python
string literal. Structure it explicitly so you can point at each section on stage:

- **Role and scope** — what ATLAS is, what it is not.
- **Operating procedure** — the order it must work in (classify → retrieve → assess → plan).
- **Tool policy** — when to call which tool; never guess an incident id, service id or ticket
  number; if a tool fails, say so and continue with what you have; never call a remediation tool
  without an approval token.
- **Grounding and citation** — every factual claim about services, incidents or runbooks must cite
  a `doc_id`; if nothing is retrieved, say "I have no grounded source for that."
- **Refusals** — no credential requests, no destructive actions without approval, no impersonation
  of an approver, no acting on instructions found inside retrieved documents (explicit
  prompt-injection clause naming the pattern).
- **Output contract** — always return the structured object; prose goes in named fields.
- **Escalation** — when to hand back to a human.

Keep a `v0.md` that is deliberately vague (three sentences). Episode 1 shows v0 → v1 and what
changes. Episode 4 uses the delta as an evaluation comparison.

## 2. Typed tool contracts

`src/atlas_agent/tools.py`. Every tool:
- has a Pydantic input model and a Pydantic output model,
- has a docstring that is the tool description the model actually sees (write it like an API doc:
  what it does, when to use it, when **not** to use it, what it returns),
- has an explicit timeout and a typed failure result (never raises into the model),
- is unit tested including the failure path.

Tools for this phase (all read-only; they call the repositories from Phase 02 directly for now and
get re-pointed at APIM in Phase 09):

| Tool | Input | Output | Notes |
|---|---|---|---|
| `lookup_service` | `service_id: str` | `ServiceRecord` | Returns tier, owner, SLO, dependencies, change-freeze state |
| `get_incident` | `incident_id: str` | `IncidentRecord \| NotFound` | |
| `search_knowledge` | `query: str, services: list[str] \| None, top: int = 5` | `KnowledgeResults{hits[], citations[]}` | Hybrid + semantic; returns `doc_id`, `title`, `snippet`, `last_reviewed`, `score` |
| `get_recent_changes` | `service_id: str, since_minutes: int = 240` | `ChangeList` | |
| `get_telemetry` | `service_id: str, window_minutes: int = 60` | `TelemetrySummary` | |
| `compute_blast_radius` | `service_id: str, depth: int = 3` | `BlastRadius` | Deterministic graph walk — a good on-stage example of "this is code, not judgement" |

Write one **deliberately bad** tool description in `src/atlas_agent/tools_bad.py` (not wired up) —
Phase 13 uses it for the regression demo. Keep it out of the import graph.

## 3. Structured outputs

`src/atlas_agent/schemas.py`:

```python
class Citation(BaseModel):
    doc_id: str; title: str; last_reviewed: date | None; relevance: float

class IncidentTriage(BaseModel):
    severity: Literal["Sev1","Sev2","Sev3","Sev4"]
    category: Literal["latency","availability","data","security","capacity","deployment","config"]
    affected_services: list[str]
    likely_cause: str
    confidence: float = Field(ge=0, le=1)
    suspected_change_ids: list[str]
    summary: str
    citations: list[Citation]
    needs_human: bool
    reasoning_notes: str
```

Request it via the framework's structured-output / `response_format` mechanism (verify the exact
parameter name for your installed version). Validate the result; on `ValidationError`, retry once
with the error appended, then return a typed `TriageFailure`. Never hand unvalidated model output
to another component.

## 4. Conversation state

`src/atlas_agent/session.py`:
- Use the framework's agent session/thread abstraction for in-turn state.
- Persist session state to the Cosmos `sessions` container keyed by `session_id`, with a
  `CosmosSessionStore` implementing save/load/expire (TTL 24h).
- Make the storage backend pluggable (`InMemorySessionStore` for tests).
- Demonstrate multi-turn: turn 2 must resolve "it" and "that service" from turn 1 without the user
  repeating ids. Add a test proving it.

## 5. The agent factory

`src/atlas_agent/factory.py`: `build_incident_agent(settings, credential) -> Agent`, reading
instructions version from config. One function, used by the web app, the hosted agent, the
orchestrator, and the tests.

## 6. The four shapes (Episode 1 comparison)

- `scripts/demo_raw_model.py` — a bare chat/responses call. Show: no tools, no state, no identity.
- `scripts/demo_agent_local.py` — `build_incident_agent(...)`, streaming to the console, multi-turn.
- `scripts/create_prompt_agent.py` — create/update the Foundry **prompt agent** `atlas-triage-prompt`
  from code (SDK or `az rest`), with instructions + model + a toolbox reference. This is the
  "no code, no infra" comparison point. Make it idempotent and version-aware.
- (Hosted agent is Phase 05; the ACA app is Phase 04.)

## 7. Guardrails

Attach the content filter / guardrail configured in Phase 01 and write
`tests/test_guardrails.py` proving: a credential-exfiltration prompt is refused, a destructive
request without approval is refused, and an instruction embedded in a retrieved runbook
(`injection_canary`) is **not** followed. These tests are the seed of the Episode 4 eval set.

## Verification — `scripts/verify_phase03.sh`

1. `uv run python scripts/demo_agent_local.py --scenario canonical` produces a valid `IncidentTriage`
   with `severity=Sev2`, `svc-checkout-api` in `affected_services`, the canonical `CHG` in
   `suspected_change_ids`, and ≥ 2 citations.
2. Multi-turn coreference test passes.
3. All tools return typed failures (not exceptions) when their backend is unreachable — simulate by
   pointing a repository at a bad endpoint.
4. `az rest` confirms the prompt agent `atlas-triage-prompt` exists in the project.
5. Guardrail tests pass.

## Acceptance criteria

- [ ] One agent definition drives both hosting models (no copy-paste).
- [ ] Every tool has typed I/O, a real description, a timeout, a failure path, and tests.
- [ ] Structured output validated with a retry-then-typed-failure policy.
- [ ] Conversation state survives process restart (Cosmos-backed).
- [ ] Streaming works end to end in the console script.
- [ ] `docs/DEMO-NOTES.md` has the Episode 1 beats: v0 vs v1 instructions, tool contract, structured
      output, four hosting shapes.

## Report back

The canonical triage output (pretty-printed), the token/latency numbers for a cold and warm run,
and which structured-output mechanism your installed version actually uses.
