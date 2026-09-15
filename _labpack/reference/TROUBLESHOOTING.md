# Troubleshooting

Grouped by where it bites. Each entry: symptom → cause → fix.

---

## Provisioning

**`azd provision` fails on model deployment: "model not available".**
The model name/version you parameterised does not exist in that region, or your quota is zero.
Run `az cognitiveservices model list -l <region> -o table` and `az cognitiveservices usage list -l
<region> -o table`. Fix the parameter, or move region. Never silently substitute a different model —
your eval baseline is model-specific.

**APIM takes forever / times out.**
StandardV2 is usually 15–25 minutes. If the deployment times out, the instance is often still
provisioning. Check `az apim show --query provisioningState` before retrying; retrying a
half-provisioned instance can leave you with a name collision.

**Role assignment conflicts on re-run.**
You used a non-deterministic GUID. Use `guid(scope, principalId, roleDefinitionId)`.

**`azd provision` succeeds but the app cannot read Cosmos.**
You created ARM role assignments. Cosmos data plane needs
`Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments`. Verify with
`az cosmosdb sql role assignment list -g <rg> -a <account>`.

**Functions app will not start with identity-based storage.**
Flex Consumption needs `AzureWebJobsStorage__accountName`,
`AzureWebJobsStorage__credential=managedidentity`, and `AzureWebJobsStorage__clientId` for a
user-assigned identity, plus blob/queue/table data roles. Missing the **queue** roles is the usual
cause, and it is also what breaks the MCP extension.

---

## Agent Framework and Foundry

**`ImportError: cannot import name 'Agent' from 'agent_framework'`.**
Your installed version exposes the older `ChatAgent` / `AzureAIAgentClient` surface. Pick one
surface, record it in `docs/API-NOTES.md`, and use it consistently. Do not mix.

**Structured output is ignored.**
The parameter name for response format differs between versions and clients. Verify against the
installed package. Some models require `strict` schemas with no optional fields at the top level —
flatten if needed, and keep the validate-retry-typed-failure path regardless.

**Tools are never called.**
Three usual causes: the tool description is vague; the instructions do not tell the agent when to
use it; or tool choice is left to `auto` in a case where `required` is correct. Fix the description
first — it is prompt engineering, and Episode 4 will measure it.

**Hosted agent deploy fails with a permissions error.**
You need **Foundry Project Manager** at project scope. Account-level Owner is not sufficient.

**Traces do not appear in the project.**
Confirm the Application Insights connection exists on the project, that you have Log Analytics
Reader, and that you waited 2–5 minutes. Content recording needs its own environment variable and is
off by default.

---

## Workflows

**Checkpoint restore fails with an executor identity error.**
The rebuilt workflow has different node ids. Every agent and executor needs a fixed logical id from
`NODE_IDS`. Never use uuids, case ids or request ids.

**Checkpoint deserialization raises on a custom type.**
The checkpoint store uses a restricted unpickler. Register your Pydantic types via
`allowed_checkpoint_types` (per-store, preferred) or the global registration helper. Add only what
you need.

**Resume re-runs everything.**
You rehydrated from the entry checkpoint rather than the latest, or you rebuilt the workflow without
passing the checkpoint storage. Check `inspect_checkpoints.py` output and the superstep number you
resumed from.

**The concurrent branch runs serially.**
You wired it as two sequential edges. Check the graph output, and assert concurrency by timing in
the test — a comment claiming it is parallel is not a test.

---

## APIM and MCP

**MCP server returns nothing / the client hangs.**
Global diagnostics are logging response payloads. Set Frontend Response payload bytes to **0** at
global scope. Also confirm no policy touches `context.Response.Body` in the MCP scope — that forces
buffering and breaks streaming.

**`tools/list` is empty.**
For the Functions MCP server, the `mcp_extension` system key is missing from the APIM backend
credentials. For REST-as-MCP, you did not select any operations as tools.

**401 from APIM with a valid token.**
Audience mismatch. The token must be for `api://atlas-gateway`, not for the Function app's audience
— APIM mints a *separate* token for the backend with its own managed identity.

**429 from APIM when you did not expect it.**
`llm-token-limit` with `estimate-prompt-tokens="true"` can pre-emptively reject a large prompt.
Check `x-atlas-remaining-tokens`.

**Backend gets the request even though validation should have blocked it.**
`validate-content` needs `action="prevent"` and a matching `content-type`. Requests with an
unexpected content type pass through unless `unspecified-content-type-action="prevent"`.

---

## Service Bus and the worker

**Duplicate executions.**
Duplicate detection only covers its window (10 minutes). Business idempotency must be enforced in
Cosmos with the `plan_id` key and conditional writes. Both, always.

**Messages stuck / not received.**
Sessions are enabled on `remediation-commands` — a non-session receiver will never get them. Also
check the receiver's role is on the right entity scope.

**Lock expired mid-step.**
Renew the lock for long-running steps, or shorten the steps. Five-minute lock duration with 2–15 s
steps should be ample; if you are hitting it, something is hanging.

**Trace breaks at the queue.**
Copy `traceparent` (and `tracestate`) into application properties on send, extract on receive, and
create a span **link** — not a parent-child relationship, since the consumer is a different trace
root in some instrumentations.

---

## Evaluation

**Evaluator fails with a data-mapping error.**
`{{item.*}}` for dataset fields, `{{sample.output_items}}` for structured agent output including
tool calls, `{{sample.output_text}}` for plain text. Tool evaluators need `tool_definitions`.
Check each evaluator's required inputs — they differ.

**Scores swing between runs with no changes.**
LLM judges are non-deterministic. Use a fixed judge deployment and version, run the held-out set
rather than tuning on the main set, and treat a change smaller than `max_composite_drop` as noise.
Report a band, not a point.

**Red teaming reports an unsupported target.**
Check the current supportability matrix for your agent type. Fall back to the callback target
wrapping your endpoint, and say so rather than claiming coverage you do not have.

**Safety category fails on a legitimate incident description.**
Over-blocking. Tune the content filter severity for the specific category, re-run, and document the
change. Do not turn the filter off.

---

## On the day

**Everything is slow.**
Run `scripts/warm.sh`. Cold ACA replicas and cold Functions are the usual cause. Min replicas should
be ≥ 2 — check nobody scaled them down.

**The agent gives a different answer than in rehearsal.**
Check `show_pinned_state.py`. A model deployment version can roll underneath you, and the search
index can drift if someone re-seeded. Re-pin and `make demo-reset`.

**A live step fails on stage.**
Do not debug in front of the audience. Say "this is why we record fallbacks", switch to
`docs/assets/fallbacks/`, and carry on. The runbooks name the fallback for every beat.
