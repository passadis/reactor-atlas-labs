# PHASE 10 — Logic Apps and Service Bus: approvals and asynchronous remediation (Episode 3)

> Depends on: Phase 09.
> Episode 3 topics: Logic Apps agent actions, Azure Service Bus, secure and asynchronous tool
> execution.

---

Close the loop: the agent creates a ticket, initiates a real approval workflow, submits a
long-running remediation command, and reports the final result — with the model never touching a
backend directly and never being able to self-approve.

## A. Logic Apps Standard — two workflows

### 1. `approve-remediation` (long-running, human-in-the-loop)

Stateful workflow, UAMI `id-atlas-logic`, no connection strings.

```
Trigger: HTTP request (POST /approvals)   [APIM-fronted, Entra-protected]
  → Parse ApprovalRequest
  → Create/patch the Cosmos `approvals` document (status=pending)
  → Parallel:
      a) Post an adaptive-card-style notification (Teams or Outlook connector if your tenant
         allows; otherwise the "email via SMTP-less" path is a no-op stub that logs)
      b) Post the approval link to the ATLAS approval console
  → Wait for callback  (HTTP Webhook action, subscribe/unsubscribe pattern)
       - timeout 30 minutes (matches the policy gate expiry)
  → On response: validate approver role against entitlements via the ops API
  → Mint a single-use approval token (HMAC over case_id|plan_id|approver|exp, key in Key Vault,
     retrieved with the workflow's managed identity)
  → POST the decision + token back to the orchestrator `/cases/{case_id}/approve`
  → On timeout: POST `/cases/{case_id}/reject` with reason=expired
```

Important: the **orchestrator** is still the authority. The Logic App is a durable notification and
callback mechanism, not the decision maker. Say that explicitly on stage — it is the difference
between "a workflow that approves things" and "a workflow that collects a human decision".

### 2. `enrich-incident` (exposed as a Foundry agent tool)

A short workflow that the agent can call as a tool: given a `service_id` and a time window, it fans
out to several connectors/actions (recent changes, on-call, related tickets, a mock external status
page), aggregates and returns a typed payload.

It **must start with a Request trigger (`When an HTTP request is received`) and end with a Response
action** — that is what makes a workflow callable as an agent tool.

Two ways to expose it; verify which is current for the **new** Foundry portal before choosing, and
record the decision in `docs/API-NOTES.md`:

- **Preferred and portable**: publish the workflow's Request trigger through APIM as an additional
  operation on `atlas-ops-api`, expose it as an MCP tool, and add it to the `atlas-tools` toolbox.
  This keeps the governance story intact (everything through the gateway) and works regardless of
  preview availability.
- **Native integration**: the "add a Logic App action as an agent tool" wizard. Note that the
  documented wizard flow is described against the **classic** Foundry portal and the capability is in
  preview — check it exists for your project type and region before you build the demo around it.
  If it is available, build it *in addition* to the APIM path so Episode 3 can show both, and be
  explicit on stage about which one you would ship.

This is the "a whole workflow can be a tool, not just one API call" beat.

## B. Service Bus — asynchronous remediation

### Dispatch (orchestrator, `DispatchExecutor`)

- Build `RemediationCommand{command_id, case_id, plan_id, steps[], approval_token, issued_at, ttl}`.
- Send to `remediation-commands` with:
  - `MessageId = plan_id` (duplicate detection window 10 min → exactly-once within the window),
  - `SessionId = target_service_id` (ordering per service),
  - `CorrelationId = case_id`,
  - application property `traceparent` (and `tracestate`) copied from the current span,
  - `TimeToLive` matching the approval expiry.
- Before sending, write an `idempotency` document keyed on `plan_id` with `status=dispatched`
  using `if-none-match` semantics. If it already exists, skip the send and return the prior result.
- Authenticate with the orchestrator UAMI (`Azure Service Bus Data Sender`). No connection strings.

### Worker (`src/atlas_worker`)

- Session-aware receiver on `remediation-commands`, `max_concurrent_sessions` bounded.
- **Validates the approval token** before executing anything (signature, expiry, plan hash, single
  use — mark it consumed atomically in Cosmos).
- Executes steps against the simulated estate: each `command_ref` maps to a handler in a registry;
  handlers write state changes to Cosmos and sleep a realistic amount (2–15 s) so the progress
  stream is visible on stage. Include one step that **fails on purpose** for a specific plan
  (`RB-0099`) so you can demo retry/DLQ.
- Emits progress to the `remediation-events` topic after every step.
- Renews locks for long steps; on transient failure, abandons with backoff; after 5 deliveries the
  message lands in the DLQ.
- Writes the final `ExecutionRecord` to `remediation-results` and to Cosmos.
- Extracts `traceparent` from application properties and **links** its spans to the originating
  trace so the whole thing is one waterfall.

### Completion path

- Orchestrator (or a small listener in the web app) consumes `remediation-events` → pushes progress
  over the existing SSE stream → the browser shows a live step-by-step.
- On `remediation-results`, the orchestrator resumes the case, runs `ReportExecutor`, and finalises.

### DLQ handling

- `scripts/dlq_inspect.py` lists dead-lettered messages with reason and correlation id.
- `scripts/dlq_replay.py --command-id` resubmits after a fix, respecting idempotency.
- An Application Insights alert fires on DLQ depth > 0 (Phase 11).

## C. Ticket creation

The agent creates a ticket via `open_ticket` → APIM → `func-atlas-api` → Cosmos. The ticket id must
appear in the final `IncidentReport` and in the web UI. Idempotent on `case_id`.

## Verification — `scripts/verify_phase10.sh`

1. End-to-end: canonical incident → plan → approval required → approval via the console → Service
   Bus command → worker executes → progress events visible → result → final report with ticket id.
   Total wall time recorded.
2. The Logic App run history shows the approval workflow with the webhook wait and the callback.
3. Duplicate dispatch of the same `plan_id` results in exactly one execution.
4. The failing-step plan (`RB-0099`) retries, lands in the DLQ after 5 deliveries, the alert
   condition is met, and `dlq_replay.py` recovers it.
5. A forged approval token is rejected by the worker with an audit record and no side effects.
6. Timeout path: approve nothing for 31 minutes (use a shortened TTL in a test env) and confirm the
   case is rejected with `reason=expired` and no command is sent.
7. One Application Insights transaction contains: web → orchestrator → APIM → Function →
   Service Bus → worker.

## Acceptance criteria

- [ ] The full happy path runs end to end in under 3 minutes, repeatably.
- [ ] Exactly-once remediation proven under duplicate dispatch.
- [ ] DLQ, retry and replay all demonstrated.
- [ ] Approval tokens are single-use and unforgeable.
- [ ] Trace context survives the Service Bus hop.
- [ ] `make demo-reset` restores a clean state in under 60 s.

## Report back

End-to-end timing breakdown, the Logic App run URL, a DLQ screenshot, and the single trace id that
covers all six hops.
