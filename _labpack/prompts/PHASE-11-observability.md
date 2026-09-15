# PHASE 11 — Observability and distributed tracing (Episode 4)

> Depends on: Phase 10.
> Episode 4 topics: distributed tracing, Application Insights, monitoring.

---

Make the whole system legible. If you cannot answer "why did the agent do that?" from a trace in
under 60 seconds, this phase is not done.

## Before you write code

Query MS Learn MCP for the current Foundry tracing setup (project → Application Insights
connection), client-side tracing for agents, the Agent Framework observability integration, and the
GenAI OpenTelemetry semantic conventions Foundry uses. Confirm the environment variables that
control content recording for your versions and record them.

## 1. One telemetry module

`src/atlas_common/telemetry.py` — used identically by web, orchestrator, worker and functions:

```python
def configure_telemetry(service_name: str, *, capture_content: bool) -> None:
    # 1. configure_azure_monitor(connection_string=...) from APPLICATIONINSIGHTS_CONNECTION_STRING
    # 2. enable GenAI/agent-framework instrumentation
    # 3. set resource attributes: service.name, service.version (git sha), deployment.environment
    # 4. set content-recording env vars ONLY when capture_content is true
```

Content recording (prompts and completions in spans) is **on in the demo environment, off by
default elsewhere**, driven by `ATLAS_CAPTURE_CONTENT`. Log a warning at startup when it is on and
say why on stage — an audience should hear you make that trade-off consciously.

## 2. Span taxonomy

Every span carries these attributes where applicable:

| Attribute | Example |
|---|---|
| `atlas.case_id` | `CASE-8f21…` |
| `atlas.session_id` | |
| `atlas.node` | `triage-agent`, `policy-gate`, `dispatch` |
| `atlas.instructions_version` | `v1` |
| `atlas.agent_version` | hosted agent version, or git sha for ACA |
| `atlas.plan_id` | |
| `atlas.decision` | `approval_required`, `auto_approved`, `blocked` |
| `atlas.degraded` | comma-separated |
| `gen_ai.*` | from the framework instrumentation — do not hand-roll these |

Add a `SpanEnricher` so these are attached once, not sprinkled through business logic.

## 3. Propagation checklist

- Browser → web: `traceparent` generated server-side per turn, returned in the SSE stream.
- Web → orchestrator: header.
- Orchestrator → APIM → Functions: header, and APIM must forward it (Phase 09 global policy).
- Orchestrator → Service Bus → worker: application property, extracted and **linked**.
- Logic App: pass `traceparent` through the HTTP trigger and back on the callback; correlate by
  `case_id` where the connector drops it, and say so honestly rather than pretending it is seamless.
- Hosted agent: server-side traces from Foundry plus your client spans — verify they correlate.

Write `scripts/trace_check.py <case_id>` that queries Application Insights (Log Analytics API) and
asserts a single `operation_Id` spans all expected components, printing the waterfall as text.
Wire it into `make trace-check` and into `verify-all`.

## 4. Application Insights assets

`infra/modules/observability.bicep` fills in:

### Workbook `ATLAS Agent Operations`
Sections:
1. **Volume and latency** — turns/min, p50/p95/p99 end-to-end, per node.
2. **Tool health** — call count, error rate, p95 duration per tool; a tool-failure heat map.
3. **Model usage** — tokens in/out per case and per agent (from `llm-emit-token-metric`), cost proxy.
4. **Decisions** — auto-approved vs approval-required vs blocked over time; approval latency.
5. **Remediation** — commands dispatched, succeeded, retried, dead-lettered.
6. **Quality** — latest eval scorecard (Phase 12 writes results to a custom table or to the
   `eval-runs` container surfaced via a workbook query).
7. **Safety** — guardrail blocks, refusals, injection-canary triggers.

Keep every KQL query in `infra/queries/*.kql` and reference them from the workbook definition so
they are reviewable in a PR.

### Alerts (action group `ag-atlas-ops`)
| Alert | Condition |
|---|---|
| Tool error rate | > 10 % over 5 min for any tool |
| End-to-end p95 | > 45 s over 10 min |
| Model throttling | 429 count > 20 over 5 min |
| Token burn | tokens/hour > threshold |
| DLQ depth | `remediation-commands/$DeadLetterQueue` active messages > 0 |
| Approval timeouts | > 2 in 30 min |
| Guardrail blocks | any injection-canary trigger reaching a tool call |
| Eval regression | latest eval composite score < baseline − 5 % |

### Availability test
A standard availability test against `ca-atlas-web/api/health` from three regions.

## 5. Foundry-side observability

- Confirm traces appear under the project's tracing view for prompt agent, hosted agent and the
  Agent Framework app.
- Enable the agent monitoring dashboard if available for your project; screenshot it.
- Document which signals come from Foundry server-side vs your client-side instrumentation — Episode
  4 should be honest about the seam.

## Verification — `scripts/verify_phase11.sh`

1. `make trace-check CASE=<canonical>` prints a waterfall containing web, orchestrator, each agent
   node, APIM, both function apps, Service Bus and the worker, under one `operation_Id`.
2. Every span in that trace carries `atlas.case_id`.
3. The workbook deploys and all seven sections return data.
4. Each alert rule exists and is enabled; fire one deliberately (DLQ) and confirm the action group
   receives it.
5. Content recording is on in the demo env and the setting is visible in the app's startup log.
6. The availability test is green from all three regions.

## Acceptance criteria

- [ ] One trace, six hops, zero broken links.
- [ ] Workbook is the single screen you can open during any episode to explain what happened.
- [ ] Alerts are real and one has been proven to fire.
- [ ] KQL lives in source control.

## Report back

The text waterfall for the canonical case, the workbook URL, and the list of enabled alert rules.
