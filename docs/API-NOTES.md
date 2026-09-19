# API notes

Every entry is a *verified* fact: either an import probe against the installed package, a live Azure
CLI/ARM call, or a fetch from the Microsoft Learn MCP server. Nothing here is recalled.

**Rule:** when the docs and the installed package disagree, the installed package wins, and the
discrepancy is recorded here.

---

## 1. Environment of record

| Item | Value | Verified |
|---|---|---|
| Date of baseline | 2026-09-15 | — |
| Python | **3.13.13** (`uv python install 3.12` also present but unused) | `uv run python -V` |
| Subscription | `AzureDev` / `c6e08b90-7ae0-4f00-8478-a1d4e73991d2` | `az account show` |
| Tenant | `35de4c50-7dcd-4871-8685-61789c017da2` (VISUALSTUDIO-MCT) | `az account show` |
| Primary region | **`eastus2`** (fallback `swedencentral`) | see section 5 |
| azd | 1.34.0 | `azd version` |
| az | 2.90.0 | `az version` |
| Functions Core Tools | 4.12.1 | `func --version` |
| azd `azure.ai.agents` | **1.0.0-beta.15** | `azd extension list` |
| azd `microsoft.foundry` | 1.0.0-beta.2 | `azd extension list` |

---

## 2. Pinned package versions (Phase 00 contract)

Resolved by `uv sync` on 2026-09-15, then pinned with `==`. **Do not change a version without
re-running the symbol probe in section 3 and updating this table.**

### Agent stack

| Package | Pinned | Notes |
|---|---|---|
| `agent-framework` | `1.18.0` | Meta package. No longer installs `agent-framework-lab`. |
| `agent-framework-core` | `1.18.0` | Pulled in by the meta package. |
| `agent-framework-foundry` | `1.13.0` | Foundry chat client. **Imports as `agent_framework.foundry`.** |
| `agent-framework-orchestrations` | `1.1.1` | Sequential / Concurrent / Handoff / Magentic builders. **Not in core.** |
| `agent-framework-azure-cosmos` | `1.0.0b260910` | `CosmosCheckpointStorage`. Pre-release only. |
| `agent-framework-foundry-hosting` | `1.0.0b260821` | `FoundryToolbox`, `ResponsesHostServer`. Beta; may change before stable. |

> **Discrepancy.** PyPI's latest `agent-framework-foundry-hosting` is `1.0.0b260910`, but the
> `agent-framework==1.18.0` dependency graph resolves `1.0.0b260821`. The installed package wins; we
> pin `1.0.0b260821`. Revisit when `agent-framework` next bumps.

### Azure SDKs

| Package | Pinned |
|---|---|
| `azure-identity` | `1.25.3` |
| `azure-cosmos` | `4.17.0` |
| `azure-search-documents` | `12.0.0` |
| `azure-storage-blob` | `12.30.1` |
| `azure-servicebus` | `7.14.3` |
| `azure-monitor-opentelemetry` | `1.8.10` |
| `opentelemetry-sdk` / `opentelemetry-api` | `1.44.0` |
| `azure-ai-projects` | `2.6.1` (group `evals`) |
| `azure-ai-evaluation` | `1.18.5` (group `evals`) |

Transitively present and worth knowing about: `azure-ai-agentserver-core` 2.1.0,
`azure-ai-agentserver-responses` 2.1.0, `azure-ai-agentserver-invocations` 1.1.0 — these back
`ResponsesHostServer`.

### Application

| Package | Pinned |
|---|---|
| `pydantic` | `2.13.5` |
| `fastapi` | `0.141.1` |
| `uvicorn` | `0.53.0` |
| `sse-starlette` | `3.4.11` |
| `httpx` | `0.28.1` |
| `python-dotenv` | `1.2.3` |

### Dev

`ruff` 0.16.7 · `mypy` 2.3.1 · `pytest` 9.1.1 · `pytest-asyncio` 1.4.0 · `pytest-cov` 7.1.0 ·
`openapi-spec-validator` 0.9.0 · `faker` 40.39.0 · `pyyaml` 6.0.3

---

## 3. Symbol probe — confirmed by import, not from memory

Run `uv run python scripts/probe_symbols.py` to reproduce. Output of the 2026-09-15 run:

### `agent_framework` 1.18.0 (325 public names)

| Symbol | Present | Decision |
|---|---|---|
| `Agent` | **YES** | **Use this.** |
| `ChatAgent` | **NO** | Gone. Any sample showing `ChatAgent` is stale. |
| `WorkflowBuilder` | YES | |
| `tool` | YES | |
| `RunContext` | YES | |
| `Executor`, `executor`, `handler` | YES | |
| `AgentExecutor`, `AgentExecutorRequest/Response` | YES | |
| `WorkflowExecutor`, `FunctionExecutor` | YES | |
| `InMemoryCheckpointStorage`, `FileCheckpointStorage` | YES | |
| `CheckpointStorage`, `CheckpointID`, `WorkflowCheckpoint` | YES | protocol + types |
| `register_checkpoint_type` | YES | This is the restricted-unpickler allow-list hook for Phase 07. |
| `response_handler` | YES | Decorator for HITL responses. |
| `WorkflowEvent`, `WorkflowEventType`, `WorkflowEventSource` | YES | |
| `RequestInfoEvent` | **NO** | Events are typed via `WorkflowEventType`, not a dedicated class. |
| `RequestInfoExecutor` | **NO** | Not in core. |
| `WorkflowOutputEvent` | **NO** | Renamed/absorbed; use `WorkflowEvent` + `WorkflowRunResult`. |
| `WorkflowViz` | YES | **Phase 06's Mermaid graph renderer — use this, do not hand-roll.** |
| `ToolApprovalMiddleware`, `ToolApprovalRule`, `ToolMode`, `ToolApprovalState` | YES | Phase 07 tool-approval path. |
| `MCPStreamableHTTPTool`, `MCPStdioTool`, `MCPWebsocketTool` | YES | Raw MCP without a Toolbox. |
| `SequentialBuilder` / `ConcurrentBuilder` / `HandoffBuilder` | **NO** | **In `agent_framework_orchestrations`.** |

### `agent_framework.foundry` (dist `agent-framework-foundry` 1.13.0)

`FoundryChatClient` YES · `FoundryAgent` YES · `FoundryChatOptions` YES

### `agent_framework_foundry_hosting` 1.0.0b260821

`FoundryToolbox` YES · `ResponsesHostServer` YES · `InvocationsHostServer` YES

### `agent_framework_azure_cosmos` 1.0.0b260910

`CosmosCheckpointStorage` YES

### `agent_framework_orchestrations` 1.1.1

`SequentialBuilder` YES · `ConcurrentBuilder` YES · `HandoffBuilder` YES · `MagenticBuilder` YES
Also: `GroupChatBuilder`, `HandoffConfiguration`, `HandoffAgentUserRequest`, `TerminationCondition`,
`MagenticPlanReviewRequest/Response`, `AgentRequestInfoResponse`.

### Exact signatures captured

```python
# agent_framework.tool  -- approval_mode IS supported (Phase 07 tool-level approval)
tool(func=None, *, name=None, description=None, schema=None,
     approval_mode: 'ApprovalMode | None' = None, kind=None,
     max_invocations=None, max_invocation_exceptions=None,
     additional_properties=None, result_parser=None) -> FunctionTool | Callable[...]

# agent_framework.WorkflowBuilder.__init__
#   NOTE: start_executor is a REQUIRED keyword-only arg. There is no set_start_executor() method.
WorkflowBuilder(max_iterations=100, name=None, description=None, *,
                start_executor: Executor | SupportsAgentRun,
                checkpoint_storage: CheckpointStorage | None = None,
                output_from=..., intermediate_output_from=..., output_executors=...)

# builder methods (the complete set)
add_chain, add_edge, add_fan_in_edges, add_fan_out_edges,
add_multi_selection_edge_group, add_switch_case_edge_group, build

# agent_framework.RunContext  -- HITL suspend point
RunContext.request_info(request_data: Any, response_type: type, *, request_id: str | None = None) -> Any
# RunContext members: add_event, get_state, set_state, is_streaming, request_info
```

> **Discrepancy.** The AutoGen migration guide mentions `ctx.set_executor_state()`. `RunContext`
> exposes `get_state` / `set_state` only. Executor-local state uses the `on_checkpoint_save` /
> `on_checkpoint_restore` hooks instead. Confirm the exact hook names against `Executor` in Phase 07.

### Structured output

Not a constructor or `run()` keyword. It lives in the `options` TypedDict:

```python
result = await agent.run(q, options={"response_format": MyModel})
value = result.value  # RAISES pydantic.ValidationError on schema failure

stream = agent.run(q, stream=True, options={"response_format": MyModel})
final = await stream.get_final_response()
value = final.value
```

`ChatOptions[T]`, `ChatResponse[T]`, `AgentResponse[T]` are generic over the response format.
`tools` and `instructions` stay as direct kwargs and are **not** passed via `options`.

> **Behaviour change that affects our retry policy.** Since python-1.0.0b260123, `.value` raises
> `ValidationError` instead of returning `None`. The Phase 03 "validate, retry once, then typed
> failure" loop must be a `try/except ValidationError`, not `if response.value:`.

---

## 4. Structural decisions

### 4.1 Two `azd` projects

`infra.provider` is one value per `azure.yaml`. The Foundry hosted-agent model needs
`microsoft.foundry`; the platform needs `bicep`. They cannot share a file.

| Project | File | Provider | Owns |
|---|---|---|---|
| Platform | `./azure.yaml` | `bicep` | RG, Foundry account + project + 3 model deployments, Cosmos, Search, Service Bus, ACA x3, Functions x2, APIM, Logic Apps, Key Vault, RBAC |
| Agents | `./agents/azure.yaml` | `microsoft.foundry` | hosted agent, toolbox, connections, routines |

The seam is supported: an `azure.ai.project` service can target an existing project with `endpoint:`
and declare no `deployments:`
([author-azure-yaml](https://learn.microsoft.com/azure/foundry/agents/how-to/author-azure-yaml)).
`scripts/postprovision.sh` writes the endpoint into `agents/.azure/<env>/.env`.

Reference shape, from the official sample
(`microsoft-foundry/foundry-samples` `.../responses/04-foundry-toolbox/azure.yaml`):

```yaml
services:
  ai-project:
    host: azure.ai.project
    deployments: [...]          # ATLAS omits this and sets `endpoint:` instead
  my-agent:
    host: azure.ai.agent
    project: src/my-agent
    language: python
    codeConfiguration:
      runtime: python_3_13
      entryPoint: main.py
    uses: [ai-project]
    kind: hosted
    protocols:
      - protocol: responses
        version: 2.0.0
    container:
      resources: { cpu: '0.5', memory: 1Gi }
infra:
  provider: microsoft.foundry
```

### 4.2 Python 3.13

`codeConfiguration.runtime` offers `python_3_13` / `python_3_14` / `dotnet_10`. **3.12 is not
offered**, so the pack's 3.12 mandate cannot hold for the hosted agent. Functions Flex Consumption
in eastus2 supports 3.10–3.14 (`az functionapp list-flexconsumption-runtimes -l eastus2`), and
`agent-framework` requires `>=3.10`. 3.13 satisfies all three. One version across the repo.

### 4.3 Hosted-agent runtime contract

- Listens on **port 8088**, serves a health probe.
- Protocol `responses`, version `2.0.0`.
- `FOUNDRY_PROJECT_ENDPOINT` is platform-injected — never declare it in `env:`. Reserved prefix
  `FOUNDRY_`.
- `container.resources`: cpu `"0.25"`–`"4.0"`, memory `0.5Gi`–`8.0Gi`.
- Deploy mode `code` (ZIP) needs no Docker; `container` does. We use `code`.
- Requires **Foundry Project Manager** at project scope to deploy.

Entrypoint shape from the official sample:

```python
from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import FoundryToolbox, ResponsesHostServer

toolbox = FoundryToolbox(credential)  # resolves TOOLBOX_ENDPOINT, or
# FOUNDRY_PROJECT_ENDPOINT + TOOLBOX_NAME
agent = Agent(client=..., tools=toolbox, default_options={"store": False})
server = ResponsesHostServer(agent)
await server.run_async()
```

`default_options={"store": False}` because the hosting infrastructure manages conversation history.

---

## 5. Region decision: `eastus2`

| Capability | eastus2 | swedencentral |
|---|---|---|
| Responses API + Agents + Private VNet | Yes | Yes |
| All 15 agent tools (MCP, OpenAPI, Function, Search, ...) | Yes | Yes |
| Batch evaluations | Yes | Yes |
| Risk and safety evaluators | Yes | Yes |
| Groundedness Pro | Yes | Yes |
| **AI Red Teaming Agent** | **Yes** | **No** |
| **Protected material evaluator** | **Yes** | **No** |
| APIM StandardV2 | Yes | Yes |
| Functions Flex Consumption | Yes | Yes |
| Service Bus Premium | Yes | Yes |
| ACA workload profiles | Yes | Yes |
| AI Search S1, Managed Redis | Yes | Yes |

Source: [evaluation-regions-limits-virtual-network](https://learn.microsoft.com/azure/foundry/concepts/evaluation-regions-limits-virtual-network),
[limits-quotas-regions](https://learn.microsoft.com/azure/foundry/agents/concepts/limits-quotas-regions).
Sweden Central would silently delete Phase 12 section 4 and the Phase 13 red-team gate step.
eastus2 is a strict superset. Decision: **eastus2 primary**.

### Quota

`az cognitiveservices usage list` returns **`sku.capacity` units (thousands of TPM)**, not raw TPM.
Calibrated against six live deployments — e.g. `GlobalStandard.gpt-4o` `currentValue` 1000 equals
four deployments of capacity 250; `Standard.text-embedding-3-large` limit 350 matches Microsoft's
documented 350K TPM default.

| Alias | Capacity needed | Candidate model | SKU | eastus2 limit | In use | Verdict |
|---|---|---|---|---|---|---|
| `gpt-main` | 200 | `gpt-5.4` (2026-03-05) | GlobalStandard | 10,000 | 0 | OK |
| `gpt-judge` | 100 | `gpt-5.4-mini` (2026-03-17) | GlobalStandard | 10,000 | 10 | OK |
| `embed-main` | 120 | `text-embedding-3-large` (1) | Standard | 350 | 0 | OK, tightest |

**No quota request required.** If `embed-main` ever needs more headroom, move it to GlobalStandard
(limit 10,000).

`text-embedding-3-large` emits **3072-dimension** vectors — set this explicitly on
`idx-atlas-knowledge` or the index is rebuilt after seeding.

---

## 6. Constraints carried into later phases

| Phase | Constraint | Source |
|---|---|---|
| 05, 09 | Toolbox attach is **server-side only**. "Passing a Toolbox client-side doesn't add it to the managed agent." A client-side attach is a no-op for prompt and hosted agents. | [foundry-toolbox](https://learn.microsoft.com/agent-framework/integrations/by-component/tools/foundry-toolbox) |
| 07 | `CosmosCheckpointStorage` auto-creates DB + container; container creation is control-plane and 403s under a data-plane-only role. Pre-create `workflow-checkpoints` (PK `/workflow_name`) in Bicep. | [checkpoints](https://learn.microsoft.com/agent-framework/workflows/checkpoints) |
| 07 | All checkpoint stores use **pickle with a restricted unpickler**. Custom Pydantic types need `register_checkpoint_type`. | same |
| 07 | Resume is `workflow.run(checkpoint_id=..., responses=..., stream=True)`. `run_stream_from_checkpoint()` is **removed**. `message` and `checkpoint_id` are mutually exclusive. | [requests-and-responses-upgrade-guide](https://learn.microsoft.com/agent-framework/support/upgrade/requests-and-responses-upgrade-guide-python) |
| 09 | APIM supports **MCP tools only** — no MCP resources, no prompts. Not supported in workspaces. MCP server = `Microsoft.ApiManagement/service/apis` with `properties.type: "mcp"`, `mcpProperties.transportType: "streamable"`. | [mcp-server-overview](https://learn.microsoft.com/azure/api-management/mcp-server-overview) |
| 09 | All `llm-*` policies work on StandardV2. Not available on Consumption. | [api-management-policies](https://learn.microsoft.com/azure/api-management/api-management-policies#ai-gateway) |
| 09 | **UNVERIFIED:** the pack's claim that global Frontend Response payload logging must be 0 bytes for MCP streaming. Test before treating as a constraint. | — |
| 10 | Logic Apps Standard historically needs shared-key access to host storage. Do not set `allowSharedKeyAccess: false` on it without proving it first. | — |
| 12, 13 | **Target-mode evaluation does not work against the orchestrator.** It requires "synchronous, non-streaming" execution; long-running/streaming agents must use **trace evaluation**. Target mode applies to the hosted agent only. | [quickstart-evaluate-hosted-agent](https://learn.microsoft.com/azure/foundry/observability/quickstarts/quickstart-evaluate-hosted-agent) |
| 12 | AI Red Teaming is **single-turn, text-only**. `Multi turn` and `Crescendo` from the pack are unavailable. Use baseline, `Jailbreak`, `IndirectJailbreak`, `Base64`, `UnicodeConfusable`. | [run-scans-ai-red-teaming-agent](https://learn.microsoft.com/azure/foundry/how-to/develop/run-scans-ai-red-teaming-agent) |

---

## 7. Tooling note: Microsoft Learn MCP

The server is declared in `.mcp.json` but its tools are not always loaded (a non-interactive session
never gets to approve it). It is reachable over raw JSON-RPC regardless — `scripts/mslearn.sh`
wraps `initialize` / `notifications/initialized` / `tools/call` against
`https://learn.microsoft.com/api/mcp`. Tools: `microsoft_docs_search`, `microsoft_code_sample_search`,
`microsoft_docs_fetch`.

Approve the server once in an interactive `claude` session so it becomes a first-class tool; the
prime directive is unenforceable without it.

---

## 8. Provisioning findings (Phase 01, 2026-09-16)

Four `azd provision` attempts were needed. Every failure was a real defect or a real Azure
constraint, and each is now fixed in the template. Recorded so none of them is rediscovered.

### 8.1 AI Search: eastus2 had no capacity, for any SKU

`InsufficientResourcesAvailable`. Probed directly: `basic` fails too, so it is regional capacity
exhaustion rather than a tier or quota problem. Resolution and the full region probe table are in
`CLAUDE.md` section 7.14. Search runs in `eastus`; everything else stays in `eastus2`.

### 8.2 One mutating operation at a time per Cognitive Services account

`RequestConflict: Another operation is in progress on the resource .../accounts/<name>`.

The Foundry **project** is a child of the account exactly as the model **deployments** are, so ARM
started both concurrently and the loser failed. Chaining the deployments to each other was not
enough. The full serial chain that works:

```
account -> raiPolicy -> gpt-main -> gpt-judge -> embed-main -> project -> connections
```

### 8.3 AcrPull must exist BEFORE a container app is created

`ContainerAppOperationError: Failed to provision revision ... Operation expired`, on all three apps,
after 20 minutes each, with **zero revisions created**.

A container app that declares a `registries` entry resolves that registry credential while
provisioning its first revision. With no AcrPull at that moment it does not fail fast - it retries
until the revision timeout. Confirmed by inspection: zero role assignments existed in the resource
group at the time, because `rbac.bicep` runs after `compute-aca`.

Microsoft documents the required order for IaC - create identity, assign acrpull, then create the
app ([managed-identity-image-pull](https://learn.microsoft.com/azure/container-apps/managed-identity-image-pull)).
The three AcrPull assignments now live in `compute-aca.bicep` next to the registry, and each app
carries `dependsOn: [acrPull]`.

**Generalise this:** any role assignment a resource needs *at creation time* belongs in the module
that creates the resource, not in `rbac.bicep`. `rbac.bicep` is for runtime access only.

### 8.4 Bicep caps a deployment at 64 outputs

Hit while adding the search location. The five managed-identity principal-id outputs were dropped;
they are consumed inside the template by `rbac.bicep` and nothing downstream reads them. Recover one
with `az identity show -n <name> -g <rg> --query principalId -o tsv`.

### 8.5 Azure CLI quirks that broke verification, not the deployment

These cost real time and will recur in the Phase 14 security verification, so they are pinned here.

| Symptom | Reality | Use instead |
|---|---|---|
| `az functionapp show --query state` returns **empty** for Flex Consumption | The app is `Running` | `az resource show -g <rg> -n <app> --resource-type Microsoft.Web/sites --query properties.state` |
| `az role assignment list -g <rg>` returns **0** | 77 assignments exist | `az rest` on `.../providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01&$filter=atScope()` |
| `az role assignment list --scope <id>` fails with `MissingSubscription` | az CLI 2.90.0 defect, not a permissions problem | same ARM REST call |

### 8.6 Foundry project data-plane endpoint shapes

Confirmed against the live project:

```
GET {PROJECT_ENDPOINT}/deployments?api-version=v1     -> 200, lists the deployment aliases
GET {PROJECT_ENDPOINT}/models?api-version=v1          -> 200
GET {PROJECT_ENDPOINT}/openai/v1/models               -> 404 without api-version
GET {PROJECT_ENDPOINT}/openai/v1/models?api-version=v1-> 400
```

That 400 is explicit and worth remembering:
`"api-version query parameter is not allowed when using /v1 path"`. The `/openai/v1/...` surface
takes **no** `api-version`; the project-level routes require it. Token scope is
`https://ai.azure.com/.default`, not `management.azure.com`.

`verify_phase01.sh` asserts all three aliases resolve through this endpoint, so a rename or a
half-applied model deployment fails here rather than in Phase 03.

### 8.7 An auto-created resource inflates the resource count

Application Insights creates a `Failure Anomalies` smart-detector alert rule by itself. The resource
group therefore holds one more resource than the template declares. Any check that asserts an exact
resource count will be wrong; `verify_phase01.sh` uses a lower bound.

### 8.8 Never give a Bicep output the same name as a parameter-input env var

`azd provision` succeeded, then the **second** run failed validation:

```
InvalidTemplate: The provided value for the template parameter 'searchLocation' is not valid.
The value 'East US' is not part of the allowed value(s): 'eastus,eastus2,centralus,swedencentral'.
```

`ATLAS_SEARCH_LOCATION` was doing two jobs: it fed the `searchLocation` parameter through
`main.parameters.json` (`${ATLAS_SEARCH_LOCATION=eastus}`), and it was also the name of an output.
azd writes deployment outputs back into the azd environment, so the output overwrote its own input.
Worse, `search.location` returns the **display** form `East US`, not the slug `eastus`, so the next
run failed the `@allowed` list.

This breaks idempotency silently: the first provision is green and the second one cannot even
validate. The output is now `ATLAS_SEARCH_SVC_LOCATION`.

**Rule for every later phase:** an output name must never collide with an env var that feeds a
parameter. When an output echoes an input, give it a distinct name, and remember that Azure
location outputs come back as display names rather than slugs.

---

## 9. Seeding and retrieval findings (Phase 02, 2026-09-18)

### 9.1 Embeddings: the OpenAI v1 surface on the account host, Entra auth

Verified via Microsoft Learn and by a live call:

```python
OpenAI(
    base_url="https://<account>.services.ai.azure.com/openai/v1/",   # account host, not project
    api_key=get_bearer_token_provider(credential, "https://ai.azure.com/.default"),
)
client.embeddings.create(model="embed-main", input=[...])            # the alias, not the model name
```

- `/openai/v1/` takes **no** `api-version` (section 8.6).
- A token provider as `api_key` needs `openai >= 1.106`; installed is 3.14.0.
- `AsyncOpenAI` accepts `Callable[[], Awaitable[str]]`, which pairs with
  `azure.identity.aio.get_bearer_token_provider`. Confirmed from the installed signature.
- Response vectors are 3072-dimensional; the seeder asserts it and fails loudly otherwise.

### 9.2 azure-search-documents 12.0.0: generated models hide their parameters

Every model constructor is `(*args, **kwargs)`, so `inspect.signature` shows nothing and IDE
completion is useless. The real names were read from `indexes/models/_models.py`:
`SearchField(vector_search_dimensions=..., vector_search_profile_name=...)`,
`VectorSearchProfile(name, algorithm_configuration_name)`,
`SemanticPrioritizedFields(title_field, content_fields)`.

`SearchFieldDataType.Collection(...)` works at runtime but is attached to an enum, so mypy reports
`"Enum" not callable`. The index definition uses the documented wire values
`"Collection(Edm.String)"` and `"Collection(Edm.Single)"` instead.

### 9.3 Chunking decided whether the canonical runbook could be retrieved at all

The first chunker split at every `##` heading. A runbook that fits in one ~800-token chunk became
five fragments, each only partly about its subject. Against 500 incident records titled literally
"Checkout Api latency degradation", the canonical query returned **five incidents and no
runbook**; RB-0042 was absent from the top five. Episode 4 asserts `must_cite: ["RB-0042"]`, so that
eval would have failed for a reason nobody would have looked for in the chunker.

Fix: pack sections greedily up to the budget, keep `## ` headings inline, prefix every chunk with
the title plus `Services: ... Risk: ...` (filters narrow retrieval, but only text reaches the
keyword and semantic rankers). Result, hybrid plus semantic reranking:

| Query scope | RB-0042 rank | Reranker score vs next |
|---|---|---|
| unscoped | #3 | 2.37 vs 2.36 - passes the spec, but by 0.01 |
| `doc_type eq 'runbook'` | **#1** | 2.37 vs 2.05 |

**Decision:** the knowledge agent retrieves runbooks and past incidents as **separate scoped
searches**, which is also how ARCHITECTURE.md describes it. `KnowledgeSearch.search(doc_types=...)`
supports it, the golden fixture freezes the scoped ranking, and `verify_phase02.sh` checks both.
The unscoped pass is not relied on; a reranker update could flip a 0.01 margin.

### 9.4 Reference-data containers belong in Bicep, not the seeder

The seeder needs `services`, `changes`, `telemetry`, `people`, `entitlements`. Container creation is
control-plane, the operator holds a data-plane role, so a seeder that created them would 403 - the
general form of the trap in section 7.9. They are in `data.bicep`, together with
`agent-instructions` for Phase 03 (CLAUDE.md 9.1). `seed.py` checks each container exists and, if
not, says to run `azd provision` rather than attempting creation.

### 9.5 Idempotency proven, not assumed

Every search document stores `content_hash`, so a re-seed embeds only changed text and deletes
documents no longer in the seed. The chunking fix exercised it for real: **60 embedded, 608
unchanged and skipped, 242 obsolete fragments deleted**. With unchanged data, `verify_phase02.sh`
re-runs the full seeder and asserts the Cosmos fingerprint is identical (system fields such as
`_ts` excluded) and that zero documents were re-embedded.

### 9.6 Seeded incidents are marked

Seeded incidents carry `source: "seed"`, so `make demo-reset` can delete only incidents raised
during a demo, and seeded-count checks are not skewed by demo traffic.

### 9.7 Harmless but noisy: Cosmos SDK logging on a dead endpoint

The failure-path test points `CosmosStore` at a non-existent account. The SDK logs `Marking
https://... unavailable for Read` before `RepositoryUnavailableError` is raised (in about 1 s). That
line is SDK logging, not an application error. The Phase 03 telemetry module should raise the
`azure.cosmos` logger to WARNING-only for anything that reaches a projected terminal.

### 9.8 Correction to an earlier report

Telemetry is **6,768** windows. The 6,816 reported mid-Phase 02 came from the first generation run,
before the near-duplicate fix changed incident services and timings. File, manifest, git and Cosmos
all agree on 6,768.

## 10. Agent core findings (Phase 03, 2026-09-18)

Installed: `agent-framework` 1.18.0, `agent-framework-openai` 1.14.3, `azure-ai-projects` 2.6.1,
`azure-monitor-opentelemetry` 1.8.10.

### 10.1 Content-filter rejections crash the framework's own exception (workaround in place)

A prompt blocked by the `atlas-strict` RAI policy (jailbreak shield) comes back from Foundry as
`400` with `innererror.code = "ContentFiltered"`. `agent_framework_openai._exceptions` builds
`OpenAIContentFilterException` with `ContentFilterCodes(inner_error["code"])`, and that enum only
has `ResponsibleAIPolicyViolation`. So constructing the exception raises **`ValueError`**, with
the real `openai.BadRequestError` as its `__context__`. Callers that catch
`ChatClientContentFilterException` never see it; the audience would see a stack trace.

**Workaround:** `atlas_agent.factory.model_failure_reason()` walks the cause/context chain and
classifies `content_filter` / `APIStatusError` / framework exceptions. `triage()` and `answer()`
both use it, and return `TriageFailure(reason="content_filtered")` or a `ChatTurn` with the fixed
degraded message. Tested live in `tests/test_guardrails.py::test_jailbreak_is_blocked_by_the_platform_and_typed`.
Re-check on every `agent-framework-openai` upgrade; remove the chain walk when the enum is fixed.

### 10.2 `rai_config.rai_policy_name` needs the full ARM resource ID, and fails open

For a prompt agent (`PromptAgentDefinition(rai_config=RaiConfig(...))`), the bare name
`atlas-strict` is rejected with `The specified RAI policy name ... is invalid`. The docs
([add-hosted-agent-guardrails](https://learn.microsoft.com/azure/foundry/agents/how-to/add-hosted-agent-guardrails))
require the full `/subscriptions/.../accounts/<acct>/raiPolicies/<name>` ID, and warn that on many
subscriptions a nonexistent ID is accepted and **applies no filtering**. `create_prompt_agent.py`
therefore builds the ID from azd env values and GETs it through ARM before creating a version;
a missing policy aborts the create.

### 10.3 The Azure AI Search agent tool needs more than Index Data Reader

The prompt agent's `AzureAISearchTool` (via the project `aisearch` connection, `authType: AAD`)
returned `Access denied ... managed identity access to the search service` although the project
identity holds Search Index Data Reader. The docs
([ai-search tool](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/ai-search)) list
**Search Index Data Contributor + Search Service Contributor** on the Foundry **account** identity.
`rbac.bicep` now adds Search Service Contributor for the account and project identities and Index
Data Reader for the account identity. Data Contributor is deliberately not granted: the tool is
read-only. If the tool still 403s after propagation, Data Contributor is the next step and this
note must be updated.

**Result after provisioning (2026-09-19):** the three roles were enough. No "Access denied" from
the first call after propagation; Data Contributor is not needed.

**But the tool then retrieved badly.** Its default `query_type` is `vector_semantic_hybrid`, which
needs an **integrated vectorizer** on the index, and `idx-atlas-knowledge` had none (ATLAS embeds at
seed time). Forced to `semantic`, the tool ranked incident records above RB-0042 for a Checkout
latency question, and it could not cite a runbook id: the tool exposes title, content and a URL
field, never `doc_id`. An honest side effect worth showing: for a topic with no runbook ("payment
provider timeouts") it answered "I have no grounded source for that", as the instructions require.

Fix, all in place, no re-embedding:
- The Search service's system identity gets Cognitive Services OpenAI User on the Foundry account.
- The index gets an `AzureOpenAIVectorizer` (`embed-main` on `https://<acct>.openai.azure.com`) on
  its vector profile, so the tool can run hybrid queries.
- The prompt agent uses `vector_semantic_hybrid`. With that alone it retrieved the right runbook
  but could only name it by title ("I have no grounded source for a runbook id").
- A `url` field (`{ATLAS_WEB_URL}/kb/{doc_id}`, metadata-only merge in `seed.py`) did **not** help:
  the tool's citation annotation carries the Search endpoint, not a document field. The field stays
  for the web app's citation links. How the tool chooses a URL field is undocumented.
- What worked: runbook titles now lead with the id (`RB-0042 Mitigating elevated p95 latency on
  Checkout API`), as incident and postmortem titles already did. The model reads titles. Only the 60
  runbook chunks were re-embedded; the Phase 02 ranking fixture (RB-0042 first, scoped) still holds.
  Result: the prompt agent answers "The runbook is **RB-0042**".

### 10.4 The knowledge tool was feeding the model 600-character excerpts

`KnowledgeHit.snippet` is the first 600 characters of a chunk, for display. The Phase 03 tool
passed it to the model, which (a) grounded "which runbook" answers on the first few lines of a
procedure and (b) never showed the model the Episode 4 injection payloads, which sit at the end of
RB-0029 and RB-0047. The injection test passed for the wrong reason: the agent could not see the
attack. `KnowledgeHit.text` now carries the full chunk; `search_knowledge` fetches `top * 2`
chunks and merges chunks of the same document, so a runbook split across chunks is one cited
source. The canonical triage goes from about 22K to 27-32K input tokens; no latency change was
measurable.

### 10.5 Agent Framework 1.18.0 behaviour relied on

- `Agent.run()` has no `instructions` parameter. Per-request instruction versions (CLAUDE.md 9.1)
  mean a per-request `Agent`, which is cheap: the expensive clients live in `IncidentAgentRuntime`.
- `STORES_BY_DEFAULT` is True. `default_options={"store": False}` makes the framework inject an
  `InMemoryHistoryProvider`; `AgentSession.to_dict()/from_dict()` then round-trips the full history.
  Sessions persist to the Cosmos `sessions` container (TTL 24 h). Both service-managed and local
  history recalled correctly in a live test; local was chosen so the transcript is ATLAS data,
  visible in Cosmos and cleared by `make demo-reset`.
- Default function invocation: 40 iterations, no call or time cap. ATLAS sets 10 iterations,
  24 calls, 90 s, no detailed errors to the model.
- A tool returning a Pydantic model is serialised with `model_dump` to JSON text for the model.
- Streaming `AgentResponseUpdate.contents` item types used: `function_call` (`name`, `call_id`,
  arguments arrive in fragments), `function_result`, `text`, `usage`.
- Structured output: `options={"response_format": IncidentTriage}`, then `response.value`, which
  raises `ValidationError`. Retry once with the validation errors in the prompt, then
  `TriageFailure(reason="invalid_output")`.

### 10.6 Telemetry pattern

Per the Learn sample for Agent Framework on Azure Monitor:
`configure_azure_monitor(resource=create_resource(...))` then
`enable_instrumentation(enable_sensitive_data=False)`. `atlas_common.telemetry` wraps both,
restricts App Insights log collection to the `atlas` logger namespace, uses managed-identity
ingestion in Azure, and exposes `inject_context` / `continued_from` for hops that are not
auto-instrumented (Service Bus). Verified end-to-end: a local demo run produced 76 spans under one
operation id in Application Insights within about two minutes. The SDK emits W3C Level 2 trace
flags (`-03`, sampled + random), so a `traceparent` validator must not insist on `-01`.

### 10.7 Severity was not stable until the rubric was measurable

With "use judgement" wording, the canonical incident graded Sev1 cold and Sev2 warm. v1 section 8
is now a table keyed on measured values (peak error rate, p95 against SLO, tier), with an explicit
"missing measurements are not evidence of no impact" rule after a probe graded a customer-visible
Search incident Sev4 because its telemetry was empty. Five parallel runs of the canonical triage all
graded Sev2 before the rule was added; `verify_phase03` re-checks it on every run.

### 10.8 Windows console

`print()` of model output containing an em dash raised or printed replacement glyphs under cp1252.
Every demo script calls `sys.stdout.reconfigure(encoding="utf-8")`, and the verify scripts export
`PYTHONIOENCODING=utf-8`.

## 11. Web app findings (Phase 04, 2026-09-19)

Installed: `fastapi` 0.141.1, `sse-starlette` 3.4.11, `starlette` 1.6.0, `uvicorn` 0.53.0,
`playwright` 1.63.0 (ui group only).

### 11.1 Container Apps built-in auth works without a client secret

With no client secret, Easy Auth uses the implicit flow and asks Entra only for an ID token
([authentication-entra](https://learn.microsoft.com/azure/container-apps/authentication-entra)).
That is all the UI needs: sign-in plus the `roles` claim. The App Service
`OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID` managed-identity option is documented for App Service and
Functions, not Container Apps, so it is not used. The `atlas-web` registration needs the
`/.auth/login/aad/callback` redirect URI and `enableIdTokenIssuance`; both are set by
`scripts/create_app_registrations.sh`, which also exposes `api://atlas-web/user_impersonation` with
the Azure CLI pre-authorized, so scripts call the app as a real signed-in user with
`az account get-access-token --scope api://atlas-web/user_impersonation`. No bypass is ever used
against Azure.

Behaviour to know when testing: an unauthenticated request with a browser User-Agent gets a 302 to
Entra; `python-httpx` gets 401; an `X-Requested-With: XMLHttpRequest` request gets 403.

### 11.2 Container Apps ingress does not propagate a client disconnect on SSE

Locally, closing the stream cancels the server-side run within a second. Through Container Apps
ingress the app never learns the client left: the run completed (status `done`, 14 s) after the
client disconnected at 4.9 s. Fix, shipped: `POST /api/chat/{turn_id}/cancel`, sent by the Stop
button and by `navigator.sendBeacon` on `pagehide`; it sets `cancel_requested` on the turn document
(any replica), and the stream loop polls it every 1.5 s and cancels the run. The socket-disconnect
path stays in place for where it does work.

### 11.3 Parallel tool calls make stream-derived durations misleading

gpt-5.4 issues tool calls in parallel and their results stream back together, so timing from the
stream showed about 3 s for every tool in a batch. `atlas_agent.streaming.ToolTimer` is function
middleware (subclass `FunctionMiddleware`, method `process`; a plain callable instance is rejected
by the framework's middleware categorisation) passed per run with `agent.run(..., middleware=[...])`.
The invocation context has no `call_id`, so timings are matched to streamed calls by tool name plus
arguments. In-region results: Cosmos lookups 5-20 ms, knowledge search (embedding + hybrid query)
about 0.5-2.7 s.

### 11.4 Reasoning effort is the latency knob

gpt-5.4 at default effort: canonical triage 19.7-20.9 s. At `reasoning.effort = "none"`:
13.7-15.9 s from a laptop, 13.9 s warm in-region, with identical results on the canonical checks
(Sev2, CHG-2026-0873, RB-0042, 2+ citations) and on the injection scenario (flagged, escalated,
RB-0029 cited) across three runs each. `Settings.reasoning_effort` (env `ATLAS_REASONING_EFFORT`)
defaults to `none`; Phase 12 re-measures quality per effort on the full evaluation set.

### 11.5 Container build gotchas

- A uv virtualenv must be built at its final path. Console-script shebangs are absolute; a venv
  built at `/src/.venv` and copied to `/app/.venv` fails with `exec ... no such file or directory`.
  The Dockerfile sets `UV_PROJECT_ENVIRONMENT=/app/.venv` and starts with `python -m uvicorn`.
- The web image installs the uv workspace, so its build context is the repository root
  (`azure.yaml` `docker.context: ../..`) with an allow-list `.dockerignore`.

### 11.6 Provision must not reset deployed images

The Phase 01 Bicep set every container app's image to the placeholder, so each `azd provision`
reverted running apps until the next deploy. `infra/main.parameters.json` now passes
`SERVICE_<NAME>_IMAGE_NAME` (written by `azd deploy`) into `compute-aca.bicep`, which uses the
placeholder only when a service has never been deployed.

### 11.7 Roles the web app was missing

"This app" mode runs the incident agent inside the web container, so its identity needs what the
agent's tools need: Search Index Data Reader on the search service (verify_phase04 found the
knowledge tool returning `ToolFailure` in Azure; the triage still passed, citing RB-0042 from the
service record's runbook list - the degraded path worked, but the tool must not be degraded), and
Cognitive Services OpenAI User on the Foundry account for query embeddings (the project-scope
Foundry User role does not cover the account's `/openai/v1` endpoint). Both are in `rbac.bicep`,
for the web and orchestrator identities.

### 11.8 Logging

`configure_azure_monitor(logger_name="atlas")` attaches a handler to the `atlas` logger, which
suppresses Python's last-resort stderr output, so exceptions were invisible in the container log
stream. `configure_telemetry` now also installs a console handler when none exists. It also installs
a plain SDK tracer provider when no connection string is set, so `traceparent` propagates even
where nothing is exported.

### 11.9 Contracts later phases must honour

- The "Hosted agent" mode calls the Foundry agent `atlas-incident-agent` on its own endpoint
  (env `ATLAS_HOSTED_AGENT_NAME`; the pack's name, adopted in Phase 05). Until it exists the mode
  answers with a typed `not_deployed` event.
- `GET /api/health` probes the orchestrator at `${ATLAS_ORCHESTRATOR_URL}/api/health` and shows the
  "single-agent path" banner until it answers 200. Phase 06 must serve that route.
- Cases live in the `cases` container (`/case_id`). Phase 07 adds `plan` with `status`, `risk`,
  `tier` and `blast_radius`; the approvals console and `POST /api/approvals/{case_id}` already
  enforce `can_approve` on exactly those fields.

## 12. Hosted agent findings (Phase 05, 2026-09-19)

Installed: `agent-framework-foundry-hosting` 1.0.0b260821, `azure-ai-agentserver-responses` 2.1.0,
`azure-ai-projects` 2.6.1, azd extension `azure.ai.agents` 1.0.0-beta.15 (kept pinned; beta.16 is
available and was deliberately not taken mid-build).

### 12.1 The hosting surface

`agent_framework_foundry_hosting.ResponsesHostServer(agent).run()` serves `POST /responses`
(protocol `responses` 2.0.0) and `/readiness` on port 8088. It accepts any `SupportsAgentRun`, not
only an `Agent`, which is how ATLAS keeps one definition: `atlas_hosted_agent.agent` is an adapter
that builds the `atlas_agent` agent per request, so instructions are still read from Cosmos per
request (CLAUDE.md 9.1). The server replays the conversation as `messages` on every request (and
passes request `options` only to a `RawAgent`), so the adapter runs the inner agent without a
session and decides triage vs prose from whether an assistant message exists. The platform injects
`FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_NAME/VERSION`, `AZURE_AI_MODEL_DEPLOYMENT_NAME`,
`APPLICATIONINSIGHTS_CONNECTION_STRING`; the agent server exports telemetry itself, so the entry
point only calls `enable_instrumentation()` and never `configure_azure_monitor()`.

### 12.2 Packaging: code deploy, remote build, workspace packages as source

`atlas_agent` and `atlas_common` are uv workspace members, not published packages, so a cloud
`pip install` cannot resolve them. `scripts/package_hosted_agent.py` stages
`agents/src/atlas-incident-agent/`: `main.py`, the three packages copied as plain source (instruction
files included), and `requirements.txt` exported from `uv.lock --frozen`, exact pins only. Foundry
installs the requirements (`dependencyResolution: remote_build`). No Docker, no duplicated agent code.
The `agent-framework` meta-package pulled every integration (212 packages); `atlas_agent` and
`atlas_orchestrator` now depend on `agent-framework-core==1.18.0` (108 packages, same imports,
all 93 tests green).

### 12.3 azd quirks

- Git Bash rewrites `--project-id /subscriptions/...` into a Windows path; run with
  `MSYS_NO_PATHCONV=1`.
- `azd ai agent init --project-id` writes `USE_EXISTING_AI_PROJECT`, `AZD_AGENT_SKIP_ACR`,
  `AZD_FOUNDRY_ACR_MODE=none` and account/project names into the env; `scripts/seed_agents_env.sh`
  now writes the same, and MERGES into the file: `azd deploy` records `AGENT_<SERVICE>_*` there and
  `azd ai agent monitor/show` stop working if they are overwritten.
- **`${VAR}` is not substituted inside `policies`** (the docs say it is). Version 1 stored the
  literal `${ATLAS_RAI_POLICY_ID}` as its `rai_policy_name` - per the docs a nonexistent policy
  fails open, i.e. no content filtering. Fixed with a `$ref` to a file the staging script writes
  with the resolved ARM id; `scripts/hosted_agent.py check-guardrail` and `verify_phase05` assert
  every pinned version's policy resolves and exists. Version 1 was deleted.
- `azd deploy` skips an unchanged service (8 s, no new version). A new version needs a real change.

### 12.4 A packaged layout broke `.env.local` discovery

`load_local_env()` assumed `parents[3]` of `config.py` is the repo root. In the hosted zip it is
`/app/atlas_common/config.py`, so startup raised `IndexError` and every invocation returned
`424 session_not_ready` (read with `azd ai agent monitor -s <session> -t console`). It now returns
when the layout is not the repo's. Version 2 had the bug and was deleted (`delete_version(force=True)`
cascades its idle sessions; without `force` it fails while sessions exist).

### 12.5 Identity and access

Each hosted agent gets an Entra agent identity (`AgentDetails.instance_identity.principal_id`),
stable across versions. It can call the model through the project by default; ATLAS grants it
Cosmos DB Built-in Data **Reader**, Search Index Data Reader and Cognitive Services OpenAI User
(embeddings) in `rbac.bicep`, driven by `ATLAS_HOSTED_AGENT_PRINCIPAL_ID` which
`scripts/hosted_agent.py record-identity` writes after the first deploy. Read-only is enough: every
tool is read-only and the platform keeps conversation history. The entry point uses
`DefaultAzureCredential`, the documented path for the platform-managed identity (not a
user-assigned identity we could name), injectable for the Phase 09 swap to the ops API.

### 12.6 Versions, pinning, rollback

The endpoint's default version selector routes to `@latest`. Pinning is
`agents.update_details(name, agent_endpoint=AgentEndpointConfig(version_selector=VersionSelector(
[FixedRatioVersionSelectionRule(agent_version=v, traffic_percentage=100)])))` - seconds, no deploy
(CLAUDE.md 9.2). Pins survive later deploys (a new version does not take traffic until pinned).
`scripts/rollback_agent.sh` pins the highest earlier version that is active and has a resolvable
guardrail; verified 4 -> 3 and back. Version status comes back as an enum
(`AgentVersionStatus.ACTIVE`); compare its `.value`.

### 12.7 Latency: sandbox start, and reusing it

Each new hosted session starts a sandbox: first triage about 28-38 s. Reusing the session
(`extra_body={"agent_session_id": ...}` from the `x-agent-session-id` response header) gives
13-17 s, on par with the ACA app. The agent keeps no state in its sandbox, so the web app starts new
conversations on the most recent warm sandbox, and retries once without it if it has expired.

### 12.8 Toolbox

`project.toolboxes.create_version(name, tools=[AzureAISearchToolboxTool(...)])` then
`toolboxes.update(name, default_version=v)`. Consumers use the default-version endpoint
`{project}/toolboxes/atlas-tools/mcp?api-version=v1`. A prompt agent attaches it server-side as an
`MCPTool` with a `RemoteTool` project connection (`azd ai connection create --kind remote-tool
--auth-type user-entra-token --audience https://ai.azure.com`); verified end to end (the prompt
agent answers RB-0042 through the toolbox). Not declared as an `azure.ai.toolbox` azd service:
its `azure_ai_search` tool must name an azd-managed connection, and ATLAS's search connection is
owned by Bicep - one owner per resource.

### 12.9 Container Apps: rollouts need node headroom

A web deploy sat in `Activating` for over 10 minutes: the new revision's second replica had no
node. The D4 profile was capped at 3 nodes (12 vCPU) and a rollout runs old and new replicas
side by side next to the orchestrator placeholder (2 x 2 vCPU), the worker and Easy Auth sidecars.
`maximumCount` is now 6; `azd deploy` reported success regardless, which is why
`verify_phase04` checks `latestRevisionName == latestReadyRevisionName`.
