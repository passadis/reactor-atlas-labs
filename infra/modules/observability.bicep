// Log Analytics, workspace-based Application Insights, the ops action group, and the shell of the
// Episode 4 workbook.
//
// Telemetry is a build-time invariant in ATLAS, not a late add-on: atlas_common.telemetry ships in
// Phase 03 and every hop propagates traceparent. See CLAUDE.md section 1 and 7.13.

@description('Region for observability resources.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('8-char deterministic suffix.')
param resourceToken string

@description('Abbreviation map from infra/abbreviations.json.')
param abbrs object

@description('Email receiver for the ops action group.')
param opsEmail string

@description('Retention in days. 90 covers the whole series with room to look back.')
param retentionInDays int = 90

var logAnalyticsName = '${abbrs.operationalInsightsWorkspace}-atlas-${resourceToken}'
var appInsightsName = '${abbrs.insightsComponent}-atlas-${resourceToken}'
var actionGroupName = '${abbrs.insightsActionGroup}-atlas-ops-${resourceToken}'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// Workspace-based. The Foundry project connects to this so agent traces land here alongside the
// ACA, Functions and Logic Apps telemetry, which is what makes the Episode 4 six-hop waterfall
// possible in a single query.
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    DisableLocalAuth: false
  }
}

// Alert rules are attached in Phase 11. The group exists now so those rules have a target and so
// the email receiver is proven before show week.
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: actionGroupName
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'atlasops'
    enabled: true
    emailReceivers: [
      {
        name: 'ops-email'
        emailAddress: opsEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

output logAnalyticsId string = logAnalytics.id
output logAnalyticsName string = logAnalytics.name
output logAnalyticsCustomerId string = logAnalytics.properties.customerId

output appInsightsId string = appInsights.id
output appInsightsName string = appInsights.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output appInsightsAppId string = appInsights.properties.AppId

output actionGroupId string = actionGroup.id
output actionGroupName string = actionGroup.name
