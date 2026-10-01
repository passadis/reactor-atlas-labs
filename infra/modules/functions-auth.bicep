// Platform Entra authentication on both Function Apps, locked to the API Management identity.
//
// Phase 08 put Entra auth on the ops API with an audience check only: any identity in the tenant
// holding a token for api://atlas-ops-api got in. Phase 09's claim is stronger - no agent reaches a
// backend except through APIM - so both apps now accept exactly one caller, APIM's system-assigned
// identity (`allowedPrincipals`). A valid token from anyone else is refused by the platform
// before any code runs.
//
// The MCP app joins it. Its webhook drops the `mcp_extension` system key (host setting
// webhookAuthorizationLevel=Anonymous, compute-functions.bicep) in favour of this, so there is no
// function key anywhere to leak; learn.microsoft.com/azure/azure-functions/functions-mcp-tutorial
// documents exactly this pairing.
//
// A separate module because APIM consumes the Function URLs and this consumes APIM's identity:
// splitting it is what breaks the cycle.

@description('Name of the ops API Function App.')
param apiAppName string

@description('Name of the MCP Function App.')
param mcpAppName string

@description('Client id of the atlas-ops-api registration: the audience both apps accept.')
param opsApiClientId string

@description('Object id of the API Management system-assigned identity - the only allowed caller.')
param apimPrincipalId string

@description('Name of the Logic Apps Standard site (Phase 10).')
param logicAppName string

resource apiApp 'Microsoft.Web/sites@2024-04-01' existing = {
  name: apiAppName
}

resource mcpApp 'Microsoft.Web/sites@2024-04-01' existing = {
  name: mcpAppName
}

// Two audiences: this registration issues v2 tokens, whose `aud` is the client id, not the api://
// URI (docs/API-NOTES.md 15.3).
var authSettings = {
  platform: {
    enabled: true
  }
  globalValidation: {
    requireAuthentication: true
    unauthenticatedClientAction: 'Return401'
    // Liveness stays open so probes and `azd` can tell a dead app from a locked one.
    excludedPaths: ['/api/health']
  }
  identityProviders: {
    azureActiveDirectory: {
      enabled: true
      registration: {
        clientId: opsApiClientId
        openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
      }
      validation: {
        allowedAudiences: [
          'api://atlas-ops-api'
          opsApiClientId
        ]
        defaultAuthorizationPolicy: {
          allowedPrincipals: {
            identities: [apimPrincipalId]
          }
        }
      }
    }
  }
  login: {
    tokenStore: {
      enabled: false
    }
  }
}

resource apiAuth 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: apiApp
  name: 'authsettingsV2'
  properties: authSettings
}

resource mcpAuth 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: mcpApp
  name: 'authsettingsV2'
  properties: authSettings
}

// Phase 10: the Logic App's Request triggers take Easy Auth instead of a SAS signature, locked to
// APIM like the Functions (learn.microsoft.com/azure/connectors/connectors-native-reqres, "Security
// and authentication"). One exception, deliberately: /runtime/webhooks/workflow is where the
// runtime serves webhook-action callbacks and its own management calls. A callback URL carries a
// per-run signature the platform issues for that one wait - a capability, not a stored secret.
resource logicApp 'Microsoft.Web/sites@2024-04-01' existing = {
  name: logicAppName
}

resource logicAuth 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: logicApp
  name: 'authsettingsV2'
  properties: union(authSettings, {
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
      // A WILDCARD. excludedPaths matches exactly: '/runtime/webhooks/workflow' alone excludes that
      // one path and still locks every callback and management call beneath it (API-NOTES 17.2).
      excludedPaths: ['/runtime/webhooks/workflow/*']
    }
  })
}
