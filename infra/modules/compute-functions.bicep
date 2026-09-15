// Two Flex Consumption function apps: the governed business REST API and the MCP tool server.
//
// Both run Python 3.13 (CLAUDE.md section 7.2 - the hosted agent has no 3.12 runtime, so the whole
// repo is 3.13) as the shared func user-assigned identity, with identity-based AzureWebJobsStorage.
// No storage keys, which is why the application storage account has allowSharedKeyAccess false.
//
// Entra authentication is enforced at the platform level in Phase 08; APIM presents a
// managed-identity token for the api://atlas-ops-api audience. Nothing here is anonymous by design,
// it is anonymous only until Phase 08 lands.

@description('Region for the function apps.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('8-char deterministic suffix.')
param resourceToken string

@description('Abbreviation map from infra/abbreviations.json.')
param abbrs object

@description('Resource id of the shared func user-assigned identity.')
param funcIdentityId string

@description('Client id of the shared func user-assigned identity.')
param funcIdentityClientId string

@description('Application storage account name (identity-based access).')
param storageAccountName string

@description('Application storage blob endpoint.')
param storageBlobEndpoint string

@description('Blob container holding the ops API deployment package.')
param apiPackageContainer string

@description('Blob container holding the MCP server deployment package.')
param mcpPackageContainer string

@description('Application Insights connection string.')
@secure()
param appInsightsConnectionString string

// -- endpoints the functions need --------------------------------------------------------------
param cosmosEndpoint string
param cosmosDatabaseName string
param searchEndpoint string
param serviceBusFqdn string
param commandsQueueName string

@description('Python version. 3.13 per CLAUDE.md section 7.2.')
param pythonVersion string = '3.13'

@description('Max Flex instances per app.')
param maximumInstanceCount int = 40

@description('Per-instance memory in MB. 2048 is the Flex default and is plenty here.')
param instanceMemoryMB int = 2048

var planName = '${abbrs.webServerFarm}-atlas-func-${resourceToken}'
var apiAppName = '${abbrs.webSitesFunctions}-atlas-api-${resourceToken}'
var mcpAppName = '${abbrs.webSitesFunctions}-atlas-mcp-${resourceToken}'

// Shared by both apps. Identity-based storage uses the __accountName / __credential / __clientId
// triple rather than a connection string.
var commonAppSettings = [
  { name: 'AzureWebJobsStorage__accountName', value: storageAccountName }
  { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
  { name: 'AzureWebJobsStorage__clientId', value: funcIdentityClientId }
  { name: 'AzureWebJobsStorage__blobServiceUri', value: storageBlobEndpoint }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
  { name: 'AZURE_CLIENT_ID', value: funcIdentityClientId }
  { name: 'ATLAS_COSMOS_ENDPOINT', value: cosmosEndpoint }
  { name: 'ATLAS_COSMOS_DATABASE', value: cosmosDatabaseName }
  { name: 'ATLAS_SEARCH_ENDPOINT', value: searchEndpoint }
  { name: 'ATLAS_SERVICEBUS_FQDN', value: serviceBusFqdn }
  { name: 'ATLAS_COMMANDS_QUEUE', value: commandsQueueName }
  { name: 'OTEL_SERVICE_NAMESPACE', value: 'atlas' }
]

// One Flex plan hosts both apps. FC1 is the only Flex Consumption SKU.
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource apiApp 'Microsoft.Web/sites@2024-04-01' = {
  name: apiAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'opsapi' })
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${funcIdentityId}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storageBlobEndpoint}${apiPackageContainer}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: funcIdentityId
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
      runtime: {
        name: 'python'
        version: pythonVersion
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: commonAppSettings
    }
  }
}

resource mcpApp 'Microsoft.Web/sites@2024-04-01' = {
  name: mcpAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'opsmcp' })
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${funcIdentityId}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storageBlobEndpoint}${mcpPackageContainer}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: funcIdentityId
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
      runtime: {
        name: 'python'
        version: pythonVersion
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: concat(commonAppSettings, [
        // The MCP extension keeps its tool-invocation state in storage QUEUES, not just blobs,
        // so this identity needs Storage Queue Data Contributor and Storage Queue Data Message
        // Processor on top of the blob roles. Both are in rbac.bicep - miss them and tool calls
        // fail at runtime with no obvious cause.
        { name: 'ATLAS_MCP_SERVER_NAME', value: 'atlas-ops-mcp' }
      ])
    }
  }
}

output planName string = plan.name
output planId string = plan.id

output apiAppName string = apiApp.name
output apiAppId string = apiApp.id
output apiAppHostname string = apiApp.properties.defaultHostName
output apiAppUrl string = 'https://${apiApp.properties.defaultHostName}'

output mcpAppName string = mcpApp.name
output mcpAppId string = mcpApp.id
output mcpAppHostname string = mcpApp.properties.defaultHostName
output mcpAppUrl string = 'https://${mcpApp.properties.defaultHostName}'
