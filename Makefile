# Project ATLAS
#
# Targets are filled in as phases land. A target that is not implemented yet exits non-zero
# with the phase that owns it, so nothing silently "passes" before it exists.
#
# On Windows, run these from Git Bash. SHELL is forced to bash so recipes are POSIX.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV      ?= uv
PY      ?= $(UV) run python
LOCATION ?= eastus2
ENV_NAME ?= atlas

# A phase-gated placeholder: fails loudly instead of pretending to succeed.
define PENDING
	@echo "Target '$@' is delivered by $(1). Not implemented yet."; exit 2
endef

.PHONY: help
help: ## Show this help
	@echo "Project ATLAS"; echo
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- Phase 00 --
.PHONY: install
install: ## Sync the uv workspace (all groups)
	$(UV) sync --python 3.13 --group dev --group evals --group ui

.PHONY: lint
lint: ## ruff check + mypy strict on src/
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy src/

.PHONY: format
format: ## Apply ruff formatting and safe fixes
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

.PHONY: test
test: ## Run the unit test suite
	$(UV) run pytest -q

.PHONY: preflight
preflight: ## Verify the local toolchain
	bash scripts/preflight.sh

.PHONY: check-region
check-region: ## Verify a region can host ATLAS (LOCATION=eastus2)
	bash scripts/check_region.sh $(LOCATION)

.PHONY: probe
probe: ## Re-verify the agent-stack symbol surface by import
	$(PY) scripts/probe_symbols.py --check

# ---------------------------------------------------------------- Phase 01 --
.PHONY: provision
provision: ## azd provision the Azure footprint
	azd provision --no-prompt

.PHONY: verify-phase01
verify-phase01: ## Verify the deployed Phase 01 footprint
	bash scripts/verify_phase01.sh

.PHONY: app-registrations
app-registrations: ## Create or update the three Entra app registrations
	bash scripts/create_app_registrations.sh

.PHONY: deploy
deploy: ## azd deploy the services built so far (web; more join in Phases 06-10)
	azd deploy web --no-prompt

# ---------------------------------------------------------------- Phase 02 --
.PHONY: seed
seed: ## Generate and load the deterministic synthetic dataset into Cosmos and AI Search
	$(PY) -m data.generators.generate_all --seed 20260914 --out data/seed
	$(PY) -m scripts.seed

.PHONY: verify-phase02
verify-phase02: ## Verify seeded Cosmos + AI Search against the deployed environment
	bash scripts/verify_phase02.sh

.PHONY: freeze-fixtures
freeze-fixtures: ## Re-freeze golden fixtures (deliberate act; review the diff)
	$(PY) -m scripts.freeze_fixtures

# ---------------------------------------------------------------- Phase 03 --
.PHONY: demo-agent
demo-agent: ## Episode 1: the incident agent locally, streaming, then a follow-up (session in Cosmos)
	$(PY) -m scripts.demo_agent_local

.PHONY: demo-raw
demo-raw: ## Episode 1: the same report to the bare model, no tools, no data
	$(PY) -m scripts.demo_raw_model

.PHONY: prompt-agent
prompt-agent: ## Create or update the Foundry prompt agent atlas-triage-prompt (idempotent)
	$(PY) -m scripts.create_prompt_agent

.PHONY: verify-phase03
verify-phase03: ## Verify the incident agent, prompt agent, guardrails and tracing against Azure
	bash scripts/verify_phase03.sh

# ---------------------------------------------------------------- Phase 04 --
.PHONY: web-local
web-local: ## Run the web app locally with the auth bypass (refused in Azure)
	ATLAS_DEMO_AUTH_BYPASS=true $(UV) run uvicorn atlas_web.app:app --port 8000 --reload

.PHONY: sse-probe
sse-probe: ## Drive the deployed web app over SSE with a real Entra token; print timings
	$(PY) -m scripts.sse_probe --follow-up "Is the service that caused this under a change freeze, and which team owns it?"

.PHONY: screenshots
screenshots: ## Capture fallback-deck screenshots of the deployed app into docs/assets
	$(PY) -m scripts.capture_screenshots

.PHONY: verify-phase04
verify-phase04: ## Verify the deployed web app: auth, health, SSE, cancellation, tracing
	bash scripts/verify_phase04.sh

# ---------------------------------------------------------------- Phase 05 --
.PHONY: hosted-deploy
hosted-deploy: ## Stage the hosted agent (atlas_agent + adapter) and deploy a new version to Foundry
	$(PY) -m scripts.package_hosted_agent
	azd deploy -C agents atlas-incident-agent --no-prompt
	$(PY) -m scripts.hosted_agent check-guardrail

.PHONY: hosted-status
hosted-status: ## Hosted agent versions, pinned version, guardrail and identity
	$(PY) -m scripts.hosted_agent status

.PHONY: hosted-pin
hosted-pin: ## Pin the hosted agent to VERSION (pointer flip, no deploy): make hosted-pin VERSION=4
	bash scripts/pin_agent_version.sh $(VERSION)

.PHONY: hosted-rollback
hosted-rollback: ## Roll the hosted agent back to the previous good version
	bash scripts/rollback_agent.sh

.PHONY: toolbox
toolbox: ## Create or update the atlas-tools toolbox and re-attach it to the prompt agent
	$(PY) -m scripts.upsert_toolbox
	$(PY) -m scripts.create_prompt_agent

.PHONY: verify-phase05
verify-phase05: ## Verify hosted agent, guardrail, identity, web equivalence, traces, rollback
	bash scripts/verify_phase05.sh

# ---------------------------------------------------------------- Phase 06 --
.PHONY: orchestrator-local
orchestrator-local: ## Run the orchestrator locally against deployed Azure resources
	$(PY) -m uvicorn atlas_orchestrator.api:app --reload --host 127.0.0.1 --port 8001

.PHONY: graph
graph: ## Print the live workflow graph as Mermaid (MODE=graph|sequential_only|handoff)
	$(PY) -m scripts.print_graph --mode $(or $(MODE),graph)

.PHONY: verify-phase06
verify-phase06: ## Verify the deployed workflow: typed state, concurrency, all three modes, Mermaid
	bash scripts/verify_phase06.sh

# ---------------------------------------------------------------- Phase 07 --
.PHONY: checkpoints
checkpoints: ## List workflow checkpoints (CASE=CASE-XXXX narrows to one case)
	$(PY) -m scripts.inspect_checkpoints $(if $(CASE),--case $(CASE),)

.PHONY: resume
resume: ## Resume a parked case from its checkpoint (CASE=... [APPROVE=1])
	$(PY) -m scripts.resume_case $(CASE) $(if $(APPROVE),--approve,)

.PHONY: kill-orchestrator
kill-orchestrator: ## Episode 2 set piece: scale the orchestrator to 0, then back to 2
	bash scripts/kill_orchestrator.sh

.PHONY: verify-phase07
verify-phase07: ## Verify checkpoints, the policy gate, approval and kill/resume
	bash scripts/verify_phase07.sh

.PHONY: screenshots-ep2
screenshots-ep2: ## Capture the Episode 2 fallback screenshots (graph and approval gate)
	$(UV) run --group ui python -m scripts.capture_phase06
	$(UV) run --group ui python -m scripts.capture_phase07

.PHONY: verify-phase07-fast
verify-phase07-fast: ## Same, skipping the several-minute orchestrator restart
	ATLAS_SKIP_KILL=1 bash scripts/verify_phase07.sh

# ---------------------------------------------------------------- Phase 12 --
.PHONY: eval
eval: ## Run the evaluation suite and write a scorecard (Phase 12)
	$(call PENDING,Phase 12 - evaluation)

.PHONY: redteam
redteam: ## Run the AI Red Teaming scan (Phase 12; eastus2/northcentralus only)
	$(call PENDING,Phase 12 - evaluation)

# ---------------------------------------------------------------- Phase 11 --
.PHONY: trace-check
trace-check: ## Assert one trace spans all six hops for CASE=<case_id> (Phase 11)
	$(call PENDING,Phase 11 - observability)

# ---------------------------------------------------------------- Phase 14 --
.PHONY: verify-all
verify-all: ## Run every scripts/verify_phase*.sh plus smoke and security (Phase 14)
	@set -e; for s in scripts/verify_phase*.sh; do 	  echo "=== $$s ==="; bash "$$s"; 	done
	@echo "All phase verifications passed."

.PHONY: demo-reset
demo-reset: ## Return runtime state to the known-good baseline in under 60s (Phase 14)
	$(call PENDING,Phase 14 - hardening and verification)
