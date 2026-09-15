# RBAC Matrix

Every assignment below is created by `infra/modules/rbac.bicep` with deterministic GUIDs.
`scripts/verify_security.sh` diffs the deployed state against this table and **fails on any extra
assignment**. Keep them in sync.

Role assignments take up to 5 minutes to propagate. A 403 immediately after provisioning is usually
propagation, not a missing role — wait, re-check, then investigate.

---

## Managed identities

| Identity | Attached to |
|---|---|
| `id-atlas-web` | `ca-atlas-web` |
| `id-atlas-orchestrator` | `ca-atlas-orchestrator` |
| `id-atlas-worker` | `ca-atlas-worker` |
| `id-atlas-func` | `func-atlas-api`, `func-atlas-mcp` |
| `id-atlas-logic` | `logic-atlas` |
| APIM system-assigned | `apim-atlas` |
| Foundry account system-assigned | Foundry account |
| Hosted agent Entra agent identity | created by Foundry on deploy |

---

## Assignments

### Foundry project / account

| Principal | Role | Scope |
|---|---|---|
| `id-atlas-web` | Foundry User (formerly Azure AI User) | project |
| `id-atlas-orchestrator` | Foundry User | project |
| `id-atlas-func` | Foundry User | project (embeddings for search-time queries) |
| `id-atlas-logic` | Foundry User | project |
| APIM system MI | Cognitive Services OpenAI User | Foundry account |
| Your user | Foundry Project Manager | project (required for hosted agent deploy) |
| Your user | Foundry Account Owner | subscription or account (creation) |

### Cosmos DB — **data-plane roles, not ARM RBAC**

Created with `Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments`. ARM `Contributor` gives you
**no** data access; this is the single most common cause of a 403 in this build.

| Principal | Role | Scope |
|---|---|---|
| `id-atlas-orchestrator` | Cosmos DB Built-in Data Contributor | account |
| `id-atlas-worker` | Cosmos DB Built-in Data Contributor | account |
| `id-atlas-func` | Cosmos DB Built-in Data Contributor | account |
| `id-atlas-web` | Cosmos DB Built-in Data Contributor | account |
| `id-atlas-logic` | Cosmos DB Built-in Data Contributor | account |
| Your user | Cosmos DB Built-in Data Contributor | account (seeding, inspection) |

Also assign ARM `Cosmos DB Account Reader Role` to identities that need to read account metadata.

### Azure AI Search

| Principal | Role | Scope |
|---|---|---|
| `id-atlas-func` | Search Index Data Reader | service |
| `id-atlas-orchestrator` | Search Index Data Reader | service |
| Your user | Search Service Contributor + Search Index Data Contributor | service (index creation, seeding) |
| Foundry account MI | Search Index Data Reader | service (project connection) |

### Service Bus

| Principal | Role | Scope |
|---|---|---|
| `id-atlas-orchestrator` | Azure Service Bus Data Sender | namespace |
| `id-atlas-func` | Azure Service Bus Data Sender | queue `remediation-commands` |
| `id-atlas-worker` | Azure Service Bus Data Receiver | queue `remediation-commands` |
| `id-atlas-worker` | Azure Service Bus Data Sender | queue `remediation-results`, topic `remediation-events` |
| `id-atlas-web` | Azure Service Bus Data Receiver | subscription `web-progress` |
| `id-atlas-orchestrator` | Azure Service Bus Data Receiver | queue `remediation-results` |
| Your user | Azure Service Bus Data Owner | namespace (DLQ inspection and replay) |

### Storage

| Principal | Role | Scope |
|---|---|---|
| `id-atlas-func` | Storage Blob Data Owner | Functions host storage |
| `id-atlas-func` | Storage Queue Data Contributor | host storage — **required by the Functions MCP extension** |
| `id-atlas-func` | Storage Queue Data Message Processor | host storage — **required by the Functions MCP extension** |
| `id-atlas-func` | Storage Table Data Contributor | host storage |
| `id-atlas-logic` | Storage Blob Data Owner, Queue Data Contributor, Table Data Contributor | Logic Apps storage |
| `id-atlas-orchestrator` | Storage Blob Data Contributor | `eval-artifacts` container |

### Container Registry

| Principal | Role | Scope |
|---|---|---|
| `id-atlas-web`, `id-atlas-orchestrator`, `id-atlas-worker` | AcrPull | ACR |
| CI service principal (OIDC) | AcrPush | ACR |

### Key Vault (RBAC mode)

| Principal | Role | Scope |
|---|---|---|
| `id-atlas-orchestrator` | Key Vault Secrets User | vault |
| `id-atlas-logic` | Key Vault Secrets User | vault (approval-token signing key) |
| `id-atlas-worker` | Key Vault Secrets User | vault (token verification) |
| APIM system MI | Key Vault Secrets User | vault (named values) |
| Your user | Key Vault Secrets Officer | vault |

### Monitoring

| Principal | Role | Scope |
|---|---|---|
| All five UAMIs | Monitoring Metrics Publisher | Application Insights |
| Your user | Log Analytics Reader | workspace (required to view traces in Foundry) |
| CI service principal | Log Analytics Reader | workspace (trace assertions in the gate) |

### Container Apps / deployment

| Principal | Role | Scope |
|---|---|---|
| CI service principal (OIDC) | Contributor | resource group |
| CI service principal | Foundry Project Manager | project (hosted agent deploy) |
| CI service principal | Cosmos DB Built-in Data Contributor | account (eval result writes) |

---

## Entra application registrations

| App | Purpose | Exposed scope / audience | App roles |
|---|---|---|---|
| `atlas-web` | Container Apps Easy Auth for the UI | — | `Atlas.Engineer`, `Atlas.Approver`, `Atlas.ReadOnly` |
| `atlas-gateway` | The audience APIM validates on inbound calls | `api://atlas-gateway` | `Atlas.Agent` |
| `atlas-ops-api` | The audience the Function app requires | `api://atlas-ops-api` | — (APIM MI only) |

Assign `Atlas.Agent` to `id-atlas-orchestrator` and to the hosted agent's Entra agent identity.
Assign `Atlas.Approver` to your demo approver account and **not** to the `readonly` demo persona —
Episode 4's `unauthorized` eval category depends on that being true.

---

## Explicitly forbidden

`verify_security.sh` fails if any of these exist:

- `Owner` or `Contributor` on any of the five UAMIs
- Any `*-key`-based access still enabled where the resource supports disabling local auth
- Any role assignment to `AllPrincipals`, `Everyone`, or a broad group not in this table
- Any assignment at subscription scope for a workload identity
