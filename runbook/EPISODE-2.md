# Episode 2 - Reliable Multi-Agent Systems: orchestration, state, human approval

**Airs:** 2026-10-08, 18:00 EEST. **Slot:** slides, then the demo, then Q&A.
**Everything is already deployed.** One beat changes infrastructure on purpose and is rehearsed:
the orchestrator kill (Beat 5). Nothing else builds, deploys or changes anything.

Every command and expected result below is executed by
`make verify-demo-paths PATHS="ep2"` (10/10 on 2026-10-07, from a cold reset). The prompts and the
approval flow are explained for a newcomer in `docs/PRESENTER-GUIDE-PROMPTS.md`.

---

## T-10 minutes (in this order)

```powershell
az login; azd auth login
git status                     # clean
make demo-reset                # 7-14 s
make warm                      # about 40 s - at T-10 only, never during the slides
make smoke                     # 5/5
```

Open and arrange:

- the web app, **Scope: Episode 2**
- the **Approvals** page in a second tab
- VS Code on `src/atlas_orchestrator/policy.py`
- a terminal

Keep the prompts in Notepad: the canonical report and reporting-ttl, from the guide.

---

## Beat 1 - The graph is real (4 min)

| Do | The room sees (measured) | Say |
|---|---|---|
| Point at the **Workflow graph** panel | 9 nodes, each marked **code** or **agent** | This is drawn from the deployed workflow object, not a slide. |
| Expand **Mermaid source** | The same graph as text | Four of the seven working nodes are plain code. |
| Shape selector: Concurrent graph, Sequential only, Handoff | 10, 8 and 20 edges | Three orchestration shapes, one typed state contract. |

## Beat 2 - Run the canonical case (6 min)

| Do | The room sees (measured) | Say |
|---|---|---|
| **New session**, canonical report, **Run in workflow** | Nodes light up: intake, triage, then **knowledge and impact together**, aggregate, planner, policy gate. Parks in **42-54 s**. | The fan-out is real: two specialists over the same facts, at once. |
| Read the gate's reasons in the approval panel | "**break glass**: svc-payments is under change freeze but is implicated in this incident; **a director must approve**", high risk, Tier 1 targets, destructive steps | The agents did the thinking. Code decided this needs a director. |

## Beat 3 - Typed state and the decision log (4 min)

| Do | The room sees | Say |
|---|---|---|
| Scroll to the decision log | About 12 entries, each naming its rule (`severity=max(triage,impact)`, `tier1-in-blast-radius`, ...) | Every transition, who decided it - code, agent or human - and why. Only code writes here. |

## Beat 4 - Agentic vs deterministic: the spine (6 min)

| Do | The room sees (measured) | Say |
|---|---|---|
| VS Code: `policy.py`, read the rules | Freeze, blast radius, entitlements, staleness, expiry. **No model import.** | You cannot prompt-engineer this guarantee. You write it. |
| Terminal: `uv run pytest -q tests/test_policy.py` | All pass in under a second, including 1000 random plans and "the gate reaches no model" | One invariant: no destructive production action is ever auto-approved. |
| Header **act as: manager**, then **Approve** | **Refused**: "this plan needs director; you are acting as manager". The case stays parked. | The UI and the server both refuse. A wrong answer cannot advance a plan. |
| **act as: myself**. Do NOT approve yet | | Hold it. First, let us kill the thing holding it. |

## Beat 5 - The set piece: kill and resume (8 min) - never cut

| Do | The room sees (measured) | Say |
|---|---|---|
| Terminal: `make checkpoints CASE=<case id>` | One checkpoint per superstep; the last is marked **PARKED ON A HUMAN** | The pending approval is a document in Cosmos, not memory in a process. |
| Terminal: `make kill-orchestrator` | "every replica is being replaced (in-memory state is lost)". Answers again after about **9 s**. **"every old replica replaced after 80-91 s - approve and start cases from here"** | Every replica is being terminated. Everything in memory is going. |
| **While it waits (about 80 s):** run `make checkpoints CASE=<id>` again | The same parked checkpoint, read straight from Cosmos while the orchestrator is down | Nothing was lost. The case is waiting exactly where it stopped. |
| **After the "replaced" line:** Approve, as myself | The case resumes **from the checkpoint**: no agent re-runs. The ticket and remediation complete about **30 s** later. | It did not restart. It resumed - from superstep 7, with its state and its question intact. |

**Wait for the "replaced" line before approving.** A restart retires the old replicas one by one,
so during those 80 s an approval or a new case can land on a replica that is about to disappear.
Measured 2026-10-07: a case started 2 minutes after a kill died with the last old replica. The
wait is the natural moment for `make checkpoints`.

## Beat 6 - The gate is graded, not "always ask" (4 min)

| Do | The room sees (measured) | Say |
|---|---|---|
| **New session**, reporting-ttl prompt, **Run in workflow** | Gate: **auto-approved** - "low risk, Tier 4, reversible steps each with a rollback, blast radius 1, staging". No approval panel. Remediation succeeds on its own. | Same gate, same code. Low risk in staging goes straight through. Break glass on a frozen Tier 1 service needs a director. |

## Close (2 min)

- Recoverable state, a real decision boundary, a human in the loop without losing context, and a
  process you can kill mid-decision.
- Still missing: real capabilities behind a governed gateway. That is Episode 3.

**Timing guard.** Over time? Drop Beat 3 and the shape selector in Beat 1. **Never cut Beat 5.**

---

## Encore - all the way to the end

The canonical case from Beat 2, approved in Beat 5, already ran end to end, including ticket and
remediation. With Scope **Episode 3** or **Full** the remediation timeline shows the worker steps.
For a fresh one: `make e2e-remediation` (70-120 s).

---

## Fallbacks

| If this fails live | Do this |
|---|---|
| The case does not park | The gate may have auto-approved or blocked it. The reasons say which. Read them - that is a beat too. |
| Kill takes longer than 2 minutes | Keep narrating `make checkpoints` (Cosmos answers while the orchestrator is down). |
| Approval does not move the case | The console says if it could not be applied and offers it again: press Approve once more. Last resort: `make resume CASE=<id> APPROVE=1` (as the operator, a director). |
| Approval refused | Check **act as** says myself. A refusal as manager is the Beat 4 point, not a failure. |
| Orchestrator unreachable | The banner says so and chat still works. Show `docs/assets/phase06-*.png` and `phase07-*.png`. |
