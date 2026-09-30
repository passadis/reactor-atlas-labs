// API Management StandardV2: the AI gateway.
//
// Created in Phase 01 rather than Phase 09 because it is the slowest resource in the footprint
// (15-45 minutes). The APIs, MCP servers and policy XML land in Phase 09; what exists here is the
// instance, its identity, the Application Insights logger, the product, and global diagnostics.
//
// StandardV2 is verified to support both MCP servers and every llm-* policy. See
// docs/API-NOTES.md section 6.

@description('Region for the APIM instance.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('8-char deterministic suffix.')
param resourceToken string

@description('Abbreviation map from infra/abbreviations.json.')
param abbrs object

@description('Publisher email shown on the developer portal. Synthetic domain only.')
param publisherEmail string = 'atlas-ops@contoso.example'

@description('Publisher organisation name.')
param publisherName string = 'Contoso Cloud Operations'

@description('Resource id of the Application Insights component.')
param appInsightsId string

@description('Application Insights connection string for the APIM logger.')
@secure()
param appInsightsConnectionString string

@description('Scale units. 1 is right for a demo and provisions fastest.')
param capacity int = 1

// -- backend URLs, stored as non-secret named values so Phase 09 policies reference them ------
param opsApiUrl string
param opsMcpUrl string
param foundryAccountEndpoint string

var apimName = '${abbrs.apiManagementService}-atlas-${resourceToken}'

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: 'StandardV2'
    capacity: capacity
  }
  identity: {
    // System-assigned: APIM presents this identity to the Functions backends and to Foundry.
    // The model never holds a credential; APIM does. That is the Episode 3 thesis.
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------------------- named values --

// Non-secret backend URLs. Policy XML in Phase 09 references these as {{ops-api-url}} etc., so a
// backend can move without editing policy.
var backendUrls = [
  { name: 'ops-api-url', value: opsApiUrl }
  { name: 'ops-mcp-url', value: opsMcpUrl }
  { name: 'foundry-endpoint', value: foundryAccountEndpoint }
]

resource namedValues 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = [
  for nv in backendUrls: {
    parent: apim
    name: nv.name
    properties: {
      displayName: nv.name
      value: nv.value
      secret: false
    }
  }
]

// The App Insights connection string, held as a secret named value rather than inline in the
// logger, so it never appears in a policy document or a diagnostic export.
resource appInsightsNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'appinsights-connection-string'
  properties: {
    displayName: 'appinsights-connection-string'
    value: appInsightsConnectionString
    secret: true
  }
}

// ---------------------------------------------------------------------------- logger --

resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'atlas-appinsights'
  properties: {
    loggerType: 'applicationInsights'
    description: 'ATLAS Application Insights logger'
    resourceId: appInsightsId
    credentials: {
      connectionString: '{{appinsights-connection-string}}'
    }
    isBuffered: true
  }
  dependsOn: [appInsightsNamedValue]
}

// ---------------------------------------------------------------------------- diagnostics --

// Global diagnostics.
//
// httpCorrelationProtocol W3C is what keeps `traceparent` intact across the gateway. Without it
// APIM would mint its own correlation scheme and the Episode 4 six-hop waterfall would break in
// the middle. CLAUDE.md section 1 makes this a bug, not a preference.
//
// Frontend and backend response payload logging are set to 0 bytes globally. The pack asserts this
// is required for MCP servers to stream correctly; that claim is UNVERIFIED (docs/API-NOTES.md
// section 6). It is set to 0 here regardless because logging response bodies globally is the wrong
// default for a system carrying incident data. Phase 09 raises it per-API where it is wanted.
resource diagnostics 'Microsoft.ApiManagement/service/diagnostics@2024-05-01' = {
  parent: apim
  name: 'applicationinsights'
  properties: {
    loggerId: logger.id
    alwaysLog: 'allErrors'
    httpCorrelationProtocol: 'W3C'
    verbosity: 'information'
    logClientIp: true
    // Phase 09: emit-metric and llm-emit-token-metric write nothing without this. The metrics and
    // every dimension (CaseId, AgentName) land in the App Insights customMetrics table.
    metrics: true
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    frontend: {
      request: {
        headers: ['traceparent', 'x-atlas-correlation-id', 'x-atlas-case-id']
        body: {
          bytes: 0
        }
      }
      response: {
        headers: ['traceparent', 'x-atlas-correlation-id']
        body: {
          bytes: 0
        }
      }
    }
    backend: {
      request: {
        headers: ['traceparent', 'x-atlas-correlation-id', 'x-atlas-case-id']
        body: {
          bytes: 0
        }
      }
      response: {
        headers: ['traceparent', 'x-atlas-correlation-id']
        body: {
          bytes: 0
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------- products --

// The agents' product. Subscription required so token limits and quotas have a key to count
// against; approval not required so provisioning stays scriptable.
resource agentToolsProduct 'Microsoft.ApiManagement/service/products@2024-05-01' = {
  parent: apim
  name: 'atlas-agent-tools'
  properties: {
    displayName: 'ATLAS agent tools'
    description: 'Tools and model access for ATLAS agents. Every agent call enters through here.'
    subscriptionRequired: true
    approvalRequired: false
    subscriptionsLimit: 10
    state: 'published'
  }
}

// Read-only product used for inspection during the demos, so the audience can see a call that
// is authenticated but cannot act.
resource readonlyProduct 'Microsoft.ApiManagement/service/products@2024-05-01' = {
  parent: apim
  name: 'atlas-readonly'
  properties: {
    displayName: 'ATLAS read-only'
    description: 'Read-only access for demonstration and inspection.'
    subscriptionRequired: true
    approvalRequired: false
    subscriptionsLimit: 10
    state: 'published'
  }
}

output apimName string = apim.name
output apimId string = apim.id
output apimGatewayUrl string = apim.properties.gatewayUrl
output apimPrincipalId string = apim.identity.principalId
output apimLoggerId string = logger.id
output agentToolsProductName string = agentToolsProduct.name
output readonlyProductName string = readonlyProduct.name
