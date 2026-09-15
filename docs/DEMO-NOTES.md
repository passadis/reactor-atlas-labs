# Demo notes

One entry per phase: what was built, what to show, the exact commands, and the fallback if it
fails live. Phases that produce no on-stage artefact still record their verification command.

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
