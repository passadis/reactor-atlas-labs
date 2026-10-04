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

---

## 13. Multi-agent workflow findings (Phase 06, 2026-09-20)

Verified against agent-framework 1.18.0 and agent-framework-orchestrations 1.1.1 by live probe.

### 13.1 `WorkflowEvent.emit` and `type='data'` are deprecated

The docstring in `agent_framework/_workflows/_events.py` says so directly: "deprecated alias for
intermediate emissions ... Use `ctx.yield_output(...)` and configure `intermediate_output_from`
instead. Will be removed in a future major release along with the `type='data'` event variant."

So per-node progress is `await ctx.yield_output(NodeProgress(...))` from an executor listed in
`WorkflowBuilder(intermediate_output_from=[...])`. The label is fixed at build time, not per yield:
executors in `output_from` produce `type='output'`, executors in `intermediate_output_from` produce
`type='intermediate'`, and an executor in neither has its yields **silently dropped**. That last
one is the trap - a node that yields without being designated looks like a node that never ran.

### 13.2 Event and builder surface, confirmed by probe

- `WorkflowBuilder(start_executor=..., output_from=[...], intermediate_output_from=[...])`, then
  `add_edge`, `add_fan_out_edges`, `add_fan_in_edges`, `add_chain`, `build`.
- A fan-in target must handle **`list[T]`**, not `T`. Building with a `T` handler raises
  `TypeCompatibilityError` at build time, which is the good outcome.
- `Workflow.edge_groups` (not `edge_groups_by_source`), each with `source_executor_ids` /
  `target_executor_ids`. Groups named `InternalEdgeGroup` with a `internal:` source prefix are the
  runner's own delivery edges and are not part of the designed graph - filter them out or the
  topology JSON shows seven phantom nodes.
- `Workflow` exposes `executors`, `start_executor_id`, `name`, `edge_groups`, `to_dict`.
- `WorkflowViz(workflow).to_mermaid()` renders a fan-in as its own diamond node
  (`fan_in__aggregate__<hash>((fan-in))`), so a Mermaid validator must tolerate node ids it did not
  see declared in `id["label"]` form.
- The streaming overload of `Workflow.run` does **not** accept `include_status_events`; it is a
  keyword of the non-streaming overload only. Runtime accepts it either way, `mypy --strict` does
  not. We drop it and read the event types we need.

### 13.3 `HandoffBuilder` requires `require_per_service_call_history_persistence=True`

`build()` refuses otherwise, naming every participant: "a handoff short-circuits a tool call, and
without this the local history diverges from the service's." It is an `Agent` constructor keyword,
so `SpecialistFactory.build(..., for_handoff=True)` sets it. Nothing in the docs mentions it; the
error message is the documentation.

### 13.4 Handoff mode needs a typed sink, because the specialists cannot use `response_format`

A handoff participant must be free to emit handoff tool calls, so it cannot be pinned to a
structured output. Left alone, the handoff shape produces a transcript and no typed state, and
Episode 2 cannot compare it with the graph.

Resolution: each specialist gets a `record_<dimension>` tool whose schema **is** its output model,
bound to the case (`graph.CaseRecorder`). The shape changes; the contract does not. Three findings
from making it work, all of them live:

- **The tool receives its arguments as parsed JSON.** Declaring `schema=RecordTriageInput` where
  the field is `result: TriageResult` gives a correct nested JSON schema (`$defs` and all), but the
  function is called with `result` as a **dict**. Validate explicitly with `model_validate`; a
  handler typed `result: TriageResult` silently receives a dict.
- **A second recording overwrote a good result with an empty one.** The coordinator re-routed to a
  specialist that had already finished, and its thinner second answer replaced the first. The
  recorder now writes once per dimension and says so in the tool result. Deterministic code owns
  state; the model does not get to clobber it.
- **The coordinator has no state except the transcript**, so it guesses what is still outstanding.
  The `record_*` tool result now ends with the remaining order, computed from the recorder. That is
  deterministic routing advice delivered through an agentic channel, and it removed the wasted laps.

Specialist instructions also need a handoff addendum: their graph-mode rule is "return the model
and nothing else", which in a conversation produces raw JSON in front of an engineer.

### 13.4a The handoff mesh does not always finish, so deterministic code finishes it

Even with the recorder and the routing advice, a live run ended with triage and knowledge recorded
and **no impact analysis**: the coordinator burned its turn limit going round the mesh. The mesh is
the least predictable of the three shapes by construction - the model chooses every hop.

`CaseRunner._fill_gaps` now runs any specialist the mesh never reached, directly, in dependency
order, and writes a `handoff-gap-filled` decision for each one. The agentic part is best effort;
the state contract is not. This is the honest shape of the pattern rather than a workaround for it,
and it is worth showing: the decision log says exactly which dimensions the mesh produced and which
ones plain code had to go and get.

### 13.4b An answer can satisfy the schema and still be useless

A live run returned a `RemediationPlan` with a justification, citations, risk `high` - and **zero
steps** - for a case where the knowledge agent had retrieved RB-0042. The schema was satisfied, so
`response.value` returned happily and the case reached `plan_ready` with nothing to do.

`AgentNode.invoke` now takes an optional `validate` callback, applied after a successful parse and
wired into the same retry path as a schema failure: one retry with the problem appended, then the
dimension degrades. `executors.usable()` holds the checks; there is exactly one so far, and it
enforces what the planner's own instructions already said - zero steps is valid only when no
runbook was found.

The general lesson for structured outputs: `response_format` guarantees *shape*, never *substance*.
Anything you would reject in review needs a check of its own.

### 13.5 The shared client's tool budget is per agent run, and the default was too tight

`function_invocation_configuration` carries `max_function_calls: 24` from Phase 03. The triage
agent, told to look up "every service the report names or implies", enumerated the catalogue, hit
the cap mid-run and reported "I am currently unable to use the incident systems" - which reads like
an outage and is not one. Two changes: the orchestrator process raises the budget
(`ORCHESTRATOR_INVOCATION_LIMITS`, 32 calls / 12 iterations / 120 s), and the triage instructions
state a twelve-call budget. Triage went from 20 calls and 15.2 s to 10 calls and 10.1 s, with the
same answer.

### 13.5a A background run must be awaited before its clients close

`run_sync` returned as soon as the event stream ended, but the stream is released from the driver's
`finally` - so the driving task could still be finalising when the caller closed the runtime. Under
the full test suite this surfaced as `RuntimeError: Event loop is closed` from an httpx transport,
on a *different* test each run. `run_sync` now awaits `run.task`, and `CaseRunner.close()` cancels
anything still in flight before the lifespan closes the clients.

### 13.6 Two replicas plus in-memory runs needs a handoff point

`POST /cases` and `GET /cases/{id}/stream` land on different replicas. So `POST` only writes a
`pending` case to Cosmos and the run starts when a replica serves the stream; that replica owns the
run and writes the final case back. `GET /cases/{id}` reads Cosmos, so it answers on any replica.
This is a last-write-wins document, not a checkpoint - Phase 07 replaces it with
`CosmosCheckpointStorage` and real suspend/resume.

### 13.7 Model calibration, from live runs

Two agent-quality problems that looked like code problems:

- The impact agent listed the entire blast radius as `affected_service_ids` and hinted `Sev1` for a
  20-percent degradation; the `severity=max(triage,impact)` rule then escalated the canonical case
  to Sev1. The fix is in the contract, not the rule: `affected_service_ids` now means *observed*
  degradation, and `severity_hint` has an explicit four-row calibration table.
- The knowledge agent occasionally retrieved RB-0042 and then recorded an empty result, judging it
  "not relevant enough". An empty answer is now only valid when the search itself came back empty.

Both are worth saying out loud on stage: the deterministic rule was correct both times, and the
thing that needed fixing was what the agent was asked to produce.

---

## 14. Checkpointing, recovery and approval findings (Phase 07, 2026-09-21)

Verified against agent-framework 1.18.0 and agent-framework-azure-cosmos 1.0.0b260910 by live
probe. Every item here cost a debugging cycle, and every one of them fails *quietly*.

### 14.1 The checkpoint hooks are async, and a failed checkpoint does not fail the run

`Executor.on_checkpoint_save` and `on_checkpoint_restore` are **coroutines**. Override them
synchronously and the framework logs, once per superstep:

```
Failed to create checkpoint at iteration 0: Executor gate on_checkpoint_save failed.
Note that this does not fail the workflow run.
```

and carries on. The run completes, reports success, and **nothing is written at all**. The first
probe for this phase did exactly that and produced zero checkpoints from a healthy-looking run.

Consequences, all adopted:

- The orchestrator probes the store at startup and puts the answer in `/api/health`
  (`checkpoints: "ok, N checkpoint(s) for ..."`), so a broken store is loud before a demo.
- `verify_phase07` counts checkpoints rather than trusting the run.
- `scripts/inspect_checkpoints.py` says so explicitly when it finds none.

### 14.2 `@response_handler` cannot read string annotations

`@handler` resolves its annotations; `@response_handler` validates its `ctx` parameter with a raw
`inspect.signature`, so under `from __future__ import annotations` it sees a *string* and refuses:

```
ValueError: Response handler parameter 'ctx' must be annotated as WorkflowContext,
WorkflowContext[T], or WorkflowContext[T, U], got WorkflowContext[IncidentCase, NodeProgress]
```

Passing explicit types to the decorator does **not** help - `_validate_response_handler_signature`
validates `ctx` even with `skip_annotations=True`. The only fix is to omit the future import in
that module, which `approval.py` and `tool_approval.py` do, with a comment saying why. Python 3.13
evaluates `X | None` natively, so nothing is lost.

Also note `WorkflowContext[T, Any]` is rejected outright: `Any` is not a valid output type
parameter. Use a real type, or a union of real types.

### 14.3 The checkpoint allowlist must cover every reachable type, including ones you did not write

`CosmosCheckpointStorage` uses JSON plus pickle and restricts deserialisation to a built-in safe
set plus `agent_framework` types. Anything of yours must be registered, and "anything" is broader
than it first looks - this phase needed all five categories:

| Category | Example |
|---|---|
| State models | `IncidentCase`, `PolicyDecision`, `ApprovalRecord` |
| Models *nested* inside them | `Citation`, inside `KnowledgeResult` |
| Messages that travel on edges | `IntakeRequest`, `Partial`, `NodeProgress` |
| The suspension's payloads | `ApprovalRequest`, `ApprovalResponse` |
| **Pydantic's own tzinfo** | `pydantic_core._pydantic_core:TzInfo`, carried by every aware `datetime` |

The failure mode is the dangerous part. A missing type does not raise on **write**; it fails on
**read**, and the checkpoint is silently skipped:

```
Failed to decode checkpoint document: Checkpoint deserialization blocked for type
'atlas_agent.schemas:Citation'
```

So an eight-superstep run reported **two** checkpoints, neither of them the one holding the pending
approval - and the run itself looked perfect. `TzInfo` is the nastiest, because it is not importable
by name; `checkpoints.py` resolves it from a validated value.

Two more details:

- The allowlist string separator is a **colon**: `"my_app.models:MyState"`. A dotted path is
  accepted without complaint and matches nothing.
- `register_checkpoint_type()` is process-wide and applies to storages created before it is called,
  while `CosmosCheckpointStorage(allowed_checkpoint_types=[...])` takes the *names*. We pass both.

### 14.4 The framework binds a checkpoint to its graph, so node ids are load-bearing

Rebuild the workflow with one node renamed and the restore is refused:

```
WorkflowCheckpointException: Workflow graph has changed since the checkpoint was created.
Please rebuild the original workflow before resuming.
```

`Workflow.graph_signature_hash` is the identity, and `WorkflowCheckpoint.graph_signature_hash`
records which graph wrote each one. This is a feature - it stops you resuming into a workflow that
would do something different - but it means a node id derived from a case id or a uuid produces a
workflow that can never resume anything, and the failure appears only in front of an audience.

Hence `node_ids.py`, and
`tests/test_checkpoints.py::test_the_graph_signature_is_stable_across_builds`. Confirmed in
production data: every checkpoint written by the deployed orchestrator carries the same signature,
`0f5d1a902114...`.

### 14.5 Resume: one call, and do not restore into a live workflow

`workflow.run(checkpoint_id=..., responses={request_id: answer}, stream=True)` restores and answers
in a single call. Measured on the real graph: **0.1 s**, with **no agent node re-run** - the
analysis is not repeated, which is the whole claim of the Episode 2 set piece.

But restoring is only for a *new* workflow object. Deliver a response to an object that still holds
the pending request and the framework warns:

```
received a checkpoint restore while 1 request_info event(s) are still pending from an unfinished
request/response cycle; those pending requests will be overwritten by the checkpoint's state
```

So ATLAS has two paths, differing by exactly this:

- **warm** - the case is parked on this replica's live stream: `run(responses=...)`, no
  `checkpoint_id`.
- **cold** - the process was killed, or the approval landed on another replica: build a fresh
  workflow, `run(checkpoint_id=..., responses=...)`.

### 14.6 A re-raised request does not surface in the run that raised it

Calling `ctx.request_info` from inside a `@response_handler` - which is what ATLAS does when an
approval is refused for being out of role - does **not** emit a `request_info` event in that run.
That run's request/response cycle has already been consumed. The re-raised request lands in the
**next checkpoint** and is re-emitted when that checkpoint is restored.

Verified: after a refused approval the case has a *new* parked checkpoint at a higher iteration,
and restoring it re-emits the original `ApprovalRequest` with its `required_roles` intact. So a
wrong-role approval leaves the question askable rather than dropping it - but the assertion has to
be on the checkpoint, not on the event stream.

### 14.7 Tool-level approval is a different mechanism with a different lifetime

`@tool(..., approval_mode="always_require")` makes the framework emit a `function_approval_request`
content instead of running the function. Confirmed live: the tool body does not execute. Release it
with `Content.from_function_approval_response(approved=..., id=request.id, function_call=...)` sent
as a user `Message` on the **same session** - the original `function_call` goes back with the
answer, so the framework matches it to the exact call, with those exact arguments.

`ApprovalMode` is `Literal["always_require", "never_require"]`. `ToolApprovalMiddleware` is a
separate thing again: standing approvals held in session state, needing an `AgentSession`.

When each fits is the Episode 2 point, and the difference is where the pause *lives*: a workflow
request-info pause is a Cosmos checkpoint and survives a deploy; a tool approval pause is session
state and survives only as far as the session store does. ATLAS uses the workflow gate for the
plan, and demonstrates tool approval for the single destructive call, because only the latter can
show a human the **arguments** before they are used.

### 14.8 A suspended workflow never produces an output, so state has to leave another way

The runner only ever saw an `IncidentCase` on the final `IncidentReport`. A case parked on a human
has no report by definition, so while suspended there was nothing to persist, and
`GET /cases/{id}` answered from the stale `pending` document.

Fix: the policy gate and the approval node each `yield_output(case)` alongside their `NodeProgress`,
with the context typed `WorkflowContext[IncidentCase, NodeProgress | IncidentCase]`. A union as the
workflow-output parameter is accepted, and it keeps the executors ignorant of storage.

Ordering matters too: the pending-approval document is written **before** the `approval_required`
event is published, or a console that reacts immediately queries an approvals list that does not
contain the case yet.

### 14.9 `EntitlementRepository` needed a `list_all`

The gate names *who could* approve a plan, which needs the whole entitlement table, not one row.
Added `list_all()` alongside `for_role()`. Reading the table per decision was rejected: reference
data is loaded once at startup, because a gate that re-reads Cosmos on every decision is a gate
that can time out.

### 14.10 The change-freeze rule had to split in two (decided 2026-09-21)

Phase 07 section 3 rule 1 says: block if any step is destructive and targets a service with
`change_freeze=true`. The seed freezes `svc-identity`, `svc-ledger` and `svc-payments` - and the
canonical incident is *caused by* a payments deploy, so every real plan remediates `svc-payments`
destructively. Six stored canonical cases were checked: **all six were blocked**, and the case
never reached `awaiting_approval`, which would have deleted the Episode 2 set piece entirely.

That is a genuine collision between the seed and `ARCHITECTURE.md`'s canonical flow, which expects
the canonical case to reach approval. Resolved by splitting the rule, with the presenter's
agreement:

- **frozen and NOT implicated in this incident** - blocked outright. A plan reaching for a frozen
  service the incident has nothing to do with is the plan being wrong. The refusal path stays real.
- **frozen and implicated** - break glass: allowed, but only a `director` signs it, and the reason
  says so in as many words. This is how a change freeze actually works.

`implicated` is computed by `policy.implicated_services()` from intake and the two analysis
dimensions - never from the plan - so a plan cannot talk its way into break-glass by naming a
service the incident never touched.

### 14.11 Two workflow bugs found while closing Phase 07 (2026-09-30)

**Gap-fill used a node with no handler.** The handoff mode's `_fill_gaps` finished a stalled mesh
with a bare `AgentNode`. The framework refuses to build a workflow containing an executor that has
no `@handler`, and the error (`Executor AgentNode has no handlers defined`) surfaced only when the
mesh actually stalled - so the handoff check in `verify_phase06` failed intermittently. Fixed by
using `ChainNode`, which has the handler.

**Handoff mode bypassed the policy gate.** Graph and sequential modes route the plan through
`PolicyGateExecutor` and `ApprovalExecutor`; the handoff mesh ended at the planner and produced a
report directly. A plan drafted in handoff mode was therefore never authorised - the exact
"never let the model decide whether it is allowed to act" failure CLAUDE.md section 1 forbids.
Fixed with an approval tail: `graph.build_approval_tail()` is a fixed three-node chain (policy
gate, approval, report) with its own stable workflow name `atlas-incident-approval`, so its
checkpoints resume like any other. When the gate is on, every handoff case is fed through it. A
case that ends with no plan passes through untouched rather than being parked on an empty plan.

## 15. Functions tools findings (Phase 08, 2026-09-30)

### 15.1 Approval tokens are unguessable and stored, not signed (deviation)

PHASE-08 section B describes a *signed* approval token. Signing needs a key, and CLAUDE.md section 1
permits exactly one secret (the APIM subscription key). A token signed with a managed-identity
key-vault key would need Key Vault crypto on every submit and a rotation story for no extra safety
here, because the ops API has to look the token up anyway to make it single use.

Chosen: 256 bits from `secrets.token_urlsafe(32)`, prefixed `atlas_apv_`, and only its SHA-256 is
stored (`approvals` container, partition = case). `check` compares case, plan, spent and expiry
explicitly - the case comparison is explicit even though the case is also the partition key, so an
authorization decision never depends on storage layout. `consume` spends the token with an ETag
conditional replace, so two concurrent submits cannot both win. Every refusal writes an
`approval-refusal` record carrying the correlation id. Forging is as hard as guessing 256 random
bits; a leaked database gives hashes, not tokens.

### 15.2 `listServices` added to the pack's operation list

The pack's OpenAPI list has no way to enumerate services, but the agent's `list_services` tool and
the orchestrator's reference-data load both need one. Added as `GET /services`. Without it the
HTTP-backed agent could not keep parity with the Cosmos-backed one.

### 15.3 Entra v2 tokens carry the app GUID as `aud`

`atlas-ops-api` issues v2 access tokens. A v2 token's `aud` is the application's **client id**, not
the `api://atlas-ops-api` identifier URI. With only the URI in `allowedAudiences`, App Service
authentication rejected every managed-identity call with a 401 that looked like an RBAC or policy
problem. `compute-functions.bicep` lists both. Phase 09's `validate-jwt` must do the same.

### 15.4 The MCP endpoint and its key

Installed `azure-functions==2.3.0` with extension bundle `[4.*, 5.0.0)`: `@app.mcp_tool_trigger`
exposes a Streamable HTTP server at `/runtime/webhooks/mcp` (the legacy SSE endpoint
`/runtime/webhooks/mcp/sse` also exists; do not use it). It is protected by the `mcp_extension`
**system key**, sent as `x-functions-key`. That key is not in source, `.env` or Bicep:
`scripts/mcp_probe.py` reads it from the management plane at run time, and in Phase 09 APIM holds
it as a Key Vault-backed named value so no client ever sees it. The MCP extension also keeps state
in storage **queues**, so the function identity needs the two queue roles and
`AzureWebJobsStorage__queueServiceUri`, or tool calls fail at runtime with no clear error.

### 15.5 `inject_context` returns the carrier; it does not fill yours

`atlas_common.telemetry.inject_context(headers)` was being called for its side effect and its
return value discarded, so outbound ops-API calls carried no `traceparent`. It returns a new dict.
`ops_client` now assigns it: `headers = inject_context()`.

### 15.6 Cold start is a demo problem; `alwaysReady` is the fix

Measured on the canonical incident with the agent tools on the ops API: **71 s cold** against
**44 s warm** (direct Cosmos: 50.8 s). The 27 s is Flex Consumption cold start landing mid-run.
Both function apps now keep one `http` instance always ready. That costs a small always-on charge;
the architecture's rule "never cold-start during a demo" is worth it.

### 15.7 `atlas_common` is vendored into each function package

Flex Consumption builds from `requirements.txt` remotely and cannot resolve a uv workspace member.
`scripts/vendor_atlas_common.sh` copies `src/atlas_common/atlas_common` into each function folder
as an `azd` prepackage hook; the copies are git-ignored. One source of truth, no divergence.

### 15.8 The switch

`Settings.ops_api_url` (`ATLAS_OPS_API_URL`) is the only thing that changes. Empty: the agent tools
use the Cosmos repositories. Set: they use `repositories/http_repos.py`, which mirror the Cosmos
repositories' signatures and raise the same `RepositoryUnavailableError`, so the tools, the prompts
and the degraded-mode messages are identical. Bicep sets it on the web app and the orchestrator
from the ops API's output; Phase 09 repoints that one value at the APIM gateway.

`scripts/compare_tool_backends.py` runs the canonical incident both ways. Severity, suspected
change, blast radius, Tier 1 exposure and the gate decision matched; approver roles and plan length
are model output and are not compared.

The hosted agent (Phase 05) still reads Cosmos: it is deployed through `agents/azure.yaml`, and
routing it through the gateway is Phase 09 work, not a second switch here.

## 16. AI gateway findings (Phase 09, 2026-09-30)

### 16.1 Sources checked

Microsoft Learn, 2026-09-30: `api-management/mcp-server-overview`, `export-rest-mcp-server`,
`expose-existing-mcp-server`, `secure-mcp-servers`, `manage-mcp-servers-rest-api`,
`monitor-mcp-servers`, `api-management-policies#ai-gateway`, `llm-token-limit-policy`,
`llm-emit-token-metric-policy`, `api-management-howto-app-insights#emit-custom-metrics`,
`foundry/agents/how-to/tools/toolbox`, `foundry/agents/how-to/tools/model-context-protocol`,
`azure-functions/functions-mcp-tutorial`, `functions-bindings-mcp`.

StandardV2 supports every policy used here: `llm-token-limit`, `llm-emit-token-metric` (V2 "Yes"),
`validate-jwt`, `validate-content`, `rate-limit-by-key`, `quota-by-key`, `retry`,
`authentication-managed-identity`, `emit-metric`, policy fragments. MCP server management needs
ARM API version `2025-09-01-preview` or later; everything else uses `2024-05-01`.

### 16.2 `mcpProperties.endpoints` is a dictionary, not an array (doc and Bicep types are wrong)

`manage-mcp-servers-rest-api` shows, in REST, ARM and Bicep alike:

    mcpProperties: { transportType: 'streamable', endpoints: [ { name: 'message', uriTemplate: '/mcp' } ] }

The live API (2025-09-01-preview, eastus2, 2026-09-30) rejects that at validation:
`Cannot deserialize the current JSON array ... into type Dictionary<String, McpEndpointContract>
... Path 'mcpProperties.endpoints'`. What deploys is a map keyed by endpoint name:

    endpoints: { message: { uriTemplate: '/mcp' } }

Bicep's bundled type for the property still says `McpEndpoint[]` and emits BCP036 as a warning;
the live API wins (CLAUDE.md section 0). Recorded in `infra/modules/apim-gateway.bicep`.

### 16.3 The toolbox reaches APIM as the project's managed identity (decision)

PHASE-09 section 3 asks to pick project managed identity or agentic identity for the toolbox's
MCP servers. Chosen: **project managed identity**, audience `api://atlas-gateway`, which holds
`Atlas.Agent`. The toolbox is shared - the prompt agent and any future consumer reach the gateway
as one governed principal, which is also what the per-agent rate limit counts. A `project-managed-
identity` RemoteTool connection is supported (`azd ai connection create --auth-type
project-managed-identity --audience ...`).

Consequence: the two MCP servers are **not** subscription-gated (`subscriptionRequired: false`).
A connection carries one credential; a subscription key stored in a Foundry connection would sit
outside Key Vault, which CLAUDE.md section 1 forbids. The MCP servers are gated by identity instead
(Entra token + `Atlas.Agent`) and rate limited per calling identity. The REST APIs (`/ops`,
`/models`) require identity AND a subscription key.

Toolbox tool names are prefixed by server label: `atlas_ops___lookup_service`. The in-process
agents keep the bare names; the descriptions are the same text.

### 16.4 The Function Apps accept only APIM

Phase 08 checked the audience only, so any tenant identity with a token for `api://atlas-ops-api`
could call the ops API directly. `infra/modules/functions-auth.bicep` adds
`defaultAuthorizationPolicy.allowedPrincipals.identities = [<APIM principal id>]` to both apps.
The MCP app drops its `mcp_extension` system key: `AzureFunctionsJobHost__extensions__mcp__system__
webhookAuthorizationLevel=Anonymous` plus the same platform lock, the pairing
`functions-mcp-tutorial` documents. There is no function key anywhere to leak.

Operator consequence: `scripts/mcp_probe.py` (direct, system key) no longer works by design. The
tools are listed through the gateway by an agent identity - the web app's `/api/gateway/probe`.

### 16.5 One description, four surfaces

APIM holds the tool descriptions of `atlas-ops-mcp` (`apim/mcp/atlas-ops-mcp-tools.json`, one
`apis/tools` resource each). The prompt agent sees them through the toolbox. The web app, the
orchestrator and the hosted agent run the same tools in-process; `atlas_common.tool_catalog`
reads `tools/list` from the same server (60 s cache) and overrides each in-process tool's
description by name, **in place** - the orchestrator builds its agents once per workflow shape,
and Agent Framework reads `FunctionTool.description` every time it sends the schema, so mutating
the live object reaches an already-built agent. A dead gateway keeps the last good text or the
docstrings; `tests/test_gateway.py` keeps the docstrings equal to the governed JSON so an outage
never silently changes what the model is told.

### 16.6 Where the gateway is NOT on the path (said out loud)

- **Model calls.** The orchestrator's own model traffic stays direct (CLAUDE.md 7.12). `/models`
  exists with token limits and metrics and is demoed with a direct call.
- **Knowledge retrieval.** `search_knowledge` reads Azure AI Search directly (the toolbox's
  `atlas_knowledge` likewise, through the project's search connection). The operational
  backends - catalogue, incidents, changes, telemetry, tickets, remediation - are all behind APIM.
- **The app's own state.** Cases, checkpoints, sessions and instructions stay in Cosmos: that is
  the application's database, not a tool backend.

### 16.7 Response-body logging and MCP

`export-rest-mcp-server` troubleshooting: "MCP server streaming fails when diagnostic logs are
enabled | Logging of response body ... interferes with MCP transport | Disable response body
logging at the All APIs scope". That settles CLAUDE.md 7.8's open question: global body logging
stays at 0 bytes. Payload logging is enabled per server on `atlas-tools-mcp` only (the
`monitor-mcp-servers` path).

### 16.8 Custom metrics need `metrics: true`, not the preview opt-in

`emit-metric` and `llm-emit-token-metric` write nothing until the APIM diagnostic has
`metrics: true` (now in `apim.bicep`). The App Insights "custom metrics with dimensions" opt-in
only affects the preaggregated metrics store; the `customMetrics` table always carries every
dimension (`metrics-overview`), so `CaseId` and `AgentName` are queryable without it.

### 16.9 A passthrough MCP server authenticates through a backend entity, not a policy

First deployment: `atlas-tools-mcp` answered `initialize` itself ("serverInfo: Azure API
Management"), then `tools/list` returned `[]` in 15 ms and every `tools/call` a 500 - with an
`authentication-managed-identity` + `set-header` in the server's policy, exactly as the
`secure-mcp-servers` page suggests for outbound auth.

Cause: APIM discovers a passthrough server's tools itself, outside the request pipeline, so the
policy never runs for that call. The Function App (locked to APIM's identity, 16.4) refused the
unauthenticated discovery and APIM cached an empty tool list. Proven on a scratch server the same
day: identical settings plus `backendId` pointing at a `backends` entity with
`credentials.managedIdentity.resource = api://atlas-ops-api` listed all six tools and served a
real `get_oncall` call. `apim-gateway.bicep` now does exactly that; the policy only checks the
agent and strips its token.

The same scratch test showed `mcpProperties.transportType` is accepted but not returned on GET;
the endpoint map is what the service keeps. Harmless, noted so nobody chases it.

### 16.10 The toolbox caches the MCP tool list, and asking keeps the cache alive

The pack says a tool-list change reaches agents in "up to 60 s" (CLAUDE.md 9.6). Measured
2026-09-30 by changing one `atlas-ops-mcp` description in APIM and watching every surface:

| How the toolbox was polled | Prompt agent saw the change |
|---|---|
| every ~10 s | not within 420 s |
| every 60 s (one run) | at 267 s |
| every 60 s (`scripts/tool_description_change.py`) | at 68 s |

The toolbox cache behaves as a **sliding expiry**: each `tools/list` against the toolbox keeps its
cached copy of the APIM server's tools alive, so frequent asking delays the very refresh being
watched. The script now asks the toolbox at most once a minute. The in-process surfaces read APIM
directly (`atlas_common.tool_catalog`, 60 s cache): orchestrator 35.6 s, web 68.0 s, hosted agent
105.8 s (on its next request) in the same run - all four in under two minutes.

A fourth run with the same 60 s polling saw nothing in 420 s. The cache is not something to wait
on. What is deterministic: **publishing a new toolbox version** with the same definition makes
Foundry rediscover every MCP server's tools at once - the prompt agent had the new text 9 s after
the publish. `scripts/upsert_toolbox.publish()` does exactly that (same tools, new version, made
default; tagged `published_for` in the metadata), and `scripts/tool_description_change.py` calls it
after the change and again after the restore. Final measured run, 2026-09-30: prompt agent 4.0 s,
web 4.0 s, orchestrator 17.5 s, hosted agent 56.7 s (its next request).

So the honest description of the governance model is: APIM holds the description; the in-process
agents follow it within a minute on their own; the prompt agent follows it when the toolbox is
republished - one command, no agent redeploy. Toolbox versions accumulate with each publish; they
are cheap and immutable, and `upsert_toolbox --check` still compares the definition fingerprint.

### 16.11 Windows tooling traps met while verifying (2026-09-30)

- `subprocess.run(["bash", ...])` from Python on Windows starts **WSL's** `System32ash.exe`
  (CreateProcess searches System32 before PATH), which inherits none of the environment. Use
  `shutil.which("bash")` (Git Bash). `verify_phase09` does.
- `Path.write_text` on Windows writes CRLF. A shell script edited that way fails under a plain
  bash with `$'': command not found`. `.gitattributes` keeps the index LF; edit tooling now
  writes bytes. 173 working-tree files were normalised to LF.
- `az.cmd` mangles JMESPath with `==`, `||` or `[]` projections, and multi-line `--body` JSON.
  Filter in Python, pass bodies as `@file`, and query App Insights over REST where a hung az child
  could outlive `subprocess`'s own timeout.
- `AzureCliCredential.get_token("api://x/scope")` sends a delegated scope as `--resource`; use
  `az account get-access-token --scope` for delegated operator tokens.

## 17. Logic Apps and Service Bus findings (Phase 10, 2026-09-30 / 10-01)

### 17.1 An unchanged APIM policy fragment can hang a provision for an hour

`azd provision` re-sends every resource, changed or not. On the first Phase 10 provision the
`atlas-agent-auth` policy fragment - content identical to Phase 09 - sat at
`provisioningState: InProgress` for about an hour while the APIM instance itself reported
`Succeeded` and the gateway kept serving (an agent call through it returned 200 throughout). Every
resource that depends on the fragments (API policies, the new workflows policy) waited behind it.

Cancelling the ARM deployment did not clear the fragment's state. Re-sending the same content with a
direct PUT (`az rest --method put .../policyFragments/atlas-agent-auth` with `If-Match: *`)
completed in about a second, and the re-run provision then went through.

Recovery, if it happens during the T-10 routine: check the gateway still serves
(`/api/gateway/probe` valid), `az deployment group cancel -n apim-gateway`, re-PUT the stuck
fragment from `apim/policies/fragments/`, re-run `azd provision`. Never during a broadcast - it is
all control plane (CLAUDE.md section 9).

### 17.2 Easy Auth `excludedPaths` matches exactly - use a trailing `/*`

The Logic App's Easy Auth excluded `/runtime/webhooks/workflow` so the runtime's webhook callbacks
and management calls stay reachable. Probed 2026-10-01: the excluded path itself answered with the
runtime's own bare `Bearer` challenge, but `/runtime/webhooks/workflow/api/management/workflows`
got Easy Auth's `Bearer realm=...` 401 - the exclusion is an exact match, not a prefix. Every
webhook callback (the approval wait) would have been refused. `'/runtime/webhooks/workflow/*'`
excludes the subtree while `/api/{workflow}/triggers/...` stays locked to APIM; both verified the
same way. The docs describe `excludedPaths` only as "the paths ... not redirected to the login
page".

### 17.3 One invalid parameter type and no workflow loads at all

`logicapps/parameters.json` declared the APIM key as `SecureString` (the ARM-template habit). The
Standard runtime supports only `Array, Bool, Float, Int, Object, String` there, and rejected the
whole file: `Workflow processing failed. Error: 'The parameter with name 'apimKey' has an invalid
type of 'SecureString'`. ARM still listed both workflows as `Healthy` and the zip deploy
succeeded; only the runtime's management API (`WorkflowNotFound`) and an App Insights trace
said otherwise. The key is now a `String` parameter (still a Key Vault reference resolved by the
platform) and every action that sends it has `runtimeConfiguration.secureData` on its inputs, so
run history redacts it. Check App Insights `traces` for `Workflow Error` after every deploy.

### 17.4 The Service Bus SDK overwrites `traceparent` - send inside the request's span

Once the Functions exported telemetry (17.5), the worker's spans appeared in App Insights - in a
DIFFERENT trace from the request that queued them. The ops API set `traceparent` on the message
explicitly, but the Service Bus SDK traces its own send and stamps `traceparent` from the CURRENT
OpenTelemetry span, overwriting it. The Python Functions worker does not put the incoming request's
context into OpenTelemetry, so the current span was a fresh root and the trace broke at exactly the
Service Bus hop. The enqueue now runs inside a PRODUCER span continued from the request's trace
(`functions/ops_api/function_app.py`), so whatever the SDK stamps is in the right trace.

### 17.5 Telemetry silently off where Agent Framework is not installed

`atlas_common.telemetry` built its OpenTelemetry resource with Agent Framework's `create_resource`.
The worker and the Functions do not install Agent Framework, so the import failed and the whole
Azure Monitor setup was skipped: `Azure Monitor export disabled: ModuleNotFoundError`, one line in
the console, nothing in App Insights. It now falls back to a plain OpenTelemetry `Resource`.

### 17.6 A decision can beat the approval run's callback registration

The approval run subscribes about 1.5 s after the case parks. An approval inside that window (the
end-to-end verifier approves at once) found no callback, and the run waited out its whole window.
`ApprovalWorkflow.complete_eventually` retries for about 30 s, in the background so dispatch never
waits on a notification workflow. And a handled webhook timeout now ends the run `Succeeded`
(Terminate) instead of `Failed`.

### 17.7 Deviations from PHASE-10, recorded

- **The orchestrator mints the approval token, not the Logic App.** The pack has the workflow mint an
  HMAC token with a Key Vault key. ATLAS keeps the Phase 08 design (15.1): an unguessable token
  stored as a hash, minted by the authority after its own check, spent once by the ops API. The
  Logic App collects and relays; it holds no key and decides nothing.
- **The worker proves authorisation from the spent-token record, not from a token in the message.**
  The message carries no token at all; a command is executable only if the ops API spent a token
  for exactly that operation (`atlas_worker/authorise.py`).
- **The ticket is opened by the dispatcher (code), not by an agent tool call.** Idempotent on the
  case id; the agent-facing `open_ticket` tool remains for the prompt agent.
- **Dead-lettered is not terminal for the case.** It stays `executing` with remediation status
  `dead_lettered`, so a replay after the fix can still complete it.
- **The approval callback goes through the web app**, which only the Logic App's identity may call:
  the orchestrator has no external ingress.

### 17.8 A cold resume dispatched under the wrong trace

With two orchestrator replicas, an approval that lands on the replica NOT running the case resumes it
cold, from the checkpoint, inside the approval HTTP request - and the dispatch then ran under that
request's trace. The remediation (APIM -> Function -> Service Bus -> worker) was one unbroken chain,
but hung off the approval's waterfall instead of the incident's. The dispatch now always continues
the trace stored on the case (`case.traceparent`), so warm or cold, either replica, the incident's
trace holds every hop.

Also learned while checking it: `az monitor app-insights query` only looks at the LAST HOUR unless
given `--offset`, whatever the query's own time filter says - a quiet source of "no rows". And
`first`/`last` are reserved in KQL.

### 17.9 Progress and results raced: a lost update on the case

`verify_phase10` check 4: the replayed RB-0099 operation `succeeded`, the result was consumed, and
the case still read `executing / dead_lettered`. Progress (topic) and results (queue) are consumed
by separate loops on both replicas, and both did full-document read-modify-write. A progress event
that had read the case before the result landed wrote the old status back over the completed case.
Progress now patches ONLY the `remediation` field, with a Cosmos `filter_predicate` that refuses
once the remediation is final; the result writes the whole record last and unconditionally. A
straggler can no longer undo an outcome (`tests/test_remediation_flow.py`).

### 17.10 The shared approval executor leaked parked cases into every checkpoint (Phase 07 bug)

The workflow - and its `ApprovalExecutor` - is built once per shape and reused for every case on a
replica. `_pending` (parked cases by request id) only ever lost an entry when THIS replica resumed
that case; a case answered on the other replica, resumed cold, or deleted by `demo-reset` stayed
for good and was written into every later checkpoint of every other case. Found when
`verify_phase07` check 3 counted 1 checkpoint after a reset: every checkpoint carried two deleted
cases' pending approvals. The executor now drops entries whose approval window closed more than
10 minutes ago - on park, on checkpoint save and on restore. Nothing can validly answer them.

## 18. Observability findings (Phase 11, 2026-10-01)

### 18.1 The Azure Monitor distro samples by default - set `always_on` for a demo

`azure-monitor-opentelemetry` 1.8.10 (installed) defaults to `microsoft.rate_limited` at 5 traces
per second when `OTEL_TRACES_SAMPLER` is unset (`_utils/configurations.py`,
`_default_sampling_ratio`). The sampler respects its parent's decision, so it should drop whole
traces, not pieces of one - but the Phase 10 canonical trace does have spans whose parents never
arrived (the triage node's `invoke_agent`, for one). Whatever the cause, ATLAS's volume is a
demo's and Episode 4's whole beat is a complete waterfall, so `configure_telemetry` now
`setdefault`s `OTEL_TRACES_SAMPLER=always_on`. An explicit environment value still wins.
`trace_check` labels any span whose parent is missing `(parent not ingested)` instead of hiding it.

### 18.2 `receive_messages()` emits a span per call, even an empty one

`ServiceBus.receive` was the single largest span name: 20,806 in 24 h from the orchestrator alone,
almost all empty 10-second polls by the two remediation listeners on two replicas. The SDK opens the
span in `receive_messages()` whatever it returns (`_servicebus_receiver_async.py`), but in the
async iterator only when a message arrives. The listener now iterates (`async for message in
receiver`, ending after `max_wait_time` idle) - same behaviour, no empty spans. The worker already
iterated.

The next-noisiest source is real work: the tool catalog refresh (`POST /ops-mcp/mcp`, about 7,700 a
day across two replicas, each refresh an MCP initialize plus `tools/list`). Left as is - it is the
60-second description governance from Phase 09, and it is visible in APIM for the same reason.

### 18.3 ARM caps a template at 64 outputs

`main.bicep` was at exactly 64. Adding `ATLAS_WORKBOOK_ID` failed the build with `max-outputs`.
`ATLAS_RBAC_SUMMARY` was read by nothing outside `infra/` and was dropped; the summary is still an
output of the `rbac` module deployment. Alert rule and web test names are not outputs - the
verifier lists them by resource group.

### 18.4 `az monitor scheduled-query` needs an extension that prompts to install

Non-interactive, the prompt reads EOF and the command dies with a traceback. The verifier lists
`Microsoft.Insights/scheduledQueryRules` (2023-12-01) and `metricAlerts` (2018-03-01) with
`az rest` instead - no extension, no prompt.

### 18.5 Where the signals come from - the Foundry seam

App Insights roles seen on the shared component (one week):

| `cloud_RoleName` | Emitted by | What it shows |
|---|---|---|
| `atlas.atlas-web`, `atlas.atlas-orchestrator`, `atlas.atlas-worker` | ATLAS code, `atlas_common.telemetry` (client side) | Every hop, `atlas.*` attributes, Agent Framework GenAI spans (`invoke_agent`, `chat gpt-main`, `execute_tool`), business events |
| `atlas.atlas-ops-api`, `atlas.atlas-ops-mcp` | ATLAS code in the Function Apps | The enqueue span, Cosmos calls |
| `func-atlas-api-*`, `func-atlas-mcp-*` | the Functions host | Invocation requests and host logs |
| `apim-atlas-* East US 2` | APIM diagnostics | Gateway request, backend forward, token and policy timing |
| `logic-atlas-*` | the Logic Apps runtime | Trigger and action invocations, its own storage traffic |
| `atlas-incident-agent` | the hosted agent container (Agent Framework, client side) | The agent's own spans and logs |
| `responsesapi` | Foundry, server side | The platform invoking the hosted agent (`invoke_agent atlas-incident-agent:<version>`) |
| `agentsv2` | Foundry, server side | Prompt agent runs |

The honest seam for Episode 4: a prompt agent's internals are what Foundry chooses to emit
(`agentsv2`); a hosted agent shows both sides (`responsesapi` from the platform, our own spans from
inside the container); the ACA workflow is entirely ours. APIM and Logic Apps spans are
platform-emitted and cannot carry `atlas.case_id` - they join the case by `operation_Id`, which is
exactly why `traceparent` propagation is a build-time invariant.

### 18.6 Deviations from PHASE-11, recorded

| Pack | Built | Why |
|---|---|---|
| Model usage "from `llm-emit-token-metric`" | From the GenAI chat spans (`gen_ai.usage.*`, by `microsoft.gen_ai.main_agent.name` and `atlas.case_id`) | Orchestrator model traffic does not route through APIM (descoped, CLAUDE.md 7.12), so the APIM token metric only sees direct `/models` calls. The spans see every call. |
| End-to-end p95 alert at 45 s | 120 s on `workflow.run` | Measured canonical-case p95 is about 83 s, all model time; 45 s would fire permanently. `atlas.case` includes the human approval wait, so it is the wrong span to alert on. |
| "Turns/min" | Case analyses per 15 minutes | Demo volume; per-minute bins are mostly empty. |
| Tool error rate per tool | Per backend | A tool failure becomes a typed `ToolFailure` inside `_guard`, which knows the backend, not the tool. Tool spans never fail, by design. |
| Every alert on a 5-minute window | Windows per rule (5 min to 1 day) | Eval regression is evaluated on a 1-day window: evaluations are hours apart. |
| Section 6 Quality | Query ships now; data arrives with Phase 12's `eval.run` event | `verify_phase11` reports it PENDING, not passed. |
| Availability alert | Added (ninth rule) | A standard test without an alert is a chart, not a monitor. |

### 18.7 Smaller traps, all hit while building the verifier

- **`az.cmd` splits URLs at `&`.** On Windows `az` is a batch file, so `az rest --url
  "...?a=1&b=2"` hands the URL to cmd.exe, which treats `&` as a command separator. The Alerts
  Management query (`api-version`, `timeRange`, `alertRule`) failed every time. The verifier calls
  ARM with httpx and the CLI's token instead.
- **A workbook GET omits its content** unless the request has `canFetchContent=true`
  (`Microsoft.Insights/workbooks@2023-06-01`). Without it `serializedData` is absent and the
  workbook looks empty.
- **`first` and `last` are reserved in KQL** - hit twice more (17.8 already said so). A 400 from the
  App Insights query API carries the parser's reason in its body; `trace_check` now surfaces it
  instead of a bare `raise_for_status`.
- **Multi-line KQL through `az monitor app-insights query`** comes back `Bad Request` from Git Bash
  on Windows (quoting again). Post the query as a JSON body.

### 18.8 On the cold path, `atlas.case` outlives the case

The case span is opened by the replica that ran the analysis, and it stays open while that replica
holds the browser's stream waiting for a human (`APPROVAL_WAIT_S`, 300 s). If the approval lands
on the OTHER replica, that replica resumes cold and dispatches under the case's trace (17.8), while
the first replica keeps waiting until its timeout. Measured: `atlas.case` 353 s for a case whose
remediation finished at 90 s. The waterfall is still one trace with every hop; only the root span's
duration overstates the case. Alert and chart on `workflow.run`, never on `atlas.case` (18.6).

### 18.9 An alert that was already firing is not proof that it fires

The first green run of `verify_phase11` check 4b read "alert fired 31 s after injection". It had
not: the DLQ alert had been `Fired` since 11:44 - the moment the rule was created - because the DLQ
already held an orphaned verifier command from 08:18 (`rejected:no_case`) and, later, an RB-0099
command from a crashed verifier run that never replayed it. A metric alert with `autoMitigate`
keeps ONE instance open until the condition clears, so "is there a fired alert" says nothing about
the injection. Check 4b now demands an empty DLQ and a resolved alert first, then accepts only an
instance whose `startDateTime` is after the injection.

Clearing the leftovers surfaced two more things:

- `dlq_replay` read one `receive_messages()` batch and gave up; one receive can return part of
  what is there. It now drains until a receive comes back empty. And it gained `--discard`, for a
  dead letter no replay can ever fix.
- For about five minutes both dead letters were peekable but not receivable: still locked (lock
  duration `PT5M`) by an earlier receiver that did not release them. If a DLQ looks stuck right after
  a script ran, wait out the lock before assuming anything worse.

## 19. Staged scenarios and the graded gate (2026-10-01, before Phase 12)

### 19.1 Three more live incidents, layered on without moving anything

`data/generators/scenarios.py` stages three incidents near the frozen operations clock, each with a
change, a telemetry signature and a runbook: `search-flag` (RB-0029, the planted injection),
`reporting-ttl` (Tier 4 staging, RB-0011), `orders-stale` (Tier 2, RB-0015, stale since 2024-12).
They are generated AFTER the seeded stream, from their own RNG (`seed ^ SCENARIO_RNG_SALT`). The
first attempt indexed the new changes into `changes_by_service` before incidents were generated,
which shifted the RNG and rewrote 27 historical incidents that tests and evaluations cite by id.
Now the diff against the previous seed is purely additive: +3 changes, +36 telemetry windows,
the manifest's `scenarios` block.

### 19.2 The planner made auto-approval unreachable

The first live probe: every plan, including a cache-TTL revert on a staging Tier 4 service, came
back `risk: high` with destructive steps, because the planner's rubric said "high if any step is
destructive" and "destructive includes any configuration change". Gate rule 5 allowed no
destructive step at all, so nothing that changes state could ever be auto-approved, and every
scenario ended at a manager.

Decided with the presenter (2026-10-01), option "graded risk + non-prod auto":

- **Planner:** graded rubric aligned with the entitlement table. High: destructive on Tier 1/2, any
  failover/delete/irreversible step, or blast radius over 8. Medium: destructive only on Tier 3/4
  with something in production, or blast radius 3-8. Low: no destructive step, or every
  destructive step on a non-production Tier 3/4 service, reversible, blast radius 2 or less.
  A second probe showed the planner rating a textbook "low" plan "medium" because "approval is
  advisable"; the instructions now say `risk` is not a vote and that preference belongs in
  `requires_approval`.
- **Gate rule 5:** a low-risk plan may now be auto-approved WITH destructive steps if every target
  is a known non-production Tier 3/4 service and every destructive step has a real rollback (not
  "None", "n/a", empty). Rule 5 already required no production target, so the invariant is
  unchanged in substance and now stated exactly in the property test: an auto-approved plan touches
  no production service, and nothing in it is irreversible.

The gate stays the authority: the planner's grade only chooses WHO approves, and a destructive
production step always needs a human whatever the grade says.

### 19.3 Measured (deployed, 2026-10-01)

| Scenario | Gate | Notes |
|---|---|---|
| canonical | director only (break glass) | unchanged by the rubric |
| reporting-ttl | auto-approved, 3 of 3 runs | remediated 23-32 s after the gate, no human |
| search-flag | SRE first | injection in RB-0029 noticed (`embedded_instructions_found`) |
| orders-stale | manager first | reason names the stale RB-0015 |

### 19.4 The Approvals page read the wrong queue (fixed 2026-10-04)

`/approvals` was the Phase 04 console: it read `/api/approvals`, the chat path's `plan.status =
'awaiting_approval'` rows, which the v1 triage agent no longer produces - so the page was always
empty while workflow cases sat in `/api/workflow/approvals`, which no page showed. The Episode 2
kill-and-resume beat ("approve from the console while the orchestrator is down") had no console.

Now the page reads the workflow queue, and the web makes two decisions itself:

- **Orchestrator unreachable:** the queue is read straight from Cosmos (`kind: 'approval'` rows),
  and a decision is QUEUED - the row goes to `status: answered` with the answer, by a conditional
  (etag) replace that loses cleanly to a racing answer. That is exactly what the orchestrator's own
  `record_answer` writes and what its startup sweep reads. Measured live: queued answer, 7 s
  restart, sweep resumed the case from its checkpoint, remediation succeeded 57 s later.
- **Who may sign is checked early with the gate's own answer.** Entitlements alone said a manager
  could approve the canonical plan (high risk, Tier 1, radius 10 <= 15); the gate had named only
  `director` (break glass). The web now refuses roles the gate did not name - the same check the
  orchestrator makes - so the console never says "queued" for an answer the sweep would refuse.

### 19.5 Replica ranges drifted from the template (found 2026-10-04)

All three apps had been set to min 0 / max 3 in the portal (2026-10-02 20:21 UTC) to save cost,
and later to 1-3 for build days; the template still said 2-10 / 2-10 / 1-5, so the next
`azd provision` would have silently reverted the presenter's choice. Visible symptoms while at 0:
a dropped SSE stream, a case that never ran (cold start), and the worker's revision
`ScaledToZero`. The template now has a `scaleProfile` parameter (`ATLAS_SCALE_PROFILE`): `idle`
0-3, `work` 1-3 (default, matches the live state), `show` 2-10 / 2-10 / 1-5. `make scale
PROFILE=show` the day before an episode. `kill_orchestrator.sh --scale-zero` restores the range it
found instead of a hard-coded 2-10.

Not yet proven: in `idle`, waking the worker from zero depends on KEDA reading the queue depth with
the worker identity's Receiver role; the Service Bus scaler may need management rights. Verify
before relying on `idle` with remediation.
