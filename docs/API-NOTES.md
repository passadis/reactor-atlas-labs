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
