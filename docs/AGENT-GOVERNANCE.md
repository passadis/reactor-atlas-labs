# ATLAS agent governance - one page

| Artefact | Owner | Changes require | Rolled back by |
|---|---|---|---|
| Agent instructions (`src/atlas_agent/atlas_agent/instructions/`) | Agent owner | A new evaluation committed (`make eval`); CI fails the PR otherwise; CODEOWNERS review | Pointer flip in Cosmos (instant, no redeploy) |
| Tool descriptions (`apim/mcp/*.json`) and the tool API (`apim/openapi/`) | Platform owner | Same: a new evaluation, CODEOWNERS review | `scripts/inject_bad_tool_description.py --revert` / redeploy the JSON |
| Thresholds (`evals/thresholds.yaml`) | Quality owner | A reviewed PR. The injection, unauthorized and safety gates stay at 100 % | Git revert |
| Reviewed judge exceptions (`evals/reviewed_exceptions.yaml`) | Quality owner, named in the file | A human review; pinned to the exact reply, lapses when it changes | Delete the entry |
| Hosted agent versions | Agent owner | The quality gate PASS on the holdout | `rollback.yml` (pin flip + smoke, measured under 3 min) |
| Approval policy (`atlas_orchestrator/policy.py`) | Platform owner | Code review + property tests; never decided by a model | Git revert |

**What the model may decide:** classification, severity, summaries, which runbook applies, the
draft plan and its risk grade.
**What it may never decide:** whether a plan may run, who approves, authorisation of a remediation
command, retries. Those are code (`policy.py`, the single-use approval token, the worker).

**Environments and access.** One demo environment, `atlas` (a staging copy is cut by design -
CLAUDE.md 7.12). Humans hold app roles (`Atlas.Engineer`, `Atlas.Approver`, `Atlas.Director`,
`Atlas.ReadOnly`); automation authenticates with OIDC as `atlas-github-ci` - no secrets - holding
only the narrow roles in `infra/modules/rbac.bicep` (CI identity). APIM products are the tool-access
boundary: `atlas-agent-tools` for the agents, a read-only product with a narrower tool set.

**The gate.** Evaluate-only: it evaluates an already-deployed hosted-agent version on 20 held-out
cases, blocks on the thresholds and the last green baseline, and never builds or deploys.

**Rollback SLO.** Under 3 minutes from decision to a green smoke check.
