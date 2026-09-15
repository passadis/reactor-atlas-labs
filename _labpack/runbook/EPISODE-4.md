# Episode 4 — Production-Grade Agents: Evaluation, Tracing, Security, Operations

**Slot:** 60 min · **Live demo:** ~32 min

## T-15 minutes

```bash
make demo-reset
bash scripts/warm.sh
bash scripts/verify_smoke.sh
uv run python scripts/restore_baseline.py       # no injections active
uv run python evals/run_eval.py --mode dataset --dataset holdout --tag pre-show   # warm the eval path
```

Open: Application Insights (workbook + transaction search), the Foundry project traces view, the
GitHub Actions tab, the ATLAS web app, a terminal.

---

## Beat 1 — "It worked in the demo" is not evidence (3 min)

Run the canonical incident. It works. Then ask the audience the five questions you cannot answer
from that:

1. Does it pick the right tool when the question is ambiguous?
2. What happens when a tool returns garbage?
3. Will it refuse a destructive action from someone who is not allowed to request it?
4. Does it follow instructions hidden inside a document it retrieved?
5. Is it better or worse than last week's version?

Everything in this episode answers one of those.

---

## Beat 2 — One trace, six hops (6 min)

```bash
make trace-check CASE=$CASE_ID
```

Then the same trace in the portal: the end-to-end transaction waterfall covering
web → orchestrator → each agent node → APIM → Function → Service Bus → worker.

Point at:
- `gen_ai.*` spans from the framework instrumentation — model calls, tool calls, token counts;
- `atlas.*` custom dimensions — `case_id`, `node`, `instructions_version`, `decision`;
- the Service Bus span **link** that survives the async hop;
- the honest seam: where the Logic Apps connector drops `traceparent` and we correlate by `case_id`
  instead. Say so. Nobody believes a tracing story with no seams.

Then the workbook: seven sections, one screen, every operational question.

---

## Beat 3 — The evaluation dataset (5 min)

Open `evals/datasets/atlas-eval-v1.jsonl` and walk the seven categories:

| Category | Why it exists |
|---|---|
| `normal` (20) | The job |
| `ambiguous` (10) | Must ask, not guess |
| `tool_failure` (8) | Must degrade and say so |
| `unauthorized` (8) | Must be refused by policy, and explained |
| `stale_knowledge` (8) | Must flag, not action |
| `prompt_injection` (10) | Direct and **indirect** — payloads hidden in retrieved runbooks |
| `safety` (8) | Harm, exfiltration, impersonation |

Show one `prompt_injection` case and the runbook it hides in
(`data/seed/runbooks/RB-00xx.md`, `injection_canary: true`). *"This is the attack that matters for
agents. The user is polite; the document is hostile."*

---

## Beat 4 — Measuring quality (6 min)

```bash
uv run python evals/run_eval.py --mode target --dataset atlas-eval-v1 --tag show
```
While it runs, show the previous scorecard (`evals/results/<run>/scorecard.md`) on screen and
explain the metric families:

- **Agent behaviour** — intent resolution, task adherence, task completion
- **Tool quality** — tool selection, tool call accuracy, tool input accuracy, tool output
  utilisation, tool call success
- **Grounding and quality** — groundedness, relevance, response completeness
- **Safety** — the risk evaluators plus indirect-attack
- **Operational** — p95 latency, tokens per case, cost proxy

Then the v0-vs-v1 table (`evals/results/v0-vs-v1.md`): same model, same tools, same data, different
instructions, materially different numbers. This closes the loop opened in Episode 1.

Then red teaming:
```bash
uv run python evals/run_redteam.py --quick --report
```
Show the Attack Success Rate scorecard. State the gate: **ASR on prohibited and irreversible actions
must be 0 %.** Content-risk ASR is reported and tracked with a threshold you chose deliberately.

---

## Beat 5 — The regression (10 min) — **the set piece**

```bash
# 1. Break it. No redeploy: we change the tool metadata in APIM.
uv run python scripts/inject_bad_tool_description.py --apply
```

Now run the canonical incident in the web app. It still *looks* fine — that is the point. The
severity is wrong and the blast radius is missing.

```bash
# 2. Find it in the traces:
make trace-check CASE=$NEW_CASE_ID
```
The agent called `get_telemetry` where it used to call `compute_blast_radius`. Show it in the
waterfall and in the workbook's tool heat map.

```bash
# 3. Quantify it:
uv run python evals/run_eval.py --mode target --dataset holdout --tag broken
uv run python evals/diff_scorecards.py --from show --to broken
```
`tool_selection` and `tool_call_accuracy` collapse; the composite drops below threshold.

```bash
# 4. Try to ship it:
gh workflow run agent-quality-gate.yml
```
Watch the gate **fail**, with the per-category diff posted on the commit. *"That is the whole point.
The system that catches this is not a person reading a diff at 6pm on a Friday."*

```bash
# 5. Fix and re-ship:
uv run python scripts/inject_bad_tool_description.py --revert
gh workflow run agent-quality-gate.yml
```
Green. Promoted. Release tagged with the scorecard attached.

> Fallback: `docs/assets/fallbacks/ep4-regression/` has both workflow runs, both scorecards and the
> trace screenshots. The eval run takes 4–7 minutes — have the recorded version cued.

---

## Beat 6 — Operations (5 min)

| Step | Action | Say |
|---|---|---|
| 1 | Alert rules list; fire the DLQ alert live via `scripts/chaos/worker_fail.sh --apply` | Alerts on tool error rate, p95, throttling, token burn, DLQ depth, approval timeouts, guardrail blocks, and **evaluation score drop**. That last one is the agent-specific addition. |
| 2 | Canary: `scripts/canary.sh <rev> 10`, then artificially trip the error alert | Auto-abort. 10 % of traffic saw a problem, not 100 %. |
| 3 | `gh workflow run rollback.yml -f release=agent-v12` | Under three minutes for both the app revision and the hosted agent version. Time it on screen. |
| 4 | `docs/AGENT-GOVERNANCE.md` on screen | Who owns instructions, who owns tools, who owns thresholds, what requires an eval, what requires a human, what the rollback SLO is. Four teams, three environments, one page. |

---

## Close (3 min)

The four-episode arc in one slide:

1. An agent with a contract — instructions, typed tools, state, structured output.
2. A workflow that can be interrupted, resumed, and stopped for a human.
3. Capabilities behind a governed gateway, with no credentials in the model's hands.
4. Evidence: traces, evaluations, adversarial tests, and a gate that blocks a bad build.

Then the honest list of what is still missing for a regulated production deployment: network
isolation, customer-managed keys, multi-region, real ITSM integration, data residency, and a
sustained human review process for the eval set.

Repo and write-up: `docs/PUBLIC-README.md`.

## Timing guard

Beat 5 is the episode. If you are short, compress Beats 2 and 4 and run the recorded eval instead of
the live one — but run the regression live.
