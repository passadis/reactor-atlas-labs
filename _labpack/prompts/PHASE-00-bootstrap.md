# PHASE 00 — Bootstrap

> Paste this into Claude Code in an empty git repository that contains the lab pack at `./_labpack`.

---

You are starting **Project ATLAS**. Read `_labpack/README.md`, `_labpack/CLAUDE.md`,
`_labpack/ARCHITECTURE.md` and `_labpack/DATA-MODEL.md` in full before doing anything.

## Tasks

1. Copy `_labpack/CLAUDE.md` to the repository root as `CLAUDE.md`.
2. Create the repository skeleton exactly as laid out in section 2 of `CLAUDE.md`. Empty packages
   get an `__init__.py`; empty directories get a `.gitkeep`.
3. Set up Python tooling:
   - `pyproject.toml` at the root using **uv** workspaces, Python 3.12.
   - Members: `src/atlas_common`, `src/atlas_agent`, `src/atlas_orchestrator`, `src/atlas_web`,
     `src/atlas_worker`, `src/atlas_hosted_agent`.
   - Dev deps: `ruff`, `mypy`, `pytest`, `pytest-asyncio`, `pytest-cov`, `types-*` as needed.
   - `ruff` config: line length 100, select `E,F,I,N,UP,B,ASYNC,S,RUF`, ignore `S101` in tests.
   - `mypy`: strict on `src/`, ignore missing imports for Azure SDKs that lack stubs.
4. **Pin the agent stack deliberately.** Do not guess versions:
   - Query MS Learn MCP for the current Agent Framework and `azure-ai-projects` guidance.
   - Run `uv add agent-framework azure-ai-projects azure-identity azure-cosmos azure-search-documents azure-servicebus azure-monitor-opentelemetry opentelemetry-sdk pydantic fastapi uvicorn httpx sse-starlette python-dotenv`
     and record the resolved versions.
   - Write `docs/API-NOTES.md` with a dated table: package, resolved version, which symbols you
     confirmed exist (`Agent` vs `ChatAgent`, `FoundryChatClient` vs `AzureAIAgentClient`,
     `WorkflowBuilder`, `CosmosCheckpointStorage` availability, `tool` decorator signature and
     whether it supports `approval_mode`).
   - This table is the contract for the rest of the build. Everything downstream uses these symbols.
5. Create `Makefile` with placeholder targets that will be filled in as phases land:
   `install`, `lint`, `test`, `provision`, `deploy`, `seed`, `eval`, `redteam`, `verify-all`,
   `demo-reset`, `trace-check`.
6. Create `.gitignore` covering `.azure/`, `.venv/`, `__pycache__/`, `*.env`, `local.settings.json`,
   `.python-version`, `dist/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `node_modules/`.
7. Create `scripts/preflight.sh` that verifies the local toolchain and prints a pass/fail table:
   - `az --version` ≥ 2.77, `azd version` ≥ 1.23
   - `az extension add --name containerapp --upgrade`, `--name application-insights`
   - `azd ext install azure.ai.agents` (the AI agent extension)
   - `func --version` ≥ 4.0.7030 (Azure Functions Core Tools — needed for the MCP extension)
   - `docker info` (optional; note that `azd` builds remotely in ACR by default)
   - `uv --version`, `python --version` (3.12.x)
   - `az account show` and print subscription id + tenant id
   - `az provider show -n Microsoft.CognitiveServices/Microsoft.App/Microsoft.ApiManagement/Microsoft.ServiceBus/Microsoft.DocumentDB/Microsoft.Search/Microsoft.Web/Microsoft.Logic` registration state; register anything not `Registered`.
8. Create `scripts/check_region.sh <location>` that verifies the target region supports:
   - the Foundry model SKUs you intend to deploy (`az cognitiveservices model list -l <loc>` — look
     for the chat model and `text-embedding-3-large` with `GlobalStandard`/`Standard` SKUs),
   - APIM `StandardV2`,
   - Functions Flex Consumption,
   - Container Apps workload profiles.
   Print a table and exit non-zero if anything is missing. Default `swedencentral`, fallback
   `eastus2`.
9. Commit: `chore(phase-00): bootstrap repository, toolchain, and API baseline`.

## Acceptance criteria

- [ ] `CLAUDE.md` at repo root; `_labpack/` retained and gitignored from linting.
- [ ] `uv sync` succeeds; `uv run ruff check .` and `uv run mypy src/` pass on the skeleton.
- [ ] `docs/API-NOTES.md` lists resolved versions **and** the exact symbol names you verified by
      importing them — not from memory.
- [ ] `bash scripts/preflight.sh` prints all-green (or tells you precisely what to install).
- [ ] `bash scripts/check_region.sh swedencentral` prints a capability table.

## Report back

A table of resolved package versions, the confirmed Agent Framework symbol set, the chosen region,
and anything in the region check that failed.
