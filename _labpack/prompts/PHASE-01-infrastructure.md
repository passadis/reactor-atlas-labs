# PHASE 01 — Infrastructure as code

> Depends on: Phase 00.

---

Build the complete Azure footprint with `azd` + Bicep, in one subscription-scoped deployment.
Everything in `_labpack/ARCHITECTURE.md` § "Azure resource inventory" and
`_labpack/reference/RBAC-MATRIX.md` must be created here. Nothing in later phases may create
infrastructure by hand.

## Ground rules

- Subscription-scoped `infra/main.bicep` creates the resource group and calls modules.
- Resource token: `uniqueString(subscription().id, environmentName, location)`, truncated to 8
  lowercase chars. All names derive from it.
- Everything gets tags: `azd-env-name`, `project=atlas`, `owner`, `purpose=reactor-demo`,
  `deleteAfter=<ISO date>`.
- **No secrets as outputs.** Output endpoints, resource ids, client ids of managed identities.
- Use `existing` references + `Microsoft.Authorization/roleAssignments` with deterministic GUIDs
  (`guid(scope, principalId, roleDefinitionId)`) so re-runs are idempotent.
- Long-pole resources (APIM, optional Managed Redis) go in their own module so a failure there does
  not roll back everything else. Use `azd` hooks or a `deploymentScript`-free two-phase approach if
  needed, but prefer one deployment with `dependsOn` correctly expressed.

## Modules to write

### `identity.bicep`
Five user-assigned managed identities: `id-atlas-web`, `id-atlas-orchestrator`, `id-atlas-worker`,
`id-atlas-func`, `id-atlas-logic`. Output principalId + clientId for each.

### `observability.bicep`
Log Analytics workspace (90-day retention), workspace-based Application Insights, plus:
- an Application Insights **workbook** resource (definition filled in Phase 11 — create the shell now),
- action group `ag-atlas-ops` with an email receiver (parameterised; default a placeholder).

### `foundry.bicep`
- `Microsoft.CognitiveServices/accounts` of kind `AIServices`, S0, with
  `allowProjectManagement: true`, system-assigned identity, custom subdomain.
- Child project resource.
- Three model deployments using **stable alias names**: `gpt-main`, `gpt-judge`, `embed-main`.
  **Resolve the actual model name and version at deploy time** — do not hardcode a model from
  memory. Drive it from parameters with defaults you verified via
  `az cognitiveservices model list -l <location> -o table`, and fail the deployment with a clear
  message if the model is unavailable.
  - `gpt-main`: GlobalStandard, capacity 200
  - `gpt-judge`: GlobalStandard, capacity 100
  - `embed-main`: Standard, capacity 120
- Project connection to Application Insights (so agent tracing lands there).
- Project connection to Azure AI Search.
- A content filter / RAI policy applied to `gpt-main` (default severity blocking plus prompt-shield
  for jailbreak and indirect attacks, if exposed in your API version). Record what you configured.

### `data.bicep`
- Cosmos DB NoSQL account, session consistency, autoscale 4000 RU/s shared throughput, containers:
  `incidents`, `tickets`, `approvals`, `workflow-checkpoints` (partition key `/workflow_name`),
  `idempotency` (TTL 604800), `sessions`, `eval-runs`. Disable local auth
  (`disableLocalAuth: true`) so only Entra data-plane access works.
- Azure AI Search, Standard S1, 1 replica / 1 partition, semantic search enabled, system-assigned
  identity, `authOptions` disabled in favour of RBAC (`disableLocalAuth: true`).
- Storage account, TLS 1.2, `allowSharedKeyAccess: false` where the Functions plan permits it
  (if Flex Consumption still requires it in your API version, note that in `docs/API-NOTES.md` and
  keep the key in Key Vault). Containers: `eval-artifacts`, `demo-assets`.

### `messaging.bicep`
Service Bus **Premium** namespace, local auth disabled. Entities:
- queue `remediation-commands`: sessions enabled, duplicate detection on (10 min window),
  max delivery count 5, lock duration 5 min, DLQ on expiration.
- queue `remediation-results`: sessions disabled, DLQ on.
- topic `remediation-events` + subscriptions `web-progress`, `audit`.

### `compute-aca.bicep`
- ACR Premium, admin user disabled.
- Container Apps environment with a **Dedicated D4 workload profile** and the Log Analytics
  workspace wired in; enable the environment's OpenTelemetry/Dapr as appropriate but do not depend
  on it for tracing.
- Three container apps using a placeholder image (`mcr.microsoft.com/k8se/quickstart:latest`) so
  provisioning succeeds before any code exists; `azd deploy` replaces them.
  - `ca-atlas-web`: external ingress 443→8000, min 2 / max 10, UAMI `id-atlas-web`.
  - `ca-atlas-orchestrator`: internal ingress 443→8001, min 2 / max 10, UAMI `id-atlas-orchestrator`.
  - `ca-atlas-worker`: no ingress, min 1 / max 5, UAMI `id-atlas-worker`, KEDA Service Bus scale
    rule on `remediation-commands` using managed-identity auth.
- All three get env vars for endpoints and `AZURE_CLIENT_ID` of their UAMI. No secrets.

### `compute-functions.bicep`
Two Flex Consumption function apps on a Flex plan, Python 3.12, UAMI `id-atlas-func`,
identity-based `AzureWebJobsStorage` (`__accountName` / `__credential=managedidentity`
`__clientId=<uami clientId>`):
- `func-atlas-api` — the business REST API.
- `func-atlas-mcp` — the MCP tool trigger server. Note the extra storage **queue** roles it needs.
Both wired to Application Insights via connection string from the observability module.

### `apim.bicep`
API Management **StandardV2**, system-assigned identity, Application Insights logger attached
(`loggerType: applicationInsights`, `credentials` via the instrumentation key reference or MI —
prefer MI if your API version supports it). Create:
- named values (non-secret) for backend URLs,
- a Key Vault-backed named value slot for the subscription key you will store,
- product `atlas-agent-tools` (subscription required, approval not required),
- placeholder API definitions — the actual API/MCP creation and policy XML land in Phase 09, but
  create the APIM instance, logger, product and diagnostics **here** because it is the slowest
  resource to provision.
- Global diagnostics: set **Frontend Response payload bytes to 0** at the global scope. This is
  required for MCP servers to stream correctly; log payloads only per-API where you need them.

### `logicapps.bicep`
Logic Apps **Standard** on a WS1 App Service plan, UAMI `id-atlas-logic`, wired to the storage
account with identity-based connections and to Application Insights.

### `rbac.bicep`
Every assignment in `_labpack/reference/RBAC-MATRIX.md`, including Cosmos DB **data-plane** role
assignments (these are `Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments`, not ARM RBAC —
get this right or every Cosmos call will 403).

## `azure.yaml`

Declare services so `azd deploy` works per-service:

```yaml
name: atlas
services:
  web:          { project: ./src/atlas_web,          host: containerapp, language: py, docker: { remoteBuild: true } }
  orchestrator: { project: ./src/atlas_orchestrator,  host: containerapp, language: py, docker: { remoteBuild: true } }
  worker:       { project: ./src/atlas_worker,        host: containerapp, language: py, docker: { remoteBuild: true } }
  opsapi:       { project: ./functions/ops_api,       host: function,     language: py }
  opsmcp:       { project: ./functions/ops_mcp,       host: function,     language: py }
hooks:
  postprovision:
    shell: sh
    run: ./scripts/postprovision.sh
```

`scripts/postprovision.sh` writes `.env.local` (gitignored) with every endpoint the code needs,
waits for role-assignment propagation, and runs `scripts/verify_phase01.sh`.

## Verification — `scripts/verify_phase01.sh`

Must check and print a table:
1. Resource group exists and contains the expected resource count.
2. Foundry project endpoint responds to an authenticated `GET` (list model deployments).
3. All three model deployments exist with the alias names and are in `Succeeded` state.
4. Cosmos: containers exist; a token from each UAMI can read (use `az rest` with the identity, or
   a tiny Python probe run locally with your own credential plus a check that `disableLocalAuth` is
   true).
5. AI Search service is running and RBAC-only.
6. Service Bus entities exist with the expected session/dedup settings.
7. ACA apps are `Running` with the placeholder image and the right UAMI attached.
8. Both function apps respond on `/api/health` (add a trivial health function now if needed) —
   or at minimum report `state: Running`.
9. APIM provisioning state is `Succeeded` and the Application Insights logger exists.
10. Logic Apps Standard site is `Running`.
11. Every role assignment in the matrix exists (`az role assignment list` per scope + Cosmos SQL
    role assignments via `az cosmosdb sql role assignment list`).

## Acceptance criteria

- [ ] `azd provision` completes green from a clean subscription in one run.
- [ ] Re-running `azd provision` is a no-op (no resource churn, no role-assignment GUID conflicts).
- [ ] `scripts/verify_phase01.sh` prints all-green.
- [ ] No secret appears in `azd env get-values`, Bicep outputs, or container env vars.
- [ ] `docs/API-NOTES.md` records the exact model names/versions deployed and any API-version
      quirks you hit.

## Report back

Provisioning wall time per module, the resolved model names/versions, the APIM gateway URL, the
Foundry project endpoint, and anything you had to work around.
