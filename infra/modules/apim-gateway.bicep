// The AI gateway's contents (Phase 09): four APIs, the policies, the products' subscriptions, and
// the one permitted secret - the subscription key - in Key Vault.
//
// apim.bicep owns the slow part (the instance, logger, global diagnostics, products); this module
// owns everything that changes with the application, so a policy edit never touches the instance.
//
//   atlas-ops-api    /ops              the ops API, imported from the hand-written OpenAPI spec
//   atlas-ops-mcp    /ops-mcp/mcp      REST-as-MCP over atlas-ops-api; tool descriptions live here
//   atlas-tools-mcp  /tools-mcp/mcp    passthrough to the Functions MCP server
//   atlas-models     /models           the Foundry model endpoint (OpenAI v1)
//
// Every policy is a file under apim/policies, loaded verbatim. MCP resources need API version
// 2025-09-01-preview or later (learn.microsoft.com/azure/api-management/manage-mcp-servers-rest-api).

@description('Name of the existing API Management instance.')
param apimName string

@description('Entra tenant id, for validate-jwt.')
param tenantId string

@description('Client id of the atlas-gateway app registration (the v2 token audience).')
param gatewayAppId string

@description('Base URL of the ops API Function App, including /api.')
param opsApiBaseUrl string

@description('Base URL of the MCP Function App (the MCP webhook lives under /runtime/webhooks/mcp).')
param mcpAppBaseUrl string

@description('Foundry account name; the model API forwards to its OpenAI v1 endpoint.')
param foundryAccountName string

@description('Base URL of the Logic Apps Standard site (Phase 10).')
param logicAppBaseUrl string

@description('Key Vault that holds the subscription keys.')
param keyVaultName string

@description('Application Insights logger id (from apim.bicep).')
param loggerId string

param agentToolsProductName string = 'atlas-agent-tools'
param readonlyProductName string = 'atlas-readonly'

var mcpTools = loadJsonContent('../../apim/mcp/atlas-ops-mcp-tools.json').tools

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource agentToolsProduct 'Microsoft.ApiManagement/service/products@2024-05-01' existing = {
  parent: apim
  name: agentToolsProductName
}

resource readonlyProduct 'Microsoft.ApiManagement/service/products@2024-05-01' existing = {
  parent: apim
  name: readonlyProductName
}

// ------------------------------------------------------------------------ named values --

resource tenantNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'tenant-id'
  properties: { displayName: 'tenant-id', value: tenantId, secret: false }
}

resource gatewayAppNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'gateway-app-id'
  properties: { displayName: 'gateway-app-id', value: gatewayAppId, secret: false }
}

// ------------------------------------------------------------------------ fragments --

var fragments = [
  { name: 'atlas-correlation', value: loadTextContent('../../apim/policies/fragments/atlas-correlation.xml') }
  { name: 'atlas-agent-auth', value: loadTextContent('../../apim/policies/fragments/atlas-agent-auth.xml') }
  { name: 'atlas-error-envelope', value: loadTextContent('../../apim/policies/fragments/atlas-error-envelope.xml') }
]

resource policyFragments 'Microsoft.ApiManagement/service/policyFragments@2024-05-01' = [
  for f in fragments: {
    parent: apim
    name: f.name
    properties: {
      format: 'rawxml'
      value: f.value
    }
    dependsOn: [tenantNamedValue, gatewayAppNamedValue]
  }
]

resource globalPolicy 'Microsoft.ApiManagement/service/policies@2024-05-01' = {
  parent: apim
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../../apim/policies/global.xml')
  }
  dependsOn: [policyFragments]
}

// ------------------------------------------------------------------------ backends --

resource opsApiBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = {
  parent: apim
  name: 'ops-api-backend'
  properties: {
    description: 'func-atlas-api. Accepts only this gateway\'s managed identity.'
    url: opsApiBaseUrl
    protocol: 'http'
    tls: { validateCertificateChain: true, validateCertificateName: true }
  }
}

resource foundryBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = {
  parent: apim
  name: 'foundry-openai-backend'
  properties: {
    description: 'Foundry OpenAI v1 endpoint. The gateway authenticates as itself.'
    url: 'https://${foundryAccountName}.openai.azure.com/openai/v1'
    protocol: 'http'
    tls: { validateCertificateChain: true, validateCertificateName: true }
  }
}

// ------------------------------------------------------------------------ atlas-ops-api --

resource opsApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'atlas-ops-api'
  properties: {
    displayName: 'ATLAS operations API'
    description: 'The governed business API. Every agent tool call enters here.'
    path: 'ops'
    protocols: ['https']
    format: 'openapi'
    value: loadTextContent('../../apim/openapi/atlas-ops-api.yaml')
    subscriptionRequired: true
    subscriptionKeyParameterNames: { header: 'Ocp-Apim-Subscription-Key', query: 'subscription-key' }
  }
}

resource opsApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: opsApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../../apim/policies/ops-api.xml')
  }
  dependsOn: [policyFragments, opsApiBackend]
}

// ------------------------------------------------------------------------ atlas-ops-mcp --

// Subscription not required on either MCP server: the Foundry toolbox reaches them through a
// project connection that carries ONE credential, and that credential is the project's managed
// identity (docs/API-NOTES.md 16). A subscription key there would live outside Key Vault. The
// servers are gated by identity instead - Entra token plus the Atlas.Agent role - and rate
// limited per calling agent.
resource opsMcp 'Microsoft.ApiManagement/service/apis@2025-09-01-preview' = {
  #disable-next-line use-parent-property
  name: '${apim.name}/atlas-ops-mcp'
  properties: {
    type: 'mcp'
    displayName: 'ATLAS ops tools (MCP)'
    description: 'The operations API as MCP tools. Tool descriptions are governed here.'
    path: 'ops-mcp'
    protocols: ['https']
    subscriptionRequired: false
  }
  dependsOn: [opsApi]
}

resource opsMcpTools 'Microsoft.ApiManagement/service/apis/tools@2025-09-01-preview' = [
  for t in mcpTools: {
    #disable-next-line use-parent-property
    name: '${apim.name}/atlas-ops-mcp/${t.name}'
    properties: {
      displayName: t.name
      description: t.description
      operationId: resourceId('Microsoft.ApiManagement/service/apis/operations', apim.name, t.?api ?? opsApi.name, t.operation)
    }
    dependsOn: [opsMcp, workflowsApi, workflowsOperations]
  }
]

resource opsMcpPolicy 'Microsoft.ApiManagement/service/apis/policies@2025-09-01-preview' = {
  #disable-next-line use-parent-property
  name: '${apim.name}/atlas-ops-mcp/policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../../apim/policies/ops-mcp.xml')
  }
  dependsOn: [opsMcp, policyFragments, agentKeyNamedValue]
}

// ------------------------------------------------------------------------ atlas-tools-mcp --

// The MCP Function App accepts only this gateway's identity. The identity MUST live on a backend
// entity, not only in the API policy: APIM discovers a passthrough server's tools itself, outside
// the request pipeline, so a policy-level token swap never reaches that discovery call - the Function
// App 401s it and the server lists no tools. Found 2026-09-30 (docs/API-NOTES.md 16.9).
resource toolsMcpBackend 'Microsoft.ApiManagement/service/backends@2025-09-01-preview' = {
  #disable-next-line use-parent-property
  name: '${apim.name}/tools-mcp-backend'
  properties: {
    description: 'func-atlas-mcp. Accepts only this gateway managed identity.'
    url: '${mcpAppBaseUrl}/runtime/webhooks'
    protocol: 'http'
    credentials: {
      managedIdentity: {
        resource: 'api://atlas-ops-api'
      }
    }
  }
}

resource toolsMcp 'Microsoft.ApiManagement/service/apis@2025-09-01-preview' = {
  #disable-next-line use-parent-property
  name: '${apim.name}/atlas-tools-mcp'
  properties: {
    type: 'mcp'
    displayName: 'ATLAS tools (Functions MCP)'
    description: 'The Functions MCP server, governed. Passthrough over streamable HTTP.'
    path: 'tools-mcp'
    protocols: ['https']
    backendId: 'tools-mcp-backend'
    subscriptionRequired: false
    mcpProperties: {
      transportType: 'streamable'
      // A DICTIONARY keyed by endpoint name. Learn's manage-mcp-servers-rest-api shows an array of
      // {name, uriTemplate}; the live API (2025-09-01-preview, 2026-09-30) refuses that with
      // "Cannot deserialize the current JSON array ... McpEndpointContract". API-NOTES 16.
      endpoints: {
        message: { uriTemplate: '/mcp' }
      }
    }
  }
  dependsOn: [toolsMcpBackend]
}

resource toolsMcpPolicy 'Microsoft.ApiManagement/service/apis/policies@2025-09-01-preview' = {
  #disable-next-line use-parent-property
  name: '${apim.name}/atlas-tools-mcp/policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../../apim/policies/tools-mcp.xml')
  }
  dependsOn: [toolsMcp, policyFragments]
}

// Tool arguments and results logged for THIS server only, for the Episode 4 traces. They can carry
// incident data; docs/DEMO-NOTES.md says so. Global body logging stays at 0 bytes (apim.bicep).
resource toolsMcpDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2025-09-01-preview' = {
  #disable-next-line use-parent-property
  name: '${apim.name}/atlas-tools-mcp/applicationinsights'
  properties: {
    loggerId: loggerId
    alwaysLog: 'allErrors'
    httpCorrelationProtocol: 'W3C'
    verbosity: 'information'
    sampling: { samplingType: 'fixed', percentage: 100 }
    frontend: {
      request: { headers: ['traceparent', 'x-atlas-correlation-id'], body: { bytes: 8192 } }
      response: { headers: ['x-atlas-correlation-id'], body: { bytes: 8192 } }
    }
    backend: {
      request: { headers: ['traceparent', 'x-atlas-correlation-id'], body: { bytes: 8192 } }
      response: { headers: ['x-atlas-correlation-id'], body: { bytes: 8192 } }
    }
  }
  dependsOn: [toolsMcp]
}

// ------------------------------------------------------------------------ atlas-workflows --

// Phase 10. The Logic Apps Standard workflows, gated like the tools. The Logic App accepts only
// APIM's managed identity (functions-auth.bicep), so there is no SAS signature anywhere.
resource logicAppBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = {
  parent: apim
  name: 'logicapp-backend'
  properties: {
    description: 'Logic Apps Standard. Accepts only this gateway managed identity.'
    url: logicAppBaseUrl
    protocol: 'http'
    tls: { validateCertificateChain: true, validateCertificateName: true }
  }
}

resource workflowsApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'atlas-workflows'
  properties: {
    displayName: 'ATLAS workflows'
    description: 'Logic Apps Standard workflows: the approval run and incident enrichment.'
    path: 'workflows'
    protocols: ['https']
    subscriptionRequired: true
    subscriptionKeyParameterNames: { header: 'Ocp-Apim-Subscription-Key', query: 'subscription-key' }
  }
}

resource workflowsPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: workflowsApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../../apim/policies/workflows.xml')
  }
  dependsOn: [policyFragments, logicAppBackend]
}

var workflowOperations = [
  {
    name: 'start-approval'
    method: 'POST'
    url: '/approvals'
    display: 'Start an approval run'
    description: 'Start the approve-remediation workflow for a parked case. Orchestrator only.'
    query: []
    policy: loadTextContent('../../apim/policies/workflows-start-approval.xml')
  }
  {
    name: 'enrich'
    method: 'GET'
    url: '/enrich'
    display: 'Enrich an incident'
    description: 'Run the enrich-incident workflow for one service and return the aggregate.'
    query: [
      { name: 'service_id', type: 'string', required: true, description: 'Exact catalogue id, for example svc-payments.' }
      { name: 'window_minutes', type: 'integer', required: false, description: 'How far back to look. Default 60.' }
    ]
    policy: loadTextContent('../../apim/policies/workflows-enrich.xml')
  }
]

resource workflowsOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [
  for op in workflowOperations: {
    parent: workflowsApi
    name: op.name
    properties: {
      displayName: op.display
      description: op.description
      method: op.method
      urlTemplate: op.url
      request: {
        queryParameters: [for q in op.query: {
          name: q.name
          type: q.type
          required: q.required
          description: q.description
        }]
      }
    }
  }
]

resource workflowsOperationPolicies 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = [
  for (op, i) in workflowOperations: {
    parent: workflowsOperations[i]
    name: 'policy'
    properties: {
      format: 'rawxml'
      value: op.policy
    }
  }
]

// ------------------------------------------------------------------------ atlas-models --

resource modelsApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'atlas-models'
  properties: {
    displayName: 'ATLAS models'
    description: 'Foundry model deployments (OpenAI v1), with token limits and token metrics.'
    path: 'models'
    protocols: ['https']
    subscriptionRequired: true
    subscriptionKeyParameterNames: { header: 'Ocp-Apim-Subscription-Key', query: 'subscription-key' }
  }
}

var modelOperations = [
  { name: 'create-response', method: 'POST', url: '/responses', display: 'Create a response' }
  { name: 'create-chat-completion', method: 'POST', url: '/chat/completions', display: 'Create a chat completion' }
]

resource modelsOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [
  for op in modelOperations: {
    parent: modelsApi
    name: op.name
    properties: {
      displayName: op.display
      method: op.method
      urlTemplate: op.url
    }
  }
]

resource modelsPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: modelsApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../../apim/policies/models.xml')
  }
  dependsOn: [policyFragments, foundryBackend]
}

// ------------------------------------------------------------------------ products --

var agentProductApis = ['atlas-ops-api', 'atlas-ops-mcp', 'atlas-tools-mcp', 'atlas-models', 'atlas-workflows']
var readonlyProductApis = ['atlas-ops-api', 'atlas-models']

resource agentProductBindings 'Microsoft.ApiManagement/service/products/apis@2025-09-01-preview' = [
  for a in agentProductApis: {
    #disable-next-line use-parent-property
    name: '${apim.name}/${agentToolsProductName}/${a}'
    dependsOn: [opsApi, opsMcp, toolsMcp, modelsApi, workflowsApi]
  }
]

resource readonlyProductBindings 'Microsoft.ApiManagement/service/products/apis@2025-09-01-preview' = [
  for a in readonlyProductApis: {
    #disable-next-line use-parent-property
    name: '${apim.name}/${readonlyProductName}/${a}'
    dependsOn: [opsApi, modelsApi]
  }
]

resource readonlyProductPolicy 'Microsoft.ApiManagement/service/products/policies@2024-05-01' = {
  parent: readonlyProduct
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../../apim/policies/product-readonly.xml')
  }
  dependsOn: [policyFragments]
}

// ------------------------------------------------------------------------ subscriptions --

resource agentSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: 'atlas-agents'
  properties: {
    displayName: 'ATLAS agents'
    scope: agentToolsProduct.id
    state: 'active'
    allowTracing: false
  }
}

resource readonlySubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: 'atlas-inspect'
  properties: {
    displayName: 'ATLAS read-only inspection'
    scope: readonlyProduct.id
    state: 'active'
    allowTracing: false
  }
}

// The one permitted secret (CLAUDE.md section 1). Written straight from the subscription into Key
// Vault inside the deployment: it never appears in source, in azd env, or in any app setting.
// Workloads read it at startup by secret URI with their own identity.
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource agentKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'apim-agent-key'
  properties: {
    value: agentSubscription.listSecrets().primaryKey
    contentType: 'APIM subscription key: atlas-agents (product atlas-agent-tools)'
  }
}

resource readonlyKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'apim-readonly-key'
  properties: {
    value: readonlySubscription.listSecrets().primaryKey
    contentType: 'APIM subscription key: atlas-inspect (product atlas-readonly)'
  }
}

// The agents' key as a named value, Key Vault-backed: read by the gateway's own identity, never
// in source or settings. Used ONLY by the MCP scope (apim/policies/ops-mcp.xml), AFTER it has
// validated the caller's token and Atlas.Agent role: a tool call from an MCP client (the Foundry
// toolbox) carries no subscription key, and the ops and workflows APIs require one
// (2026-10-08: every prompt-agent tool call returned 401 SubscriptionKeyNotFound).
resource agentKeyNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'agents-subscription-key'
  properties: {
    displayName: 'agents-subscription-key'
    secret: true
    keyVault: { secretIdentifier: '${keyVault.properties.vaultUri}secrets/apim-agent-key' }
  }
  dependsOn: [agentKeySecret]
}

output opsUrl string = '${apim.properties.gatewayUrl}/ops'
output opsMcpUrl string = '${apim.properties.gatewayUrl}/ops-mcp/mcp'
output toolsMcpUrl string = '${apim.properties.gatewayUrl}/tools-mcp/mcp'
output modelsUrl string = '${apim.properties.gatewayUrl}/models'
output agentKeySecretUri string = '${keyVault.properties.vaultUri}secrets/apim-agent-key'
output readonlyKeySecretUri string = '${keyVault.properties.vaultUri}secrets/apim-readonly-key'
