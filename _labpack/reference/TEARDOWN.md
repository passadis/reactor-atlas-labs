# Parking and Teardown

Cost is deliberately not optimised during the build and the series. Here is how to stop paying
between episodes and how to remove everything afterwards.

## Between episodes — park, do not delete

`scripts/park.sh` and `scripts/unpark.sh`.

| Resource | Park | Unpark | Saving |
|---|---|---|---|
| `ca-atlas-web`, `ca-atlas-orchestrator` | min replicas 0 | min replicas 2 | large |
| `ca-atlas-worker` | min 0 (KEDA scales on queue) | min 1 | moderate |
| Cosmos DB | autoscale max 4000 → 1000 RU/s | back to 4000 | moderate |
| Azure AI Search | leave running | — | S1 has no scale-to-zero; deleting loses the index |
| Service Bus Premium | **downgrade to Standard between episodes if you must**, but re-test dedup and sessions after upgrading back | | large |
| APIM StandardV2 | leave running | — | deleting and recreating takes 20+ min and changes the gateway URL |
| Functions Flex Consumption | consumption-based, no action | — | |
| Logic Apps WS1 plan | leave running | — | |
| Model deployments | leave — you pay per token on GlobalStandard | — | |

**Always run `scripts/unpark.sh` and then `scripts/warm.sh` at least 30 minutes before an
episode.** Container Apps scaling back up plus a Cosmos RU change is not instant.

Do not park APIM or AI Search. Recreating them changes URLs and index state, which invalidates your
pinned demo.

## After the series — full teardown

```bash
azd env select atlas-stg && azd down --force --purge
azd env select atlas     && azd down --force --purge
```

Then clean up what `azd down` does not:

```bash
# Soft-deleted Foundry/Cognitive Services accounts block name reuse
az cognitiveservices account list-deleted -o table
az cognitiveservices account purge -n <name> -l <location> -g <rg>

# Soft-deleted Key Vault
az keyvault list-deleted -o table
az keyvault purge -n <name> -l <location>

# Soft-deleted APIM
az apim deletedservice list -o table
az apim deletedservice purge --service-name <name> --location <location>

# Entra app registrations
az ad app delete --id <appId>      # atlas-web, atlas-gateway, atlas-ops-api

# Federated credentials / CI service principal
az ad sp delete --id <spId>
```

Verify nothing is left:

```bash
az resource list --tag project=atlas -o table
az ad app list --filter "startswith(displayName,'atlas')" -o table
```

## Keep

Before tearing down, archive:

- `evals/results/**` — the scorecards are the evidence in your write-up
- `docs/assets/fallbacks/**` — recordings and screenshots
- `verification-report.json` from the final `make verify-all`
- The architecture diagram export
- `docs/PUBLIC-README.md`

These are what you publish after Episode 4. The infrastructure is disposable; the evidence is not.
