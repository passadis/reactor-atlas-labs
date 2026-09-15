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

@description('Placeholder image used until azd deploy pushes the real one.')
param placeholderImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

// ACR names: alphanumeric only, no hyphens.
var acrName = take('${abbrs.containerRegistryRegistry}atlas${resourceToken}', 50)
var envName = '${abbrs.appManagedEnvironment}-atlas-${resourceToken}'
var webAppName = '${abbrs.appContainerApp}-atlas-web-${resourceToken}'
var orchestratorAppName = '${abbrs.appContainerApp}-atlas-orch-${resourceToken}'
var workerAppName = '${abbrs.appContainerApp}-atlas-worker-${resourceToken}'

// Shared by all three. Per-app values are appended at the call site below.
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
]

// ---------------------------------------------------------------------------- registry --

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Premium'
  }
  properties: {
    // No admin user. Pulls are identity-based.
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

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
        maximumCount: 3
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
        // SSE streams are long-lived; the default 240s would cut a slow triage off mid-answer.
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
          image: placeholderImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: concat(commonEnv, [
            { name: 'AZURE_CLIENT_ID', value: webIdentityClientId }
            { name: 'OTEL_SERVICE_NAME', value: 'atlas-web' }
            { name: 'ATLAS_ORCHESTRATOR_URL', value: 'https://${orchestratorAppName}.internal.${acaEnv.properties.defaultDomain}' }
          ])
        }
      ]
      // Min 2: never cold-start during a demo.
      scale: {
        minReplicas: 2
        maxReplicas: 10
      }
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
          image: placeholderImage
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
        }
      ]
      scale: {
        minReplicas: 2
        maxReplicas: 10
      }
    }
  }
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
          image: placeholderImage
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
