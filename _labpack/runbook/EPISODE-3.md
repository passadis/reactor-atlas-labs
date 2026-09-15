# Episode 3 — Agents That Take Action: Secure Enterprise Integration

**Slot:** 60 min · **Live demo:** ~32 min

## T-10 minutes

```bash
make demo-reset
bash scripts/warm.sh
bash scripts/verify_smoke.sh
bash scripts/chaos/restore_all.sh          # ensure no chaos flags are set
```

Open: ATLAS web app, Azure portal on the APIM instance (APIs + MCP Servers blades), Logic Apps run
history, Service Bus metrics, a terminal, VS Code on `apim/policies`.

---

## Beat 1 — The full action, end to end (7 min)

Run it once, straight through, before explaining anything. The audience needs to see the
destination.

Canonical incident → triage → plan → **approval required** → approve in the console → Service Bus
command → worker executes step by step (live progress in the UI) → ticket number → final report
with citations.

Then: *"Nothing you just saw touched a backend directly. Let me show you the layer in between."*

---

## Beat 2 — Five ways to give an agent a capability (6 min)

Open the Foundry toolbox `atlas-tools` and the APIM **MCP Servers** blade side by side.

| Mechanism | What it is | Use it when |
|---|---|---|
| **OpenAPI tool** | An existing REST API described by a spec; operations become tools | You already have an API and a spec you trust |
| **MCP server** | A tool server speaking a protocol; discovery, descriptions and auth are part of it | You want tools shared across agents, frameworks and teams |
| **Logic Apps action / workflow-as-tool** | A whole workflow behind one tool call | The capability is multi-step, long-running, or needs 1,400 connectors |
| **Azure Function** | Your own code as a tool | The logic is yours and does not exist as an API yet |
| **Asynchronous message** | The agent submits work; something else does it | The work takes minutes to hours, or must survive the conversation |

We use all five. Point at each in the architecture diagram.

Then the key structural point: **one toolbox, one MCP endpoint, four agent surfaces.** Change a tool
description in APIM and show the prompt agent, hosted agent, orchestrator and web app all pick it up
with no redeploy.

---

## Beat 3 — APIM as the AI gateway (10 min) — **the core of the episode**

```bash
bash scripts/prove_governance.sh
```

Walk the eight proofs as they print:

1. Direct call to the Function app from an unauthorised principal → **401**
2. Through APIM without a subscription → **401**
3. With a subscription but without the `Atlas.Agent` role → **403**
4. Valid call → **200**, correlation id echoed
5. 121 calls in a minute → **429** with `Retry-After`
6. Malformed body → **400 at the gateway**; show the Function log proving the backend was never hit
7. Token limit exceeded on the model API → **429**, `x-atlas-remaining-tokens: 0`
8. Backend 503 → APIM retries three times, then a typed error envelope with no internals

Open `apim/policies/atlas-ops-api.xml` and name each block: `validate-jwt`, `rate-limit-by-key`,
`validate-content`, `authentication-managed-identity`, `retry`, `on-error`.

Then the model API policy: `llm-token-limit`, `llm-emit-token-metric` with the `CaseId` dimension.
Switch to the Application Insights chart showing tokens per case and per agent. *"Per-agent cost
attribution, without a line of application code."*

---

## Beat 4 — No keys, anywhere (5 min)

| Step | Action | Say |
|---|---|---|
| 1 | `grep -rniE "(api[-_]?key\|AccountKey\|SharedAccessKey)" src/ functions/ infra/` → nothing | |
| 2 | `az containerapp show ... --query properties.template.containers[0].env` | Endpoints and a client id. No secrets. |
| 3 | Show the identity chain diagram | Web → orchestrator → APIM → Function → Service Bus → worker, every hop an Entra token. |
| 4 | **Break it live:** remove the orchestrator's `Azure Service Bus Data Sender` role, re-run a remediation | Dispatch fails *before* the plan is marked executed. Idempotency record stays clean. Fail closed, not open. |
| 5 | Restore the role, replay from the DLQ | `uv run python scripts/dlq_replay.py --command-id $CMD` |

This is the most persuasive five minutes in the series. Rehearse the role removal and restore until
it is muscle memory, and have the recorded version ready.

---

## Beat 5 — Long-running work done properly (6 min)

| Step | Action | Say |
|---|---|---|
| 1 | Show the `RemediationCommand` message in Service Bus Explorer: `MessageId = plan_id`, `SessionId = service_id`, `traceparent` in application properties | Dedup, ordering, and trace context, all in the envelope. |
| 2 | Dispatch the same plan twice | One execution. Duplicate detection plus an idempotency record — belt and braces, because the message window is 10 minutes and business idempotency has to outlive it. |
| 3 | Run the `RB-0099` plan that fails on step 3 | Retries, backoff, five deliveries, dead letter. Alert fires. |
| 4 | `dlq_inspect.py` then `dlq_replay.py` | Operable, not just observable. |
| 5 | Logic Apps run history for `approve-remediation` | Webhook subscribe/unsubscribe, a 30-minute durable wait, then the callback. This workflow *collects* the decision; the orchestrator still *makes* it. |

---

## Beat 6 — The security posture in one sentence (2 min)

*"The model never holds a credential and has no network path to a backend that does not pass through
the gateway."*

Show the `verify_security.sh` summary table. Then name what you would still add for real production:
VNet integration and private endpoints, customer-managed keys, per-team APIM products, data
residency, and a real ITSM connector instead of our synthetic one.

---

## Close (2 min)

- Next week: none of this proves the agent is *good*. We will measure it, trace it, break it on
  purpose, and put a quality gate in front of the release.

## Timing guard

Beats 3 and 4 are non-negotiable. If short, cut Beat 5 steps 3–4 and show the recorded DLQ clip.
