// Azure Container Registry, the Container Apps environment, and the three ATLAS container apps.
//
// All three apps deploy with a PLACEHOLDER image so `azd provision` succeeds before any application
// code exists. `azd deploy` replaces the image; the azd-service-name tag is what maps each app to a
// service in azure.yaml.
//
// Every app runs as a user-assigned identity and pulls from ACR with that same identity. There is
// no admin user on the registry and no registry password anywhere.

@description('Region for compute resources.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('8-char deterministic suffix.')
param resourceToken string

@description('Abbreviation map from infra/abbreviations.json.')
param abbrs object

@description('Log Analytics workspace resource id for the ACA environment.')
param logAnalyticsId string

@description('Application Insights connection string, injected into every app.')
@secure()
param appInsightsConnectionString string

// -- identities ------------------------------------------------------------------------------
param webIdentityId string
param webIdentityClientId string
param orchestratorIdentityId string
param orchestratorIdentityClientId string
param workerIdentityId string
param workerIdentityClientId string

// Principal ids are needed here, not just in rbac.bicep, because AcrPull must exist BEFORE the
// container apps are created. See the AcrPull block below.
param webIdentityPrincipalId string
param orchestratorIdentityPrincipalId string
param workerIdentityPrincipalId string

// -- endpoints the apps need ------------------------------------------------------------------
param cosmosEndpoint string
param cosmosDatabaseName string
param searchEndpoint string
param foundryProjectEndpoint string
param foundryModelDeployment string
param serviceBusFqdn string
param serviceBusNamespaceName string
param commandsQueueName string
param storageBlobEndpoint string

@description('azd environment name, surfaced to telemetry as deployment.environment.')
param azdEnvironmentName string

@description('Foundry account name; Settings derives the OpenAI v1 and vectorizer URLs from it.')
param foundryAccountName string

@description('Application Insights resource id, for trace deep links in the UI.')
param appInsightsId string

@description('atlas-web app registration client id. Empty disables built-in auth (never in a demo env).')
param webAppClientId string = ''

@description('Entra tenant for the sign-in issuer.')
param tenantId string = tenant().tenantId

@description('Placeholder image used until azd deploy pushes the real one.')
param placeholderImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

// The image azd last deployed for each service (SERVICE_<NAME>_IMAGE_NAME in the azd env). Without
// these, every `azd provision` would reset a running app to the placeholder until the next deploy,
// which breaks provision idempotency and would take the demo down in the T-10 routine.
@description('Last deployed web image; empty until the first azd deploy.')
param webImage string = ''
@description('Last deployed orchestrator image; empty until the first azd deploy.')
param orchestratorImage string = ''
@description('Last deployed worker image; empty until the first azd deploy.')
param workerImage string = ''

// ACR names: alphanumeric only, no hyphens.
var acrName = take('${abbrs.containerRegistryRegistry}atlas${resourceToken}', 50)
var envName = '${abbrs.appManagedEnvironment}-atlas-${resourceToken}'
var webAppName = '${abbrs.appContainerApp}-atlas-web-${resourceToken}'
var orchestratorAppName = '${abbrs.appContainerApp}-atlas-orch-${resourceToken}'
var workerAppName = '${abbrs.appContainerApp}-atlas-worker-${resourceToken}'

// Shared by all three. Per-app values are appended at the call site below.
@description('Base URL of the operations API. Empty keeps the agent tools on direct Cosmos reads.')
param opsApiUrl string = ''

var commonEnv = [
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
  { name: 'ATLAS_COSMOS_ENDPOINT', value: cosmosEndpoint }
  { name: 'ATLAS_COSMOS_DATABASE', value: cosmosDatabaseName }
  { name: 'ATLAS_SEARCH_ENDPOINT', value: searchEndpoint }
  { name: 'ATLAS_STORAGE_BLOB_ENDPOINT', value: storageBlobEndpoint }
  { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
  { name: 'FOUNDRY_MODEL_DEPLOYMENT', value: foundryModelDeployment }
  { name: 'ATLAS_SERVICEBUS_FQDN', value: serviceBusFqdn }
  { name: 'OTEL_SERVICE_NAMESPACE', value: 'atlas' }
  { name: 'ATLAS_FOUNDRY_ACCOUNT_NAME', value: foundryAccountName }
  { name: 'AZURE_ENV_NAME', value: azdEnvironmentName }
  // THE switch (PHASE-08 section C): set, and the agent tools call the operations API over HTTP.
  { name: 'ATLAS_OPS_API_URL', value: opsApiUrl }
]

// ---------------------------------------------------------------------------- registry --

// Standard, not the Premium the lab pack specifies.
//
// Premium buys geo-replication, private link and higher concurrency. ATLAS uses none of them: one
// region, public endpoint, three images. Standard includes 100 GiB (vs Premium's 500), which is far
// more than three Python images across many revisions will consume.
//
// NOT Basic, deliberately. azure.yaml sets docker.remoteBuild, so `azd deploy` builds through ACR
// Tasks. The official SKU doc says all three SKUs share the same programmatic capabilities, but
// Microsoft support guidance states Tasks are unavailable on Basic and fail with
// TasksOperationsNotAllowed. That conflict is unresolved, and finding out the hard way means a
// broken `azd deploy` in Phase 04. Standard satisfies both readings.
//
// SKU changes apply in place with no downtime and no image loss, so this can move either way later.
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    // No admin user. Pulls are identity-based.
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

// AcrPull, assigned HERE rather than in rbac.bicep.
//
// A container app that declares a `registries` entry resolves that registry credential while it
// provisions its first revision. If the identity lacks AcrPull at that moment the operation does
// not fail fast - it retries until the 20 minute revision timeout and reports
// "ContainerAppOperationError: Operation expired". Observed on the third provision attempt,
// 2026-09-15: all three apps hung for 20 minutes each with zero revisions created, because
// rbac.bicep runs after this module and so no role assignment existed yet.
//
// Microsoft documents this ordering explicitly for IaC: create the identity, assign acrpull, THEN
// create the container app.
// https://learn.microsoft.com/azure/container-apps/managed-identity-image-pull
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in [webIdentityPrincipalId, orchestratorIdentityPrincipalId, workerIdentityPrincipalId]: {
    name: guid(acr.id, p, acrPullRoleId)
    scope: acr
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
      principalId: p
      principalType: 'ServicePrincipal'
    }
  }
]

// ---------------------------------------------------------------------------- environment --

resource acaEnv 'Microsoft.App/managedEnvironments@2026-01-01' = {
  name: envName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: reference(logAnalyticsId, '2023-09-01').customerId
        sharedKey: listKeys(logAnalyticsId, '2023-09-01').primarySharedKey
      }
    }
    // A Dedicated D4 profile alongside Consumption. Dedicated gives predictable latency on stage;
    // cold starts during a demo are the thing we are buying our way out of.
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
      {
        name: 'dedicated-d4'
        workloadProfileType: 'D4'
        minimumCount: 1
        // Headroom for rollouts: a new revision's replicas run beside the old ones until they are
        // ready. At 3 (12 vCPU) web x2 + orchestrator x2 (2 vCPU each) + worker + built-in auth
        // sidecars left no room, and a web revision sat in Activating with an unscheduled replica
        // (2026-09-19, API-NOTES 12). Nodes above the steady state exist only during a rollout.
        maximumCount: 6
      }
    ]
    zoneRedundant: false
  }
}

// ---------------------------------------------------------------------------- apps --

resource webApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: webAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${webIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: acaEnv.id
    workloadProfileName: 'dedicated-d4'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        // SSE streams are long-lived; the app sends a keepalive every 15 s so idle timeouts never
        // cut a slow triage off. Sticky sessions keep a browser on one replica; turns live in
        // Cosmos anyway, so a replica change mid-conversation is still safe.
        stickySessions: {
          affinity: 'sticky'
        }
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: webIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: empty(webImage) ? placeholderImage : webImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: concat(commonEnv, [
            { name: 'AZURE_CLIENT_ID', value: webIdentityClientId }
            { name: 'OTEL_SERVICE_NAME', value: 'atlas-web' }
            { name: 'ATLAS_ORCHESTRATOR_URL', value: 'https://${orchestratorAppName}.internal.${acaEnv.properties.defaultDomain}' }
            { name: 'ATLAS_APPINSIGHTS_ID', value: appInsightsId }
          ])
          // Liveness and readiness use /api/live, which checks nothing downstream: a Cosmos blip
          // must degrade the page, not pull every replica out of rotation. /api/health is the
          // dependency probe for humans and verify scripts.
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/api/live', port: 8000 }
              periodSeconds: 15
              failureThreshold: 4
            }
            {
              type: 'Readiness'
              httpGet: { path: '/api/live', port: 8000 }
              periodSeconds: 10
              failureThreshold: 3
            }
            {
              type: 'Startup'
              httpGet: { path: '/api/live', port: 8000 }
              periodSeconds: 5
              failureThreshold: 24
            }
          ]
        }
      ]
      // Min 2: never cold-start during a demo.
      scale: {
        minReplicas: 2
        maxReplicas: 10
      }
    }
  }
  dependsOn: [acrPull]
}

// Built-in authentication (Easy Auth) with Microsoft Entra ID. No client secret: without one the
// platform uses the implicit flow and returns an ID token, which is all the UI needs (sign-in plus
// the `roles` claim). Bearer tokens for the atlas-web API (verify scripts, sse_probe) are accepted
// through allowedAudiences. Health and liveness are the only anonymous paths.
resource webAuth 'Microsoft.App/containerApps/authConfigs@2026-01-01' = if (!empty(webAppClientId)) {
  parent: webApp
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      excludedPaths: [
        '/api/health'
        '/api/live'
      ]
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: webAppClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            webAppClientId
            'api://atlas-web'
          ]
        }
      }
    }
    httpSettings: {
      requireHttps: true
    }
  }
}

resource orchestratorApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: orchestratorAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'orchestrator' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${orchestratorIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: acaEnv.id
    workloadProfileName: 'dedicated-d4'
    configuration: {
      activeRevisionsMode: 'Single'
      // Internal only. The orchestrator is never reachable from the internet; the web app calls it
      // over the environment's internal domain.
      ingress: {
        external: false
        targetPort: 8001
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: orchestratorIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'orchestrator'
          image: empty(orchestratorImage) ? placeholderImage : orchestratorImage
          resources: {
            cpu: json('2.0')
            memory: '4Gi'
          }
          env: concat(commonEnv, [
            { name: 'AZURE_CLIENT_ID', value: orchestratorIdentityClientId }
            { name: 'OTEL_SERVICE_NAME', value: 'atlas-orchestrator' }
            { name: 'ATLAS_COMMANDS_QUEUE', value: commandsQueueName }
            { name: 'ATLAS_ORCHESTRATION_MODE', value: 'graph' }
          ])
          // The orchestrator opens Cosmos, Search and Foundry clients at startup, so the startup
          // probe is given a long runway; liveness is deliberately slack because a replica running
          // a case is busy, not dead.
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/api/health', port: 8001 }
              periodSeconds: 20
              failureThreshold: 5
            }
            {
              type: 'Readiness'
              httpGet: { path: '/api/health', port: 8001 }
              periodSeconds: 10
              failureThreshold: 3
            }
            {
              type: 'Startup'
              httpGet: { path: '/api/health', port: 8001 }
              periodSeconds: 5
              failureThreshold: 24
            }
          ]
        }
      ]
      scale: {
        minReplicas: 2
        maxReplicas: 10
      }
    }
  }
  dependsOn: [acrPull]
}

resource workerApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: workerAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'worker' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${workerIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: acaEnv.id
    workloadProfileName: 'dedicated-d4'
    configuration: {
      activeRevisionsMode: 'Single'
      // No ingress at all. The worker is reachable only via Service Bus.
      registries: [
        {
          server: acr.properties.loginServer
          identity: workerIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: empty(workerImage) ? placeholderImage : workerImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: concat(commonEnv, [
            { name: 'AZURE_CLIENT_ID', value: workerIdentityClientId }
            { name: 'OTEL_SERVICE_NAME', value: 'atlas-worker' }
            { name: 'ATLAS_COMMANDS_QUEUE', value: commandsQueueName }
          ])
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 5
        rules: [
          {
            // KEDA scales on queue depth using the worker's own identity. No connection string.
            name: 'servicebus-commands'
            custom: {
              type: 'azure-servicebus'
              identity: workerIdentityId
              metadata: {
                namespace: serviceBusNamespaceName
                queueName: commandsQueueName
                messageCount: '5'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [acrPull]
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output acrId string = acr.id

output environmentName string = acaEnv.name
output environmentId string = acaEnv.id
output environmentDefaultDomain string = acaEnv.properties.defaultDomain

output webAppName string = webApp.name
output webAppFqdn string = webApp.properties.configuration.ingress.fqdn
output webAppUrl string = 'https://${webApp.properties.configuration.ingress.fqdn}'
output orchestratorAppName string = orchestratorApp.name
output orchestratorInternalUrl string = 'https://${orchestratorAppName}.internal.${acaEnv.properties.defaultDomain}'
output workerAppName string = workerApp.name
