# PHASE 05 — Foundry hosted agent (Episode 1)

> Depends on: Phase 03 (and Phase 04 for the mode switch).

---

Deploy the **same** `atlas_agent` definition as a Foundry **hosted agent**, so Episode 1 can put
"Foundry runs it" and "you run it" side by side with identical behaviour.

## Before you write code

Query MS Learn MCP for the current hosted-agent guidance. Confirm:
- the hosting library and entrypoint for Python (the agent-server package that wraps an Agent
  Framework agent and serves the Foundry protocol),
- whether you are using the **Responses** or **Invocations** protocol (use **Responses**),
- the `azd ai agent` command surface (`init`, `run`, `invoke`, `show`, `monitor`, `deploy`),
- whether source-zip deploy or container deploy is right for you,
- the exact RBAC role required to deploy (project-scoped) and assign it to your user.

Record all of it in `docs/API-NOTES.md`.

## Tasks

1. `src/atlas_hosted_agent/agent.py`:
   - Import `build_incident_agent` from `atlas_agent`. **Do not redefine the agent.**
   - Wrap it with the Foundry agent-server adapter and `run()` it.
   - Read config from the environment variables Foundry injects at runtime
     (`FOUNDRY_PROJECT_ENDPOINT`, model deployment name, `APPLICATIONINSIGHTS_CONNECTION_STRING`)
     with local fallbacks for `azd ai agent run`.
   - Tools must work under the agent's **own Entra agent identity**, not yours. That means the
     hosted agent reaches data only through the ops API/Toolbox path once Phase 09 lands; for now,
     give it the same repositories but make the credential injectable so the swap is one line.

2. `azure.yaml` / agent manifest: add the hosted agent service with a stable agent name
   `atlas-incident-agent`, remote build, and pinned resources (CPU/memory). Keep deploy mode
   consistent (source zip is simpler; container if you need custom system packages).

3. Deploy:
   ```bash
   azd ai agent init -m <your manifest or azure.yaml>   # once
   azd ai agent run                                      # local loop, http://localhost:8088
   azd ai agent invoke --local "Checkout is timing out for EU customers since 14:05"
   azd deploy                                            # creates a new agent version
   azd ai agent show --output table
   azd ai agent invoke "Checkout is timing out for EU customers since 14:05"
   ```

4. **Versioning discipline** (this pays off in Episode 4): every deploy creates a version. Record
   the version number in `docs/DEMO-NOTES.md` and pin the demo to a known-good version. Write
   `scripts/pin_agent_version.sh <version>` and `scripts/rollback_agent.sh` using the SDK/REST
   surface you verified.

5. **Wire the web app**: implement the `Hosted agent` and `Prompt agent` modes in the mode switch by
   calling their endpoints. Same UI, same activity panel, different backend.

6. **Toolbox (preview) hook-up**: create a Foundry **Toolbox** `atlas-tools` that will later hold the
   APIM-fronted MCP server and the Logic Apps tool. For now put the knowledge search behind it (or
   a single placeholder tool) and attach it to the prompt agent, so Episode 1 can say "tools are
   curated once and shared" and Episode 3 can extend it without re-architecting. Script it —
   `scripts/upsert_toolbox.py` — and make it idempotent.

7. Enable **agent tracing**: confirm the project is connected to Application Insights (Phase 01) and
   that a hosted-agent invocation produces server-side traces visible under the project's traces
   view. If client-side instrumentation is also needed for your version, add it.

## Verification — `scripts/verify_phase05.sh`

1. `azd ai agent show` reports an `active` version.
2. `azd ai agent invoke "<canonical incident>"` returns a valid `IncidentTriage` shape.
3. The hosted agent has a dedicated Entra agent identity (report its object id).
4. The same prompt through the web app's `Hosted agent` mode and `This app` mode produce
   structurally equivalent output (same severity, overlapping services). Assert it in a test with a
   tolerance for prose differences.
5. Traces for the hosted-agent invocation appear in Application Insights / the project's trace view
   within 5 minutes.
6. `scripts/rollback_agent.sh` moves the active version back and `azd ai agent show` confirms it,
   then re-pin forward.

## Acceptance criteria

- [ ] One agent definition, three live surfaces: prompt agent, hosted agent, ACA app.
- [ ] Hosted agent deploys from CI-able commands, not portal clicks.
- [ ] Rollback is a single scripted command and is proven to work.
- [ ] `docs/DEMO-NOTES.md` records the pinned version numbers for every surface.

## Report back

Hosted agent endpoint, active version, agent identity object id, and a side-by-side of the three
surfaces' output for the canonical incident.
