// Service Bus Premium: the asynchronous remediation path (Episode 3).
//
// Premium, not Standard, so nothing throttles mid-demo and sessions are available for ordering.
// Local auth is disabled: the orchestrator sends and the worker receives using their user-assigned
// identities and the role assignments in rbac.bicep.

@description('Region for the Service Bus namespace.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('8-char deterministic suffix.')
param resourceToken string

@description('Abbreviation map from infra/abbreviations.json.')
param abbrs object

@description('Premium messaging units. 1 is ample for a demo and provisions fastest.')
param capacity int = 1

var namespaceName = '${abbrs.serviceBusNamespace}-atlas-${resourceToken}'

resource namespace 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: namespaceName
  location: location
  tags: tags
  sku: {
    name: 'Premium'
    tier: 'Premium'
    capacity: capacity
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // Entra-only. No connection strings anywhere (CLAUDE.md section 1).
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    zoneRedundant: false
  }
}

// The remediation command queue. Two properties carry the whole exactly-once story:
//   requiresSession            -> SessionId = target service id, so commands for one service are
//                                 ordered relative to each other.
//   requiresDuplicateDetection -> MessageId = plan_id, so a resubmitted plan within the window is
//                                 silently dropped by the broker rather than executed twice.
resource commandsQueue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = {
  parent: namespace
  name: 'remediation-commands'
  properties: {
    requiresSession: true
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'PT10M'
    maxDeliveryCount: 5
    lockDuration: 'PT5M'
    deadLetteringOnMessageExpiration: true
    defaultMessageTimeToLive: 'PT1H'
    enableBatchedOperations: true
    enablePartitioning: false
  }
}

resource resultsQueue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = {
  parent: namespace
  name: 'remediation-results'
  properties: {
    requiresSession: false
    requiresDuplicateDetection: false
    maxDeliveryCount: 5
    lockDuration: 'PT1M'
    deadLetteringOnMessageExpiration: true
    defaultMessageTimeToLive: 'P1D'
    enableBatchedOperations: true
    enablePartitioning: false
  }
}

// Progress events. web-progress drives the live UI timeline; audit is the durable record.
resource eventsTopic 'Microsoft.ServiceBus/namespaces/topics@2024-01-01' = {
  parent: namespace
  name: 'remediation-events'
  properties: {
    defaultMessageTimeToLive: 'P1D'
    enableBatchedOperations: true
    enablePartitioning: false
    supportOrdering: true
  }
}

resource webProgressSubscription 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2024-01-01' = {
  parent: eventsTopic
  name: 'web-progress'
  properties: {
    maxDeliveryCount: 5
    lockDuration: 'PT1M'
    deadLetteringOnMessageExpiration: true
    defaultMessageTimeToLive: 'PT1H'
    enableBatchedOperations: true
  }
}

// Phase 10: the orchestrator follows each remediation step through this and writes it onto the
// case, where the UI reads it. Separate from web-progress so neither consumer steals the other's.
resource orchestratorProgressSubscription 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2024-01-01' = {
  parent: eventsTopic
  name: 'orchestrator-progress'
  properties: {
    maxDeliveryCount: 5
    lockDuration: 'PT1M'
    defaultMessageTimeToLive: 'PT1H'
    deadLetteringOnMessageExpiration: false
  }
}

resource auditSubscription 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2024-01-01' = {
  parent: eventsTopic
  name: 'audit'
  properties: {
    maxDeliveryCount: 10
    lockDuration: 'PT1M'
    deadLetteringOnMessageExpiration: true
    defaultMessageTimeToLive: 'P7D'
    enableBatchedOperations: true
  }
}

output namespaceName string = namespace.name
output namespaceId string = namespace.id
output namespaceFqdn string = '${namespace.name}.servicebus.windows.net'
output commandsQueueName string = commandsQueue.name
output resultsQueueName string = resultsQueue.name
output eventsTopicName string = eventsTopic.name
output webProgressSubscriptionName string = webProgressSubscription.name
output auditSubscriptionName string = auditSubscription.name
