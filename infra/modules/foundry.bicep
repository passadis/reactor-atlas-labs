// Microsoft Foundry account, project, model deployments, content filter, and project connections.
//
// Verified against the official template at
// microsoft-foundry/foundry-samples/infrastructure/infrastructure-setup-bicep/40-basic-agent-setup
// and 01-connections, plus live API-version discovery. See docs/API-NOTES.md.
//
// Two deliberate departures from the official samples, both driven by CLAUDE.md section 1:
//   1. The AI Search connection uses authType 'AAD', not 'ApiKey'. The sample calls
//      listAdminKeys() and stores the key in the connection. We do not put keys anywhere.
//   2. disableLocalAuth is true on the account, so the data plane is Entra-only.

@description('Region for the Foundry account and project.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('8-char deterministic suffix.')
param resourceToken string

@description('Abbreviation map from infra/abbreviations.json.')
param abbrs object

@description('Resource id of the Application Insights component to connect for agent tracing.')
param appInsightsId string

@description('Name of the Application Insights component.')
param appInsightsName string

@description('Resource id of the Azure AI Search service.')
param searchServiceId string

@description('Endpoint of the Azure AI Search service.')
param searchServiceEndpoint string

@description('Region of the Azure AI Search service. May differ from the account region.')
param searchServiceLocation string = location

// -- model parameters ------------------------------------------------------------------------
param gptMainModelName string
param gptMainModelVersion string
param gptMainCapacity int
param gptJudgeModelName string
param gptJudgeModelVersion string
param gptJudgeCapacity int
param embedModelName string
param embedModelVersion string
param embedCapacity int

var accountName = '${abbrs.cognitiveServicesAccount}-atlas-${resourceToken}'
var projectName = 'atlas-${resourceToken}'

// ---------------------------------------------------------------------------- account --

resource account 'Microsoft.CognitiveServices/accounts@2026-07-01' = {
  name: accountName
  location: location
  tags: tags
  sku: {
    name: 'S0'
  }
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // Required for the project child resource and therefore for Agent Service.
    allowProjectManagement: true
    customSubDomainName: accountName
    // Entra-only data plane. No keys, ever.
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      virtualNetworkRules: []
      ipRules: []
    }
  }
}

// ---------------------------------------------------------------------------- content filter --
// Attached to gpt-main below. Episode 1 must be able to demonstrate a refusal path, and Phase 03
// tests it. Prompt shields cover direct jailbreak and indirect (XPIA) attacks, which is what the
// injection_canary runbooks exercise in Episode 4.

resource raiPolicy 'Microsoft.CognitiveServices/accounts/raiPolicies@2026-07-01' = {
  parent: account
  name: 'atlas-strict'
  properties: {
    mode: 'Blocking'
    basePolicyName: 'Microsoft.DefaultV2'
    contentFilters: [
      { name: 'Hate', blocking: true, enabled: true, severityThreshold: 'Medium', source: 'Prompt' }
      { name: 'Hate', blocking: true, enabled: true, severityThreshold: 'Medium', source: 'Completion' }
      { name: 'Sexual', blocking: true, enabled: true, severityThreshold: 'Medium', source: 'Prompt' }
      { name: 'Sexual', blocking: true, enabled: true, severityThreshold: 'Medium', source: 'Completion' }
      { name: 'Violence', blocking: true, enabled: true, severityThreshold: 'Medium', source: 'Prompt' }
      { name: 'Violence', blocking: true, enabled: true, severityThreshold: 'Medium', source: 'Completion' }
      { name: 'Selfharm', blocking: true, enabled: true, severityThreshold: 'Medium', source: 'Prompt' }
      { name: 'Selfharm', blocking: true, enabled: true, severityThreshold: 'Medium', source: 'Completion' }
      // Prompt shields. The indirect attack filter is the one that matters for XPIA.
      { name: 'Jailbreak', blocking: true, enabled: true, source: 'Prompt' }
      { name: 'Indirect Attack', blocking: true, enabled: true, source: 'Prompt' }
      { name: 'Protected Material Text', blocking: true, enabled: true, source: 'Completion' }
      { name: 'Protected Material Code', blocking: false, enabled: true, source: 'Completion' }
    ]
  }
}

// ---------------------------------------------------------------------------- deployments --
// Model deployment names are STABLE LOGICAL ALIASES, never raw model names (CLAUDE.md section 3),
// so the underlying model can be swapped without touching code or APIM policy.
//
// These are chained with dependsOn rather than deployed in parallel. Concurrent deployments against
// one Cognitive Services account race on the account's capacity ledger and fail intermittently.

resource gptMain 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: 'gpt-main'
  sku: {
    name: 'GlobalStandard'
    capacity: gptMainCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: gptMainModelName
      version: gptMainModelVersion
    }
    raiPolicyName: raiPolicy.name
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource gptJudge 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: 'gpt-judge'
  sku: {
    name: 'GlobalStandard'
    capacity: gptJudgeCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: gptJudgeModelName
      version: gptJudgeModelVersion
    }
    raiPolicyName: raiPolicy.name
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [gptMain]
}

resource embedMain 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: 'embed-main'
  sku: {
    name: 'Standard'
    capacity: embedCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embedModelName
      version: embedModelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [gptJudge]
}

// ---------------------------------------------------------------------------- project --

// Chained behind the model deployments, not parallel with them.
//
// A Cognitive Services account accepts ONE mutating operation at a time. The project is a child of
// the account just as the deployments are, so ARM happily starts both at once and the second one
// fails with RequestConflict: "Another operation is in progress on the resource". Observed on the
// first real provision, 2026-09-15. The full serial chain is:
//   account -> raiPolicy -> gpt-main -> gpt-judge -> embed-main -> project -> connections
resource project 'Microsoft.CognitiveServices/accounts/projects@2026-07-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  dependsOn: [embedMain]
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'ATLAS incident copilot'
    description: 'Contoso Cloud Operations incident copilot. Microsoft Reactor series.'
  }
}

// ---------------------------------------------------------------------------- connections --

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

// Application Insights connection, at project scope, so agent traces land in the same workspace as
// every other hop. The platform requires the connection string as the credential here; there is no
// AAD auth type for AppInsights connections. It is read from the resource at deploy time, never
// hardcoded and never emitted as an output. Documented as the second permitted secret in
// docs/API-NOTES.md.
resource projectAppInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2026-05-01' = {
  parent: project
  name: 'appinsights'
  properties: {
    category: 'AppInsights'
    target: appInsightsId
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsightsId
    }
  }
}

// Azure AI Search connection using Entra, not an admin key. The project's system-assigned identity
// gets Search Index Data Reader and Search Service Contributor in rbac.bicep.
resource projectSearchConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2026-05-01' = {
  parent: project
  name: 'aisearch'
  properties: {
    category: 'CognitiveSearch'
    target: searchServiceEndpoint
    authType: 'AAD'
    isSharedToAll: true
    metadata: {
      ApiType: 'Azure'
      ResourceId: searchServiceId
      location: searchServiceLocation
    }
  }
  dependsOn: [projectAppInsightsConnection]
}

// ---------------------------------------------------------------------------- outputs --

output accountName string = account.name
output accountId string = account.id
// NOTE: account.properties.endpoint is https://<acct>.cognitiveservices.azure.com/ which is NOT the
// project endpoint. The agent stack needs the AI Foundry API endpoint below. docs/API-NOTES.md.
output accountEndpoint string = account.properties.endpoint
output accountPrincipalId string = account.identity.principalId

output projectName string = project.name
output projectId string = project.id
output projectPrincipalId string = project.identity.principalId

@description('FOUNDRY_PROJECT_ENDPOINT. Verified format: https://<account>.services.ai.azure.com/api/projects/<project>')
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'

output gptMainDeploymentName string = gptMain.name
output gptJudgeDeploymentName string = gptJudge.name
output embedDeploymentName string = embedMain.name
output raiPolicyName string = raiPolicy.name
output searchConnectionName string = projectSearchConnection.name
output appInsightsConnectionName string = projectAppInsightsConnection.name
