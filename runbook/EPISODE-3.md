# Episode 3 - Agents That Take Action: secure enterprise integration

**Airs:** 2026-10-13, 18:00 EEST. **Slot:** slides, then the demo, then Q&A.
**Everything is already deployed. Nothing on this page changes infrastructure live.** The two
control-plane beats - the tool-description change and removing a Service Bus role - are shown from
recordings (CLAUDE.md 9). Their measured numbers are below.

Every live command here is executed by `make verify-demo-paths PATHS="ep3"`. The prompts, Send vs
Run in workflow and the approval flow are explained in `docs/PRESENTER-GUIDE-PROMPTS.md`.

---

## T-10 minutes (in this order)

```powershell
az login; azd auth login
git status                                      # clean
make demo-reset                                 # 10-50 s
bash scripts/chaos/ops_api_down.sh --status     # off
bash scripts/chaos/revoke_servicebus_role.sh --status   # baseline
make warm                                       # T-10 only, never during the slides
make smoke                                      # 5/5
```

Open and arrange:

- the web app, **Scope: Episode 3**
- the Azure portal on **APIM** (APIs, then MCP Servers) and on the **Logic App** (run history)
- VS Code on `apim/policies/`
- a terminal

---

## Beat 1 - The full action, end to end (7 min)

Run it once, straight through, before explaining anything.

| Do | The room sees (measured 2026-10-11) | Say |
|---|---|---|
| **New session**, canonical report, **Run in workflow** | The graph runs and the gate parks it (about 45-60 s). Read the reasons. | |
| **Approve** (comment required) | The **Gateway and remediation** panel fills in: ticket, operation, "succeeded (9/9 steps)", path **orchestrator → APIM → Function → Service Bus → worker**. About 30 s after approving. | Nothing you just saw touched a backend directly. Let me show you the layer in between. |

The approver depends on the plan the AI drafts: director only if the plan acts on the frozen
payments service (break glass), otherwise manager or director. Read the reasons.

## Beat 2 - Five ways to give an agent a capability (6 min)

| Mechanism | In ATLAS |
|---|---|
| OpenAPI tool | `apim/openapi/atlas-ops-api.yaml`: 11 operations, each description saying when NOT to use it |
| MCP server | `atlas-ops-mcp` on APIM (REST as MCP), `atlas-tools-mcp` (Functions MCP, passthrough) |
| Logic Apps workflow as a tool | `enrich-incident`, the MCP tool `enrich_incident` |
| Azure Function | `func-atlas-api`, the governed business API |
| Asynchronous message | Service Bus `remediation-commands` and the worker |

**Live:** Prompt agent shape, ask *"Enrich the incident on svc-orders over the last 60 minutes in
one call."* The activity panel shows `atlas_ops___enrich_incident`, and the answer is the
workflow's aggregate (service, on-call, changes, health): prompt agent → Foundry toolbox → APIM MCP
→ Logic App. About 15-25 s.

**Recorded, never live:** "one tool description changed in APIM reaches all four agent surfaces"
is an APIM write. Show the numbers from 2026-10-11: prompt agent 28 s, web app 43 s, orchestrator
54 s, hosted agent 109 s (its next request). Say why there are four numbers: each surface caches
the catalogue differently.

## Beat 3 - APIM as the AI gateway (10 min) - the core, never cut

| Do | The room sees (measured) |
|---|---|
| `make prove-governance` | **9/9 proofs**, about 60 s. Direct call to the Function **401**. No key **401**. Valid key, not an agent (you) **403**. Valid agent call **200**. Flood **429** with Retry-After. Malformed body **400 at the gateway**, backend never hit. Model token budget **429**. Backend 503: **retried 3 times**, then a typed envelope with no internals. |
| VS Code: `apim/policies/ops-api.xml`, read its header | The 7 layers in order: rate limit and quota, agent auth (401 / 403), contract, identity swap, retry, response contract, error envelope. |

Proof 3 is you: a real identity and a real key, still 403, because a person is not an agent.

## Beat 4 - No keys, anywhere (5 min) - never cut

| Do | The room sees | Say |
|---|---|---|
| `grep -rniE "AccountKey\|SharedAccessKey\|api[-_]?key" src functions infra` | **One** hit: `infra/modules/logicapps.bicep`, the Logic App's own host storage, built from `listKeys()` at deploy time. | Say it before it prints: Logic Apps Standard needs shared-key access to its own host storage. It is isolated in its own account, holds no ATLAS data, and the key is never in source. |
| Show the identity chain (deck slide 8) | Web → orchestrator → APIM → Function → Service Bus → worker: an Entra token on every hop. | |
| **The recorded drill**: `docs/assets/fallbacks/ep3-4-revoke-servicebus-role.txt` | The ops API's Sender role removed. It took effect after **236 s** (a running workload keeps its cached token). The queue refused it: **503 queue_unavailable**. The approved case stayed `approved`, **nothing ran**. Role back (96 s), retry dispatch, **completed**. | Fail closed, not open. And the four minutes are why you never do this live: revocation is not instant. |

## Beat 5 - Long-running work done properly (6 min)

| Do | The room sees (measured) | Say |
|---|---|---|
| `make inject-failing-plan` | Prints an operation id. Step 2 (RB-0099) fails on purpose. | |
| Wait about 40 s, then `make dlq-inspect` | The command, **dead-lettered after 5 deliveries**, with its reason and case. | Dead-lettered is not failed: the case is waiting for a human. |
| `make dlq-replay OP=<OP-...>` | The fault is cleared, the command replays with a NEW MessageId, step 1 is **not** re-run, the case completes. | Operable, not just observable. |
| Portal: Logic App `approve-remediation`, run history | A run that waited on its webhook, then completed by the decision callback. | The workflow collects the decision; the orchestrator makes it. |

Duplicate dispatch (one plan sent twice, executed once) is proven by `make verify-phase10`, check
3. Mention it, do not run it.

## Beat 6 - The security posture in one sentence (2 min)

*"The model never holds a credential and has no network path to a backend that does not pass
through the gateway."*

Show `make verify-security` output, run at T-10: 9/9, zero findings, one named exception. Then
name what real production adds: private endpoints and VNet, customer-managed keys, per-team APIM
products, data residency, a real ITSM connector.

## Close (2 min)

None of this proves the agent is good. Next week: measure it, trace it, break it on purpose, and
put a quality gate in front of the release.

**Timing guard.** Beats 3 and 4 are non-negotiable. If short, show Beat 5 from the recording.

---

## Encore

Already done in Beat 1. Every other outcome in one command: `make e2e-batch` (approved, rejected,
auto-approved, dead letter, rollback; about 7 minutes).

---

## Fallbacks

| If this fails live | Do this |
|---|---|
| Approve does not seem to move the case | Wait 5-10 s: the console follows the replica that decided it. The case document is the truth: `GET /api/workflow/<id>`. |
| A governance proof fails | `docs/assets/fallbacks/ep3-3-prove-governance.txt` (9/9). Proof 5's counter is distributed: rerun after a minute. |
| Prompt agent tool errors | Show the activity panel's typed outcome. The recorded tool call is `docs/assets/ep3-slide09-tool-call.png`. |
| DLQ beat slow | `docs/assets/fallbacks/ep3-5-outcomes-and-dead-letter.txt`. |
| Anything else | `docs/assets/fallbacks/ep3-*`, `ep1-encore-e2e.txt`, `docs/assets/ep3-slide*.png`. |
