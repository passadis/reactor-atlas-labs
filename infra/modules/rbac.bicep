// Every role assignment in _labpack/reference/RBAC-MATRIX.md.
//
// Two rules that this module exists to get right:
//
// 1. Cosmos data access is NOT ARM RBAC. It uses
//    Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments. An ARM Contributor assignment grants
//    zero data access. This is the single most common cause of a 403 in this build.
//
// 2. Every GUID is deterministic - guid(scope, principalId, roleDefinitionId) - so re-running
//    `azd provision` is a no-op instead of a name conflict.
//
// Role definition ids below were resolved from the live directory, not recalled. Note that
// "Azure AI User" has been renamed "Foundry User"; the GUID is unchanged. docs/API-NOTES.md.

@description('Foundry account name.')
param foundryAccountName string

@description('Foundry project name (child of the account).')
param foundryProjectName string

@description('Foundry project system-assigned principal id.')
param foundryProjectPrincipalId string

@description('Foundry account system-assigned principal id. Server-side agent tools (the Azure AI Search tool) call Search as this identity.')
param foundryAccountPrincipalId string

@description('Cosmos account name.')
param cosmosAccountName string

@description('AI Search service name.')
param searchServiceName string

@description('AI Search system-assigned principal id. The index vectorizer calls embed-main as this identity.')
param searchPrincipalId string

@description('Entra agent identity of the Foundry hosted agent (created by the platform on its first deploy; recorded by scripts/record_hosted_identity.sh). Empty until then.')
param hostedAgentPrincipalId string = ''

@description('Service Bus namespace name.')
param serviceBusNamespaceName string

@description('Commands queue name.')
param commandsQueueName string

@description('Results queue name.')
param resultsQueueName string

@description('Events topic name.')
param eventsTopicName string

@description('Application storage account name.')
param storageAccountName string

@description('Logic Apps host storage account name.')
param logicStorageAccountName string

@description('Key Vault name.')
param keyVaultName string

@description('Application Insights component name.')
param appInsightsName string

@description('Log Analytics workspace name.')
param logAnalyticsName string

@description('APIM system-assigned principal id.')
param apimPrincipalId string

// -- workload principals ----------------------------------------------------------------------
param webPrincipalId string
param orchestratorPrincipalId string
param workerPrincipalId string
param funcPrincipalId string
param logicPrincipalId string

@description('Object id of the human operator. Gets seeding and inspection access. Empty disables those assignments.')
param principalId string = ''

@description('Principal type of the human operator.')
param principalType string = 'User'

// ---------------------------------------------------------------------------- role ids --

var roles = {
  foundryUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  foundryProjectManager: 'eadc314b-1a2d-4efa-be10-5d325db5065e'
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  serviceBusDataSender: '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
  serviceBusDataReceiver: '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
  serviceBusDataOwner: '090c5cfd-751d-490a-894a-3ce6f1109419'
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  storageQueueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  storageQueueDataMessageProcessor: '8a0f0c08-91a1-4084-bc3d-661d67233fed'
  storageTableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
  logAnalyticsReader: '73c42c96-874c-492b-b04d-ab87d138a893'
  privilegedMonitoringDataReader: 'dbc9c667-e97f-4491-aee6-90b9cf960190'
  cosmosAccountReader: 'fbdf93bf-df7d-467e-a4d2-9458aa1360c8'
}

// Cosmos DB Built-in Data Contributor. A fixed, well-known data-plane role id, not an ARM role.
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'
var cosmosDataReaderRoleId = '00000000-0000-0000-0000-000000000001'

var hasOperator = !empty(principalId)

// ---------------------------------------------------------------------------- existing refs --

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2026-07-01' existing = {
  name: foundryAccountName
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2026-07-01' existing = {
  parent: foundryAccount
  name: foundryProjectName
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2026-03-15' existing = {
  name: cosmosAccountName
}

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: searchServiceName
}

resource sbNamespace 'Microsoft.ServiceBus/namespaces@2024-01-01' existing = {
  name: serviceBusNamespaceName
}

resource commandsQueue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' existing = {
  parent: sbNamespace
  name: commandsQueueName
}

resource resultsQueue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' existing = {
  parent: sbNamespace
  name: resultsQueueName
}

resource eventsTopic 'Microsoft.ServiceBus/namespaces/topics@2024-01-01' existing = {
  parent: sbNamespace
  name: eventsTopicName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource logicStorage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: logicStorageAccountName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

// ---------------------------------------------------------------------------- foundry --

// Workloads that call the model or the project API.
var foundryUserPrincipals = [
  { name: 'web', id: webPrincipalId }
  { name: 'orchestrator', id: orchestratorPrincipalId }
  { name: 'func', id: funcPrincipalId }
  { name: 'logic', id: logicPrincipalId }
]

resource foundryUserAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in foundryUserPrincipals: {
    name: guid(foundryProject.id, p.id, roles.foundryUser)
    scope: foundryProject
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.foundryUser)
      principalId: p.id
      principalType: 'ServicePrincipal'
    }
  }
]

// The knowledge tool embeds queries through the ACCOUNT's /openai/v1 endpoint; the project-scope
// Foundry User role above does not cover account-level data actions. Web and orchestrator both run
// the incident agent in-process.
resource agentHostOpenAIUsers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in [
    { name: 'web', id: webPrincipalId }
    { name: 'orchestrator', id: orchestratorPrincipalId }
    // Phase 08: the MCP server's search_ops_knowledge tool embeds through the account too.
    { name: 'func', id: funcPrincipalId }
  ]: {
    name: guid(foundryAccount.id, p.id, roles.cognitiveServicesOpenAIUser)
    scope: foundryAccount
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAIUser)
      principalId: p.id
      principalType: 'ServicePrincipal'
    }
  }
]

// APIM reaches the models as itself. The model never holds a credential; APIM does.
resource apimOpenAIUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, apimPrincipalId, roles.cognitiveServicesOpenAIUser)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAIUser)
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// The knowledge index's integrated vectorizer embeds agent-tool queries by calling embed-main as the
// Search service's own identity. Without it the Foundry Azure AI Search tool cannot run hybrid
// queries (docs/API-NOTES.md 10.3).
resource searchOpenAIUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, searchPrincipalId, roles.cognitiveServicesOpenAIUser)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAIUser)
    principalId: searchPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Required to deploy a hosted agent (Phase 05).
resource operatorProjectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasOperator) {
  name: guid(foundryProject.id, principalId, roles.foundryProjectManager)
  scope: foundryProject
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.foundryProjectManager)
    principalId: principalId
    principalType: principalType
  }
}

// ---------------------------------------------------------------------------- ai search --

resource searchReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in [
    { name: 'func', id: funcPrincipalId }
    { name: 'orchestrator', id: orchestratorPrincipalId }
    // The web app runs the incident agent in-process ("This app" mode, Phase 04), so its
    // search_knowledge tool reads the index directly. Missing in Phase 01; found by verify_phase04.
    { name: 'web', id: webPrincipalId }
    // The project's own identity, so the AAD-authenticated project connection can actually read.
    { name: 'foundry-project', id: foundryProjectPrincipalId }
  ]: {
    name: guid(search.id, p.id, roles.searchIndexDataReader)
    scope: search
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader)
      principalId: p.id
      principalType: 'ServicePrincipal'
    }
  }
]

// The Foundry Azure AI Search agent tool (prompt and hosted agents) reads the index definition as
// well as its documents, and runs as the Foundry identities, not as the caller. Index Data Reader
// alone returns "Access denied" from the tool (probed 2026-09-18). Reads only: Data Contributor,
// which the docs list, is not needed for a read-only knowledge tool.
resource foundryAccountSearchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, foundryAccountPrincipalId, roles.searchIndexDataReader)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader)
    principalId: foundryAccountPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource foundrySearchServiceContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in [foundryAccountPrincipalId, foundryProjectPrincipalId]: {
    name: guid(search.id, p, roles.searchServiceContributor)
    scope: search
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchServiceContributor)
      principalId: p
      principalType: 'ServicePrincipal'
    }
  }
]

// Index creation and seeding happen as the operator, from a laptop, in Phase 02.
resource operatorSearchContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasOperator) {
  name: guid(search.id, principalId, roles.searchServiceContributor)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchServiceContributor)
    principalId: principalId
    principalType: principalType
  }
}

resource operatorSearchIndexContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasOperator) {
  name: guid(search.id, principalId, roles.searchIndexDataContributor)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataContributor)
    principalId: principalId
    principalType: principalType
  }
}

// ---------------------------------------------------------------------------- service bus --
// Scoped per entity, not per namespace, so nothing can send where it should only receive.

resource orchestratorSbSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(commandsQueue.id, orchestratorPrincipalId, roles.serviceBusDataSender)
  scope: commandsQueue
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataSender)
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource funcSbSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(commandsQueue.id, funcPrincipalId, roles.serviceBusDataSender)
  scope: commandsQueue
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataSender)
    principalId: funcPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource workerSbReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(commandsQueue.id, workerPrincipalId, roles.serviceBusDataReceiver)
  scope: commandsQueue
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataReceiver)
    principalId: workerPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource workerResultsSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resultsQueue.id, workerPrincipalId, roles.serviceBusDataSender)
  scope: resultsQueue
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataSender)
    principalId: workerPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource orchestratorResultsReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resultsQueue.id, orchestratorPrincipalId, roles.serviceBusDataReceiver)
  scope: resultsQueue
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataReceiver)
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource workerEventsSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(eventsTopic.id, workerPrincipalId, roles.serviceBusDataSender)
  scope: eventsTopic
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataSender)
    principalId: workerPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Phase 10: the orchestrator follows remediation progress (subscription orchestrator-progress).
resource orchestratorEventsReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(eventsTopic.id, orchestratorPrincipalId, roles.serviceBusDataReceiver)
  scope: eventsTopic
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataReceiver)
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource webEventsReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(eventsTopic.id, webPrincipalId, roles.serviceBusDataReceiver)
  scope: eventsTopic
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataReceiver)
    principalId: webPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// DLQ inspection and replay during Episode 3.
resource operatorSbOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasOperator) {
  name: guid(sbNamespace.id, principalId, roles.serviceBusDataOwner)
  scope: sbNamespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataOwner)
    principalId: principalId
    principalType: principalType
  }
}

// ---------------------------------------------------------------------------- storage --

// The Functions MCP extension keeps tool-invocation state in storage QUEUES. Without the two queue
// roles below, tool calls fail at runtime with no obvious cause. RBAC-MATRIX.md flags this; it is
// the least guessable entry in the whole table.
var funcStorageRoles = [
  roles.storageBlobDataOwner
  roles.storageQueueDataContributor
  roles.storageQueueDataMessageProcessor
  roles.storageTableDataContributor
]

resource funcStorageAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in funcStorageRoles: {
    name: guid(storage.id, funcPrincipalId, r)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
      principalId: funcPrincipalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource orchestratorStorageContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, orchestratorPrincipalId, roles.storageBlobDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Agents read their instruction versions from blob at runtime, so the v0/v1 switch is a data read
// rather than a redeploy. CLAUDE.md section 9.1.
resource webStorageReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, webPrincipalId, roles.storageBlobDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: webPrincipalId
    principalType: 'ServicePrincipal'
  }
}

var logicStorageRoles = [
  roles.storageBlobDataOwner
  roles.storageQueueDataContributor
  roles.storageTableDataContributor
]

resource logicStorageAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in logicStorageRoles: {
    name: guid(logicStorage.id, logicPrincipalId, r)
    scope: logicStorage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
      principalId: logicPrincipalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource operatorStorageContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasOperator) {
  name: guid(storage.id, principalId, roles.storageBlobDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: principalId
    principalType: principalType
  }
}

// ---------------------------------------------------------------------------- acr --
// AcrPull is assigned in compute-aca.bicep, not here. It must exist BEFORE the container apps are
// created or their first revision hangs until the 20 minute timeout. See that module.

// ---------------------------------------------------------------------------- key vault --

var keyVaultReaders = [orchestratorPrincipalId, logicPrincipalId, workerPrincipalId, apimPrincipalId, webPrincipalId, funcPrincipalId]

resource keyVaultSecretsUsers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  // web, func and the hosted agent joined in Phase 09: each reads the APIM subscription key by
  // secret URI at startup (the one permitted secret, CLAUDE.md section 1).
  for p in concat(keyVaultReaders, empty(hostedAgentPrincipalId) ? [] : [hostedAgentPrincipalId]): {
    name: guid(keyVault.id, p, roles.keyVaultSecretsUser)
    scope: keyVault
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
      principalId: p
      principalType: 'ServicePrincipal'
    }
  }
]

resource operatorKeyVaultOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasOperator) {
  name: guid(keyVault.id, principalId, roles.keyVaultSecretsOfficer)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsOfficer)
    principalId: principalId
    principalType: principalType
  }
}

// ---------------------------------------------------------------------------- monitoring --

resource metricsPublishers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in [webPrincipalId, orchestratorPrincipalId, workerPrincipalId, funcPrincipalId, logicPrincipalId]: {
    name: guid(appInsights.id, p, roles.monitoringMetricsPublisher)
    scope: appInsights
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringMetricsPublisher)
      principalId: p
      principalType: 'ServicePrincipal'
    }
  }
]

// The Foundry project reads back its own agent traces for evaluation. Privileged Monitoring Data
// Reader is what allows GenAI content (prompts and completions) to be read, not just metrics -
// without it, Phase 12 trace evaluation returns empty. Taken from the official
// connection-application-insights.bicep template.
var projectMonitoringRoles = [
  roles.logAnalyticsReader
  roles.privilegedMonitoringDataReader
]

resource projectMonitoringAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in projectMonitoringRoles: {
    name: guid(appInsights.id, foundryProjectPrincipalId, r)
    scope: appInsights
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
      principalId: foundryProjectPrincipalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource operatorLogAnalyticsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasOperator) {
  name: guid(logAnalytics.id, principalId, roles.logAnalyticsReader)
  scope: logAnalytics
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.logAnalyticsReader)
    principalId: principalId
    principalType: principalType
  }
}

// ---------------------------------------------------------------------------- cosmos --
// DATA PLANE. Not ARM RBAC. See the header comment.

var cosmosDataPrincipals = [
  { name: 'web', id: webPrincipalId }
  { name: 'orchestrator', id: orchestratorPrincipalId }
  { name: 'worker', id: workerPrincipalId }
  { name: 'func', id: funcPrincipalId }
  { name: 'logic', id: logicPrincipalId }
]

resource cosmosDataAssignments 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2026-03-15' = [
  for p in cosmosDataPrincipals: {
    parent: cosmos
    name: guid(cosmos.id, p.id, cosmosDataContributorRoleId)
    properties: {
      roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
      principalId: p.id
      scope: cosmos.id
    }
  }
]

// ---------------------------------------------------------------- hosted agent (Phase 05) --
// The Foundry hosted agent runs the same atlas_agent tools as the web app, under its OWN Entra agent
// identity. Until Phase 09 re-points the tools at the APIM-fronted ops API, that identity reads
// Cosmos and Search directly - READ ONLY: every ATLAS agent tool is read-only, and the platform
// keeps the hosted agent's conversation history, so it never writes to Cosmos. Embeddings go
// through the account's /openai/v1 endpoint, which the project-scope defaults do not cover.
var hasHostedAgent = !empty(hostedAgentPrincipalId)

resource hostedAgentCosmosReader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2026-03-15' = if (hasHostedAgent) {
  parent: cosmos
  name: guid(cosmos.id, hostedAgentPrincipalId, cosmosDataReaderRoleId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataReaderRoleId}'
    principalId: hostedAgentPrincipalId
    scope: cosmos.id
  }
}

resource hostedAgentSearchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasHostedAgent) {
  name: guid(search.id, hostedAgentPrincipalId, roles.searchIndexDataReader)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader)
    principalId: hostedAgentPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource hostedAgentOpenAIUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasHostedAgent) {
  name: guid(foundryAccount.id, hostedAgentPrincipalId, roles.cognitiveServicesOpenAIUser)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAIUser)
    principalId: hostedAgentPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// The operator seeds Cosmos from a laptop in Phase 02 and inspects checkpoints on stage in
// Episode 2. Without this, both 403.
resource operatorCosmosData 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2026-03-15' = if (hasOperator) {
  parent: cosmos
  name: guid(cosmos.id, principalId, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: principalId
    scope: cosmos.id
  }
}

// Account metadata reads (ARM, not data plane).
resource cosmosAccountReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in [orchestratorPrincipalId, workerPrincipalId, funcPrincipalId]: {
    name: guid(cosmos.id, p, roles.cosmosAccountReader)
    scope: cosmos
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cosmosAccountReader)
      principalId: p
      principalType: 'ServicePrincipal'
    }
  }
]

output assignmentSummary string = 'foundry=${length(foundryUserPrincipals)} search=${length(searchReaders)} cosmosData=${length(cosmosDataPrincipals)} acrPull=3 operator=${hasOperator}'
