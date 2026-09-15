# Demo notes

One entry per phase: what was built, what to show, the exact commands, and the fallback if it
fails live. Phases that produce no on-stage artefact still record their verification command.

---

## Series-wide presentation model

Decided 2026-09-15. Full detail in `CLAUDE.md` section 8.

Four sessions delivered **sequentially** (Episode 1, then 2, then 3, then 4). Each: about 15
minutes of narrative, then demo mode scoped to that episode — **with the option to go end-to-end
at any moment, in any episode, including Episode 1.**

That option is the constraint that matters. It means:

- The whole system is live before Episode 1. No finishing Episode 3's plumbing between sessions.
- `ATLAS_DEMO_SCOPE` (`ep1 | ep2 | ep3 | ep4 | full`) is a **view filter, not a capability
  switch** — a dropdown and a `?scope=` parameter, applied per request. No restart, no redeploy.
  A restart in front of an audience is a failure mode.
- Every episode runbook gets an **Encore** section: the one command to go end-to-end, its measured
  wall-clock time, and the point in the slot after which there is no longer time to start it.
- `scripts/verify_demo_paths.sh` (Phase 14) asserts **five** paths, not four: each episode's scoped
  demo plus the full encore, each from a cold `make demo-reset`, none depending on another having
  run first.

Encore wall-clock time: **to be measured in Phase 14 rehearsal and recorded here.** If it exceeds
five minutes it gets split or cut.

### Pre-broadcast checklist (replaces the pack's)

- [ ] `make verify-all` green
- [ ] `scripts/verify_security.sh` zero findings
- [ ] Full four-episode dry run completed and timed, including all five demo paths
- [ ] `PINNED STATE` recorded below
- [ ] Fallback assets captured in `docs/assets/fallbacks/`
- [ ] **Fresh `az login` and `azd auth login`** — do this in the T-10 routine, every episode
- [ ] **Clean `git status`** — no half-applied chaos injection from rehearsal
- [ ] `scripts/restore_baseline.py` proven to restore
- [ ] Mobile hotspot, in case the venue network dies during Episode 3

The pack's "second machine or co-presenter" item is **deliberately dropped** — solo presenter,
system already deployed, deployment never demoed. See `CLAUDE.md` section 7.12. The items in bold
above are what actually carried that risk; a second laptop does not survive a dead venue network,
and the fallback assets do.

---

## Phase 00 — Bootstrap

**Built:** repository skeleton, uv workspace on Python 3.13, pinned agent stack, the reproducible
symbol probe, toolchain preflight, region check, and the Makefile surface.

**Not an on-stage phase.** Nothing here is demoed. It exists so that every later phase starts from a
known, machine-checkable baseline.

### What to show, if asked "how do you know the API is right?"

This is worth 30 seconds in Episode 1 if someone challenges the "verify, never recall" claim:

```bash
uv run python scripts/probe_symbols.py
```

It imports the installed packages and asserts, symbol by symbol, that the surface the build depends
on is really there — including asserting that stale names like `ChatAgent` are **absent**. It is
wired into `pytest`, so a dependency bump that moves a symbol fails on the next commit rather than
three phases later.

### Commands

```bash
make install         # uv sync --python 3.13 --group dev --group evals
make preflight       # toolchain pass/fail table
make check-region    # LOCATION=eastus2 by default
make probe           # symbol contract
make lint            # ruff check + ruff format --check + mypy strict
make test            # pytest
```

### Verified state at the end of Phase 00

| Check | Result |
|---|---|
| `make preflight` | 31 pass, 1 warn (Docker daemon not running), 0 fail |
| `make check-region` (eastus2) | 18 pass, 0 warn, 0 fail |
| `bash scripts/check_region.sh swedencentral` | 17 pass, **1 warn: AI Red Teaming unsupported** |
| `make probe` | all symbol and signature contracts hold |
| `make lint` | ruff clean, mypy strict clean on 6 source packages |
| `make test` | 9 passed |

The swedencentral run is kept deliberately as a negative control: it proves the region check
actually discriminates rather than always printing green.

### Fallbacks

| If this fails live | Do this |
|---|---|
| `make` not found | It is GNU Make 4.4.1 from `winget install --id ezwinports.make`. Git Bash does not ship it. Restart the shell after install for PATH. Every target is a one-line shell command you can paste directly. |
| Microsoft Learn MCP tools missing from the session | `bash scripts/mslearn.sh search "<query>"` hits the same server over raw JSON-RPC. Approve the server once in an interactive `claude` session to get first-class tools. |
| `uv sync` resolves different versions | Versions are pinned with `==` in the member `pyproject.toml` files. If a pin no longer resolves, read `docs/API-NOTES.md` section 2 before changing anything, then re-run `make probe`. |

### Decisions taken here that later phases inherit

1. **Region `eastus2`**, not `swedencentral` — AI red teaming is eastus2/northcentralus only.
2. **Python 3.13**, not 3.12 — hosted agents offer no `python_3_12` runtime.
3. **Two `azd` projects** — `./azure.yaml` (bicep, platform) and `./agents/azure.yaml`
   (microsoft.foundry, agent). `infra.provider` is one value per file.

All three are recorded with evidence in `CLAUDE.md` section 7 and `docs/API-NOTES.md`.

### Known warning, accepted

Docker daemon is not running. It is only needed for `azd ai agent run` (the Phase 05 local loop);
`azd deploy` builds remotely in ACR. Start Docker Desktop before Phase 05 if you want the local
loop, otherwise ignore.

---

## Phase 01 — Infrastructure

**Built:** the full Azure footprint. Subscription-scoped `infra/main.bicep` plus ten modules, the
three Entra app registrations, the postprovision hook and `verify_phase01.sh`.

**Not an on-stage phase**, but Episode 3 inspects several of these resources read-only, and the
Episode 4 trace waterfall depends on the wiring done here.

### Live endpoints

| Thing | Value |
|---|---|
| Resource group | `rg-atlas-atlas` (eastus2) |
| Foundry project | `https://aif-atlas-mkruniv7.services.ai.azure.com/api/projects/atlas-mkruniv7` |
| Model aliases | `gpt-main` (gpt-5.4), `gpt-judge` (gpt-5.4-mini), `embed-main` (text-embedding-3-large) |
| AI Search | `srch-atlas-mkruniv7` — **in eastus**, see CLAUDE.md 7.14 |
| APIM gateway | `apim-atlas-mkruniv7` |
| Web app | `ca-atlas-web-mkruniv7` (placeholder image until Phase 04) |

### Commands

```bash
make provision          # azd provision --no-prompt
make verify-phase01     # 31 checks against deployed resources
make app-registrations  # idempotent; safe to re-run
make verify-all         # every verify_phase*.sh in order
```

### Verified state at the end of Phase 01

| Check | Result |
|---|---|
| `azd provision` | green |
| Re-run `azd provision` | **green in 2m58s, no resource churn** (idempotency criterion) |
| `make verify-phase01` | **31 pass, 0 fail** |
| Secrets in `azd env get-values` | none |
| Secrets in container env vars | none — endpoints and client ids only |
| ARM role assignments | 77 |
| Cosmos data-plane role assignments | 6 |

### What took four attempts, and why it will not recur

Recorded in full in `docs/API-NOTES.md` section 8. Short version, because each one is a trap that
looks like something else:

1. **AI Search had no capacity in eastus2, for any SKU.** Not quota. Search moved to `eastus`.
2. **A Cognitive Services account accepts one mutating operation at a time.** The Foundry project
   raced its own model deployments. Now serialised.
3. **AcrPull must exist before a container app is created**, or the first revision hangs for 20
   minutes and reports `Operation expired`. The assignment moved out of `rbac.bicep` and into the
   module that creates the registry.
4. **A Bicep output must never share a name with a parameter-input env var.** `ATLAS_SEARCH_LOCATION`
   was both; azd wrote the output back over its own input as the display form `East US`, and the
   second provision could not validate. This is the one that breaks idempotency silently.

### Deviations from the pack made here

- **ACR is Standard, not Premium.** ATLAS uses no geo-replication, no private link, one region.
  Standard still includes 100 GiB. Not Basic, because `azd deploy` builds through ACR Tasks and the
  sources conflict on whether Basic permits them. SKU changes apply in place.
- **Search is in `eastus`** while everything else is in `eastus2`.

### Fallbacks

| If this fails live | Do this |
|---|---|
| A resource looks wrong on stage | `make verify-phase01` prints 31 named checks in about 40 seconds. Do not debug in the portal in front of an audience. |
| A 403 right after any provision | Role propagation, up to 5 minutes. Wait, re-run, then investigate. `postprovision.sh` already polls for Cosmos role propagation. |
| `az role assignment list` shows 0 | Azure CLI 2.90.0 defect, not a real problem. Use the ARM REST call in `verify_phase01.sh`. |
