// Cosmos DB (state, checkpoints, idempotency), Azure AI Search (runbooks + past incidents),
// and the application Storage account.
//
// All three are Entra-only. Cosmos and Search have local auth disabled outright; Storage disables
// shared-key access. Logic Apps Standard gets its OWN storage account in logicapps.bicep because it
// still requires shared-key access to its host storage (CLAUDE.md section 7.9) - isolating that
// exception keeps it out of the application data path.

@description('Region for data resources.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('8-char deterministic suffix.')
param resourceToken string

@description('Abbreviation map from infra/abbreviations.json.')
param abbrs object

@description('Max autoscale throughput shared across the ATLAS database.')
param cosmosAutoscaleMaxThroughput int = 4000

var cosmosAccountName = '${abbrs.documentDbDatabaseAccount}-atlas-${resourceToken}'
var searchServiceName = '${abbrs.searchSearchService}-atlas-${resourceToken}'
// Storage account names: no hyphens, max 24 chars, lowercase.
var storageAccountName = take('${abbrs.storageStorageAccount}atlas${resourceToken}', 24)
var cosmosDatabaseName = 'atlas'

// ---------------------------------------------------------------------------- cosmos --

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2026-03-15' = {
  name: cosmosAccountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    databaseAccountOfferType: 'Standard'
    // Session consistency: the orchestrator reads its own writes, which is what checkpoint
    // resume depends on, without paying for strong.
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    // Entra-only data plane. Every Cosmos call uses a SQL role assignment from rbac.bicep;
    // there is no account key path. Get this wrong and every call 403s.
    disableLocalAuth: true
    disableKeyBasedMetadataWriteAccess: true
    publicNetworkAccess: 'Enabled'
    minimalTlsVersion: 'Tls12'
    capabilities: []
  }
}

resource cosmosDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2026-03-15' = {
  parent: cosmos
  name: cosmosDatabaseName
  properties: {
    resource: {
      id: cosmosDatabaseName
    }
    options: {
      autoscaleSettings: {
        maxThroughput: cosmosAutoscaleMaxThroughput
      }
    }
  }
}

// Containers share the database-level autoscale throughput, so none declares its own options.
// defaultTtl -1 means "TTL enabled, no default expiry"; only `idempotency` sets a real TTL.
var containers = [
  { name: 'incidents', partitionKey: '/incident_id', defaultTtl: -1 }
  { name: 'tickets', partitionKey: '/ticket_id', defaultTtl: -1 }
  { name: 'approvals', partitionKey: '/case_id', defaultTtl: -1 }
  // Partition key is fixed by CosmosCheckpointStorage. Pre-created here because the store would
  // otherwise create it on first use, which is a control-plane call that 403s under a
  // data-plane-only role. See CLAUDE.md section 7.9.
  { name: 'workflow-checkpoints', partitionKey: '/workflow_name', defaultTtl: -1 }
  // 7 days. Guarantees exactly-once remediation within the window that matters.
  { name: 'idempotency', partitionKey: '/key', defaultTtl: 604800 }
  { name: 'sessions', partitionKey: '/session_id', defaultTtl: 86400 }
  { name: 'eval-runs', partitionKey: '/run_id', defaultTtl: -1 }
]

resource cosmosContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2026-03-15' = [
  for c in containers: {
    parent: cosmosDatabase
    name: c.name
    properties: {
      resource: {
        id: c.name
        partitionKey: {
          paths: [c.partitionKey]
          kind: 'Hash'
          version: 2
        }
        defaultTtl: c.defaultTtl
        indexingPolicy: {
          indexingMode: 'consistent'
          automatic: true
          includedPaths: [{ path: '/*' }]
          excludedPaths: [{ path: '/"_etag"/?' }]
        }
      }
    }
  }
]

// ---------------------------------------------------------------------------- ai search --

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: searchServiceName
  location: location
  tags: tags
  sku: {
    name: 'standard'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'Default'
    publicNetworkAccess: 'enabled'
    // RBAC only. authOptions and disableLocalAuth are mutually exclusive; setting
    // disableLocalAuth true is what turns off admin/query keys entirely.
    disableLocalAuth: true
    // Required for the Episode 1 hybrid + semantic retrieval story.
    semanticSearch: 'standard'
  }
}

// ---------------------------------------------------------------------------- storage --

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    // Functions Flex Consumption supports identity-based AzureWebJobsStorage, so the application
    // storage needs no keys. Logic Apps is the exception and gets its own account.
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    publicNetworkAccess: 'Enabled'
    defaultToOAuthAuthentication: true
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {}
}

var blobContainers = [
  'eval-artifacts'
  'demo-assets'
  'agent-instructions'
]

resource storageContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [
  for name in blobContainers: {
    parent: blobService
    name: name
    properties: {
      publicAccess: 'None'
    }
  }
]

// ---------------------------------------------------------------------------- outputs --

output cosmosAccountName string = cosmos.name
output cosmosAccountId string = cosmos.id
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output cosmosDatabaseName string = cosmosDatabase.name
output cosmosContainerNames array = [for (c, i) in containers: c.name]

output searchServiceName string = search.name
output searchServiceId string = search.id
output searchServiceEndpoint string = 'https://${search.name}.search.windows.net'
output searchPrincipalId string = search.identity.principalId

output storageAccountName string = storage.name
output storageAccountId string = storage.id
output storageBlobEndpoint string = storage.properties.primaryEndpoints.blob
output storageQueueEndpoint string = storage.properties.primaryEndpoints.queue
