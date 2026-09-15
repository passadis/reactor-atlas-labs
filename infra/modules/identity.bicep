// Five user-assigned managed identities, one per compute boundary.
// No secrets anywhere in ATLAS: every service authenticates as one of these. CLAUDE.md section 1.

@description('Region for all identities.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('8-char deterministic suffix.')
param resourceToken string

@description('Abbreviation map from infra/abbreviations.json.')
param abbrs object

var names = {
  web: '${abbrs.managedIdentityUserAssignedIdentity}-atlas-web-${resourceToken}'
  orchestrator: '${abbrs.managedIdentityUserAssignedIdentity}-atlas-orch-${resourceToken}'
  worker: '${abbrs.managedIdentityUserAssignedIdentity}-atlas-worker-${resourceToken}'
  func: '${abbrs.managedIdentityUserAssignedIdentity}-atlas-func-${resourceToken}'
  logic: '${abbrs.managedIdentityUserAssignedIdentity}-atlas-logic-${resourceToken}'
}

resource web 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: names.web
  location: location
  tags: tags
}

resource orchestrator 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: names.orchestrator
  location: location
  tags: tags
}

resource worker 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: names.worker
  location: location
  tags: tags
}

resource func 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: names.func
  location: location
  tags: tags
}

resource logic 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: names.logic
  location: location
  tags: tags
}

output webId string = web.id
output webName string = web.name
output webClientId string = web.properties.clientId
output webPrincipalId string = web.properties.principalId

output orchestratorId string = orchestrator.id
output orchestratorName string = orchestrator.name
output orchestratorClientId string = orchestrator.properties.clientId
output orchestratorPrincipalId string = orchestrator.properties.principalId

output workerId string = worker.id
output workerName string = worker.name
output workerClientId string = worker.properties.clientId
output workerPrincipalId string = worker.properties.principalId

output funcId string = func.id
output funcName string = func.name
output funcClientId string = func.properties.clientId
output funcPrincipalId string = func.properties.principalId

output logicId string = logic.id
output logicName string = logic.name
output logicClientId string = logic.properties.clientId
output logicPrincipalId string = logic.properties.principalId
