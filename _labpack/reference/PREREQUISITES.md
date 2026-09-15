# Prerequisites

Do all of this **before** you start Phase 00. Several items have lead times measured in days.

## Azure subscription

| Requirement | Detail |
|---|---|
| Role | **Owner** on the subscription, or Contributor **plus** User Access Administrator. You are creating role assignments; Contributor alone will fail Phase 01. |
| Tenant | You must be able to create an **app registration** (for the web app's Entra auth and the API audiences). If your tenant blocks this, get an admin to pre-create three registrations: `atlas-web`, `atlas-gateway`, `atlas-ops-api`. |
| Foundry role | **Foundry Account Owner** at subscription scope to create the account/project, then **Foundry Project Manager** at project scope (required to deploy hosted agents). Note these roles were previously named Azure AI Account Owner / Azure AI Project Manager. |
| Policy | Confirm no Azure Policy blocks public ingress on Container Apps, public Function apps, or resource creation in your chosen region. Check before you provision, not after. |

## Quota — check this first, it is the most common blocker

```bash
# Model quota in the target region
az cognitiveservices usage list -l swedencentral -o table

# Model + SKU availability
az cognitiveservices model list -l swedencentral -o table
```

You need, in one region:

| Need | Amount |
|---|---|
| Chat model, GlobalStandard | ≥ 300K TPM total (200K for `gpt-main`, 100K for `gpt-judge`) |
| `text-embedding-3-large`, Standard | ≥ 120K TPM |
| Regional vCPU quota | ≥ 20 vCPU for the Container Apps dedicated workload profile |
| APIM StandardV2 | available in region |
| Service Bus Premium | available in region |
| Functions Flex Consumption | available in region |

If chat model quota is short, request an increase **at least 5 business days before Episode 1**.
Evaluation runs and red teaming consume a lot of judge tokens — do not skimp on `gpt-judge`.

## Region

Default: `swedencentral`. Fallback: `eastus2`.

Verify before committing — some capabilities are region-limited:

- Foundry Agent Service (hosted agents) availability
- Foundry evaluation and the **AI Red Teaming Agent** supported-region list
- Logic Apps Foundry agent integration (preview — check current regions)
- APIM MCP server support in your chosen tier

`scripts/check_region.sh <location>` (built in Phase 00) automates this. Run it before Phase 01.

## Local tooling

| Tool | Minimum | Install |
|---|---|---|
| Azure CLI | 2.77 | `curl -sL https://aka.ms/InstallAzureCLIDeb \| sudo bash` |
| Azure Developer CLI | 1.23 | `curl -fsSL https://aka.ms/install-azd.sh \| bash` |
| `azd` AI agent extension | current | `azd ext install azure.ai.agents` |
| Azure Functions Core Tools | 4.0.7030 | needed for the Functions MCP extension |
| Python | 3.12.x | |
| uv | 0.5+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Docker | any recent | Optional — `azd` builds remotely in ACR by default. Required for `azd ai agent run` local container loop. |
| GitHub CLI | 2.60+ | For the Phase 13 workflows |
| Node.js | 20+ | Only if you use the MCP Inspector for manual MCP testing |

Azure CLI extensions: `containerapp`, `application-insights`, `logic` (as needed). Register the
resource providers: `Microsoft.CognitiveServices`, `Microsoft.App`, `Microsoft.ApiManagement`,
`Microsoft.ServiceBus`, `Microsoft.DocumentDB`, `Microsoft.Search`, `Microsoft.Web`,
`Microsoft.Logic`, `Microsoft.OperationalInsights`, `Microsoft.Insights`, `Microsoft.KeyVault`,
`Microsoft.ContainerRegistry`.

## GitHub

- A repository you control (private is fine).
- OIDC federated credentials configured for two environments (`staging`, `production`) so CI needs
  no client secrets. Set up the app registration and federated credential subjects before Phase 13.
- Repository variables for subscription id, tenant id, and the two `azd` environment names.

## Claude Code

- The **Microsoft Learn MCP server** connected. `CLAUDE.md` requires Claude Code to verify APIs
  against it before writing Foundry / Agent Framework code. Without it, expect stale APIs.
- Generous tool-permission settings for `az`, `azd`, `uv`, `pytest`, `gh` — the build involves a lot
  of CLI work and constant approval prompts will make this take twice as long.

## Time and money

| | |
|---|---|
| Build time | ~1 day of Claude Code working, plus ~2 hours of Azure provisioning wall time. APIM alone is 15–25 minutes. |
| Rehearsal | Budget a half day for Phase 14 plus one full four-episode dry run. |
| Cost | The stack as specified (Service Bus Premium, APIM StandardV2, AI Search S1, ACA dedicated profile, Cosmos autoscale) is roughly **mid-three-figures USD per month** if left running, before model tokens. Cost is deliberately not optimised for demo reliability. See `TEARDOWN.md` for how to park it between episodes. |

## Before Episode 1

- [ ] `make verify-all` green
- [ ] `scripts/verify_security.sh` zero findings
- [ ] Full four-episode dry run completed and timed
- [ ] `PINNED STATE` recorded in `docs/DEMO-NOTES.md`
- [ ] Fallback assets captured in `docs/assets/fallbacks/`
- [ ] A second machine or a co-presenter with the repo cloned and authenticated
- [ ] Mobile hotspot, in case the venue network dies during Episode 3
