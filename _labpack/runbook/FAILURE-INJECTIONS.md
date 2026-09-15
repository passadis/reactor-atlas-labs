# Failure Injection Kit

Every injection: `--apply` / `--revert`, under 20 seconds, prints exactly what it changed, and is
CI-proven to restore cleanly. `scripts/restore_baseline.py` reverts all of them.

**Rule: never apply an injection without first confirming `verify_smoke.sh` is green.** You cannot
debug a break on top of a break in front of an audience.

---

## Quality regressions (Episode 4)

### `inject_bad_tool_description.py`
Replaces `compute_blast_radius`'s description in the APIM MCP tool metadata with the misleading text
from `src/atlas_agent/tools_bad.py`. **No redeploy** — the toolbox picks it up on the next tool list.

- Expected: `tool_selection` and `tool_call_accuracy` fall below threshold; traces show
  `get_telemetry` called where `compute_blast_radius` should have been; blast radius missing from the
  report; severity often under-called.
- Revert time: < 10 s. Tool list cache may take up to 60 s to refresh — factor that into the demo.

### `inject_schema_drift.py`
Removes the `depth` parameter from the tool schema while the backend still requires it.

- Expected: `tool_input_accuracy` fails; the Function returns 400; the agent degrades and says the
  impact analysis is unavailable. Shows a **contract break**, not a model failure — a useful
  distinction to draw on stage.

### `inject_weak_instructions.py`
Switches every agent to `instructions v0`.

- Expected: broad drop in `task_adherence`, `intent_resolution`, `groundedness`; citations vanish.
- This is also the mechanism behind the v0-vs-v1 comparison in Episode 1 and Beat 4 of Episode 4.

### `inject_stale_knowledge.py`
Boosts the five out-of-date runbooks in the AI Search ranking profile.

- Expected: `stale_knowledge` category fails; the agent actions a step that no longer exists instead
  of flagging the staleness. Revert restores the default scoring profile.

---

## Safety and security (Episodes 3 and 4)

### `inject_prompt_injection.py`
Raises the ranking of an `injection_canary` runbook so it is retrieved for the demo query.

- Expected: **the injection category still passes.** The agent reports the suspicious content and
  does not call `submit_remediation`. This injection proves the defence works, so run it *after* the
  ones that break things — it is the recovery beat.
- If it ever fails, that is a real finding. Do not ship.

### `inject_forged_approval.py`
Submits a remediation with a fabricated `approval_token`.

- Expected: the worker rejects it, writes an audit record, and performs no side effects. Cosmos
  shows the rejection, not an execution.

### `chaos/revoke_servicebus_role.sh`
Removes the orchestrator's `Azure Service Bus Data Sender` assignment.

- Expected: dispatch fails **before** the plan is marked executed. Fail closed.
- Revert requires up to 5 minutes for role propagation — apply the revert before the beat that needs
  it, not during.

---

## Reliability (Episodes 1–3)

| Script | Effect | Expected behaviour |
|---|---|---|
| `chaos/ops_api_down.sh` | Stops `func-atlas-api` (or sets its chaos flag to return 503) | Tools return typed failures; UI shows a clear message; no hallucinated ticket |
| `chaos/knowledge_down.sh` | Points AI Search at a bad endpoint | Case completes `degraded`; report names the missing analysis |
| `chaos/model_throttle.sh` | Drops `llm-token-limit` to 100 TPM | 429 handling, retry countdown banner, alert fires |
| `chaos/worker_fail.sh` | Forces the worker to fail on step 3 | Retry → 5 deliveries → DLQ → alert → `dlq_replay.py` recovers |
| `chaos/cosmos_throttle.sh` | Drops RU to minimum | Backoff, no data loss, alert fires |
| `chaos/latency.sh` | Adds 4 s to every ops API call | p95 alert fires; latency gate fails |
| `kill_orchestrator.sh` | Scales ACA to 0 then back to 2 | Checkpoint resume (Episode 2 set piece) |

---

## Nightly proof

`.github/workflows/chaos-restore.yml` runs, every night against staging:

```
for each injection: apply -> assert expected signal -> revert -> verify_smoke.sh
```

If a revert path rots, you find out at 03:00, not on stage. This job is the reason you can trust
`restore_baseline.py` in front of an audience.

---

## Pre-episode checklist

```bash
uv run python scripts/restore_baseline.py
bash scripts/chaos/restore_all.sh
make demo-reset
bash scripts/warm.sh
bash scripts/verify_smoke.sh          # must be green
uv run python scripts/show_pinned_state.py   # confirm versions match docs/DEMO-NOTES.md
```

If `verify_smoke.sh` is not green, **do not start the live demo.** Switch to the recorded fallback
for the affected beat and carry on. The runbooks tell you which fallback to use for every beat.
