# Project ATLAS — Microsoft Reactor Lab Pack

**One build. Four show days.**

This pack is the complete instruction set for Claude Code to build and deploy a single,
production-shaped agentic solution on Azure — *Project ATLAS*, the Contoso Cloud Operations
incident copilot. Everything is deployed once, up front. The four Reactor episodes are then
*views* onto the same running system, not four separate builds.

| Episode | Title | What you show from the live system |
|---|---|---|
| 1 | Beyond Chatbots | The core agent: instructions, typed tools, structured output, conversation state, streaming. Foundry **prompt agent** vs **hosted agent** vs **ACA custom app**. |
| 2 | Reliable Multi-Agent Systems | The orchestrator: sequential → concurrent → handoff, typed workflow state, Cosmos checkpoints, kill/resume, human approval gate, deterministic policy engine. |
| 3 | Agents That Take Action | The governed integration layer: APIM AI gateway, OpenAPI + MCP tools, Logic Apps approval, Azure Functions, Service Bus async remediation, managed identity everywhere. |
| 4 | Production-Grade Agents | Evaluation dataset, cloud evals, distributed traces, the deliberate regression, the quality gate, monitoring, rollout/rollback. |

---

## How to use this pack

### 1. Read these first (you, not Claude Code)

- `reference/PREREQUISITES.md` — subscription, quota, roles, tooling. **Do this before anything else.**
- `ARCHITECTURE.md` — what gets deployed and why.
- `DATA-MODEL.md` — the synthetic dataset.

### 2. Seed the repo

Create an empty folder, `cd` into it, start Claude Code, and paste
`prompts/PHASE-00-bootstrap.md`. That phase copies `CLAUDE.md` and the reference docs into the
repo so Claude Code keeps its context across sessions.

```bash
mkdir atlas && cd atlas
git init
# copy this whole lab pack into ./_labpack
claude
```

Then paste each phase prompt **in order**. One phase per Claude Code turn-set. Do not skip ahead —
phases declare their own dependencies and acceptance criteria.

### 3. Phase order

| # | Phase | Est. wall time | Gate |
|---|---|---|---|
| 00 | Bootstrap repo + CLAUDE.md | 10 min | `_labpack` copied, CLAUDE.md in root |
| 01 | Infrastructure as code (`azd` + Bicep) | 45 min | `azd provision` green |
| 02 | Synthetic data + seeding | 30 min | Cosmos + AI Search populated |
| 03 | Agent core (Ep1) | 60 min | `pytest tests/agent` green, agent answers locally |
| 04 | Streaming web app on ACA (Ep1) | 45 min | Public URL streams a triage |
| 05 | Foundry hosted agent (Ep1) | 30 min | `azd ai agent invoke` returns |
| 06 | Multi-agent workflow (Ep2) | 90 min | All 3 orchestration patterns run |
| 07 | Typed state, checkpoints, HITL (Ep2) | 60 min | Kill/resume demo works |
| 08 | Functions: business API + MCP server (Ep3) | 60 min | Tools callable, OpenAPI valid |
| 09 | APIM AI gateway (Ep3) | 60 min | Agent reaches backends only via APIM |
| 10 | Logic Apps + Service Bus (Ep3) | 60 min | End-to-end remediation with approval |
| 11 | Observability + distributed tracing (Ep4) | 45 min | One trace spans all 6 hops |
| 12 | Evaluation dataset + cloud evals (Ep4) | 60 min | Baseline scorecard produced |
| 13 | CI/CD quality gate + regression kit (Ep4) | 45 min | Gate blocks the bad build |
| 14 | Hardening, chaos, and full verification | 60 min | `make verify-all` green |

Budget a full day with Claude Code working, plus Azure provisioning time.

### 4. Rehearse

`runbook/EPISODE-1.md` … `EPISODE-4.md` are your on-stage scripts: exact commands, exact URLs,
exact talking beats, timings, and a fallback for every live step.
`runbook/FAILURE-INJECTIONS.md` holds the Episode 4 regression kit — the scripts that break the
agent on purpose and put it back.

---

## Non-negotiables baked into this pack

1. **Everything is deployed before Episode 1 airs.** No `azd up` on stage.
2. **No API keys in code or config.** Managed identity + Entra ID, end to end. The one exception
   (an APIM subscription key) lives in Key Vault and is referenced, never printed.
3. **Every demo step has a non-live fallback** — a recorded trace, a cached response, a screenshot.
4. **Claude Code must verify APIs against the installed package and MS Learn before writing code.**
   The Foundry and Agent Framework surfaces moved a lot in 2025–2026; `CLAUDE.md` enforces this.
5. **Synthetic data only.** No customer data, no real tenant secrets, no real ticketing system.
6. **Cost is not optimised.** SKUs are chosen for provisioning speed and demo reliability.
   `reference/TEARDOWN.md` tells you how to stop paying afterwards.

---

## Pack contents

```
_labpack/
├── README.md                     ← you are here
├── CLAUDE.md                     ← copied to repo root by Phase 00
├── ARCHITECTURE.md
├── DATA-MODEL.md
├── prompts/
│   ├── PHASE-00-bootstrap.md
│   ├── PHASE-01-infrastructure.md
│   ├── PHASE-02-synthetic-data.md
│   ├── PHASE-03-agent-core.md
│   ├── PHASE-04-web-app.md
│   ├── PHASE-05-hosted-agent.md
│   ├── PHASE-06-multi-agent-workflow.md
│   ├── PHASE-07-state-checkpoints-hitl.md
│   ├── PHASE-08-functions-tools.md
│   ├── PHASE-09-apim-gateway.md
│   ├── PHASE-10-logicapps-servicebus.md
│   ├── PHASE-11-observability.md
│   ├── PHASE-12-evaluation.md
│   ├── PHASE-13-cicd-quality-gate.md
│   └── PHASE-14-hardening-verification.md
├── runbook/
│   ├── EPISODE-1.md … EPISODE-4.md
│   └── FAILURE-INJECTIONS.md
└── reference/
    ├── PREREQUISITES.md
    ├── RBAC-MATRIX.md
    ├── VERIFICATION.md
    ├── TROUBLESHOOTING.md
    └── TEARDOWN.md
```
