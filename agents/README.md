# ATLAS Foundry agents project

This directory is a **second `azd` project**, separate from the repository root. It owns the Foundry
hosted agent, the toolbox, and the project connections. Nothing else.

## Why it is separate

`infra.provider` is a single value per `azure.yaml`. The hosted-agent model requires
`microsoft.foundry`; the ATLAS platform requires `bicep`. One file cannot serve both. Full reasoning
and evidence in `CLAUDE.md` section 7.3.

| Project | File | Provider | Owns |
|---|---|---|---|
| Platform | `../azure.yaml` | `bicep` | RG, Foundry account + project + the 3 model deployments, Cosmos, Search, Service Bus, ACA, Functions, APIM, Logic Apps, Key Vault, RBAC |
| Agents | `./azure.yaml` | `microsoft.foundry` | hosted agent, toolbox, connections, routines |

The seam: an `azure.ai.project` service can target an **existing** project by setting `endpoint:`
and declaring no `deployments:`. So Bicep provisions the project and the models; this project only
references them. No duplication, no drift.

`scripts/postprovision.sh` writes `FOUNDRY_PROJECT_ENDPOINT` into `agents/.azure/<env>/.env` so
`${...}` substitution resolves here.

## Status

`azure.yaml` is **generated in Phase 05**, not hand-written, via:

```bash
azd ai agent init --kind hosted --protocol responses --deploy-mode code \
  --runtime python_3_13 --entry-point main.py \
  --agent-name atlas-incident-agent --project-id <provisioned project id>
```

Letting the extension generate it guarantees the schema is current. Phase 05 then edits it to point
`ai-project` at the existing endpoint and to attach the `atlas-tools` toolbox.

## Open decision for Phase 05: how the agent package gets `atlas_agent`

The hosted agent must import `build_incident_agent` from `atlas_agent` — one agent definition, two
hosting models, which is the whole Episode 1 payoff. But `atlas_agent` and `atlas_common` are local
uv workspace members, not PyPI packages, so a plain `code` (ZIP) deploy with a `requirements.txt`
cannot resolve them.

Three candidates, to be decided with a real probe in Phase 05:

1. **`--dep-resolution bundled`** on a `code` deploy. The extension advertises `remote_build` and
   `bundled`; if `bundled` vendors local paths, this is the cheapest answer. **Try this first.**
2. **Build wheels** for `atlas_common` and `atlas_agent` into the agent directory and reference them
   from `requirements.txt`. Reliable, but adds a build step to every deploy.
3. **`container` deploy mode** with a Dockerfile whose build context is the repository root. Most
   faithful to "one definition", but needs a running Docker daemon for the local
   `azd ai agent run` loop, and a remote ACR build otherwise.

Do not resolve this by copying agent code into this directory. Duplicating the definition destroys
the Episode 1 claim that the hosted agent and the ACA app are the same agent.

## Runtime facts that constrain whatever we pick

- The agent listens on **port 8088** and serves a health probe.
- Protocol `responses` (`POST /responses`), version `2.0.0`.
- `FOUNDRY_PROJECT_ENDPOINT` is injected by the platform. Never declare it in `env:`.
- `container.resources`: cpu `"0.25"`-`"4.0"`, memory `0.5Gi`-`8.0Gi`.
- Deploying needs **Foundry Project Manager** at project scope.
- Python runtime must be `python_3_13` or `python_3_14`. There is no `python_3_12`.
