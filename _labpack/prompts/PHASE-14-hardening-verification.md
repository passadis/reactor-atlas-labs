# PHASE 14 — Hardening, chaos, and full verification

> Depends on: Phases 00–13. Run this **at least 48 hours before Episode 1 airs.**

---

Everything is built. This phase makes it survive a live audience.

## 1. Full verification harness

`make verify-all` runs every `scripts/verify_phase*.sh` in order, plus:

- `scripts/verify_smoke.sh` — the 90-second confidence check you run before going on air.
- `scripts/verify_security.sh` — see below.
- `scripts/verify_demo_paths.sh` — every single command in `runbook/EPISODE-*.md`, executed, with
  its expected output asserted. **If a command is in the runbook, it is in this script.**

Output: one pass/fail table, and a machine-readable `verification-report.json` with timings.

## 2. Security hardening pass

`scripts/verify_security.sh` must prove:

1. **No secrets in the repo** — run `gitleaks` (or equivalent) over full history; zero findings.
2. **No secrets in configuration** — `azd env get-values`, ACA env vars, Function app settings and
   Logic App parameters contain no key material; the only Key Vault reference is the APIM
   subscription key.
3. **Local auth disabled** on Cosmos, AI Search, Service Bus, and Storage where supported; assert via
   ARM properties.
4. **Least privilege** — diff actual role assignments against `reference/RBAC-MATRIX.md`; fail on
   any extra assignment, especially `Contributor` or `Owner` on a managed identity.
5. **No public path to backends** — the function apps reject requests without an Entra token for the
   expected audience; assert from an unauthenticated client.
6. **Content filters active** on `gpt-main`; assert via the RAI policy association.
7. **Prompt-injection defence** — the injection canary suite passes against all four agent surfaces.
8. **Approval token integrity** — forged, expired, replayed and cross-case tokens are all rejected.
9. **PII hygiene** — content recording is on only in the demo environment, and seeded data contains
   no real emails or domains (regex scan for non-`.example` domains).

## 3. Chaos drills

Run each, confirm the system degrades visibly and safely, and record what the audience would see:

| Drill | Command | Expected |
|---|---|---|
| Model throttling | Exhaust the APIM token limit | Retry countdown banner, no crash, alert fires |
| Foundry unavailable | Point the model backend at a dead URL | Typed error in UI, case marked `failed`, trace shows the boundary |
| Knowledge agent down | Bad AI Search endpoint | Case completes `degraded`, report says which analysis is missing |
| Ops API down | Stop `func-atlas-api` | Tools return typed failures; agent says it cannot reach the ticketing system |
| Service Bus unreachable | Revoke the sender role | Dispatch fails **before** marking the plan executed; idempotency record stays `dispatched=false` |
| Worker crash mid-plan | Kill the worker between steps 2 and 3 | Message redelivered, idempotent step handlers skip completed steps, execution completes |
| Orchestrator restart | `scripts/kill_orchestrator.sh` | Case resumes from checkpoint |
| Cosmos throttling (429) | Drop RU to minimum | Backoff, no data loss, alert fires |
| Approval timeout | Shorten TTL | Case rejected with `expired`, no command sent |
| Network partition to APIM | Block egress | All agent surfaces fail closed, never fail open to a direct backend call |

Write each as `scripts/chaos/<name>.sh` with `--apply`/`--revert`.

**Critical assertion**: in no drill does the system fall back to an ungoverned path. Failing closed
is the correct behaviour and is part of the Episode 3 story.

## 4. Demo reliability work

1. **Warm everything.** `scripts/warm.sh`: pre-pull images, force ACA min replicas, issue one
   throwaway model call per deployment, prime the AI Search cache, touch every Function to avoid
   cold starts, refresh tokens. Run it 10 minutes before each episode.
2. **`make demo-reset`** — under 60 seconds, returns runtime containers, Service Bus queues, approval
   state, injected regressions, agent versions and traffic splits to baseline. Idempotent. Run it
   between rehearsals and after every episode.
3. **Pin everything.** Model deployment versions, hosted agent version, prompt agent version,
   container image digests, toolbox version, instruction version. Record them all in
   `docs/DEMO-NOTES.md` under a `PINNED STATE` heading with the date.
4. **Freeze.** After this phase, no deploys to `atlas` except via `rollback.yml` or a deliberate
   episode-day change you have rehearsed.
5. **Offline fallbacks.** For every live step in the runbooks, capture:
   - a recorded terminal session (`asciinema` or a plain log),
   - screenshots of every portal blade you plan to open,
   - a saved Application Insights query result and trace waterfall,
   - the eval scorecards as committed markdown.
   Store in `docs/assets/fallbacks/` and reference them from the runbooks. Assume the venue Wi-Fi
   will fail during Episode 3.
6. **Latency budget.** Measure and record p50/p95 for every on-stage action. Anything over 20 s needs
   either a narration plan or a pre-warmed result.

## 5. Documentation for the audience

- `docs/PUBLIC-README.md` — what you will publish after the series: architecture diagram, how to
  deploy it themselves, cost estimate, and the "what I would change for real production" section
  (VNet isolation, private endpoints, customer-managed keys, multi-region, real ITSM integration,
  data residency).
- Architecture diagram exported to SVG/PNG in `docs/assets/`.
- A single `docs/DEMO-NOTES.md` that a co-presenter could run the whole series from.

## 6. Final rehearsal

Run all four episodes back to back against the deployed system, timed, with `make demo-reset`
between them. Record total time per episode and flag anything over its slot. Fix or cut.

## Acceptance criteria

- [ ] `make verify-all` is green, end to end, from a clean shell.
- [ ] `scripts/verify_security.sh` has zero findings.
- [ ] All ten chaos drills behave as specified; none falls open.
- [ ] `make demo-reset` completes in under 60 s and is idempotent.
- [ ] Every runbook command is executed by `verify_demo_paths.sh`.
- [ ] Fallback assets exist for every live step.
- [ ] `PINNED STATE` recorded.
- [ ] Full four-episode rehearsal completed within the time slots.

## Report back

The verification table, the chaos drill results, the latency budget table, and the pinned state.
