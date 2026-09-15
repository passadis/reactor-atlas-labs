# PHASE 09 — Azure API Management as the AI gateway (Episode 3)

> Depends on: Phase 08.
> Episode 3 topics: APIM as AI gateway, OpenAPI and MCP tools, typed contracts, authN/authZ, rate
> limiting, schema validation, retries, centralised logging, managed identity.

---

Put APIM between the agents and everything else. After this phase, **no agent and no model has a
network path to a backend that does not pass through APIM.** That is the claim; make it true and
make it demonstrable.

## Before you write code

Query MS Learn MCP for the current pages on: MCP servers in API Management (REST-as-MCP and
existing-MCP passthrough), securing MCP servers, the AI gateway policies (`llm-token-limit`,
`llm-emit-token-metric`, `llm-semantic-cache-lookup`/`-store`, `llm-content-safety`), importing a
Microsoft Foundry API, and monitoring MCP server traffic. Confirm which policies your APIM tier
(StandardV2) supports and record it.

## 1. APIs to create (all via Bicep or `az apim`, not portal clicks)

| API | Source | Path | Purpose |
|---|---|---|---|
| `atlas-ops-api` | `apim/openapi/atlas-ops-api.yaml` | `/ops` | The governed business API |
| `atlas-ops-mcp` | REST-API-as-MCP over `atlas-ops-api` | `/ops-mcp/mcp` | Selected operations exposed as MCP tools |
| `atlas-tools-mcp` | Existing-MCP passthrough to `func-atlas-mcp` | `/tools-mcp/mcp` | The Functions MCP server, governed |
| `atlas-models` | Import from Microsoft Foundry | `/models` | The model endpoint, governed |

Products: `atlas-agent-tools` (the agents' subscription) and `atlas-readonly` (for demo/inspection).
Subscription key stored in Key Vault, surfaced to the orchestrator via a Key Vault reference — never
in code, never printed on stage.

## 2. Policies

Keep every policy in `apim/policies/*.xml`, deployed as `policyFragment`s where reusable, and
version-controlled. Build them up in layers so Episode 3 can peel them back one at a time.

### Global (all APIs)
- `set-header` to ensure `traceparent` and `x-atlas-correlation-id` flow through; generate a
  correlation id if absent.
- `emit-metric` with dimensions `api_id`, `operation_id`, `subscription_id`.
- Frontend response payload logging **0 bytes** globally (required for MCP streaming).

### `atlas-ops-api`
```xml
<inbound>
  <base />
  <validate-jwt header-name="Authorization" failed-validation-httpcode="401">
    <openid-config url="https://login.microsoftonline.com/{{tenant-id}}/v2.0/.well-known/openid-configuration" />
    <audiences><audience>api://atlas-gateway</audience></audiences>
    <required-claims>
      <claim name="roles" match="any"><value>Atlas.Agent</value></claim>
    </required-claims>
  </validate-jwt>
  <rate-limit-by-key calls="120" renewal-period="60"
      counter-key="@(context.Subscription?.Id ?? context.Request.IpAddress)" />
  <quota-by-key calls="5000" renewal-period="86400" counter-key="@(context.Subscription.Id)" />
  <validate-content unspecified-content-type-action="prevent"
      max-size="65536" size-exceeded-action="prevent" errors-variable-name="validationErrors">
    <content type="application/json" validate-as="json" action="prevent" />
  </validate-content>
  <authentication-managed-identity resource="api://atlas-ops-api"
      output-token-variable-name="backend-token" />
  <set-header name="Authorization" exists-action="override">
    <value>@("Bearer " + (string)context.Variables["backend-token"])</value>
  </set-header>
  <set-backend-service backend-id="ops-api-backend" />
</inbound>
<backend>
  <retry condition="@(context.Response.StatusCode == 429 || context.Response.StatusCode >= 500)"
         count="3" interval="1" max-interval="8" delta="2" first-fast-retry="false">
    <forward-request buffer-request-body="true" timeout="30" />
  </retry>
</backend>
<on-error>
  <set-body>@{ /* typed error envelope, no internal detail, includes correlation id */ }</set-body>
</on-error>
```

Add `validate-odata-request`-style **response** validation where the spec defines response schemas,
so a backend contract break is caught at the gateway, not by the model.

### `atlas-models`
```xml
<llm-token-limit counter-key="@(context.Subscription.Id)" tokens-per-minute="20000"
    estimate-prompt-tokens="true" remaining-tokens-header-name="x-atlas-remaining-tokens"
    remaining-tokens-variable-name="remainingTokens" />
<llm-emit-token-metric namespace="atlas">
  <dimension name="Subscription" value="@(context.Subscription.Id)" />
  <dimension name="AgentName" value="@(context.Request.Headers.GetValueOrDefault("x-atlas-agent","unknown"))" />
  <dimension name="CaseId" value="@(context.Request.Headers.GetValueOrDefault("x-atlas-case-id","none"))" />
</llm-emit-token-metric>
<authentication-managed-identity resource="https://cognitiveservices.azure.com" ... />
```
Optionally `llm-content-safety` against an Azure AI Content Safety resource; if you enable it, prove
the block path with a test. Semantic caching (`llm-semantic-cache-lookup`/`-store`) only if you set
`enableSemanticCache=true` in Phase 01 — it needs Azure Managed Redis with RediSearch as an external
cache.

### MCP servers
Apply rate limiting and a custom trace of the calling agent id at the MCP server scope. **Do not
touch `context.Response.Body` in MCP policies** — it breaks streaming.
Enable per-MCP diagnostic payload logging only on `atlas-tools-mcp` (you want tool arguments and
results visible in Episode 4 traces), and note the data-sensitivity caveat in `docs/DEMO-NOTES.md`.

## 3. Repoint everything

- `OpsApiClient` base URL → APIM `/ops`, plus the subscription key header from Key Vault and an
  Entra token for `api://atlas-gateway`.
- Foundry **Toolbox** `atlas-tools`: replace the placeholder with the APIM MCP server
  (`/tools-mcp/mcp`), authenticated with the agent's identity (project managed identity or agentic
  identity — pick and document which, and why). Attach the toolbox to the prompt agent and the
  hosted agent.
- Orchestrator specialist agents use the same toolbox endpoint, so one tool change propagates to
  every surface. Demonstrate that in Episode 3 by changing a tool description in APIM and watching
  all four surfaces pick it up.
- Optionally route the orchestrator's model traffic through `atlas-models` so token metrics and
  limits apply to your custom app too. Do this — it makes the Episode 4 cost/latency dashboards far
  more interesting. Keep the direct Foundry path behind a config flag as a live fallback.

## 4. Prove the governance

Write `scripts/prove_governance.sh` that demonstrates, in order:

1. Direct call to `func-atlas-api` from an unauthorised principal → 401.
2. Call through APIM without a subscription key → 401.
3. Call through APIM with a key but without the `Atlas.Agent` role → 403.
4. Valid call → 200, with `x-atlas-correlation-id` echoed.
5. 121 calls in a minute → 429 with `Retry-After`.
6. Malformed body → 400 from the gateway, backend never invoked (prove with Function logs).
7. Token limit exceeded on `atlas-models` → 429 with `x-atlas-remaining-tokens: 0`.
8. Backend 503 → APIM retries 3 times then returns the typed envelope (use a chaos flag in the
   Function app).

This script **is** a large part of the Episode 3 demo. Make its output readable on a projector.

## Verification — `scripts/verify_phase09.sh`

1. All four APIs exist with the expected operations; MCP servers list their tools.
2. `prove_governance.sh` passes all eight checks.
3. The hosted agent, prompt agent, orchestrator and web app all reach tools **only** via APIM
   (assert: no direct Function invocations in the last N minutes except from APIM's IP/identity —
   check Function logs for caller identity).
4. `llm-emit-token-metric` custom metrics appear in Application Insights with the `CaseId` dimension.
5. APIM diagnostics show one request row per MCP tool call with `gen_ai.*` dimensions.

## Acceptance criteria

- [ ] Zero direct backend access from any agent surface.
- [ ] Every policy is in source control and deployed by IaC.
- [ ] `prove_governance.sh` is green and legible on screen.
- [ ] Changing one tool description in APIM changes behaviour on all four agent surfaces.
- [ ] No key material anywhere except Key Vault.

## Report back

The eight governance proofs' output, the APIM gateway URL, the toolbox id, and the token-metric
chart query.
