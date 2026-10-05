# ATLAS - Presenter Guide

Read this first if you have never seen ATLAS before. It explains what the system is, what every
control on the screen does, what happens behind the scenes when you press a button, and what to
show in each episode. It is deliberately plain. The deep technical record lives in
`docs/DEMO-NOTES.md` (per-phase detail and measured timings) and `docs/API-NOTES.md` (every
surprise we hit and why the code looks the way it does).

---

## 1. What ATLAS is, in one paragraph

ATLAS is an **incident copilot** for a fictional company, Contoso, that runs 25 online services
(checkout, payments, search, orders and so on). When something breaks, an engineer describes the
problem in plain English. ATLAS investigates the way a good on-call engineer would: it looks up the
affected services, checks what was deployed recently, reads the live metrics, finds the right
runbook and similar past incidents, works out how far the damage spreads, and drafts a fix. Then
**plain code - not the AI - decides whether that fix may run and who must approve it**. Once
approved, the fix is executed asynchronously by a separate worker, every step is recorded, and the
whole journey is one traceable line in the monitoring system.

The series is four episodes, and each one is a **view** onto the same running system:

| Episode | Title | The question it answers |
|---|---|---|
| 1 | Beyond Chatbots | What makes an agent more than a chatbot? (instructions, tools, structured output, state) |
| 2 | Reliable Multi-Agent Systems | How do several agents work together safely? (orchestration, checkpoints, human approval, deterministic policy) |
| 3 | Agents That Take Action | How does an agent act on real systems without being dangerous? (gateway, identity, async execution) |
| 4 | Production-Grade Agents | How do you know it works and keep it working? (tracing, monitoring, safety, evaluation) |

**The single most important fact for a presenter:** the whole system is live and complete in every
episode. The Scope selector only hides parts of the screen. You can take any episode all the way to
the end ("the encore") at any moment.

---

## 2. The 60-second mental model

```
  You (browser)
     |
     v
  WEB APP  ----- chat path (Episode 1) ------> one agent, six tools -----> answer + triage card
     |
     |---- workflow path (Episode 2+) ----> ORCHESTRATOR
                                               |
                       intake (code) -> triage agent -> knowledge agent ---\
                                                      \-> impact agent  ----> aggregate (code)
                                                                              -> planner agent
                                                                              -> POLICY GATE (code)
                                                                                    |
                         auto-approved <------------------------------+-------------+
                                |                                   approval required
                                |                                     (a human, in the console)
                                v                                           |
                    dispatch: ticket + single-use token  <------------------+
                                |
                                v
                     API MANAGEMENT (the AI gateway: identity, limits, logging)
                                |
                                v
                     OPS API (Azure Function) -> SERVICE BUS queue -> WORKER (runs each step)
                                                                          |
                     progress and result flow back to the case  <---------+
```

Everything above shares **one trace id**, so in Application Insights the entire journey - browser
to worker - is a single waterfall.

Where things live (all in Azure, region East US 2, resource group `rg-atlas-atlas`):

| Piece | Azure service | Plain-English job |
|---|---|---|
| Web app | Container Apps (`ca-atlas-web-...`) | The screen you present |
| Orchestrator | Container Apps (`ca-atlas-orch-...`) | Runs the multi-agent workflow, two replicas |
| Worker | Container Apps (`ca-atlas-worker-...`) | Executes approved remediation steps |
| Agents and models | Microsoft Foundry (`gpt-main`, `gpt-judge`, `embed-main`) | The AI |
| Data | Cosmos DB | Services, incidents, changes, telemetry, cases, checkpoints |
| Knowledge | Azure AI Search (East US - capacity, see API-NOTES 7.14) | Runbooks and past incidents, searchable |
| Gateway | API Management | Every agent-to-backend call goes through it |
| Ops API / MCP server | Azure Functions | The "business systems" the agents call |
| Messaging | Service Bus | The queue between "approved" and "done" |
| Approval workflow | Logic Apps | Notifies approvers and waits for the decision |
| Monitoring | Application Insights + Log Analytics | Traces, workbook, alerts |

No passwords or keys anywhere in the code: every service proves who it is with a managed identity.

---

## 3. Words you will hear

| Term | Meaning |
|---|---|
| **Case** | One incident being worked, id like `CASE-51363567`. Lives in Cosmos. |
| **Agent** | An AI with instructions and tools. ATLAS has a chat agent and five workflow agents (triage, knowledge, impact, planner, coordinator). |
| **Tool** | A function an agent may call, like `get_recent_changes` or `compute_blast_radius`. Tools return facts; the agent may not invent them. |
| **Plan** | The ordered fix the planner drafts. Each step cites a runbook step. |
| **Destructive step** | A step that changes something: restart, rollback, config change, failover. |
| **Policy gate** | Plain code that reads the plan and decides: blocked, auto-approved, or approval required (and by whom). The AI cannot influence it. |
| **Blast radius** | How many services a failure can reach through dependencies. Computed by a tool, never estimated. |
| **Change freeze** | A service where planned changes are not allowed. Fixing an incident on a frozen service is "break glass" and needs a director. |
| **Persona** | Who you are acting as: readonly, engineer, sre, manager, director. Each has different approval rights. |
| **Checkpoint** | A saved snapshot of the workflow. A parked case survives an orchestrator restart. |
| **Remediation** | Executing an approved plan, step by step, by the worker. |
| **DLQ (dead-letter queue)** | Where a command goes after failing 5 times. Nothing is lost; it can be replayed. |
| **Trace** | The recorded path of one request across every service. |
| **Ops clock** | ATLAS's "now" is frozen at **2026-09-01 14:20 UTC**. All "since 14:05" style times refer to it, so every rehearsal gives the same answer. |

---

## 4. Your first fifteen minutes

1. Open the web app: `https://ca-atlas-web-mkruniv7.bluebay-ec2ff390.eastus2.azurecontainerapps.io`
   (or `azd env get-value ATLAS_WEB_URL`). Sign in with your Microsoft account. The top-right should
   say you are a **director** - that is the role the presenter holds (`Atlas.Director`).
2. Set **Scope** to *Full (encore)* so you see everything.
3. Click the first example ("Checkout is timing out...") and press **Send**. Watch the right-hand
   **Agent activity** panel fill with tool calls, then a triage card appear in the chat. About 30-60
   seconds.
4. Now click the same example and press **Run in workflow** (in the Workflow graph panel). Watch the
   nodes light up: intake, triage, then knowledge and impact *in parallel*, aggregate, planner. About
   one minute later an **Approval required** box appears with the gate's reasons.
5. Type a comment ("approved for rehearsal"), press **Approve**. Within seconds the **Gateway and
   remediation** panel shows a ticket id and the worker's steps ticking to *succeeded*. About 30 s.
6. You have just run the whole system end to end. That is the encore.

---

## 5. The screen, control by control

### Top bar

| Control | What it does | What it does NOT do |
|---|---|---|
| **Model call / Prompt agent / Hosted agent / This app** (four buttons) | Chooses *which kind of agent* answers the chat. Same question, four architectures: **Model call** = the bare AI with no tools or data (it will guess, sometimes invent change ids - that is the point); **Prompt agent** = an agent defined in Foundry with knowledge search; **Hosted agent** = our agent code running inside Foundry; **This app** = our agent running in the web app with six typed tools and Cosmos-backed memory. | Does not affect the workflow panel. |
| **Instructions** (dropdown) | Which version of the chat agent's instructions to use: **v0** is three lines ("be helpful"), **v1** is the 111-line operating procedure (classify, correlate, measure, retrieve, assess; never guess an id), **v2** (active since 2026-10-05) adds four rules found by the evaluation: care first on self-harm, neutral refusals, low confidence on vague reports, and checking the service a flag or config change lives in. Read from Cosmos per request, so switching is instant - no redeploy. *active* means whatever the shared pointer says (v2). | Only applies to the chat path (*This app*) and the hosted agent. |
| **Show the contract** (checkbox) | Reveals the raw JSON behind the triage card: the typed structure the agent must return (severity, category, confidence, suspected changes, citations...). Shows that the output is data, not prose. | Changes nothing the agent does. |
| **Scope** (dropdown) | Which panels are visible: Episode 1 shows chat and tool activity; 2 adds the workflow and approval; 3 adds gateway and remediation; 4 and Full show everything. Also settable in the URL: `?scope=ep2`. | **Never** changes behaviour. Hidden panels keep running and keep their state, so switching mid-run loses nothing. |
| **Persona / act as** | Shows who you are and which persona's rights apply. **act as** lets you *lower* your privilege for a demo: *manager* (cannot sign a break-glass plan - the 403 beat) or *readonly* (may look, may not act). | Can never raise privilege. Your real identity comes from your sign-in token, not the page. |
| **Approvals** (link) | Opens the approval console: every workflow case waiting for a human, with the gate's reasons, who may sign, risk, blast radius and expiry, and Approve / Reject with a required comment. It shares the main page's *act as* setting and refreshes every 5 seconds. **It keeps working while the orchestrator is down:** the queue is read straight from Cosmos and your decision is recorded; the case resumes from its checkpoint when the orchestrator is back (a banner says so). | Does not bypass any check: the orchestrator re-validates role, plan and expiry when it resumes the case. |

### Chat area (left)

| Control | What it does |
|---|---|
| **Example buttons** | Fill the text box with a ready prompt. The first is the canonical incident; the last three are the extra scenarios (section 6). Clicking does not send - press Send or Run in workflow. |
| **Text box** | Type an incident or a follow-up question. Enter sends; Shift+Enter is a new line. |
| **Send** | Sends to the chat agent (the shape chosen in the top bar). |
| **Stop** | Cancels the running answer (appears while one is running). |
| **New session** | Forgets the conversation. Without it, follow-ups ("which team owns it?") use the earlier context, which is stored in Cosmos. |

### Agent activity panel (right, always visible)

| Field | Meaning |
|---|---|
| Shape | Which of the four agent kinds answered |
| Instructions | Which instruction version was used, and where it was read from |
| Session | Conversation id |
| Trace | The trace id - click it to see the trace for this turn |
| First event / Elapsed | Time to the first streamed token, and the total |
| Tokens in | How much text was sent to the model |
| Tool list | Every tool the agent called, how long it took, and its outcome (ok, not found, failed). A failed backend shows here as a typed failure, never as a crash. |

### Workflow graph panel (Episode 2 and up)

| Control | What it does |
|---|---|
| **Shape** (dropdown) | The orchestration pattern: **Concurrent graph** (knowledge and impact run in parallel - the default and fastest), **Sequential only** (one after another - slower, same result), **Handoff** (a coordinator agent decides who works next). This one *does* change how the work runs - it is not a view filter. |
| **Run in workflow** | Sends the text box content (or the first example if empty) through the multi-agent workflow. |
| Node list | Each step in order. Tags: **agent** (AI) or **code** (deterministic), **parallel** (runs at the same time as its sibling). Each node shows its time and tool count when done, or turns amber if it degraded. |
| Case / Severity / Plan / Elapsed | The case id, the assessed severity, the plan id with step count, total time |
| **Approval required** box | Appears when the gate wants a human. Shows *who* may approve (e.g. "director"), the gate's **reasons** (written by code, verbatim), the plan summary and when the approval window expires. Needs a **comment** (a decision without a reason is not an audit trail). **Approve** / **Reject**. A refusal ("role manager may not approve this plan") is part of the demo, not an error. |
| Decision log | Every decision made by deterministic code during the case, with the rule name. Nothing the AI wrote. |
| Mermaid source | The live graph definition, generated from the running orchestrator, not drawn by hand. |

### Gateway and remediation panel (Episode 3 and up)

Empty until a plan is approved (or auto-approved). Then: the **ticket** id, the **operation** id,
the **status** (queued, running, succeeded, dead_lettered...) with a step count, the fixed path
(orchestrator -> APIM -> Function -> Service Bus -> worker), and every step as the worker runs it,
including retry attempts.

### Evaluation panel (Episode 4)

A placeholder today. Scorecards and the quality gate arrive in Phases 12-13 (section 11).

---

## 6. The four live incidents

The data is a fixed, synthetic two-year history: 500 past incidents, 303 deployments, 60 runbooks,
25 services, 16 people. Any history question works for any service ("Have we seen this before on
Orders?", "What changed on Fraud last week?", "Who owns Identity, and is it frozen?").

Four incidents are staged as *happening now* (relative to the 14:20 ops clock). Each has a real
deployment that caused it, live metrics showing it, and a runbook - and each is built so the policy
gate reaches a **different** answer:

| Example button | What is going on | What the gate decides (measured) | Best episode |
|---|---|---|---|
| "Checkout is timing out ... since 14:05 ... payments deploy" (**canonical**) | A risky payments deploy (CHG-2026-0873) at 13:59 broke checkout. Payments is under change freeze. | **Director only** - break glass on a frozen service that is implicated in the incident | 1, 2, 3 |
| "Reporting dashboards ... 5xx at 13:50 ... staging" (**reporting-ttl**) | A cache setting change on a staging Tier 4 service | **Auto-approved** - low risk, staging, every step reversible. Fixed in about 25 s with no human | 2 (the contrast) |
| "Search ... wrong results ... since 13:40 ... feature-flag rollout" (**search-flag**) | A feature flag rollout broke search. The matching runbook (RB-0029) contains a planted malicious instruction. | **SRE** approval. The knowledge agent reports the planted instruction instead of obeying it. | 4 (safety) |
| "Order placement p95 ... tripled since 14:10" (**orders-stale**) | A logging change slowed orders; the right runbook (RB-0015) has not been reviewed since 2024 | **Manager** approval; the gate's reasons name the stale runbook | 2 or 4 |

The AI drafts the plan, so the approver can occasionally differ from the table. Whatever it decides,
the reasons are printed and explainable. `make probe-scenarios` (about 5 minutes) checks all four
before you go live.

Two more staged behaviours:
- **RB-0099** - a plan with a step that fails on purpose: it retries five times, lands in the
  dead-letter queue, and is replayed after the "fix" (Episode 3).
- **readonly** - act as readonly and raise a case: the gate blocks it (no authority to act).

---

## 7. What actually happens when you approve - the timeline

Measured on the deployed system (canonical case):

| Moment | What happens | Time |
|---|---|---|
| Run in workflow | Intake (code) resolves services; triage agent investigates with tools | 0-20 s |
| | Knowledge and impact agents run in parallel; aggregate (code) merges | 20-40 s |
| | Planner drafts the plan; the **policy gate** (code) decides | 40-70 s |
| Gate parks the case | Checkpoint saved; Logic App starts and waits for the decision | instant |
| Approve | Decision checked against your role, the plan id and the expiry | under 1 s |
| Dispatch | Ticket opened, single-use approval token minted, command sent through APIM to the ops Function, onto Service Bus | 2-5 s |
| Worker | Proves the command was authorised (from the spent token), runs each step, reports progress | 7-9 s to the first step |
| Done | Case completed with ticket and full step record | 30-37 s after approval |
| Trace | The whole journey visible in Application Insights | 1-3 minutes later (ingestion lag) |

---

## 8. Episode by episode

The lab pack's run sheets (`_labpack/runbook/EPISODE-1.md` to `-4.md`) have the minute-by-minute
beats. This is the short version, mapped to what is built.

### Episode 1 - Beyond Chatbots (Scope: Episode 1)
1. **Four shapes:** ask the canonical question with *Model call* (it guesses), then *This app* (it
   investigates with tools and cites real change ids). Show *Prompt agent* and *Hosted agent* -
   same question, different hosting.
2. **Instructions are a contract:** switch Instructions to v0, ask again: vague. Back to v1: a
   procedure. Instant, no redeploy.
3. **Tools are an API:** point at the activity panel - each tool, its time, its outcome.
4. **State:** ask a follow-up ("is the service that caused this frozen?") - it remembers. New
   session - it does not.
5. **Structured output:** tick *Show the contract*.
- `make demo-raw` shows the bare model inventing a change id in the terminal.

### Episode 2 - Multi-agent, state, approval (Scope: Episode 2)
1. Run the canonical case in workflow with *Concurrent graph*; point at the two parallel nodes.
   Switch Shape to *Sequential only* and *Handoff* to compare.
2. **Agentic vs deterministic:** the agent/code tags, the Decision log, the gate's reasons.
3. **The contrast:** run *reporting-ttl* - auto-approved and fixed with no human. Same gate, a
   different answer, and the reasons say why.
4. **Human approval:** act as *manager* and try to approve the canonical plan - refused (director
   only). Switch back and approve.
5. **Kill and resume:** park the canonical case, `make kill-orchestrator`, approve it in the
   Approvals console. If the orchestrator is still down the console says your decision is recorded;
   when it is back the case resumes from its checkpoint and completes. The restart is fast (about
   7 s measured), so you may well approve after it is back - the case still resumes cold, from the
   checkpoint, on whichever replica gets it.

### Episode 3 - Taking action (Scope: Episode 3)
1. Approve a case and watch the remediation panel: ticket, operation, each step.
2. `make prove-governance` - eight proofs that the gateway enforces identity, limits and validation.
3. `make mcp-probe` - the same tools exposed as an MCP server.
4. No keys anywhere: every hop uses managed identity.
5. **Long-running work done properly:** `make inject-failing-plan`, then `make dlq-inspect` (it
   failed 5 times and is safely parked), then `make dlq-replay OP=OP-XXXX` (fixed and finished,
   without re-running the steps that already succeeded).

### Episode 4 - Production grade (Scope: Episode 4)
1. **One trace:** `make trace-check CASE=<a case you ran>` prints the whole journey and asserts every
   hop. Then the same trace in Application Insights.
2. **The workbook** "ATLAS Agent Operations" (Azure portal, resource group `rg-atlas-atlas`,
   Workbooks): latency per agent, tool health, tokens per agent, gate decisions, remediation, safety.
3. **Alerts:** nine rules; show the dead-letter alert history from rehearsal (it takes about 5
   minutes to fire - too slow to do live).
4. **Safety:** run *search-flag* - the planted instruction is noticed and escalated, not obeyed.
   Run *orders-stale* - the gate names the stale runbook.
5. **Evaluation** (`evals/reports/` in the repo - project these): the v0 / v1 / v2 comparison on
   72 cases. The headline: the generic AI judges barely separate the versions (composite 0.938 /
   0.931 / 0.939), the targeted checks do - prompt injection 60 % to 100 %, safety 62 % to 88 %,
   deterministic checks 88 % to 99 %. Then the v2 scorecard (gate PASS, one judge finding
   reviewed as a false positive and shown on the card) and the red-team summary (84 attacks, 0
   prohibited actions).
6. The quality gate and the deliberate regression: **not built yet** (Phase 13).

### The encore (any episode)
Set Scope to *Full*, run the canonical case in workflow, approve, watch the remediation finish.
About two to three minutes in total.

---

## 9. What is AI and what is code

This is the message of the whole series, and the screen shows it everywhere:

| AI decides | Code decides |
|---|---|
| What the incident is, how severe, what probably caused it | Whether the plan may run at all |
| Which runbook and past incidents are relevant | Who must approve, and until when |
| How far the impact goes (from the tool's number) | Change freezes, blast-radius limits, stale-runbook holds |
| The draft plan and its risk grade | Authorisation of every remediation command (single-use token) |
| The summary you read | Retries, dead-lettering, idempotency |

The AI's risk grade only chooses *who* approves. A destructive step on a production service always
needs a human, whatever the AI says.

---

## 10. Commands you will actually use

Run from the repo folder in a terminal (`make help` lists everything).

| When | Command | What it does |
|---|---|---|
| Before the session (T-10) | `az login` and `azd auth login` | Fresh sign-in |
| | `make demo-reset` | Returns all cases, queues and approvals to a clean baseline (under 60 s). **Only at T-10, never during a session.** |
| | `make probe-scenarios` | Runs all four incidents and checks each gate outcome (about 5 min) |
| | `make hosted-status` | Which hosted agent version is live |
| The day before an episode | `make scale PROFILE=show` | Always-warm replicas (web and orchestrator 2-10, worker 1-5). `PROFILE=work` (1-3) for build days, `PROFILE=idle` (0-3) between sessions |
| During | `make trace-check CASE=CASE-XXXX` | The trace of one case as text |
| | `make prove-governance` | Gateway proofs (Episode 3) |
| | `make inject-failing-plan`, `make dlq-inspect`, `make dlq-replay OP=...` | The dead-letter beat (Episode 3) |
| | `make kill-orchestrator` | The kill-and-resume set piece (Episode 2) |
| | `make chaos-ops-api-on` / `-off` | Makes the ops API return errors for 120 s, to show graceful degradation |
| Never during a broadcast | `azd provision`, `azd deploy`, any `make *-deploy` | Builds and deployments are off-limits live (CLAUDE.md section 9) |

---

## 11. Known gaps (as of 2026-10-02)

Honest list of what is not done or not right yet:

1. **Replica ranges are set to the `work` profile (1-3) for build days.** Before an episode switch
   to always-warm: `make scale PROFILE=show` (about 5 minutes, the day before - never live). The
   `idle` profile (0-3) saves cost between sessions but means cold starts. See section 10.
2. **The quality gate (Phase 13)** is not built, and the Evaluation panel on the main screen is
   still a placeholder. Evaluation itself (Phase 12) is done: scorecards in `evals/reports/`,
   the workbook's Quality section has data. A full `make eval` takes about 45 minutes of model and
   judge time - run it the day before, never live.
3. **Phase 14** - the full four-episode dry run, the five demo-path checks from a cold reset, the
   measured encore time, and the fallback screenshot deck - is not done.
4. **The gate's approver depends on the plan the AI drafts.** Usually as in section 6; if not, read
   the reasons out - they are always right about the plan in front of them.
5. **Traces appear 1-3 minutes after the fact.** Run the case you want to trace before the beat.

---

## 12. When something goes wrong live

| Symptom | What you see | What to do |
|---|---|---|
| A backend is down | "I could not reach the ..." in the answer; the tool shows as failed | That is the designed behaviour - say so. |
| Model throttling | A banner with a retry countdown | Wait it out or switch to another example. |
| Orchestrator unreachable | Banner: "the chat path still works, the workflow does not" | Continue in chat; `make hosted-status`; the case is checkpointed. |
| A case hangs at approval | The approval box is gone or stuck | `make resume CASE=CASE-XXXX APPROVE=1` |
| Trace not found | `trace-check` finds nothing | Ingestion lag: use the case from the T-10 rehearsal. |
| Anything else | | `docs/DEMO-NOTES.md` has a Fallback section per phase. |

---

## 13. Where to look next

| If you want to understand... | Read |
|---|---|
| The original design | `_labpack/ARCHITECTURE.md`, `_labpack/DATA-MODEL.md` |
| The minute-by-minute episode scripts | `_labpack/runbook/EPISODE-*.md` |
| Each phase: what was built, how to show it, timings | `docs/DEMO-NOTES.md` |
| Why something is built the way it is | `docs/API-NOTES.md` |
| The rules everything follows | `CLAUDE.md` (sections 7-9 especially) |
| The policy gate itself (about 250 lines, readable) | `src/atlas_orchestrator/atlas_orchestrator/policy.py` |
| The chat agent's instructions | `src/atlas_agent/atlas_agent/instructions/atlas_incident_agent.v1.md` |
| The staged scenarios | `data/generators/scenarios.py` |
