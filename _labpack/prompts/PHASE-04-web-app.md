# PHASE 04 — Streaming web application on Azure Container Apps (Episode 1)

> Depends on: Phase 03.

---

Build `src/atlas_web`: the FastAPI application that is the face of ATLAS for all four episodes.
It must look credible on a projector — dark-friendly, large type, no clutter.

## Surface

| Route | Purpose |
|---|---|
| `GET /` | Chat console. Left: conversation. Right: live "agent activity" panel. |
| `POST /api/chat` | Starts a turn; returns a `turn_id`. |
| `GET /api/chat/{turn_id}/stream` | **SSE**: token deltas, tool-call started/finished events, structured-output-ready event, error events. |
| `GET /api/cases/{case_id}` | Case detail: typed state, timeline, citations. |
| `GET /approvals` | Approval console (populated in Phase 07/10). |
| `POST /api/approvals/{case_id}` | Approve / reject with a reason. Entra-protected, entitlement-checked server side. |
| `GET /api/health` | Liveness + dependency probe. |
| `GET /api/trace/{turn_id}` | Returns the `traceparent` and a deep link to the Application Insights transaction. |

## Requirements

1. **Streaming**: server-sent events via `sse-starlette`. Stream the model deltas *and* structured
   lifecycle events so the right-hand panel can show `tool:lookup_service started → 142 ms → ok`.
   That panel is what makes the agent legible to an audience — invest in it.
2. **Backpressure and cancellation**: closing the browser tab must cancel the underlying run.
3. **Auth**: Microsoft Entra ID via Container Apps built-in authentication (Easy Auth) with an app
   registration; app roles `Atlas.Engineer`, `Atlas.Approver`, `Atlas.ReadOnly`. Map the signed-in
   user to a `Person` from the seed data via a claim (email) and fall back to a demo identity if
   `ATLAS_DEMO_AUTH_BYPASS=true` — but that flag must be **off** in the deployed app and the app
   must log loudly if it is on.
4. **Entitlements are server-side.** The UI hides buttons; the API refuses the call. Both.
5. **Telemetry**: create the root span, put the `traceparent` in the SSE stream, propagate it on
   every downstream call, and record custom dimensions `case_id`, `session_id`, `user_role`,
   `instructions_version`, `agent_version`.
6. **Degraded modes**: if the orchestrator is unreachable, fall back to the single-agent path and
   show a visible banner. If Foundry is throttling (429), show a retry countdown, not a stack trace.
   Never render an exception to the page.
7. **Containerisation**: distroless or slim Python 3.12 base, non-root user, `HEALTHCHECK`, no
   secrets in the image, `PORT=8000`. Build remotely via ACR (`remoteBuild: true`).

## Episode-1 affordances to build now

- A **mode switch** in the header: `Model call` | `Prompt agent` | `Hosted agent` | `This app`.
  Selecting a mode routes the same user message to that shape and shows the same panel. This single
  control carries most of Episode 1's argument. Hosted-agent and prompt-agent modes will light up
  after Phase 05 — stub them with a clear "not yet deployed" state until then.
- A **"show the contract"** toggle that renders the raw structured output JSON next to the prose.

## Verification — `scripts/verify_phase04.sh`

1. `azd deploy web` succeeds; the ACA app reports `Running` with the new image and min 2 replicas.
2. `curl` the public URL `/api/health` → 200 with all dependencies `ok`.
3. A scripted SSE client (`scripts/sse_probe.py`) sends the canonical incident and asserts:
   first token < 3 s, at least 4 tool lifecycle events, a valid `IncidentTriage` at the end.
4. Entra auth actually challenges an unauthenticated request (302 to login), and
   `ATLAS_DEMO_AUTH_BYPASS` is unset in the deployed revision.
5. Cancelling mid-stream terminates the server-side run (assert via logs).
6. Two consecutive turns in one session resolve coreference.

## Acceptance criteria

- [ ] Public URL streams a full triage in under 15 seconds, warm.
- [ ] The activity panel shows every tool call with duration and outcome.
- [ ] No unhandled exception can reach the browser; the error path is tested.
- [ ] `traceparent` from the browser turn appears in Application Insights within 2 minutes.
- [ ] Screenshots saved to `docs/assets/` for the fallback deck.

## Report back

The public URL, cold and warm time-to-first-token, and a screenshot of the activity panel.
