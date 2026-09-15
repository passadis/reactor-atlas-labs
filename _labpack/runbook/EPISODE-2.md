# Episode 2 — Reliable Multi-Agent Systems: Orchestration, State, Human Approval

**Slot:** 60 min · **Live demo:** ~30 min

## T-10 minutes

```bash
make demo-reset
bash scripts/warm.sh
bash scripts/verify_smoke.sh
uv run python scripts/inspect_checkpoints.py --clear-demo-case
```

Open: ATLAS web app (chat + approvals tab), Cosmos Data Explorer on `workflow-checkpoints`,
a terminal, VS Code on `src/atlas_orchestrator`.

---

## Beat 1 — Why more agents is not automatically better (4 min)

Start with the failure. Run the canonical incident through
`ATLAS_ORCHESTRATION_MODE=sequential_only` with all four specialists chained naively and narrate
what is wrong: knowledge and impact do not depend on each other but run serially; a failure anywhere
kills the run; nobody owns the decision to act.

Then show the real graph: web app → case view → **Graph** tab (live Mermaid from
`GET /cases/{id}/graph`). Not a slide. The running system's topology.

---

## Beat 2 — Three orchestration patterns, one case (7 min)

| Pattern | How you show it | When it is right |
|---|---|---|
| **Sequential** | `intake → triage` — triage's output is the next node's input | A genuine dependency chain |
| **Concurrent** | `knowledge ‖ impact` — show wall time (~11 s) vs sum of branches (~19 s) in the activity panel | Independent analyses over the same facts |
| **Handoff** | Case view → *Ask* → "who else is affected?" → `front_desk` hands off to `impact_agent`, control returns to you | Interactive, open-ended follow-up where the user is in the loop every turn |

Say plainly: sequential and concurrent do not pause for free-form human input on their own. If you
need that between steps, you add an explicit request port — which is exactly what we do next.

---

## Beat 3 — Typed state and the decision log (5 min)

| Step | Action | Say |
|---|---|---|
| 1 | Open `state.py`, show `IncidentCase` and `schema_version` | State is a typed document with a version, not a dict that grows. |
| 2 | Case view → **Decisions** tab | Every transition: who decided (code / agent / human), what, and why. This is your audit trail and your debugging tool. |
| 3 | Show `transition()` rejecting an illegal status change in the test output | The state machine is enforced, not hoped for. |

---

## Beat 4 — Agentic vs deterministic (6 min) — **the spine of the episode**

Open `src/atlas_orchestrator/policy.py` on screen and read the six rules out loud.

| Step | Action | Say |
|---|---|---|
| 1 | Show `PolicyDecision` and the rules | Authorization, change freeze, blast-radius limits, entitlements, staleness, expiry. **No model call in this file.** |
| 2 | Run the property test: 1000 random plans, invariant "no destructive production action is ever auto-approved" | You cannot prompt-engineer your way to this guarantee. You write it. |
| 3 | Contrast: the planner's `requires_approval` field | The agent has an *opinion*. The gate has *authority*. Never let those be the same object. |
| 4 | Run a low-risk Tier-4 case → auto-approved, no human | The gate is not a rubber stamp on everything; it is a rule set. |

---

## Beat 5 — The set piece: kill and resume (8 min)

```bash
# 1. Start the canonical case from the web app. Let it reach "awaiting approval".
# 2. Show the checkpoints:
uv run python scripts/inspect_checkpoints.py $CASE_ID
#    -> supersteps, timestamps, pending approval request
# 3. Show the checkpoint document in Cosmos Data Explorer.
# 4. Kill it:
bash scripts/kill_orchestrator.sh        # scales to 0, then back to 2
#    Show the web app going into "orchestrator unavailable" degraded state.
# 5. While it is down, approve from the approvals console (request queues).
# 6. When replicas are back:
#    the sweeper picks up the approved case and resumes automatically.
# 7. Compare token usage before/after:
uv run python scripts/case_cost.py $CASE_ID
#    -> triage/knowledge/impact were NOT re-run. Resume is not replay.
```

Say: *"The workflow did not restart. It resumed from superstep 7 with its state and its pending
request intact. That is the difference between a demo and something you can operate."*

> Fallback: `docs/assets/fallbacks/ep2-kill-resume.cast` and the archived checkpoint JSON.

---

## Beat 6 — Human approval, two mechanisms (5 min)

| Mechanism | Where | When to use |
|---|---|---|
| Workflow request-info / request port | After `policy_gate`, for the *plan* | The pause is a business step, and the whole plan is what needs a decision |
| Tool-level approval | On `submit_remediation` | A specific sensitive call needs sign-off wherever it occurs |

Show both firing on the same case. Then show the three things the approval is bound to:
the **approver's role**, the **exact `plan_id`**, and an **expiry**. Change the plan, and the
previous approval is void — demonstrate the 409.

---

## Close (2 min)

- Recoverable state, controlled execution, a real decision boundary, and a human in the loop without
  losing execution context.
- Still missing: the agent cannot actually *do* anything. Every "action" so far has been a write to
  our own database.
- Next week: we give it real capabilities, and we put a governed gateway in front of every one of
  them.

## Timing guard

Beat 5 is non-negotiable. If short, compress Beat 2 to two patterns and drop Beat 3 step 3.
