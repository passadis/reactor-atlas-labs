# PHASE 13 — CI/CD quality gate, regression kit, rollout and rollback (Episode 4)

> Depends on: Phase 12.
> Episode 4 topics: CI/CD quality gates, regression testing, versioning, rollout/rollback,
> governance.

---

This phase produces the live centrepiece of Episode 4: **break the agent on purpose, watch the
traces and the evaluation find it, fix it, and watch the gate let it through.**

## 1. Environments

Two `azd` environments in the same subscription:

| Env | Purpose | Notes |
|---|---|---|
| `atlas-stg` | Quality gate target | Same shape, smaller SKUs where it does not change behaviour. Separate Foundry project, separate APIM *product* (can share the APIM instance via a second product + API suffix if a second APIM is too slow to provision — document the compromise). |
| `atlas` | Demo/production | The one you present from. |

Promotion is by **image digest and agent version**, never by rebuilding.

## 2. GitHub Actions

### `.github/workflows/ci.yml` (every PR)
1. `ruff`, `mypy`, `pytest` (unit + contract).
2. Determinism test for data generation.
3. OpenAPI spec validation + route-parity test.
4. APIM policy XML lint (schema validate against the policy XSD).
5. Bicep build + `what-if` against staging.
6. `evals/local_checks.py` against recorded fixtures (no Azure calls).

### `.github/workflows/agent-quality-gate.yml` (main, and manual dispatch)
```
build → deploy-staging → smoke → evaluate → gate → promote | fail
```
1. **build** — build and push images to ACR, record digests; package the hosted agent.
2. **deploy-staging** — `azd deploy` into `atlas-stg`; deploy a new hosted agent version; record it.
3. **smoke** — `scripts/verify_smoke.sh`: health endpoints, one canonical case end to end,
   `prove_governance.sh` subset.
4. **evaluate** — `evals/run_eval.py --mode target --env stg --dataset holdout` plus
   `evals/run_redteam.py --quick`.
5. **gate** — compare against `evals/thresholds.yaml` **and** against the last green baseline
   (`max_composite_drop`). On failure: upload the scorecard as an artifact, comment on the PR/commit
   with the per-category diff, and **stop**.
6. **promote** — on pass, deploy the same digests and hosted-agent version to `atlas`, tag the
   release `agent-vN`, write a release note containing the scorecard, and pin the agent version.

Authenticate with OIDC federated credentials (no client secrets in GitHub).

### `.github/workflows/rollback.yml` (manual)
Takes a target release tag; re-points ACA revisions and the hosted agent version, runs smoke, and
reports. Must complete in under 3 minutes — you will run this on stage.

## 3. Rollout strategy

- **ACA**: revision-scoped ingress with traffic splitting. Promote 10 % → 50 % → 100 % with a
  30-second soak and an automatic abort if the error-rate alert fires. Scripts:
  `scripts/canary.sh <revision> <percent>` and `scripts/canary_abort.sh`.
- **Hosted agent**: each `azd deploy` is a new version; the demo pins a specific version.
  `scripts/pin_agent_version.sh` / `scripts/rollback_agent.sh` from Phase 05.
- **Prompt agent**: versioned via the SDK; keep the previous version and script the switch.
- **Instructions and tool descriptions are versioned artefacts** with their own changelog
  (`docs/CHANGELOG-agent.md`). A change to either requires an eval run — enforce it with a CODEOWNERS
  rule and a CI check that fails if `src/**/instructions/**` or `apim/openapi/**` changed without a
  new eval result committed.

## 4. The regression kit

`scripts/inject_*.py` — each takes `--apply` / `--revert`, is fast (< 20 s), and prints exactly what
it changed.

| Injection | What it does | Expected detection |
|---|---|---|
| `inject_bad_tool_description.py` | Swaps `compute_blast_radius`'s description for the misleading one from `tools_bad.py` (via APIM MCP tool metadata, so no redeploy is needed) | `tool_selection` and `tool_call_accuracy` collapse; traces show the agent calling `get_telemetry` instead |
| `inject_schema_drift.py` | Removes the `depth` parameter from the tool schema while the backend still requires it | `tool_input_accuracy` fails; Function 400s visible in traces |
| `inject_stale_knowledge.py` | Boosts the 5 stale runbooks in the search ranking | `groundedness` and `stale_knowledge` category fail |
| `inject_weak_instructions.py` | Switches the agent to `instructions v0` | `task_adherence`, `intent_resolution` drop across the board |
| `inject_latency.py` | Adds 4 s of latency to `func-atlas-api` via a chaos flag | p95 alert fires; latency gate fails |
| `inject_prompt_injection.py` | Raises the ranking of an `injection_canary` runbook for the demo query | Injection category must still pass — this one proves the defence works |

`scripts/restore_baseline.py` reverts **all** injections and verifies with `verify_smoke.sh`.
CI runs apply-then-revert nightly to prove the restore path never rots.

## 5. Governance across teams and environments

Document and demonstrate:
- Foundry project per environment; RBAC groups (`atlas-devs`, `atlas-approvers`, `atlas-operators`)
  and what each can do.
- APIM products as the tool-access boundary between teams; a second product with a narrower tool set
  to show least privilege.
- Toolbox versioning: publish a new toolbox version, test it, promote to default, roll back.
- Agent registry/publishing: where a versioned agent endpoint is registered and who may consume it.
- An `docs/AGENT-GOVERNANCE.md` one-pager: who owns instructions, who owns tools, who owns
  thresholds, what requires an eval, what requires a human approval, what the rollback SLO is.

## Verification — `scripts/verify_phase13.sh`

1. CI passes on a clean commit.
2. `inject_bad_tool_description.py --apply` → quality gate **fails** with a clear per-category diff;
   `--revert` → gate passes. Both runs' scorecards are archived.
3. Canary: promote a revision to 10 %, fire the error alert artificially, confirm auto-abort.
4. `rollback.yml` restores the previous release and smoke passes, timed under 3 minutes.
5. The CODEOWNERS/eval-required check fails a synthetic PR that edits instructions without evals.
6. Nightly apply/revert job is green.

## Acceptance criteria

- [ ] The gate demonstrably blocks a bad build and demonstrably passes a good one.
- [ ] Every injection is one command to apply and one to revert, and the revert is CI-proven.
- [ ] Rollback of app and agent is under 3 minutes end to end.
- [ ] No secrets in CI; OIDC only.
- [ ] `docs/AGENT-GOVERNANCE.md` is written and short enough to read on a slide.

## Report back

The failing gate's output, the passing gate's output, and the rollback timing.
