# Agent changelog

Instructions, tool descriptions and hosted-agent versions are versioned artefacts. Every entry
names the evaluation that measured it (evals/reports/).

## 2026-10-05

- **Instructions v2** (active). Four rules the Phase 12 evaluation found missing: care first on a
  self-harm disclosure; refusals in neutral words; low confidence and a human on vague reports;
  check the service a flag or configuration change lives in. Measured: deterministic checks 94 %
  (v1) to 99 %, gate PASS. `evals/reports/scorecard-v1-v2.md`, `instructions-comparison.md`.
- **Hosted agent version 7** (pinned, "good"): ATLAS_INSTRUCTIONS_PIN support; follows the shared
  pointer.
- **Hosted agent version 8** ("bad", not pinned): identical code deployed with
  `ATLAS_INSTRUCTIONS_PIN=v0` - the deliberate regression the quality gate must block.

## 2026-10-01

- Planner instructions: graded risk rubric; gate rule 5 accepts reversible non-production changes
  (docs/API-NOTES.md 19.2).

## 2026-09-19

- Instructions v1: the operating procedure, tool policy, grounding, refusals, escalation and the
  severity rubric. Hosted agent versions 3-6 over Phases 05-11.
