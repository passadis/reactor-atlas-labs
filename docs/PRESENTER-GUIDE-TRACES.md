# ATLAS - Presenter Guide: showing one case, step by step (traces)

What to look for in **Application Insights** and **Foundry Traces**, and how to show a case so a
room can follow it. Measured on the Episode 2 case CASE-4B3E155C (2026-10-08).

---

## 1. The one idea

Every step of a case - web, orchestrator, each agent, every tool call through APIM, the Function,
Service Bus, the worker, even the Logic App - carries the **same trace id**. So one case is one
trace. That is the whole Episode 4 point about observability: you do not stitch logs together,
you open one id.

The catch: one case is **about 340 spans**. Raw, nobody can follow it. So use three views, from
simple to detailed.

---

## 2. Three views, in this order

### View 1 - the story (terminal, 15 lines)

```powershell
make trace-story CASE=CASE-XXXXXXXX
```

One line per step, in plain words, from the real telemetry:

```
t+   0.0s  REPORT                 the engineer's report enters through the web app
t+   0.4s  INTAKE (code)          resolves the services named in the report - 0.2 s
t+   0.7s  TRIAGE (agent)         what is broken, how bad, what changed - 10.8 s, 3 model calls, tools: ...
t+  11.6s  KNOWLEDGE (agent) [parallel]  which runbook and past incidents apply - 12.2 s
t+  11.6s  IMPACT (agent) [parallel]     what else this takes down - 5.4 s
t+  11.6s                         knowledge and impact ran side by side: 5.4 s saved
t+  23.8s  AGGREGATE (code)       merges the analyses by fixed rules
t+  23.9s  PLANNER (agent)        drafts the remediation plan - 16.5 s
t+  40.5s  POLICY GATE (code)     approval required - director must approve. No model call
t+  40.7s  LOGIC APP              notifies approvers, waits for the decision
t+  62.9s  HUMAN                  APPROVED by a director, 22.2 s after the gate asked
t+  63.1s  DISPATCH (code)        opens ticket, mints a single-use token, submits through APIM
t+  63.9s  OPS API (Function)     checks the token, spends it, queues the command on Service Bus
t+  64.5s  WORKER                 runs 8 steps, publishes 17 progress events - 29.3 s
t+  93.9s  DONE                   remediation succeeded after 1 delivery attempt
```

**What to say, line by line:** agents think (TRIAGE, KNOWLEDGE, IMPACT, PLANNER), code decides
(INTAKE, AGGREGATE, POLICY GATE, DISPATCH), a human approves, and execution is asynchronous
(Service Bus, WORKER). Point at the two `[parallel]` lines, at "No model call" on the gate, and at
the 22 seconds the case simply waited for a human.

### View 2 - the same trace in Application Insights (the proof it is real)

1. Azure portal, Application Insights **`appi-atlas-mkruniv7`**, **Investigate**, then
   **Transaction search**.
2. Search the case id (for example `CASE-4B3E155C`), time range **Last 24 hours**. Open any result,
   then **View all** in the end-to-end transaction view.
3. What to look for:
   - **The left column of components**: web, orchestrator, APIM, ops API Function, worker, Logic App
     - all in one timeline, one id.
   - **`invoke_agent triage_agent`** (and the other three): expand it to see `chat gpt-main` (a
     model call) and `execute_tool lookup_service` (a tool call) underneath. That is "the agent
     thinking" made visible.
   - **Click one `chat gpt-main`**: content capture is on in the demo environment, so the panel on
     the right shows the **prompt the agent actually saw** and its answer.
   - **`atlas.dispatch`**, then **`POST /ops/remediation/commands`** through APIM, then
     **`ServiceBus.send`**, then **`atlas.worker.remediation`**: the hand-off from decision to
     execution.

### View 3 - Foundry Traces (the agent's own view)

Foundry portal, your project, **Agents**, then **Traces** at the top. Search by trace id, or open
an agent and its **Traces** tab.

What it shows - and what it does not:

| Shown in Foundry Traces | Not shown there |
|---|---|
| The **hosted agent** (`atlas-incident-agent`): each call, its model calls and tool calls, with a **Trajectory** waterfall and a **User view** of the conversation | The **workflow** (triage, knowledge, impact, planner in Container Apps): that is our own application, so its traces live in Application Insights - views 1 and 2 |
| The **prompt agent** (`atlas-triage-prompt`): what Foundry emits server side | |

So in Foundry, run a triage in the web app's **Hosted agent** shape first, then show its trace:
the same agent, managed by Foundry, with Foundry's own view of it. Say it plainly: *"Foundry shows
the agents it hosts. The workflow around them is our code, and its trace is in Application
Insights - same store, same trace ids."*

---

## 3. Prepare before the beat (telemetry is 1-3 minutes behind)

1. At T-10, run one case end to end: canonical report, Run in workflow, Approve. Or
   `make e2e-remediation`, which prints the case id.
2. Note the case id on your run sheet.
3. Two minutes later, check it is ingested: `make trace-story CASE=<id>` prints the full story.
4. For Foundry: run one triage in the **Hosted agent** shape at T-10 as well.

Never run the case live and open its trace straight away. It will not be there yet.

---

## 4. Also worth showing (Episode 4)

| Where | What |
|---|---|
| `make trace-check CASE=<id>` | The full 230-line waterfall plus PASS lines: all seven components on one trace, `atlas.case_id` on every ATLAS span. The engineer's proof. |
| Workbook **ATLAS Agent Operations** (resource group, Workbooks) | Latency per node (the model is the slow part), tokens per agent (knowledge costs most), what the gate decided, the injection canary. |
| Alerts (`ag-atlas-ops`) | The dead-letter alert history from rehearsal: fired, then resolved after the replay. |

---

## 5. If it fails live

| Problem | Do this |
|---|---|
| Trace not found | Ingestion lag. Use the T-10 case id, or any case from the last 7 days. |
| Application Insights portal slow | `make trace-story` and `make trace-check` query the same data from the terminal. |
| Foundry Traces empty | Show the hosted agent's spans in Application Insights instead (role `atlas-incident-agent` and `responsesapi`). |
