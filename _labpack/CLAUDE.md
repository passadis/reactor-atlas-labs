# CLAUDE.md — Project ATLAS

You are building **Project ATLAS**, the Contoso Cloud Operations incident copilot: a single,
production-shaped agentic solution on Azure that will be demonstrated live across four Microsoft
Reactor sessions. Everything must be **deployed and working on Azure before the first session**.

Read `_labpack/ARCHITECTURE.md` and `_labpack/DATA-MODEL.md` before your first code change, and
re-read the current phase prompt in `_labpack/prompts/` before each work block.

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
   uv pip show agent-framework azure-ai-projects azure-identity azure-monitor-opentelemetry
   python -c "import agent_framework, inspect; print(agent_framework.__version__)"
   python -c "import agent_framework as m; print([n for n in dir(m) if not n.startswith('_')])"
   ```
3. If docs and the installed package disagree, **the installed package wins**. Record the
   discrepancy in `docs/API-NOTES.md` with the date, both sources, and what you chose.

**Hard rule — never use deprecated surfaces.** Anything under
`learn.microsoft.com/azure/foundry-classic/**` is the *classic* portal/API, deprecated and retiring
2027-03-31. Do not use `AIProjectClient.from_connection_string`, `project_client.agents.create_agent`
with `toolset=`, `Azure.AI.Agents.Persistent`, or the classic "assistants/threads/runs" shapes.
Use the current Foundry Agent Service (prompt agents / hosted agents / Responses API) and Agent
Framework instead. If a search result URL contains `foundry-classic`, treat it as a negative example.

**Known-current baseline as of this pack (re-verify, do not assume):**

```python
# pip install agent-framework
from agent_framework import Agent, WorkflowBuilder, tool
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential

agent = Agent(
    client=FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],   # https://<acct>.services.ai.azure.com/api/projects/<proj>
        model=os.environ["FOUNDRY_MODEL_DEPLOYMENT"],
        credential=DefaultAzureCredential(),
    ),
    name="...", instructions="...", tools=[...],
)
```

Some published samples still show `agent_framework.azure.AzureAIAgentClient` + `ChatAgent`. Check
which of the two your installed version exposes and use that one **consistently across the whole
repo**. Do not mix them.

---

## 1. Ground rules

| Rule | Detail |
|---|---|
| **No secrets in code** | Managed identity + Entra ID everywhere. No connection strings, no API keys, no account keys in source, `.env`, Bicep params, or container env vars. The only permitted secret is the APIM subscription key, stored in Key Vault and referenced by URI. |
| **No `DefaultAzureCredential` surprises** | In Azure compute, pass `managed_identity_client_id=` explicitly for user-assigned identities. Locally, `AzureCliCredential`. Wrap both in one `atlas_common.auth.get_credential()`. |
| **Everything is idempotent** | `azd provision` and every seed/config script must be safe to re-run. Use deterministic resource names derived from `azd env` + a resource token. |
| **Deterministic vs agentic is a design decision, not an accident** | Authorization, entitlement checks, change-freeze windows, blast-radius thresholds, idempotency and retries are **plain code**. Classification, summarisation, ranking, plan drafting and explanation are **agent** work. Never let the model decide whether it is allowed to act. |
| **Structured outputs everywhere** | Every agent that feeds another component returns a Pydantic model, not prose. Validate before use; on validation failure, retry once with the error, then fail to a typed error object. |
| **Correlation ID on every hop** | W3C `traceparent` is propagated web → orchestrator → APIM → Functions → Service Bus → worker. Anything that drops it is a bug. |
| **No emojis** in code, logs, commit messages, or generated docs. |
| **Python 3.12**, `uv` for dependency management, `ruff` + `mypy --strict` on `src/`, `pytest` for tests. |

---

## 2. Repository layout

```
atlas/
├── CLAUDE.md
├── Makefile                        # verify-all, seed, eval, trace-check, demo-reset
├── azure.yaml                      # azd: services + hooks
├── pyproject.toml                  # workspace root (uv)
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
│   └── atlas_hosted_agent/         # Ep1 Foundry hosted agent entrypoint + manifest
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
| `AZURE_LOCATION` | `swedencentral` | Fallback `eastus2`. **Verify** model + agent + evaluation + red-teaming availability before committing. |
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
