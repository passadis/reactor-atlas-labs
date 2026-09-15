# Episode 1 — Beyond Chatbots: Build Your First Production Agent

**Slot:** 60 min · **Live demo:** ~28 min · **Everything is already deployed.**

## T-10 minutes

```bash
make demo-reset
bash scripts/warm.sh
bash scripts/verify_smoke.sh      # must be green before you go on air
```

Open and arrange: Foundry portal (project home), the ATLAS web app, VS Code on the repo, a terminal.

---

## Beat 1 — The four shapes (8 min)

The argument: *"chatbot", "assistant", "agent" and "agent workflow" are four different things with
four different failure modes.*

| Step | Command / action | Say |
|---|---|---|
| 1 | `uv run python scripts/demo_raw_model.py` | A model call. Stateless, toolless, identityless. It will confidently invent an incident number. Show it doing exactly that. |
| 2 | Foundry portal → Agents → `atlas-triage-prompt` → playground. Same prompt. | A **prompt agent**. Instructions + model + a toolbox, no code, no infrastructure. Foundry runs it. |
| 3 | `azd ai agent invoke "<canonical incident>"` | A **hosted agent**. My code, my framework, containerised, managed endpoint, its own Entra identity, versioned. |
| 4 | ATLAS web app, mode switch → `This app` | A **custom agent application**. My HTTP surface, my UI, my orchestration. Same agent definition as (3). |

Land the point: **one agent definition, three hosting models.** Open
`src/atlas_hosted_agent/agent.py` and show it importing `build_incident_agent` — nine lines.

> Fallback: `docs/assets/fallbacks/ep1-four-shapes.cast` + screenshots.

---

## Beat 2 — Instructions are a contract (5 min)

| Step | Action | Say |
|---|---|---|
| 1 | Open `instructions/atlas_incident_agent.v0.md` — three vague sentences. | This is what most prototypes ship. |
| 2 | Run the canonical incident with `ATLAS_INSTRUCTIONS_VERSION=v0`. | Watch it skip retrieval and guess a cause. |
| 3 | Open `v1.md` — walk the seven sections: role, procedure, tool policy, grounding, refusals, output contract, escalation. | Each section exists because something went wrong without it. |
| 4 | Same prompt, `v1`. | Cited, structured, honest about uncertainty. |
| 5 | Tease: `evals/results/v0-vs-v1.md` | We will put numbers on this difference in Episode 4. |

---

## Beat 3 — Tools are an API, not a hint (6 min)

| Step | Action | Say |
|---|---|---|
| 1 | Open `src/atlas_agent/tools.py`, show `compute_blast_radius`. | Pydantic in, Pydantic out, a docstring written for the model including *when not to use it*, an explicit timeout, a typed failure. |
| 2 | Web app activity panel during a live run. | Every call, every duration, every outcome. |
| 3 | Break it: `scripts/chaos/ops_api_down.sh --apply`, re-run. | The tool returns a typed failure. The agent says "I could not reach the ticketing system" and continues with what it has. It does not hallucinate a ticket number. |
| 4 | `scripts/chaos/ops_api_down.sh --revert` | |

This is the "failure boundary" beat from the abstract. Do not skip it — it is the most
production-relevant thing in the episode.

---

## Beat 4 — State and streaming (5 min)

| Step | Action | Say |
|---|---|---|
| 1 | Turn 1: the canonical incident. Turn 2: *"what changed on it in the last hour?"* | "It" resolves. Conversation state is real. |
| 2 | Show the Cosmos `sessions` document. | State lives outside the process. Kill the container, the conversation survives — and in Episode 2 so does the *workflow*. |
| 3 | Point at the token stream and the activity panel arriving together. | Streaming is not just tokens; stream your tool lifecycle too or your users stare at a spinner. |

---

## Beat 5 — Structured output (4 min)

| Step | Action | Say |
|---|---|---|
| 1 | Toggle "show the contract" in the UI. | The prose is for the human. The JSON is for the next system. |
| 2 | Show `IncidentTriage` in `schemas.py` and the validate-retry-typed-failure path. | Never pass unvalidated model output to another component. This is the seam where Episode 2's workflow plugs in. |

---

## Close (2 min)

- What we have: an agent with a contract, typed tools, durable state, streaming, structured output,
  three hosting options, and an identity.
- What we do not have yet: more than one specialist, recoverable execution, human approval, any
  ability to *act*, and any evidence that it is good.
- Next week: Episode 2 turns this into a coordinated workflow that can be interrupted and resumed.

## Timing guard

If you are over at Beat 3, cut Beat 4 step 2 and Beat 5 step 2. Never cut Beat 3.
