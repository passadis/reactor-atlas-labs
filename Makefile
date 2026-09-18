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
	$(UV) sync --python 3.13 --group dev --group evals

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
deploy: ## azd deploy all services (Phase 04)
	$(call PENDING,Phase 04 - web app)

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
