// ATLAS platform infrastructure. Subscription-scoped: creates the resource group, then calls
// every module. See CLAUDE.md section 2 for the module inventory and section 7 for the verified
// deviations that shaped it.
//
// The Foundry hosted agent is NOT created here. It lives in ../agents (a separate azd project with
// infra.provider = microsoft.foundry) because infra.provider is one value per azure.yaml.
// See CLAUDE.md section 7.3.

targetScope = 'subscription'

// ---------------------------------------------------------------------------- parameters --

@minLength(1)
@maxLength(24)
@description('Name of the azd environment. Drives resource naming and the azd-env-name tag.')
param environmentName string

@minLength(1)
@description('Primary region. eastus2 is the verified default - Sweden Central cannot run the AI Red Teaming Agent (CLAUDE.md 7.1).')
@allowed([
  'eastus2'
  'swedencentral'
])
param location string = 'eastus2'

@description('Object id of the principal running the deployment. azd supplies AZURE_PRINCIPAL_ID. Gets data-plane roles so you can seed and debug locally.')
param principalId string = ''

@description('Type of the deploying principal. Use ServicePrincipal in CI, User locally.')
@allowed([
  'User'
  'ServicePrincipal'
])
param principalType string = 'User'

@description('Owner tag. Purely informational.')
param owner string = 'reactor-demo'

@description('ISO date after which this footprint can be deleted. Informational tag only - nothing enforces it.')
param deleteAfter string = '2026-12-31'

@description('Email for the ops action group. Placeholder by default; alerts still deploy.')
param opsEmail string = 'atlas-ops@contoso.example'

// -- model deployment parameters -------------------------------------------------------------
// Resolved against `az cognitiveservices model list -l <location>` and pinned in
// docs/API-NOTES.md section 5. Deployment fails loudly if a model is unavailable rather than
// silently substituting one.

@description('Model backing the gpt-main alias.')
param gptMainModelName string = 'gpt-5.4'
@description('Version of the gpt-main model.')
param gptMainModelVersion string = '2026-03-05'
@description('Capacity for gpt-main, in units of 1000 TPM. eastus2 limit is 10000 units.')
param gptMainCapacity int = 200

@description('Model backing the gpt-judge alias. Deliberately a different family so eval runs never starve the demo.')
param gptJudgeModelName string = 'gpt-5.4-mini'
@description('Version of the gpt-judge model.')
param gptJudgeModelVersion string = '2026-03-17'
@description('Capacity for gpt-judge, in units of 1000 TPM.')
param gptJudgeCapacity int = 100

@description('Embedding model backing the embed-main alias. Produces 3072-dimension vectors (CLAUDE.md 7.11).')
param embedModelName string = 'text-embedding-3-large'
@description('Version of the embedding model.')
param embedModelVersion string = '1'
@description('Capacity for embed-main, in units of 1000 TPM. Standard SKU limit is 350 units - the tightest of the three.')
param embedCapacity int = 120

@description('Deploy the optional Azure Managed Redis used for APIM semantic caching. Adds roughly 25 minutes to provisioning.')
param enableSemanticCache bool = false

// ---------------------------------------------------------------------------- variables --

// 8 lowercase chars, deterministic per (subscription, env, location). Every name derives from it.
var resourceToken = toLower(take(uniqueString(subscription().id, environmentName, location), 8))
var abbrs = loadJsonContent('./abbreviations.json')
var rgName = 'rg-atlas-${environmentName}'

var tags = {
  'azd-env-name': environmentName
  project: 'atlas'
  owner: owner
  purpose: 'reactor-demo'
  deleteAfter: deleteAfter
}

// ---------------------------------------------------------------------------- resource group --

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: rgName
  location: location
  tags: tags
}

// ---------------------------------------------------------------------------- modules --

// Five user-assigned identities, one per compute boundary. Everything else depends on these.
module identity './modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    abbrs: abbrs
  }
}

// Log Analytics + Application Insights + action group. Needed by Foundry, ACA, Functions and
// Logic Apps, so it comes early.
module observability './modules/observability.bicep' = {
  name: 'observability'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    abbrs: abbrs
    opsEmail: opsEmail
  }
}

// Cosmos, AI Search and Storage. Foundry connects to Search, so this runs before it.
module data './modules/data.bicep' = {
  name: 'data'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    abbrs: abbrs
  }
}

// Foundry account, project, the three model-deployment aliases, the content filter, and the
// App Insights + AI Search project connections.
module foundry './modules/foundry.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    abbrs: abbrs
    appInsightsId: observability.outputs.appInsightsId
    appInsightsName: observability.outputs.appInsightsName
    searchServiceId: data.outputs.searchServiceId
    searchServiceEndpoint: data.outputs.searchServiceEndpoint
    gptMainModelName: gptMainModelName
    gptMainModelVersion: gptMainModelVersion
    gptMainCapacity: gptMainCapacity
    gptJudgeModelName: gptJudgeModelName
    gptJudgeModelVersion: gptJudgeModelVersion
    gptJudgeCapacity: gptJudgeCapacity
    embedModelName: embedModelName
    embedModelVersion: embedModelVersion
    embedCapacity: embedCapacity
  }
}

// ---------------------------------------------------------------------------- outputs --

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output AZURE_RESOURCE_GROUP string = rg.name
output ATLAS_RESOURCE_TOKEN string = resourceToken
output ATLAS_ENV_NAME string = environmentName
output ATLAS_PRINCIPAL_ID string = principalId
output ATLAS_PRINCIPAL_TYPE string = principalType

// Managed identity client ids. Containers read these as AZURE_CLIENT_ID so DefaultAzureCredential
// picks the right user-assigned identity (CLAUDE.md section 1).
output ATLAS_WEB_IDENTITY_CLIENT_ID string = identity.outputs.webClientId
output ATLAS_ORCHESTRATOR_IDENTITY_CLIENT_ID string = identity.outputs.orchestratorClientId
output ATLAS_WORKER_IDENTITY_CLIENT_ID string = identity.outputs.workerClientId
output ATLAS_FUNC_IDENTITY_CLIENT_ID string = identity.outputs.funcClientId
output ATLAS_LOGIC_IDENTITY_CLIENT_ID string = identity.outputs.logicClientId

output ATLAS_WEB_IDENTITY_PRINCIPAL_ID string = identity.outputs.webPrincipalId
output ATLAS_ORCHESTRATOR_IDENTITY_PRINCIPAL_ID string = identity.outputs.orchestratorPrincipalId
output ATLAS_WORKER_IDENTITY_PRINCIPAL_ID string = identity.outputs.workerPrincipalId
output ATLAS_FUNC_IDENTITY_PRINCIPAL_ID string = identity.outputs.funcPrincipalId
output ATLAS_LOGIC_IDENTITY_PRINCIPAL_ID string = identity.outputs.logicPrincipalId

output ATLAS_LOG_ANALYTICS_ID string = observability.outputs.logAnalyticsId
output ATLAS_APPINSIGHTS_NAME string = observability.outputs.appInsightsName
output ATLAS_APPINSIGHTS_ID string = observability.outputs.appInsightsId
// Not a secret in the CLAUDE.md sense, but never printed on stage. See docs/API-NOTES.md.
output APPLICATIONINSIGHTS_CONNECTION_STRING string = observability.outputs.appInsightsConnectionString

output ATLAS_ENABLE_SEMANTIC_CACHE bool = enableSemanticCache

output ATLAS_COSMOS_ACCOUNT_NAME string = data.outputs.cosmosAccountName
output ATLAS_COSMOS_ENDPOINT string = data.outputs.cosmosEndpoint
output ATLAS_COSMOS_DATABASE string = data.outputs.cosmosDatabaseName
output ATLAS_SEARCH_SERVICE_NAME string = data.outputs.searchServiceName
output ATLAS_SEARCH_ENDPOINT string = data.outputs.searchServiceEndpoint
output ATLAS_STORAGE_ACCOUNT_NAME string = data.outputs.storageAccountName
output ATLAS_STORAGE_BLOB_ENDPOINT string = data.outputs.storageBlobEndpoint

output ATLAS_FOUNDRY_ACCOUNT_NAME string = foundry.outputs.accountName
output ATLAS_FOUNDRY_PROJECT_NAME string = foundry.outputs.projectName
output ATLAS_FOUNDRY_PROJECT_PRINCIPAL_ID string = foundry.outputs.projectPrincipalId
// The env var the agent stack reads. Format verified against a live project; note this is NOT
// account.properties.endpoint, which points at a different host. docs/API-NOTES.md.
output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output FOUNDRY_MODEL_DEPLOYMENT string = foundry.outputs.gptMainDeploymentName
output ATLAS_MODEL_JUDGE string = foundry.outputs.gptJudgeDeploymentName
output ATLAS_MODEL_EMBED string = foundry.outputs.embedDeploymentName
output ATLAS_RAI_POLICY_NAME string = foundry.outputs.raiPolicyName
