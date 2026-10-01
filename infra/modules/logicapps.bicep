// Logic Apps Standard on a WS1 plan: the approval workflow and the enrich-incident workflow.
//
// This module owns its OWN storage account. Logic Apps Standard still requires shared-key access to
// its host storage (it mounts an Azure Files content share), which the application storage account
// forbids. Isolating the exception here keeps shared-key access entirely out of the ATLAS data path
// - no incident data, no checkpoints, no eval artefacts live on this account. CLAUDE.md section 7.9.
//
// The workflows themselves are deployed in Phase 10. This creates the host.

@description('Region for the Logic Apps resources.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('8-char deterministic suffix.')
param resourceToken string

@description('Abbreviation map from infra/abbreviations.json.')
param abbrs object

@description('Resource id of the logic user-assigned identity.')
param logicIdentityId string

@description('Client id of the logic user-assigned identity.')
param logicIdentityClientId string

@description('Application Insights connection string.')
@secure()
param appInsightsConnectionString string

@description('Orchestrator internal URL. The approval workflow posts the human decision back here.')
param orchestratorUrl string

// Phase 10. The orchestrator is internal-only, so the approval workflow reaches it through the web
// app's callback endpoint, as its own identity. The enrich workflow calls the ops API through the
// gateway as an agent; the subscription key is a Key Vault REFERENCE resolved by the platform with
// the workflow identity - the value never appears in source, Bicep or an app setting.
@description('Public URL of the web app.')
param webUrl string

@description('APIM gateway base URL.')
param gatewayUrl string

@description('Key Vault secret URI of the agent APIM subscription key.')
param apimKeySecretUri string

var planName = '${abbrs.webServerFarm}-atlas-logic-${resourceToken}'
var siteName = '${abbrs.webSitesLogicApp}-atlas-${resourceToken}'
// Separate from the application storage account, deliberately. Names: no hyphens, max 24 chars.
var storageName = take('${abbrs.storageStorageAccount}atlaslogic${resourceToken}', 24)
var contentShareName = toLower(siteName)

// Host storage for the Logic Apps runtime only. Shared-key access is ON because the runtime
// requires it; nothing else uses this account.
resource logicStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: union(tags, { purpose: 'logicapps-host-storage' })
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: true
    allowBlobPublicAccess: false
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'elastic'
  sku: {
    name: 'WS1'
    tier: 'WorkflowStandard'
  }
  properties: {
    maximumElasticWorkerCount: 3
    elasticScaleEnabled: true
    isSpot: false
  }
}

// Built at deploy time from listKeys; never in source, never an output of this module.
var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${logicStorage.name};AccountKey=${logicStorage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'

resource logicApp 'Microsoft.Web/sites@2024-04-01' = {
  name: siteName
  location: location
  tags: tags
  kind: 'functionapp,workflowapp'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${logicIdentityId}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    keyVaultReferenceIdentity: logicIdentityId
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      netFrameworkVersion: 'v8.0'
      appSettings: [
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'node' }
        { name: 'WEBSITE_NODE_DEFAULT_VERSION', value: '~20' }
        { name: 'APP_KIND', value: 'workflowApp' }
        { name: 'AzureWebJobsStorage', value: storageConnectionString }
        { name: 'WEBSITE_CONTENTAZUREFILECONNECTIONSTRING', value: storageConnectionString }
        { name: 'WEBSITE_CONTENTSHARE', value: contentShareName }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
        // Workflows authenticate outbound as the UAMI, so no connection strings to backends.
        { name: 'AZURE_CLIENT_ID', value: logicIdentityClientId }
        { name: 'ATLAS_ORCHESTRATOR_URL', value: orchestratorUrl }
        { name: 'WORKFLOWS_SUBSCRIPTION_ID', value: subscription().subscriptionId }
        { name: 'WORKFLOWS_RESOURCE_GROUP_NAME', value: resourceGroup().name }
        { name: 'WORKFLOWS_LOCATION_NAME', value: location }
        { name: 'ATLAS_WEB_CALLBACK_URL', value: '${webUrl}/api/callbacks/approvals' }
        { name: 'ATLAS_WEB_AUDIENCE', value: 'api://atlas-web' }
        { name: 'ATLAS_LOGIC_IDENTITY_ID', value: logicIdentityId }
        { name: 'ATLAS_GATEWAY_URL', value: gatewayUrl }
        { name: 'ATLAS_GATEWAY_AUDIENCE', value: 'api://atlas-gateway' }
        { name: 'ATLAS_APIM_KEY', value: '@Microsoft.KeyVault(SecretUri=${apimKeySecretUri})' }
      ]
    }
  }
}

output logicAppName string = logicApp.name
output logicAppId string = logicApp.id
output logicAppHostname string = logicApp.properties.defaultHostName
output logicAppUrl string = 'https://${logicApp.properties.defaultHostName}'
output logicStorageName string = logicStorage.name
output planName string = plan.name
