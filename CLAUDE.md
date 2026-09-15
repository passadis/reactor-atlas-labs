# CLAUDE.md — Project ATLAS

You are building **Project ATLAS**, the Contoso Cloud Operations incident copilot: a single,
production-shaped agentic solution on Azure that will be demonstrated live across four Microsoft
Reactor sessions. Everything must be **deployed and working on Azure before the first session**.

Read `_labpack/ARCHITECTURE.md` and `_labpack/DATA-MODEL.md` before your first code change, and
re-read the current phase prompt in `_labpack/prompts/` before each work block.

**Section 7 records every verified deviation from the lab pack.** Where section 7 and the lab pack
disagree, section 7 wins — it is backed by live API probes, not by assumptions.

---

## 0. Prime directive: verify APIs, never recall them

The Microsoft Foundry and Microsoft Agent Framework surfaces changed substantially through
2025–2026. Class names, module paths, and portal flows in your training data are likely stale.

**Before writing or changing any code that touches Foundry, Agent Framework, `azd ai`, APIM MCP,
Functions MCP, or the evaluation SDKs, you MUST:**

1. Query the Microsoft Learn MCP server (`microsoft_docs_search`, then `microsoft_docs_fetch` on
   the best page, and `microsoft_code_sample_search` for the language pivot). Prefer pages under
   `learn.microsoft.com/azure/foundry/...` and `learn.microsoft.com/agent-framework/...`.
2. Inspect what is actually installed:
   ```bash
   uv pip show agent-framework agent-framework-foundry azure-ai-projects azure-identity
   uv run python -c "import agent_framework as m; print(m.__version__)"
   uv run python -c "import agent_framework as m; print([n for n in dir(m) if not n.startswith('_')])"
   ```
3. If docs and the installed package disagree, **the installed package wins**. Record the
   discrepancy in `docs/API-NOTES.md` with the date, both sources, and what you chose.

**If the Microsoft Learn MCP tools are not loaded in your session**, they can still be reached over
raw JSON-RPC — see `scripts/mslearn.sh`. Do not skip step 1 because the tool is missing.

**Hard rule — never use deprecated surfaces.** Anything under
`learn.microsoft.com/azure/foundry-classic/**` is the *classic* portal/API, deprecated and retiring
2027-03-31. Do not use `AIProjectClient.from_connection_string`, `project_client.agents.create_agent`
with `toolset=`, `Azure.AI.Agents.Persistent`, or the classic "assistants/threads/runs" shapes.
Use the current Foundry Agent Service (prompt agents / hosted agents / Responses API) and Agent
Framework instead. If a search result URL contains `foundry-classic`, treat it as a negative example.

**Verified baseline — confirmed by import. See `docs/API-NOTES.md` for the dated probe output.**

Note the asymmetry, confirmed by import: the **distribution** is `agent-framework-foundry` but the
**import namespace** is `agent_framework.foundry`. The hosting package is flat:
`agent_framework_foundry_hosting`. Orchestration builders are a separate distribution entirely,
`agent-framework-orchestrations`, imported as `agent_framework_orchestrations`.

```python
from agent_framework import Agent, WorkflowBuilder, tool
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential

agent = Agent(
    client=FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["FOUNDRY_MODEL_DEPLOYMENT"],
        credential=DefaultAzureCredential(),
    ),
    name="...",
    instructions="...",
    tools=[...],
)
```

Structured output is **not** a constructor or `run()` keyword. It goes in the `options` TypedDict:

```python
result = await agent.run(query, options={"response_format": MyPydanticModel})
value = result.value  # raises pydantic.ValidationError on schema failure
```

---

## 1. Ground rules

| Rule | Detail |
|---|---|
| **No secrets in code** | Managed identity + Entra ID everywhere. No connection strings, no API keys, no account keys in source, `.env`, Bicep params, or container env vars. The only permitted secret is the APIM subscription key, stored in Key Vault and referenced by URI. |
| **No `DefaultAzureCredential` surprises** | In Azure compute, pass `managed_identity_client_id=` explicitly for user-assigned identities. Locally, `AzureCliCredential`. Wrap both in one `atlas_common.auth.get_credential()`. |
| **Everything is idempotent** | `azd provision` and every seed/config script must be safe to re-run. Use deterministic resource names derived from `azd env` + a resource token. |
| **Deterministic vs agentic is a design decision, not an accident** | Authorization, entitlement checks, change-freeze windows, blast-radius thresholds, idempotency and retries are **plain code**. Classification, summarisation, ranking, plan drafting and explanation are **agent** work. Never let the model decide whether it is allowed to act. |
| **Structured outputs everywhere** | Every agent that feeds another component returns a Pydantic model, not prose. Validate before use; on validation failure, retry once with the error, then fail to a typed error object. `response.value` **raises** `ValidationError` — catch it, do not test for `None`. |
| **Correlation ID on every hop** | W3C `traceparent` is propagated web to orchestrator to APIM to Functions to Service Bus to worker. Anything that drops it is a bug. `atlas_common.telemetry` ships in Phase 03, not Phase 11 — this is a build-time invariant, not a late add-on. |
| **Pin exact versions** | The agent stack ships breaking changes roughly weekly. Every Azure/agent dependency is pinned with `==`. Never let `uv add` re-resolve mid-build; upgrade deliberately and re-run the symbol probe. |
| **No emojis** in code, logs, commit messages, or generated docs. |
| **Python 3.13**, `uv` for dependency management, `ruff` + `mypy --strict` on `src/`, `pytest` for tests. |

---

## 2. Repository layout

```
atlas/
├── CLAUDE.md
├── Makefile                        # verify-all, seed, eval, trace-check, demo-reset
├── azure.yaml                      # azd PLATFORM project: ACA + Functions, infra.provider=bicep
├── pyproject.toml                  # workspace root (uv)
├── agents/
│   └── azure.yaml                  # azd FOUNDRY project: hosted agent, toolbox, connections
├── infra/
│   ├── main.bicep                  # subscription-scope; creates RG + modules
│   ├── main.parameters.json
│   ├── abbreviations.json
│   └── modules/
│       ├── foundry.bicep           # account, project, model deployments, AI Search conn, App Insights conn
│       ├── observability.bicep     # LAW + App Insights + workbook + alerts
│       ├── data.bicep              # Cosmos DB, AI Search, Storage
│       ├── messaging.bicep         # Service Bus namespace, queues, topic, DLQ config
│       ├── compute-aca.bicep       # ACR, ACA env, 3 container apps
│       ├── compute-functions.bicep # 2 Flex Consumption function apps
│       ├── apim.bicep              # APIM StandardV2, APIs, products, policies, backends, MCP servers
│       ├── logicapps.bicep         # Logic Apps Standard + plan + workflows deployment
│       ├── identity.bicep          # user-assigned MIs
│       └── rbac.bicep              # every role assignment in RBAC-MATRIX.md
├── src/
│   ├── atlas_common/               # shared: auth, models, telemetry, config, cosmos, search
│   ├── atlas_agent/                # Ep1 core agent: instructions, tools, structured outputs
│   ├── atlas_orchestrator/         # Ep2 workflow: executors, agents, policy engine, checkpoints
│   ├── atlas_web/                  # Ep1/Ep2 FastAPI + SSE UI + approval UI
│   ├── atlas_worker/               # Ep3 Service Bus consumer (remediation executor)
│   └── atlas_hosted_agent/         # Ep1 Foundry hosted agent entrypoint (serves port 8088)
├── functions/
│   ├── ops_api/                    # HTTP business API + OpenAPI spec  (func-atlas-api)
│   └── ops_mcp/                    # MCP tool trigger server            (func-atlas-mcp)
├── logicapps/
│   ├── approve-remediation/        # workflow.json
│   └── enrich-incident/            # workflow.json (exposed as a Foundry agent tool)
├── apim/
│   ├── policies/*.xml
│   └── openapi/atlas-ops-api.yaml
├── data/
│   ├── generators/                 # deterministic synthetic data generators (seeded RNG)
│   └── seed/                       # generated JSON/JSONL, committed
├── evals/
│   ├── datasets/atlas-eval-v1.jsonl
│   ├── run_eval.py
│   ├── run_redteam.py
│   └── thresholds.yaml
├── scripts/                        # verification, demo reset, failure injection
├── tests/                          # unit + integration + smoke
├── docs/
│   ├── API-NOTES.md                # doc/package discrepancies you found
│   └── DEMO-NOTES.md
└── .github/workflows/
    ├── ci.yml
    └── agent-quality-gate.yml
```

---

## 3. Naming and environment

Derive everything from `azd env` values. Defaults:

| Variable | Default | Notes |
|---|---|---|
| `AZURE_ENV_NAME` | `atlas` | |
| `AZURE_LOCATION` | **`eastus2`** | Fallback `swedencentral`. See section 7.1 — Sweden Central cannot run the AI Red Teaming Agent. |
| `RESOURCE_GROUP` | `rg-atlas-<env>` | |
| resource token | `uniqueString(subscription().id, env, location)` | 8 chars, lowercase |

Resource names: `<abbrev>-atlas-<token>` (e.g. `ca-atlas-web-a1b2c3d4`, `func-atlas-mcp-a1b2c3d4`,
`apim-atlas-a1b2c3d4`). Model deployment names are **stable logical aliases**, never raw model
names: `gpt-main`, `gpt-judge`, `embed-main`. Code and policies reference the aliases so you can
swap the underlying model without touching code.

---

## 4. Definition of done for every phase

A phase is complete only when **all** of these hold:

1. `uv run ruff check . && uv run mypy src/` passes.
2. `uv run pytest -q` passes, including the new tests for this phase.
3. The phase's own acceptance criteria (listed in its prompt) are demonstrably met against
   **deployed Azure resources**, not local stubs.
4. `scripts/verify_<phase>.sh` exists, is idempotent, exits non-zero on failure, and is wired into
   `make verify-all`.
5. `docs/DEMO-NOTES.md` has a short entry: what was built, what to show, exact commands, and the
   fallback if it fails live.
6. A conventional commit is made (`feat(phase-06): ...`). Never commit secrets; `.gitignore` covers
   `.azure/`, `*.env`, `local.settings.json`.

If you cannot meet a criterion, **stop and report** rather than marking the phase done. Say exactly
what is blocked and what you need.

---

## 5. Working style

- **Plan before you build.** For each phase, produce a short plan (files to create/change, Azure
  calls, risks), then execute. Keep a running TODO.
- **Small, verifiable steps.** Deploy and probe after each meaningful unit rather than at the end.
- **Prefer `az` CLI + Bicep over portal clicks** so the build is reproducible. Where a feature is
  portal-only, script it with `az rest` against the management API and note it in `docs/API-NOTES.md`.
- **Never fake success.** If a deployment fails, surface the error and the remediation. If a
  capability is unavailable in the chosen region, say so and propose the alternative region or
  approach — do not silently substitute a mock.
- **Retry sanely.** Azure control-plane calls get exponential backoff with a cap; do not loop
  forever on a 403 (that is a role assignment problem, and role assignments take up to 5 minutes to
  propagate — wait, then re-check, then escalate).
- **Long-running provisioning** (APIM, Managed Redis) runs in the background; keep working on code
  that does not depend on it.
- **Workarounds are acceptable when they reach the promised result.** Document the compromise in
  `docs/API-NOTES.md` and say it out loud on stage rather than hiding it.

---

## 6. Demo safety rules

These exist because this runs live in front of an audience.

- Every agent has a **content filter / guardrail** attached and a refusal path, and you must test it.
- Every external call has an explicit timeout and a user-visible degraded mode. A dead backend must
  produce "I could not reach the ticketing system" — never a stack trace on screen, never a hang.
- Seeded data is **deterministic** (fixed RNG seed), so the same demo prompt gives the same shape of
  answer every time.
- `make demo-reset` returns Cosmos, Service Bus queues, approval state and agent versions to a known
  good baseline in under 60 seconds.
- The regression kit in `scripts/inject_*.py` must be **reversible with one command**
  (`scripts/restore_baseline.py`), and CI must prove the restore works.

---

## 7. Verified deviations from the lab pack

Established by live probes on **2026-09-15**. Each entry names the evidence. Re-verify before
Episode 1; do not silently revert any of these.

### 7.1 Primary region is `eastus2`, not `swedencentral`

The AI Red Teaming Agent is supported in **East US 2 and North Central US only**
([evaluation-regions-limits-virtual-network](https://learn.microsoft.com/azure/foundry/concepts/evaluation-regions-limits-virtual-network)).
Sweden Central would silently delete Phase 12 section 4 and the Phase 13 gate's red-team step.
The Protected-material evaluator is also East US 2 only.

`eastus2` is a strict superset for this build: identical model catalogue (`gpt-5.4`, `gpt-5.4-mini`,
`text-embedding-3-large` all present), identical quota limits, APIM StandardV2, Functions Flex
Consumption, Service Bus Premium, ACA workload profiles, AI Search S1, Managed Redis, Agents plus
Responses API plus all 15 agent tools. No capability lost, one Episode-4 segment gained.

### 7.2 Python 3.13 everywhere, not 3.12

Hosted-agent code deployment accepts `codeConfiguration.runtime` of `python_3_13` or `python_3_14`
only — **3.12 is not offered** (`azd ai agent init --runtime` help, azure.ai.agents 1.0.0-beta.15;
[azure-yaml-reference](https://learn.microsoft.com/azure/foundry/agents/concepts/azure-yaml-reference)).
Functions Flex Consumption in eastus2 supports Python 3.10 through 3.14
(`az functionapp list-flexconsumption-runtimes`). 3.13 is the only version that satisfies the hosted
agent, Functions, and Agent Framework (`>=3.10`) at once. One version across the repo beats a split.

### 7.3 Two `azd` projects, not one `azure.yaml`

The hosted-agent project model sets `infra: provider: microsoft.foundry` and uses `host:
azure.ai.project | azure.ai.agent | azure.ai.connection | azure.ai.toolbox | azure.ai.skill |
azure.ai.routine`. The ATLAS platform needs `provider: bicep` with `host: containerapp | function`.
`infra.provider` is a single value per file, so one file cannot serve both.

**Resolution:**

| Project | File | Provider | Owns | Driven by |
|---|---|---|---|---|
| Platform | `./azure.yaml` | `bicep` | RG, Foundry **account + project + the 3 model deployments**, Cosmos, Search, Service Bus, ACA x3, Functions x2, APIM, Logic Apps, Key Vault, RBAC | `azd provision`, `azd deploy` |
| Agents | `./agents/azure.yaml` | `microsoft.foundry` | hosted agent, toolbox, connections, routines | `azd ai agent ... -C agents`, `azd deploy -C agents` |

The seam is documented and supported: an `azure.ai.project` service can target an **existing**
project by setting `endpoint:` and declaring no `deployments:`
([author-azure-yaml](https://learn.microsoft.com/azure/foundry/agents/how-to/author-azure-yaml)).
So Bicep provisions the project and the models; `agents/azure.yaml` only references them. No
duplication, no drift, no provider conflict. `scripts/postprovision.sh` writes the project endpoint
into `agents/.azure/<env>/.env` so `${...}` substitution resolves.

### 7.4 Hosted-agent runtime facts

- The agent process listens on **port 8088** and must serve a health probe, whatever the protocol.
- Protocol is **`responses`** (`POST /responses`), version `2.0.0`.
- `FOUNDRY_PROJECT_ENDPOINT` is **injected by the platform** — never declare it in `env:`.
  Platform-injected variables use the reserved `FOUNDRY_` prefix.
- `container.resources`: `cpu` from `"0.25"` to `"4.0"`, `memory` from `0.5Gi` to `8.0Gi`.
- Deploy mode `code` (ZIP) needs no Docker; `container` mode does. Prefer `code`.
- Deploying a hosted agent needs **Foundry Project Manager** at project scope.

### 7.5 Toolbox attach is server-side only

"Attach the Toolbox to the Prompt or Hosted Agent definition in Foundry. `FoundryAgent` uses that
stored tool configuration; **passing a Toolbox client-side doesn't add it to the managed agent**"
([foundry-toolbox](https://learn.microsoft.com/agent-framework/integrations/by-component/tools/foundry-toolbox)).
Phase 05 section 6 and Phase 09 section 3 must update the server-side agent definition — a
client-side attach is a no-op for the prompt and hosted agents. `FoundryToolbox` ships in the beta
`agent-framework-foundry-hosting` package and may change before stable.

### 7.6 Target-mode evaluation does not work against the orchestrator

"Target-based evaluation invokes your hosted agent directly. It works with agents that use the
responses or invocations protocol with **synchronous, non-streaming** execution. To evaluate agents
that use the A2A or Activity protocol, or other execution patterns such as **long-running or
streaming**, evaluate the traces your agent emits instead"
([quickstart-evaluate-hosted-agent](https://learn.microsoft.com/azure/foundry/observability/quickstarts/quickstart-evaluate-hosted-agent)).

The ACA orchestrator is SSE-streaming, long-running and suspends for approval. Phase 12 `--mode
target` and Phase 13 gate step 4 apply to the **hosted agent** only; the orchestrator is evaluated
by **trace evaluation**. Do not build a target harness against it.

### 7.7 AI Red Teaming is single-turn, text-only

"AI Red Teaming Agent only supports single-turn interactions in text-only scenarios"
([run-scans-ai-red-teaming-agent](https://learn.microsoft.com/azure/foundry/how-to/develop/run-scans-ai-red-teaming-agent)).
Phase 12 section 4's `Multi turn` and `Crescendo` strategies are not available. Use baseline,
`Jailbreak`, `IndirectJailbreak` (XPIA), `Base64`, `UnicodeConfusable`.

### 7.8 APIM MCP constraints

StandardV2 supports MCP servers and all `llm-*` policies (confirmed: `llm-token-limit`,
`llm-emit-token-metric`, `llm-semantic-cache-lookup`/`-store`, `llm-content-safety` are all "Yes"
for V2 tiers; **not** available on Consumption). But APIM supports **MCP tools only — no MCP
resources, no prompts** — and MCP server capabilities are **not supported in workspaces**
([mcp-server-overview](https://learn.microsoft.com/azure/api-management/mcp-server-overview)).
MCP servers are `Microsoft.ApiManagement/service/apis` with `properties.type: "mcp"` and
`mcpProperties.transportType: "streamable"`; ARM/Bicep templates are documented.

The pack's claim that global Frontend Response payload logging must be 0 bytes for MCP streaming is
**unverified** — test streaming with logging enabled before treating it as a constraint.

### 7.9 Storage and Cosmos local-auth caveats

- Logic Apps Standard historically requires shared-key access to its host storage. Do not apply
  `allowSharedKeyAccess: false` to the Logic Apps storage account without proving it first; plan for
  a second storage account if needed.
- `CosmosCheckpointStorage` auto-creates its database and container on first use. Container creation
  is a **control-plane** operation and will 403 under a data-plane-only role. Pre-create
  `workflow-checkpoints` (partition key `/workflow_name`) in Bicep and confirm the store tolerates an
  existing container.

### 7.10 Quota is not a blocker

`az cognitiveservices usage list` returns values in **`sku.capacity` units (thousands of TPM)**, not
raw TPM — calibrated against six live deployments. In eastus2: `gpt-5.4` GlobalStandard limit 10,000
units (need 200), `gpt-5.4-mini` GlobalStandard limit 10,000 (need 100),
`text-embedding-3-large` **Standard** limit 350 (need 120). No quota request required. The embedding
Standard limit of 350 units is the tightest; switch `embed-main` to GlobalStandard (limit 10,000) if
more headroom is ever needed.

### 7.11 AI Search vector dimensions

`text-embedding-3-large` produces **3072-dimension** vectors. The pack never states this. Set it
explicitly in the `idx-atlas-knowledge` index definition or you rebuild the index after seeding.

### 7.14 Azure AI Search runs in `eastus`, not `eastus2`

Discovered during the first `azd provision` on 2026-09-15. `eastus2` returned
`InsufficientResourcesAvailable` for Azure AI Search. Probed directly afterwards: **every SKU
fails**, `basic` as well as `standard`, so this is regional capacity exhaustion, not a tier or
quota problem. Probe results that day:

| Region | Search `standard` |
|---|---|
| `eastus2` | **out of capacity (all SKUs)** |
| `eastus` | available |
| `centralus` | available |
| `swedencentral` | available |
| `westus3` | `ResourcesForSkuUnavailable` |

**Resolution: `searchLocation` defaults to `eastus`; everything else stays in `eastus2`.**

Moving the whole footprint was the wrong trade. The only other region that can run the AI Red
Teaming Agent is `northcentralus`, and its agent-tool matrix is weaker - no Computer Use and no
Function tool. Keeping `eastus2` preserves red teaming and all 15 agent tools; only the search
index moves.

The cross-region hop is cheap and safe: the Foundry project connection is an endpoint plus Entra
auth and works across regions, `eastus` and `eastus2` are the same geography, and the added latency
on knowledge retrieval is tens of milliseconds against a 60-document index. Say it out loud on
stage rather than hiding it - regional capacity is a real production constraint and handling it
without moving the whole system is a better story than pretending it did not happen.

Retry `eastus2` before Episode 1; if capacity returns, `azd env set ATLAS_SEARCH_LOCATION eastus2`
and re-provision. Do not treat that as required.

**Lesson for `scripts/check_region.sh`:** it reported AI Search as available in `eastus2` and was
wrong. Provider-level region listings describe where a service is *offered*, not whether the region
has capacity *right now*. Availability is not capacity. The script now says so; only a real create
attempt proves capacity.

### 7.12 Descoped for time (agreed 2026-09-15)

Cut deliberately, not forgotten. Each keeps its Episode beat via a cheaper path:

| Cut | Kept instead |
|---|---|
| `atlas-stg` second environment (Phase 13.1) | Gate runs against the demo env with a run tag and a recorded green baseline. The gate still blocks the bad build. |
| ACA canary traffic splitting (Phase 13.3) | `rollback.yml` only — that is what runs on stage. |
| Routing orchestrator model traffic via APIM `atlas-models` (Phase 09.3) | The API and `llm-*` policies still exist and are demoed against a direct call. |
| Logic Apps native agent-tool wizard (Phase 10.A2) | APIM path only — the portable one. |
| Cloud red-team run (Phase 12.4) | Local scan only; same scorecard. |
| Semantic caching / Managed Redis | `enableSemanticCache=false`, as the pack already defaults. |
| **Second machine / co-presenter** (PREREQUISITES pre-Episode-1 checklist) | Solo presenter, system already deployed, deployment never demoed. Replaced by: fresh `az login` + `azd auth login` in the T-10 routine, a clean `git status` before going live, proven `scripts/restore_baseline.py`, complete fallback assets, and the hotspot. A second laptop does not survive a dead venue network; the fallback assets do. |

**Reinstated 2026-09-15 (Episode 1 is 20 days out, runway is comfortable):**

- **Handoff orchestration (Phase 06.3) is back in scope.** It was costed assuming a hand-rolled
  implementation; `agent_framework_orchestrations` ships `HandoffBuilder` as a first-class builder.
  All three orchestration patterns run live in Episode 2.

**Confirmed still cut, despite the runway:** `atlas-stg`. The evaluate-only gate in section 9.2
removed its purpose - the quality gate no longer deploys, and the deliberately-bad agent version
lives as a pinned inactive version on the demo project. A second environment would double cost and
double the surface that can drift between episodes, for no demo benefit.

**Surplus time goes to Phase 14**, not to new scope: the full four-episode dry run, measured
wall-clock for every control-plane chaos step (section 9.6), and complete fallback assets.

### 7.13 Sequencing changes

1. Region decided before Phase 01 (done).
2. `azure.yaml` structure spiked before any Bicep (done — section 7.3).
3. The three app registrations (`atlas-web`, `atlas-gateway`, `atlas-ops-api`) are created in
   **Phase 01**, not Phase 09. Phase 08 platform auth and Phase 09 `validate-jwt` both depend on
   them, and audience/app-role mistakes surface as 401s that look like policy bugs.
4. `atlas_common.telemetry` and `traceparent` propagation ship in **Phase 03**. Phase 11 builds the
   workbook, alerts and trace tooling on top of an already-instrumented system.
5. Phase 02 is budgeted at 90 minutes, not 30. Everything downstream blocks on the seed data.
6. Phases 06 through 13 are budgeted at roughly double the pack's estimate.

---

## 8. Presentation model (decided 2026-09-15)

This shapes Phases 04, 06, 10 and 14. Read it before designing any UI or demo entry point.

### The format

Four sessions, delivered **sequentially** — Episode 1, then 2, then 3, then 4, spread over the
series. Each session is roughly **15 minutes of narrative** (the case, the concepts, the why),
then **demo mode** for the rest of the slot.

In demo mode the presenter shows **only the parts that belong to that episode** — but must be able
to take the demo **all the way to end-to-end at any moment, in any episode, including Episode 1**,
if the room asks for it or there is time left.

### Two hard consequences

**1. The entire system is live from Episode 1.** There is no "Episode 3 plumbing gets finished in
the gap between sessions." Episode 1's encore path runs through APIM, Functions, Service Bus,
Logic Apps and the worker. Everything in Phases 01-14 is deployed, verified and frozen before
Episode 1 airs. This is the pack's rule already; the encore requirement makes it non-negotiable.

**2. Scope is a VIEW filter, never a capability switch.** This is the load-bearing design decision.

`ATLAS_DEMO_SCOPE = ep1 | ep2 | ep3 | ep4 | full` controls **what the UI reveals**, not what the
backend can do. The backend is always fully capable. Switching scope must be:

- **hot** — a dropdown in the web app and a `?scope=` URL parameter, applied per request;
- **instant** — no restart, no redeploy, no env-var change, no container revision;
- **non-destructive** — it never changes system behaviour, only which panels, nodes and events are
  surfaced.

If scope ever becomes a functional switch, the encore becomes a restart, and a restart in front of
an audience is a failure mode. Any design that requires `azd deploy` to change scope is wrong.

Do not confuse `ATLAS_DEMO_SCOPE` with `ATLAS_ORCHESTRATION_MODE` (Phase 06, which genuinely does
change the graph shape) or with the four-hosting-shapes mode switch (Phase 04/05). Three different
controls, three different jobs.

### What each scope reveals

| Scope | Reveals | Hides (still running underneath) |
|---|---|---|
| `ep1` | Chat console, streaming tokens, tool-call activity panel, structured output, citations, the four hosting shapes | Workflow graph, approval console, remediation timeline, traces, scorecards |
| `ep2` | Everything in `ep1` plus the live workflow graph, typed state, decision log, checkpoints, approval gate | Remediation execution detail, gateway internals, evaluation |
| `ep3` | Everything in `ep2` plus APIM hop annotations, MCP tool provenance, Service Bus progress, worker step timeline, ticket creation | Evaluation and quality-gate surfaces |
| `ep4` | Everything | Nothing |
| `full` | Alias for `ep4`. The encore. | Nothing |

### Requirements this places on later phases

- **Phase 04 (web app):** the scope selector, `?scope=`, and per-scope panel visibility. Panels are
  hidden with `hidden`, not unmounted, so switching mid-run does not lose state.
- **Phase 06:** `GET /cases/{id}/graph` and the SSE event stream always emit the full event set;
  the client filters. Never filter server-side, or the encore loses history for events that already
  happened.
- **Phase 10:** the remediation path must be demoable cold, from Episode 1, without Episode 2's
  approval demo having been run first.
- **Phase 14:** `scripts/verify_demo_paths.sh` asserts **five** paths — the four scoped episode
  demos and the full end-to-end encore — each starting from a cold `make demo-reset`. No episode
  demo may depend on another episode's demo having been run.
- **Runbooks:** every episode gets an **Encore** section with the one command that takes it to
  end-to-end, its expected wall-clock time, and the point of no return after which there is not
  enough slot left to start it.

### Timing budget per episode

| Segment | Minutes |
|---|---|
| Narrative and case | 15 |
| Scoped demo | 25-30 |
| Encore (optional, end-to-end) | 3-5 |
| Q&A and buffer | 10-15 |

The encore must be measured during Phase 14 rehearsal and its real wall-clock time recorded in
`docs/DEMO-NOTES.md`. If it does not fit in five minutes, it is not an encore — split it or cut it.

---

## 9. Live-session mutation policy (decided 2026-09-15)

**No builds, no deployments, no infrastructure changes during a broadcast.** Sessions are
demonstrations of capability plus read-only inspection of code and already-deployed resources.

"Read-only" means no `azd deploy`, no `docker build`, no `az ... create/update` on infrastructure,
no ACA revision change, no Bicep. It does **not** mean the application is read-only: raising an
incident, approving a plan, dispatching remediation and writing a ticket are the demo. Application
data writes are fine. Control-plane writes are not.

`make demo-reset` is a data write and belongs in the **T-10 routine only** — never mid-session.

### Consequences the build must honour

**9.1 Agent instructions live outside the container image.** Phase 03 says instructions are "loaded
at runtime". That is not enough: if they are read from a file baked into the image, switching v0 to
v1 is a redeploy. The v0/v1 switch is used in **Episode 1 Beat 2** and **Episode 4 Beat 4**, so it
must be a data read. Store instruction versions in Cosmos (or blob) and select the active version
per request via a header or session setting. `inject_weak_instructions.py` then becomes a document
write, not a deployment. Files under `src/atlas_agent/instructions/` remain the source of truth in
git and are seeded to Cosmos by `scripts/seed.py`.

**9.2 The Phase 13 quality gate needs an evaluate-only mode.** Episode 4's centrepiece is "the gate
blocks the bad build". A gate that builds and deploys is a build and a deploy. Requirements:
- The deliberately-bad artifact is **pre-created and already deployed** as a pinned, non-active
  agent version before Episode 1. Breaking the agent on stage is
  `scripts/pin_agent_version.sh <bad-version>` — a pointer flip, not a build.
- `agent-quality-gate.yml` gains `--evaluate-only`, which skips build and deploy and evaluates an
  already-deployed version.
- On stage you show a **run that already happened**: the red X, the scorecard artifact, the
  per-category diff. Optionally trigger a live re-run and let it churn in a background tab while you
  talk. Never wait on it. Never let it deploy.

**9.3 `chaos/ops_api_down.sh` uses the chaos flag, never stops the Function App.** The pack offers
both; the flag (application-level 503) is the only permitted one. Stopping a Function App is a
control-plane write with a cold-start tail on the way back.

**9.4 `chaos/revoke_servicebus_role.sh` is pre-staged or recorded.** Role-assignment propagation is
up to 5 minutes and not under our control, so a live revert can hang past the beat. Either
demonstrate it against a secondary identity that is not on the demo path, or use the recorded
fallback. Do not revoke a role the live demo depends on.

**9.5 `chaos-restore.yml` retargets.** The pack runs it nightly against `atlas-stg`, which is cut
(section 7.12). Retarget to the demo environment, at 03:00, with a **blackout window** that
refuses to run within 6 hours either side of a scheduled episode. It must still end every run with
`restore_baseline.py` plus `verify_smoke.sh` green.

**9.6 Time every control-plane chaos step in Phase 14 rehearsal** and record the measured
wall-clock in `docs/DEMO-NOTES.md`: `kill_orchestrator.sh` (ACA scale 0 then 2),
`chaos/model_throttle.sh` (APIM policy propagation), and the tool-list cache refresh after
`inject_bad_tool_description.py` (the pack says up to 60 s). These are the beats most likely to
overrun their slot.
