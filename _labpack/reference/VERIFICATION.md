# Verification

Every phase ships a `scripts/verify_phase<NN>.sh`. They are idempotent, exit non-zero on failure,
and print a readable pass/fail table. `make verify-all` runs them in order plus the three
cross-cutting checks.

```
make verify-all
```

Output: a table on stdout and `verification-report.json` with per-check timings, so you can see
what got slower between rehearsals.

---

## The 90-second pre-flight — `scripts/verify_smoke.sh`

Run this before **every** rehearsal and **every** episode. If it is not green, use the recorded
fallback for the affected beat.

| # | Check | Pass condition |
|---|---|---|
| 1 | `ca-atlas-web` health | 200, all dependencies `ok`, ≥ 2 replicas ready |
| 2 | `ca-atlas-orchestrator` health | 200, ≥ 2 replicas ready |
| 3 | `ca-atlas-worker` | ≥ 1 replica, Service Bus receiver connected |
| 4 | `func-atlas-api` / `func-atlas-mcp` health | 200 each |
| 5 | APIM gateway | `atlas-ops-api` ping returns 200 with a valid token |
| 6 | MCP `tools/list` | 6 tools with schemas |
| 7 | Foundry model | one throwaway completion on `gpt-main` succeeds < 5 s |
| 8 | Hosted agent | `azd ai agent show` reports the pinned version `active` |
| 9 | Cosmos | read + write to `sessions` with the deployed identity |
| 10 | AI Search | canonical query returns the expected top hit |
| 11 | Service Bus | queue depths are 0, DLQ depth is 0 |
| 12 | Canonical case | end-to-end to `awaiting_approval` in < 60 s |
| 13 | Pinned state | versions match `docs/DEMO-NOTES.md` `PINNED STATE` |
| 14 | No injections active | `restore_baseline.py --check` reports clean |

---

## Cross-cutting checks

### `scripts/verify_security.sh`
See `RBAC-MATRIX.md` § "Explicitly forbidden" and Phase 14 § 2. Zero findings required.

### `scripts/trace_check.py <case_id>`
Asserts one `operation_Id` covers web, orchestrator, every agent node, APIM, both function apps,
Service Bus and the worker; every span carries `atlas.case_id`; prints the waterfall as text.

### `scripts/verify_demo_paths.sh`
Executes **every command that appears in `runbook/EPISODE-*.md`** and asserts its expected output.
If you add a command to a runbook, add it here. This is what stops a runbook from silently rotting.

---

## Per-phase summary

| Phase | Script | Headline assertion |
|---|---|---|
| 00 | `verify_phase00.sh` | Toolchain green; `docs/API-NOTES.md` records verified symbols |
| 01 | `verify_phase01.sh` | Every resource present, every role assignment present, no secrets in outputs |
| 02 | `verify_phase02.sh` | Deterministic data; Cosmos and Search counts match; frozen blast radius matches |
| 03 | `verify_phase03.sh` | Canonical triage is correct and cited; tools fail typed; guardrails hold |
| 04 | `verify_phase04.sh` | Public URL streams; TTFT < 3 s warm; auth enforced; cancel works |
| 05 | `verify_phase05.sh` | Hosted agent active; three surfaces agree; rollback proven |
| 06 | `verify_phase06.sh` | All three orchestration patterns complete; concurrency proven by timing; degradation works |
| 07 | `verify_phase07.sh` | Kill/resume works; policy invariants hold over 1000 random cases |
| 08 | `verify_phase08.sh` | OpenAPI valid and matches deployment; idempotency proven; MCP lists 6 tools |
| 09 | `verify_phase09.sh` | Eight governance proofs pass; no direct backend access from any surface |
| 10 | `verify_phase10.sh` | End-to-end remediation < 3 min; exactly-once; DLQ replay; trace crosses Service Bus |
| 11 | `verify_phase11.sh` | One trace, six hops; workbook populated; an alert proven to fire |
| 12 | `verify_phase12.sh` | Baseline passes thresholds; safety/unauthorized/injection at 100 %; ASR 0 % on prohibited actions |
| 13 | `verify_phase13.sh` | Gate blocks the injected regression and passes after revert; rollback < 3 min |
| 14 | `verify_phase14.sh` | Chaos drills all fail closed; demo-reset < 60 s; all runbook commands execute |

---

## Reading a failure

1. **403 right after provisioning** → role propagation. Wait 5 minutes, re-run. If it persists on
   Cosmos, you almost certainly created an ARM role assignment instead of a Cosmos SQL role
   assignment.
2. **429 from the model** → quota or the APIM token limit. Check `x-atlas-remaining-tokens`.
3. **MCP `tools/list` empty** → the Functions `mcp_extension` system key, or APIM response payload
   logging is not 0 bytes at global scope (it buffers and breaks streaming).
4. **Checkpoint restore fails** → node ids changed. Check `NODE_IDS`; never derive a node id from a
   case, request or user id.
5. **Trace breaks at Service Bus** → `traceparent` not copied into application properties, or the
   worker is not creating a span *link*.
6. **Eval scores drop with no code change** → check whether the model deployment version rolled
   underneath you. This is why versions are pinned; `show_pinned_state.py` will tell you.
