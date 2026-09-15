# PHASE 08 — Azure Functions: business API and MCP server (Episode 3)

> Depends on: Phase 07.
> Episode 3 topics: Azure Functions, OpenAPI tools, MCP servers.

---

Move the agent's access to data and actions out of in-process repositories and behind real,
independently deployable services. Phase 09 puts APIM in front of them; nothing should talk to these
function apps directly once that lands.

## A. `functions/ops_api` — the protected business API

Python v2 programming model, Flex Consumption, HTTP triggers, `authLevel=ANONYMOUS` **with Entra
authentication enforced at the platform level** (App Service authentication, requiring a token whose
audience is `api://atlas-ops-api`). APIM will present a managed-identity token for that audience.

### Operations

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/services/{service_id}` | CMDB record |
| `GET` | `/api/services/{service_id}/health` | Telemetry summary + SLO burn |
| `GET` | `/api/services/{service_id}/blast-radius?depth=` | Deterministic graph walk |
| `GET` | `/api/incidents/{incident_id}` | Incident record |
| `GET` | `/api/changes?service_id=&since_minutes=` | Recent changes |
| `POST` | `/api/tickets` | **Create a ticket** — idempotent on `Idempotency-Key` header |
| `GET` | `/api/tickets/{ticket_id}` | Ticket state |
| `POST` | `/api/remediation/commands` | **Submit a long-running remediation** — enqueues to Service Bus, returns `202` + `operation_id` + `Location` |
| `GET` | `/api/remediation/operations/{operation_id}` | Poll status |
| `GET` | `/api/health` | |

### Requirements

- **OpenAPI 3.0 spec is authored by hand** at `apim/openapi/atlas-ops-api.yaml`, not generated as an
  afterthought. It is the tool contract the model sees: every operation needs `operationId`,
  `summary`, `description` written for an LLM audience, typed schemas, examples, and documented
  error responses. Validate it in CI (`openapi-spec-validator`).
- A test asserts the deployed routes match the spec exactly (path, method, status codes).
- Request validation against the schema at the function boundary too — never trust APIM alone.
- `POST /api/tickets` and `POST /api/remediation/commands` are idempotent: same `Idempotency-Key`
  returns the original result with `200` and a `Idempotency-Replayed: true` header.
- Structured logging with the incoming `traceparent`; every response echoes `x-atlas-correlation-id`.
- All data access via the Phase 02 repositories with the function app's UAMI.
- Explicit, tight timeouts; typed error envelopes `{code, message, correlation_id}` — never a stack
  trace, never an internal detail.

## B. `functions/ops_mcp` — the MCP tool server

Use the **Azure Functions MCP extension** (tool triggers), Python v2 model. Verify the current
decorator surface and required `azure-functions` version via MS Learn MCP and the installed package
before writing code — this API is newer than most of your training data.

### Tools to expose

| Tool | Description (what the model reads) | Backed by |
|---|---|---|
| `search_ops_knowledge` | Search Contoso runbooks, past incidents and postmortems. Use for "how do we fix", "has this happened before". Returns cited chunks with `doc_id` and `last_reviewed`. | AI Search |
| `get_service_topology` | Return a service's dependencies and dependents up to a depth. Use before assessing impact. Deterministic; never estimate this yourself. | graph walk |
| `get_oncall` | Who is on call for a service right now, and their escalation chain. | people/rotations |
| `open_ticket` | Create an operations ticket. Requires `case_id`, `severity`, `summary`. Returns `ticket_id`. Idempotent on `case_id`. | ops API |
| `submit_remediation` | Submit an approved remediation plan for asynchronous execution. **Requires a valid `approval_token`.** Returns an `operation_id`; poll `get_remediation_status`. | ops API → Service Bus |
| `get_remediation_status` | Poll a submitted remediation. | ops API |

Notes:
- Tool descriptions are prompt engineering. Write them as carefully as the agent instructions, and
  include negative guidance ("do not use this to look up an incident by id — use `get_incident`").
- `submit_remediation` validates the `approval_token` server side against the Cosmos `approvals`
  record (signature, case, plan_id, expiry, approver role). **The token is minted by the
  orchestrator after a human approves, and is single use.** If the model fabricates a token, the
  call fails and that failure is an eval case in Episode 4.
- Note the storage **queue** role requirements for the MCP extension (Storage Queue Data Contributor
  and Storage Queue Data Message Processor on the host storage account) — confirm these are in the
  RBAC matrix and applied.
- Record the MCP endpoint path and the `mcp_extension` system key handling in `docs/API-NOTES.md`.
  Do not put that key in source; APIM will hold it as a Key Vault-backed named value in Phase 09.

## C. Wire the agent tools to HTTP

Refactor `src/atlas_agent/tools.py` so each tool calls the ops API over HTTP through a single
`OpsApiClient` in `atlas_common` (base URL from config, credential injected, `traceparent`
propagated, retries with jitter on 5xx/429 only, circuit breaker after 5 consecutive failures).
In Phase 09 the base URL becomes the APIM gateway and nothing else changes — prove that by making
the base URL the only thing that differs.

Keep the direct-repository implementation available behind a flag for local development and tests.

## Verification — `scripts/verify_phase08.sh`

1. Both function apps deploy and report `Running`; `/api/health` returns 200.
2. `openapi-spec-validator apim/openapi/atlas-ops-api.yaml` passes; route-parity test passes.
3. Unauthenticated call to `func-atlas-api` is rejected (401), authenticated call succeeds.
4. Idempotency: two `POST /api/tickets` with the same key produce one ticket and the replay header.
5. MCP server responds to `tools/list` over Streamable HTTP with all six tools and their schemas.
6. `submit_remediation` with a forged/expired `approval_token` returns a typed 403 and writes an
   audit record.
7. The orchestrator running against the HTTP-backed tools completes the canonical case with the
   same result as Phase 07 (fixture comparison).

## Acceptance criteria

- [ ] The OpenAPI spec is hand-written, LLM-readable, validated, and matches the deployment.
- [ ] Both write operations are idempotent and proven by test.
- [ ] MCP tools enumerate correctly and every description includes when *not* to use the tool.
- [ ] Approval tokens cannot be forged; the failure path is typed and audited.
- [ ] Switching the agent from in-process repositories to HTTP changed exactly one config value.

## Report back

`tools/list` output, the OpenAPI operation list, and evidence of the idempotency replay.
