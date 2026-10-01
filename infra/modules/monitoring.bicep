// Phase 11: the "ATLAS Agent Operations" workbook, the alert rules on the ops action group, and the
// standard availability test against the web app.
//
// Every KQL query lives in infra/queries/*.kql and is pulled in with loadTextContent, so a query
// change is a reviewable diff in one file, and the workbook and alerts cannot drift from it.
//
// A separate module from observability.bicep because it needs outputs (web URL, Service Bus
// namespace) from modules that themselves depend on observability.

@description('Region for the workbook, alert rules and web test.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('8-char deterministic suffix.')
param resourceToken string

@description('Application Insights component id.')
param appInsightsId string

@description('Ops action group id.')
param actionGroupId string

@description('Service Bus namespace id (DLQ metric alert).')
param serviceBusNamespaceId string

@description('Queue whose dead-letter depth is alerted on.')
param commandsQueueName string

@description('Public base URL of the web app.')
param webUrl string

// ------------------------------------------------------------------------------------- workbook
var timeRangeParam = {
  type: 9
  name: 'parameters'
  content: {
    version: 'KqlParameterItem/1.0'
    parameters: [
      {
        id: 'b6a0f2c1-11a1-4f6e-9a00-000000000001'
        version: 'KqlParameterItem/1.0'
        name: 'TimeRange'
        label: 'Time range'
        type: 4
        isRequired: true
        value: { durationMs: 86400000 }
        typeSettings: {
          selectableValues: [
            { durationMs: 3600000 }
            { durationMs: 14400000 }
            { durationMs: 86400000 }
            { durationMs: 259200000 }
            { durationMs: 604800000 }
          ]
        }
      }
    ]
  }
}

func header(name string, markdown string) object => {
  type: 1
  name: name
  content: { json: markdown }
}

func kql(name string, title string, query string, visualization string, sourceId string) object => {
  type: 3
  name: name
  content: {
    version: 'KqlItem/1.0'
    title: title
    query: query
    size: 0
    queryType: 0
    resourceType: 'microsoft.insights/components'
    crossComponentResources: [sourceId]
    timeContextFromParameter: 'TimeRange'
    timeContext: { durationMs: 86400000 }
    visualization: visualization
  }
}

var workbookItems = [
  header('title', '# ATLAS Agent Operations\nOne screen for any episode: what the agents did, what the tools and models cost, what the gate decided, what the worker executed, and what the guardrails caught. Every query is in `infra/queries/` in the repo.')
  timeRangeParam
  header('h1', '## 1. Volume and latency\n`workflow.run` is the agents\' share of a case, intake to plan; the human approval wait is excluded.')
  kql('q01', 'Case analyses and latency (s)', loadTextContent('../queries/01-volume-latency.kql'), 'timechart', appInsightsId)
  kql('q02', 'Latency per node (s)', loadTextContent('../queries/02-node-latency.kql'), 'table', appInsightsId)
  header('h2', '## 2. Tool health')
  kql('q03', 'Calls and latency per tool', loadTextContent('../queries/03-tool-health.kql'), 'table', appInsightsId)
  kql('q04', 'Tool failures per backend', loadTextContent('../queries/04-tool-failures.kql'), 'barchart', appInsightsId)
  header('h3', '## 3. Model usage\nFrom the GenAI chat spans Agent Framework emits. Cost units weight output 4x input: a ranking, not a bill.')
  kql('q05', 'Tokens per agent', loadTextContent('../queries/05-model-usage.kql'), 'table', appInsightsId)
  kql('q06', 'Tokens per case', loadTextContent('../queries/06-model-usage-per-case.kql'), 'table', appInsightsId)
  header('h4', '## 4. Decisions\nThe policy gate is plain code; the model never decides whether it may act.')
  kql('q07', 'Gate and human decisions', loadTextContent('../queries/07-decisions.kql'), 'barchart', appInsightsId)
  kql('q08', 'Approval latency (s)', loadTextContent('../queries/08-approval-latency.kql'), 'tiles', appInsightsId)
  header('h5', '## 5. Remediation')
  kql('q09', 'Dispatched and outcomes', loadTextContent('../queries/09-remediation.kql'), 'barchart', appInsightsId)
  kql('q10', 'Worker deliveries by settlement', loadTextContent('../queries/10-worker-deliveries.kql'), 'table', appInsightsId)
  header('h6', '## 6. Quality\nWritten by `evals/run_eval.py` (Phase 12) as one `eval.run` event per run.')
  kql('q11', 'Latest evaluation scorecards', loadTextContent('../queries/11-quality.kql'), 'table', appInsightsId)
  header('h7', '## 7. Safety')
  kql('q12', 'Guardrail blocks and injection canaries', loadTextContent('../queries/12-safety.kql'), 'table', appInsightsId)
]

// Workbook names must be GUIDs; deterministic so re-provisioning updates in place.
resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'atlas-agent-operations')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: 'ATLAS Agent Operations'
    category: 'workbook'
    sourceId: appInsightsId
    serializedData: string({
      version: 'Notebook/1.0'
      items: workbookItems
      isLocked: false
      fallbackResourceIds: [appInsightsId]
    })
  }
}

// --------------------------------------------------------------------------------- log alerts
// Each query returns rows only when the condition breaches, so every rule is "row count > 0".
var logAlerts = [
  {
    key: 'tool-error-rate'
    title: 'ATLAS tool error rate above 10 %'
    severity: 2
    window: 'PT5M'
    frequency: 'PT5M'
    query: loadTextContent('../queries/alert-tool-error-rate.kql')
  }
  {
    key: 'e2e-latency'
    title: 'ATLAS case analysis p95 above 120 s'
    severity: 3
    window: 'PT10M'
    frequency: 'PT5M'
    query: loadTextContent('../queries/alert-e2e-latency.kql')
  }
  {
    key: 'model-throttling'
    title: 'ATLAS model throttling (429) above 20 in 5 min'
    severity: 2
    window: 'PT5M'
    frequency: 'PT5M'
    query: loadTextContent('../queries/alert-model-throttling.kql')
  }
  {
    key: 'token-burn'
    title: 'ATLAS token burn above 2M per hour'
    severity: 3
    window: 'PT1H'
    frequency: 'PT15M'
    query: loadTextContent('../queries/alert-token-burn.kql')
  }
  {
    key: 'approval-timeouts'
    title: 'ATLAS approvals expiring unanswered'
    severity: 3
    window: 'PT30M'
    frequency: 'PT10M'
    query: loadTextContent('../queries/alert-approval-expired.kql')
  }
  {
    key: 'injection-canary'
    title: 'ATLAS injection canary reached an agent'
    severity: 1
    window: 'PT5M'
    frequency: 'PT5M'
    query: loadTextContent('../queries/alert-injection-canary.kql')
  }
  {
    key: 'eval-regression'
    title: 'ATLAS evaluation composite below baseline by more than 5 %'
    severity: 2
    window: 'P1D'
    frequency: 'PT1H'
    query: loadTextContent('../queries/alert-eval-regression.kql')
  }
]

resource logAlertRules 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [for a in logAlerts: {
  name: 'alert-atlas-${a.key}-${resourceToken}'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: a.title
    description: a.title
    severity: a.severity
    enabled: true
    scopes: [appInsightsId]
    evaluationFrequency: a.frequency
    windowSize: a.window
    autoMitigate: true
    criteria: {
      allOf: [
        {
          query: a.query
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroupId]
    }
  }
}]

// ------------------------------------------------------------------------------ metric alerts
// DLQ depth is a platform metric, not a log: it fires even when every ATLAS process is down.
resource dlqAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-atlas-dlq-depth-${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    description: 'ATLAS remediation command dead-lettered'
    severity: 1
    enabled: true
    scopes: [serviceBusNamespaceId]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          criterionType: 'StaticThresholdCriterion'
          name: 'dlq'
          metricNamespace: 'Microsoft.ServiceBus/namespaces'
          metricName: 'DeadletteredMessages'
          dimensions: [
            { name: 'EntityName', operator: 'Include', values: [commandsQueueName] }
          ]
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Maximum'
        }
      ]
    }
    actions: [
      { actionGroupId: actionGroupId }
    ]
  }
}

// ------------------------------------------------------------------------- availability test
var webTestName = 'webtest-atlas-web-health-${resourceToken}'

resource webTest 'Microsoft.Insights/webtests@2022-06-15' = {
  name: webTestName
  location: location
  // The hidden-link tag is what binds a web test to its Application Insights component.
  tags: union(tags, { 'hidden-link:${appInsightsId}': 'Resource' })
  kind: 'standard'
  properties: {
    SyntheticMonitorId: webTestName
    Name: 'ATLAS web /api/health'
    Description: 'Standard test against the web app health endpoint (excluded from Easy Auth).'
    Enabled: true
    Frequency: 300
    Timeout: 30
    Kind: 'standard'
    RetryEnabled: true
    Locations: [
      { Id: 'us-va-ash-azr' } // East US
      { Id: 'us-ca-sjc-azr' } // West US
      { Id: 'emea-nl-ams-azr' } // West Europe
    ]
    Request: {
      RequestUrl: '${webUrl}/api/health'
      HttpVerb: 'GET'
      ParseDependentRequests: false
    }
    ValidationRules: {
      ExpectedHttpStatusCode: 200
      SSLCheck: true
      SSLCertRemainingLifetimeCheck: 7
    }
  }
}

resource availabilityAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-atlas-web-availability-${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    description: 'ATLAS web health failing from 2 or more regions'
    severity: 1
    enabled: true
    scopes: [webTest.id, appInsightsId]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.WebtestLocationAvailabilityCriteria'
      webTestId: webTest.id
      componentId: appInsightsId
      failedLocationCount: 2
    }
    actions: [
      { actionGroupId: actionGroupId }
    ]
  }
}

output workbookId string = workbook.id
output webTestName string = webTest.name
output alertRuleNames array = concat(map(logAlerts, a => 'alert-atlas-${a.key}-${resourceToken}'), [
  dlqAlert.name
  availabilityAlert.name
])
