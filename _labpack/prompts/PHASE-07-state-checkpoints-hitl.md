# PHASE 07 — Typed state, checkpointing, recovery, human approval (Episode 2)

> Depends on: Phase 06.
> Episode 2 topics: typed workflow state, checkpointing and recovery, human-in-the-loop approvals,
> failure handling and idempotency.

---

This is the Episode 2 set piece: **kill the orchestrator mid-approval, restart it, resume exactly
where it stopped.** Build it so it works every time.

## Before you write code

Query MS Learn MCP for the current Agent Framework pages on checkpoints, workflow state, and
human-in-the-loop (request/response, request ports, and tool approval). Confirm against the
installed package:
- the checkpoint storage protocol and whether `agent-framework-azure-cosmos` /
  `CosmosCheckpointStorage` is available in your version (install it if so),
- how executors persist their own state (`on_checkpoint_save` / `on_checkpoint_restore` or the
  equivalent),
- how a workflow raises a request for external input and how responses are delivered when resuming,
- whether pending requests are re-emitted on restore.

Update `docs/API-NOTES.md`.

## 1. Typed workflow state

`src/atlas_orchestrator/state.py`:

```python
class Decision(BaseModel):
    at: datetime; by: Literal["code","agent","human"]; node: str
    decision: str; rationale: str; inputs_hash: str

class IncidentCase(BaseModel):
    case_id: str
    schema_version: Literal[1] = 1          # version your state from day one
    created_at: datetime
    reporter: str
    raw_report: str
    triage: TriageResult | None = None
    knowledge: KnowledgeResult | None = None
    impact: ImpactResult | None = None
    plan: RemediationPlan | None = None
    policy: PolicyDecision | None = None
    approval: ApprovalRecord | None = None
    execution: ExecutionRecord | None = None
    degraded: list[str] = []
    decisions: list[Decision] = []
    status: Literal["intake","analysing","plan_ready","awaiting_approval",
                    "approved","rejected","executing","completed","failed"]
```

Rules:
- Only executors mutate state, and only through a `transition()` helper that appends a `Decision`
  and validates the status transition against an explicit state machine. Illegal transitions raise.
- `schema_version` is checked on checkpoint restore; a mismatch fails loudly with a migration hint.
- Add `allowed_checkpoint_types` (or the equivalent) for your custom Pydantic types if the
  checkpoint store uses a restricted deserializer — do not widen it beyond what you need.

## 2. Checkpointing

- Wire `CosmosCheckpointStorage` (container `workflow-checkpoints`, partition key `/workflow_name`)
  with `DefaultAzureCredential` and the orchestrator UAMI. **No account keys.**
- Keep a `FileCheckpointStorage` option behind config for local development.
- Executor state: implement save/restore hooks on every stateful executor.
- **Executor identity stability**: the resumed workflow must rebuild with identical node ids. Give
  every agent and executor a fixed logical id (`"triage-agent"`, `"aggregate"`, …) in a single
  `NODE_IDS` module. Never use a request id, case id, or generated uuid as a node id. Add a test
  that builds the workflow twice and asserts identical topology and ids.
- Write `scripts/inspect_checkpoints.py <case_id>` that lists checkpoints with superstep number,
  timestamp, status, and pending requests — this is what you project on stage.

## 3. The deterministic policy gate

`src/atlas_orchestrator/policy.py` — **pure code, exhaustively unit tested, no model call.**

```python
class PolicyDecision(BaseModel):
    approval_required: bool
    auto_approved: bool
    blocked: bool
    reasons: list[str]            # human-readable, shown in the UI and the report
    required_approver_roles: list[str]
    expires_at: datetime
```

Rules, evaluated in order and all recorded in `reasons`:

1. **Block** if any step is `destructive` and targets a service with `change_freeze=true`.
2. **Block** if the requester's entitlement does not permit `may_execute`.
3. **Require approval** if `plan.risk == "high"`, or any target service is Tier 1 or 2, or
   `impact.blast_radius > entitlement.max_blast_radius`, or any step is `destructive`.
4. **Require approval** if any cited runbook has `last_reviewed` older than 12 months
   (stale knowledge must not be actioned silently).
5. **Auto-approve** only if risk is `low`, all targets are Tier 3/4, nothing is destructive, blast
   radius ≤ 2, and the environment is not `prod`.
6. Approval expires after 30 minutes; expired approvals are invalid and re-raise the request.

Required approver role is derived from `entitlements.json`, not from the model.

Property tests: generate random plans/entitlements and assert the invariant
*"no destructive prod action is ever auto-approved"* over ≥ 1000 cases.

## 4. Human-in-the-loop

- After `policy_gate`, if `approval_required`, the workflow raises a request for external input
  carrying an `ApprovalRequest{case_id, plan_id, plan_summary, risk, blast_radius, reasons,
  required_roles, expires_at}` and **suspends**. A checkpoint exists at this point.
- The orchestrator writes an `approvals` document to Cosmos so the approval console and the Logic
  App (Phase 10) can both see it.
- `POST /cases/{case_id}/approve` and `/reject` on the orchestrator:
  - verify the caller's role against `entitlements.json` server side,
  - verify the approval has not expired and that `plan_id` still matches the current plan
    (an approval is for a *specific* plan — if the plan changed, the approval is void),
  - resume the workflow from the checkpoint, delivering the response.
- Also demonstrate **tool-level approval**: mark the future `execute_remediation` tool as
  approval-required so the framework itself raises the approval event. Show both mechanisms and
  explain when each fits.
- The web app approval console (Phase 04) renders the pending approvals with the policy reasons,
  the plan diff, and Approve/Reject with a mandatory comment.

## 5. Idempotency and recovery

- `plan_id` is a deterministic hash of the case and the normalised plan. Dispatch (Phase 10) uses it
  as the Service Bus `MessageId` and as the Cosmos `idempotency` key.
- `scripts/kill_orchestrator.sh` — scales the ACA app to 0 replicas, then back to 2. Use this for
  the live kill/resume demo; it is far more reliable than killing a process by hand.
- `scripts/resume_case.py <case_id> [--checkpoint <id>]` — rehydrates the workflow from the latest
  (or a chosen) checkpoint and completes it, re-emitting pending requests.
- On restart, the orchestrator sweeps Cosmos for cases in `awaiting_approval` whose approvals have
  arrived while it was down, and resumes them automatically. Test this.

## 6. Tests

- State machine: every illegal transition raises; every legal one appends a `Decision`.
- Checkpoint round-trip: run to `awaiting_approval`, discard the workflow object, rebuild from a
  new process, restore, respond, complete. Assert the final state equals the uninterrupted run.
- Policy gate: table-driven over all rules + the property test.
- Approval validity: expired, wrong role, stale `plan_id` — all rejected with distinct errors.
- Idempotency: dispatch the same plan twice, assert one execution record.

## Verification — `scripts/verify_phase07.sh`

1. Canonical incident reaches `awaiting_approval` and a checkpoint exists in Cosmos.
2. `scripts/kill_orchestrator.sh` runs; while down, approve via the API is queued or retried;
   after restart, the case resumes and completes without re-running triage/knowledge/impact
   (assert by comparing span counts / token usage before and after).
3. `scripts/inspect_checkpoints.py` lists ≥ 5 checkpoints with the pending approval request visible.
4. A Tier-1 destructive plan is never auto-approved (run the property test).
5. A `readonly` persona's approval is rejected with 403 and an audit entry.
6. Approving a superseded `plan_id` is rejected.

## Acceptance criteria

- [ ] Kill/resume works reliably three times in a row from a cold start.
- [ ] Typed state is versioned and validated on restore.
- [ ] The policy gate is 100 % deterministic and 100 % covered by tests.
- [ ] Both approval mechanisms (workflow request-info and tool approval) are live.
- [ ] Exactly-once remediation is proven by test.
- [ ] `docs/DEMO-NOTES.md` has the Episode 2 second half with the exact kill/resume script.

## Report back

The checkpoint listing for the canonical case, before/after token usage across the resume, and the
policy reasons string that the UI shows.
