# Project ATLAS — Architecture

## The scenario

**Contoso Cloud Operations** runs ~25 internal services. When an alert fires or an engineer reports
a problem, ATLAS:

1. takes the report in natural language,
2. enriches it from the CMDB and telemetry,
3. triages it (severity, category, affected services),
4. retrieves the relevant runbooks and similar past incidents,
5. assesses blast radius and SLO impact,
6. drafts a remediation plan,
7. **stops** and asks a human to approve anything that touches production,
8. on approval, submits the remediation as a long-running, idempotent command,
9. reports back with a cited, structured summary and a ticket number.

Steps 1, 8 and the approval gate are deterministic code. Steps 3–6 are agents. That split is the
spine of the whole series.

---

## Component map

```
                         ┌───────────────────────────────────────────┐
  Browser ──SSE──▶ ca-atlas-web (FastAPI)                            │
                         │  · chat + streaming                        │
                         │  · approval console                        │
                         └───────┬───────────────────────────────────┘
                                 │ HTTP (+traceparent)
                                 ▼
                    ca-atlas-orchestrator  (Agent Framework Workflow)
                    ├─ intake            [deterministic executor]
                    ├─ triage_agent      [agent]
                    ├─ knowledge_agent ──┐ concurrent
                    ├─ impact_agent    ──┘
                    ├─ aggregate         [deterministic executor, typed state]
                    ├─ planner_agent     [agent]
                    ├─ policy_gate       [deterministic rules engine]
                    ├─ request_info(...) [HUMAN APPROVAL — workflow pauses]
                    ├─ dispatch          [deterministic, idempotent]
                    └─ report            [structured output]
                                 │
        ┌────────────────────────┼─────────────────────────┬──────────────────────┐
        │                        │                         │                      │
        ▼                        ▼                         ▼                      ▼
  Foundry project          apim-atlas               sb-atlas             cosmos-atlas
  · gpt-main               (AI gateway)             · remediation-        · incidents
  · gpt-judge              · model API               commands (sessions)  · tickets
  · embed-main               (token limit,          · remediation-        · approvals
  · Toolbox (MCP)            token metrics,           results             · workflow-checkpoints
  · prompt agent             content safety)        · remediation-        · idempotency
  · hosted agent           · atlas-ops-api            events (topic)      · sessions
  · tracing → App Ins.       (OpenAPI → MCP)              │
                           · atlas-ops-mcp                │
                             (MCP passthrough)            ▼
                                 │                  ca-atlas-worker
                    ┌────────────┴────────────┐     (executes remediation,
                    ▼                         ▼      emits progress, writes result)
            func-atlas-api            func-atlas-mcp
            (business REST API,       (MCP tool trigger
             OpenAPI 3.0)              server)
                    │
                    ▼
            logic-atlas (Standard)
            · approve-remediation  (long-running, webhook callback)
            · enrich-incident      (exposed as a Foundry agent tool)

  srch-atlas (AI Search)  ← runbooks + past-incident index, vector + semantic
  appi-atlas (App Insights) ← every hop; Foundry tracing lands here too
  kv-atlas (Key Vault)    ← the one permitted secret
  id-atlas-*              ← user-assigned managed identities
```

---

## Azure resource inventory

| # | Resource | SKU / tier | Why this one |
|---|---|---|---|
| 1 | Microsoft Foundry account + project | S0 | Agent Service, toolboxes, tracing, evaluation, red teaming |
| 2 | Model deployment `gpt-main` | GlobalStandard, 200K TPM | Main reasoning model. Alias, not a raw model name. |
| 3 | Model deployment `gpt-judge` | GlobalStandard, 100K TPM | LLM-as-judge for evaluators; isolated so eval runs never starve the demo |
| 4 | Model deployment `embed-main` | Standard | Embeddings for AI Search vectors |
| 5 | Log Analytics workspace | PerGB2018 | |
| 6 | Application Insights | workspace-based | Connected to the Foundry project for agent tracing |
| 7 | Azure Container Registry | Premium | ACA images |
| 8 | Container Apps environment | workload profiles, D4 | Dedicated profile, predictable latency on stage |
| 9 | `ca-atlas-web` | 2 replicas min | Never cold-start during a demo |
| 10 | `ca-atlas-orchestrator` | 2 replicas min | |
| 11 | `ca-atlas-worker` | 1–5, Service Bus scale rule | |
| 12 | Cosmos DB (NoSQL) | autoscale 4000 RU/s shared | State, checkpoints, idempotency |
| 13 | Azure AI Search | Standard S1, semantic ranker | Runbooks + past incidents |
| 14 | Service Bus | **Premium** | No throttling mid-demo; sessions for ordering |
| 15 | `func-atlas-api` | Flex Consumption | Business REST API behind APIM |
| 16 | `func-atlas-mcp` | Flex Consumption | MCP tool trigger server |
| 17 | API Management | **StandardV2** | MCP server support + fast provisioning (~15 min). PremiumV2 if you want VNet. |
| 18 | Logic Apps Standard + WS1 plan | | Approval workflow + agent-tool workflow |
| 19 | Key Vault | RBAC mode | APIM subscription key only |
| 20 | Storage account | StandardLRS | Functions host storage, eval artefacts, checkpoint blobs |
| 21 | User-assigned MIs ×5 | | web, orchestrator, worker, functions, logicapps |
| 22 | *(optional)* Azure Managed Redis + RediSearch | Balanced B1 | APIM semantic caching. `enableSemanticCache=false` by default — it adds ~25 min to provisioning. |

Cost is deliberately not optimised. See `reference/TEARDOWN.md`.

---

## The four hosting shapes (Episode 1's payoff)

Deploy **all four** so you can put them side by side on stage:

| Shape | Where it lives | What it demonstrates |
|---|---|---|
| **Raw model call** | `scripts/demo_raw_model.py` | A completion. No state, no tools, no identity. |
| **Prompt agent** | Foundry project, `atlas-triage-prompt` | Config-only: instructions + model + toolbox. Zero code, zero infra. |
| **Hosted agent** | Foundry Agent Service, `atlas-incident-agent` | Your code + framework, containerised, managed endpoint, dedicated Entra agent identity, versioning, built-in tracing. |
| **Custom app** | `ca-atlas-web` + `ca-atlas-orchestrator` | Full control: your HTTP surface, your UI, your workflow engine, your scaling. |

The same `atlas_agent` package backs the hosted agent and the ACA app — one agent definition, two
hosting models. That is the point.

---

## Identity model

Five user-assigned managed identities, one per compute boundary, plus the APIM system-assigned
identity and the Foundry hosted agent's own Entra agent identity. Full role table in
`reference/RBAC-MATRIX.md`. Summary of the trust chain:

```
Browser ──(Entra ID, app roles)──▶ ca-atlas-web
ca-atlas-web ──(UAMI token, aud=api://atlas-orchestrator)──▶ ca-atlas-orchestrator
ca-atlas-orchestrator ──(UAMI)──▶ Foundry project      [Foundry User]
ca-atlas-orchestrator ──(UAMI, APIM subscription)──▶ apim-atlas
apim-atlas ──(system MI)──▶ func-atlas-api             [Entra, aud=api://atlas-ops-api]
apim-atlas ──(system MI)──▶ Foundry models             [Cognitive Services OpenAI User]
ca-atlas-orchestrator ──(UAMI)──▶ Service Bus          [Data Sender]
ca-atlas-worker ──(UAMI)──▶ Service Bus                [Data Receiver]
Foundry hosted agent ──(agent identity)──▶ Toolbox MCP ──▶ apim-atlas
```

**The model never holds a credential and never has a network path to a backend that does not pass
through APIM.** That single sentence is the Episode 3 thesis.

---

## Data flow for one incident (the canonical demo)

1. Engineer: *"Checkout is timing out for about 20% of EU customers since 14:05, started right after
   the payments deploy."*
2. `ca-atlas-web` opens an SSE stream, mints a `traceparent`, POSTs to the orchestrator.
3. **intake** validates, extracts a candidate service (`svc-checkout-api`), loads the CMDB record
   and the last 10 changes from the ops API *through APIM*.
4. **triage_agent** → `TriageResult{severity: Sev2, category: latency, services: [...], confidence}`.
5. **concurrent fan-out**
   - **knowledge_agent** → hybrid search over runbooks + past incidents → `KnowledgeResult{citations[]}`
   - **impact_agent** → dependency graph walk + SLO burn rate → `ImpactResult{blast_radius, error_budget_pct}`
6. **aggregate** merges into typed `IncidentCase` state; a checkpoint is written to Cosmos.
7. **planner_agent** → `RemediationPlan{steps[], risk, estimated_minutes, rollback}`.
8. **policy_gate** (code, not model): plan touches a Tier-1 production service ⇒ `approval_required`.
9. `ctx.request_info(...)` — **the workflow suspends**. State is durable in Cosmos. The process can
   be killed here and resumed later; that is the Episode 2 set piece.
10. Approver opens the ATLAS approval console (or the Logic Apps approval callback), approves.
11. **dispatch** posts `RemediationCommand` to Service Bus with `MessageId = plan_id` and a session
    id of the service — dedup and ordering for free.
12. `ca-atlas-worker` executes step by step, emits progress to the `remediation-events` topic,
    writes the result to `remediation-results`.
13. **report** produces `IncidentReport` with the ticket number created via the ops API, the actions
    taken, and citations. Streamed back to the browser.
14. One distributed trace in Application Insights covers all of it.

---

## Episode → component mapping

| Episode | Built in phases | Live artefacts you show |
|---|---|---|
| 1 | 01–05 | `scripts/demo_raw_model.py`, prompt agent in portal, hosted agent endpoint, `ca-atlas-web` streaming, `src/atlas_agent/tools.py` typed contracts, `IncidentTriage` structured output |
| 2 | 06–07 | Orchestrator graph render, sequential/concurrent/handoff switch, Cosmos checkpoint documents, kill/resume, approval console, `policy_gate` code |
| 3 | 08–10 | APIM MCP servers blade, policy XML, Functions logs, Logic Apps run history, Service Bus metrics + DLQ, "delete the role assignment and watch it fail safely" |
| 4 | 11–14 | Trace waterfall across 6 hops, eval scorecard, the injected regression, the failing quality gate, alerts firing, rollback |
